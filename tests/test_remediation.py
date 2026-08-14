"""Unit tests for remediation proposal generation and persistence."""

import json

import pytest

from edge_ai_monitor.error_analyzer import AnalysisResult
from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.remediation import (
    DISCLAIMER,
    RemediationGenerator,
    problem_key,
    requires_human_review,
)

METADATA = {
    "service_name": "sensor",
    "version": "1.2.0",
    "organization": "examples",
}


def make_result(**kwargs):
    event = ErrorEvent(
        workload_id=kwargs.pop("workload_id", "examples/example.com.sensor_1.2.0_amd64"),
        log_path=kwargs.pop("log_path", "/var/log/workloads/sensor/app.log"),
        line_number=kwargs.pop("line_number", 42),
        line="ERROR could not connect to database",
        matched_pattern="ERROR",
    )
    defaults = dict(
        event=event,
        workload_metadata=dict(METADATA),
        error_summary="Database connection refused",
        root_cause="The postgres container is not listening",
        severity="high",
        remediation_steps=["Check the database container", "Restart the service"],
        confidence=0.9,
        occurrences=1,
        analysis_duration_ms=1234,
    )
    defaults.update(kwargs)
    return AnalysisResult(**defaults)


# ---------------- human review policy ----------------


@pytest.mark.parametrize(
    "severity,confidence,expected",
    [
        ("critical", 0.95, True),   # critical always reviewed
        ("low", 0.55, True),        # confidence below 0.6
        ("medium", 0.5, True),
        ("low", 0.9, False),        # routine
        ("medium", 0.85, False),    # routine
        ("high", 0.95, True),       # high severity still reviewed
        ("medium", 0.7, True),      # mid confidence
    ],
)
def test_human_review_policy(severity, confidence, expected):
    assert requires_human_review(severity, confidence) is expected


def test_critical_proposal_flags_review():
    proposal = RemediationGenerator().generate(make_result(severity="critical", confidence=0.95))
    assert proposal.requires_human_review is True


def test_routine_proposal_does_not_flag_review():
    proposal = RemediationGenerator().generate(make_result(severity="low", confidence=0.9))
    assert proposal.requires_human_review is False


# ---------------- proposal structure ----------------


def test_proposal_contains_all_required_fields():
    proposal = RemediationGenerator().generate(make_result())
    payload = proposal.to_dict()
    for field in (
        "error_summary",
        "root_cause",
        "severity",
        "remediation_steps",
        "confidence",
        "requires_human_review",
        "disclaimer",
        "metadata",
    ):
        assert field in payload


def test_every_proposal_includes_disclaimer():
    proposal = RemediationGenerator().generate(make_result())
    assert proposal.to_dict()["disclaimer"] == DISCLAIMER


def test_proposal_serialises_to_valid_json():
    proposal = RemediationGenerator().generate(make_result())
    parsed = json.loads(proposal.to_json())
    assert parsed["severity"] == "high"
    assert isinstance(parsed["remediation_steps"], list)


# ---------------- step generation ----------------


def test_steps_are_ordered_and_numbered():
    proposal = RemediationGenerator().generate(make_result())
    assert proposal.remediation_steps[0].startswith("Step 1: ")
    assert proposal.remediation_steps[1].startswith("Step 2: ")
    assert "Check the database container" in proposal.remediation_steps[0]


def test_single_step_remediation_is_supported():
    proposal = RemediationGenerator().generate(
        make_result(remediation_steps=["Restart the service"])
    )
    assert "Restart the service" in proposal.remediation_steps[0]


def test_verification_step_is_appended_with_workload_identity():
    proposal = RemediationGenerator().generate(make_result())
    final = proposal.remediation_steps[-1]
    assert "examples/sensor v1.2.0" in final
    assert "/var/log/workloads/sensor/app.log" in final


def test_low_confidence_adds_diagnostic_steps():
    proposal = RemediationGenerator().generate(make_result(confidence=0.3))
    joined = " ".join(proposal.remediation_steps)
    assert "hzn service list" in joined
    assert proposal.requires_human_review is True


def test_high_confidence_omits_diagnostic_steps():
    proposal = RemediationGenerator().generate(make_result(confidence=0.95, severity="low"))
    assert "hzn service list" not in " ".join(proposal.remediation_steps)


def test_empty_steps_fall_back_to_diagnostics():
    proposal = RemediationGenerator().generate(make_result(remediation_steps=[]))
    assert len(proposal.remediation_steps) > 1


# ---------------- metadata ----------------


def test_metadata_includes_required_fields():
    proposal = RemediationGenerator().generate(make_result())
    metadata = proposal.metadata
    assert metadata["service_name"] == "sensor"
    assert metadata["service_version"] == "1.2.0"
    assert metadata["log_path"] == "/var/log/workloads/sensor/app.log"
    assert metadata["line_number"] == 42
    assert metadata["analysis_duration_ms"] == 1234


def test_metadata_timestamp_is_iso8601():
    from datetime import datetime

    proposal = RemediationGenerator().generate(make_result())
    # Raises if the timestamp is not ISO 8601.
    datetime.fromisoformat(proposal.metadata["timestamp"])


def test_metadata_records_occurrence_count():
    proposal = RemediationGenerator().generate(make_result(occurrences=7))
    assert proposal.metadata["occurrences"] == 7


# ---------------- persistence ----------------


def test_proposal_written_to_workload_problem_key_path(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    proposal = generator.generate(make_result())
    path = generator.persist(proposal)

    assert path is not None
    assert path.exists()
    assert path.parent.name == "examples_example.com.sensor_1.2.0_amd64"
    assert path.suffix == ".json"
    assert generator.written_count == 1


def test_written_proposal_is_readable_json(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    path = generator.persist(generator.generate(make_result()))
    payload = json.loads(path.read_text())
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["metadata"]["workload_id"].startswith("examples/")


def test_no_temp_files_left_behind(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.persist(generator.generate(make_result()))
    assert list(tmp_path.rglob("*.tmp")) == []


def test_storage_failure_falls_back_to_stdout(tmp_path):
    # Point the proposal dir at a path that cannot be created.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    generator = RemediationGenerator(proposal_dir=str(blocker / "proposals"))

    proposal = generator.generate(make_result())
    assert generator.persist(proposal) is None
    assert generator.storage_failures == 1


def test_handle_result_generates_and_persists(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    proposal = generator.handle_result(make_result())
    assert proposal.severity == "high"
    assert generator.written_count == 1


def test_workload_id_is_sanitised_for_filesystem(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    result = make_result(workload_id="org/with spaces/and:colons")
    path = generator.persist(generator.generate(result))
    assert "/" not in path.parent.name
    assert ":" not in path.name


# ---------------- deduplication: one file per problem ----------------


def test_recurring_error_updates_one_file(tmp_path):
    """A recurring error must not accumulate one proposal file per occurrence."""
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    for _ in range(10):
        generator.handle_result(make_result())

    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1


def test_occurrences_accumulate_across_recurrences(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result(occurrences=3))
    generator.handle_result(make_result(occurrences=2))

    payload = json.loads(list(tmp_path.rglob("*.json"))[0].read_text())
    assert payload["metadata"]["total_occurrences"] == 5
    assert payload["metadata"]["analysis_count"] == 2  # both were real analyses


def test_first_seen_is_preserved_and_last_seen_advances(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    first = make_result()
    first.event.timestamp = "2026-08-14T10:00:00+00:00"
    generator.handle_result(first)

    later = make_result()
    later.event.timestamp = "2026-08-14T12:00:00+00:00"
    generator.handle_result(later)

    metadata = json.loads(list(tmp_path.rglob("*.json"))[0].read_text())["metadata"]
    assert metadata["first_seen"] == "2026-08-14T10:00:00+00:00"
    assert metadata["last_seen"] == "2026-08-14T12:00:00+00:00"


def test_distinct_problems_get_distinct_files(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result())

    other = make_result()
    other.event.line = "ERROR disk full on /var"
    generator.handle_result(other)

    assert len(list(tmp_path.rglob("*.json"))) == 2


def test_problem_key_ignores_embedded_numbers(tmp_path):
    """Timestamps and counters must not fragment one problem into many files."""
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    for i in range(5):
        result = make_result()
        result.event.line = f"2026-08-14T12:0{i}:00Z ERROR retry {i} of 5 failed"
        generator.handle_result(result)

    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_latest_analysis_replaces_the_previous_one(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result(root_cause="First guess", severity="low"))
    generator.handle_result(make_result(root_cause="Better guess", severity="high"))

    payload = json.loads(list(tmp_path.rglob("*.json"))[0].read_text())
    assert payload["root_cause"] == "Better guess"
    assert payload["severity"] == "high"


def test_cache_hits_do_not_inflate_analysis_count(tmp_path):
    """analysis_count tracks real inferences, not file writes."""
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result())                      # real analysis
    for _ in range(5):
        generator.handle_result(make_result(from_cache=True))   # cache hits

    metadata = json.loads(list(tmp_path.rglob("*.json"))[0].read_text())["metadata"]
    assert metadata["analysis_count"] == 1
    assert metadata["total_occurrences"] == 6


def test_create_and_update_are_counted_separately(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result())
    generator.handle_result(make_result())
    generator.handle_result(make_result())

    assert generator.written_count == 1
    assert generator.updated_count == 2


def test_corrupt_existing_file_is_replaced_not_trusted(tmp_path):
    generator = RemediationGenerator(proposal_dir=str(tmp_path))
    generator.handle_result(make_result())
    path = list(tmp_path.rglob("*.json"))[0]
    path.write_text("{ not valid json")

    generator.handle_result(make_result(occurrences=4))
    payload = json.loads(path.read_text())
    assert payload["metadata"]["total_occurrences"] == 4
    assert payload["metadata"]["analysis_count"] == 1


def test_problem_key_is_stable_and_short():
    key = problem_key(make_result())
    assert key == problem_key(make_result())
    assert len(key) == 16


def test_concurrent_writers_do_not_lose_occurrences(tmp_path):
    """The analyzer thread and the cache-hit path both persist proposals."""
    import threading

    generator = RemediationGenerator(proposal_dir=str(tmp_path))

    def worker():
        for _ in range(20):
            generator.handle_result(make_result(occurrences=1))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    payload = json.loads(list(tmp_path.rglob("*.json"))[0].read_text())
    assert payload["metadata"]["total_occurrences"] == 80
    assert len(list(tmp_path.rglob("*.json"))) == 1
