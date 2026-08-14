## Why

Proposals and detected errors currently exist only as JSON files on the node
that produced them. An operator running more than a handful of edge nodes has no
way to see what is failing across the fleet without connecting to each node
individually — which does not scale, and means problems go unnoticed until
someone goes looking. Meanwhile the organisations deploying these nodes already
run observability platforms and issue trackers that are the established place
for this information to land.

## What Changes

- **New optional export pipeline** that forwards detected errors and generated
  proposals to external systems. Disabled by default; the service's existing
  local-only behaviour is unchanged when no sink is configured.
- **Observability sinks** for streaming telemetry: OTLP (OpenTelemetry), webhook
  POST, syslog, and MQTT. These cover single-pane-of-glass control centres
  (Grafana, Datadog, Splunk, Instana) either natively or through their standard
  collectors.
- **Issue-tracker sinks** that open and maintain work items: GitHub Issues, Jira,
  and ServiceNow. One problem produces one issue, updated as the problem recurs
  and closed when it stops — never a new ticket per occurrence.
- **Store-and-forward delivery** with an on-disk queue, bounded retention, and
  exponential backoff, because edge nodes lose connectivity routinely. This
  node's own event log shows repeated `network is unreachable` heartbeat
  failures; export must survive them without losing records or growing without
  bound.
- **Per-sink redaction policy** controlling what leaves the node — proposal
  metadata only, proposal plus analysis, or full log context. Defaults to the
  least revealing option that is still useful.
- **Delivery state recorded on the proposal**, so an operator reading a local
  proposal can see where it was sent and follow the link to the resulting issue.

No breaking changes: with export unconfigured, behaviour is identical to today.

## Capabilities

### New Capabilities
- `northbound-export`: Forwarding errors and proposals to external systems —
  sink configuration, payload shaping, redaction, store-and-forward delivery,
  retry and backoff, and bounded queue behaviour under sustained outage.
- `issue-tracking`: Maintaining work items in external trackers — creating one
  issue per distinct problem, updating it as the problem recurs, resolving it
  when the problem stops, and reconciling state after the node has been offline.

### Modified Capabilities
- `remediation-proposals`: proposals gain export state — which sinks a proposal
  was delivered to, when, and any external reference (such as an issue URL)
  returned by the sink.

## Impact

**Affected systems:**
- New outbound network egress from the monitor. Until now the service made no
  outbound connections beyond the local anax and Ollama APIs; nodes behind
  restrictive firewalls or proxies will need configuration.
- Local storage: an export queue shares the node's disk with the proposal store.
- The `remediation-proposals` capability, which gains delivery metadata.

**Dependencies:**
- Sink-specific clients. Prefer plain HTTP via the existing `requests` dependency
  over heavyweight SDKs, to keep the image small enough for edge deployment.
- Credentials for authenticated sinks, which must be supplied without being
  written into service definitions or logged.

**Design tension this change introduces:**

The service's stated guarantee is that log content never leaves the node.
Northbound export exists to break that guarantee deliberately and selectively.
That makes explicit opt-in, per-sink redaction, and clear documentation of what
each configuration actually transmits load-bearing requirements rather than
niceties — a workload's operator may have consented to local monitoring without
consenting to their logs reaching a third-party SaaS.

**Breaking changes:**
None. Export is opt-in and inert when unconfigured.
