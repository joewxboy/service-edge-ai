"""Async, severity-ordered coordination of LLM error analysis."""

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .log_monitor import ErrorEvent
from .ollama_client import LLMError, OllamaClient

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 50
RECURRENCE_THRESHOLD = 3  # occurrences that make an error "recurring"
RECURRENCE_WINDOW_SECONDS = 300  # 5 minutes
CACHE_TTL_SECONDS = 900  # reuse an analysis for 15 minutes

# Higher number == more urgent. Used for queue ordering and shedding.
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
DEFAULT_RANK = 2  # severity is unknown until the LLM answers

# Patterns that let us guess urgency before the LLM has seen the error.
_PRIORITY_HINTS = (
    ("critical", ("fatal", "critical", "panic", "out of memory", "oom")),
    ("high", ("exception", "traceback", "refused", "denied", "timeout")),
)


@dataclass
class AnalysisResult:
    """A completed LLM analysis for one error (or error group)."""

    event: ErrorEvent
    workload_metadata: Dict[str, Any]
    error_summary: str
    root_cause: str
    severity: str
    remediation_steps: List[str]
    confidence: float
    occurrences: int = 1
    analysis_duration_ms: int = 0
    from_cache: bool = False

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, DEFAULT_RANK)


@dataclass(order=True)
class _QueueItem:
    """Heap entry ordered by (-priority, sequence) for stable FIFO per level."""

    sort_key: tuple = field(compare=True)
    event: ErrorEvent = field(compare=False, default=None)
    metadata: Dict[str, Any] = field(compare=False, default_factory=dict)
    priority: int = field(compare=False, default=DEFAULT_RANK)
    occurrences: int = field(compare=False, default=1)


def estimate_priority(event: ErrorEvent) -> int:
    """Guess severity rank from the raw log line before analysis runs."""
    haystack = f"{event.line} {event.matched_pattern}".lower()
    for severity, hints in _PRIORITY_HINTS:
        if any(hint in haystack for hint in hints):
            return SEVERITY_RANK[severity]
    return DEFAULT_RANK


class _RecurrenceTracker:
    """Counts occurrences of an error signature inside a sliding window."""

    def __init__(
        self,
        threshold: int = RECURRENCE_THRESHOLD,
        window_seconds: float = RECURRENCE_WINDOW_SECONDS,
    ):
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._seen: Dict[str, List[float]] = {}

    def record(self, signature: str, now: Optional[float] = None) -> int:
        """Record an occurrence and return the count inside the window."""
        now = time.monotonic() if now is None else now
        timestamps = [t for t in self._seen.get(signature, []) if now - t <= self.window_seconds]
        timestamps.append(now)
        self._seen[signature] = timestamps
        return len(timestamps)

    def is_recurring(self, signature: str) -> bool:
        return len(self._seen.get(signature, [])) >= self.threshold


class ErrorAnalyzer:
    """Queues detected errors and analyses them with the LLM asynchronously.

    Log monitoring never blocks on inference: ``submit`` only enqueues, and a
    worker thread drains the queue in severity order.
    """

    def __init__(
        self,
        llm_client: OllamaClient,
        metadata_provider: Callable[[str], Dict[str, Any]],
        on_result: Optional[Callable[[AnalysisResult], None]] = None,
        max_queue_size: int = MAX_QUEUE_SIZE,
        cache_ttl: float = CACHE_TTL_SECONDS,
        knowledge_provider: Optional[Callable[[str], str]] = None,
    ):
        self.llm_client = llm_client
        self.metadata_provider = metadata_provider
        # Returns domain guidance for a workload id; absent means no guidance.
        self.knowledge_provider = knowledge_provider
        self.on_result = on_result
        self.max_queue_size = max_queue_size
        self.cache_ttl = cache_ttl

        self._heap: List[_QueueItem] = []
        self._counter = itertools.count()
        self._lock = threading.RLock()
        self._work_available = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._recurrence = _RecurrenceTracker()
        self._cache: Dict[str, tuple] = {}  # signature -> (expiry, AnalysisResult)
        self._pending_signatures: Dict[str, _QueueItem] = {}

        self.dropped_count = 0
        self.analyzed_count = 0
        self.failed_count = 0

    # ---------------- submission ----------------

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def submit(self, event: ErrorEvent) -> bool:
        """Queue an error for analysis. Returns False when it was not queued."""
        signature = event.signature()

        with self._lock:
            count = self._recurrence.record(signature)

            # Aggregate: an identical error already waiting just bumps its count
            # instead of occupying another queue slot.
            pending = self._pending_signatures.get(signature)
            if pending is not None:
                pending.occurrences = count
                logger.debug(
                    "aggregated recurring error %s (%d occurrences)", signature, count
                )
                return True

            cached = self._get_cached(signature)
            if cached is not None:
                logger.debug("reusing cached analysis for %s", signature)
                self._emit(
                    AnalysisResult(**{**cached.__dict__, "event": event, "from_cache": True})
                )
                return True

            priority = estimate_priority(event)
            item = _QueueItem(
                sort_key=(-priority, next(self._counter)),
                event=event,
                metadata=self.metadata_provider(event.workload_id),
                priority=priority,
                occurrences=count,
            )

            if len(self._heap) >= self.max_queue_size and not self._shed_lowest(priority):
                self.dropped_count += 1
                logger.warning(
                    "analysis queue full (%d); dropping error from %s",
                    self.max_queue_size,
                    event.log_path,
                )
                return False

            heapq.heappush(self._heap, item)
            self._pending_signatures[signature] = item

        self._work_available.set()
        return True

    def _shed_lowest(self, incoming_priority: int) -> bool:
        """Drop the lowest-priority queued item to make room. Caller holds lock."""
        lowest = max(self._heap, key=lambda i: (-i.priority, -i.sort_key[1]))
        if lowest.priority >= incoming_priority:
            # Nothing queued is less urgent than what is arriving.
            return False

        self._heap.remove(lowest)
        heapq.heapify(self._heap)
        self._pending_signatures.pop(lowest.event.signature(), None)
        self.dropped_count += 1
        logger.warning(
            "analysis queue full; shed lower-severity error from %s",
            lowest.event.log_path,
        )
        return True

    # ---------------- analysis ----------------

    def process_next(self) -> Optional[AnalysisResult]:
        """Analyse the highest-priority queued error, if any."""
        with self._lock:
            if not self._heap:
                return None
            item = heapq.heappop(self._heap)
            self._pending_signatures.pop(item.event.signature(), None)

        knowledge = ""
        if self.knowledge_provider is not None:
            try:
                knowledge = self.knowledge_provider(item.event.workload_id)
            except Exception:  # pragma: no cover - guidance is best-effort
                logger.exception("knowledge provider failed; analysing without it")

        started = time.monotonic()
        try:
            payload = self.llm_client.analyze(
                item.event, item.metadata, item.occurrences, knowledge
            )
        except LLMError as exc:
            self.failed_count += 1
            logger.error("analysis failed for %s: %s", item.event.log_path, exc)
            return None

        result = AnalysisResult(
            event=item.event,
            workload_metadata=item.metadata,
            occurrences=item.occurrences,
            analysis_duration_ms=int((time.monotonic() - started) * 1000),
            **payload,
        )
        self.analyzed_count += 1

        with self._lock:
            self._cache[item.event.signature()] = (time.monotonic() + self.cache_ttl, result)

        if result.confidence < 0.5:
            logger.warning(
                "low-confidence analysis (%.2f) for %s; human review recommended",
                result.confidence,
                item.event.log_path,
            )

        self._emit(result)
        return result

    def _emit(self, result: AnalysisResult) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(result)
        except Exception:  # pragma: no cover - defensive
            logger.exception("analysis result callback failed")

    def _get_cached(self, signature: str) -> Optional[AnalysisResult]:
        """Return a still-valid cached analysis, purging it once expired."""
        entry = self._cache.get(signature)
        if entry is None:
            return None
        expiry, result = entry
        if time.monotonic() > expiry:
            del self._cache[signature]
            return None
        return result

    def is_recurring(self, event: ErrorEvent) -> bool:
        return self._recurrence.is_recurring(event.signature())

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        """Start the background analysis worker."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="error-analyzer", daemon=True
        )
        self._thread.start()
        logger.info("error analysis worker started")

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.process_next() is None:
                # Queue drained: sleep until submit() signals more work.
                self._work_available.wait(1.0)
                self._work_available.clear()

    def stop(self, timeout: float = 35.0) -> None:
        """Stop the worker, allowing an in-flight inference to finish."""
        self._stop.set()
        self._work_available.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("error analysis worker stopped")
