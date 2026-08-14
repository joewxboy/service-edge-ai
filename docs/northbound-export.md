# Northbound export

Forward detected errors and remediation proposals to observability platforms,
single-pane-of-glass control centres, and issue trackers.

**Disabled by default.** With no sinks configured the service makes no outbound
connections and behaves exactly as it did before.

---

## Why you would turn this on

Proposals are JSON files on the node that produced them. That works for one
node. Past a handful, nobody is going to connect to each one to find out what is
failing — so problems go unnoticed. Export puts them where your team already
looks.

Two kinds of destination, because they behave differently:

| | Event sinks | Issue sinks |
|---|---|---|
| Examples | OTLP, webhook, syslog, MQTT | GitHub, Jira, ServiceNow |
| Sends | every occurrence | one work item per problem |
| Lifecycle | none — fire and forget | created, updated, resolved, reopened |
| Use it for | dashboards, alerting, trend analysis | work that someone must action |

---

## ⚠️ Read this before enabling

The service's standing guarantee is that **log content never leaves the node**,
and workload owners opt into monitoring on that basis. Export exists to break
that guarantee — deliberately, selectively, and visibly.

Three controls make that safe:

1. **Redaction level, per sink.** Defaults to `metadata`, which carries no
   log-derived content at all.
2. **Workload consent.** Levels above `metadata` only apply to workloads that
   set `MONITORING_EXPORT=true` in their own deployment.
3. **Recorded delivery.** Each proposal records where it was sent, so anyone
   reading the local file can see what left the node.

If you raise a sink to `analysis` or `full`, tell the owners of the workloads
involved. They consented to local monitoring, not to a third-party SaaS.

---

## Redaction levels

| Level | Transmits | Log-derived? |
|---|---|---|
| `metadata` *(default)* | Problem key, service identity, node, severity, confidence, occurrence counts, timestamps, matched pattern | **No** |
| `analysis` | The above plus error summary, root cause, remediation steps | Yes |
| `full` | The above plus log path, line number, and the log context window | Yes |

`metadata` is enough to answer "which services are failing, how often, how
badly" — which is what a fleet dashboard needs. Start there.

```yaml
export:
  sinks:
    fleet-dashboard:
      type: otlp
      endpoint: https://collector.example.com
      level: metadata          # the default; stated here for clarity
```

### How consent interacts with levels

```
sink level = analysis
   workload sets MONITORING_EXPORT=true   ->  analysis is sent
   workload does not                      ->  clamped to metadata
```

Nothing fails and nothing is dropped — the record is simply less revealing. A
workload can therefore appear in fleet counts without its log content ever
leaving the node.

---

## Configuring sinks

Sinks live under `export.sinks`, keyed by a name you choose. That name appears in
health output and in each proposal's delivery record.

**Sinks are configured through a mounted config file, not environment
variables.** A sink is a nested structure and `MONITOR_*` variables are flat, so
the service definition mounts the host's config:

```json
"binds": ["/etc/edge-ai-monitor/config.yaml:/etc/edge-ai-monitor/config.yaml:ro"]
```

Edit `/etc/edge-ai-monitor/config.yaml` on the node and restart the service —
no rebuild, no republish. The scalar settings (`MONITOR_EXPORT_ENABLED`,
`MONITOR_EXPORT_NODE_ID`, spool sizing) remain available as environment
overrides for per-node tuning through `userInput`.

### Credentials

**Never put a secret in a service definition or deployment policy** — both are
stored in the exchange and readable by anyone who can query it. Name where the
secret lives instead:

```yaml
    ops-alerts:
      type: webhook
      url: https://hooks.example.com/edge
      token_env: MONITOR_EXPORT_WEBHOOK_TOKEN     # read from the environment
    oncall:
      type: github
      repository: myorg/edge-incidents
      token_file: /run/secrets/github-token       # or from a file
```

Supported for `token`, `password`, `api_key`, and `secret`. An inline value
works but logs a warning. A sink whose credential does not resolve disables
itself at startup and says which variable was missing; the others keep working.

Secret values are scrubbed from sink error messages before they reach the log.

### Event sinks

**OTLP** — reaches most platforms through their existing collector:

```yaml
    fleet:
      type: otlp
      endpoint: https://collector.example.com
      level: metadata
```

**Webhook** — a JSON POST, with an `Idempotency-Key` header:

```yaml
    hooks:
      type: webhook
      url: https://hooks.example.com/edge
      token_env: WEBHOOK_TOKEN
      headers: { X-Team: platform }
```

**Syslog** — RFC 5424 over UDP or TCP, severity mapped from the proposal:

```yaml
    central-syslog:
      type: syslog
      host: logs.example.com
      port: 514
      protocol: tcp
```

**MQTT** — published to `<topic>/<severity>` so subscribers can filter without
parsing:

```yaml
    edge-bus:
      type: mqtt
      host: mqtt.example.com
      topic: edge-ai/proposals
      password_env: MQTT_PASSWORD
```

> MQTT needs the `paho-mqtt` package. If it is absent the sink reports that and
> disables itself rather than failing silently.

### Issue sinks

```yaml
    oncall:
      type: github
      repository: myorg/edge-incidents
      token_env: GITHUB_TOKEN
      level: analysis
      min_severity: high        # high | critical | medium | low | all
      update_interval: 3600     # seconds between comments on one issue
      resolve_after: 86400      # quiet period before closing
      dry_run: true             # log intended actions, change nothing
```

Jira and ServiceNow take the same lifecycle settings:

```yaml
    jira:
      type: jira
      base_url: https://myorg.atlassian.net
      project: OPS
      email: bot@myorg.com
      token_env: JIRA_TOKEN
      resolve_transition: Done
      reopen_transition: Reopen

    servicenow:
      type: servicenow
      instance_url: https://myorg.service-now.com
      username: edge-ai-bot
      password_env: SERVICENOW_PASSWORD
      table: incident
```

**Always start with `dry_run: true`.** It logs the issue it would create or
update and touches nothing, which is how you find out that your severity
threshold is wrong before it opens forty tickets.

---

## Issue lifecycle

One problem gets one issue, keyed on the same `problem_key` the proposal store
uses. Occurrences that differ only in numbers — timestamps, counters, PIDs — are
the same problem and do not open a second ticket.

| Event | Action |
|---|---|
| First occurrence at or above `min_severity` | Create the issue |
| Recurrence | Comment, at most once per `update_interval` |
| Re-analysis with a different root cause | Comment immediately, bypassing the rate limit |
| No recurrence for `resolve_after` | Close, with a comment saying so |
| Recurrence after closing | Reopen, or open a linked issue if the tracker cannot reopen |
| Someone closed it manually and it recurs | Reopen |

### What resolution means

The monitor knows the error **stopped appearing**. It does not know it was
fixed, and closing comments say exactly that:

> This problem has not recurred in 24 hours (last seen …), so it is being closed
> automatically. Note that this reflects the absence of further occurrences,
> **not** confirmation that the underlying cause was fixed.

Resolution is judged from the last *occurrence*, not the last comment — an issue
that was commented on recently is still closed if the error itself has stopped.

### Rate limiting matters

An error firing every minute would otherwise produce a comment every minute. The
default is one comment per hour, carrying the accumulated occurrence count.
The count is the signal; the comment frequency is not.

---

## Delivery guarantees

**At-least-once, store and forward.** Every record is written to an on-disk
spool before any delivery is attempted, so nothing is lost to a restart — which
is how outages on edge nodes usually end.

- Failures retry with **exponential backoff and jitter**, so a fleet reconnecting
  after the same outage does not stampede the sink.
- Each record carries a stable **idempotency key**, so a retry after an ambiguous
  failure can be recognised as a duplicate by the receiver.
- The spool is **bounded**: 64 MB and 7 days by default. At the limit the oldest
  records are dropped, counted, and logged.

Exactly-once is not offered, and is not achievable across arbitrary third-party
APIs. Dropping records on ambiguity would lose real data, which is worse.

### Watching it work

```shell
curl -sS http://127.0.0.1:8080/health | jq '{
  export_enabled, export_sinks, export_queue_depth,
  exports_delivered, exports_dropped, exports_expired,
  issues_created, issues_updated, issues_resolved}'
```

| Signal | Means |
|---|---|
| `export_queue_depth` growing | Sink unreachable; records are queuing as designed |
| `exports_dropped` non-zero | Spool hit its ceiling — a long outage, or too much volume |
| `exports_expired` non-zero | Records aged out before connectivity returned |
| `issues_created` climbing steadily | New distinct problems, or a threshold set too low |

Validate configuration without exporting anything:

```shell
edge-ai-monitor --check
```

This reports each sink as reachable or not, alongside the anax and Ollama checks.

---

## Where a proposal went

Each proposal records its own delivery:

```json
"exports": {
  "fleet-dashboard": { "status": "delivered", "at": "2026-08-14T12:00:05+00:00" },
  "oncall": { "status": "delivered", "at": "2026-08-14T12:00:07+00:00", "reference": "1423" }
}
```

`pending` means queued, `failed` means the last attempt failed and it will
retry. For issue sinks, `reference` is the issue number or key — so a local file
leads you straight to the ticket.

---

## Network requirements

The monitor previously made no outbound connections beyond `localhost`. Export
changes that:

- Egress to each sink endpoint, on nodes that may sit behind a restrictive
  firewall or a proxy.
- Standard `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` variables are honoured by
  the underlying HTTP client.
- The spool shares the `edge-ai-monitor-state` volume with proposals; size that
  volume for both.

---

## Turning it off

Remove the sinks, or set `MONITOR_EXPORT_ENABLED=false`, and restart. The spool
can be deleted — nothing reads it back — and local proposals are unaffected.

---

## Related

| Document | Contents |
|---|---|
| [operations.md](operations.md) | Day-to-day operation, including fleet-level workflows |
| [tuning.md](tuning.md) | Every configuration variable |
| [service-developer-guide.md](service-developer-guide.md) | `MONITORING_EXPORT` and workload consent |
| [architecture.md](architecture.md) | Where export sits in the pipeline |
