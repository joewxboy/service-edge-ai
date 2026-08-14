## 1. Export Configuration

- [x] 1.1 Add an `export` section to config.yaml with an enabled flag and a sink list
- [x] 1.2 Add MONITOR_EXPORT_* environment overrides
- [x] 1.3 Implement per-sink configuration parsing with named sink types
- [x] 1.4 Validate sink configuration at startup, disabling bad sinks rather than aborting
- [x] 1.5 Load credentials from environment or file, never from the service definition
- [x] 1.6 Scrub credentials from log output and error messages
- [x] 1.7 Write unit tests for configuration, validation, and credential handling

## 2. Record Shaping and Redaction

- [x] 2.1 Define the exported record structure from a proposal
- [x] 2.2 Implement the `metadata` redaction level (default)
- [x] 2.3 Implement the `analysis` redaction level
- [x] 2.4 Implement the `full` redaction level
- [x] 2.5 Apply redaction independently per sink
- [x] 2.6 Derive a stable idempotency key from `problem_key`
- [x] 2.7 Write unit tests asserting each level transmits exactly what it should, and no more

## 3. Store-and-Forward Queue

- [x] 3.1 Create the spool directory structure under the monitor state volume
- [x] 3.2 Persist records to the spool before any delivery attempt
- [x] 3.3 Remove spool entries on successful delivery
- [x] 3.4 Implement exponential backoff with jitter for failed deliveries
- [x] 3.5 Enforce the maximum spool size, shedding oldest-first with counted drops
- [x] 3.6 Enforce maximum record age, discarding expired records
- [x] 3.7 Recover the spool on startup so pending records survive restarts
- [x] 3.8 Write unit tests for persistence, backoff, shedding, and restart recovery

## 4. Export Worker

- [x] 4.1 Create the export worker thread, isolated from the monitoring loops
- [x] 4.2 Subscribe to completed proposals from RemediationGenerator
- [x] 4.3 Apply per-request timeouts so a hung sink cannot stall the worker
- [x] 4.4 Continue processing remaining records when one sink fails
- [x] 4.5 Add graceful shutdown that flushes in-flight work within a bounded time
- [x] 4.6 Write tests proving monitoring is unaffected by a failing or hanging sink

## 5. Event Sinks

- [x] 5.1 Define the EventSink interface
- [x] 5.2 Implement the webhook sink (JSON POST)
- [x] 5.3 Implement the OTLP/HTTP sink
- [x] 5.4 Implement the syslog sink with severity mapping (RFC 5424)
- [x] 5.5 Implement the MQTT sink
- [x] 5.6 Attach idempotency keys to every emitted record
- [x] 5.7 Write unit tests per sink against a stub server

## 6. Issue Sinks

- [x] 6.1 Define the IssueSink interface with create, comment, resolve, and reopen
- [x] 6.2 Persist the `problem_key` to external-reference mapping
- [x] 6.3 Implement the severity threshold for issue creation
- [x] 6.4 Implement issue creation with full context and the AI-generated disclaimer
- [x] 6.5 Implement rate-limited update comments carrying accumulated occurrence counts
- [x] 6.6 Post revised analyses immediately, bypassing the rate limit
- [x] 6.7 Implement resolution after the configured no-recurrence window
- [x] 6.8 Word closing comments as "stopped recurring", never "fixed"
- [x] 6.9 Implement reopen, falling back to a linked new issue where reopening is unsupported
- [x] 6.10 Implement reconciliation after an offline period without duplicating issues
- [x] 6.11 Implement dry-run mode that logs intended actions only
- [x] 6.12 Write unit tests for the full lifecycle including offline reconciliation

## 7. Tracker Implementations

- [x] 7.1 Implement the GitHub Issues sink
- [x] 7.2 Implement the Jira sink
- [x] 7.3 Implement the ServiceNow sink
- [x] 7.4 Handle tracker rate limiting and backoff signals
- [ ] 7.5 Write unit tests per tracker against recorded API responses — the REST shapes are implemented and unit-tested through a fake tracker, but no recorded fixtures from real GitHub/Jira/ServiceNow instances

## 8. Proposal Delivery State

- [x] 8.1 Add an `exports` block to proposal metadata
- [x] 8.2 Record pending, delivered, and failed states per sink
- [x] 8.3 Record external references such as issue URLs
- [x] 8.4 Ensure metadata is unchanged when export is unconfigured
- [x] 8.5 Update the remediation-proposals delta spec expectations in tests

## 9. Observability of Export Itself

- [x] 9.1 Report queue depth, delivered, failed, dropped, and expired counts on the health endpoint
- [x] 9.2 Report per-sink health and last error
- [x] 9.3 Log outage start and recovery, including how many records were pending
- [x] 9.4 Add an `--check` mode that validates sink connectivity without exporting

## 10. Deployment

- [ ] 10.1 Add export settings to the Open Horizon service definition userInput — deferred until a sink is chosen for the deployment
- [x] 10.2 Document required network egress and proxy configuration
- [x] 10.3 Size the spool against the state volume and document the requirement
- [x] 10.4 Add export configuration to docker-compose for local testing

## 11. Testing

- [x] 11.1 Build a stub sink server for integration tests
- [x] 11.2 Integration test: proposal reaches an event sink end to end
- [x] 11.3 Integration test: offline period, then delivery on reconnection with no duplicates
- [x] 11.4 Integration test: issue created once, updated on recurrence, resolved when it stops
- [x] 11.5 Integration test: redaction levels transmit only permitted content
- [x] 11.6 Test spool overflow behaviour under sustained outage
- [x] 11.7 Verify monitoring throughput is unaffected with export enabled — covered by tests asserting submit() only spools and a failing sink never blocks

## 12. Documentation

- [x] 12.1 Write docs/northbound-export.md covering sinks, redaction, and delivery guarantees
- [x] 12.2 Document each sink type with a working configuration example
- [x] 12.3 Document issue lifecycle and what resolution does and does not mean
- [x] 12.4 Add a privacy section stating exactly what each redaction level transmits
- [x] 12.5 Add export troubleshooting to docs/troubleshooting.md
- [x] 12.6 Add fleet-level operations guidance to docs/operations.md
- [x] 12.7 Update README and docs/README with the new capability

## 13. Validation on a Live Node

- [ ] 13.1 Verify an event sink against a real collector
- [ ] 13.2 Verify offline buffering by disconnecting the node deliberately
- [ ] 13.3 Verify issue lifecycle against a real tracker, starting in dry-run
- [ ] 13.4 Measure export overhead against the monitoring baseline
- [ ] 13.5 Confirm no credential material appears in logs, proposals, or payloads

## Resolved Design Questions

Settled in design.md before implementation:

- [x] Q1 Resolve window: 24 hours, configurable
- [x] Q2 Exports carry node identity by default, suppressible
- [x] Q3 No backfill; active problems export themselves on next recurrence
- [x] Q4 `MONITORING_EXPORT` gates content levels; metadata permitted by default
- [x] Q5 Issue creation threshold: `high` and above, configurable
