# Operations guide

**Audience:** you run edge nodes in production and consume what the monitor
produces. This covers the daily loop — find errors, act on them, undo what did
not work — plus tuning the model's context and keeping the whole thing fast.

For getting the monitor deployed in the first place, see
[deployment.md](deployment.md). For diagnosing a monitor that is itself broken,
see [troubleshooting.md](troubleshooting.md).

Every command here has been run against a live node.

---

## 1. Finding reported errors

### Where proposals live

One JSON file **per distinct problem** (not per occurrence), inside the
`edge-ai-monitor-state` volume:

```
/var/lib/monitor/proposals/<org>_<service>_<version>_<arch>/<problem-key>.json
```

There is no query API and no CLI — proposals are files, and you read them with
the shell. The recipes below assume `jq` on the host; the helper container only
needs `busybox`.

### The one command to start with

Triage table, newest first:

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox \
  sh -c 'cat /state/proposals/*/*.json' \
| jq -rs 'sort_by(.metadata.timestamp) | reverse | .[:20][]
  | [.metadata.timestamp[0:19], .severity, (.confidence|tostring),
     (.metadata.occurrences|tostring), .metadata.service_name,
     .error_summary[0:50]] | @tsv' \
| column -t -s$'\t'
```

```
2026-08-14T14:12:55  high  0.9  5  sample-monitored-workload  Repeated connection-refused errors when atte
2026-08-14T14:11:55  high  0.9  5  sample-monitored-workload  Repeated connection-refused errors when atte
```

Save it as a shell function — you will run it constantly:

```shell
proposals() {
  docker run --rm -v edge-ai-monitor-state:/state busybox \
    sh -c 'cat /state/proposals/*/*.json' 2>/dev/null | jq -s "$@"
}
```

### Recipes

**What needs a human right now** — critical or low-confidence, deduplicated:

```shell
proposals -r '[.[] | select(.requires_human_review)]
  | group_by(.error_summary) | map(max_by(.metadata.timestamp)) 
  | sort_by(.severity) | .[]
  | "\(.severity)\t\(.metadata.service_name)\t\(.error_summary)"' | column -t -s$'\t'
```

**Distinct problems, not distinct files** — the number that actually matters:

```shell
proposals '{files: length,
            distinct_problems: ([.[].root_cause] | unique | length),
            by_workload: (group_by(.metadata.workload_id)
                          | map({(.[0].metadata.workload_id): length}) | add)}'
```

```json
{
  "files": 137,
  "distinct_problems": 14,
  "by_workload": { "myorg/sample-monitored-workload_1.0.0_amd64": 137 }
}
```

Group by `metadata.workload_id`, not `service_name` — the workload id is stable
across versions and monitor upgrades.

**Everything for one service, most recent first:**

```shell
proposals -r '[.[] | select(.metadata.service_name == "sensor")]
  | sort_by(.metadata.timestamp) | reverse | .[0]'
```

**What changed in the last hour:**

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox \
  sh -c 'find /state/proposals -name "*.json" -mmin -60 -exec cat {} \;' \
| jq -rs 'sort_by(.metadata.timestamp) | .[] | "\(.metadata.timestamp[0:19]) \(.severity) \(.error_summary)"'
```

**Full detail for one proposal:**

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox \
  sh -c 'ls -t /state/proposals/*/*.json | head -1 | xargs cat' | jq
```

### Reading the fields that matter

| Field | Use it for |
|---|---|
| `severity` | `critical` / `high` / `medium` / `low` |
| `confidence` | Below `0.5` the proposal carries diagnostic rather than corrective steps |
| `requires_human_review` | `false` only for low/medium severity at ≥ 0.8 confidence |
| `metadata.total_occurrences` | How often this problem has fired in total — blast radius |
| `metadata.first_seen` / `last_seen` | How long it has been happening, and whether it still is |
| `metadata.analysis_count` | How many times it was actually re-analysed; a high count with an unchanged root cause means the explanation is stable |
| `metadata.line_number` | Where to look in the log yourself (most recent occurrence) |
| `metadata.analysis_duration_ms` | Slow analyses signal an overloaded node |

**Is it still happening?** Compare `last_seen` to now:

```shell
proposals -r 'sort_by(.metadata.last_seen) | reverse | .[]
  | "\(.metadata.last_seen[0:19])  x\(.metadata.total_occurrences)  \(.severity)  \(.error_summary[0:46])"'
```

A `last_seen` that stopped advancing after your fix is the signal that it worked.

> **One file per problem.** A recurring error updates its existing proposal
> rather than writing a new one, so the file count *is* the problem count.
> `total_occurrences` tells you how often it has fired, `first_seen`/`last_seen`
> over what span, and `analysis_count` how many times it was actually
> re-analysed. Proposals written before this behaviour existed are one-per-event
> and can be cleared out; see [§5 Storage growth](#storage-growth).

### Getting them off the node

```shell
# Copy out for offline analysis
docker run --rm -v edge-ai-monitor-state:/state -v "$PWD:/out" busybox \
  sh -c 'cp -r /state/proposals /out/'

# Or ship the digest somewhere central
proposals -c '[.[] | {t: .metadata.timestamp, sev: .severity,
                      svc: .metadata.service_name, cause: .root_cause}]' \
  | curl -sS -X POST -H 'Content-Type: application/json' -d @- https://your-collector/ingest
```

The monitor deliberately has no outbound integrations — shipping data off the
node is your decision, not its default.

---

## 2. Remediating an issue

Proposals are **hypotheses from a small model working off ~100 lines of log**.
The workflow below exists because acting on them directly is not safe.

### The loop

**1. Confirm the error is real.** Read the log yourself at the cited line:

```shell
sed -n "$(( LINE - 20 )),$(( LINE + 20 ))p" /var/log/workloads/<service>/app.log
```

**2. Judge the proposal.** `confidence ≥ 0.8` with a root cause that matches
what you see is worth acting on. Low confidence, or steps that contradict the
log, means the model was guessing — treat the proposal as a prompt to
investigate, not a plan.

**3. Check it is not already known.** If the same `root_cause` has appeared for
weeks, the fix is a code or config change, not a restart.

**4. Record what you are about to change.** This is what makes rollback
possible; see §3.

**5. Apply the narrowest change that could work.**

| Proposal points at | Typical action |
|---|---|
| Configuration | Change `userInput` in the deployment policy, republish, re-negotiate |
| A dependency being down | Fix the dependency; the workload usually recovers on its own |
| Resource exhaustion | Raise `max_memory_mb` / `max_cpus` in the service definition |
| A code defect | File it; do not patch in production |
| Something environmental | Fix the node, not the service |

**6. Verify recovery.** Every proposal's final step is a verification step:

```shell
tail -f /var/log/workloads/<service>/app.log
```

Then confirm the monitor agrees — the error should stop generating new
proposals:

```shell
watch -n30 'docker run --rm -v edge-ai-monitor-state:/state busybox \
  sh -c "find /state/proposals -name \"*.json\" -mmin -5 | wc -l"'
```

### Applying a configuration fix

```shell
# 1. Record current state (see §3)
hzn policy list > /var/backups/node-policy-$(date +%F-%H%M).json

# 2. Edit userInput in the deployment policy, then republish
hzn exchange deployment addpolicy -f deployment.policy.json myorg/policy-<service>

# 3. Re-negotiate — a policy edit alone does NOT restart a running service
hzn agreement list
hzn agreement cancel <agreement-id>

# 4. Confirm the new value took effect
hzn agreement list | jq -r '.[].workload_to_run | "\(.url) \(.version)"'
```

> ⚠️ **Republishing the same version does not redeploy.** The agent keeps the
> existing agreement. Either bump the version or cancel the agreement.

### Never do these

- **Do not run remediation steps as a script.** They are natural-language
  suggestions from a model, not validated commands.
- **Do not act on `requires_human_review: true` without reading the log.** That
  flag exists precisely because the model was unsure or the stakes were high.
- **Do not fix the monitor's complaint instead of the problem.** If proposals
  are noise, narrow the workload's `MONITORING_ERROR_PATTERNS`.

---

## 3. Rolling back a fix that did not work

The monitor never changes anything, so everything here rolls back *your* action.
Rollback is only easy if you recorded state first.

### Before you change anything

```shell
BACKUP=/var/backups/edge-ai/$(date +%F-%H%M%S)
mkdir -p "$BACKUP"

hzn policy list                > "$BACKUP/node-policy.json"
hzn agreement list             > "$BACKUP/agreements.json"
hzn exchange deployment listpolicy myorg/policy-<service> > "$BACKUP/deployment-policy.json"
hzn exchange service list -l myorg/<service>_<version>_amd64 > "$BACKUP/service.json"
```

Those four files are enough to reconstruct any of the rollbacks below.

### Rolling back a service version

Every published version stays in the exchange, so rollback is a policy edit:

```shell
# What is deployed now
hzn agreement list | jq -r '.[].workload_to_run | "\(.url) \(.version)"'

# What you could go back to
hzn exchange service list | grep '<service>'
```

Point the deployment policy at the older version and re-negotiate:

```json
"serviceVersions": [{ "version": "0.0.5" }]
```

```shell
hzn exchange deployment addpolicy -f deployment.policy.json myorg/policy-<service>
hzn agreement cancel <agreement-id>
```

Confirm the rollback actually landed — check the **image digest**, not the
version string:

```shell
hzn exchange service list -l myorg/<service>_0.0.5_amd64 \
  | jq -r '.[].deployment' | jq -r '.services[].image'
```

> ⚠️ **Version numbers can lie.** The service version and the image tag are
> independent. A service version republished against a stale image tag deploys
> old code under a new number. The digest is the only reliable answer to "what
> is actually running".

### Rolling back a node policy change

```shell
hzn policy update -f "$BACKUP/node-policy.json"
```

> ⚠️ **`hzn policy update` replaces the entire policy — it does not merge.** A
> policy file missing a property that other deployment policies match on will
> cancel those agreements. We have seen an unrelated workload torn down by a
> node policy edit that omitted one property. Always diff before applying:
>
> ```shell
> diff <(jq -S . "$BACKUP/node-policy.json") <(hzn policy list | jq -S .)
> ```

### Rolling back a configuration change

`userInput` values live in the deployment policy, so restore the backed-up
policy and re-negotiate:

```shell
hzn exchange deployment addpolicy -f "$BACKUP/deployment-policy.json" myorg/policy-<service>
hzn agreement cancel <agreement-id>
```

### Removing a workload entirely

```shell
hzn exchange deployment removepolicy myorg/policy-<service>   # stops scheduling
hzn exchange service remove -f myorg/<service>_<version>_amd64
```

### Confirming a rollback worked

```shell
hzn eventlog list | tail -20        # the audit trail
hzn agreement list                  # what re-formed
curl -sS http://127.0.0.1:8080/health | jq   # monitor's own view
```

Watch for the error you were fixing to stop producing new proposals. If it
returns, the rollback restored the previous *behaviour* as intended — the
original problem is still there and needs a different fix.

---

## 4. Tuning context prompts

Context is the highest-leverage control you have over output quality. Full
mechanics are in [context-and-steering.md](context-and-steering.md); this is the
operational tuning loop.

### When to reach for it

| Symptom in proposals | Fix |
|---|---|
| Generic advice ("check the service is running") | Add architecture facts — what hostnames resolve to, what depends on what |
| Names the wrong component | Add an explicit correction, including what *not* to touch |
| Right diagnosis, useless steps | Add the real recovery commands with flags |
| Treats normal conditions as outages | Document what is expected and self-healing |
| Severity consistently too high | Say which conditions are routine |

### The loop

**1. Find your worst recurring proposal.**

```shell
proposals -r 'group_by(.root_cause)
  | map({cause: .[0].root_cause, n: length})
  | sort_by(-.n) | .[:5][] | "\(.n)\t\(.cause)"' | column -t -s$'\t'
```

**2. Write the correction** into the node context directory:

```shell
sudo tee /etc/edge-ai-monitor/context/20-database.md <<'EOF'
# Database connectivity

`db:5432` is the timescale-proxy sidecar, NOT a database. Connection refused
means the proxy pool is exhausted. Do NOT restart the database.

Recovery:
1. `curl -s localhost:9090/pool | jq .in_use`
2. If in_use == max_size: `hzn service restart timescale-proxy`
EOF
```

**3. Confirm it loaded.** No restart is needed — files are re-read per analysis:

```shell
curl -sS http://127.0.0.1:8080/health | jq '{node_context_documents, node_context_bytes}'
```

**4. Wait for the next occurrence and compare.** Cached analyses are reused for
15 minutes, so force a fresh one if you are impatient:

```shell
MONITOR_CACHE_TTL=0    # testing only — every occurrence re-runs inference
```

**5. Iterate.** If the model still ignores the guidance, shorten the file and
move the critical instruction to the top. Position beats emphasis in a small
model.

### What works

Measured on a live node, same error and model, with and without a 733-byte
`SKILL.md`:

| | Root cause |
|---|---|
| Without | "Database connection attempt timed out due to incorrect host or port configuration" |
| With | "timescale-proxy sidecar exhausted its connection pool" |

The unguided run recommended restarting the database — which the runbook
explicitly forbids. The guided run returned the runbook's own commands, and ran
faster (33 s vs 75 s), because a model that knows the answer stops speculating.

### Rules of thumb

- **Negative instructions carry weight.** "Do NOT restart the database" is what
  flipped the example above.
- **Exact commands get reproduced verbatim.** Vague advice gets paraphrased into
  mush.
- **Keep it under ~2 KB per file.** The budget is 4 KB per file and 8 KB per
  prompt; beyond that you crowd out the log evidence.
- **Node-wide context reaches every workload on the node**, including other
  teams'. Team-specific knowledge belongs in per-workload
  `MONITORING_CONTEXT_PATHS`.
- **Never put credentials in context files.** Everything in them goes into the
  prompt.
- **Stale context is worse than none** — the model follows it confidently.
  Review it whenever the architecture changes.

---

## 5. Production performance recommendations

Ordered by impact.

### Right-size the model and timeout

The single biggest factor. Measured on an 8-core x86_64 node, CPU-only:

| Model | Time per analysis |
|---|---|
| `llama3.2:3b-instruct-q4_K_M` | ~55 s |
| `qwen2.5-coder:7b` | ~136 s |

**Set `MONITOR_LLM_TIMEOUT` to roughly twice your measured time** — 120 s for
the 3B default. The 30 s default assumes GPU acceleration and will time out
every analysis on a CPU-only node.

A 3B model at ~55 s means the node sustains **about one analysis per minute**.
That number is your capacity budget; everything below is about staying under it.

### Narrow error patterns — the cheapest win

Every pattern match costs an inference. Broad patterns are the usual cause of a
saturated queue.

```shell
# How much work is arriving
curl -sS http://127.0.0.1:8080/health | jq '{analysis_queue_depth, analyses_dropped}'
```

Sustained `analyses_dropped` means arrival exceeds capacity. Fixing it means
narrowing `MONITORING_ERROR_PATTERNS` on the noisy workload — a service
developer change, not an operator one. Raising `MONITOR_MAX_QUEUE_SIZE` only
delays shedding; it adds no throughput.

### Storage growth

Proposals are keyed by problem, so storage grows with the number of *distinct*
problems, not with time or error frequency. A workload stuck in a crash loop for
a month occupies one file, a few kilobytes.

That makes unbounded growth unlikely, but two cases still accumulate:

- **Problems that were fixed** — the file remains after the error stops.
- **Workloads that were removed** — their directory is never cleaned up.

Retention is still worth configuring, just far less urgently:

```shell
# /etc/cron.daily/edge-ai-proposals
#!/bin/sh
# Remove proposals for problems that have not recurred in 30 days.
docker run --rm -v edge-ai-monitor-state:/state busybox \
  find /state/proposals -name '*.json' -mtime +30 -delete
```

Because a live problem's file is rewritten on every occurrence, its mtime tracks
`last_seen` — so age-based deletion removes exactly the resolved ones.

> **Upgrading from an older monitor?** Versions before proposal deduplication
> wrote one file per occurrence and can leave thousands behind. They are safe to
> delete wholesale; nothing reads proposals back.

Check current usage:

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox du -sh /state/proposals
```

Nothing reads proposals back, so deleting old ones is always safe.

### Cache and poll intervals

| Setting | Default | Production guidance |
|---|---|---|
| `MONITOR_CACHE_TTL` | `900` | Raise to `1800`–`3600` for stable workloads; each cache hit is an inference avoided |
| `MONITOR_POLL_INTERVAL` | `60` | Fine as-is; lower only if workloads churn constantly |
| `MONITOR_LOG_POLL_INTERVAL` | `1` | Raise to `2`–`5` on I/O-constrained nodes |
| `MONITOR_MAX_BUFFER_MB` | `100` | Lower to `50` on memory-constrained nodes |

### Keep the monitor out of the way

The service definition caps it at 4 GB and 2 CPUs. On a node running real
workloads, verify it is honouring that:

```shell
docker stats --no-stream | grep edge-ai
```

If the monitor is competing with production workloads, lower `max_cpus` in the
service definition rather than accepting the contention — a slower analysis is
almost always preferable to a slower workload.

### Pre-pull models

The first analysis on a fresh node blocks on a ~2 GB download. Bake it into node
provisioning:

```shell
docker exec <monitor-container> ollama pull llama3.2:3b-instruct-q4_K_M
```

On air-gapped nodes, pre-seed the `edge-ai-monitor-models` volume and set
`MONITOR_LLM_AUTO_PULL=false`.

### Invest in context before bigger models

A 3B model with a good runbook outperforms a 7B model without one, at a quarter
of the latency. If output quality is the problem, write context first and only
then consider a larger model.

### What to watch

```shell
curl -sS http://127.0.0.1:8080/health | jq '{
  analysis_queue_depth, analyses_dropped, analyses_failed,
  proposals_written, proposals_updated,
  proposal_storage_failures, workloads_monitored, node_context_documents}'
```

| Signal | Means |
|---|---|
| `analyses_failed` climbing | Timeout too low, or the runtime is down |
| `analyses_dropped` non-zero | Arrival exceeds capacity — narrow patterns |
| `analysis_queue_depth` pinned at max | Sustained overload |
| `proposals_written` climbing steadily | New *distinct* problems appearing — worth investigating |
| `proposals_updated` climbing, `written` flat | Known problems recurring; no new failure modes |
| `proposal_storage_failures` non-zero | Disk full or unwritable — check retention |
| `workloads_monitored` dropping to 0 | Workloads stopped, or opt-in was lost on republish |
| `node_context_documents` at 0 | Context directory not mounted |

---

## Related

| Document | Contents |
|---|---|
| [troubleshooting.md](troubleshooting.md) | The monitor itself is broken |
| [tuning.md](tuning.md) | Every configuration variable |
| [context-and-steering.md](context-and-steering.md) | Context mechanics in depth |
| [deployment.md](deployment.md) | Getting the monitor onto a node |
| [service-developer-guide.md](service-developer-guide.md) | For the workload owners you support |
