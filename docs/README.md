# Documentation

**New to this repository?** Start with
**[overview.md](overview.md)** — what this project is, how the code is laid out,
and the non-obvious constraints that shape it.

Otherwise, start with the guide for your role.

## I own a workload and want it monitored

→ **[service-developer-guide.md](service-developer-guide.md)** — the complete
walkthrough: share your logs, declare the opt-in, publish, verify, read the
proposals. Start here.

Supporting reference:

| Document | Contents |
|---|---|
| [context-and-steering.md](context-and-steering.md) | **Steering the model with SKILL.md files, runbooks, and wiki content** |
| [monitoring-variables.md](../horizon/monitoring-variables.md) | Every `MONITORING_*` variable and its parsing rules |
| [monitoring-configuration.md](monitoring-configuration.md) | Log mounts and error patterns in depth |
| [examples/](examples/) | Four copy-and-adapt service definitions |

### Examples at a glance

| File | Shows |
|---|---|
| [monitored](examples/service.definition.monitored.json) | The standard case: one log file, custom patterns, host-path bind |
| [multi-log](examples/service.definition.multi-log.json) | Several log files, regex patterns, JSON-array list form |
| [named-volume](examples/service.definition.named-volume.json) | Sharing logs without `privileged: true` |
| [opted-out](examples/service.definition.opted-out.json) | Explicitly declining monitoring |

## I operate an edge node and want to run the monitor

| Document | Contents |
|---|---|
| [deployment.md](deployment.md) | Build, publish, register, verify |
| [tuning.md](tuning.md) | **Model selection, timeouts, throughput, and every config variable** |
| [context-and-steering.md](context-and-steering.md) | Node-wide knowledge injection |
| [llm-models.md](llm-models.md) | Model detail: sizes, updates, air-gapped nodes |
| [troubleshooting.md](troubleshooting.md) | Symptom-first diagnosis |

## I want to understand or change the monitor

| Document | Contents |
|---|---|
| [overview.md](overview.md) | **Repository orientation — read this first** |
| [architecture.md](architecture.md) | Components, data flow, threading, failure behaviour |
| [../README.md](../README.md) | Project overview and configuration reference |
| [../openspec/changes/llm-workload-monitor/](../openspec/changes/llm-workload-monitor/) | The specification this was built from |

---

## Two things that surprise most people

**The opt-in goes inside `deployment`, not at the top level.** The Open Horizon
exchange silently discards unknown top-level service definition fields, so a
`monitoring` section there never reaches the node. See the
[service developer guide](service-developer-guide.md#step-2--declare-the-opt-in).

**Republishing the same version does not redeploy.** The agent keeps the
existing agreement and your changes never take effect. Bump the version or
`hzn agreement cancel <id>`.
