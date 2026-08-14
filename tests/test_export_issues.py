"""Unit tests for issue lifecycle and tracker sinks."""

import json
import time

import pytest

from edge_ai_monitor.export.issue_sinks import (
    GitHubIssueSink,
    IssueSink,
    JiraIssueSink,
    ServiceNowIssueSink,
    SinkError,
    build_issue_sink,
)
from edge_ai_monitor.export.lifecycle import IssueLedger, IssueLifecycle

RECORD = {
    "problem_key": "abc123",
    "workload_id": "examples/sensor_1.2.0_amd64",
    "service_name": "sensor",
    "service_version": "1.2.0",
    "organization": "examples",
    "severity": "high",
    "confidence": 0.9,
    "first_seen": "2026-08-14T10:00:00+00:00",
    "last_seen": "2026-08-14T12:00:00+00:00",
    "total_occurrences": 5,
    "root_cause": "Database is down",
    "remediation_steps": ["Restart the database"],
    "node": {"id": "myorg/edge-1"},
}


class FakeTracker(IssueSink):
    """In-memory tracker recording every operation."""

    def __init__(self, name="tracker", supports_reopen=True, dry_run=False):
        super().__init__(name, dry_run=dry_run)
        self.supports_reopen = supports_reopen
        self.issues = {}
        self.comments = []
        self.actions = []
        self._next = 1

    def create(self, title, body, record):
        reference = str(self._next)
        self._next += 1
        self.issues[reference] = {"title": title, "body": body, "state": "open"}
        self.actions.append(("create", reference))
        return reference

    def comment(self, reference, body):
        self.comments.append((reference, body))
        self.actions.append(("comment", reference))

    def resolve(self, reference, body):
        self.issues[reference]["state"] = "closed"
        self.comments.append((reference, body))
        self.actions.append(("resolve", reference))

    def reopen(self, reference, body):
        if not self.supports_reopen:
            raise SinkError("reopen unsupported")
        self.issues[reference]["state"] = "open"
        self.actions.append(("reopen", reference))

    def is_open(self, reference):
        issue = self.issues.get(reference)
        return None if issue is None else issue["state"] == "open"


def recent(seconds_ago=0):
    """A record whose last_seen is relative to now, for resolution tests."""
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return {**RECORD, "last_seen": when.isoformat()}


def make_lifecycle(tmp_path, tracker=None, **kwargs):
    tracker = tracker or FakeTracker()
    ledger = IssueLedger(str(tmp_path / "issues.json"))
    return IssueLifecycle(tracker, ledger, **kwargs), tracker


# ---------------- one issue per problem ----------------


def test_first_occurrence_creates_one_issue(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    reference = lifecycle.handle(RECORD)
    assert reference == "1"
    assert len(tracker.issues) == 1
    assert lifecycle.created_count == 1


def test_recurrence_does_not_create_a_second_issue(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=0.0)
    for _ in range(10):
        lifecycle.handle(RECORD)
    assert len(tracker.issues) == 1


def test_distinct_problems_get_distinct_issues(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle(RECORD)
    lifecycle.handle({**RECORD, "problem_key": "def456"})
    assert len(tracker.issues) == 2


def test_record_without_problem_key_is_ignored(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    assert lifecycle.handle({**RECORD, "problem_key": ""}) is None
    assert tracker.issues == {}


# ---------------- severity threshold ----------------


def test_below_threshold_creates_nothing(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, min_severity="high")
    assert lifecycle.handle({**RECORD, "severity": "medium"}) is None
    assert tracker.issues == {}
    assert lifecycle.skipped_count == 1


def test_at_threshold_creates_an_issue(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, min_severity="high")
    assert lifecycle.handle({**RECORD, "severity": "critical"}) is not None


def test_threshold_all_tracks_everything(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, min_severity="all")
    assert lifecycle.handle({**RECORD, "severity": "low"}) is not None


def test_severity_raised_later_creates_the_issue(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, min_severity="high")
    lifecycle.handle({**RECORD, "severity": "medium"})
    assert tracker.issues == {}
    lifecycle.handle({**RECORD, "severity": "critical"})
    assert len(tracker.issues) == 1


# ---------------- rate limiting ----------------


def test_rapid_recurrence_is_rate_limited(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=3600.0)
    now = time.time()
    lifecycle.handle(RECORD, now=now)
    for i in range(1, 20):
        lifecycle.handle(RECORD, now=now + i)      # all within the hour
    assert tracker.comments == []                   # creation is not a comment


def test_comment_posted_once_the_interval_elapses(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=3600.0)
    now = time.time()
    lifecycle.handle(RECORD, now=now)
    lifecycle.handle(RECORD, now=now + 3601)
    assert len(tracker.comments) == 1
    assert "occurrence(s)" in tracker.comments[0][1]


def test_update_carries_the_occurrence_count(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=0.0)
    lifecycle.handle(RECORD)
    lifecycle.handle({**RECORD, "total_occurrences": 99})
    assert "99" in tracker.comments[-1][1]


def test_revised_analysis_bypasses_the_rate_limit(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=3600.0)
    now = time.time()
    lifecycle.handle(RECORD, now=now)
    lifecycle.handle({**RECORD, "root_cause": "Actually a DNS failure"}, now=now + 1)
    assert len(tracker.comments) == 1
    assert "DNS failure" in tracker.comments[0][1]


def test_quiet_recurrence_still_updates_last_seen(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=3600.0)
    now = time.time()
    lifecycle.handle(RECORD, now=now)
    lifecycle.handle({**RECORD, "last_seen": "2026-08-14T18:00:00+00:00"}, now=now + 1)
    assert lifecycle.ledger.get("abc123").last_seen == "2026-08-14T18:00:00+00:00"


# ---------------- resolution ----------------


def test_quiet_problem_is_resolved(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, resolve_after=60.0)
    now = time.time()
    lifecycle.handle(recent(seconds_ago=600), now=now)   # last seen 10 min ago
    assert lifecycle.resolve_stale(now=now) == 1
    assert tracker.issues["1"]["state"] == "closed"


def test_active_problem_is_not_resolved(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, resolve_after=3600.0)
    now = time.time()
    lifecycle.handle(recent(seconds_ago=5), now=now)     # last seen 5s ago
    assert lifecycle.resolve_stale(now=now) == 0
    assert tracker.issues["1"]["state"] == "open"


def test_resolution_uses_last_occurrence_not_last_comment(tmp_path):
    """An issue commented on recently is still stale if the error stopped."""
    lifecycle, tracker = make_lifecycle(tmp_path, resolve_after=60.0)
    now = time.time()
    lifecycle.handle(recent(seconds_ago=3600), now=now)  # commented now, seen an hour ago
    assert lifecycle.resolve_stale(now=now) == 1


def test_closing_comment_does_not_claim_a_fix(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, resolve_after=0.0)
    lifecycle.handle(recent())
    lifecycle.resolve_stale(now=time.time() + 10)
    closing = tracker.comments[-1][1]
    assert "not recurred" in closing
    assert "not** confirmation" in closing or "not confirmation" in closing
    assert "fixed" not in closing.replace("was fixed", "")


def test_resolved_problem_recurring_reopens(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, resolve_after=0.0)
    lifecycle.handle(recent())
    lifecycle.resolve_stale(now=time.time() + 10)
    lifecycle.handle(recent())
    assert tracker.issues["1"]["state"] == "open"
    assert lifecycle.reopened_count == 1
    assert len(tracker.issues) == 1


def test_tracker_without_reopen_creates_a_linked_issue(tmp_path):
    tracker = FakeTracker(supports_reopen=False)
    lifecycle, _ = make_lifecycle(tmp_path, tracker=tracker, resolve_after=0.0)
    lifecycle.handle(recent())
    lifecycle.resolve_stale(now=time.time() + 10)
    lifecycle.handle(recent())

    assert len(tracker.issues) == 2
    assert any("previously closed issue 1" in body for _, body in tracker.comments)


def test_issue_closed_externally_is_reopened_on_recurrence(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path, update_interval=0.0)
    lifecycle.handle(RECORD)
    tracker.issues["1"]["state"] = "closed"      # someone closed it by hand
    lifecycle.handle(RECORD)
    assert tracker.issues["1"]["state"] == "open"


# ---------------- offline reconciliation ----------------


def test_offline_accumulation_creates_one_issue(tmp_path):
    """Occurrences accumulated while offline must not become many issues."""
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle({**RECORD, "total_occurrences": 500})
    assert len(tracker.issues) == 1
    assert "500" in tracker.issues["1"]["body"]


def test_ledger_survives_restart(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle(RECORD)

    # A fresh ledger over the same file is what a restart looks like.
    revived = IssueLifecycle(tracker, IssueLedger(str(tmp_path / "issues.json")), update_interval=0.0)
    revived.handle(RECORD)
    assert len(tracker.issues) == 1          # not recreated
    assert revived.updated_count == 1


def test_corrupt_ledger_starts_fresh(tmp_path):
    path = tmp_path / "issues.json"
    path.write_text("{ corrupt")
    ledger = IssueLedger(str(path))
    assert len(ledger) == 0


# ---------------- dry run ----------------


def test_dry_run_creates_nothing(tmp_path):
    tracker = FakeTracker(dry_run=True)
    lifecycle, _ = make_lifecycle(tmp_path, tracker=tracker)
    assert lifecycle.handle(RECORD) is None
    assert tracker.issues == {}
    assert tracker.actions == []


# ---------------- issue content ----------------


def test_issue_body_carries_actionable_context(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle(RECORD)
    body = tracker.issues["1"]["body"]
    for expected in ("sensor", "examples", "myorg/edge-1", "Database is down",
                     "Restart the database", "abc123", "2026-08-14T10:00:00+00:00"):
        assert expected in body


def test_issue_body_marks_ai_generated_content(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle(RECORD)
    assert "generated by an AI system" in tracker.issues["1"]["body"]


def test_title_includes_severity_and_service(tmp_path):
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle({**RECORD, "error_summary": "Connection refused"})
    title = tracker.issues["1"]["title"]
    assert "high" in title and "sensor" in title


def test_body_omits_log_location_when_redacted(tmp_path):
    """A metadata-level record has no log path, so the body must not invent one."""
    lifecycle, tracker = make_lifecycle(tmp_path)
    lifecycle.handle({k: v for k, v in RECORD.items()})
    assert "## Location" not in tracker.issues["1"]["body"]


# ---------------- tracker construction ----------------


def test_build_issue_sink_by_type():
    sink = build_issue_sink("gh", {"type": "github", "repository": "o/r", "token": "t"})
    assert isinstance(sink, GitHubIssueSink)


def test_build_issue_sink_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_issue_sink("x", {"type": "postits"})


def test_build_issue_sink_strips_lifecycle_settings():
    sink = build_issue_sink(
        "gh",
        {"type": "github", "repository": "o/r", "token": "t",
         "min_severity": "high", "resolve_after": 60, "level": "analysis"},
    )
    assert isinstance(sink, GitHubIssueSink)


def test_servicenow_does_not_support_reopen():
    sink = ServiceNowIssueSink("sn", instance_url="https://x", username="u", password="p")
    assert sink.supports_reopen is False


def test_tracker_tokens_are_registered_as_secrets():
    gh = GitHubIssueSink("gh", repository="o/r", token="ghp_secretvalue")
    assert "ghp_secretvalue" not in gh.scrub("failed with ghp_secretvalue")
    jira = JiraIssueSink("j", base_url="https://x", project="P", email="e", token="jira_secret")
    assert "jira_secret" not in jira.scrub("bad token jira_secret")
