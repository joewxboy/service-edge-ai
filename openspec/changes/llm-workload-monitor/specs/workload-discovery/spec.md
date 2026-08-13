## ADDED Requirements

### Requirement: Periodic workload polling
The system SHALL poll the Open Horizon anax API every 60 seconds to discover registered and running workloads on the local node.

#### Scenario: Successful workload discovery
- **WHEN** the polling interval elapses
- **THEN** system queries anax API endpoints (/node, /service/config, /agreement) and updates internal workload registry

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

#### Scenario: Service with monitoring config
- **WHEN** service definition contains monitoring section
- **THEN** system extracts monitoring.enabled, monitoring.logPaths, and monitoring.errorPatterns fields

#### Scenario: Service without monitoring config
- **WHEN** service definition lacks monitoring section
- **THEN** system marks the service as not eligible for monitoring

### Requirement: Agreement tracking
The system SHALL track active agreements for each workload to determine if the workload is currently running.

#### Scenario: Active agreement exists
- **WHEN** workload has one or more active agreements in anax API response
- **THEN** system marks workload as running and eligible for monitoring (if permitted)

#### Scenario: No active agreements
- **WHEN** workload has no active agreements in anax API response
- **THEN** system marks workload as registered but not running, ineligible for monitoring
