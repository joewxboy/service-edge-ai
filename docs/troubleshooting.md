# Troubleshooting

Start here:

```shell
curl -sS http://127.0.0.1:8080/health | jq
```

The `startup_errors` array and the counters below it identify most problems
without reading a single log line.

You can also validate configuration and dependencies without starting the
service:

```shell
edge-ai-monitor --check
```

---

## No workloads are discovered

`"workloads_discovered": 0`

**Is the agent reachable?**

```shell
curl -sS http://localhost:8510/node | jq .configstate.state
```

Expect `"configured"`. If the request fails, the agent is not running or the
monitor is pointed at the wrong URL — check `MONITOR_ANAX_URL`.

**Running in a container?** The monitor **must** share the host network
namespace. anax binds to `127.0.0.1:8510` only:

```shell
ss -lnt | grep 8510      # LISTEN 127.0.0.1:8510
```

Because it never listens on the docker bridge, a bridge-networked container
cannot reach the agent at *any* address — `host.docker.internal` and
`host-gateway` do not help. The shipped service definition already sets
`"network": "host"`; if you are running the image by hand, use:

```shell
docker run --network host ... joewxboy/edge-ai:0.0.5
```

Under host networking the container's bundled Ollama also finds port 11434
already bound if the host runs its own Ollama, and will use the host's runtime —
usually what you want on a node that already has models pulled.

**Is anything actually deployed?**

```shell
hzn agreement list
```

No active agreements means there is genuinely nothing running to discover.

> Workloads are discovered from `/agreement`, `/service`, and `/service/config`
> together. A workload running under an agreement appears even when it has no
> `/service/config` entry — which is the norm, since that endpoint is empty on a
> policy-registered node.

---

## Workloads are discovered but not monitored

`"workloads_discovered": 3, "workloads_monitored": 0`

A workload is only monitored when **all three** hold:

1. Its deployment environment sets `MONITORING_ENABLED=true`.
2. It has at least one active agreement (state is `running`).
3. `MONITORING_LOG_PATHS` is non-empty.

The service log says which condition failed:

```
INFO skipping workload examples/... : monitoring not enabled in service definition
```

A workload discovered only through an agreement has no deployment string to
carry the opt-in, so it is never monitored — this is by design.

**Check what actually reached the node**, since a `monitoring` section placed at
the top level of a service definition is silently discarded by the exchange:

```shell
curl -sS http://localhost:8510/service \
  | jq -r '.definitions.active[] | select(.specRef=="example.com.sensor") | .deployment' \
  | jq '.services[].environment'
```

`null` means the variables never arrived — they must be inside `deployment`.

---

## Monitoring is enabled but no log paths are watched

`"workloads_monitored": 1, "log_paths_watched": 0`

Look for:

```
WARNING log path /var/log/workloads/sensor/app.log is not accessible: [Errno 2] No such file or directory
```

Causes, in order of likelihood:

- **The volume is not shared.** `MONITORING_LOG_PATHS` is resolved inside the *monitor*
  container. Confirm the file is visible there:
  ```shell
  docker exec edge-ai-monitor ls -l /var/log/workloads/sensor/
  ```
- **The file does not exist yet.** The monitor does not create log files. It
  picks the path up on a later discovery poll once the workload creates it.
- **Permissions.** The monitor must be able to read the file. The mount is
  read-only by design; read permission is still required.

---

## Errors in the log produce no proposals

**Do the patterns match?** Matching is case-insensitive and each pattern is
treated as a regex. A malformed `MONITORING_*` value disables monitoring for the
workload and logs a warning. Test one:

```shell
grep -iE 'ERROR|FATAL|Exception|CRITICAL' /var/log/workloads/sensor/app.log
```

**Only new lines are read.** The monitor seeks to the end of a file when it
starts watching it; pre-existing content is never replayed. Append a fresh line
to test.

**An error on the very last line waits briefly.** Trailing context is collected
before analysis, so the error is emitted on the next read cycle (about a second
later).

**Repeated identical errors are aggregated.** The same error 3+ times in 5
minutes produces one analysis carrying an occurrence count — not three. Digits
are normalised, so `retry 1 of 5` and `retry 2 of 5` count as the same error.

**Recently analysed errors are served from cache** for 15 minutes
(`MONITOR_CACHE_TTL`), rather than re-analysed.

---

## Analyses time out

```
ERROR analysis failed for /var/log/...: LLM analysis exceeded 30.0s limit
```

Inference time is dominated by model size and host CPU. Measured on an 8-core
x86_64 node, CPU-only:

| Model | Time for one analysis |
|---|---|
| `llama3.2:3b-instruct-q4_K_M` (default) | **~55 s** |
| `qwen2.5-coder:7b` | **~136 s** |

**Neither completes inside the 30-second default on CPU-only hardware.** Raise
the limit:

```shell
MONITOR_LLM_TIMEOUT=120
```

Through Open Horizon, set it in the deployment policy `userInput` and cancel the
agreement (`hzn agreement cancel <id>`) so the agent re-negotiates — a policy
edit alone does not restart a running service with new values.

Raising the timeout increases the chance the analysis queue backs up and starts
shedding low-severity work.

---

## Queue is full / analyses are dropped

`"analysis_queue_depth": 50, "analyses_dropped": 17`

The queue holds 50 requests and sheds the lowest-severity entries when full.
Persistent shedding means errors arrive faster than the LLM can analyse them:

- Narrow `MONITORING_ERROR_PATTERNS` so fewer lines qualify.
- Use a smaller/faster model.
- Raise `MONITOR_MAX_QUEUE_SIZE` (delays shedding; does not add throughput).

---

## Analyses fail with "LLM returned an empty response"

The runtime answered but the reply carried no content. Check, in order:

- **Model still loading.** The first call after a restart can return empty while
  the model is paged in. It resolves on its own.
- **The model does not honour `format: json`.** Try it directly:
  ```shell
  curl -sS http://localhost:11434/api/chat -d '{"model":"llama3.2:3b-instruct-q4_K_M","messages":[{"role":"user","content":"Reply with {\"ok\":1}"}],"format":"json","stream":false}'
  ```

---

## Ollama is unavailable

```
ERROR Ollama runtime not available at http://localhost:11434
```

```shell
curl -sS http://localhost:11434/api/tags | jq '.models[].name'
```

The container starts `ollama serve` from its entrypoint. If the runtime is
external, set `MONITOR_LLM_HOST`.

The monitor keeps running with Ollama down — discovery and log monitoring
continue, and analyses fail until the runtime returns.

---

## Model is missing

```
INFO model llama3.2:3b-instruct-q4_K_M not found locally; pulling
```

The first pull downloads roughly 2 GB and needs connectivity. On an air-gapped
node, pre-seed the model volume and disable pulling:

```shell
MONITOR_LLM_AUTO_PULL=false
```

---

## Proposals are not being written

`"proposal_storage_failures": 4`

```
ERROR could not write proposal to /var/lib/monitor/proposals/...: [Errno 13] Permission denied
```

The proposal is not lost — it is printed to stdout as a fallback, so
`make log` still shows it. Fix the mount:

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox ls -ld /state/proposals
```

Or redirect with `MONITOR_PROPOSAL_DIR`.

---

## Health endpoint is unreachable

```
ERROR could not bind health endpoint to 0.0.0.0:8080: [Errno 98] Address already in use
```

Something else holds port 8080. The service keeps running without the health
endpoint. Change it with `MONITOR_HEALTH_PORT`, or find the conflict:

```shell
ss -lntp | grep 8080
```

The endpoint returns **503** while the service is shutting down, and **200**
otherwise. A temporarily unreachable dependency is reported in
`startup_errors` but does not make the service unhealthy — the loops recover on
their own.

---

## Turning up logging

```shell
MONITOR_LOG_LEVEL=DEBUG
```

DEBUG adds per-error aggregation and cache decisions, which is usually what you
need when proposals are missing but errors are clearly present.
