## ADDED Requirements

### Requirement: LLM-powered error analysis
The system SHALL use a local LLM to analyze detected errors and their context to identify root causes and patterns.

#### Scenario: Error analysis request
- **WHEN** an error is detected with context window
- **THEN** system sends the error context and workload metadata to the LLM for analysis

#### Scenario: Analysis timeout
- **WHEN** LLM analysis exceeds 30 seconds
- **THEN** system cancels the request and logs a timeout error

### Requirement: Structured analysis output
The system SHALL request structured JSON output from the LLM containing error summary, root cause, severity, and confidence score.

#### Scenario: Valid JSON response
- **WHEN** LLM returns valid JSON matching the expected schema
- **THEN** system parses the response and proceeds to remediation proposal generation

#### Scenario: Invalid JSON response
- **WHEN** LLM returns malformed JSON or missing required fields
- **THEN** system logs the error and retries analysis once with clarified prompt

### Requirement: Workload context injection
The system SHALL provide workload metadata to the LLM including service name, version, organization, and service definition details.

#### Scenario: Context-aware analysis
- **WHEN** LLM receives workload metadata along with error context
- **THEN** analysis output references specific service configuration and deployment details

### Requirement: Domain knowledge injection
The system SHALL inject operator- and developer-supplied documentation into the analysis prompt so that analyses reflect the specific deployment rather than generic assumptions.

#### Scenario: Node-wide context
- **WHEN** readable `.md`, `.markdown`, or `.txt` files exist in the configured context directory
- **THEN** system includes their content in every analysis prompt, in filename order

#### Scenario: Per-workload context
- **WHEN** a workload declares MONITORING_CONTEXT_PATHS
- **THEN** system includes those files only in that workload's analysis prompts, ordered before node-wide context

#### Scenario: No context configured
- **WHEN** no context directory exists and no workload context paths are declared
- **THEN** system omits the knowledge section from the prompt entirely

#### Scenario: Unreadable context file
- **WHEN** a declared context path does not exist or cannot be read
- **THEN** system logs a warning and continues with the remaining context files

#### Scenario: Context updated at runtime
- **WHEN** a context file is edited after the service starts
- **THEN** the next analysis uses the updated content without requiring a restart

### Requirement: Context budget
The system SHALL bound injected knowledge so that guidance does not crowd out the log evidence in the model's context window.

#### Scenario: Oversized file
- **WHEN** a context file exceeds the per-file byte budget
- **THEN** system truncates it and marks the truncation in the prompt

#### Scenario: Total budget reached
- **WHEN** accumulated context reaches the total byte budget
- **THEN** system omits remaining documents and logs which were dropped

#### Scenario: Budget pressure with both sources
- **WHEN** the budget cannot fit both workload and node-wide context
- **THEN** system retains workload-specific context in preference to node-wide context

### Requirement: Severity classification
The system SHALL classify errors into severity levels: low, medium, high, or critical based on LLM analysis.

#### Scenario: Critical error
- **WHEN** LLM identifies an error that prevents service operation
- **THEN** system assigns severity "critical" and prioritizes remediation proposal

#### Scenario: Low severity error
- **WHEN** LLM identifies a non-blocking warning or informational message
- **THEN** system assigns severity "low" and may defer remediation proposal

### Requirement: Confidence scoring
The system SHALL include a confidence score (0.0-1.0) in the analysis output indicating LLM certainty.

#### Scenario: High confidence analysis
- **WHEN** LLM confidence score is >= 0.8
- **THEN** system proceeds with remediation proposal generation

#### Scenario: Low confidence analysis
- **WHEN** LLM confidence score is < 0.5
- **THEN** system logs the uncertainty and may request human review

### Requirement: Pattern detection
The system SHALL detect recurring error patterns across multiple log entries for the same workload.

#### Scenario: Recurring error
- **WHEN** the same error pattern appears 3+ times within 5 minutes
- **THEN** system aggregates occurrences and performs single analysis with frequency context

#### Scenario: Unique error
- **WHEN** an error pattern appears for the first time
- **THEN** system performs immediate analysis without aggregation

### Requirement: Resource-aware processing
The system SHALL queue analysis requests and process them asynchronously to avoid blocking log monitoring.

#### Scenario: Analysis queue
- **WHEN** multiple errors are detected simultaneously
- **THEN** system queues analysis requests and processes them in order of severity

#### Scenario: Queue overflow
- **WHEN** analysis queue exceeds 50 pending requests
- **THEN** system drops lowest severity requests and logs a warning
