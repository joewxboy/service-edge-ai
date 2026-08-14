"""Unit tests for WorkloadRegistry discovery and state tracking."""

import copy
import json

import pytest

from edge_ai_monitor.anax_client import AnaxClient
from edge_ai_monitor.workload_registry import (
    DEFAULT_ERROR_PATTERNS,
    MonitoringConfig,
    WorkloadRegistry,
    WorkloadState,
    make_workload_id,
)

from conftest import FakeSession

SENSOR = make_workload_id("examples", "example.com.sensor", "1.2.0", "amd64")
LEGACY = make_workload_id("examples", "example.com.legacy", "3.0.0", "amd64")
QUIET = make_workload_id("examples", "example.com.quiet", "0.9.1", "amd64")


EMPTY_DEFINITIONS = {"definitions": {"active": [], "archived": []}}


def make_session(service_configs, agreements, definitions=None):
    return FakeSession(
        {
            "/service/config": service_configs,
            "/agreement": agreements,
            "/service": definitions if definitions is not None else EMPTY_DEFINITIONS,
        }
    )


def make_registry(service_configs, agreements, definitions=None, **kwargs):
    session = make_session(service_configs, agreements, definitions)
    return WorkloadRegistry(AnaxClient(session=session, backoff_factor=0.0), **kwargs)


# ---------------- monitoring opt-in parsing ----------------


def deployment_with(env, service="app"):
    return {"services": {service: {"image": "x:1", "environment": env}}}


def test_extracts_monitoring_from_env_vars():
    config = MonitoringConfig.from_env(
        {
            "MONITORING_ENABLED": "true",
            "MONITORING_LOG_PATHS": "/var/log/a.log",
            "MONITORING_ERROR_PATTERNS": "ERROR,SensorFault",
        }
    )
    assert config.enabled is True
    assert config.log_paths == ["/var/log/a.log"]
    assert config.error_patterns == ["ERROR", "SensorFault"]


def test_absent_enabled_var_is_not_eligible():
    assert MonitoringConfig.from_env({}).enabled is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "Yes"])
def test_truthy_enabled_values(value):
    assert MonitoringConfig.from_env({"MONITORING_ENABLED": value}).enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
def test_falsey_enabled_values(value):
    assert MonitoringConfig.from_env({"MONITORING_ENABLED": value}).enabled is False


def test_comma_separated_log_paths():
    config = MonitoringConfig.from_env(
        {"MONITORING_ENABLED": "true", "MONITORING_LOG_PATHS": "/a.log, /b.log ,/c.log"}
    )
    assert config.log_paths == ["/a.log", "/b.log", "/c.log"]


def test_json_array_preserves_patterns_containing_commas():
    config = MonitoringConfig.from_env(
        {
            "MONITORING_ENABLED": "true",
            "MONITORING_LOG_PATHS": "/a.log",
            "MONITORING_ERROR_PATTERNS": '["retry a{1,3} failed", "FATAL"]',
        }
    )
    assert config.error_patterns == ["retry a{1,3} failed", "FATAL"]


def test_malformed_json_disables_monitoring():
    config = MonitoringConfig.from_env(
        {"MONITORING_ENABLED": "true", "MONITORING_LOG_PATHS": "[/unclosed"}
    )
    assert config.enabled is False


def test_empty_error_patterns_fall_back_to_defaults():
    config = MonitoringConfig.from_env(
        {"MONITORING_ENABLED": "true", "MONITORING_LOG_PATHS": "/a.log"}
    )
    assert config.error_patterns == DEFAULT_ERROR_PATTERNS


def test_from_deployment_accepts_json_string():
    deployment = json.dumps(
        deployment_with(["MONITORING_ENABLED=true", "MONITORING_LOG_PATHS=/a.log"])
    )
    assert MonitoringConfig.from_deployment(deployment).log_paths == ["/a.log"]


def test_from_deployment_accepts_dict():
    deployment = deployment_with(
        ["MONITORING_ENABLED=true", "MONITORING_LOG_PATHS=/a.log"]
    )
    assert MonitoringConfig.from_deployment(deployment).enabled is True


def test_from_deployment_handles_missing_and_invalid_input():
    assert MonitoringConfig.from_deployment(None).enabled is False
    assert MonitoringConfig.from_deployment("not json").enabled is False
    assert MonitoringConfig.from_deployment({"no_services": True}).enabled is False


def test_from_deployment_combines_paths_across_containers():
    deployment = {
        "services": {
            "app": {
                "environment": [
                    "MONITORING_ENABLED=true",
                    "MONITORING_LOG_PATHS=/app.log",
                ]
            },
            "sidecar": {"environment": ["MONITORING_LOG_PATHS=/sidecar.log"]},
        }
    }
    config = MonitoringConfig.from_deployment(deployment)
    assert config.enabled is True
    assert set(config.log_paths) == {"/app.log", "/sidecar.log"}


def test_env_value_containing_equals_is_preserved():
    config = MonitoringConfig.from_deployment(
        deployment_with(
            ["MONITORING_ENABLED=true", "MONITORING_ERROR_PATTERNS=key=value"]
        )
    )
    assert config.error_patterns == ["key=value"]


# ---------------- discovery ----------------


def test_poll_discovers_all_services(service_configs, agreements, service_definitions):
    registry = make_registry(service_configs, agreements, service_definitions)
    workloads = registry.poll_once()
    assert len(workloads) == 4


def test_running_state_requires_active_agreement(service_configs, agreements, service_definitions):
    registry = make_registry(service_configs, agreements, service_definitions)
    registry.poll_once()
    assert registry.get(SENSOR).state == WorkloadState.RUNNING
    # legacy has no agreement in the fixture
    assert registry.get(LEGACY).state == WorkloadState.REGISTERED


def test_only_opted_in_running_workloads_are_monitorable(service_configs, agreements, service_definitions):
    registry = make_registry(service_configs, agreements, service_definitions)
    registry.poll_once()
    ids = {w.workload_id for w in registry.monitorable_workloads()}
    # sensor and defaults opted in and are running; quiet opted out; legacy has
    # no monitoring section and no agreement.
    assert SENSOR in ids
    assert QUIET not in ids
    assert LEGACY not in ids


def test_terminated_agreement_does_not_mark_running(service_configs, agreements):
    terminated = copy.deepcopy(agreements)
    terminated["agreements"]["active"][0]["agreement_terminated_time"] = 1786000999
    registry = make_registry(service_configs, terminated)
    registry.poll_once()
    assert registry.get(SENSOR).state == WorkloadState.REGISTERED


def test_running_workload_losing_agreement_becomes_stopped(service_configs, agreements):
    session = make_session(service_configs, agreements)
    registry = WorkloadRegistry(AnaxClient(session=session, backoff_factor=0.0))
    registry.poll_once()
    assert registry.get(SENSOR).state == WorkloadState.RUNNING

    session.routes["/agreement"] = {"agreements": {"active": [], "archived": []}}
    registry.poll_once()
    assert registry.get(SENSOR).state == WorkloadState.STOPPED


def test_workload_absent_from_response_is_marked_removed(service_configs, agreements):
    session = make_session(service_configs, agreements)
    registry = WorkloadRegistry(AnaxClient(session=session, backoff_factor=0.0))
    registry.poll_once()

    trimmed = {"config": [c for c in service_configs["config"] if c["name"] != "legacy"]}
    session.routes["/service/config"] = trimmed
    registry.poll_once()
    assert registry.get(LEGACY).state == WorkloadState.REMOVED


def test_state_transitions_invoke_callback(service_configs, agreements):
    events = []
    session = make_session(service_configs, agreements)
    registry = WorkloadRegistry(
        AnaxClient(session=session, backoff_factor=0.0),
        on_change=lambda w, prev: events.append((w.workload_id, prev, w.state)),
    )
    registry.poll_once()
    assert len(events) == 4  # one per newly discovered workload

    events.clear()
    session.routes["/agreement"] = {"agreements": {"active": [], "archived": []}}
    registry.poll_once()
    changed = {e[0] for e in events}
    assert SENSOR in changed


def test_repeat_poll_is_idempotent(service_configs, agreements):
    registry = make_registry(service_configs, agreements)
    registry.poll_once()
    first_seen = registry.get(SENSOR).first_seen
    registry.poll_once()
    assert len(registry.all_workloads()) == 4
    assert registry.get(SENSOR).first_seen == first_seen


def test_anax_failure_does_not_crash_poll(service_configs, agreements):
    import requests

    session = FakeSession(
        {
            "/service/config": requests.ConnectionError("down"),
            "/agreement": agreements,
            "/service": EMPTY_DEFINITIONS,
        }
    )
    registry = WorkloadRegistry(
        AnaxClient(session=session, backoff_factor=0.0, max_retries=1)
    )
    assert registry.poll_once() == []


def test_service_config_without_url_is_skipped():
    no_agreements = {"agreements": {"active": [], "archived": []}}
    registry = make_registry(
        {"config": [{"org": "examples", "version": "1.0.0"}]}, no_agreements
    )
    assert registry.poll_once() == []


def test_definition_name_field_is_not_used_as_service_name():
    """anax reports "name" as the full workload id, which would corrupt output."""
    definitions = {
        "definitions": {
            "active": [
                {
                    "specRef": "sample-monitored-workload",
                    "organization": "myorg",
                    "version": "1.0.0",
                    "arch": "amd64",
                    "name": "myorg/sample-monitored-workload_1.0.0_amd64",
                    "deployment": json.dumps(
                        deployment_with(
                            ["MONITORING_ENABLED=true", "MONITORING_LOG_PATHS=/a.log"],
                            service="sample-monitored-workload",
                        )
                    ),
                }
            ],
            "archived": [],
        }
    }
    registry = make_registry(
        {"config": []}, {"agreements": {"active": [], "archived": []}}, definitions
    )
    registry.poll_once()
    workload = registry.all_workloads()[0]
    assert workload.name == "sample-monitored-workload"
    assert workload.metadata()["service_name"] == "sample-monitored-workload"


def test_workload_known_only_from_agreement_is_discovered():
    """A running workload with no /service/config entry must still be seen."""
    configs = {"config": []}
    agreements = {
        "agreements": {
            "active": [
                {
                    "current_agreement_id": "abc123",
                    "agreement_terminated_time": 0,
                    "workload_to_run": {
                        "url": "ibm.helloworld",
                        "org": "IBM",
                        "version": "1.0.0",
                        "arch": "amd64",
                    },
                }
            ],
            "archived": [],
        }
    }
    registry = make_registry(configs, agreements)
    workloads = registry.poll_once()

    assert len(workloads) == 1
    assert workloads[0].state == WorkloadState.RUNNING
    assert workloads[0].name == "helloworld"
    # No service definition means no monitoring opt-in, so it is not monitored.
    assert workloads[0].is_monitorable is False


def test_definition_opt_in_survives_agreement_merge(service_configs, agreements, service_definitions):
    """The same workload in every source keeps its monitoring opt-in."""
    registry = make_registry(service_configs, agreements, service_definitions)
    registry.poll_once()
    sensor = registry.get(SENSOR)
    assert sensor.state == WorkloadState.RUNNING
    assert sensor.monitoring.enabled is True
    assert sensor.is_monitorable is True


def test_workload_metadata_includes_service_identity(service_configs, agreements, service_definitions):
    registry = make_registry(service_configs, agreements, service_definitions)
    registry.poll_once()
    metadata = registry.get(SENSOR).metadata()
    assert metadata["service_name"] == "sensor"
    assert metadata["organization"] == "examples"
    assert metadata["version"] == "1.2.0"
    assert metadata["state"] == "running"


# ---------------- export consent ----------------


def test_export_consent_defaults_to_false():
    config = MonitoringConfig.from_env(
        {"MONITORING_ENABLED": "true", "MONITORING_LOG_PATHS": "/a.log"}
    )
    assert config.export_content is False


def test_export_consent_when_explicitly_granted():
    config = MonitoringConfig.from_env(
        {"MONITORING_ENABLED": "true", "MONITORING_LOG_PATHS": "/a.log",
         "MONITORING_EXPORT": "true"}
    )
    assert config.export_content is True


def test_export_consent_read_from_deployment():
    config = MonitoringConfig.from_deployment(
        deployment_with([
            "MONITORING_ENABLED=true",
            "MONITORING_LOG_PATHS=/a.log",
            "MONITORING_EXPORT=yes",
        ])
    )
    assert config.export_content is True


def test_workload_exposes_export_consent(service_configs, agreements, service_definitions):
    registry = make_registry(service_configs, agreements, service_definitions)
    registry.poll_once()
    # The fixture workloads do not opt into export.
    assert registry.get(SENSOR).permits_content_export is False
