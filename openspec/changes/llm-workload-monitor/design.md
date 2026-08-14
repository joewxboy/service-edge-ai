## Context

This design implements an autonomous LLM-based monitoring service for Open Horizon edge workloads. The service runs as a containerized Open Horizon service on edge nodes, periodically discovering other workloads via the anax API, monitoring their logs (with permission), and using an LLM to analyze errors and propose remediation.

**Current State:**
- Open Horizon provides anax API for querying node state, agreements, and service configurations
- Workloads run as containers with logs accessible via container runtime or filesystem
- No existing intelligent monitoring solution for edge workloads

**Constraints:**
- Must run on resource-constrained edge devices (limited CPU, memory)
- Must support multi-arch (x86_64, arm64)
- Must respect workload privacy (opt-in monitoring only)
- Must operate autonomously without cloud connectivity
- Must not impact monitored workload performance

**Stakeholders:**
- Edge operators needing automated error detection
- Service developers wanting intelligent monitoring
- Platform team maintaining Open Horizon infrastructure

## Goals / Non-Goals

**Goals:**
- Autonomous workload discovery via anax API
- Permission-based log monitoring with explicit opt-in
- LLM-powered error analysis and remediation proposals
- Multi-arch container support (x86_64, arm64)
- Minimal resource footprint suitable for edge devices
- Integration with Open Horizon service lifecycle

**Non-Goals:**
- Real-time alerting or notification system (future enhancement)
- Automatic remediation execution (proposals only)
- Cloud-based LLM inference (local only)
- Monitoring of non-Open Horizon workloads
- Historical log analysis (current logs only)
- Multi-node monitoring (single node scope)

## Decisions

### 1. LLM Runtime: Ollama

**Decision:** Use Ollama for local LLM inference

**Rationale:**
- Lightweight, optimized for edge/local deployment
- Simple HTTP API for inference
- Supports quantized models (reduced memory footprint)
- Multi-arch support (x86_64, arm64)
- Easy model management and updates

**Alternatives Considered:**
- llama.cpp: More complex integration, requires building bindings
- Transformers library: Heavy dependencies, slower inference
- Cloud LLM APIs: Violates edge-first constraint, requires connectivity

### 2. Service Architecture: Sidecar Pattern

**Decision:** Deploy as independent Open Horizon service that monitors other services

**Rationale:**
- Clean separation of concerns
- No modification to monitored workloads
- Can be deployed/updated independently
- Follows Open Horizon service model

**Alternatives Considered:**
- Agent plugin: Requires anax modifications, tight coupling
- Library integration: Requires workload code changes
- Daemon process: Harder to manage lifecycle, no Open Horizon integration

### 3. Workload Discovery: Polling Anax API

**Decision:** Poll anax API every 60 seconds for workload state

**Rationale:**
- Simple, reliable mechanism
- Anax API provides authoritative workload state
- Low overhead (read-only queries)
- No need for event streaming infrastructure

**Endpoints Used:**
- `GET /node` - Node registration status
- `GET /service` - Local service definitions, including the deployment string
  that carries each workload's `MONITORING_*` opt-in variables
- `GET /service/config` - Operator-supplied service configuration, when present
- `GET /agreement` - Active agreements and workload state

**Discovery unions `/agreement` and `/service`.** `/service/config` lists only
services the operator supplied user input for, and is empty on a
policy-registered node; a workload can be running under an active agreement
without appearing there at all. Agreements establish what is *running*, while
the service definitions carry the monitoring opt-in. Neither source alone sees
every workload.

**Alternatives Considered:**
- Event-based: Anax doesn't expose event stream, would require modifications
- Container runtime API: Less reliable, doesn't capture Open Horizon semantics

### 4. Permission Model: Deployment Environment Variables

**Decision:** Workloads opt in with `MONITORING_*` environment variables declared
in the `deployment` string of their service definition.

**Format:**
```json
{
  "deployment": {
    "services": {
      "sensor": {
        "image": "examples/sensor:1.2.0",
        "environment": [
          "MONITORING_ENABLED=true",
          "MONITORING_LOG_PATHS=/var/log/workloads/sensor/app.log",
          "MONITORING_ERROR_PATTERNS=ERROR,FATAL,Exception",
          "MONITORING_CONTEXT_PATHS=/var/log/workloads/sensor/SKILL.md"
        ]
      }
    }
  }
}
```

`MONITORING_LOG_PATHS`, `MONITORING_ERROR_PATTERNS`, and
`MONITORING_CONTEXT_PATHS` are comma-separated. A value beginning with `[` is
parsed as a JSON array instead, so patterns containing commas (`a{1,3}`) remain
expressible. `MONITORING_CONTEXT_PATHS` names documentation injected into this
workload's prompts (see Decision 6a).

The monitor reads these from the anax API at
`GET /service` → `definitions.active[].deployment.services.<name>.environment`.

**Rationale:**
- **A custom top-level `monitoring` section does not survive publishing.** The
  Open Horizon exchange stores only its known service schema fields and silently
  discards unknown ones. Verified against a live hub: a published definition
  containing `monitoring` returns from `hzn exchange service list -l` with no
  such key, so the field can never reach the node.
- The `deployment` string is stored verbatim and signed, so anything inside it —
  including `environment` — round-trips intact to the agent. Verified
  empirically.
- Environment variables are already the standard Open Horizon mechanism for
  passing per-service settings, so this adds no new concepts for developers.
- Still an explicit, declarative, per-workload opt-in: absent variables mean no
  monitoring.

**Trade-off:** Flat strings are less structured than nested JSON, so the monitor
validates and normalises the values itself (see the workload-discovery spec).

**Alternatives Considered:**
- **Top-level `monitoring` section:** the original design; does not work, as
  above.
- **`userInput` variables:** retained by the exchange, but `/service/config` is
  empty on a policy-registered node even when the deployment policy supplies
  user input, so the values never become readable locally.
- **Service policy properties:** survive, but reading them requires querying the
  exchange with credentials, breaking the local-only/offline constraint.
- **Separate config file:** additional deployment complexity.
- **Implicit monitoring:** privacy concerns, no control.

### 5. Log Access: Filesystem Mounts

**Decision:** Access logs via shared volume mounts specified in service definition

**Rationale:**
- Direct filesystem access (fast, low overhead)
- Works with any logging mechanism
- No dependency on container runtime APIs
- Supports log rotation and standard patterns

**Implementation:**
- Monitored services mount logs to known paths
- Monitor service mounts same paths read-only
- Service definition specifies mount points

**Alternatives Considered:**
- Container logs API: Limited to stdout/stderr, misses file-based logs
- Log forwarding: Requires workload modifications, additional infrastructure
- Shared logging service: Centralization overhead, single point of failure

### 6. Error Analysis: Sliding Window + LLM

**Decision:** Maintain sliding window of recent log entries, analyze with LLM on error detection

**Process:**
1. Tail log files for new entries
2. Match against error patterns from service definition
3. On match, collect context window (50 lines before/after)
4. Send to LLM with workload metadata for analysis
5. Generate remediation proposal

**Rationale:**
- Context-aware analysis (not just isolated errors)
- Efficient (only analyze on error detection)
- Balances accuracy with resource usage

**Alternatives Considered:**
- Continuous LLM analysis: Too resource-intensive for edge
- Pattern matching only: Misses complex issues, no remediation
- Batch analysis: Delays detection, misses time-sensitive issues

### 6a. Prompt Steering: Injected Domain Knowledge

**Decision:** Inject operator- and developer-supplied documentation (SKILL.md
files, runbooks, wiki exports) into the analysis prompt.

**Rationale:**
- The model knows nothing about the deployment it watches. From log lines alone
  it produces generically correct but deployment-agnostic advice, and sometimes
  advice that is wrong for the architecture.
- Measured on a live node with `llama3.2:3b-instruct-q4_K_M`: for
  `connection refused on db:5432`, an unguided analysis recommended verifying and
  restarting the database. Given a 733-byte SKILL.md explaining that the hostname
  resolves to a connection-pool sidecar and that the database must NOT be
  restarted, the same model identified the sidecar pool exhaustion and returned
  the runbook's exact recovery commands — and did so faster (33s vs 75s).

**Two sources:**
- **Node-wide** — files in `MONITOR_CONTEXT_DIR`, applied to every analysis,
  owned by the node operator.
- **Per-workload** — files named by `MONITORING_CONTEXT_PATHS`, applied to one
  workload, owned by the service developer. Consistent with the other
  `MONITORING_*` opt-in variables.

**Budgets:** a small quantized model has a small context window, and guidance
competes with the log lines that describe the error. Defaults are 4000 bytes per
file and 8000 bytes per prompt. Per-workload documents are assembled first so
the most specific knowledge survives budget pressure.

**Freshness:** files are read at analysis time and cached against mtime/size, so
editing a runbook takes effect on the next error without a restart or republish.

**Alternatives Considered:**
- **Fine-tuning a model per deployment:** far too heavy for edge nodes, and
  stale the moment the deployment changes.
- **Embedding knowledge in the system prompt at build time:** requires a rebuild
  per deployment and cannot carry per-workload knowledge.
- **Retrieval over a vector store:** more infrastructure and memory than an edge
  node can spare, for a corpus small enough to fit in the prompt outright.

### 7. Model Selection: Llama 3.2 3B Quantized

**Decision:** Use Llama 3.2 3B model with 4-bit quantization

**Rationale:**
- Small enough for edge devices (~2GB RAM)
- Strong reasoning capabilities for error analysis
- Good instruction following for structured output
- Quantization reduces memory without significant quality loss

**Alternatives Considered:**
- Larger models (7B+): Too resource-intensive for edge
- Smaller models (1B): Insufficient reasoning capability
- Specialized models: Limited availability, less flexible

### 8. Output Format: Structured JSON

**Decision:** LLM outputs structured JSON with error analysis and remediation steps

**Format:**
```json
{
  "error_summary": "Brief description",
  "root_cause": "Likely cause analysis",
  "severity": "low|medium|high|critical",
  "remediation_steps": [
    "Step 1: Action to take",
    "Step 2: Next action"
  ],
  "confidence": 0.85
}
```

**Rationale:**
- Machine-readable for future automation
- Structured for consistent presentation
- Confidence score for reliability assessment

## Risks / Trade-offs

**[Risk] LLM inference latency on resource-constrained devices**
→ Mitigation: Use quantized models, async processing, queue analysis requests

**[Risk] False positives in error detection**
→ Mitigation: Configurable error patterns, confidence scoring, human review loop

**[Risk] Privacy concerns with log content**
→ Mitigation: Explicit opt-in, local-only processing, no data persistence beyond analysis

**[Risk] Resource contention with monitored workloads**
→ Mitigation: CPU/memory limits, low-priority scheduling, configurable polling intervals

**[Risk] Model hallucination in remediation proposals**
→ Mitigation: Confidence scoring, disclaimer in output, human verification required

**[Risk] Log file access permissions**
→ Mitigation: Clear documentation, validation on startup, graceful degradation

**[Trade-off] Local LLM vs Cloud API**
- Chosen: Local LLM for edge-first, privacy, offline operation
- Cost: Higher resource usage, limited model size, slower inference

**[Trade-off] Polling vs Event-driven**
- Chosen: Polling for simplicity, reliability
- Cost: Slight delay in workload discovery, periodic overhead

**[Trade-off] Structured output vs Natural language**
- Chosen: Structured JSON for automation potential
- Cost: More complex prompting, potential parsing errors

## Migration Plan

**Phase 1: Initial Deployment**
1. Build and publish multi-arch container images
2. Create Open Horizon service definition and policies
3. Deploy to test nodes with sample workloads
4. Validate workload discovery and permission checking

**Phase 2: Monitoring Integration**
1. Update sample service definitions with monitoring config
2. Test log access and error detection
3. Validate LLM analysis and proposal generation
4. Tune error patterns and analysis prompts

**Phase 3: Production Rollout**
1. Document monitoring configuration for service developers
2. Deploy to production edge nodes
3. Monitor resource usage and performance impact
4. Collect feedback on remediation proposals

**Rollback Strategy:**
- Service can be unregistered/stopped without affecting monitored workloads
- No persistent state or data dependencies
- Monitored services continue operating normally without monitor

## Open Questions

1. **Model updates:** How to update LLM models on deployed edge nodes? (OTA updates, separate service?)
2. **Proposal storage:** Where to persist remediation proposals for review? (Local file, external API, ephemeral?)
3. **Multi-workload correlation:** Should monitor detect patterns across multiple workloads? (Future enhancement)
4. **Resource limits:** What are appropriate CPU/memory limits for different edge device classes?
5. **Monitoring the monitor:** How to detect if the monitoring service itself fails? (Health checks, watchdog?)
