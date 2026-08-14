"""Event sinks: fire-and-forget delivery of records to observability systems.

Standards rather than vendor SDKs. OTLP reaches most single-pane-of-glass
platforms through their existing collectors, MQTT is the lingua franca of edge
estates, and syslog and webhooks cover nearly everything else — all over HTTP or
a socket, keeping the image small enough for an edge device.
"""

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0

# RFC 5424 severity for each proposal severity.
SYSLOG_SEVERITY = {"critical": 2, "high": 3, "medium": 4, "low": 5}
SYSLOG_FACILITY = 1  # user-level
DEFAULT_SYSLOG_PORT = 514


class SinkError(Exception):
    """Delivery failed. Raised so the spool schedules a retry."""


def _scrub(text: str, secrets: Any) -> str:
    """Remove credential material from text destined for a log."""
    result = str(text)
    for secret in secrets or []:
        if secret and len(str(secret)) >= 4:
            result = result.replace(str(secret), "***")
    return result


class EventSink:
    """Base class for streaming sinks."""

    kind = "event"

    def __init__(self, name: str, timeout: float = DEFAULT_TIMEOUT, **_: Any):
        self.name = name
        self.timeout = timeout
        self._secrets: list = []

    def send(self, record: Dict[str, Any], idempotency_key: str) -> None:
        raise NotImplementedError

    def check(self) -> bool:
        """Best-effort reachability probe for --check. Never raises."""
        return True

    def scrub(self, text: Any) -> str:
        return _scrub(text, self._secrets)


class WebhookSink(EventSink):
    """POST the record as JSON to an arbitrary endpoint."""

    def __init__(
        self,
        name: str,
        url: str,
        token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        **_: Any,
    ):
        super().__init__(name, timeout)
        self.url = url
        self.token = token
        self.headers = dict(headers or {})
        self._secrets = [token] if token else []

    def send(self, record: Dict[str, Any], idempotency_key: str) -> None:
        import requests

        headers = {
            "Content-Type": "application/json",
            # Lets a well-behaved receiver dedupe our at-least-once retries.
            "Idempotency-Key": idempotency_key,
            **self.headers,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = requests.post(
                self.url, json=record, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SinkError(self.scrub(exc)) from None


class OTLPSink(EventSink):
    """Emit records as OpenTelemetry log records over OTLP/HTTP."""

    def __init__(
        self,
        name: str,
        endpoint: str,
        token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        service_name: str = "edge-ai-monitor",
        **_: Any,
    ):
        super().__init__(name, timeout)
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.headers = dict(headers or {})
        self.service_name = service_name
        self._secrets = [token] if token else []

    @staticmethod
    def _attribute(key: str, value: Any) -> Dict[str, Any]:
        """OTLP attributes are typed; map Python values onto its value types."""
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        if isinstance(value, int):
            return {"key": key, "value": {"intValue": str(value)}}
        if isinstance(value, float):
            return {"key": key, "value": {"doubleValue": value}}
        if isinstance(value, (list, dict)):
            return {"key": key, "value": {"stringValue": json.dumps(value)}}
        return {"key": key, "value": {"stringValue": str(value)}}

    def build_payload(self, record: Dict[str, Any]) -> Dict[str, Any]:
        severity = str(record.get("severity", "medium"))
        # OTLP severity numbers: 9=INFO, 13=WARN, 17=ERROR, 21=FATAL.
        number = {"low": 9, "medium": 13, "high": 17, "critical": 21}.get(severity, 13)
        body = record.get("error_summary") or f"{severity} problem detected"

        return {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            self._attribute("service.name", self.service_name),
                            self._attribute(
                                "service.instance.id",
                                (record.get("node") or {}).get("id", "unknown"),
                            ),
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "edge-ai-monitor"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(
                                        int(datetime.now(timezone.utc).timestamp() * 1e9)
                                    ),
                                    "severityNumber": number,
                                    "severityText": severity.upper(),
                                    "body": {"stringValue": str(body)},
                                    "attributes": [
                                        self._attribute(k, v)
                                        for k, v in sorted(record.items())
                                        if v is not None
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def send(self, record: Dict[str, Any], idempotency_key: str) -> None:
        import requests

        headers = {"Content-Type": "application/json", **self.headers}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = requests.post(
                f"{self.endpoint}/v1/logs",
                json=self.build_payload(record),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SinkError(self.scrub(exc)) from None


class SyslogSink(EventSink):
    """Emit RFC 5424 messages over UDP or TCP."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int = DEFAULT_SYSLOG_PORT,
        protocol: str = "udp",
        app_name: str = "edge-ai-monitor",
        timeout: float = DEFAULT_TIMEOUT,
        **_: Any,
    ):
        super().__init__(name, timeout)
        self.host = host
        self.port = int(port)
        self.protocol = protocol.lower()
        self.app_name = app_name

    def build_message(self, record: Dict[str, Any]) -> str:
        severity = str(record.get("severity", "medium"))
        priority = SYSLOG_FACILITY * 8 + SYSLOG_SEVERITY.get(severity, 5)
        timestamp = datetime.now(timezone.utc).isoformat()
        hostname = (record.get("node") or {}).get("id", "-") or "-"
        msgid = str(record.get("problem_key", "-"))[:32] or "-"
        return (
            f"<{priority}>1 {timestamp} {hostname} {self.app_name} - {msgid} - "
            f"{json.dumps(record, sort_keys=True)}"
        )

    def send(self, record: Dict[str, Any], idempotency_key: str) -> None:
        message = self.build_message(record).encode("utf-8")
        try:
            if self.protocol == "tcp":
                with socket.create_connection((self.host, self.port), self.timeout) as sock:
                    # RFC 6587 octet counting keeps framing unambiguous.
                    sock.sendall(f"{len(message)} ".encode("ascii") + message)
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendto(message, (self.host, self.port))
        except OSError as exc:
            raise SinkError(f"syslog delivery to {self.host}:{self.port} failed: {exc}") from None


class MQTTSink(EventSink):
    """Publish records to an MQTT topic, the common bus in edge estates."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int = 1883,
        topic: str = "edge-ai/proposals",
        username: Optional[str] = None,
        password: Optional[str] = None,
        qos: int = 1,
        timeout: float = DEFAULT_TIMEOUT,
        **_: Any,
    ):
        super().__init__(name, timeout)
        self.host = host
        self.port = int(port)
        self.topic = topic
        self.username = username
        self.password = password
        self.qos = int(qos)
        self._secrets = [password] if password else []

    def topic_for(self, record: Dict[str, Any]) -> str:
        """Per-severity subtopic, so subscribers can filter without parsing."""
        return f"{self.topic}/{record.get('severity', 'unknown')}"

    def send(self, record: Dict[str, Any], idempotency_key: str) -> None:
        try:
            import paho.mqtt.publish as publish
        except ImportError:
            raise SinkError(
                "the 'paho-mqtt' package is required for the MQTT sink"
            ) from None

        auth = None
        if self.username:
            auth = {"username": self.username, "password": self.password or ""}

        try:
            publish.single(
                self.topic_for(record),
                payload=json.dumps(record, sort_keys=True),
                qos=self.qos,
                hostname=self.host,
                port=self.port,
                auth=auth,
                keepalive=int(self.timeout),
            )
        except Exception as exc:  # paho raises a variety of types
            raise SinkError(self.scrub(exc)) from None


EVENT_SINK_TYPES = {
    "webhook": WebhookSink,
    "otlp": OTLPSink,
    "syslog": SyslogSink,
    "mqtt": MQTTSink,
}


def build_event_sink(name: str, settings: Dict[str, Any]) -> EventSink:
    """Construct an event sink from its configuration block."""
    kind = str(settings.get("type", "")).lower()
    if kind not in EVENT_SINK_TYPES:
        raise ValueError(
            f"unknown event sink type {kind!r}; expected one of "
            f"{', '.join(sorted(EVENT_SINK_TYPES))}"
        )
    options = {k: v for k, v in settings.items() if k not in ("type", "level", "enabled")}
    return EVENT_SINK_TYPES[kind](name=name, **options)
