# LLM model selection and updates

> For the full configuration reference — timeouts, throughput, and every
> `MONITOR_*` variable — see [tuning.md](tuning.md). This page covers model
> choice, updates, and air-gapped nodes in depth.

## The default

`llama3.2:3b-instruct-q4_K_M` — Llama 3.2 3B, 4-bit quantized.

It was chosen because it is the smallest model that reliably does the two things
this service needs: reason about an error in context, and emit well-formed JSON
on demand. It needs roughly 2 GB of RAM and runs acceptably on CPU-only edge
hardware.

## Choosing a different model

Any Ollama model works:

```shell
MONITOR_LLM_MODEL=qwen2.5:3b-instruct-q4_K_M
```

What matters, in order:

1. **It must follow instructions well enough to return JSON.** The client asks
   Ollama for `format: json` and validates every field, retrying once with a
   clarified prompt. A model that cannot hold the schema fails both attempts and
   the analysis is dropped.
2. **It must fit in RAM alongside the workloads being monitored.** The monitor
   is meant to be invisible to the services it watches.
3. **It must finish inside `MONITOR_LLM_TIMEOUT`.** The 30 s default is
   optimistic for CPU-only nodes — see the measurements below.

### Measured inference times

Measured on an 8-core x86_64 node, CPU-only, with a full 100-line context
window:

| Model | Size | Time |
|---|---|---|
| `llama3.2:3b-instruct-q4_K_M` | ~2 GB | **~55 s** |
| `qwen2.5-coder:7b` | 4.7 GB | **~136 s** |

**Both exceed the 30-second default.** CPU-only inference is far slower than the
30 s budget assumes; that default is realistic only with GPU acceleration or a
notably faster CPU. On a typical edge node, raise it:

```shell
MONITOR_LLM_TIMEOUT=120
```

The shipped deployment policy sets 120 s for this reason. Expect the analysis
queue to shed low-severity work under sustained load — with a 55 s analysis, the
monitor completes roughly one error per minute.

### Sizes worth avoiding

- **1B and below:** produce plausible-sounding but frequently wrong root causes,
  and drop schema fields.
- **7B and above on CPU:** ~136 s per analysis, as measured above. Usable only
  with a much longer timeout and a low error rate.

## How models are obtained

On startup the monitor lists the models Ollama has and pulls the configured one
if it is missing:

```
INFO model llama3.2:3b-instruct-q4_K_M not found locally; pulling
```

The first pull downloads roughly 2 GB. Models live in the `/var/lib/ollama`
volume (`OLLAMA_MODELS`), so the download happens once per node and survives
container restarts.

## Updating a model

```shell
# Pull the new model into the shared volume
docker exec edge-ai-monitor ollama pull llama3.2:3b-instruct-q5_K_M

# Point the service at it and restart
MONITOR_LLM_MODEL=llama3.2:3b-instruct-q5_K_M
```

Through Open Horizon, change the `MONITOR_LLM_MODEL` user input in the
deployment policy and republish; the agent restarts the service with the new
value.

Removing the old model reclaims its disk space:

```shell
docker exec edge-ai-monitor ollama rm llama3.2:3b-instruct-q4_K_M
```

## Air-gapped nodes

Nodes without internet cannot pull. Pre-seed the volume and disable pulling:

```shell
# On a connected machine, populate a volume then transfer it
docker run --rm -v edge-ai-monitor-models:/root/.ollama ollama/ollama pull llama3.2:3b-instruct-q4_K_M

# On the edge node
MONITOR_LLM_AUTO_PULL=false
```

With `auto_pull` disabled and the model absent, startup reports the problem in
`startup_errors` and analyses fail — discovery and log monitoring still run.

## Using an external Ollama

If a more capable machine on the LAN runs Ollama, point the monitor at it:

```shell
MONITOR_LLM_HOST=http://inference-host.local:11434
```

This trades the edge-first, offline guarantee for speed. Log content leaves the
node, so only do this on a network you trust.

## Prompting

The prompt lives in `src/edge_ai_monitor/ollama_client.py`
(`ANALYSIS_SYSTEM_PROMPT` and `ANALYSIS_PROMPT_TEMPLATE`). It pins the JSON
schema, the severity vocabulary, and the confidence scale.

If you change models and see malformed output, tune the system prompt before
concluding the model is unsuitable — small models are sensitive to how firmly
the schema is stated.
