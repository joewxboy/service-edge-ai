"""Shared pytest fixtures and helpers."""

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Load a JSON fixture from tests/fixtures by file name."""
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


class Queue:
    """Marks a sequence of per-call results for a route.

    A bare ``list`` is a legitimate JSON payload, so queued results need an
    explicit wrapper to stay unambiguous.
    """

    def __init__(self, *results):
        self.results = list(results)

    def next(self):
        # Repeat the final entry once the queue is drained.
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class FakeSession:
    """Session double that replays responses per URL path."""

    def __init__(self, routes=None):
        # routes: {path_suffix: payload | FakeResponse | Exception | Queue}
        self.routes = routes or {}
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for suffix, value in self.routes.items():
            if url.endswith(suffix):
                if isinstance(value, Queue):
                    value = value.next()
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, FakeResponse) else FakeResponse(value)
        raise AssertionError(f"unexpected URL requested: {url}")


@pytest.fixture
def node_status():
    return load_fixture("node.json")


@pytest.fixture
def service_configs():
    return load_fixture("service_config.json")


@pytest.fixture
def service_definitions():
    return load_fixture("service_definitions.json")


@pytest.fixture
def agreements():
    return load_fixture("agreement.json")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep retry/backoff paths fast in tests."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
