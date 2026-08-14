"""Unit tests for ErrorAnalyzer queueing, aggregation, and caching."""

import pytest

from edge_ai_monitor.error_analyzer import (
    SEVERITY_RANK,
    AnalysisResult,
    ErrorAnalyzer,
    estimate_priority,
)
from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.ollama_client import LLMError

METADATA = {"service_name": "sensor", "version": "1.2.0"}


class FakeLLM:
    """Returns a canned analysis, recording every call."""

    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "error_summary": "Connection refused",
            "root_cause": "Database is down",
            "severity": "high",
            "remediation_steps": ["Restart the database"],
            "confidence": 0.9,
        }
        self.error = error
        self.calls = []

    def analyze(self, event, metadata, occurrences=1, knowledge=""):
        self.calls.append((event, metadata, occurrences, knowledge))
        if self.error:
            raise self.error
        return dict(self.payload)


def make_event(line="ERROR could not connect", workload_id="examples/app_1.0.0_amd64", **kw):
    return ErrorEvent(
        workload_id=workload_id,
        log_path=kw.get("log_path", "/var/log/app.log"),
        line_number=kw.get("line_number", 10),
        line=line,
        matched_pattern=kw.get("matched_pattern", "ERROR"),
    )


def make_analyzer(llm=None, **kwargs):
    results = []
    analyzer = ErrorAnalyzer(
        llm_client=llm or FakeLLM(),
        metadata_provider=lambda wid: dict(METADATA),
        on_result=results.append,
        **kwargs,
    )
    return analyzer, results


# ---------------- priority estimation ----------------


def test_fatal_lines_get_critical_priority():
    assert estimate_priority(make_event("FATAL kernel panic")) == SEVERITY_RANK["critical"]


def test_exception_lines_get_high_priority():
    assert estimate_priority(make_event("Exception in thread main")) == SEVERITY_RANK["high"]


def test_plain_errors_get_default_priority():
    assert estimate_priority(make_event("ERROR something mundane")) == SEVERITY_RANK["medium"]


# ---------------- queueing ----------------


def test_submit_enqueues_without_blocking():
    analyzer, results = make_analyzer()
    assert analyzer.submit(make_event()) is True
    assert analyzer.queue_depth == 1
    assert results == []  # analysis has not run yet


def test_process_next_returns_analysis():
    analyzer, results = make_analyzer()
    analyzer.submit(make_event())
    result = analyzer.process_next()
    assert result.severity == "high"
    assert result.root_cause == "Database is down"
    assert results == [result]
    assert analyzer.queue_depth == 0


def test_process_next_on_empty_queue_returns_none():
    analyzer, _ = make_analyzer()
    assert analyzer.process_next() is None


def test_higher_severity_is_processed_first():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event("ERROR mundane thing", log_path="/var/log/low.log"))
    analyzer.submit(make_event("FATAL total meltdown", log_path="/var/log/high.log"))
    assert analyzer.process_next().event.log_path == "/var/log/high.log"
    assert analyzer.process_next().event.log_path == "/var/log/low.log"


def test_equal_severity_is_processed_in_order():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event("ERROR first issue", log_path="/var/log/1.log"))
    analyzer.submit(make_event("ERROR second issue", log_path="/var/log/2.log"))
    assert analyzer.process_next().event.log_path == "/var/log/1.log"
    assert analyzer.process_next().event.log_path == "/var/log/2.log"


def test_analysis_records_duration():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event())
    assert analyzer.process_next().analysis_duration_ms >= 0


def test_llm_failure_is_counted_not_raised():
    analyzer, results = make_analyzer(llm=FakeLLM(error=LLMError("model unavailable")))
    analyzer.submit(make_event())
    assert analyzer.process_next() is None
    assert analyzer.failed_count == 1
    assert results == []


# ---------------- queue overflow ----------------


def test_queue_sheds_lowest_severity_when_full():
    analyzer, _ = make_analyzer(max_queue_size=3)
    # Distinct messages: identical text would be aggregated into one entry.
    for i, message in enumerate(["ERROR disk full", "ERROR bad config", "ERROR no route"]):
        assert analyzer.submit(make_event(message, log_path=f"/var/log/{i}.log"))
    assert analyzer.queue_depth == 3

    # A critical error displaces a queued medium-severity one.
    assert analyzer.submit(make_event("FATAL meltdown", log_path="/var/log/crit.log"))
    assert analyzer.queue_depth == 3
    assert analyzer.dropped_count == 1
    assert analyzer.process_next().event.log_path == "/var/log/crit.log"


def test_queue_rejects_when_full_and_nothing_less_urgent():
    analyzer, _ = make_analyzer(max_queue_size=2)
    analyzer.submit(make_event("FATAL one", log_path="/var/log/a.log"))
    analyzer.submit(make_event("FATAL two", log_path="/var/log/b.log"))

    assert analyzer.submit(make_event("ERROR mundane", log_path="/var/log/c.log")) is False
    assert analyzer.dropped_count == 1
    assert analyzer.queue_depth == 2


# ---------------- recurrence aggregation ----------------


def test_repeated_error_aggregates_into_one_queue_entry():
    analyzer, _ = make_analyzer()
    for _ in range(4):
        analyzer.submit(make_event("ERROR conn refused to db"))
    assert analyzer.queue_depth == 1

    result = analyzer.process_next()
    assert result.occurrences == 4


def test_recurrence_counts_ignore_embedded_numbers():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event("ERROR retry 1 of 5 failed"))
    analyzer.submit(make_event("ERROR retry 2 of 5 failed"))
    analyzer.submit(make_event("ERROR retry 3 of 5 failed"))
    assert analyzer.queue_depth == 1
    assert analyzer.is_recurring(make_event("ERROR retry 9 of 5 failed")) is True


def test_distinct_errors_are_not_aggregated():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event("ERROR database unreachable"))
    analyzer.submit(make_event("ERROR disk full on /var"))
    assert analyzer.queue_depth == 2


def test_unique_error_is_not_recurring():
    analyzer, _ = make_analyzer()
    event = make_event("ERROR one off")
    analyzer.submit(event)
    assert analyzer.is_recurring(event) is False


def test_occurrence_count_reaches_llm():
    llm = FakeLLM()
    analyzer, _ = make_analyzer(llm=llm)
    for _ in range(3):
        analyzer.submit(make_event("ERROR conn refused"))
    analyzer.process_next()
    assert llm.calls[0][2] == 3


# ---------------- caching ----------------


def test_identical_error_reuses_cached_analysis():
    llm = FakeLLM()
    analyzer, results = make_analyzer(llm=llm)

    analyzer.submit(make_event("ERROR conn refused"))
    analyzer.process_next()
    assert len(llm.calls) == 1

    # Same signature again after the queue drained: served from cache.
    analyzer.submit(make_event("ERROR conn refused"))
    assert analyzer.queue_depth == 0
    assert len(llm.calls) == 1
    assert results[-1].from_cache is True


def test_expired_cache_entry_triggers_reanalysis():
    llm = FakeLLM()
    analyzer, _ = make_analyzer(llm=llm, cache_ttl=-1.0)

    analyzer.submit(make_event("ERROR conn refused"))
    analyzer.process_next()
    analyzer.submit(make_event("ERROR conn refused"))
    analyzer.process_next()
    assert len(llm.calls) == 2


def test_low_confidence_result_is_still_emitted():
    llm = FakeLLM(
        {
            "error_summary": "Unclear failure",
            "root_cause": "Unknown",
            "severity": "medium",
            "remediation_steps": ["Collect more logs"],
            "confidence": 0.3,
        }
    )
    analyzer, results = make_analyzer(llm=llm)
    analyzer.submit(make_event())
    result = analyzer.process_next()
    assert result.confidence == 0.3
    assert results == [result]


def test_metadata_provider_supplies_workload_context():
    llm = FakeLLM()
    analyzer, _ = make_analyzer(llm=llm)
    analyzer.submit(make_event())
    analyzer.process_next()
    assert llm.calls[0][1]["service_name"] == "sensor"


def test_severity_rank_exposed_on_result():
    analyzer, _ = make_analyzer()
    analyzer.submit(make_event())
    assert analyzer.process_next().severity_rank == SEVERITY_RANK["high"]


def test_failing_result_callback_does_not_propagate():
    def boom(result):
        raise RuntimeError("consumer exploded")

    analyzer = ErrorAnalyzer(
        llm_client=FakeLLM(),
        metadata_provider=lambda wid: dict(METADATA),
        on_result=boom,
    )
    analyzer.submit(make_event())
    assert analyzer.process_next() is not None  # must not raise
