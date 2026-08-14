# Model selection, tuning, and configuration

Everything the monitor does is controlled by `config/config.yaml`, overridden by
`MONITOR_*` environment variables. This page is the complete reference, ordered
by what you are most likely to change.

For **steering the model with knowledge about your services** — SKILL.md files,
runbooks, wiki exports — see [context-and-steering.md](context-and-steering.md).

---

## How configuration is resolved

Three layers, last one wins:

1. **Built-in defaults** — compiled into `config.py`; a working configuration on
   a standard edge node with no config file at all.
2. **`config.yaml`** — read from `MONITOR_CONFIG`, default
   `/etc/edge-ai-monitor/config.yaml`. A missing file is not an error.
3. **`MONITOR_*` environment variables** — how Open Horizon `userInput` supplies
   per-node settings.

Validate the result without starting the service:

```shell
edge-ai-monitor --check
```

This reports invalid values (`discovery.poll_interval must be greater than 0`)
and unreachable dependencies, then exits non-zero. Unknown keys in `config.yaml`
are logged and ignored rather than rejected.

### Setting values through Open Horizon

Declare them in the deployment policy's `userInput`:

```json
"userInput": [
  {
    "serviceOrgid": "myorg",
    "serviceUrl": "service-edge-ai",
    "serviceArch": "amd64",
    "serviceVersionRange": "[0.0.0,INFINITY)",
    "inputs": [
      { "name": "MONITOR_LLM_MODEL", "value": "llama3.2:3b-instruct-q4_K_M" },
      { "name": "MONITOR_LLM_TIMEOUT", "value": "120" }
    ]
  }
]
```

> ⚠️ **A policy edit does not restart a running service.** The agent keeps the
> existing agreement and your new values never take effect. After republishing:
>
> ```shell
> hzn agreement list          # find the monitor's agreement id
> hzn agreement cancel <id>   # re-negotiates within ~1 minute
> ```

---

## Choosing a model

```shell
MONITOR_LLM_MODEL=llama3.2:3b-instruct-q4_K_M   # default
```

Any model your Ollama runtime can serve works. Three things matter, in order:

1. **It must reliably emit JSON.** The client requests `format: json` and
   validates every field, retrying once with a clarified prompt. A model that
   cannot hold the schema fails both attempts and the analysis is dropped.
2. **It must fit in RAM beside the workloads it watches.** The monitor is meant
   to be invisible to the services it monitors.
3. **It must finish inside `MONITOR_LLM_TIMEOUT`.**

### Measured inference times

One error, full 100-line context window, on an 8-core x86_64 node, CPU-only:

| Model | Size | Time per analysis | Verdict |
|---|---|---|---|
| `llama3.2:3b-instruct-q4_K_M` | ~2 GB | **~55 s** | Recommended default |
| `qwen2.5-coder:7b` | 4.7 GB | **~136 s** | Only with a long timeout and low error rate |

**Neither fits the 30 s default on CPU-only hardware.** That default assumes GPU
acceleration. Raise it (see below) or expect every analysis to time out.

### Sizes worth avoiding

- **1B and below** — plausible-sounding but frequently wrong root causes, and
  they drop schema fields.
- **7B and above on CPU** — see the measurement. Throughput becomes roughly one
  analysis every two minutes, so the queue sheds work under any real error rate.

### Where models live

Models are stored in the `/var/lib/ollama` volume (`OLLAMA_MODELS`), so a pull
happens once per node and survives container restarts.

On startup the monitor lists available models and pulls the configured one if
missing. The pull has its own generous timeout — it does **not** inherit
`MONITOR_LLM_TIMEOUT`, because a 2 GB download has nothing to do with the
inference budget.

Air-gapped nodes cannot pull. Pre-seed the volume and disable pulling:

```shell
MONITOR_LLM_AUTO_PULL=false
```

Startup then reports the missing model in `startup_errors` and analyses fail,
while discovery and log monitoring keep running.

### Using an external Ollama

```shell
MONITOR_LLM_HOST=http://inference-host.local:11434
```

This trades the edge-first, offline guarantee for speed — log content leaves the
node, so only do it on a network you trust.

> Under host networking (which the monitor requires), the container's bundled
> Ollama finds port 11434 already taken if the host runs its own, and uses the
> host's runtime. On a node that already has models pulled, that is usually what
> you want.

---

## Tuning the analysis timeout

```shell
MONITOR_LLM_TIMEOUT=120     # seconds; default 30
```

The single most important setting on CPU-only hardware. Symptom of it being too
low:

```
ERROR analysis failed for /var/log/...: LLM analysis exceeded 30.0s limit
```

**How to pick a value:** measure, then double it. Time one analysis directly
against your runtime and model —

```shell
time curl -sS http://localhost:11434/api/chat -d '{
  "model": "llama3.2:3b-instruct-q4_K_M",
  "messages": [{"role":"user","content":"Summarise: ERROR connection refused"}],
  "format": "json", "stream": false
}' > /dev/null
```

— then set the timeout to roughly twice that. A real prompt carries ~100 lines
of log context, so it runs longer than this minimal probe.

The trade-off: a higher timeout means a stuck analysis occupies the worker for
longer, so errors queue up behind it and low-severity work gets shed.

---

## Tuning throughput and back-pressure

Inference is the bottleneck. Everything here decides what happens when errors
arrive faster than the model can analyse them.

| Variable | Default | Raise it when | Lower it when |
|---|---|---|---|
| `MONITOR_MAX_QUEUE_SIZE` | `50` | Bursts are brief and you want them all analysed | You would rather shed early than analyse stale errors |
| `MONITOR_CACHE_TTL` | `900` | Errors repeat and re-analysis is wasted | Conditions change fast and stale analyses mislead |
| `MONITOR_POLL_INTERVAL` | `60` | — | You need faster reaction to workloads starting/stopping |
| `MONITOR_LOG_POLL_INTERVAL` | `1` | The node is I/O constrained | You need faster error detection |

When the queue is full the monitor sheds the **lowest-severity** pending
requests, never the newest. Watch it happen:

```shell
curl -sS http://127.0.0.1:8080/health | jq '{analysis_queue_depth, analyses_dropped, analyses_failed}'
```

Persistent `analyses_dropped` means the arrival rate genuinely exceeds capacity.
Raising the queue size only delays shedding — it adds no throughput. The real
fixes are a faster model or narrower `MONITORING_ERROR_PATTERNS` on the
workloads (which is a service developer's change, not yours).

---

## Tuning memory and log handling

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_MAX_BUFFER_MB` | `100` | In-memory log buffer ceiling across all watched files |

At the limit the monitor drops the oldest buffered context and warns. Two
further ceilings are fixed rather than configurable: a monitored file over 1 GB
is tailed from its most recent 100 MB, and the context window is 50 lines either
side of an error.

Lower `MONITOR_MAX_BUFFER_MB` on memory-constrained nodes; raise it only if you
monitor many high-volume files and see buffer warnings.

---

## Complete variable reference

### LLM

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_LLM_MODEL` | `llama3.2:3b-instruct-q4_K_M` | Ollama model tag |
| `MONITOR_LLM_HOST` | `http://localhost:11434` | Ollama runtime endpoint |
| `MONITOR_LLM_TIMEOUT` | `30` | Seconds before an analysis is cancelled |
| `MONITOR_LLM_AUTO_PULL` | `true` | Pull the model at startup if missing |

### Analysis

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_MAX_QUEUE_SIZE` | `50` | Pending analyses before shedding |
| `MONITOR_CACHE_TTL` | `900` | Seconds a completed analysis is reused |

### Context and steering

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_CONTEXT_DIR` | `/etc/edge-ai-monitor/context` | Node-wide guidance directory |
| `MONITOR_CONTEXT_ENABLED` | `true` | Master switch for knowledge injection |
| `MONITOR_CONTEXT_MAX_BYTES` | `8000` | Total guidance budget per prompt |
| `MONITOR_CONTEXT_MAX_FILE_BYTES` | `4000` | Per-file truncation point |

See [context-and-steering.md](context-and-steering.md).

### Northbound export

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_EXPORT_ENABLED` | `true` | Master switch; inert without sinks |
| `MONITOR_EXPORT_SPOOL_DIR` | `/var/lib/monitor/spool` | Store-and-forward queue |
| `MONITOR_EXPORT_SPOOL_MAX_BYTES` | `67108864` | Spool ceiling (64MB) |
| `MONITOR_EXPORT_NODE_ID` | *(asks the agent)* | Node identity on exported records |
| `MONITOR_EXPORT_INCLUDE_NODE` | `true` | Attach node identity at all |

Sinks themselves are configured under `export.sinks` in `config.yaml`. See
[northbound-export.md](northbound-export.md).

### Discovery and logs

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_ANAX_URL` | `http://localhost:8510` | Open Horizon agent API |
| `MONITOR_ANAX_TIMEOUT` | `10` | Seconds per anax request |
| `MONITOR_POLL_INTERVAL` | `60` | Seconds between discovery polls |
| `MONITOR_LOG_POLL_INTERVAL` | `1` | Seconds between log reads |
| `MONITOR_MAX_BUFFER_MB` | `100` | In-memory log buffer ceiling |

### Output and operations

| Variable | Default | Meaning |
|---|---|---|
| `MONITOR_PROPOSAL_DIR` | `/var/lib/monitor/proposals` | Where proposals are written |
| `MONITOR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `MONITOR_LOG_FORMAT` | `%(asctime)s %(levelname)s [%(name)s] %(message)s` | Python logging format |
| `MONITOR_HEALTH_ENABLED` | `true` | Serve the health endpoint |
| `MONITOR_HEALTH_PORT` | `8080` | Health endpoint port |
| `MONITOR_CONFIG` | `/etc/edge-ai-monitor/config.yaml` | Config file location |

---

## Profiles that work

**Constrained node** (2–4 GB RAM, slow CPU) — accept fewer, slower analyses:

```shell
MONITOR_LLM_MODEL=llama3.2:3b-instruct-q4_K_M
MONITOR_LLM_TIMEOUT=180
MONITOR_MAX_QUEUE_SIZE=20
MONITOR_MAX_BUFFER_MB=50
MONITOR_CACHE_TTL=1800
```

**Capable node** (8+ GB RAM, GPU or fast CPU) — the defaults, with a realistic
timeout:

```shell
MONITOR_LLM_TIMEOUT=120
```

**Debugging a deployment** — verbose, no shedding, no cache masking behaviour:

```shell
MONITOR_LOG_LEVEL=DEBUG
MONITOR_CACHE_TTL=0
MONITOR_POLL_INTERVAL=15
```

`MONITOR_CACHE_TTL=0` makes every occurrence re-analyse, which is expensive —
use it while testing, not in production.

---

## Related

| Document | Contents |
|---|---|
| [context-and-steering.md](context-and-steering.md) | Steering the model with SKILL.md files, runbooks, wiki content |
| [troubleshooting.md](troubleshooting.md) | Symptom-first diagnosis |
| [deployment.md](deployment.md) | Publishing and deploying the monitor |
| [service-developer-guide.md](service-developer-guide.md) | For workload owners opting in |
