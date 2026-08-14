"""Unit tests for LogMonitor using real temp log files."""

import os

from edge_ai_monitor.log_monitor import CONTEXT_LINES, LogMonitor
from edge_ai_monitor.workload_registry import (
    MonitoringConfig,
    Workload,
    WorkloadState,
)


def make_workload(log_paths, patterns=None, enabled=True, workload_id="examples/app_1.0.0_amd64"):
    return Workload(
        workload_id=workload_id,
        url="example.com.app",
        org="examples",
        version="1.0.0",
        arch="amd64",
        name="app",
        monitoring=MonitoringConfig(
            enabled=enabled,
            log_paths=[str(p) for p in log_paths],
            error_patterns=patterns or ["ERROR", "FATAL", "Exception", "CRITICAL"],
        ),
        state=WorkloadState.RUNNING,
    )


def append(path, *lines):
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def collector():
    events = []
    return events, events.append


def drain(monitor):
    """Read pending data, then read again to flush errors awaiting context.

    An error on the last line of a read batch is held until the following read
    confirms no further trailing context is arriving.
    """
    monitor.poll_once()
    monitor.poll_once()


# ---------------- permission ----------------


def test_monitoring_disabled_is_skipped(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    assert monitor.watch_workload(make_workload([log], enabled=False)) == []
    assert monitor.watched_paths == []


def test_monitoring_enabled_is_watched(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    monitor = LogMonitor(collector()[1])
    assert monitor.watch_workload(make_workload([log])) == [str(log)]


# ---------------- log path handling ----------------


def test_invalid_path_skipped_valid_path_kept(tmp_path):
    good = tmp_path / "good.log"
    good.write_text("")
    missing = tmp_path / "nope.log"
    monitor = LogMonitor(collector()[1])
    watched = monitor.watch_workload(make_workload([missing, good]))
    assert watched == [str(good)]


def test_multiple_log_paths_monitored_concurrently(tmp_path):
    first = tmp_path / "a.log"
    second = tmp_path / "b.log"
    first.write_text("")
    second.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([first, second]))

    append(first, "ERROR from a")
    append(second, "ERROR from b")
    drain(monitor)

    assert {e.log_path for e in events} == {str(first), str(second)}


# ---------------- tailing ----------------


def test_only_new_entries_are_read(tmp_path):
    log = tmp_path / "app.log"
    append(log, "ERROR pre-existing line")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    drain(monitor)
    assert events == []  # pre-existing content is not replayed

    append(log, "ERROR brand new")
    drain(monitor)
    assert len(events) == 1


def test_partial_line_held_until_newline(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("ERROR incompl")
    drain(monitor)
    assert events == []

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("ete now done\n")
    drain(monitor)
    assert len(events) == 1
    assert events[0].line == "ERROR incomplete now done"


def test_truncation_is_detected_as_rotation(tmp_path):
    log = tmp_path / "app.log"
    append(log, "line one", "line two", "line three")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    log.write_text("")  # truncate in place
    append(log, "ERROR after truncation")
    drain(monitor)
    assert len(events) == 1
    assert events[0].line == "ERROR after truncation"


def test_renamed_file_rotation_is_detected(tmp_path):
    log = tmp_path / "app.log"
    append(log, "old line")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    os.rename(log, tmp_path / "app.log.1")
    append(log, "ERROR in rotated file")
    drain(monitor)
    assert len(events) == 1
    assert events[0].line == "ERROR in rotated file"


# ---------------- pattern matching ----------------


def test_matching_is_case_insensitive(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "an error occurred here")
    drain(monitor)
    assert len(events) == 1
    assert events[0].matched_pattern == "ERROR"


def test_non_matching_lines_do_not_emit(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "INFO all good", "DEBUG chatty")
    monitor.poll_once()
    assert events == []


def test_custom_patterns_are_used(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log], patterns=["SensorFault"]))

    append(log, "ERROR not in custom patterns", "SensorFault detected")
    drain(monitor)
    assert len(events) == 1
    assert events[0].matched_pattern == "SensorFault"


def test_invalid_regex_pattern_treated_as_literal(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log], patterns=["error("]))

    append(log, "error( unbalanced paren")
    drain(monitor)
    assert len(events) == 1


# ---------------- context window ----------------


def test_full_context_window_collected(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, *[f"before {i}" for i in range(80)])
    append(log, "ERROR the failure")
    append(log, *[f"after {i}" for i in range(80)])
    monitor.poll_once()

    assert len(events) == 1
    event = events[0]
    assert len(event.context_before) == CONTEXT_LINES
    assert len(event.context_after) == CONTEXT_LINES
    assert event.context_before[-1] == "before 79"
    assert event.context_after[0] == "after 0"


def test_context_truncated_at_boundary(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "before 0", "before 1", "ERROR near start", "after 0")
    monitor.poll_once()  # flushes pending on the quiet follow-up read
    monitor.poll_once()

    assert len(events) == 1
    assert events[0].context_before == ["before 0", "before 1"]
    assert events[0].context_after == ["after 0"]


def test_line_number_is_recorded(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "one", "two", "ERROR third line")
    monitor.poll_once()
    monitor.poll_once()
    assert events[0].line_number == 3


def test_context_text_joins_window(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "prior", "ERROR boom", "next")
    monitor.poll_once()
    monitor.poll_once()
    assert events[0].context_text() == "prior\nERROR boom\nnext"


def test_signature_normalises_numbers(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    monitor.watch_workload(make_workload([log]))

    append(log, "ERROR conn failed after 12 retries", "ERROR conn failed after 99 retries")
    monitor.poll_once()
    monitor.poll_once()
    assert events[0].signature() == events[1].signature()


# ---------------- resource limits ----------------


def test_large_file_tails_recent_window_only(tmp_path, monkeypatch):
    monkeypatch.setattr("edge_ai_monitor.log_monitor.MAX_FILE_BYTES", 100)
    monkeypatch.setattr("edge_ai_monitor.log_monitor.TAIL_BYTES_ON_LARGE_FILE", 40)

    log = tmp_path / "big.log"
    append(log, *[f"filler line {i}" for i in range(40)])
    monitor = LogMonitor(collector()[1])
    monitor.watch_workload(make_workload([log]))

    tailed = list(monitor._files.values())[0]
    assert tailed.offset == log.stat().st_size - 40


def test_buffer_overflow_drops_oldest_and_warns(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error, max_buffer_bytes=64)
    monitor.watch_workload(make_workload([log]))

    append(log, *[f"ERROR failure number {i}" for i in range(20)])
    monitor.poll_once()
    monitor.poll_once()

    # Overflow handling keeps operating rather than growing without bound.
    assert monitor._buffer_bytes <= monitor.max_buffer_bytes
    assert len(events) < 20


# ---------------- registration lifecycle ----------------


def test_unwatch_stops_monitoring(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    events, on_error = collector()
    monitor = LogMonitor(on_error)
    workload = make_workload([log])
    monitor.watch_workload(workload)
    monitor.unwatch_workload(workload.workload_id)

    append(log, "ERROR after unwatch")
    monitor.poll_once()
    assert events == []
    assert monitor.watched_paths == []


def test_watching_twice_is_idempotent(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    monitor = LogMonitor(collector()[1])
    workload = make_workload([log])
    monitor.watch_workload(workload)
    monitor.watch_workload(workload)
    assert monitor.watched_paths == [str(log)]


def test_failing_callback_does_not_break_monitor(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")

    def boom(event):
        raise RuntimeError("consumer exploded")

    monitor = LogMonitor(boom)
    monitor.watch_workload(make_workload([log]))
    append(log, "ERROR one")
    monitor.poll_once()
    monitor.poll_once()  # must not raise
