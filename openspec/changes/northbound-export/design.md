## Context

See proposal.md — Why.

**Current state:**
- Proposals are written to `/var/lib/monitor/proposals/<workload>/<problem-key>.json`,
  one file per distinct problem, updated in place as it recurs. Each carries
  `problem_key`, `first_seen`, `last_seen`, `total_occurrences`, and
  `analysis_count`.
- `RemediationGenerator.handle_result()` is the single funnel every proposal
  passes through, on two threads (the analyzer and the cache-hit path).
- The service makes no outbound connections beyond `localhost` (anax, Ollama).
- Four independent loops already exist, decoupled so nothing blocks log reading.

**Constraints that shape this design:**
- **Connectivity is unreliable.** This node's event log shows repeated heartbeat
  failures with `network is unreachable` and `i/o timeout` lasting minutes.
  Export cannot assume the network is there.
- **Resources are scarce.** The container is capped at 4 GB and shares a node
  with the workloads being monitored. A queue cannot grow without bound.
- **Privacy is a stated guarantee.** "Log content never leaves the node" is
  documented and is the basis on which workloads opt into monitoring.
- **Edge images should stay small.** The image is already ~5.6 GB because it
  bundles Ollama; vendor SDKs would make that worse.

## Goals / Non-Goals

**Goals:**
- Export is entirely optional and inert when unconfigured.
- No export failure can affect monitoring: a broken sink must never block log
  reading, analysis, or local proposal writing.
- Records survive restarts and outages, and are delivered once connectivity
  returns.
- One problem maps to one issue in a tracker, for the life of that problem.
- What each sink transmits is explicit and auditable.

**Non-Goals:**
- Bidirectional integration. Nothing external commands the monitor; this is
  outbound only.
- Executing remediation from a tracker (e.g. an "Apply fix" button).
- Alert routing, escalation, or on-call policy — that is the receiving
  platform's job.
- Exactly-once delivery. At-least-once with idempotency keys is the target;
  exactly-once across arbitrary third-party APIs is not achievable.
- A plugin API for third-party sinks. Sinks are in-tree until there is demand.

## Decisions

### 1. Export is a consumer of proposals, not a step in producing them

**Decision:** The exporter subscribes to completed proposals via a callback from
`RemediationGenerator`, and does its work on its own thread.

**Rationale:** The existing pipeline is deliberately decoupled so inference
latency cannot stall log reading. A network call is far less predictable than
inference. Making export a synchronous step would put a third-party API's
availability in the path of local monitoring.

**Alternatives considered:**
- *Inline in `handle_result`*: simplest, but a hung HTTP call blocks the analyzer
  thread and, through the cache-hit path, the log-monitor thread.
- *Separate sidecar container tailing the proposal directory*: clean isolation,
  but doubles the deployment footprint and loses the in-process metadata
  (`from_cache`, delivery state) that makes issue lifecycle tracking cheap.

### 2. Store-and-forward through a durable on-disk queue

**Decision:** Every export is written to a spool directory first, then delivered
by a worker. Successful delivery removes the spool entry; failure leaves it for
retry with exponential backoff.

**Rationale:** Edge connectivity is intermittent by nature. An in-memory queue
loses everything on the restart that an outage often causes.

**Bounded by policy, not by hope:**
- Maximum spool size (default 64 MB) and maximum age (default 7 days).
- On overflow, the **oldest** entries are dropped first and the drop is counted
  and logged — matching how the analysis queue already sheds work.

**Alternatives considered:**
- *In-memory queue*: loses records across restarts.
- *SQLite spool*: better querying, but adds a dependency and offers little over
  one file per pending export.
- *Unbounded spool*: risks filling the disk of a device that may have 8 GB total.

### 3. Two sink families with different contracts

**Decision:** Separate `EventSink` and `IssueSink` interfaces rather than one
generic sink.

**Rationale:** They differ in kind, not degree:

| | Event sinks | Issue sinks |
|---|---|---|
| Semantics | Fire-and-forget stream | Stateful record with a lifecycle |
| Volume | Every occurrence | One per distinct problem |
| Duplicate handling | Harmless | Must be prevented |
| Needs correlation | No | Yes — external id per problem |
| Failure mode | Lost datapoint | Duplicate or orphaned ticket |

Collapsing these into one interface would force issue trackers to reimplement
deduplication per sink, which is exactly where duplicate-ticket bugs come from.

### 4. `problem_key` is the correlation identity

**Decision:** The existing `problem_key` — a stable hash of the normalised error
signature — is the idempotency key for event sinks and the correlation key for
issue sinks. A local mapping of `problem_key -> external issue reference` is
persisted alongside the proposal.

**Rationale:** This is precisely the identity the proposal store already uses to
mean "the same ongoing problem", so issues inherit correct deduplication for
free. Digits are normalised out of the signature, so timestamps and counters do
not fragment one problem into many tickets.

**Alternatives considered:**
- *Searching the tracker by title on every occurrence*: an API call per
  occurrence, rate-limit exposure, and fragile against edited titles.
- *A new export-specific identity*: gratuitous divergence from the proposal store.

### 5. Issue lifecycle driven by proposal state

**Decision:**

| Condition | Action |
|---|---|
| First occurrence of a problem, above the severity threshold | Create issue, record its reference |
| Recurrence, existing issue open | Comment with updated occurrence count — rate-limited |
| Recurrence, issue closed externally | Reopen, or create a new one if reopening is not permitted |
| No recurrence for the resolve window | Resolve with a closing comment |
| Analysis changed for a known problem | Comment with the revised root cause |

**Rate limiting is essential.** An error firing every minute must not produce a
comment every minute. Comments are throttled (default: at most one per hour per
issue), with the occurrence count carrying the real signal.

**Resolution is a judgement call, not a fact.** The monitor knows the error
stopped appearing, not that it was fixed. Closing comments say exactly that.

### 6. Protocol coverage via standards, not vendor SDKs

**Decision:** Implement OTLP/HTTP, generic webhook, syslog (RFC 5424), and MQTT
for events; GitHub, Jira, and ServiceNow REST for issues. All over `requests`
plus, for MQTT, a single small client library.

**Rationale:** OTLP alone reaches most single-pane-of-glass platforms through
their existing collectors, and MQTT is the lingua franca of edge/IoT estates.
Vendor SDKs would multiply image size and dependency churn for the same
outcome — an HTTP POST with the right shape.

**Alternatives considered:**
- *Vendor SDKs per platform*: better ergonomics per platform, unacceptable image
  growth for an edge device.
- *Webhook only*: minimal, but pushes payload shaping onto every operator.
- *Prometheus scrape endpoint*: pull-based, so it fails exactly when the node is
  unreachable — the moment you most want the data. Deferred as a possible
  addition, not a substitute.

### 7. Redaction is per sink and defaults to the least revealing level

**Decision:** Three levels, chosen per sink:

| Level | Transmits |
|---|---|
| `metadata` (default) | Problem key, service identity, severity, counts, timestamps |
| `analysis` | The above plus summary, root cause, and remediation steps |
| `full` | The above plus the log context window |

**Rationale:** The privacy guarantee is what workload owners rely on when opting
in. Defaulting to `full` would silently export log content the moment an
operator configured any sink. `metadata` is enough to drive a fleet dashboard;
raising the level is a deliberate act.

Credentials come from environment variables or a file, never from the service
definition, and are redacted from all logging.

### 8. Delivery state on the proposal, not a separate ledger

**Decision:** Each proposal gains an `exports` block recording per-sink delivery
status, timestamp, and any external reference.

**Rationale:** An operator reading a local proposal can see whether it escaped
the node and follow the link to the ticket. It also makes the spool
reconstructable: state lives with the record it describes.

## Risks / Trade-offs

**[Risk] Export becomes an exfiltration path for sensitive logs**
→ Redaction defaults to `metadata`; the level is documented per sink; the
proposal records what was sent. Workload owners can see what leaves.

**[Risk] Duplicate or runaway tickets**
→ Correlation by `problem_key`, a persisted mapping, comment rate limiting, and
a severity threshold for issue creation. A dry-run mode logs intended actions
without performing them.

**[Risk] Spool fills the disk during a long outage**
→ Hard size and age caps with oldest-first shedding, counted and logged, and
surfaced on the health endpoint.

**[Risk] A slow or hanging sink degrades monitoring**
→ Export runs on its own thread with per-request timeouts; the queue is bounded;
nothing in the monitoring path waits on it.

**[Risk] Credential leakage through logs or proposals**
→ Credentials only from environment or file, never echoed, never written to a
proposal, and scrubbed from error messages before logging.

**[Trade-off] At-least-once delivery**
→ A retry after an ambiguous failure can duplicate an event. Idempotency keys
let well-behaved sinks dedupe; the alternative — dropping on ambiguity — loses
real data, which is worse for this use case.

**[Trade-off] In-tree sinks rather than a plugin API**
→ Less extensible, but avoids designing a plugin contract before the shape of
demand is known. Sinks are small and self-contained enough to add later.

## Migration Plan

1. Ship with export disabled. No behaviour changes for existing deployments.
2. Enable one event sink on a test node with `metadata` redaction; confirm the
   fleet view populates and the spool stays empty under normal connectivity.
3. Exercise offline behaviour deliberately — disconnect the node, confirm the
   spool grows and drains on reconnection without duplicates.
4. Enable an issue sink in dry-run; inspect the intended issue actions in the
   log before allowing real writes.
5. Raise the redaction level only if the receiving platform genuinely needs more
   than metadata, and record that decision where workload owners can see it.

**Rollback:** unset the sink configuration and restart. The spool can be deleted;
nothing reads it back. Local proposals are unaffected.

## Open Questions

1. **Resolve window.** How long without recurrence before an issue is closed?
   Too short reopens tickets on flapping problems; too long leaves stale ones.
2. **Fleet identity.** Proposals identify a workload but not a node. Should
   exports carry a node identifier by default, given it is arguably sensitive?
3. **Backfill on first connection.** When a node has been offline since before
   export was configured, should existing local proposals be exported, or only
   new ones?
4. **Per-workload export opt-out.** Should a workload be able to consent to local
   monitoring but refuse export, via a `MONITORING_EXPORT=false` variable?
5. **Severity threshold defaults.** Which severities warrant an issue rather than
   just an event? `critical` and `high` is the obvious starting point.
