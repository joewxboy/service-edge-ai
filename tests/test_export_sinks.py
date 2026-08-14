"""Unit tests for event sinks and the exporter loop."""

import json

import pytest

from edge_ai_monitor.export.event_sinks import (
    OTLPSink,
    SinkError,
    SyslogSink,
    WebhookSink,
    build_event_sink,
)
from edge_ai_monitor.export.exporter import Exporter
from edge_ai_monitor.export.spool import Spool

RECORD = {
    "problem_key": "abc123",
    "workload_id": "examples/sensor_1.2.0_amd64",
    "service_name": "sensor",
    "severity": "high",
    "error_summary": "Database connection refused",
    "total_occurrences": 5,
    "node": {"id": "myorg/edge-1"},
}


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status_code = status
        self.content = body

    def json(self):
        return json.loads(self.content or b"{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")

    @property
    def text(self):
        return self.content.decode()


class FakeRequests:
    """Captures outbound calls instead of making them."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or FakeResponse()
        self.error = error

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response

    class RequestException(Exception):
        pass


@pytest.fixture
def fake_requests(monkeypatch):
    import requests

    fake = FakeRequests()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "request", fake.request)
    return fake


# ---------------- webhook ----------------


def test_webhook_posts_the_record(fake_requests):
    WebhookSink("hook", url="https://example.test/in").send(RECORD, "abc123-5")
    method, url, kwargs = fake_requests.calls[0]
    assert method == "POST" and url == "https://example.test/in"
    assert kwargs["json"]["problem_key"] == "abc123"


def test_webhook_sends_idempotency_key(fake_requests):
    WebhookSink("hook", url="https://example.test/in").send(RECORD, "abc123-5")
    assert fake_requests.calls[0][2]["headers"]["Idempotency-Key"] == "abc123-5"


def test_webhook_sends_bearer_token(fake_requests):
    WebhookSink("hook", url="https://example.test/in", token="s3cret-token").send(RECORD, "k")
    assert fake_requests.calls[0][2]["headers"]["Authorization"] == "Bearer s3cret-token"


def test_webhook_failure_raises_sink_error(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(SinkError):
        WebhookSink("hook", url="https://example.test/in").send(RECORD, "k")


def test_webhook_error_scrubs_the_token(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("failed with token s3cret-token-value")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(SinkError) as excinfo:
        WebhookSink("hook", url="https://x", token="s3cret-token-value").send(RECORD, "k")
    assert "s3cret-token-value" not in str(excinfo.value)


# ---------------- OTLP ----------------


def test_otlp_payload_shape():
    payload = OTLPSink("otlp", endpoint="https://collector.test").build_payload(RECORD)
    logs = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert logs["severityText"] == "HIGH"
    assert logs["body"]["stringValue"] == "Database connection refused"
    assert logs["severityNumber"] == 17


def test_otlp_maps_severity_numbers():
    sink = OTLPSink("otlp", endpoint="https://collector.test")
    numbers = {
        sev: sink.build_payload({**RECORD, "severity": sev})["resourceLogs"][0]["scopeLogs"][0][
            "logRecords"
        ][0]["severityNumber"]
        for sev in ("low", "medium", "high", "critical")
    }
    assert numbers == {"low": 9, "medium": 13, "high": 17, "critical": 21}


def test_otlp_posts_to_v1_logs(fake_requests):
    OTLPSink("otlp", endpoint="https://collector.test/").send(RECORD, "k")
    assert fake_requests.calls[0][1] == "https://collector.test/v1/logs"


def test_otlp_attribute_typing():
    sink = OTLPSink("otlp", endpoint="https://x")
    assert sink._attribute("n", 5)["value"] == {"intValue": "5"}
    assert sink._attribute("b", True)["value"] == {"boolValue": True}
    assert sink._attribute("s", "x")["value"] == {"stringValue": "x"}
    assert "stringValue" in sink._attribute("l", [1, 2])["value"]


# ---------------- syslog ----------------


def test_syslog_message_is_rfc5424():
    message = SyslogSink("sys", host="localhost").build_message(RECORD)
    # <priority>version timestamp host app - msgid - structured
    assert message.startswith("<11>1 ")  # facility 1 * 8 + severity 3 (high)
    assert "myorg/edge-1" in message
    assert "abc123" in message


def test_syslog_severity_mapping():
    sink = SyslogSink("sys", host="localhost")
    priorities = {
        sev: sink.build_message({**RECORD, "severity": sev}).split(">")[0][1:]
        for sev in ("low", "medium", "high", "critical")
    }
    assert priorities == {"low": "13", "medium": "12", "high": "11", "critical": "10"}


def test_syslog_unreachable_host_raises_sink_error():
    sink = SyslogSink("sys", host="localhost", port=1, protocol="tcp", timeout=0.5)
    with pytest.raises(SinkError):
        sink.send(RECORD, "k")


# ---------------- construction ----------------


def test_build_event_sink_by_type():
    sink = build_event_sink("hook", {"type": "webhook", "url": "https://x"})
    assert isinstance(sink, WebhookSink)


def test_build_event_sink_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_event_sink("x", {"type": "carrier-pigeon"})


def test_build_event_sink_ignores_meta_keys():
    sink = build_event_sink("hook", {"type": "webhook", "url": "https://x", "level": "full"})
    assert isinstance(sink, WebhookSink)


# ---------------- exporter loop ----------------


class RecordingSink:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.name = "recording"

    def send(self, record, idempotency_key):
        if self.fail:
            raise SinkError("sink is down")
        self.sent.append((record, idempotency_key))

    def check(self):
        return not self.fail


def make_exporter(tmp_path, sink, **kwargs):
    return Exporter(
        spool=Spool(directory=str(tmp_path / "spool")),
        event_sinks={"recording": sink},
        levels={"recording": "analysis"},
        **kwargs,
    )


PROPOSAL = {
    "severity": "high",
    "confidence": 0.9,
    "error_summary": "Connection refused",
    "root_cause": "Database down",
    "remediation_steps": ["Restart"],
    "metadata": {
        "problem_key": "abc123",
        "workload_id": "examples/sensor_1.2.0_amd64",
        "service_name": "sensor",
        "total_occurrences": 3,
    },
}


def test_submit_spools_without_delivering(tmp_path):
    sink = RecordingSink()
    exporter = make_exporter(tmp_path, sink)
    assert exporter.submit(PROPOSAL) == 1
    assert sink.sent == []          # nothing sent yet
    assert exporter.spool.depth == 1


def test_deliver_once_sends_spooled_records(tmp_path):
    sink = RecordingSink()
    exporter = make_exporter(tmp_path, sink)
    exporter.submit(PROPOSAL)
    assert exporter.deliver_once() == 1
    assert len(sink.sent) == 1
    assert exporter.spool.depth == 0


def test_idempotency_key_is_not_part_of_the_payload(tmp_path):
    sink = RecordingSink()
    exporter = make_exporter(tmp_path, sink)
    exporter.submit(PROPOSAL)
    exporter.deliver_once()
    record, key = sink.sent[0]
    assert "idempotency_key" not in record
    assert key == "abc123-3"


def test_failed_delivery_stays_queued(tmp_path):
    sink = RecordingSink(fail=True)
    exporter = make_exporter(tmp_path, sink)
    exporter.submit(PROPOSAL)
    assert exporter.deliver_once() == 0
    assert exporter.spool.depth == 1


def test_consent_clamps_the_level(tmp_path):
    sink = RecordingSink()
    exporter = make_exporter(tmp_path, sink, consent_provider=lambda w: False)
    exporter.submit(PROPOSAL)
    exporter.deliver_once()
    record, _ = sink.sent[0]
    assert "root_cause" not in record       # clamped to metadata
    assert record["service_name"] == "sensor"


def test_consent_permits_content(tmp_path):
    sink = RecordingSink()
    exporter = make_exporter(tmp_path, sink, consent_provider=lambda w: True)
    exporter.submit(PROPOSAL)
    exporter.deliver_once()
    assert sink.sent[0][0]["root_cause"] == "Database down"


def test_delivery_callback_reports_states(tmp_path):
    events = []
    sink = RecordingSink()
    exporter = make_exporter(
        tmp_path, sink, on_delivery=lambda k, s, st, r: events.append((k, s, st))
    )
    exporter.submit(PROPOSAL)
    exporter.deliver_once()
    assert ("abc123", "recording", "pending") in events
    assert ("abc123", "recording", "delivered") in events


def test_disabled_exporter_does_nothing(tmp_path):
    exporter = Exporter(spool=Spool(directory=str(tmp_path / "spool")))
    assert exporter.enabled is False
    assert exporter.submit(PROPOSAL) == 0


def test_unknown_sink_record_is_discarded(tmp_path):
    exporter = make_exporter(tmp_path, RecordingSink())
    exporter.spool.put("removed-sink", {"problem_key": "x", "idempotency_key": "x-1"})
    exporter.deliver_once()
    assert exporter.spool.depth == 0


def test_sink_raising_unexpectedly_does_not_kill_the_loop(tmp_path):
    class Exploding(RecordingSink):
        def send(self, record, idempotency_key):
            raise RuntimeError("bug in sink")

    exporter = make_exporter(tmp_path, Exploding())
    exporter.submit(PROPOSAL)
    exporter.deliver_once()  # must not raise
    assert exporter.spool.depth == 1


def test_check_reports_per_sink_health(tmp_path):
    exporter = make_exporter(tmp_path, RecordingSink(fail=True))
    assert exporter.check() == {"recording": False}
