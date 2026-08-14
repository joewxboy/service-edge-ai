"""Permission-based tailing of workload log files with error detection.

Uses ``watchdog`` for filesystem notifications when it is available and falls
back to interval polling otherwise, so the monitor still works on minimal
images where the optional dependency is missing.
"""

import logging
import os
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on deployment image
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False

# Resource ceilings from the log-monitoring spec.
MAX_BUFFER_BYTES = 100 * 1024 * 1024  # 100MB in-memory buffer
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1GB file size limit
TAIL_BYTES_ON_LARGE_FILE = 100 * 1024 * 1024  # tail most recent 100MB
CONTEXT_LINES = 50  # lines before/after an error


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ErrorEvent:
    """A log line that matched an error pattern, with surrounding context."""

    workload_id: str
    log_path: str
    line_number: int
    line: str
    matched_pattern: str
    context_before: List[str] = field(default_factory=list)
    context_after: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_utcnow)

    def context_text(self) -> str:
        """Full context window as a single block of text."""
        return "\n".join(self.context_before + [self.line] + self.context_after)

    def signature(self) -> str:
        """Identity used to group recurring occurrences of the same error.

        Digits are normalised out so that timestamps, PIDs and counters do not
        make every occurrence look unique.
        """
        normalised = re.sub(r"\d+", "#", self.line.strip())
        return f"{self.workload_id}:{self.matched_pattern}:{normalised}"


class _PatternMatcher:
    """Case-insensitive substring/regex matching over error patterns."""

    def __init__(self, patterns: List[str]):
        self.patterns = list(patterns)
        self._compiled = []
        for pattern in self.patterns:
            try:
                self._compiled.append((pattern, re.compile(pattern, re.IGNORECASE)))
            except re.error:
                # Treat an invalid regex as a literal substring.
                self._compiled.append(
                    (pattern, re.compile(re.escape(pattern), re.IGNORECASE))
                )

    def match(self, line: str) -> Optional[str]:
        for pattern, regex in self._compiled:
            if regex.search(line):
                return pattern
        return None


class _TailedFile:
    """Tracks read position and context buffers for one log file."""

    def __init__(self, path: str, workload_id: str, matcher: _PatternMatcher):
        self.path = path
        self.workload_id = workload_id
        self.matcher = matcher
        self.offset = 0
        self.inode: Optional[int] = None
        self.line_number = 0
        self.buffer_bytes = 0
        # Lines preceding the current position, capped at the context window.
        self.context_before: Deque[str] = deque(maxlen=CONTEXT_LINES)
        # Errors still collecting their trailing context.
        self.pending: List[ErrorEvent] = []
        self._partial = ""

    def open_initial(self) -> bool:
        """Seek to the tail of an existing file. Returns False if unreadable."""
        try:
            stat = os.stat(self.path)
        except OSError as exc:
            logger.warning("log path %s is not accessible: %s", self.path, exc)
            return False

        self.inode = stat.st_ino
        size = stat.st_size
        if size > MAX_FILE_BYTES:
            logger.warning(
                "log file %s is %.1fGB, exceeding the %dGB limit; tailing most recent %dMB",
                self.path,
                size / (1024**3),
                MAX_FILE_BYTES // (1024**3),
                TAIL_BYTES_ON_LARGE_FILE // (1024**2),
            )
            self.offset = size - TAIL_BYTES_ON_LARGE_FILE
        else:
            # Start at the end: only new entries are of interest.
            self.offset = size
        return True

    def _detect_rotation(self) -> bool:
        """True when the file was replaced or truncated since the last read."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return False
        rotated = (self.inode is not None and stat.st_ino != self.inode) or (
            stat.st_size < self.offset
        )
        if rotated:
            logger.info("detected rotation of %s; reopening from start", self.path)
            self.inode = stat.st_ino
            self.offset = 0
            self._partial = ""
        return rotated

    def read_new_lines(self) -> List[str]:
        """Return log lines appended since the last read."""
        self._detect_rotation()
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
                if self.inode is None:
                    self.inode = os.fstat(handle.fileno()).st_ino
        except OSError as exc:
            logger.warning("failed reading %s: %s", self.path, exc)
            return []

        if not chunk:
            return []

        # Hold back a trailing partial line until its newline arrives.
        chunk = self._partial + chunk
        self._partial = ""
        if not chunk.endswith("\n"):
            chunk, _, self._partial = chunk.rpartition("\n")
            if not chunk:
                return []

        return chunk.splitlines()


class LogMonitor:
    """Tails permitted workload logs and emits ErrorEvents on pattern matches.

    ``on_error`` is invoked once an error's trailing context window is complete
    (or when the file goes quiet and the window is flushed).
    """

    def __init__(
        self,
        on_error: Callable[[ErrorEvent], None],
        poll_interval: float = 1.0,
        max_buffer_bytes: int = MAX_BUFFER_BYTES,
    ):
        self.on_error = on_error
        self.poll_interval = poll_interval
        self.max_buffer_bytes = max_buffer_bytes
        self._files: Dict[str, _TailedFile] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._observer = None
        self._buffer_bytes = 0

    # ---------------- registration ----------------

    def watch_workload(self, workload) -> List[str]:
        """Start tailing every readable log path for a permitted workload.

        Returns the paths actually being monitored. Unreadable paths are logged
        and skipped so one bad entry does not disable the rest.
        """
        if not workload.monitoring.enabled:
            logger.info(
                "skipping workload %s: monitoring not enabled in service definition",
                workload.workload_id,
            )
            return []

        matcher = _PatternMatcher(workload.monitoring.error_patterns)
        watched = []
        with self._lock:
            for path in workload.monitoring.log_paths:
                key = f"{workload.workload_id}:{path}"
                if key in self._files:
                    watched.append(path)
                    continue
                tailed = _TailedFile(path, workload.workload_id, matcher)
                if not tailed.open_initial():
                    continue
                self._files[key] = tailed
                watched.append(path)
                logger.info("monitoring %s for workload %s", path, workload.workload_id)

        if watched:
            self._register_watchdog_paths(watched)
        return watched

    def unwatch_workload(self, workload_id: str) -> None:
        """Stop tailing all log paths for a workload."""
        with self._lock:
            for key in [k for k in self._files if k.startswith(f"{workload_id}:")]:
                del self._files[key]
        logger.info("stopped monitoring workload %s", workload_id)

    @property
    def watched_paths(self) -> List[str]:
        with self._lock:
            return [f.path for f in self._files.values()]

    # ---------------- reading ----------------

    def poll_once(self) -> int:
        """Read all watched files once. Returns the number of errors emitted."""
        with self._lock:
            files = list(self._files.values())

        emitted = 0
        for tailed in files:
            emitted += self._process_file(tailed)
        return emitted

    def _process_file(self, tailed: _TailedFile) -> int:
        lines = tailed.read_new_lines()
        if not lines:
            # Quiet file: release any error still waiting on trailing context.
            return self._flush_pending(tailed, force=True)

        emitted = 0
        for line in lines:
            tailed.line_number += 1
            self._account_buffer(tailed, line)

            # Complete the trailing context of previously detected errors.
            for event in tailed.pending:
                if len(event.context_after) < CONTEXT_LINES:
                    event.context_after.append(line)
            emitted += self._flush_pending(tailed)

            matched = tailed.matcher.match(line)
            if matched:
                event = ErrorEvent(
                    workload_id=tailed.workload_id,
                    log_path=tailed.path,
                    line_number=tailed.line_number,
                    line=line,
                    matched_pattern=matched,
                    # Copy: the deque keeps moving as reading continues.
                    context_before=list(tailed.context_before),
                )
                tailed.pending.append(event)

            tailed.context_before.append(line)

        return emitted

    def _flush_pending(self, tailed: _TailedFile, force: bool = False) -> int:
        """Emit errors whose trailing context window is complete."""
        ready = [
            e
            for e in tailed.pending
            if force or len(e.context_after) >= CONTEXT_LINES
        ]
        for event in ready:
            tailed.pending.remove(event)
            self._release_buffer(tailed, event)
            try:
                self.on_error(event)
            except Exception:  # pragma: no cover - defensive
                logger.exception("error callback failed for %s", event.log_path)
        return len(ready)

    def _account_buffer(self, tailed: _TailedFile, line: str) -> None:
        """Track buffered bytes and drop oldest context on overflow."""
        size = len(line.encode("utf-8", errors="replace"))
        self._buffer_bytes += size
        tailed.buffer_bytes += size
        if self._buffer_bytes <= self.max_buffer_bytes:
            return

        logger.warning(
            "log buffer exceeded %dMB; dropping oldest buffered entries",
            self.max_buffer_bytes // (1024 * 1024),
        )
        with self._lock:
            for other in self._files.values():
                other.context_before.clear()
                # Oldest pending errors go first; keep the most recent one.
                dropped = other.pending[:-1]
                other.pending = other.pending[-1:]
                if dropped:
                    logger.warning(
                        "dropped %d pending error(s) for %s under buffer pressure",
                        len(dropped),
                        other.path,
                    )
                other.buffer_bytes = 0
        self._buffer_bytes = 0

    def _release_buffer(self, tailed: _TailedFile, event: ErrorEvent) -> None:
        size = len(event.context_text().encode("utf-8", errors="replace"))
        self._buffer_bytes = max(0, self._buffer_bytes - size)
        tailed.buffer_bytes = max(0, tailed.buffer_bytes - size)

    # ---------------- lifecycle ----------------

    def _register_watchdog_paths(self, paths: List[str]) -> None:
        """Ask watchdog to wake the reader when watched directories change."""
        if not WATCHDOG_AVAILABLE or self._observer is None:
            return
        handler = _WakeHandler(self._wake)
        for directory in {os.path.dirname(p) for p in paths if os.path.dirname(p)}:
            if os.path.isdir(directory):
                try:
                    self._observer.schedule(handler, directory, recursive=False)
                except OSError as exc:  # pragma: no cover - platform specific
                    logger.warning("watchdog could not watch %s: %s", directory, exc)

    def _wake(self) -> None:
        self._wake_event.set()

    def start(self) -> None:
        """Start the background reader loop."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._wake_event = threading.Event()

        if WATCHDOG_AVAILABLE and Observer is not None:
            self._observer = Observer()
            self._observer.start()
            self._register_watchdog_paths(self.watched_paths)
            logger.info("log monitoring started (watchdog notifications)")
        else:
            logger.info(
                "watchdog unavailable; log monitoring started (%ss polling)",
                self.poll_interval,
            )

        self._thread = threading.Thread(target=self._run, name="log-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("unexpected error during log monitoring")
            # Watchdog shortens the wait; polling is the fallback cadence.
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the reader loop and release watchdog resources."""
        self._stop.set()
        if getattr(self, "_wake_event", None) is not None:
            self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=timeout)
            self._observer = None
        logger.info("log monitoring stopped")


class _WakeHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Watchdog handler that just nudges the reader loop."""

    def __init__(self, wake: Callable[[], None]):
        self._wake = wake

    def on_modified(self, event: Any) -> None:  # pragma: no cover - needs watchdog
        self._wake()

    def on_created(self, event: Any) -> None:  # pragma: no cover - needs watchdog
        self._wake()

    def on_moved(self, event: Any) -> None:  # pragma: no cover - needs watchdog
        self._wake()
