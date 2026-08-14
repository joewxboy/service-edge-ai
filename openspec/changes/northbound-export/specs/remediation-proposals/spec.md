## MODIFIED Requirements

### Requirement: Proposal metadata
The system SHALL include metadata in each proposal: timestamp, workload identifier, error location, analysis duration, and — when export is configured — where the proposal was delivered.

#### Scenario: Complete metadata
- **WHEN** proposal is generated
- **THEN** metadata includes ISO 8601 timestamp, service name/version, log file path, line number, and analysis duration in milliseconds

#### Scenario: Problem lifetime
- **WHEN** a proposal describes a problem that has recurred
- **THEN** metadata includes first_seen, last_seen, total_occurrences, and analysis_count so an operator can judge duration and blast radius

#### Scenario: Export delivery recorded
- **WHEN** a proposal is delivered to a configured sink
- **THEN** the proposal records that sink, the delivery time, and the outcome

#### Scenario: External reference recorded
- **WHEN** a sink returns an external reference such as an issue URL
- **THEN** the proposal records it so an operator reading the local file can follow it

#### Scenario: Delivery pending
- **WHEN** a proposal is queued for export but not yet delivered
- **THEN** the proposal shows that sink as pending rather than delivered

#### Scenario: Delivery failed permanently
- **WHEN** a proposal's export is dropped or expires without delivery
- **THEN** the proposal records the failure and the reason

#### Scenario: Export not configured
- **WHEN** no export sink is configured
- **THEN** proposal metadata is unchanged from a non-exporting deployment
