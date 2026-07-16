# session-management Specification

## Purpose

Define the stateful logical session as the primary execution mechanism, including idle expiry and concurrency limits with queueing, so that commands can reuse a host/user/approval context without holding a persistent connection.

## Requirements

### Requirement: Logical session as execution context
The system SHALL provide a stateful logical session as the primary execution mechanism, plus a one-shot convenience wrapper. A session is a context record (host, runas user, session-scoped approved-regex set, last-activity time), not a persistent connection — each command is dispatched as a separate JumpServer ops job using that context.

#### Scenario: Open a session and reuse its context
- **WHEN** caller invokes `open_session(host, user_id?)`
- **THEN** system creates a logical session context bound to the host and user, returns a `session_id`, and subsequent `execute(session_id, command)` calls reuse that context to dispatch each command as a separate ops job

#### Scenario: One-shot convenience call
- **WHEN** caller invokes `run(host, user_id, command)` for a single command
- **THEN** system internally opens a session context, dispatches the command as an ops job, and closes the context, returning the result in one call

#### Scenario: Open session without specifying user when multiple exist
- **WHEN** caller calls `open_session(host)` and the host has multiple available users
- **THEN** system returns the list of available users and does not create an active context until a `user_id` is supplied

#### Scenario: Explicit session close
- **WHEN** caller invokes `close_session(session_id)`
- **THEN** system discards the session context, releases its concurrency slot, and marks the session closed

### Requirement: Session idle timeout
The system SHALL expire session contexts after a configurable idle period, defaulting to 15 minutes.

#### Scenario: Idle timeout expires session
- **WHEN** a session receives no command for the configured idle period (default 15 minutes)
- **THEN** system discards the session context, releases its concurrency slot, and rejects later use of that `session_id` with a clear "session expired" message

#### Scenario: Activity resets idle timer
- **WHEN** a command is executed on an active session before the idle period elapses
- **THEN** system resets the idle timer from the moment of that command

#### Scenario: Idle timeout is configurable
- **WHEN** the deployment configures a non-default idle timeout
- **THEN** system honours the configured value instead of 15 minutes

### Requirement: Concurrency limit with queueing
The system SHALL cap the number of concurrent active sessions (and their in-flight ops jobs) and queue requests beyond the cap.

#### Scenario: Open within capacity
- **WHEN** the number of active sessions is below the configured maximum (default 10) and `open_session` is called
- **THEN** system creates the session immediately

#### Scenario: Open beyond capacity queues
- **WHEN** the configured maximum concurrent sessions (default 10) is reached and another `open_session` is called
- **THEN** system places the request in a queue and returns a "queued" status indicating the caller is waiting for a free slot

#### Scenario: Queued request proceeds when slot frees
- **WHEN** an active session closes (explicitly, by error, or by idle timeout) while requests are queued
- **THEN** system admits the next queued request and creates its session context

#### Scenario: Concurrency limit is configurable
- **WHEN** the deployment configures a non-default maximum concurrent sessions value
- **THEN** system enforces that value instead of 10
