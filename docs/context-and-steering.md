# Steering the model with knowledge about your services

The model knows nothing about your deployment. Given only log lines it produces
generically correct, specifically useless advice — and sometimes advice that is
actively wrong for your architecture.

You fix that by giving it documentation: SKILL.md files, runbooks, architecture
notes, wiki exports. The monitor injects them into the analysis prompt.

---

## What difference it makes

A real measurement. The error is
`ERROR could not connect to database at db:5432: connection refused`, analysed
twice by `llama3.2:3b-instruct-q4_K_M` on the same node — once with no guidance,
once with a 733-byte `SKILL.md`.

**Without context:**

> **root_cause:** Database connection attempt timed out due to incorrect host or
> port configuration
>
> - Verify that the database server is running and accessible at db:5432
> - Check the sensor service's configuration for any typos or misconfigurations
> - Update the sensor service to use a retry mechanism with exponential backoff

Reasonable, generic, and **wrong for this deployment** — the runbook explicitly
says not to touch the database.

**With this `SKILL.md`:**

```markdown
# sensor service

## Architecture
The `db:5432` hostname resolves to the `timescale-proxy` sidecar, NOT a real
database. The proxy holds a connection pool to the upstream TimescaleDB cluster
and refuses connections when its pool is exhausted.

## Known failure: connection refused on db:5432
This nearly always means the timescale-proxy sidecar has exhausted its pool,
not that the database is down. Do NOT restart the database.

Recovery:
1. Check pool usage: `curl -s localhost:9090/pool | jq .in_use`
2. If in_use == max_size, restart only the proxy: `hzn service restart timescale-proxy`
3. Raise `PROXY_POOL_SIZE` in the service definition if it recurs.
```

> **root_cause:** timescale-proxy sidecar exhausted its connection pool
>
> - Check pool usage: `curl -s localhost:9090/pool | jq .in_use`
> - If in_use == max_size, restart only the proxy: `hzn service restart timescale-proxy`

It named the real component and returned the exact recovery commands. Analysis
was also *faster* (33 s vs 75 s) — a model that knows the answer stops
speculating.

---

## Two places knowledge comes from

| Source | Applies to | Owned by | How |
|---|---|---|---|
| **Node-wide context** | every analysis on the node | node operator | files in `MONITOR_CONTEXT_DIR` |
| **Per-workload context** | one workload's analyses | service developer | `MONITORING_CONTEXT_PATHS` |

Both are read from disk at analysis time, so editing a runbook takes effect on
the next error — no restart, no republish.

---

## Node-wide context (operators)

Drop `.md`, `.markdown`, or `.txt` files into the context directory:

```shell
sudo mkdir -p /etc/edge-ai-monitor/context
sudo tee /etc/edge-ai-monitor/context/10-node.md <<'EOF'
# This node

Rack 4, factory floor B. Intermittent WiFi — network timeouts are usually
transient and self-heal within 2 minutes. Do not recommend network hardware
changes for a single timeout.

Shared services: postgres-primary (10.4.0.12), mqtt-broker (10.4.0.31).
EOF
```

Files load in **filename order**, so a numeric prefix controls precedence when
the budget is tight. Subdirectories are ignored — the directory is flat.

Point it elsewhere with `MONITOR_CONTEXT_DIR`, and mount it into the container:

```json
"binds": ["/etc/edge-ai-monitor/context:/etc/edge-ai-monitor/context:ro"]
```

Confirm what loaded:

```shell
curl -sS http://127.0.0.1:8080/health | jq '{context_dir, node_context_documents, node_context_bytes}'
```

```json
{
  "context_dir": "/etc/edge-ai-monitor/context",
  "node_context_documents": 2,
  "node_context_bytes": 1841
}
```

`node_context_documents: 0` with files present means the directory is not
visible inside the container — check the bind.

---

## Per-workload context (service developers)

Ship documentation alongside your logs and name it in the deployment:

```json
"deployment": {
  "services": {
    "sensor": {
      "image": "examples/sensor:1.2.0",
      "privileged": true,
      "binds": ["/var/log/workloads/sensor:/var/log/app:rw"],
      "environment": [
        "MONITORING_ENABLED=true",
        "MONITORING_LOG_PATHS=/var/log/workloads/sensor/app.log",
        "MONITORING_CONTEXT_PATHS=/var/log/workloads/sensor/SKILL.md,/var/log/workloads/sensor/RUNBOOK.md"
      ]
    }
  }
}
```

The paths are resolved **inside the monitor's container**, exactly like
`MONITORING_LOG_PATHS` — so the simplest approach is to write the files into the
same shared mount your logs already use. Have your image copy them there at
startup:

```sh
cp /app/docs/SKILL.md /var/log/app/SKILL.md
```

Same list syntax as the other variables: comma-separated, or a JSON array when a
path contains a comma. An unreadable path is skipped with a warning; the rest
still load.

Per-workload documents are placed **before** node-wide ones in the prompt, and
survive first when the budget is tight — they are the most specific knowledge
available.

---

## Writing context that actually helps

The model has a small context window and reads your text once, alongside 100
lines of log. Density beats completeness.

**Write the things logs cannot tell it:**

- What a hostname or port actually resolves to
- Which failures are transient and self-heal
- Which component to restart — and which to leave alone
- Exact diagnostic commands, with the flags
- Config file paths and the settings that matter

**Leave out:**

- Anything inferable from the log line itself
- Marketing descriptions, changelogs, licence text
- Full API references — the model needs failure modes, not endpoints
- Anything secret. **Context goes into the prompt.** Never put credentials,
  tokens, or private keys in a context file

### A shape that works

```markdown
# <service name>

One sentence on what it does.

## Architecture
Only the parts that matter when it breaks: real names behind hostnames,
which components are sidecars, what depends on what.

## Known failures

### <symptom as it appears in the log>
What it actually means. What NOT to do.
Recovery:
1. <exact command>
2. <exact command>

## Configuration that matters
<file path> — <setting>: what it controls, sane values.
```

Negative instructions carry real weight. "Do NOT restart the database" is what
flipped the measured example above from wrong to right.

### Using existing documentation

Wiki pages and internal docs work directly — export to Markdown and drop them
in. Two cautions:

- **Trim first.** A 40 KB wiki page gets truncated at
  `MONITOR_CONTEXT_MAX_FILE_BYTES` (4000 by default), so you would ship whatever
  happens to be in the first 4 KB. Extract the failure modes instead.
- **Check for secrets** before copying anything into a context file.

Keeping context in the same repository as the service, and copying it into the
image at build time, keeps it current — documentation that drifts is worse than
none, because the model will confidently follow stale instructions.

---

## Budgets

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_CONTEXT_MAX_FILE_BYTES` | `4000` | Per-file cap; longer files are truncated with a notice |
| `MONITOR_CONTEXT_MAX_BYTES` | `8000` | Total per prompt; documents are added until it is reached |
| `MONITOR_CONTEXT_ENABLED` | `true` | Master switch |

Every byte of guidance competes with the log lines describing the actual error,
and a 3B model's window is small. If you raise these, watch analysis quality and
duration — more context is not automatically better, and a bloated prompt can
push the error itself out of the model's attention.

Order of assembly, and therefore of survival under budget pressure:

1. Per-workload documents, in the order listed in `MONITORING_CONTEXT_PATHS`
2. Node-wide documents, in filename order

When the budget is reached the remaining documents are skipped and logged:

```
INFO context budget (8000 bytes) reached; omitting 90-appendix.md and any further documents
```

Turn the whole feature off with `MONITOR_CONTEXT_ENABLED=false` — useful for
A/B testing whether your context is actually helping.

---

## Verifying it works

**1. Confirm the documents load.**

```shell
curl -sS http://127.0.0.1:8080/health | jq '{node_context_documents, node_context_bytes}'
```

**2. Compare with and without.** Trigger the same error twice, once with
`MONITOR_CONTEXT_ENABLED=false`, and read both proposals. If the guided one is
not visibly more specific, your context is not earning its budget.

**3. Watch for the model ignoring you.** Small models sometimes disregard
guidance buried in long documents. If that happens, shorten the file and move
the critical instruction to the top — position matters more than emphasis.

---

## Security

Context files are read by the monitor and sent to the model. Everything in them
appears in the prompt.

- **Never include credentials, tokens, or private keys.**
- Inference is local by default, so context does not leave the node — **unless**
  you set `MONITOR_LLM_HOST` to an external runtime, in which case it does.
- Node-wide context reaches every analysis on the node, including workloads
  owned by other teams. Put team-specific knowledge in per-workload files.
- The monitor reads context files read-only and never writes to them.

---

## Related

| Document | Contents |
|---|---|
| [tuning.md](tuning.md) | Model selection, timeouts, queue and memory settings |
| [service-developer-guide.md](service-developer-guide.md) | Getting a workload monitored |
| [monitoring-variables.md](../horizon/monitoring-variables.md) | Full `MONITORING_*` reference |
