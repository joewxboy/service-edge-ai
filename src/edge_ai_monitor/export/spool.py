"""Durable store-and-forward queue for pending exports.

Edge nodes lose connectivity routinely, and an outage often ends in a restart.
An in-memory queue would lose exactly the records an operator most wants once
the node comes back, so pending exports are written to disk before any delivery
is attempted.

Bounded by policy rather than hope: the spool has a size ceiling and an age
ceiling, and sheds oldest-first when it hits either.
"""

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64MB
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
DEFAULT_BASE_BACKOFF = 30.0
DEFAULT_MAX_BACKOFF = 3600.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SpooledRecord:
    """A record awaiting delivery to one sink."""

    path: Path
    sink: str
    payload: Dict[str, Any]
    attempts: int = 0
    first_queued: str = ""
    next_attempt: float = 0.0

    @property
    def idempotency_key(self) -> str:
        return str(self.payload.get("idempotency_key", ""))

    def is_due(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.next_attempt


def backoff_delay(
    attempts: int,
    base: float = DEFAULT_BASE_BACKOFF,
    maximum: float = DEFAULT_MAX_BACKOFF,
) -> float:
    """Exponential backoff with jitter.

    Jitter matters here: a fleet of nodes reconnecting after the same network
    outage would otherwise retry in lockstep and hammer the sink.
    """
    delay = min(maximum, base * (2 ** max(0, attempts - 1)))
    return delay * (0.5 + random.random() * 0.5)


class Spool:
    """On-disk queue of records pending delivery."""

    def __init__(
        self,
        directory: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
    ):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self.base_backoff = base_backoff
        self._lock = threading.RLock()
        self._sequence = 0

        self.queued_count = 0
        self.delivered_count = 0
        self.dropped_count = 0
        self.expired_count = 0

    # ---------------- writing ----------------

    def _next_name(self, sink: str) -> str:
        # Monotonic prefix keeps lexical order equal to arrival order, which is
        # what makes oldest-first shedding a simple sort.
        self._sequence += 1
        return f"{int(time.time() * 1000):013d}-{self._sequence:06d}-{sink}.json"

    def put(self, sink: str, payload: Dict[str, Any]) -> Optional[Path]:
        """Persist a record for later delivery."""
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / self._next_name(sink)
                document = {
                    "sink": sink,
                    "payload": payload,
                    "attempts": 0,
                    "first_queued": _utcnow(),
                    "next_attempt": 0.0,
                }
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(document), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:
                logger.error("could not spool export for %s: %s", sink, exc)
                return None

            self.queued_count += 1
            self._enforce_limits()
            return path

    # ---------------- reading ----------------

    def _load(self, path: Path) -> Optional[SpooledRecord]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("discarding unreadable spool entry %s", path)
            self._discard(path)
            return None
        return SpooledRecord(
            path=path,
            sink=str(document.get("sink", "")),
            payload=document.get("payload") or {},
            attempts=int(document.get("attempts") or 0),
            first_queued=str(document.get("first_queued") or ""),
            next_attempt=float(document.get("next_attempt") or 0.0),
        )

    def _entries(self) -> List[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(p for p in self.directory.glob("*.json") if p.is_file())

    def pending(self) -> List[SpooledRecord]:
        """All spooled records, oldest first."""
        with self._lock:
            records = [self._load(p) for p in self._entries()]
            return [r for r in records if r is not None]

    def due(self) -> List[SpooledRecord]:
        """Records whose backoff has elapsed."""
        return [r for r in self.pending() if r.is_due()]

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._entries())

    def size_bytes(self) -> int:
        with self._lock:
            return sum(p.stat().st_size for p in self._entries() if p.exists())

    # ---------------- outcomes ----------------

    def _discard(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def succeed(self, record: SpooledRecord) -> None:
        """Delivery succeeded; the record is done."""
        with self._lock:
            self._discard(record.path)
            self.delivered_count += 1

    def fail(self, record: SpooledRecord) -> None:
        """Delivery failed; schedule a retry unless the record has expired."""
        with self._lock:
            if self._expired(record):
                logger.warning(
                    "dropping export to %s after %s: exceeded max age",
                    record.sink,
                    record.first_queued,
                )
                self._discard(record.path)
                self.expired_count += 1
                return

            record.attempts += 1
            delay = backoff_delay(record.attempts, base=self.base_backoff)
            document = {
                "sink": record.sink,
                "payload": record.payload,
                "attempts": record.attempts,
                "first_queued": record.first_queued,
                "next_attempt": time.time() + delay,
            }
            try:
                record.path.write_text(json.dumps(document), encoding="utf-8")
            except OSError as exc:
                logger.error("could not update spool entry %s: %s", record.path, exc)

    def _expired(self, record: SpooledRecord) -> bool:
        if not record.first_queued:
            return False
        try:
            queued = datetime.fromisoformat(record.first_queued)
        except ValueError:
            return False
        age = (datetime.now(timezone.utc) - queued).total_seconds()
        return age > self.max_age_seconds

    # ---------------- limits ----------------

    def _enforce_limits(self) -> None:
        """Shed oldest-first when over the size ceiling. Caller holds the lock."""
        entries = self._entries()
        total = sum(p.stat().st_size for p in entries if p.exists())
        if total <= self.max_bytes:
            return

        dropped = 0
        for path in entries:  # oldest first
            if total <= self.max_bytes:
                break
            try:
                total -= path.stat().st_size
            except OSError:
                continue
            self._discard(path)
            dropped += 1

        if dropped:
            self.dropped_count += dropped
            logger.warning(
                "export spool exceeded %dMB; dropped %d oldest record(s)",
                self.max_bytes // (1024 * 1024),
                dropped,
            )

    def purge_expired(self) -> int:
        """Remove records older than the age ceiling. Returns how many went."""
        removed = 0
        with self._lock:
            for record in self.pending():
                if self._expired(record):
                    self._discard(record.path)
                    removed += 1
            if removed:
                self.expired_count += removed
                logger.warning("purged %d expired export record(s)", removed)
        return removed

    def describe(self) -> Dict[str, Any]:
        """Counters for the health endpoint."""
        return {
            "export_queue_depth": self.depth,
            "export_queue_bytes": self.size_bytes(),
            "exports_delivered": self.delivered_count,
            "exports_dropped": self.dropped_count,
            "exports_expired": self.expired_count,
        }
