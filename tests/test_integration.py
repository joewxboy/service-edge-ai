"""Integration tests covering the full detect -> analyse -> propose pipeline.

These wire the real components together with stubbed external services (anax
HTTP and the Ollama runtime) and drive them through the sample log fixtures.
"""

import json
import shutil
import time
from pathlib import Path

import pytest

from edge_ai_monitor.anax_client import AnaxClient
from edge_ai_monitor.error_analyzer import ErrorAnalyzer
from edge_ai_monitor.log_monitor import LogMonitor
from edge_ai_monitor.remediation import RemediationGenerator
from edge_ai_monitor.workload_registry import WorkloadRegistry, WorkloadState

from conftest import FIXTURES, FakeSession

LOG_FIXTURES = FIXTURES / "logs"


class ScriptedLLM:
    """Returns analyses keyed by what the prompt contains."""

    def __init__(self):
        self.calls = []

    def analyze(self, event, metadata, occurrences=1, knowledge=""):
        self.calls.append((event, metadata, occurrences, knowledge))
        line = event.line.lower()
        if "out of memory" in line or "fatal" in line:
            return {
                "error_summary": "Inference process ran out of memory",
                "root_cause": "The 3B model exceeds the container's 4GB memory limit",
                "severity": "critical",
                "remediation_steps": [
                    "Raise the container memory limit to 6GB",
                    "Switch to a smaller quantized model",
                ],
                "confidence": 0.92,
            }
        if "zerodivision" in line or "traceback" in line:
            return {
                "error_summary": "Division by zero while processing a batch",
                "root_cause": "A record had a samples count of zero",
                "severity": "medium",
                "remediation_steps": ["Guard against zero samples before dividing"],
                "confidence": 0.85,
            }
        return {
            "error_summary": "Database connection refused",
            "root_cause": "The database is not accepting connections on port 5432",
            "severity": "high",
            "remediation_steps": ["Verify the database is running", "Check network policy"],
            "confidence": 0.88,
        }


def workload_definition(name, log_path, patterns=None, enabled=True):
    """A local service definition carrying the MONITORING_* opt-in variables."""
    env = [
        f"MONITORING_ENABLED={'true' if enabled else 'false'}",
        f"MONITORING_LOG_PATHS={log_path}",
    ]
    if patterns:
        env.append("MONITORING_ERROR_PATTERNS=" + ",".join(patterns))
    return {
        "specRef": f"example.com.{name}",
        "organization": "examples",
        "version": "1.0.0",
        "arch": "amd64",
        "name": name,
        "deployment": json.dumps(
            {"services": {name: {"image": f"examples/{name}:1.0.0", "environment": env}}}
        ),
    }


def agreement_for(name):
    return {
        "current_agreement_id": f"agreement-{name}",
        "agreement_terminated_time": 0,
        "workload_to_run": {
            "url": f"example.com.{name}",
            "org": "examples",
            "version": "1.0.0",
            "arch": "amd64",
        },
    }


class Pipeline:
    """The real component graph with stubbed anax and LLM at the edges."""

    def __init__(self, tmp_path, definitions, agreements, llm=None):
        self.session = FakeSession(
            {
                "/service/config": {"config": []},
                "/agreement": {"agreements": {"active": agreements, "archived": []}},
                "/service": {
                    "definitions": {"active": definitions, "archived": []}
                },
            }
        )
        self.registry = WorkloadRegistry(
            AnaxClient(session=self.session, backoff_factor=0.0)
        )
        self.llm = llm or ScriptedLLM()
        self.generator = RemediationGenerator(proposal_dir=str(tmp_path / "proposals"))
        self.proposals = []
        self.analyzer = ErrorAnalyzer(
            llm_client=self.llm,
            metadata_provider=self._metadata,
            on_result=self._handle,
        )
        self.monitor = LogMonitor(on_error=self.analyzer.submit)
        self.proposal_dir = tmp_path / "proposals"

    def _metadata(self, workload_id):
        workload = self.registry.get(workload_id)
        return workload.metadata() if workload else {"workload_id": workload_id}

    def _handle(self, result):
        self.proposals.append(self.generator.handle_result(result))

    def discover(self):
        self.registry.poll_once()
        for workload in self.registry.monitorable_workloads():
            self.monitor.watch_workload(workload)

    def drain(self):
        """Read logs, flush context windows, then analyse everything queued."""
        self.monitor.poll_once()
        self.monitor.poll_once()
        while self.analyzer.process_next() is not None:
            pass


def seed_log(tmp_path, fixture_name):
    """Copy a fixture log to a writable path, returning (path, source lines)."""
    source = LOG_FIXTURES / fixture_name
    target = tmp_path / fixture_name
    target.write_text("")  # start empty so tailing sees the content as new
    return target, source.read_text().splitlines()


def append_lines(path, lines):
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


# ---------------- fixtures on disk ----------------


def test_log_fixtures_exist():
    names = {p.name for p in LOG_FIXTURES.glob("*.log")}
    assert names == {
        "database_failure.log",
        "python_traceback.log",
        "fatal_oom.log",
        "clean.log",
    }


# ---------------- workload discovery flow ----------------


def test_discovery_flow_registers_and_watches(tmp_path):
    log, _ = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()

    workloads = pipeline.registry.all_workloads()
    assert len(workloads) == 1
    assert workloads[0].state == WorkloadState.RUNNING
    assert pipeline.monitor.watched_paths == [str(log)]


def test_discovery_flow_respects_opt_out(tmp_path):
    log, _ = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path,
        [workload_definition("sensor", log, enabled=False)],
        [agreement_for("sensor")],
    )
    pipeline.discover()
    assert pipeline.monitor.watched_paths == []


def test_discovery_flow_ignores_workload_without_agreement(tmp_path):
    log, _ = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(tmp_path, [workload_definition("sensor", log)], [])
    pipeline.discover()
    assert pipeline.registry.get(
        "examples/example.com.sensor_1.0.0_amd64"
    ).state == WorkloadState.REGISTERED
    assert pipeline.monitor.watched_paths == []


def test_stopped_workload_is_unwatched(tmp_path):
    log, _ = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()
    assert pipeline.monitor.watched_paths

    pipeline.session.routes["/agreement"] = {"agreements": {"active": [], "archived": []}}
    pipeline.registry.poll_once()
    workload = pipeline.registry.get("examples/example.com.sensor_1.0.0_amd64")
    assert workload.state == WorkloadState.STOPPED
    pipeline.monitor.unwatch_workload(workload.workload_id)
    assert pipeline.monitor.watched_paths == []


# ---------------- log monitoring flow ----------------


def test_monitoring_flow_detects_errors_in_sample_log(tmp_path):
    log, lines = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.monitor.poll_once()
    pipeline.monitor.poll_once()

    # Three identical connection-refused errors aggregate into one queue entry.
    assert pipeline.analyzer.queue_depth == 1


def test_monitoring_flow_ignores_clean_log(tmp_path):
    log, lines = seed_log(tmp_path, "clean.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("gateway", log)], [agreement_for("gateway")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.monitor.poll_once()
    pipeline.monitor.poll_once()
    assert pipeline.analyzer.queue_depth == 0
    assert pipeline.proposals == []


def test_monitoring_flow_handles_multiple_workloads(tmp_path):
    db_log, db_lines = seed_log(tmp_path, "database_failure.log")
    oom_log, oom_lines = seed_log(tmp_path, "fatal_oom.log")
    pipeline = Pipeline(
        tmp_path,
        [workload_definition("sensor", db_log), workload_definition("inference", oom_log)],
        [agreement_for("sensor"), agreement_for("inference")],
    )
    pipeline.discover()
    assert len(pipeline.monitor.watched_paths) == 2

    append_lines(db_log, db_lines)
    append_lines(oom_log, oom_lines)
    pipeline.drain()

    workloads = {p.metadata["service_name"] for p in pipeline.proposals}
    assert workloads == {"sensor", "inference"}


def test_custom_error_patterns_from_service_definition(tmp_path):
    log, lines = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path,
        [workload_definition("sensor", log, patterns=["WARN"])],
        [agreement_for("sensor")],
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    assert pipeline.proposals
    assert all(
        p.metadata["matched_pattern"] == "WARN" for p in pipeline.proposals
    )


# ---------------- error analysis flow ----------------


def test_analysis_flow_produces_severity_and_confidence(tmp_path):
    log, lines = seed_log(tmp_path, "fatal_oom.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("inference", log)], [agreement_for("inference")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    assert pipeline.proposals
    proposal = pipeline.proposals[0]
    assert proposal.severity == "critical"
    assert proposal.confidence == 0.92


def test_analysis_flow_injects_workload_metadata(tmp_path):
    log, lines = seed_log(tmp_path, "python_traceback.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("worker", log)], [agreement_for("worker")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    _, metadata, _, _ = pipeline.llm.calls[0]
    assert metadata["service_name"] == "worker"
    assert metadata["organization"] == "examples"
    assert metadata["state"] == "running"


def test_analysis_flow_aggregates_recurring_errors(tmp_path):
    log, lines = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    # One analysis covering all three identical occurrences.
    assert len(pipeline.llm.calls) == 1
    assert pipeline.llm.calls[0][2] == 3


def test_analysis_flow_sees_surrounding_context(tmp_path):
    log, lines = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    event = pipeline.llm.calls[0][0]
    context = event.context_text()
    assert "connection pool size set to 8" in context  # preceding context
    assert "degraded mode" in context  # trailing context


# ---------------- remediation proposal flow ----------------


def test_proposal_flow_writes_file_per_workload(tmp_path):
    log, lines = seed_log(tmp_path, "fatal_oom.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("inference", log)], [agreement_for("inference")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    written = list(pipeline.proposal_dir.rglob("*.json"))
    assert len(written) == len(pipeline.proposals)
    payload = json.loads(written[0].read_text())
    assert payload["metadata"]["workload_id"] == "examples/example.com.inference_1.0.0_amd64"


def test_proposal_flow_flags_critical_for_human_review(tmp_path):
    log, lines = seed_log(tmp_path, "fatal_oom.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("inference", log)], [agreement_for("inference")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()
    assert pipeline.proposals[0].requires_human_review is True


def test_proposal_flow_marks_routine_medium_confidence_work(tmp_path):
    log, lines = seed_log(tmp_path, "python_traceback.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("worker", log)], [agreement_for("worker")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    # medium severity at 0.85 confidence is routine.
    proposal = next(p for p in pipeline.proposals if p.severity == "medium")
    assert proposal.requires_human_review is False


def test_proposal_flow_includes_disclaimer_and_steps(tmp_path):
    log, lines = seed_log(tmp_path, "database_failure.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("sensor", log)], [agreement_for("sensor")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()

    proposal = pipeline.proposals[0]
    assert "generated by an AI system" in proposal.disclaimer
    assert proposal.remediation_steps[0].startswith("Step 1: ")
    assert proposal.metadata["occurrences"] == 3


def test_proposal_flow_records_analysis_duration(tmp_path):
    log, lines = seed_log(tmp_path, "python_traceback.log")
    pipeline = Pipeline(
        tmp_path, [workload_definition("worker", log)], [agreement_for("worker")]
    )
    pipeline.discover()
    append_lines(log, lines)
    pipeline.drain()
    assert pipeline.proposals[0].metadata["analysis_duration_ms"] >= 0


# ---------------- performance / resource usage ----------------


def test_log_reading_throughput_is_reasonable(tmp_path):
    """10k clean lines should be read well under a second."""
    log = tmp_path / "bulk.log"
    log.write_text("")
    pipeline = Pipeline(
        tmp_path, [workload_definition("bulk", log)], [agreement_for("bulk")]
    )
    pipeline.discover()

    append_lines(log, [f"INFO heartbeat {i}" for i in range(10_000)])
    started = time.monotonic()
    pipeline.monitor.poll_once()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"reading 10k lines took {elapsed:.2f}s"
    assert pipeline.analyzer.queue_depth == 0


def test_buffer_stays_within_configured_limit(tmp_path):
    log = tmp_path / "noisy.log"
    log.write_text("")
    pipeline = Pipeline(
        tmp_path, [workload_definition("noisy", log)], [agreement_for("noisy")]
    )
    pipeline.discover()
    pipeline.monitor.max_buffer_bytes = 4096

    append_lines(log, [f"ERROR distinct failure {chr(97 + i % 26)}{i}" for i in range(5_000)])
    pipeline.monitor.poll_once()

    assert pipeline.monitor._buffer_bytes <= pipeline.monitor.max_buffer_bytes


def test_analysis_queue_never_exceeds_limit(tmp_path):
    log = tmp_path / "flood.log"
    log.write_text("")
    pipeline = Pipeline(
        tmp_path, [workload_definition("flood", log)], [agreement_for("flood")]
    )
    pipeline.discover()
    pipeline.analyzer.max_queue_size = 10

    # Distinct messages so nothing is aggregated away.
    append_lines(log, [f"ERROR failure of kind {chr(97 + i)}" for i in range(26)])
    pipeline.monitor.poll_once()
    pipeline.monitor.poll_once()

    assert pipeline.analyzer.queue_depth <= 10
    assert pipeline.analyzer.dropped_count > 0
