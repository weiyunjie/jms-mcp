# batch-operations Specification

## Purpose

Support executing a command across many hosts with flexible host selection, concurrency control, progress tracking, result aggregation, cancellation, scheduling, and reporting — using pure parallel semantics with no rollback or automatic retry.

## Requirements

### Requirement: Execute command across multiple hosts
The system SHALL support executing a command on multiple hosts in a single batch operation.

#### Scenario: Sequential execution across hosts
- **WHEN** user specifies multiple hosts and command for batch execution
- **THEN** system executes command on each host sequentially and returns results

#### Scenario: Parallel execution with concurrency control
- **WHEN** user specifies max_concurrency parameter
- **THEN** system executes up to N commands in parallel while respecting limit

#### Scenario: Batch with single host
- **WHEN** batch operation specifies single host
- **THEN** system executes command on that host with batch tracking

#### Scenario: Empty host list
- **WHEN** batch operation specifies empty host list
- **THEN** system returns error indicating no hosts specified

### Requirement: Batch result aggregation
The system SHALL collect and aggregate results from all batch operations.

#### Scenario: Collect all execution results
- **WHEN** batch execution completes
- **THEN** system returns results indexed by hostname with individual outcomes

#### Scenario: Partial batch failure
- **WHEN** some hosts fail while others succeed
- **THEN** system returns results for all hosts with failure details

#### Scenario: All hosts fail
- **WHEN** batch execution fails on all hosts
- **THEN** system returns aggregated error information with common patterns identified

#### Scenario: Large batch result handling
- **WHEN** the aggregate batch result set is large
- **THEN** system writes the results to a compressed file and returns a download channel reference instead of returning the full result set inline

### Requirement: Batch execution progress tracking
The system SHALL emit periodic progress updates during batch execution, reported as completed-host-count over total-host-count.

#### Scenario: Periodic progress updates every 30 seconds
- **WHEN** a batch operation is running
- **THEN** system emits a progress update every 30 seconds in the form "completed/total" (e.g., "37/100 hosts completed")

#### Scenario: Final progress on completion
- **WHEN** a batch operation finishes
- **THEN** system emits a final progress update showing total completed equal to total hosts

#### Scenario: Progress reflects only completed hosts
- **WHEN** some hosts are still in flight at a progress tick
- **THEN** system counts only hosts whose execution has fully completed (success or failure) toward the completed count

### Requirement: Batch host selection
The system SHALL support flexible host selection for batch operations.

#### Scenario: Select hosts by hostname pattern
- **WHEN** batch operation specifies hostname_pattern="web-*"
- **THEN** system expands pattern and executes on all matching hosts

#### Scenario: Select hosts by asset group
- **WHEN** batch operation specifies group="production"
- **THEN** system executes on all hosts in specified group

#### Scenario: Select hosts by filter criteria
- **WHEN** batch operation specifies filters (os_type=linux, protocol=ssh)
- **THEN** system applies filters and executes on all matching hosts

#### Scenario: Explicit host list
- **WHEN** batch operation provides explicit list of hostnames
- **THEN** system executes on specified hosts in order

#### Scenario: Host exclusion patterns
- **WHEN** batch operation specifies exclude_hosts=["maintenance-*"]
- **THEN** system excludes matching hosts from execution

### Requirement: Parallel execution without rollback
The system SHALL execute batch operations purely in parallel across hosts, with no transaction or rollback semantics. Each host's outcome is independent.

#### Scenario: Pure parallel execution
- **WHEN** a batch operation is started across multiple hosts
- **THEN** system executes the command on all hosts in parallel (subject to the connection-pool limit) without ordering or dependency constraints

#### Scenario: Partial failure reported as counts
- **WHEN** some hosts succeed and some fail
- **THEN** system returns a result of the form "N succeeded, M failed" with per-host detail, and does NOT roll back the hosts that already succeeded

#### Scenario: No rollback on failure
- **WHEN** any host fails during the batch
- **THEN** system leaves all already-executed hosts in their resulting state and never attempts compensating/rollback commands

### Requirement: Batch cancellation
The system SHALL allow an in-progress batch operation to be cancelled, reporting what completed and what was skipped.

#### Scenario: Cancel an in-progress batch
- **WHEN** a user cancels a running batch operation
- **THEN** system stops dispatching new hosts and returns a result stating "operation cancelled", listing the hosts whose execution already completed and indicating the remaining hosts will not be executed

#### Scenario: In-flight hosts at cancellation
- **WHEN** a batch is cancelled while some hosts are mid-execution
- **THEN** system reports those hosts with an interrupted/unknown status rather than claiming success

### Requirement: Batch error handling without auto-retry
The system SHALL handle per-host failures without automatically retrying, leaving the retry decision to the caller.

#### Scenario: Skip failed hosts and continue
- **WHEN** batch operation encounters a host failure
- **THEN** system records the failure and continues executing the remaining hosts (parallel execution is unaffected by individual host failures)

#### Scenario: No automatic retry of failed hosts
- **WHEN** one or more hosts fail during a batch
- **THEN** system does NOT automatically retry them and instead returns each failure with its classified error category for the caller to decide on retry

#### Scenario: Report success/failure summary
- **WHEN** batch execution completes
- **THEN** system returns a summary in the form "N succeeded, M failed" with per-host details, without retry recommendations or automatic recovery

#### Scenario: Host interrupted mid-execution
- **WHEN** a host's command is interrupted by a connection drop during the batch
- **THEN** system reports that host with `connection_interrupted` and an "execution status unknown" note rather than claiming success or silently retrying

### Requirement: Batch operation scheduling
The system SHALL support scheduling batch operations for future or recurring execution.

#### Scenario: Schedule batch operation for specific time
- **WHEN** user specifies scheduled_for=2024-12-25T10:00:00
- **THEN** system schedules batch operation to execute at specified time

#### Scenario: Recurring batch operation
- **WHEN** user specifies cron_schedule="0 2 * * *"
- **THEN** system schedules batch operation to execute daily at 2 AM

#### Scenario: Batch operation with delay
- **WHEN** user specifies delay_seconds=300
- **THEN** system waits specified duration before starting batch execution

### Requirement: Batch operation reporting
The system SHALL generate comprehensive reports of batch operations.

#### Scenario: Generate execution report
- **WHEN** batch operation completes
- **THEN** system generates report with duration, success rate, output summary

#### Scenario: Export batch results
- **WHEN** user requests export of batch results
- **THEN** system exports results in requested format (JSON, CSV, etc.)

#### Scenario: Batch comparison report
- **WHEN** user compares current batch results with previous execution
- **THEN** system highlights differences and changes
