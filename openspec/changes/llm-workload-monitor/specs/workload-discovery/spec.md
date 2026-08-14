## ADDED Requirements

### Requirement: Periodic workload polling
The system SHALL poll the Open Horizon anax API every 60 seconds to discover registered and running workloads on the local node.

#### Scenario: Successful workload discovery
- **WHEN** the polling interval elapses
- **THEN** system queries anax API endpoints (/node, /service, /service/config, /agreement) and updates internal workload registry

#### Scenario: Workload present only in agreements
- **WHEN** a workload has an active agreement but no /service/config entry
- **THEN** system still discovers the workload and marks it running

#### Scenario: Anax API unavailable
- **WHEN** anax API is unreachable during polling
- **THEN** system logs the error and retries on next polling interval without crashing

### Requirement: Workload state tracking
The system SHALL maintain an internal registry of discovered workloads with their current state (registered, running, stopped).

#### Scenario: New workload detected
- **WHEN** a workload appears in anax API response that was not previously tracked
- **THEN** system adds the workload to internal registry with timestamp and initial state

#### Scenario: Workload state change
- **WHEN** a tracked workload's state changes in anax API response
- **THEN** system updates the workload's state in internal registry and logs the transition

#### Scenario: Workload removal
- **WHEN** a previously tracked workload no longer appears in anax API response
- **THEN** system marks the workload as removed in internal registry

### Requirement: Service metadata extraction
The system SHALL extract service definition metadata from anax API responses including service name, version, organization, and monitoring configuration.

The monitoring configuration SHALL be read from `MONITORING_*` environment variables declared in the service's deployment string, available at `GET /service` under `definitions.active[].deployment.services.<name>.environment`. A custom top-level field cannot be used because the Open Horizon exchange discards unknown service definition fields before they reach the node.

#### Scenario: Service with monitoring variables
- **WHEN** a service's deployment environment contains MONITORING_ENABLED, MONITORING_LOG_PATHS, and MONITORING_ERROR_PATTERNS
- **THEN** system extracts the enabled flag, log path list, and error pattern list from those variables

#### Scenario: Service without monitoring variables
- **WHEN** a service's deployment environment contains no MONITORING_ENABLED variable
- **THEN** system marks the service as not eligible for monitoring

#### Scenario: Service declaring context paths
- **WHEN** a service's deployment environment contains MONITORING_CONTEXT_PATHS
- **THEN** system extracts the list and associates it with the workload for prompt injection

#### Scenario: Multiple containers in one deployment
- **WHEN** a service's deployment defines several containers
- **THEN** system reads MONITORING_* variables from all of them, combining declared log paths

### Requirement: Monitoring variable parsing
The system SHALL parse `MONITORING_LOG_PATHS`, `MONITORING_ERROR_PATTERNS`, and `MONITORING_CONTEXT_PATHS` as comma-separated lists, accepting a JSON array when the value begins with `[`.

#### Scenario: Comma-separated list
- **WHEN** MONITORING_LOG_PATHS is "/var/log/a.log,/var/log/b.log"
- **THEN** system monitors both paths

#### Scenario: JSON array for values containing commas
- **WHEN** MONITORING_ERROR_PATTERNS begins with "[" and contains a JSON array
- **THEN** system parses it as JSON so patterns containing commas are preserved

#### Scenario: Boolean interpretation
- **WHEN** MONITORING_ENABLED is any of "true", "1", or "yes" (case-insensitive)
- **THEN** system treats monitoring as enabled, and treats any other value as disabled

#### Scenario: Malformed value
- **WHEN** a MONITORING_* variable cannot be parsed
- **THEN** system logs a warning and treats the workload as not eligible for monitoring

### Requirement: Agreement tracking
The system SHALL track active agreements for each workload to determine if the workload is currently running.

#### Scenario: Active agreement exists
- **WHEN** workload has one or more active agreements in anax API response
- **THEN** system marks workload as running and eligible for monitoring (if permitted)

#### Scenario: No active agreements
- **WHEN** workload has no active agreements in anax API response
- **THEN** system marks workload as registered but not running, ineligible for monitoring
