# Monitoring opt-in variables

> This is the variable reference. For the full walkthrough, see the
> [service developer guide](../docs/service-developer-guide.md).

A workload opts into monitoring by declaring `MONITORING_*` environment
variables in the `deployment` section of its service definition.

> **Why environment variables and not a `monitoring` JSON section?**
> The Open Horizon exchange stores only the fields in its service schema and
> silently discards unknown ones. A custom top-level `monitoring` section is
> dropped at publish time and never reaches the node. The `deployment` string,
> by contrast, is stored verbatim and signed, so anything inside it — including
> `environment` — arrives intact.

## Variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `MONITORING_ENABLED` | yes | *(absent = off)* | `true`, `1`, or `yes` (case-insensitive) enables monitoring. Any other value disables it. |
| `MONITORING_LOG_PATHS` | yes when enabled | — | Log files to tail, as the **monitor container** sees them. |
| `MONITORING_ERROR_PATTERNS` | no | `ERROR,FATAL,Exception,CRITICAL` | Case-insensitive patterns marking a line as an error. |
| `MONITORING_CONTEXT_PATHS` | no | — | Documentation files (SKILL.md, runbooks) injected into this workload's analysis prompts. See [context-and-steering.md](../docs/context-and-steering.md). |

## List syntax

`MONITORING_LOG_PATHS`, `MONITORING_ERROR_PATTERNS`, and
`MONITORING_CONTEXT_PATHS` are comma-separated:

```
MONITORING_LOG_PATHS=/var/log/workloads/app/app.log,/var/log/workloads/app/error.log
```

If the value begins with `[` it is parsed as a JSON array instead, which is how
you express a pattern containing a comma:

```
MONITORING_ERROR_PATTERNS=["retry a{1,3} failed", "FATAL"]
```

A malformed value disables monitoring for that workload and logs a warning,
rather than silently monitoring with partial configuration.

## Example

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
        "privileged": true,
        "binds": ["/var/log/workloads/sensor:/var/log/app:rw"],
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

`privileged: true` is required for a writable bind to the root-owned
`/var/log/workloads`; a shared named volume avoids it. See the
[service developer guide](../docs/service-developer-guide.md#step-1--write-logs-where-the-monitor-can-read-them).

## Multi-container services

The variables may be declared on any container in the deployment. Log paths are
combined across all of them, so a sidecar can contribute its own log file:

```json
"services": {
  "app":     { "environment": ["MONITORING_ENABLED=true", "MONITORING_LOG_PATHS=/var/log/workloads/app/app.log"] },
  "sidecar": { "environment": ["MONITORING_LOG_PATHS=/var/log/workloads/app/sidecar.log"] }
}
```

## Where the monitor reads them

```
GET http://localhost:8510/service
  -> definitions.active[]
     -> deployment.services.<name>.environment
```

Verify what the agent actually holds for your workload:

```shell
curl -sS http://localhost:8510/service \
  | jq -r '.definitions.active[] | select(.specRef=="example.com.sensor") | .deployment' \
  | jq '.services[].environment'
```

If that prints `null`, the variables did not reach the node — re-publish the
service and confirm they are inside the `deployment` section, not at the top
level of the service definition.
