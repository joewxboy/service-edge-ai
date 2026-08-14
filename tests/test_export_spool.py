"""Unit tests for the store-and-forward spool."""

import json
import time

from edge_ai_monitor.export.spool import Spool, backoff_delay


def make_spool(tmp_path, **kwargs):
    return Spool(directory=str(tmp_path / "spool"), **kwargs)


def payload(n=1):
    return {"problem_key": f"key{n}", "severity": "high", "idempotency_key": f"key{n}-1"}


# ---------------- persistence ----------------


def test_record_is_written_before_delivery(tmp_path):
    spool = make_spool(tmp_path)
    path = spool.put("webhook", payload())
    assert path is not None and path.exists()
    assert spool.depth == 1


def test_pending_returns_records_oldest_first(tmp_path):
    spool = make_spool(tmp_path)
    for i in range(3):
        spool.put("webhook", payload(i))
    keys = [r.payload["problem_key"] for r in spool.pending()]
    assert keys == ["key0", "key1", "key2"]


def test_success_removes_the_record(tmp_path):
    spool = make_spool(tmp_path)
    spool.put("webhook", payload())
    spool.succeed(spool.pending()[0])
    assert spool.depth == 0
    assert spool.delivered_count == 1


def test_records_survive_a_restart(tmp_path):
    first = make_spool(tmp_path)
    first.put("webhook", payload())

    # A new Spool over the same directory is what a restart looks like.
    second = make_spool(tmp_path)
    assert second.depth == 1
    assert second.pending()[0].payload["problem_key"] == "key1"


def test_unreadable_entry_is_discarded(tmp_path):
    spool = make_spool(tmp_path)
    path = spool.put("webhook", payload())
    path.write_text("{ corrupt")
    assert spool.pending() == []
    assert spool.depth == 0


# ---------------- retry and backoff ----------------


def test_failure_schedules_a_retry(tmp_path):
    spool = make_spool(tmp_path)
    spool.put("webhook", payload())
    record = spool.pending()[0]
    spool.fail(record)

    still_there = spool.pending()[0]
    assert still_there.attempts == 1
    assert still_there.next_attempt > time.time()
    assert spool.depth == 1


def test_failed_record_is_not_due_immediately(tmp_path):
    spool = make_spool(tmp_path)
    spool.put("webhook", payload())
    spool.fail(spool.pending()[0])
    assert spool.due() == []


def test_backoff_grows_with_attempts():
    delays = [backoff_delay(n, base=10.0) for n in range(1, 6)]
    # Jittered, so compare envelopes rather than exact values.
    assert min(delays[3:]) > max(delays[:1])


def test_backoff_is_capped():
    assert backoff_delay(50, base=30.0, maximum=3600.0) <= 3600.0


def test_backoff_is_jittered():
    """A fleet reconnecting together must not retry in lockstep."""
    values = {backoff_delay(3, base=30.0) for _ in range(20)}
    assert len(values) > 1


# ---------------- limits ----------------


def test_size_limit_drops_oldest_first(tmp_path):
    spool = make_spool(tmp_path, max_bytes=1200)
    for i in range(20):
        spool.put("webhook", {**payload(i), "filler": "x" * 200})

    assert spool.size_bytes() <= 1200
    assert spool.dropped_count > 0
    # What survives is the newest, not the oldest.
    remaining = [r.payload["problem_key"] for r in spool.pending()]
    assert "key19" in remaining
    assert "key0" not in remaining


def test_expired_records_are_purged(tmp_path):
    spool = make_spool(tmp_path, max_age_seconds=0.0)
    spool.put("webhook", payload())
    # Age limit of zero makes anything already queued expired.
    assert spool.purge_expired() == 1
    assert spool.depth == 0
    assert spool.expired_count == 1


def test_expired_record_is_dropped_on_failure(tmp_path):
    spool = make_spool(tmp_path, max_age_seconds=0.0)
    spool.put("webhook", payload())
    spool.fail(spool.pending()[0])
    assert spool.depth == 0
    assert spool.expired_count == 1


def test_fresh_record_is_not_expired(tmp_path):
    spool = make_spool(tmp_path, max_age_seconds=3600.0)
    spool.put("webhook", payload())
    assert spool.purge_expired() == 0
    assert spool.depth == 1


# ---------------- reporting ----------------


def test_describe_reports_counters(tmp_path):
    spool = make_spool(tmp_path)
    spool.put("webhook", payload())
    described = spool.describe()
    assert described["export_queue_depth"] == 1
    assert described["export_queue_bytes"] > 0
    assert described["exports_delivered"] == 0


def test_unwritable_directory_does_not_raise(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    spool = Spool(directory=str(blocker / "spool"))
    assert spool.put("webhook", payload()) is None
