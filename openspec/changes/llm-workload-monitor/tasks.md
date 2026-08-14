## 1. Project Setup

- [x] 1.1 Create project directory structure (src/, tests/, config/, docs/)
- [x] 1.2 Initialize Python project with pyproject.toml and dependencies
- [x] 1.3 Add Ollama Python client library
- [x] 1.4 Add requests library for anax API calls
- [x] 1.5 Add watchdog library for log file monitoring
- [x] 1.6 Create .gitignore for Python project

## 2. Anax API Integration

- [x] 2.1 Implement AnaxClient class with base HTTP client
- [x] 2.2 Add get_node_status() method for /node endpoint
- [x] 2.3 Add get_service_configs() method for /service/config endpoint
- [x] 2.4 Add get_agreements() method for /agreement endpoint
- [x] 2.5 Implement error handling and retry logic for API calls
- [x] 2.6 Add unit tests for AnaxClient with mocked responses

## 3. Workload Discovery

- [x] 3.1 Create WorkloadRegistry class to track discovered workloads
- [x] 3.2 Implement periodic polling loop (60 second interval)
- [x] 3.3 Add workload state tracking (registered, running, stopped)
- [x] 3.4 Implement service metadata extraction from API responses
- [x] 3.5 Add monitoring permission checking from service definitions
- [x] 3.6 Implement agreement tracking for running workload detection
- [x] 3.7 Add logging for workload state transitions
- [x] 3.8 Write unit tests for WorkloadRegistry

## 4. Log Monitoring

- [x] 4.1 Create LogMonitor class using watchdog library
- [x] 4.2 Implement log file tailing with rotation detection
- [x] 4.3 Add error pattern matching (case-insensitive)
- [x] 4.4 Implement default error patterns (ERROR, FATAL, Exception, CRITICAL)
- [x] 4.5 Add context window collection (50 lines before/after error)
- [x] 4.6 Implement memory limits (100MB buffer) with overflow handling
- [x] 4.7 Add file size limits (1GB max, tail recent 100MB)
- [x] 4.8 Handle multiple log paths per workload concurrently
- [x] 4.9 Write unit tests for LogMonitor with test log files

## 5. LLM Integration

- [x] 5.1 Create OllamaClient class for LLM inference
- [x] 5.2 Implement model initialization (Llama 3.2 3B quantized)
- [x] 5.3 Add structured prompt template for error analysis
- [x] 5.4 Implement JSON response parsing with validation
- [x] 5.5 Add timeout handling (30 second limit)
- [x] 5.6 Implement retry logic for malformed responses
- [x] 5.7 Add workload context injection in prompts
- [x] 5.8 Write unit tests for OllamaClient with mocked responses

## 6. Error Analysis

- [x] 6.1 Create ErrorAnalyzer class to coordinate analysis
- [x] 6.2 Implement async analysis queue with priority ordering
- [x] 6.3 Add queue overflow handling (50 request limit)
- [x] 6.4 Implement recurring error pattern detection
- [x] 6.5 Add severity classification (low, medium, high, critical)
- [x] 6.6 Implement confidence scoring (0.0-1.0)
- [x] 6.7 Add analysis result caching to avoid duplicate work
- [x] 6.8 Write unit tests for ErrorAnalyzer

## 7. Remediation Proposals

- [x] 7.1 Create RemediationGenerator class
- [x] 7.2 Implement structured JSON proposal format
- [x] 7.3 Add step-by-step remediation generation
- [x] 7.4 Implement workload-specific recommendation tailoring
- [x] 7.5 Add proposal metadata (timestamp, workload ID, location)
- [x] 7.6 Implement human review flag logic (confidence/severity based)
- [x] 7.7 Add disclaimer to all proposals
- [x] 7.8 Implement proposal persistence to /var/lib/monitor/proposals/
- [x] 7.9 Add fallback logging to stdout on storage failure
- [x] 7.10 Write unit tests for RemediationGenerator

## 8. Multi-arch Container

- [x] 8.1 Create Dockerfile with multi-stage build
- [x] 8.2 Add Ollama installation for x86_64 and arm64
- [x] 8.3 Configure Python runtime and dependencies
- [x] 8.4 Add health check endpoint
- [x] 8.5 Set resource limits (CPU, memory)
- [x] 8.6 Create docker-compose.yml for local testing
- [x] 8.7 Build and test x86_64 image — built and smoke-tested; exposed a bad `host.docker.internal` default (anax binds loopback only)
- [x] 8.8 Build and test arm64 image — built under QEMU emulation via buildx
- [x] 8.9 Push multi-arch manifest to container registry — `joewxboy/edge-ai:0.0.1` + `:latest`, manifest carries linux/amd64 + linux/arm64

## 9. Open Horizon Integration

- [x] 9.1 Create horizon/service.definition.json
- [x] 9.2 Add monitoring permission schema to service definition
- [x] 9.3 Create horizon/service.policy.json with constraints
- [x] 9.4 Create horizon/deployment.policy.json
- [x] 9.5 Add volume mount configuration for log access
- [x] 9.6 Configure anax API endpoint (http://localhost:8510)
- [x] 9.7 Set resource constraints in service definition
- [x] 9.8 Create horizon/node.policy.json for testing

## 10. Configuration

- [x] 10.1 Create config.yaml with default settings
- [x] 10.2 Add polling interval configuration (default 60s)
- [x] 10.3 Add LLM model configuration (model name, quantization)
- [x] 10.4 Add resource limit configuration (memory, queue size)
- [x] 10.5 Add logging configuration (level, format, output)
- [x] 10.6 Implement environment variable overrides
- [x] 10.7 Add configuration validation on startup

## 11. Main Service Orchestration

- [x] 11.1 Create main.py entry point
- [x] 11.2 Initialize all components (AnaxClient, WorkloadRegistry, etc.)
- [x] 11.3 Start workload discovery polling loop
- [x] 11.4 Start log monitoring for discovered workloads
- [x] 11.5 Start error analysis processing loop
- [x] 11.6 Add graceful shutdown handling (SIGTERM, SIGINT)
- [x] 11.7 Implement health check HTTP endpoint
- [x] 11.8 Add startup validation (anax API reachable, Ollama running)

## 12. Testing

- [x] 12.1 Create test fixtures for anax API responses
- [x] 12.2 Create sample log files with various error patterns
- [x] 12.3 Write integration test for workload discovery flow
- [x] 12.4 Write integration test for log monitoring flow
- [x] 12.5 Write integration test for error analysis flow
- [x] 12.6 Write integration test for remediation proposal generation
- [x] 12.7 Add performance tests for resource usage
- [x] 12.8 Test with sample Open Horizon workload — verified against the live anax agent (discovered running `IBM/ibm.helloworld`, which exposed the agreement-only discovery bug) and a full pipeline run against the live Ollama runtime

## 15. Prompt Steering with Domain Knowledge

Added after deployment: unguided analyses were generically correct but
deployment-agnostic. See design.md Decision 6a.

- [x] 15.1 Create ContextLibrary for loading node-wide and per-workload knowledge
- [x] 15.2 Add MONITORING_CONTEXT_PATHS to the workload opt-in variables
- [x] 15.3 Inject a "Service knowledge" section into the analysis prompt
- [x] 15.4 Implement per-file and per-prompt byte budgets with truncation
- [x] 15.5 Prefer workload-specific context over node-wide under budget pressure
- [x] 15.6 Re-read context files on change without requiring a restart
- [x] 15.7 Add context configuration section and MONITOR_CONTEXT_* overrides
- [x] 15.8 Report loaded context on the health endpoint and at startup
- [x] 15.9 Write unit tests for ContextLibrary and prompt integration
- [x] 15.10 Verify against the live model: measured A/B showing correct root cause with context

## 13. Documentation

- [x] 13.1 Create README.md with project overview
- [x] 13.2 Document monitoring permission configuration
- [x] 13.3 Add deployment guide for Open Horizon
- [x] 13.4 Document log file mount requirements
- [x] 13.5 Add troubleshooting guide
- [x] 13.6 Document LLM model selection and updates
- [x] 13.7 Add example service definitions with monitoring config
- [x] 13.8 Create architecture diagram
- [x] 13.9 Document model selection and configuration tuning (docs/tuning.md)
- [x] 13.10 Document prompt steering with SKILL.md/runbooks/wiki (docs/context-and-steering.md)
- [x] 13.11 Write the service developer guide (docs/service-developer-guide.md)

## 14. Deployment

> Deployed to the live `myorg/edge-llm-server` node with user authorization.
> The full pipeline was verified on-node: a sample workload's errors were
> detected, analysed by the local LLM, and written as a proposal.

- [x] 14.1 Publish container images to registry — `joewxboy/edge-ai:0.0.1` (multi-arch) on Docker Hub
- [x] 14.2 Publish Open Horizon service to exchange — `myorg/service-edge-ai_0.0.1_amd64`, image pinned to sha256 digest
- [x] 14.3 Create deployment policies for test environment — service policy + `myorg/policy-service-edge-ai_0.0.1` published
- [x] 14.4 Deploy to test edge node — agreement formed on `edge-llm-server`; required setting `openhorizon.allowPrivileged=true` (host networking)
- [x] 14.5 Validate workload discovery on test node — deployed monitor discovers 2 running workloads via anax
- [x] 14.6 Validate log monitoring with test workload — `myorg/sample-monitored-workload` opts in via `MONITORING_*` deployment variables; monitor reports `workloads_monitored: 1`, `log_paths_watched: 1`
- [x] 14.7 Validate error analysis and proposal generation — on-node run produced `analyses_completed: 1`, `proposals_written: 1`; proposal verified in the state volume
- [x] 14.8 Monitor resource usage and performance — measured `llama3.2:3b-instruct-q4_K_M` at ~55s per analysis on this 8-core CPU node; deployment policy raised `MONITOR_LLM_TIMEOUT` to 120s
- [ ] 14.9 Collect feedback and iterate — requires operators using the deployment

---

## Constraints discovered during live deployment

All four were found by deploying to a real node and hub, and are now reflected
in `design.md`, the specs, and the docs.

**1. A custom `monitoring` service definition section does not survive
publishing.** The Open Horizon exchange stores only its known service schema
fields and silently discards unknown ones, so the section never reaches the
node. `/service/config` is not a fallback either — it is empty on a
policy-registered node even when the deployment policy supplies `userInput`.

*Resolution:* the opt-in moved to `MONITORING_*` environment variables inside
the deployment string, which is stored verbatim and signed. Read on-node via
`GET /service` → `definitions.active[].deployment.services.<name>.environment`.
See `horizon/monitoring-variables.md`.

**2. The monitor requires host networking, which requires privilege.** anax
binds `127.0.0.1:8510` only, so no bridge-networked container can reach it. Host
networking makes Open Horizon require `openhorizon.allowPrivileged=true` on the
node.

**3. A workload writing to a root-owned host directory needs
`privileged: true`.** An `:rw` bind alone is refused with *"Write permission for
bind to ... is denied"* and the workload crash-loops. A shared **named volume**
is the non-privileged alternative.

**4. The 30s analysis timeout is optimistic for CPU-only nodes.** Measured on
this 8-core x86_64 node: `llama3.2:3b-instruct-q4_K_M` takes ~55s per analysis,
`qwen2.5-coder:7b` ~136s. The deployment policy now sets
`MONITOR_LLM_TIMEOUT=120`.

## Bugs found by testing against live infrastructure

1. **Discovery missed running workloads** — built only from `/service/config`,
   which is empty on a policy-registered node. Now unions `/agreement`,
   `/service`, and `/service/config`.
2. **`host.docker.internal` default was unusable** — anax binds loopback only.
3. **Invalid deployment fields** — `memory`/`cpu_shares` are not Open Horizon
   fields; the correct names are `max_memory_mb`/`max_cpus`.
4. **Model pull inherited the 30s inference timeout** — a 2GB pull could never
   finish. Separate `pull_timeout` added.
5. **`ollama` package returns `ChatResponse`, not a dict** — every analysis
   failed with "empty response" on the node while unit tests passed against dict
   fixtures. Both access styles now handled.
6. **anax reports `name` as the full workload id** — corrupted `service_name` in
   proposals. The name is now derived from the service url.
