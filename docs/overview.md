# Repository overview

**New here? Read this first.** It explains what this project is, how the code is
laid out, and the handful of non-obvious constraints that shape almost every
design decision in it.

For *using* the service, see the [documentation index](README.md). This page is
about the repository itself.

---

## What this is

An **Open Horizon service that monitors other Open Horizon services** on the same
edge node, using a local LLM to explain their errors.

It runs as an ordinary containerized service on an edge node. It asks the local
Open Horizon agent what else is running, reads the logs of workloads that have
explicitly opted in, and when it sees an error it asks a local language model
what went wrong and what to do about it. The answer is written to disk as a JSON
proposal.

```
workload logs ─▶ error detected ─▶ local LLM analysis ─▶ remediation proposal JSON
```

Three properties define the design:

- **Local.** Inference runs on the node. Log content never leaves it. The service
  works with no internet connection.
- **Opt-in.** A workload is never read unless its own service definition asks to
  be monitored. There is no way for an operator to monitor a workload that has
  not consented.
- **Advisory.** It proposes; it never acts. Nothing restarts, reconfigures, or
  modifies a monitored workload.

### What it is not

Not an alerting system, not an auto-remediation system, not a log aggregator, and
not multi-node. It watches one node and writes files. Anything that consumes
those files is somebody else's component.

---

## How it works

Four independent loops, deliberately decoupled so that slow LLM inference can
never stall log reading:

| Loop | Cadence | Job |
|---|---|---|
| Discovery | 60 s | Ask the agent what is running; track state changes |
| Log monitor | 1 s | Tail permitted log files; match error patterns |
| Analyzer | queue-driven | Run inference; emit results |
| Health | on request | Serve `/health` |

The path an error takes:

1. **Discovery** polls the anax API and builds a registry of workloads, each with
   its state and its monitoring opt-in.
2. **Log monitoring** tails the declared files for every opted-in, running
   workload. On a pattern match it captures 50 lines either side.
3. The error is **queued**, never analysed inline. Identical errors aggregate;
   recently-analysed ones are served from cache.
4. The **analyzer** builds a prompt — workload metadata, any operator-supplied
   documentation, then the log window — and asks the model for structured JSON.
5. The **generator** turns that into a proposal, flags it for human review if
   warranted, and writes it to disk.

Back-pressure is handled by shedding the *lowest-severity* queued work, never by
slowing down log reading.

See [architecture.md](architecture.md) for diagrams and failure behaviour.

---

## Repository layout

```
src/edge_ai_monitor/    the service (~2,750 lines)
tests/                  210 test functions (242 cases with parametrisation)
horizon/                Open Horizon service definition and policies
docs/                   user and operator documentation
config/config.yaml      default configuration, fully commented
openspec/               the specification this was built from
Dockerfile              multi-arch (amd64/arm64) image, bundles Ollama
docker-compose.yml      local harness: monitor + a workload that emits errors
Makefile                build, publish, deploy, test
```

### The modules, in data-flow order

| Module | Lines | Responsibility |
|---|---|---|
| `anax_client.py` | 122 | Read-only Open Horizon agent API client, with bounded retries |
| `workload_registry.py` | 512 | Discovery, state tracking, parsing the `MONITORING_*` opt-in |
| `log_monitor.py` | 413 | Tailing, rotation detection, pattern matching, context windows |
| `context.py` | 176 | Loading domain knowledge (SKILL.md, runbooks) for the prompt |
| `ollama_client.py` | 397 | Prompt construction, inference, JSON validation and retry |
| `error_analyzer.py` | 306 | Priority queue, aggregation, caching, orchestration |
| `remediation.py` | 207 | Proposal construction, review flags, persistence |
| `config.py` | 259 | Config file + env overrides + validation |
| `health.py` | 89 | Health/status HTTP endpoint |
| `main.py` | 266 | Wires it together; lifecycle and signal handling |

`main.py` is the best entry point for reading the code: its `MonitorService`
constructor builds every component and shows how they connect in under 50 lines.

---

## Getting started

### Run the tests

```shell
pip install -e '.[dev]'
make unittest
```

The suite is hermetic: the anax API and the Ollama runtime are stubbed, and log
tests drive real files in temp directories. No node, no hub, no model required.

### Run it locally

```shell
docker compose up --build
curl -sS http://127.0.0.1:8080/health | jq
```

This starts the monitor next to a sample workload that emits an error every 60
seconds, so you can watch the whole pipeline without an Open Horizon deployment.

### Deploy it for real

See [deployment.md](deployment.md). You will need a hub, a registered node, and
at least 4 GB of RAM on that node.

---

## Five things that will surprise you

Each of these was discovered by deploying to a real node, and each one shapes
code you will otherwise find puzzling.

**1. A custom field in a service definition does not survive publishing.**
The Open Horizon exchange stores only its known schema fields and silently drops
everything else. The original design put monitoring config in a top-level
`monitoring` section; it never reached the node. The opt-in lives in
`MONITORING_*` environment variables inside the `deployment` string instead,
because that string is stored verbatim and signed.

**2. Discovery has to union two API endpoints.** `/service/config` is empty on a
policy-registered node, so a workload can be running under an active agreement
and appear nowhere in it. Agreements say what is *running*; service definitions
carry the *opt-in*. Neither alone sees everything.

**3. The monitor requires host networking, which requires privilege.** The anax
API binds `127.0.0.1` only — no bridge-networked container can reach it at any
address. Host networking makes Open Horizon classify the service as privileged,
so the node must advertise `openhorizon.allowPrivileged: true`.

**4. CPU-only inference is much slower than the defaults assume.** On an 8-core
x86_64 node, a 3B quantized model takes ~55 s per analysis and a 7B model ~136 s
— against a 30 s default timeout. Every deployment needs
`MONITOR_LLM_TIMEOUT` raised. This is why the analyzer is a queue with shedding
rather than a simple worker.

**5. Context is what makes the output useful.** Without domain knowledge the
model produces generically correct, deployment-agnostic advice — and sometimes
advice that is wrong for your architecture. Given a runbook it names the real
components and returns the real recovery commands. See
[context-and-steering.md](context-and-steering.md) for the measured comparison.

---

## Design conventions

**External dependencies are optional at startup and recoverable at runtime.**
The agent unreachable, the model missing, the log path absent, the disk
unwritable, the health port taken — none of these abort the service. They are
reported in `startup_errors` and the loops recover on their own, because on an
edge node the monitor may well start before the things it depends on.

**Nothing blocks log reading.** Detection is cheap and must keep up; analysis is
expensive and is allowed to fall behind. Anything that could block gets a queue
in front of it.

**Failures degrade rather than cascade.** One unreadable log path does not stop
the others. A proposal that cannot be written to disk is printed to stdout
instead of being lost. A callback that raises is caught and logged.

**Configuration is layered:** built-in defaults → `config.yaml` → `MONITOR_*`
environment variables. The defaults alone are a working configuration.

---

## How this was built

The repo uses [OpenSpec](https://github.com/Fission-AI/OpenSpec): the change in
`openspec/changes/llm-workload-monitor/` holds the proposal, design decisions,
capability specs, and task list that this implementation was built from.

If you are changing behaviour, update the specs alongside the code — `design.md`
records not just what was decided but what was tried and rejected, including
several approaches that failed only when tested against a live hub. That history
is the most valuable thing in the directory.

---

## Where to go next

| If you want to… | Read |
|---|---|
| Get your workload monitored | [service-developer-guide.md](service-developer-guide.md) |
| Deploy the monitor to a node | [deployment.md](deployment.md) |
| Choose a model or tune settings | [tuning.md](tuning.md) |
| Make the analyses smarter | [context-and-steering.md](context-and-steering.md) |
| Understand the internals | [architecture.md](architecture.md) |
| Diagnose something broken | [troubleshooting.md](troubleshooting.md) |
| Contribute | [../CONTRIBUTING.md](../CONTRIBUTING.md) — commits need DCO sign-off (`git commit -s`) |
