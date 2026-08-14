# Issue tracking

## Purpose

Maintain work items in external issue trackers for problems that warrant human
action, so that operators find them in the system they already use.

One problem maps to one issue for the life of that problem: created when it
first crosses the severity threshold, updated as it recurs, resolved when it
stops, and reopened if it returns. Deduplication and rate limiting are
requirements rather than refinements — a per-minute error must not produce a
per-minute comment, and a node returning from an outage must not open an issue
for every occurrence it missed.

Resolution reflects the absence of further occurrences, never a claim that the
underlying cause was repaired.

## Requirements

### Requirement: One issue per problem
The system SHALL maintain at most one open issue per distinct problem per workload, regardless of how many times that problem recurs.

#### Scenario: First occurrence
- **WHEN** a problem crosses the issue-creation threshold for the first time
- **THEN** system creates one issue and records its external reference against the problem

#### Scenario: Problem recurs
- **WHEN** a problem with an existing open issue occurs again
- **THEN** system updates the existing issue rather than creating another

#### Scenario: Distinct problems in one workload
- **WHEN** two different problems occur in the same workload
- **THEN** system creates a separate issue for each

#### Scenario: Same problem in different workloads
- **WHEN** the same error signature occurs in two different workloads
- **THEN** system creates a separate issue for each workload

#### Scenario: Occurrences differing only by numbers
- **WHEN** occurrences differ only in embedded values such as timestamps, counters, or process identifiers
- **THEN** system treats them as one problem and does not create additional issues

### Requirement: Issue creation threshold
The system SHALL create issues only for problems meeting a configurable severity threshold, so that low-value findings do not generate work items.

#### Scenario: Severity above threshold
- **WHEN** a problem's severity meets or exceeds the configured threshold
- **THEN** system creates an issue for it

#### Scenario: Severity below threshold
- **WHEN** a problem's severity is below the threshold
- **THEN** system does not create an issue, while event sinks still receive the record

#### Scenario: Severity revised upward by re-analysis
- **WHEN** re-analysis raises a problem's severity to meet the threshold
- **THEN** system creates an issue at that point

### Requirement: Issue updates are rate limited
The system SHALL limit how often it comments on an existing issue, so that a frequently recurring problem does not flood it.

#### Scenario: Rapid recurrence
- **WHEN** a problem recurs more often than the configured update interval
- **THEN** system posts at most one update per interval, carrying the accumulated occurrence count

#### Scenario: Update content
- **WHEN** system updates an issue
- **THEN** the update states the total occurrences and the period over which they occurred

#### Scenario: Analysis changed
- **WHEN** re-analysis produces a different root cause for a problem with an open issue
- **THEN** system posts the revised analysis regardless of the rate limit

### Requirement: Issue resolution
The system SHALL resolve an issue when its problem stops recurring, and SHALL make clear that this reflects absence of recurrence rather than confirmed repair.

#### Scenario: Problem stops recurring
- **WHEN** a problem has not recurred for the configured resolve window
- **THEN** system resolves the issue with a comment stating the problem has not recurred since a given time

#### Scenario: Resolution wording
- **WHEN** system resolves an issue
- **THEN** the closing comment states that recurrence stopped, and does not assert that the problem was fixed

#### Scenario: Problem returns after resolution
- **WHEN** a resolved problem occurs again
- **THEN** system reopens the issue, or creates a new one when the tracker does not permit reopening, and links it to the previous issue

### Requirement: Reconciliation after disconnection
The system SHALL reconcile issue state after a period without connectivity, without creating duplicates for problems that occurred while offline.

#### Scenario: Problem first occurred while offline
- **WHEN** connectivity returns and a problem occurred for the first time during the outage
- **THEN** system creates one issue reflecting the accumulated occurrences

#### Scenario: Known problem recurred while offline
- **WHEN** connectivity returns and a problem with an existing issue recurred during the outage
- **THEN** system posts a single update rather than one per occurrence

#### Scenario: Issue closed externally during the outage
- **WHEN** an operator closed the issue while the node was offline and the problem has since recurred
- **THEN** system reopens or recreates the issue rather than silently discarding the recurrence

### Requirement: Issue content
The system SHALL include enough context in an issue for someone who cannot access the node to act on it.

#### Scenario: Created issue
- **WHEN** system creates an issue
- **THEN** it includes the service identity and version, the node, severity, confidence, first and last occurrence times, occurrence count, the root cause, and the remediation steps

#### Scenario: AI-generated content is marked
- **WHEN** system creates or updates an issue
- **THEN** the content states that the analysis was generated by an AI system and requires validation before execution

#### Scenario: Redaction level respected
- **WHEN** the sink is configured below full redaction level
- **THEN** the issue omits the log context window

### Requirement: Dry-run mode
The system SHALL support a mode that reports intended issue actions without performing them, so an operator can validate configuration before it writes to a live tracker.

#### Scenario: Dry run enabled
- **WHEN** an issue sink is configured in dry-run mode
- **THEN** system logs the issue it would create or update, and makes no change to the tracker

#### Scenario: Leaving dry run
- **WHEN** dry-run mode is disabled after having been enabled
- **THEN** system creates issues for problems still active, without backfilling ones that have since resolved

### Requirement: Supported trackers
The system SHALL support issue creation and update against commonly used trackers.

#### Scenario: GitHub Issues
- **WHEN** a GitHub issue sink is configured
- **THEN** system creates and updates issues in the configured repository

#### Scenario: Jira
- **WHEN** a Jira sink is configured
- **THEN** system creates and updates issues in the configured project

#### Scenario: ServiceNow
- **WHEN** a ServiceNow sink is configured
- **THEN** system creates and updates records in the configured table
