"""Unit tests for AnaxClient with mocked HTTP responses."""

import pytest
import requests

from edge_ai_monitor.anax_client import AnaxClient, AnaxError

from conftest import FakeResponse, FakeSession, Queue


def make_client(routes, **kwargs):
    return AnaxClient(session=FakeSession(routes), backoff_factor=0.0, **kwargs)


def test_get_node_status(node_status):
    client = make_client({"/node": node_status})
    assert client.get_node_status()["configstate"]["state"] == "configured"


def test_get_service_configs_unwraps_config_key(service_configs):
    client = make_client({"/service/config": service_configs})
    configs = client.get_service_configs()
    assert len(configs) == 4
    assert configs[0]["url"] == "example.com.sensor"


def test_get_service_configs_accepts_bare_list(service_configs):
    client = make_client({"/service/config": service_configs["config"]})
    assert len(client.get_service_configs()) == 4


def test_get_agreements_returns_active_only(agreements):
    client = make_client({"/agreement": agreements})
    active = client.get_agreements()
    assert len(active) == 3
    assert active[0]["workload_to_run"]["url"] == "example.com.sensor"


def test_get_agreements_handles_missing_active_key():
    client = make_client({"/agreement": {"agreements": {"archived": []}}})
    assert client.get_agreements() == []


def test_retries_then_succeeds(node_status):
    routes = {
        "/node": Queue(
            requests.ConnectionError("refused"),
            FakeResponse(node_status),
        )
    }
    client = make_client(routes)
    assert client.get_node_status()["id"] == "edge-node-01"


def test_raises_anax_error_after_exhausting_retries():
    client = make_client({"/node": requests.ConnectionError("refused")}, max_retries=2)
    with pytest.raises(AnaxError):
        client.get_node_status()


def test_http_error_status_raises():
    client = make_client({"/node": FakeResponse({}, status_code=503)}, max_retries=1)
    with pytest.raises(AnaxError):
        client.get_node_status()


def test_malformed_json_raises():
    client = make_client({"/node": FakeResponse(ValueError("bad json"))}, max_retries=1)
    with pytest.raises(AnaxError):
        client.get_node_status()


def test_is_reachable_reflects_api_state(node_status):
    assert make_client({"/node": node_status}).is_reachable() is True
    unreachable = make_client({"/node": requests.ConnectionError("x")}, max_retries=1)
    assert unreachable.is_reachable() is False
