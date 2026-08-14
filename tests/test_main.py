"""Unit tests for service orchestration, health endpoint, and CLI."""

import json
import urllib.error
import urllib.request

import pytest

from edge_ai_monitor.config import Config
from edge_ai_monitor.health import HealthServer
from edge_ai_monitor.main import MonitorService, main, parse_args


def make_config(tmp_path, **overrides):
    config = Config()
    config.proposals.directory = str(tmp_path / "proposals")
    config.health.enabled = False
    config.discovery.poll_interval = 0.05
    config.logs.poll_interval = 0.05
    for dotted, value in overrides.items():
        section, key = dotted.split(".")
        setattr(getattr(config, section), key, value)
    return config


class StubAnax:
    def __init__(self, reachable=True, configs=None, agreements=None, definitions=None):
        self.reachable = reachable
        self.configs = configs or []
        self.agreements = agreements or []
        self.definitions = definitions or []

    def is_reachable(self):
        return self.reachable

    def get_service_configs(self):
        return self.configs

    def get_agreements(self):
        return self.agreements

    def get_service_definitions(self):
        return self.definitions


class StubLLM:
    def __init__(self, available=True, model_ok=True):
        self.available = available
        self.model_ok = model_ok

    def is_available(self):
        return self.available

    def ensure_model(self):
        return self.model_ok

    def analyze(self, event, metadata, occurrences=1, knowledge=""):
        return {
            "error_summary": "Connection refused",
            "root_cause": "Database down",
            "severity": "high",
            "remediation_steps": ["Restart the database"],
            "confidence": 0.9,
        }


def build_service(tmp_path, anax=None, llm=None, **overrides):
    service = MonitorService(make_config(tmp_path, **overrides))
    service.anax_client = anax or StubAnax()
    service.llm_client = llm or StubLLM()
    service.registry.anax_client = service.anax_client
    service.analyzer.llm_client = service.llm_client
    return service


# ---------------- startup validation ----------------


def test_startup_validation_passes_when_dependencies_up(tmp_path):
    service = build_service(tmp_path)
    assert service.validate_startup() is True
    assert service.startup_errors == []


def test_unreachable_anax_is_reported_not_fatal(tmp_path):
    service = build_service(tmp_path, anax=StubAnax(reachable=False))
    assert service.validate_startup() is False
    assert any("anax" in e for e in service.startup_errors)


def test_unavailable_ollama_is_reported(tmp_path):
    service = build_service(tmp_path, llm=StubLLM(available=False))
    assert service.validate_startup() is False
    assert any("Ollama" in e for e in service.startup_errors)


def test_model_pull_failure_is_reported(tmp_path):
    service = build_service(tmp_path, llm=StubLLM(model_ok=False))
    assert service.validate_startup() is False
    assert any("model" in e for e in service.startup_errors)


def test_auto_pull_disabled_skips_model_check(tmp_path):
    service = build_service(tmp_path, llm=StubLLM(model_ok=False))
    service.config.llm.auto_pull = False
    assert service.validate_startup() is True


# ---------------- component wiring ----------------


def test_discovered_workload_gets_watched(tmp_path, service_configs, agreements):
    log = tmp_path / "sensor.log"
    log.write_text("")
    definitions = [
        {
            "specRef": "example.com.sensor",
            "organization": "examples",
            "version": "1.2.0",
            "arch": "amd64",
            "name": "sensor",
            "deployment": json.dumps(
                {
                    "services": {
                        "sensor": {
                            "image": "examples/sensor:1.2.0",
                            "environment": [
                                "MONITORING_ENABLED=true",
                                f"MONITORING_LOG_PATHS={log}",
                            ],
                        }
                    }
                }
            ),
        }
    ]
    service = build_service(
        tmp_path,
        anax=StubAnax(
            definitions=definitions, agreements=agreements["agreements"]["active"]
        ),
    )
    service.registry.poll_once()
    for workload in service.registry.monitorable_workloads():
        service.log_monitor.watch_workload(workload)

    assert service.log_monitor.watched_paths == [str(log)]


def test_end_to_end_error_becomes_persisted_proposal(tmp_path, agreements):
    log = tmp_path / "sensor.log"
    log.write_text("")
    definitions = [
        {
            "specRef": "example.com.sensor",
            "organization": "examples",
            "version": "1.2.0",
            "arch": "amd64",
            "name": "sensor",
            "deployment": json.dumps(
                {
                    "services": {
                        "sensor": {
                            "image": "examples/sensor:1.2.0",
                            "environment": [
                                "MONITORING_ENABLED=true",
                                f"MONITORING_LOG_PATHS={log}",
                            ],
                        }
                    }
                }
            ),
        }
    ]
    service = build_service(
        tmp_path,
        anax=StubAnax(
            definitions=definitions, agreements=agreements["agreements"]["active"]
        ),
    )
    service.registry.poll_once()
    for workload in service.registry.monitorable_workloads():
        service.log_monitor.watch_workload(workload)

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("ERROR could not connect to database\n")

    service.log_monitor.poll_once()
    service.log_monitor.poll_once()  # flush trailing-context window
    assert service.analyzer.queue_depth == 1

    result = service.analyzer.process_next()
    assert result.severity == "high"

    written = list((tmp_path / "proposals").rglob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["metadata"]["service_name"] == "sensor"
    assert payload["requires_human_review"] is True  # high severity


def test_metadata_provider_falls_back_for_unknown_workload(tmp_path):
    service = build_service(tmp_path)
    assert service._workload_metadata("ghost")["workload_id"] == "ghost"


# ---------------- status ----------------


def test_status_reports_counters(tmp_path):
    service = build_service(tmp_path)
    status = service.status()
    assert status["healthy"] is True
    assert status["workloads_discovered"] == 0
    assert status["analysis_queue_depth"] == 0
    assert status["proposals_written"] == 0


def test_status_reports_startup_errors(tmp_path):
    service = build_service(tmp_path, anax=StubAnax(reachable=False))
    service.validate_startup()
    assert service.status()["startup_errors"]


def test_status_unhealthy_after_shutdown(tmp_path):
    service = build_service(tmp_path)
    service.stop()
    assert service.status()["healthy"] is False


# ---------------- health endpoint ----------------


def test_health_endpoint_serves_status():
    server = HealthServer(lambda: {"healthy": True, "workloads_discovered": 3}, port=0)
    server.start()
    port = server._server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["workloads_discovered"] == 3
    finally:
        server.stop()


def test_health_endpoint_returns_503_when_unhealthy():
    server = HealthServer(lambda: {"healthy": False}, port=0)
    server.start()
    port = server._server.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        assert excinfo.value.code == 503
    finally:
        server.stop()


def test_health_endpoint_404s_unknown_path():
    server = HealthServer(lambda: {"healthy": True}, port=0)
    server.start()
    port = server._server.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert excinfo.value.code == 404
    finally:
        server.stop()


def test_health_server_bind_failure_is_not_fatal():
    first = HealthServer(lambda: {"healthy": True}, port=0)
    first.start()
    port = first._server.server_address[1]
    try:
        second = HealthServer(lambda: {"healthy": True}, port=port)
        second.start()  # must not raise
        assert second._server is None
    finally:
        first.stop()


# ---------------- lifecycle ----------------


def test_start_and_stop_are_clean(tmp_path):
    service = build_service(tmp_path)
    service.start()
    service.stop()
    assert service.status()["healthy"] is False


def test_stop_is_idempotent(tmp_path):
    service = build_service(tmp_path)
    service.start()
    service.stop()
    service.stop()  # must not raise


def test_request_shutdown_releases_run_forever(tmp_path):
    import threading

    service = build_service(tmp_path)
    thread = threading.Thread(target=service.run_forever, daemon=True)
    thread.start()
    service.request_shutdown()
    thread.join(timeout=10)
    assert thread.is_alive() is False


# ---------------- CLI ----------------


def test_parse_args_defaults():
    args = parse_args([])
    assert args.config is None
    assert args.check is False


def test_parse_args_accepts_config_and_check():
    args = parse_args(["--config", "/tmp/c.yaml", "--check"])
    assert args.config == "/tmp/c.yaml"
    assert args.check is True


def test_main_returns_2_on_invalid_config(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("discovery:\n  poll_interval: -1\n")
    assert main(["--config", str(bad)]) == 2
