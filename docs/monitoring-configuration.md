# Enabling monitoring for your workload

> Looking for the full end-to-end walkthrough — sharing logs, publishing,
> verifying, reading proposals? See the
> **[service developer guide](service-developer-guide.md)**. This page is the
> in-depth reference for the configuration itself.

Monitoring is **opt-in**. The edge-ai monitor never reads a workload's logs
unless that workload's service definition explicitly asks it to. A service with
no `MONITORING_*` variables is discovered, but never monitored.

## The monitoring variables

Add `MONITORING_*` environment variables to your service's **deployment**
section:

```json
{
  "org": "examples",
  "url": "example.com.sensor",
  "version": "1.2.0",
  "arch": "amd64",
  "deployment": {
    "services": {
      "sensor": {
        "image": "examples/sensor:1.2.0",
        "environment": [
          "MONITORING_ENABLED=true",
          "MONITORING_LOG_PATHS=/var/log/workloads/sensor/app.log",
          "MONITORING_ERROR_PATTERNS=ERROR,FATAL,SensorFault"
        ]
      }
    }
  }
}
```

> **They must be inside `deployment`.** A custom top-level `monitoring` section
> does not work: the Open Horizon exchange stores only its known service schema
> fields and silently discards anything else, so such a section never reaches
> the node. The `deployment` string is stored verbatim and signed, so variables
> declared there arrive intact.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `MONITORING_ENABLED` | yes | *(absent = off)* | `true`, `1`, or `yes` (case-insensitive) enables monitoring. |
| `MONITORING_LOG_PATHS` | yes when enabled | — | Log files to tail, as the **monitor container** sees them. |
| `MONITORING_ERROR_PATTERNS` | no | `ERROR,FATAL,Exception,CRITICAL` | Case-insensitive patterns marking a line as an error. |

Full reference: [`horizon/monitoring-variables.md`](../horizon/monitoring-variables.md).

### List syntax

Both list variables are comma-separated. If a value begins with `[` it is parsed
as a JSON array instead, which is how you express a pattern containing a comma:

```
MONITORING_ERROR_PATTERNS=["retry a{1,3} failed", "FATAL"]
```

A malformed value disables monitoring for that workload and logs a warning,
rather than monitoring with partial configuration.

### About `MONITORING_ERROR_PATTERNS`

Each entry is treated as a **regular expression**, matched case-insensitively.
If an entry is not a valid regex it falls back to a literal substring match, so
a pattern like `error(` still works as written.

Omitting the variable, or supplying an empty value, applies the defaults.

Keep patterns specific. Broad patterns generate more analysis requests, and the
analysis queue sheds low-severity work once it exceeds
`MONITOR_MAX_QUEUE_SIZE` (default 50).

### Multi-container services

The variables may be declared on any container in the deployment; log paths are
combined across all of them.

## Making logs visible to the monitor

`MONITORING_LOG_PATHS` entries are resolved **inside the monitor container**,
not inside your workload. Both containers must share a volume.

1. Have your workload write logs under a shared host directory, conventionally
   `/var/log/workloads/<service-name>/`:

   ```json
   "privileged": true,
   "binds": ["/var/log/workloads/sensor:/var/log/app:rw"]
   ```

   Your workload writes to `/var/log/app/app.log`.

   > **`privileged: true` is required for a writable bind to a root-owned host
   > directory.** Without it the agent refuses the deployment with
   > *"contains unsupported bind for a workload, Write permission for bind to
   > /var/log/workloads/sensor is denied"*, and the workload crash-loops. The
   > node must also advertise `openhorizon.allowPrivileged: true`.
   >
   > If you would rather not run privileged, use a **named volume** shared
   > between the workload and the monitor instead — named volumes accept `rw`
   > without privilege:
   >
   > ```json
   > "binds": ["edge-ai-workload-logs:/var/log/app:rw"]
   > ```
   >
   > The monitor must then mount the same named volume read-only in place of
   > the host path.

2. The monitor mounts the same tree read-only (already configured in
   [`horizon/service.definition.json`](../horizon/service.definition.json)):

   ```json
   "binds": ["/var/log/workloads:/var/log/workloads:ro"]
   ```

3. Set `MONITORING_LOG_PATHS` to the **monitor's** view of the file:

   ```
   MONITORING_LOG_PATHS=/var/log/workloads/sensor/app.log
   ```

The monitor only ever opens these files for reading.

### stdout-only workloads

The monitor reads files, not container stdout. If your workload logs only to
stdout, either configure your logging library to also write a file in the shared
mount, or point the variable at the container's json-file log:

```
MONITORING_LOG_PATHS=/var/lib/docker/containers/<container-id>/<container-id>-json.log
```

The first option is strongly preferred — container IDs change on every restart.

## What the monitor does with a match

1. Collects a context window of 50 lines before and 50 lines after the matching
   line.
2. Aggregates identical errors: the same error 3+ times within 5 minutes becomes
   one analysis carrying an occurrence count. Digits are normalised when
   comparing, so `retry 1 of 5` and `retry 2 of 5` count as the same error.
3. Sends the window plus your service metadata to the local LLM.
4. Writes a remediation proposal to
   `/var/lib/monitor/proposals/<workload>/<problem-key>.json`. A recurring
   error updates that one file rather than creating a new one each time.

Nothing leaves the node: inference is local, and proposals are written to local
storage only.

## Resource limits you should know about

| Limit | Value | Behaviour when exceeded |
|---|---|---|
| Log buffer | 100 MB (`MONITOR_MAX_BUFFER_MB`) | Oldest buffered entries dropped, warning logged. |
| Log file size | 1 GB | Only the most recent 100 MB is tailed, warning logged. |
| Analysis queue | 50 (`MONITOR_MAX_QUEUE_SIZE`) | Lowest-severity pending requests are shed. |
| Analysis timeout | 30 s default, but realistically 120 s (`MONITOR_LLM_TIMEOUT`) | Request cancelled, timeout logged. CPU-only inference takes ~55 s with the default 3B model, so node operators normally raise this. |

## Turning monitoring off

Set `MONITORING_ENABLED=false`, or remove the variables entirely, and
republish the service. The monitor stops watching the workload's logs on its
next discovery poll (within 60 seconds by default).
