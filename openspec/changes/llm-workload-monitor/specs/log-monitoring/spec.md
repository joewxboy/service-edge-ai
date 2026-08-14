## ADDED Requirements

### Requirement: Permission-based monitoring
The system SHALL only monitor workloads that have explicitly enabled monitoring via the `MONITORING_ENABLED` environment variable in their service definition's deployment string.

#### Scenario: Monitoring enabled
- **WHEN** workload deployment environment contains MONITORING_ENABLED=true
- **THEN** system proceeds with log monitoring for that workload

#### Scenario: Monitoring disabled
- **WHEN** workload deployment environment sets MONITORING_ENABLED to a false value or omits it entirely
- **THEN** system skips log monitoring for that workload and logs the decision

#### Scenario: Enabled without log paths
- **WHEN** MONITORING_ENABLED=true but MONITORING_LOG_PATHS is absent or empty
- **THEN** system logs a warning and does not monitor the workload

### Requirement: Log file access
The system SHALL access log files listed in the workload's `MONITORING_LOG_PATHS` variable via shared volume mounts.

#### Scenario: Valid log path
- **WHEN** MONITORING_LOG_PATHS specifies a file path that exists and is readable
- **THEN** system opens the file for tailing and monitoring

#### Scenario: Invalid log path
- **WHEN** MONITORING_LOG_PATHS specifies a file path that does not exist or is not readable
- **THEN** system logs a warning and continues monitoring other valid paths

#### Scenario: Multiple log paths
- **WHEN** MONITORING_LOG_PATHS contains multiple file paths
- **THEN** system monitors all valid paths concurrently

### Requirement: Log tailing
The system SHALL continuously tail monitored log files to detect new entries in real-time.

#### Scenario: New log entry
- **WHEN** a new line is appended to a monitored log file
- **THEN** system reads the new line and processes it for error detection

#### Scenario: Log rotation
- **WHEN** a monitored log file is rotated (renamed/truncated)
- **THEN** system detects the rotation and reopens the new log file without losing entries

### Requirement: Error pattern matching
The system SHALL match new log entries against error patterns specified in the workload's `MONITORING_ERROR_PATTERNS` variable.

#### Scenario: Pattern match
- **WHEN** a log entry contains text matching any error pattern (case-insensitive)
- **THEN** system marks the entry as an error and triggers error analysis

#### Scenario: No pattern match
- **WHEN** a log entry does not match any error pattern
- **THEN** system continues monitoring without triggering analysis

#### Scenario: Default patterns
- **WHEN** MONITORING_ERROR_PATTERNS is not specified or empty
- **THEN** system uses default patterns: ["ERROR", "FATAL", "Exception", "CRITICAL"]

### Requirement: Context window collection
The system SHALL collect a context window of log entries surrounding detected errors for analysis.

#### Scenario: Error with context
- **WHEN** an error is detected in a log entry
- **THEN** system collects 50 lines before and 50 lines after the error entry

#### Scenario: Error near file boundary
- **WHEN** an error is detected within 50 lines of file start or end
- **THEN** system collects all available lines up to the boundary

### Requirement: Resource limits
The system SHALL limit log monitoring resource usage to prevent impact on monitored workloads.

#### Scenario: Memory limit
- **WHEN** log monitoring buffer exceeds 100MB
- **THEN** system drops oldest entries and logs a warning

#### Scenario: File size limit
- **WHEN** a monitored log file exceeds 1GB
- **THEN** system only tails the most recent 100MB and logs a warning
