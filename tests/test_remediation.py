"""Unit tests for remediation proposal generation and persistence."""

import json

import pytest

from edge_ai_monitor.error_analyzer import AnalysisResult
from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.remediation import (
    DISCLAIMER,
    RemediationGenerator,
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


def test_proposal_written_to_workload_timestamp_path(tmp_path):
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
