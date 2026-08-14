## 1. Export Configuration

- [ ] 1.1 Add an `export` section to config.yaml with an enabled flag and a sink list
- [ ] 1.2 Add MONITOR_EXPORT_* environment overrides
- [ ] 1.3 Implement per-sink configuration parsing with named sink types
- [ ] 1.4 Validate sink configuration at startup, disabling bad sinks rather than aborting
- [ ] 1.5 Load credentials from environment or file, never from the service definition
- [ ] 1.6 Scrub credentials from log output and error messages
- [ ] 1.7 Write unit tests for configuration, validation, and credential handling

## 2. Record Shaping and Redaction

- [ ] 2.1 Define the exported record structure from a proposal
- [ ] 2.2 Implement the `metadata` redaction level (default)
- [ ] 2.3 Implement the `analysis` redaction level
- [ ] 2.4 Implement the `full` redaction level
- [ ] 2.5 Apply redaction independently per sink
- [ ] 2.6 Derive a stable idempotency key from `problem_key`
- [ ] 2.7 Write unit tests asserting each level transmits exactly what it should, and no more

## 3. Store-and-Forward Queue

- [ ] 3.1 Create the spool directory structure under the monitor state volume
- [ ] 3.2 Persist records to the spool before any delivery attempt
- [ ] 3.3 Remove spool entries on successful delivery
- [ ] 3.4 Implement exponential backoff with jitter for failed deliveries
- [ ] 3.5 Enforce the maximum spool size, shedding oldest-first with counted drops
- [ ] 3.6 Enforce maximum record age, discarding expired records
- [ ] 3.7 Recover the spool on startup so pending records survive restarts
- [ ] 3.8 Write unit tests for persistence, backoff, shedding, and restart recovery

## 4. Export Worker

- [ ] 4.1 Create the export worker thread, isolated from the monitoring loops
- [ ] 4.2 Subscribe to completed proposals from RemediationGenerator
- [ ] 4.3 Apply per-request timeouts so a hung sink cannot stall the worker
- [ ] 4.4 Continue processing remaining records when one sink fails
- [ ] 4.5 Add graceful shutdown that flushes in-flight work within a bounded time
- [ ] 4.6 Write tests proving monitoring is unaffected by a failing or hanging sink

## 5. Event Sinks

- [ ] 5.1 Define the EventSink interface
- [ ] 5.2 Implement the webhook sink (JSON POST)
- [ ] 5.3 Implement the OTLP/HTTP sink
- [ ] 5.4 Implement the syslog sink with severity mapping (RFC 5424)
- [ ] 5.5 Implement the MQTT sink
- [ ] 5.6 Attach idempotency keys to every emitted record
- [ ] 5.7 Write unit tests per sink against a stub server

## 6. Issue Sinks

- [ ] 6.1 Define the IssueSink interface with create, comment, resolve, and reopen
- [ ] 6.2 Persist the `problem_key` to external-reference mapping
- [ ] 6.3 Implement the severity threshold for issue creation
- [ ] 6.4 Implement issue creation with full context and the AI-generated disclaimer
- [ ] 6.5 Implement rate-limited update comments carrying accumulated occurrence counts
- [ ] 6.6 Post revised analyses immediately, bypassing the rate limit
- [ ] 6.7 Implement resolution after the configured no-recurrence window
- [ ] 6.8 Word closing comments as "stopped recurring", never "fixed"
- [ ] 6.9 Implement reopen, falling back to a linked new issue where reopening is unsupported
- [ ] 6.10 Implement reconciliation after an offline period without duplicating issues
- [ ] 6.11 Implement dry-run mode that logs intended actions only
- [ ] 6.12 Write unit tests for the full lifecycle including offline reconciliation

## 7. Tracker Implementations

- [ ] 7.1 Implement the GitHub Issues sink
- [ ] 7.2 Implement the Jira sink
- [ ] 7.3 Implement the ServiceNow sink
- [ ] 7.4 Handle tracker rate limiting and backoff signals
- [ ] 7.5 Write unit tests per tracker against recorded API responses

## 8. Proposal Delivery State

- [ ] 8.1 Add an `exports` block to proposal metadata
- [ ] 8.2 Record pending, delivered, and failed states per sink
- [ ] 8.3 Record external references such as issue URLs
- [ ] 8.4 Ensure metadata is unchanged when export is unconfigured
- [ ] 8.5 Update the remediation-proposals delta spec expectations in tests

## 9. Observability of Export Itself

- [ ] 9.1 Report queue depth, delivered, failed, dropped, and expired counts on the health endpoint
- [ ] 9.2 Report per-sink health and last error
- [ ] 9.3 Log outage start and recovery, including how many records were pending
- [ ] 9.4 Add an `--check` mode that validates sink connectivity without exporting

## 10. Deployment

- [ ] 10.1 Add export settings to the Open Horizon service definition userInput
- [ ] 10.2 Document required network egress and proxy configuration
- [ ] 10.3 Size the spool against the state volume and document the requirement
- [ ] 10.4 Add export configuration to docker-compose for local testing

## 11. Testing

- [ ] 11.1 Build a stub sink server for integration tests
- [ ] 11.2 Integration test: proposal reaches an event sink end to end
- [ ] 11.3 Integration test: offline period, then delivery on reconnection with no duplicates
- [ ] 11.4 Integration test: issue created once, updated on recurrence, resolved when it stops
- [ ] 11.5 Integration test: redaction levels transmit only permitted content
- [ ] 11.6 Test spool overflow behaviour under sustained outage
- [ ] 11.7 Verify monitoring throughput is unaffected with export enabled

## 12. Documentation

- [ ] 12.1 Write docs/northbound-export.md covering sinks, redaction, and delivery guarantees
- [ ] 12.2 Document each sink type with a working configuration example
- [ ] 12.3 Document issue lifecycle and what resolution does and does not mean
- [ ] 12.4 Add a privacy section stating exactly what each redaction level transmits
- [ ] 12.5 Add export troubleshooting to docs/troubleshooting.md
- [ ] 12.6 Add fleet-level operations guidance to docs/operations.md
- [ ] 12.7 Update README and docs/README with the new capability

## 13. Validation on a Live Node

- [ ] 13.1 Verify an event sink against a real collector
- [ ] 13.2 Verify offline buffering by disconnecting the node deliberately
- [ ] 13.3 Verify issue lifecycle against a real tracker, starting in dry-run
- [ ] 13.4 Measure export overhead against the monitoring baseline
- [ ] 13.5 Confirm no credential material appears in logs, proposals, or payloads

## Open Questions to Resolve Before Implementing

These are recorded in design.md and should be settled early, as several change
the shape of the work:

- [ ] Q1 Default resolve window before an issue is closed
- [ ] Q2 Whether exports carry a node identifier by default
- [ ] Q3 Whether existing local proposals are backfilled when export is first enabled
- [ ] Q4 Whether workloads can refuse export while permitting local monitoring
- [ ] Q5 Default severity threshold for issue creation
