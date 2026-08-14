"""Integration tests: proposals flowing through export to sinks.

These wire the real generator, exporter, spool, and sink machinery together,
stubbing only the network boundary.
"""

import json

import pytest

from edge_ai_monitor.error_analyzer import AnalysisResult
from edge_ai_monitor.export.credentials import MissingCredential, resolve_settings
from edge_ai_monitor.export.event_sinks import SinkError
from edge_ai_monitor.export.exporter import Exporter, build_exporter
from edge_ai_monitor.export.lifecycle import IssueLedger, IssueLifecycle
from edge_ai_monitor.export.spool import Spool
from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.remediation import RemediationGenerator

from test_export_issues import FakeTracker


class FlakySink:
    """An event sink that can be taken offline mid-test."""

    def __init__(self):
        self.online = True
        self.received = []
        self.name = "flaky"

    def send(self, record, idempotency_key):
        if not self.online:
            raise SinkError("network is unreachable")
        self.received.append((record, idempotency_key))

    def check(self):
        return self.online


def make_result(**kwargs):
    event = ErrorEvent(
        workload_id=kwargs.pop("workload_id", "examples/sensor_1.2.0_amd64"),
        log_path="/var/log/workloads/sensor/app.log",
        line_number=42,
        line=kwargs.pop("line", "ERROR could not connect to database"),
        matched_pattern="ERROR",
    )
    defaults = dict(
        event=event,
        workload_metadata={"service_name": "sensor", "version": "1.2.0", "organization": "examples"},
        error_summary="Database connection refused",
        root_cause="The database is not accepting connections",
        severity="high",
        remediation_steps=["Restart the database"],
        confidence=0.9,
        occurrences=1,
    )
    defaults.update(kwargs)
    return AnalysisResult(**defaults)


def build_pipeline(tmp_path, sink, level="analysis", consent=True):
    """Generator -> exporter -> sink, as the service wires them."""
    spool = Spool(directory=str(tmp_path / "spool"))
    exporter = Exporter(
        spool=spool,
        event_sinks={"flaky": sink},
        levels={"flaky": level},
        node={"id": "myorg/edge-1"},
        consent_provider=lambda w: consent,
    )
    generator = RemediationGenerator(
        proposal_dir=str(tmp_path / "proposals"),
        on_proposal=exporter.submit,
    )
    exporter.on_delivery = generator.record_delivery
    return generator, exporter


# ---------------- end to end ----------------


def test_proposal_reaches_the_sink(tmp_path):
    sink = FlakySink()
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    exporter.deliver_once()

    assert len(sink.received) == 1
    record, _ = sink.received[0]
    assert record["service_name"] == "sensor"
    assert record["node"]["id"] == "myorg/edge-1"


def test_local_proposal_written_regardless_of_export(tmp_path):
    sink = FlakySink()
    sink.online = False
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    exporter.deliver_once()

    # The local record is the source of truth and must exist either way.
    assert len(list((tmp_path / "proposals").rglob("*.json"))) == 1


def test_delivery_state_recorded_on_the_proposal(tmp_path):
    sink = FlakySink()
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    exporter.deliver_once()

    payload = json.loads(list((tmp_path / "proposals").rglob("*.json"))[0].read_text())
    assert payload["metadata"]["exports"]["flaky"]["status"] == "delivered"


def test_export_state_survives_proposal_updates(tmp_path):
    sink = FlakySink()
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    exporter.deliver_once()
    generator.handle_result(make_result())  # recurrence updates the proposal

    payload = json.loads(list((tmp_path / "proposals").rglob("*.json"))[0].read_text())
    assert "flaky" in payload["metadata"]["exports"]


# ---------------- offline and recovery ----------------


def test_records_queue_while_offline_and_flush_on_reconnect(tmp_path):
    sink = FlakySink()
    sink.online = False
    generator, exporter = build_pipeline(tmp_path, sink)

    # Five distinct problems occur during the outage.
    for i in range(5):
        generator.handle_result(make_result(line=f"ERROR failure of kind {chr(97 + i)}"))
    exporter.deliver_once()

    assert sink.received == []
    assert exporter.spool.depth == 5

    sink.online = True
    # Backoff was applied on the failed attempts, so make them due again.
    _make_all_due(exporter.spool)

    assert exporter.deliver_once() == 5
    assert exporter.spool.depth == 0
    assert len(sink.received) == 5


def _make_all_due(spool):
    """Clear scheduled backoff so queued records are retried immediately."""
    for path in spool.directory.glob("*.json"):
        document = json.loads(path.read_text())
        document["next_attempt"] = 0.0
        path.write_text(json.dumps(document))


def test_no_duplicates_after_reconnection(tmp_path):
    sink = FlakySink()
    sink.online = False
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    for _ in range(3):
        exporter.deliver_once()      # repeated failures while offline
        _make_all_due(exporter.spool)

    sink.online = True
    exporter.deliver_once()
    exporter.deliver_once()          # a second sweep must find nothing left

    assert len(sink.received) == 1


def test_retry_reuses_the_idempotency_key(tmp_path):
    sink = FlakySink()
    sink.online = False
    generator, exporter = build_pipeline(tmp_path, sink)

    generator.handle_result(make_result())
    first_key = exporter.spool.pending()[0].idempotency_key
    exporter.deliver_once()
    _make_all_due(exporter.spool)

    sink.online = True
    exporter.deliver_once()
    assert sink.received[0][1] == first_key


# ---------------- redaction in the pipeline ----------------


def test_consent_denied_clamps_to_metadata(tmp_path):
    sink = FlakySink()
    generator, exporter = build_pipeline(tmp_path, sink, level="full", consent=False)

    generator.handle_result(make_result())
    exporter.deliver_once()

    record, _ = sink.received[0]
    assert "root_cause" not in record
    assert "log_path" not in record
    assert record["total_occurrences"] == 1     # metadata still present


def test_consent_granted_permits_full_content(tmp_path):
    sink = FlakySink()
    generator, exporter = build_pipeline(tmp_path, sink, level="full", consent=True)

    generator.handle_result(make_result())
    exporter.deliver_once()

    record, _ = sink.received[0]
    assert record["root_cause"].startswith("The database")
    assert record["log_path"] == "/var/log/workloads/sensor/app.log"


# ---------------- issue lifecycle through the exporter ----------------


def test_recurring_problem_yields_one_issue(tmp_path):
    tracker = FakeTracker()
    spool = Spool(directory=str(tmp_path / "spool"))
    lifecycle = IssueLifecycle(
        tracker, IssueLedger(str(tmp_path / "issues.json")), update_interval=0.0
    )
    exporter = Exporter(
        spool=spool,
        issue_lifecycles={"tracker": lifecycle},
        levels={"tracker": "analysis"},
        consent_provider=lambda w: True,
    )
    generator = RemediationGenerator(
        proposal_dir=str(tmp_path / "proposals"), on_proposal=exporter.submit
    )

    for _ in range(6):
        generator.handle_result(make_result())
        exporter.deliver_once()

    assert len(tracker.issues) == 1
    assert lifecycle.created_count == 1
    assert lifecycle.updated_count == 5


def test_issue_reference_recorded_on_the_proposal(tmp_path):
    tracker = FakeTracker()
    spool = Spool(directory=str(tmp_path / "spool"))
    lifecycle = IssueLifecycle(tracker, IssueLedger(str(tmp_path / "issues.json")))
    exporter = Exporter(
        spool=spool, issue_lifecycles={"tracker": lifecycle}, consent_provider=lambda w: True
    )
    generator = RemediationGenerator(
        proposal_dir=str(tmp_path / "proposals"), on_proposal=exporter.submit
    )
    exporter.on_delivery = generator.record_delivery

    generator.handle_result(make_result())
    exporter.deliver_once()

    payload = json.loads(list((tmp_path / "proposals").rglob("*.json"))[0].read_text())
    assert payload["metadata"]["exports"]["tracker"]["reference"] == "1"


# ---------------- construction from config ----------------


class ExportSettings:
    """Stands in for the ExportConfig dataclass."""

    def __init__(self, sinks, spool_dir):
        self.sinks = sinks
        self.spool_directory = spool_dir
        self.spool_max_bytes = 1024 * 1024
        self.spool_max_age_seconds = 3600.0


def test_build_exporter_from_config(tmp_path):
    settings = ExportSettings(
        {"hook": {"type": "webhook", "url": "https://example.test", "level": "metadata"}},
        str(tmp_path / "spool"),
    )
    exporter = build_exporter(settings, env={})
    assert exporter.enabled is True
    assert exporter.sink_names == ["hook"]


def test_bad_sink_is_disabled_not_fatal(tmp_path):
    settings = ExportSettings(
        {
            "good": {"type": "webhook", "url": "https://example.test"},
            "bad": {"type": "carrier-pigeon"},
        },
        str(tmp_path / "spool"),
    )
    exporter = build_exporter(settings, env={})
    assert exporter.sink_names == ["good"]


def test_missing_credential_disables_only_that_sink(tmp_path):
    settings = ExportSettings(
        {
            "good": {"type": "webhook", "url": "https://example.test"},
            "needs-token": {"type": "github", "repository": "o/r", "token_env": "ABSENT_VAR"},
        },
        str(tmp_path / "spool"),
    )
    exporter = build_exporter(settings, env={})
    assert exporter.sink_names == ["good"]


def test_credential_resolved_from_environment(tmp_path):
    settings = ExportSettings(
        {"gh": {"type": "github", "repository": "o/r", "token_env": "GH_TOKEN"}},
        str(tmp_path / "spool"),
    )
    exporter = build_exporter(settings, env={"GH_TOKEN": "ghp_secret"})
    assert exporter.sink_names == ["gh"]
    sink = exporter.issue_lifecycles["gh"].sink
    assert "ghp_secret" not in sink.scrub("error mentioning ghp_secret")


def test_credential_resolved_from_file(tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text("filetoken123\n")
    settings, secrets = resolve_settings(
        "gh", {"type": "github", "token_file": str(secret_file)}, {}
    )
    assert settings["token"] == "filetoken123"
    assert secrets == ["filetoken123"]


def test_disabled_sink_is_skipped(tmp_path):
    settings = ExportSettings(
        {"hook": {"type": "webhook", "url": "https://x", "enabled": False}},
        str(tmp_path / "spool"),
    )
    assert build_exporter(settings, env={}).enabled is False
