## 1. Project Setup

- [ ] 1.1 Create project directory structure (src/, tests/, config/, docs/)
- [ ] 1.2 Initialize Python project with pyproject.toml and dependencies
- [ ] 1.3 Add Ollama Python client library
- [ ] 1.4 Add requests library for anax API calls
- [ ] 1.5 Add watchdog library for log file monitoring
- [ ] 1.6 Create .gitignore for Python project

## 2. Anax API Integration

- [ ] 2.1 Implement AnaxClient class with base HTTP client
- [ ] 2.2 Add get_node_status() method for /node endpoint
- [ ] 2.3 Add get_service_configs() method for /service/config endpoint
- [ ] 2.4 Add get_agreements() method for /agreement endpoint
- [ ] 2.5 Implement error handling and retry logic for API calls
- [ ] 2.6 Add unit tests for AnaxClient with mocked responses

## 3. Workload Discovery

- [ ] 3.1 Create WorkloadRegistry class to track discovered workloads
- [ ] 3.2 Implement periodic polling loop (60 second interval)
- [ ] 3.3 Add workload state tracking (registered, running, stopped)
- [ ] 3.4 Implement service metadata extraction from API responses
- [ ] 3.5 Add monitoring permission checking from service definitions
- [ ] 3.6 Implement agreement tracking for running workload detection
- [ ] 3.7 Add logging for workload state transitions
- [ ] 3.8 Write unit tests for WorkloadRegistry

## 4. Log Monitoring

- [ ] 4.1 Create LogMonitor class using watchdog library
- [ ] 4.2 Implement log file tailing with rotation detection
- [ ] 4.3 Add error pattern matching (case-insensitive)
- [ ] 4.4 Implement default error patterns (ERROR, FATAL, Exception, CRITICAL)
- [ ] 4.5 Add context window collection (50 lines before/after error)
- [ ] 4.6 Implement memory limits (100MB buffer) with overflow handling
- [ ] 4.7 Add file size limits (1GB max, tail recent 100MB)
- [ ] 4.8 Handle multiple log paths per workload concurrently
- [ ] 4.9 Write unit tests for LogMonitor with test log files

## 5. LLM Integration

- [ ] 5.1 Create OllamaClient class for LLM inference
- [ ] 5.2 Implement model initialization (Llama 3.2 3B quantized)
- [ ] 5.3 Add structured prompt template for error analysis
- [ ] 5.4 Implement JSON response parsing with validation
- [ ] 5.5 Add timeout handling (30 second limit)
- [ ] 5.6 Implement retry logic for malformed responses
- [ ] 5.7 Add workload context injection in prompts
- [ ] 5.8 Write unit tests for OllamaClient with mocked responses

## 6. Error Analysis

- [ ] 6.1 Create ErrorAnalyzer class to coordinate analysis
- [ ] 6.2 Implement async analysis queue with priority ordering
- [ ] 6.3 Add queue overflow handling (50 request limit)
- [ ] 6.4 Implement recurring error pattern detection
- [ ] 6.5 Add severity classification (low, medium, high, critical)
- [ ] 6.6 Implement confidence scoring (0.0-1.0)
- [ ] 6.7 Add analysis result caching to avoid duplicate work
- [ ] 6.8 Write unit tests for ErrorAnalyzer

## 7. Remediation Proposals

- [ ] 7.1 Create RemediationGenerator class
- [ ] 7.2 Implement structured JSON proposal format
- [ ] 7.3 Add step-by-step remediation generation
- [ ] 7.4 Implement workload-specific recommendation tailoring
- [ ] 7.5 Add proposal metadata (timestamp, workload ID, location)
- [ ] 7.6 Implement human review flag logic (confidence/severity based)
- [ ] 7.7 Add disclaimer to all proposals
- [ ] 7.8 Implement proposal persistence to /var/lib/monitor/proposals/
- [ ] 7.9 Add fallback logging to stdout on storage failure
- [ ] 7.10 Write unit tests for RemediationGenerator

## 8. Multi-arch Container

- [ ] 8.1 Create Dockerfile with multi-stage build
- [ ] 8.2 Add Ollama installation for x86_64 and arm64
- [ ] 8.3 Configure Python runtime and dependencies
- [ ] 8.4 Add health check endpoint
- [ ] 8.5 Set resource limits (CPU, memory)
- [ ] 8.6 Create docker-compose.yml for local testing
- [ ] 8.7 Build and test x86_64 image
- [ ] 8.8 Build and test arm64 image
- [ ] 8.9 Push multi-arch manifest to container registry

## 9. Open Horizon Integration

- [ ] 9.1 Create horizon/service.definition.json
- [ ] 9.2 Add monitoring permission schema to service definition
- [ ] 9.3 Create horizon/service.policy.json with constraints
- [ ] 9.4 Create horizon/deployment.policy.json
- [ ] 9.5 Add volume mount configuration for log access
- [ ] 9.6 Configure anax API endpoint (http://localhost:8510)
- [ ] 9.7 Set resource constraints in service definition
- [ ] 9.8 Create horizon/node.policy.json for testing

## 10. Configuration

- [ ] 10.1 Create config.yaml with default settings
- [ ] 10.2 Add polling interval configuration (default 60s)
- [ ] 10.3 Add LLM model configuration (model name, quantization)
- [ ] 10.4 Add resource limit configuration (memory, queue size)
- [ ] 10.5 Add logging configuration (level, format, output)
- [ ] 10.6 Implement environment variable overrides
- [ ] 10.7 Add configuration validation on startup

## 11. Main Service Orchestration

- [ ] 11.1 Create main.py entry point
- [ ] 11.2 Initialize all components (AnaxClient, WorkloadRegistry, etc.)
- [ ] 11.3 Start workload discovery polling loop
- [ ] 11.4 Start log monitoring for discovered workloads
- [ ] 11.5 Start error analysis processing loop
- [ ] 11.6 Add graceful shutdown handling (SIGTERM, SIGINT)
- [ ] 11.7 Implement health check HTTP endpoint
- [ ] 11.8 Add startup validation (anax API reachable, Ollama running)

## 12. Testing

- [ ] 12.1 Create test fixtures for anax API responses
- [ ] 12.2 Create sample log files with various error patterns
- [ ] 12.3 Write integration test for workload discovery flow
- [ ] 12.4 Write integration test for log monitoring flow
- [ ] 12.5 Write integration test for error analysis flow
- [ ] 12.6 Write integration test for remediation proposal generation
- [ ] 12.7 Add performance tests for resource usage
- [ ] 12.8 Test with sample Open Horizon workload

## 13. Documentation

- [ ] 13.1 Create README.md with project overview
- [ ] 13.2 Document monitoring permission configuration
- [ ] 13.3 Add deployment guide for Open Horizon
- [ ] 13.4 Document log file mount requirements
- [ ] 13.5 Add troubleshooting guide
- [ ] 13.6 Document LLM model selection and updates
- [ ] 13.7 Add example service definitions with monitoring config
- [ ] 13.8 Create architecture diagram

## 14. Deployment

- [ ] 14.1 Publish container images to registry
- [ ] 14.2 Publish Open Horizon service to exchange
- [ ] 14.3 Create deployment policies for test environment
- [ ] 14.4 Deploy to test edge node
- [ ] 14.5 Validate workload discovery on test node
- [ ] 14.6 Validate log monitoring with test workload
- [ ] 14.7 Validate error analysis and proposal generation
- [ ] 14.8 Monitor resource usage and performance
- [ ] 14.9 Collect feedback and iterate
