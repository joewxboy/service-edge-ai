"""The export worker: a consumer of proposals, isolated from monitoring.

Nothing in the monitoring pipeline waits on this. Proposals arrive through a
callback, are spooled to disk immediately, and a background thread does the
network work. A sink that is down, slow, or hanging costs nothing but spool
space.
"""

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .credentials import MissingCredential, redact_settings, resolve_settings
from .event_sinks import EventSink, SinkError, build_event_sink
from .issue_sinks import IssueSink, build_issue_sink
from .lifecycle import IssueLedger, IssueLifecycle
from .records import DEFAULT_LEVEL, build_record, effective_level
from .spool import Spool

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_RESOLVE_SWEEP_INTERVAL = 900.0


class Exporter:
    """Owns sinks, the spool, and the delivery loop."""

    def __init__(
        self,
        spool: Spool,
        event_sinks: Optional[Dict[str, EventSink]] = None,
        issue_lifecycles: Optional[Dict[str, IssueLifecycle]] = None,
        levels: Optional[Dict[str, str]] = None,
        node: Optional[Dict[str, str]] = None,
        consent_provider: Optional[Callable[[str], bool]] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_delivery: Optional[Callable[[str, str, str, str], None]] = None,
    ):
        self.spool = spool
        self.event_sinks = event_sinks or {}
        self.issue_lifecycles = issue_lifecycles or {}
        self.levels = levels or {}
        self.node = node or {}
        # Whether a workload has consented to content-bearing export levels.
        self.consent_provider = consent_provider
        self.poll_interval = poll_interval
        # Called as (problem_key, sink, status, reference) so proposals can
        # record where they went.
        self.on_delivery = on_delivery

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_resolve_sweep = 0.0
        self._offline_since: Optional[float] = None

        self.submitted_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.event_sinks or self.issue_lifecycles)

    @property
    def sink_names(self) -> List[str]:
        return sorted(set(self.event_sinks) | set(self.issue_lifecycles))

    # ---------------- submission ----------------

    def submit(self, proposal: Dict[str, Any]) -> int:
        """Queue a proposal for every configured sink. Never blocks or raises."""
        if not self.enabled:
            return 0

        metadata = proposal.get("metadata", {})
        workload_id = str(metadata.get("workload_id", ""))
        permits_content = True
        if self.consent_provider is not None:
            try:
                permits_content = bool(self.consent_provider(workload_id))
            except Exception:  # pragma: no cover - defensive
                logger.exception("consent lookup failed; assuming metadata only")
                permits_content = False

        queued = 0
        for sink_name in self.sink_names:
            requested = self.levels.get(sink_name, DEFAULT_LEVEL)
            level = effective_level(requested, permits_content)
            record = build_record(proposal, level=level, node=self.node)
            document = dict(record.payload)
            document["idempotency_key"] = record.idempotency_key
            if self.spool.put(sink_name, document) is not None:
                queued += 1
                if self.on_delivery:
                    self._notify(record.problem_key, sink_name, "pending", "")

        self.submitted_count += queued
        if queued:
            self._wake.set()
        return queued

    def _notify(self, problem_key: str, sink: str, status: str, reference: str) -> None:
        try:
            self.on_delivery(problem_key, sink, status, reference)  # type: ignore[misc]
        except Exception:  # pragma: no cover - defensive
            logger.exception("delivery notification failed")

    # ---------------- delivery ----------------

    def deliver_once(self) -> int:
        """Attempt every due record. Returns how many were delivered."""
        delivered = 0
        for record in self.spool.due():
            if self._stop.is_set():
                break
            if self._deliver(record):
                delivered += 1

        if delivered and self._offline_since is not None:
            outage = time.time() - self._offline_since
            logger.info(
                "export connectivity restored after %.0fs; %d record(s) still queued",
                outage,
                self.spool.depth,
            )
            self._offline_since = None
        return delivered

    def _deliver(self, spooled) -> bool:
        payload = dict(spooled.payload)
        idempotency_key = str(payload.pop("idempotency_key", ""))
        problem_key = str(payload.get("problem_key", ""))
        sink_name = spooled.sink

        try:
            if sink_name in self.event_sinks:
                self.event_sinks[sink_name].send(payload, idempotency_key)
                reference = ""
            elif sink_name in self.issue_lifecycles:
                reference = self.issue_lifecycles[sink_name].handle(payload) or ""
            else:
                # Sink removed from configuration while records were queued.
                logger.warning("dropping queued record for unknown sink %s", sink_name)
                self.spool.succeed(spooled)
                return False
        except SinkError as exc:
            if self._offline_since is None:
                self._offline_since = time.time()
                logger.warning("export to %s failing, queuing: %s", sink_name, exc)
            self.spool.fail(spooled)
            if self.on_delivery and problem_key:
                self._notify(problem_key, sink_name, "failed", "")
            return False
        except Exception:  # pragma: no cover - a sink bug must not kill the loop
            logger.exception("unexpected error delivering to %s", sink_name)
            self.spool.fail(spooled)
            return False

        self.spool.succeed(spooled)
        if self.on_delivery and problem_key:
            self._notify(problem_key, sink_name, "delivered", reference)
        return True

    def sweep_resolutions(self, now: Optional[float] = None) -> int:
        """Close issues whose problems have gone quiet."""
        resolved = 0
        for lifecycle in self.issue_lifecycles.values():
            try:
                resolved += lifecycle.resolve_stale(now)
            except Exception:  # pragma: no cover - defensive
                logger.exception("resolution sweep failed for %s", lifecycle.sink.name)
        return resolved

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread is not None or not self.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="export-worker", daemon=True)
        self._thread.start()
        logger.info(
            "export worker started for sink(s): %s", ", ".join(self.sink_names) or "none"
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.deliver_once()
                now = time.time()
                if now - self._last_resolve_sweep >= DEFAULT_RESOLVE_SWEEP_INTERVAL:
                    self._last_resolve_sweep = now
                    self.sweep_resolutions(now)
                    self.spool.purge_expired()
            except Exception:  # pragma: no cover - defensive
                logger.exception("unexpected error in export worker")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the worker, giving in-flight delivery a bounded chance to finish."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("export worker stopped")

    # ---------------- diagnostics ----------------

    def check(self) -> Dict[str, bool]:
        """Probe every sink. Used by --check; never raises."""
        results = {}
        for name, sink in self.event_sinks.items():
            try:
                results[name] = bool(sink.check())
            except Exception:
                results[name] = False
        for name, lifecycle in self.issue_lifecycles.items():
            try:
                results[name] = bool(lifecycle.sink.check())
            except Exception:
                results[name] = False
        return results

    def describe(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "export_enabled": self.enabled,
            "export_sinks": self.sink_names,
            "exports_submitted": self.submitted_count,
        }
        status.update(self.spool.describe())
        for lifecycle in self.issue_lifecycles.values():
            status.update(lifecycle.describe())
        return status


def build_exporter(
    config: Any,
    node: Optional[Dict[str, str]] = None,
    consent_provider: Optional[Callable[[str], bool]] = None,
    on_delivery: Optional[Callable[[str, str, str, str], None]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Exporter:
    """Construct an Exporter from configuration, skipping sinks that fail.

    A bad sink disables itself and is reported; it never prevents the service
    from starting or the other sinks from working.
    """
    spool = Spool(
        directory=config.spool_directory,
        max_bytes=config.spool_max_bytes,
        max_age_seconds=config.spool_max_age_seconds,
    )

    event_sinks: Dict[str, EventSink] = {}
    issue_lifecycles: Dict[str, IssueLifecycle] = {}
    levels: Dict[str, str] = {}

    for name, settings in (config.sinks or {}).items():
        if not settings.get("enabled", True):
            logger.info("export sink %s is disabled in configuration", name)
            continue

        kind = str(settings.get("type", "")).lower()
        levels[name] = settings.get("level", DEFAULT_LEVEL)
        try:
            settings, secrets = resolve_settings(
                name, settings, env if env is not None else os.environ
            )
            if kind in ("github", "jira", "servicenow"):
                sink = build_issue_sink(name, settings)
                sink._secrets = list(secrets)
                ledger = IssueLedger(f"{config.spool_directory}/issues-{name}.json")
                issue_lifecycles[name] = IssueLifecycle(
                    sink,
                    ledger,
                    min_severity=settings.get("min_severity", "high"),
                    update_interval=float(settings.get("update_interval", 3600.0)),
                    resolve_after=float(settings.get("resolve_after", 86400.0)),
                )
            else:
                event_sink = build_event_sink(name, settings)
                event_sink._secrets = list(secrets)
                event_sinks[name] = event_sink
        except (ValueError, TypeError, MissingCredential) as exc:
            logger.error(
                "export sink %s disabled: %s (settings: %s)",
                name,
                exc,
                redact_settings(settings),
            )
            levels.pop(name, None)

    return Exporter(
        spool=spool,
        event_sinks=event_sinks,
        issue_lifecycles=issue_lifecycles,
        levels=levels,
        node=node,
        consent_provider=consent_provider,
        on_delivery=on_delivery,
    )
