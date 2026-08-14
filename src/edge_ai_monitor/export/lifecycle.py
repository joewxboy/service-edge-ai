"""Issue lifecycle: one problem, one issue, from first occurrence to resolution.

The tracker is not the source of truth — the local ledger is. That is what lets
a node that has been offline for hours reconcile without opening duplicates for
every occurrence it missed.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .issue_sinks import IssueSink, SinkError
from .records import severity_at_least

logger = logging.getLogger(__name__)

DEFAULT_MIN_SEVERITY = "high"
DEFAULT_UPDATE_INTERVAL = 3600.0  # at most one comment per hour per issue
DEFAULT_RESOLVE_AFTER = 24 * 3600.0  # a full daily cycle without recurrence


def _parse(timestamp: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None


@dataclass
class IssueState:
    """What we know locally about one problem's issue."""

    problem_key: str
    reference: str = ""
    status: str = "none"  # none | open | resolved
    last_comment_at: float = 0.0
    last_seen: str = ""
    last_root_cause: str = ""
    reported_occurrences: int = 0
    created_at: str = ""
    history: list = field(default_factory=list)


class IssueLedger:
    """Durable map of problem key to issue state, per sink."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._states: Dict[str, IssueState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("issue ledger at %s is unreadable; starting fresh", self.path)
            return
        for key, value in (raw or {}).items():
            try:
                self._states[key] = IssueState(**value)
            except TypeError:
                continue

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({k: asdict(v) for k, v in self._states.items()}, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.error("could not persist issue ledger: %s", exc)

    def get(self, problem_key: str) -> IssueState:
        with self._lock:
            return self._states.get(problem_key) or IssueState(problem_key=problem_key)

    def put(self, state: IssueState) -> None:
        with self._lock:
            self._states[state.problem_key] = state
            self._save()

    def open_states(self) -> list:
        with self._lock:
            return [s for s in self._states.values() if s.status == "open"]

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)


class IssueLifecycle:
    """Decides what should happen to an issue, then asks the sink to do it."""

    def __init__(
        self,
        sink: IssueSink,
        ledger: IssueLedger,
        min_severity: str = DEFAULT_MIN_SEVERITY,
        update_interval: float = DEFAULT_UPDATE_INTERVAL,
        resolve_after: float = DEFAULT_RESOLVE_AFTER,
    ):
        self.sink = sink
        self.ledger = ledger
        self.min_severity = min_severity
        self.update_interval = update_interval
        self.resolve_after = resolve_after

        self.created_count = 0
        self.updated_count = 0
        self.resolved_count = 0
        self.reopened_count = 0
        self.skipped_count = 0

    # ---------------- decisions ----------------

    def _should_track(self, record: Dict[str, Any]) -> bool:
        return severity_at_least(str(record.get("severity", "")), self.min_severity)

    def _due_for_comment(self, state: IssueState, now: float) -> bool:
        return (now - state.last_comment_at) >= self.update_interval

    def _analysis_changed(self, state: IssueState, record: Dict[str, Any]) -> bool:
        current = str(record.get("root_cause") or "")
        return bool(current) and bool(state.last_root_cause) and current != state.last_root_cause

    # ---------------- actions ----------------

    def _announce(self, action: str, detail: str) -> None:
        logger.info("[dry-run] would %s issue via %s: %s", action, self.sink.name, detail)

    def handle(self, record: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
        """Apply one occurrence to the issue lifecycle. Returns the reference."""
        now = time.time() if now is None else now

        if not self._should_track(record):
            self.skipped_count += 1
            return None

        problem_key = str(record.get("problem_key", ""))
        if not problem_key:
            logger.warning("record has no problem_key; cannot track an issue for it")
            return None

        state = self.ledger.get(problem_key)

        if state.status == "none":
            return self._create(state, record, now)
        if state.status == "resolved":
            return self._reopen(state, record, now)
        return self._update(state, record, now)

    def _create(self, state: IssueState, record: Dict[str, Any], now: float) -> Optional[str]:
        title = self.sink.build_title(record)
        body = self.sink.build_body(record)

        if self.sink.dry_run:
            self._announce("create", title)
            return None

        try:
            reference = self.sink.create(title, body, record)
        except SinkError as exc:
            logger.error("could not create issue via %s: %s", self.sink.name, exc)
            raise

        state.reference = reference
        state.status = "open"
        state.created_at = datetime.now(timezone.utc).isoformat()
        state.last_comment_at = now
        state.last_seen = str(record.get("last_seen") or "")
        state.last_root_cause = str(record.get("root_cause") or "")
        state.reported_occurrences = int(record.get("total_occurrences") or 0)
        state.history.append({"action": "created", "reference": reference, "at": state.created_at})
        self.ledger.put(state)
        self.created_count += 1
        logger.info("opened issue %s via %s for %s", reference, self.sink.name, state.problem_key)
        return reference

    def _update(self, state: IssueState, record: Dict[str, Any], now: float) -> Optional[str]:
        # A changed explanation is worth saying immediately; routine recurrence
        # is not, or a per-minute error would comment every minute.
        if self._analysis_changed(state, record):
            body = self.sink.build_revised_analysis(record)
        elif self._due_for_comment(state, now):
            body = self.sink.build_update(record)
        else:
            # Below the comment threshold, but the occurrence still counts
            # towards deciding when the problem has gone quiet.
            state.last_seen = str(record.get("last_seen") or state.last_seen)
            state.reported_occurrences = int(
                record.get("total_occurrences") or state.reported_occurrences
            )
            self.ledger.put(state)
            return state.reference

        if self.sink.dry_run:
            self._announce("comment on", f"{state.reference}: {body[:60]}")
            return state.reference

        # If it was closed behind our back and is happening again, reopen it.
        if self.sink.is_open(state.reference) is False:
            return self._reopen(state, record, now)

        try:
            self.sink.comment(state.reference, body)
        except SinkError as exc:
            logger.error("could not update issue %s: %s", state.reference, exc)
            raise

        state.last_comment_at = now
        state.last_seen = str(record.get("last_seen") or state.last_seen)
        state.last_root_cause = str(record.get("root_cause") or state.last_root_cause)
        state.reported_occurrences = int(record.get("total_occurrences") or 0)
        self.ledger.put(state)
        self.updated_count += 1
        return state.reference

    def _reopen(self, state: IssueState, record: Dict[str, Any], now: float) -> Optional[str]:
        body = (
            f"This problem has recurred (last seen {record.get('last_seen')}, "
            f"{record.get('total_occurrences')} occurrence(s) in total)."
        )

        if self.sink.dry_run:
            self._announce("reopen", state.reference)
            return state.reference

        if self.sink.supports_reopen and state.reference:
            try:
                self.sink.reopen(state.reference, body)
                state.status = "open"
                state.last_comment_at = now
                state.history.append({"action": "reopened", "at": datetime.now(timezone.utc).isoformat()})
                self.ledger.put(state)
                self.reopened_count += 1
                return state.reference
            except SinkError as exc:
                logger.warning(
                    "could not reopen %s (%s); creating a linked issue instead",
                    state.reference,
                    exc,
                )

        # Trackers that do not reopen get a fresh record linked to the old one.
        previous = state.reference
        fresh = IssueState(problem_key=state.problem_key, history=list(state.history))
        reference = self._create(fresh, record, now)
        if reference and previous:
            try:
                self.sink.comment(
                    reference, f"Recurrence of previously closed issue {previous}."
                )
            except SinkError:
                pass
        return reference

    # ---------------- resolution ----------------

    def resolve_stale(self, now: Optional[float] = None) -> int:
        """Close issues whose problems have stopped recurring."""
        now = time.time() if now is None else now
        resolved = 0

        for state in self.ledger.open_states():
            last_seen = _parse(state.last_seen)
            # Fall back to the last comment when the timestamp is unusable, so a
            # malformed value cannot pin an issue open forever.
            reference_time = last_seen.timestamp() if last_seen else state.last_comment_at
            if (now - reference_time) < self.resolve_after:
                continue

            record = {
                "total_occurrences": state.reported_occurrences,
                "last_seen": state.last_seen
                or datetime.fromtimestamp(reference_time, timezone.utc).isoformat(),
            }
            body = self.sink.build_resolution(record, self.resolve_after / 3600.0)

            if self.sink.dry_run:
                self._announce("resolve", state.reference)
                continue

            try:
                self.sink.resolve(state.reference, body)
            except SinkError as exc:
                logger.error("could not resolve issue %s: %s", state.reference, exc)
                continue

            state.status = "resolved"
            state.history.append(
                {"action": "resolved", "at": datetime.now(timezone.utc).isoformat()}
            )
            self.ledger.put(state)
            self.resolved_count += 1
            resolved += 1
            logger.info("resolved issue %s: no recurrence within the window", state.reference)

        return resolved

    def describe(self) -> Dict[str, Any]:
        return {
            "issues_created": self.created_count,
            "issues_updated": self.updated_count,
            "issues_resolved": self.resolved_count,
            "issues_reopened": self.reopened_count,
            "issues_below_threshold": self.skipped_count,
            "issues_tracked": len(self.ledger),
        }
