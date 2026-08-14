## ADDED Requirements

### Requirement: Optional export
The system SHALL forward errors and proposals to external systems only when at least one sink is configured, and SHALL behave identically to a non-exporting deployment when none is.

#### Scenario: No sinks configured
- **WHEN** the service starts with no export sinks configured
- **THEN** no outbound connections are attempted and no export state is recorded

#### Scenario: Export explicitly disabled
- **WHEN** export is disabled while sinks remain configured
- **THEN** system skips all forwarding and logs that export is disabled

#### Scenario: Sink configuration invalid
- **WHEN** a sink is configured with missing or malformed settings
- **THEN** system reports the specific problem at startup and continues running with that sink disabled

### Requirement: Monitoring is never blocked by export
The system SHALL isolate export from the monitoring pipeline so that no sink failure, slowness, or outage affects error detection, analysis, or local proposal storage.

#### Scenario: Sink unreachable
- **WHEN** a configured sink cannot be reached
- **THEN** log monitoring, analysis, and local proposal writing continue unaffected

#### Scenario: Sink responds slowly
- **WHEN** a sink exceeds its request timeout
- **THEN** system abandons that attempt, schedules a retry, and does not delay any other component

#### Scenario: Sink returns an error
- **WHEN** a sink rejects a record
- **THEN** system records the failure against that record and continues processing others

### Requirement: Store-and-forward delivery
The system SHALL persist records destined for export before attempting delivery, and SHALL retry undelivered records until they succeed, expire, or are shed.

#### Scenario: Delivery while connected
- **WHEN** a record is queued and the sink is reachable
- **THEN** system delivers it and removes it from the queue

#### Scenario: Node offline
- **WHEN** the node has no network connectivity
- **THEN** system retains queued records and continues accepting new ones

#### Scenario: Connectivity restored
- **WHEN** connectivity returns after an outage
- **THEN** system delivers the queued records and reports how many were pending

#### Scenario: Service restarted with records pending
- **WHEN** the service restarts while records are undelivered
- **THEN** those records survive the restart and are delivered afterwards

#### Scenario: Repeated delivery failure
- **WHEN** a record fails delivery repeatedly
- **THEN** system retries with increasing delay rather than at a fixed interval

### Requirement: Bounded queue
The system SHALL bound the export queue by both size and age, and SHALL shed records rather than exhaust node storage.

#### Scenario: Queue size limit reached
- **WHEN** the queue reaches its configured size limit
- **THEN** system drops the oldest records first, counts the drops, and logs a warning

#### Scenario: Record older than the retention limit
- **WHEN** a queued record exceeds the configured maximum age
- **THEN** system discards it and counts it as expired

#### Scenario: Queue state is observable
- **WHEN** an operator queries service health
- **THEN** system reports queue depth, delivered count, failed count, and dropped count

### Requirement: Redaction levels
The system SHALL support per-sink redaction levels controlling what content leaves the node, and SHALL default to the least revealing level.

#### Scenario: Default level
- **WHEN** a sink is configured without an explicit redaction level
- **THEN** system transmits only problem metadata: identifiers, service identity, severity, counts, and timestamps

#### Scenario: Analysis level
- **WHEN** a sink is configured for analysis-level redaction
- **THEN** transmitted records also include the error summary, root cause, and remediation steps

#### Scenario: Full level
- **WHEN** a sink is configured for full redaction level
- **THEN** transmitted records also include the log context window

#### Scenario: Levels are independent per sink
- **WHEN** two sinks are configured with different redaction levels
- **THEN** each receives only the content its own level permits

### Requirement: Credential handling
The system SHALL accept sink credentials without exposing them in service definitions, logs, proposals, or exported payloads.

#### Scenario: Credentials supplied
- **WHEN** a sink requires authentication
- **THEN** system reads the credential from an environment variable or file rather than from the service definition

#### Scenario: Sink error mentions a credential
- **WHEN** a sink returns an error containing credential material
- **THEN** system scrubs it before logging

#### Scenario: Credential missing
- **WHEN** a sink requires a credential that is not supplied
- **THEN** system disables that sink at startup and reports which credential is missing

### Requirement: Event sink protocols
The system SHALL support forwarding records over protocols that established observability platforms consume.

#### Scenario: OTLP endpoint
- **WHEN** an OTLP sink is configured
- **THEN** system emits records to it in OpenTelemetry format

#### Scenario: Webhook endpoint
- **WHEN** a webhook sink is configured
- **THEN** system POSTs records as JSON to the configured URL

#### Scenario: Syslog destination
- **WHEN** a syslog sink is configured
- **THEN** system emits records as RFC 5424 messages with severity mapped from the proposal

#### Scenario: MQTT broker
- **WHEN** an MQTT sink is configured
- **THEN** system publishes records to the configured topic

### Requirement: Idempotency
The system SHALL include a stable identifier with each exported record so receiving systems can recognise duplicates arising from retries.

#### Scenario: Retry after ambiguous failure
- **WHEN** a record is retried after a failure that may have partially succeeded
- **THEN** the retry carries the same identifier as the original attempt

#### Scenario: Distinct problems
- **WHEN** two different problems are exported
- **THEN** their records carry different identifiers
