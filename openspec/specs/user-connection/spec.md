# user-connection Specification

## Purpose

Govern how a connecting identity (runas user) is selected for a host and how commands are executed against JumpServer via Ops Jobs using a logical session context, with permissions discovered at execution time rather than pre-checked.

## Requirements

### Requirement: List available users for a host
The system SHALL enumerate all users configured in JumpServer that can connect to a specific host.

#### Scenario: Host has multiple users
- **WHEN** user queries available users for host "web-01"
- **THEN** system returns list of users with their authentication methods (password, key, etc.)

#### Scenario: Host has no configured users
- **WHEN** user queries available users for host with no user mappings
- **THEN** system returns empty list with informative message

#### Scenario: User lacks permission to view users
- **WHEN** user queries available users they cannot access
- **THEN** system returns permission error

### Requirement: Interactive user selection
The system SHALL provide interactive selection when multiple users are available for a connection.

#### Scenario: Present user options interactively
- **WHEN** multiple users are available and interactive mode is enabled
- **THEN** system displays numbered list of users for selection

#### Scenario: User selects valid option
- **WHEN** user selects user from presented list
- **THEN** system initiates connection with selected user

#### Scenario: User provides invalid selection
- **WHEN** user selects invalid option number
- **THEN** system reprompts with valid options

### Requirement: Pre-configured user for automation
The system SHALL support specifying a user before connection for non-interactive automation scenarios.

#### Scenario: Pre-select user for batch operation
- **WHEN** task specifies user_name in connection parameters
- **THEN** system connects directly with specified user without prompting

#### Scenario: Specified user not available for host
- **WHEN** specified user_name cannot connect to target host
- **THEN** system returns error before attempting connection

#### Scenario: Automation with multiple hosts, same user
- **WHEN** batch operation uses single user for all hosts
- **THEN** system attempts connection with specified user for each host

### Requirement: Execute via JumpServer Ops Job using the selected runas user
The system SHALL execute commands by creating a JumpServer Ops Job, passing the selected user as the job's `runas` identity, rather than holding a persistent SSH/RDP connection.

#### Scenario: Selected user becomes the job runas
- **WHEN** a command is executed for a host with a selected user
- **THEN** system creates an Ops Job targeting that host's asset with `runas` set to the selected user

#### Scenario: Host or asset not resolvable
- **WHEN** the target host cannot be resolved to a JumpServer asset id
- **THEN** system returns an error before submitting any job

#### Scenario: Job submission unreachable
- **WHEN** JumpServer is unreachable while submitting the job
- **THEN** system retries up to 3 times and then returns a `jumpserver_unreachable` error

### Requirement: Logical session context, not a persistent connection
The system SHALL maintain a logical session context (host, runas user, approved-regex set, last-active time) that is reused across commands, without holding any live SSH channel between commands.

#### Scenario: Context reused across commands
- **WHEN** multiple commands are executed under the same `session_id`
- **THEN** system reuses the stored host + runas user + approval context, submitting a separate Ops Job per command

#### Scenario: Context released on close or timeout
- **WHEN** a session is closed explicitly or expires by idle timeout
- **THEN** system discards the logical context and rejects later use of that `session_id`

### Requirement: Permission discovered at execution time, not pre-checked
The system SHALL NOT pre-query a user's permission on a target host before connecting; instead it SHALL attempt the operation and surface a permission error only when JumpServer denies it at execution time.

#### Scenario: No upfront permission query
- **WHEN** a connection or command is requested with a selected user
- **THEN** system proceeds without first querying JumpServer RBAC for that host/user pair

#### Scenario: Permission denial surfaced at execution
- **WHEN** the selected user lacks permission and JumpServer denies the operation
- **THEN** system returns a `permission_denied` error reflecting the runtime denial

#### Scenario: Same host, different users may differ in permission
- **WHEN** the same command is run on one host under two different users
- **THEN** system reports each user's outcome independently based on JumpServer's runtime decision

### Requirement: User selection drives the connecting identity
The system SHALL, when multiple users are available and none was pre-specified, return the candidate list for the caller to choose from, and SHALL use the chosen user for the resulting connection.

#### Scenario: Multiple users returned for selection
- **WHEN** a host has 3 eligible users and no user was specified
- **THEN** system returns the 3 users and does not connect yet

#### Scenario: Chosen user is used for the connection
- **WHEN** the caller selects one user from the returned list
- **THEN** system establishes the connection using exactly that selected user

#### Scenario: Pre-specified user skips selection
- **WHEN** a user is provided up front (automation)
- **THEN** system connects directly with that user without returning a selection list
