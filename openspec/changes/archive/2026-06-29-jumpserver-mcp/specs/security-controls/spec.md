## ADDED Requirements

### Requirement: Dangerous command detection and blocking
The system SHALL identify and block execution of known dangerous commands without authorization.

#### Scenario: Block destructive rm command
- **WHEN** user attempts to execute "rm -rf /"
- **THEN** system blocks command and returns security policy violation error

#### Scenario: Block permission modification commands
- **WHEN** user attempts to execute "chmod 777 /", "chown root:root /", or similar
- **THEN** system blocks command and logs security event

#### Scenario: Block network disruption commands
- **WHEN** user attempts to execute "iptables", "tc", "systemctl stop networking"
- **THEN** system blocks command with security policy violation

#### Scenario: Block kernel/system modification
- **WHEN** user attempts to execute "insmod", "rmmod", "modprobe" on restricted modules
- **THEN** system blocks command and requires elevated authorization

#### Scenario: Allow safe commands
- **WHEN** user executes safe informational command "ls", "whoami", "ps"
- **THEN** system allows execution without blocking

### Requirement: Command authorization workflow
The system SHALL support requesting authorization for blocked commands when permitted.

#### Scenario: Request elevated privileges for blocked command
- **WHEN** user attempts blocked command with authorization request
- **THEN** system creates authorization request and routes to approval workflow

#### Scenario: Pre-approved command patterns
- **WHEN** command pattern is pre-approved in authorization policy
- **THEN** system allows execution without additional approval

#### Scenario: Authorization denied
- **WHEN** authorization request for dangerous command is denied
- **THEN** system blocks execution and logs denial

### Requirement: Command pattern matching for security policies
The system SHALL use pattern matching to identify dangerous commands and variations.

#### Scenario: Match exact command names
- **WHEN** command is "rm", "mkfs", "dd"
- **THEN** system identifies as dangerous

#### Scenario: Match command with dangerous flags
- **WHEN** command is "rm -r -f /" or variations with whitespace
- **THEN** system identifies dangerous intent and blocks

#### Scenario: Match piped dangerous commands
- **WHEN** command contains "rm" piped with "find" or similar combinations
- **THEN** system analyzes full pipeline and blocks if dangerous

#### Scenario: Allow whitelisted variations
- **WHEN** safe variation of command is whitelisted (e.g., "rm" with restriction to /tmp only)
- **THEN** system allows execution with restrictions applied

### Requirement: Destructive operation detection
The system SHALL detect and require confirmation for operations that modify system state.

#### Scenario: Detect file deletion operations
- **WHEN** command would delete files outside temp directories
- **THEN** system requires confirmation or elevated authorization

#### Scenario: Detect service disruption
- **WHEN** command would stop critical services
- **THEN** system blocks with security policy violation

#### Scenario: Detect configuration changes
- **WHEN** command modifies system configuration files (/etc/*)
- **THEN** system requires authorization and logs change

#### Scenario: Detect user/permission changes
- **WHEN** command modifies user accounts, groups, or file permissions
- **THEN** system requires authorization and logs change

### Requirement: Audit logging of command execution
The system SHALL log all command executions with complete context for security auditing.

#### Scenario: Log successful command execution
- **WHEN** command executes successfully
- **THEN** system logs: timestamp, user, host, command, exit code, duration

#### Scenario: Log blocked commands
- **WHEN** command is blocked by security policy
- **THEN** system logs: timestamp, user, host, blocked command, reason, policy violated

#### Scenario: Log authorization requests
- **WHEN** user requests authorization for dangerous command
- **THEN** system logs: request timestamp, user, host, command, request status, approver

#### Scenario: Log failed authentication
- **WHEN** connection or authentication fails
- **THEN** system logs: timestamp, user, host, failure reason

### Requirement: Lazy permission enforcement via JumpServer
The system SHALL NOT pre-check JumpServer RBAC before execution. Instead it SHALL let JumpServer enforce permissions at execution time and surface any resulting denial as a distinct `permission_denied` error. Local dangerous-command tiers (Tier 1 hard block, Tier 2 approval) are evaluated locally BEFORE the command is sent; permission denial is discovered only when JumpServer rejects execution.

#### Scenario: Permission denied surfaced at execution time
- **WHEN** a user without permission on the target host executes a command that passes local security tiers
- **THEN** the system sends the command, JumpServer rejects it, and the system returns a `permission_denied` error rather than having pre-validated access

#### Scenario: Same command differs by user
- **WHEN** two different users run the identical command on the same host and only one is permitted by JumpServer
- **THEN** the permitted user's command executes and the other receives `permission_denied`, reflecting JumpServer's per-user authorization

#### Scenario: Local tiers evaluated before permission check
- **WHEN** a command matches a Tier 1 destructive pattern
- **THEN** the system hard-blocks it locally and never reaches JumpServer permission enforcement, regardless of the user's RBAC

### Requirement: Command content inspection
The system SHALL inspect command content to detect injection attempts and policy violations.

#### Scenario: Detect command injection via input
- **WHEN** command parameters contain shell metacharacters and injection patterns
- **THEN** system sanitizes or blocks injection attempts

#### Scenario: Detect credential exposure attempts
- **WHEN** command attempts to dump environment or config with credentials
- **THEN** system blocks and logs as security violation

#### Scenario: Detect reverse shell attempts
- **WHEN** command contains reverse shell patterns (nc, bash /dev/tcp, etc.)
- **THEN** system blocks and logs security event

### Requirement: Two-tier dangerous command classification
The system SHALL classify commands against an admin-managed regex blacklist into two tiers with distinct handling: Tier 1 (destructive) is hard-blocked, Tier 2 (risky) requires human approval.

#### Scenario: Tier 1 destructive command is hard-blocked
- **WHEN** a command matches a Tier 1 pattern (e.g. "rm -rf /", "mkfs.ext4 /dev/sda", "dd if=/dev/zero of=/dev/sda")
- **THEN** system immediately rejects execution and returns an explanatory message, with no override or approval path

#### Scenario: Tier 2 risky command requires approval
- **WHEN** a command matches a Tier 2 pattern (e.g. "rm -f /var/log/app/old.log", "echo '' > config.yml")
- **THEN** system returns a `pending_approval` status and does not execute until a human approves

#### Scenario: Command matches no blacklist tier
- **WHEN** a command matches neither Tier 1 nor Tier 2 patterns
- **THEN** system allows execution to proceed normally

### Requirement: Blacklist is administrator-managed only
The system SHALL allow only the MCP deployment administrator to modify the dangerous-command blacklist; no MCP tool exposed to callers SHALL be able to read or modify it.

#### Scenario: Caller cannot modify blacklist via MCP tools
- **WHEN** an agent or user attempts to alter the blacklist through any MCP tool
- **THEN** system rejects the attempt because no such tool capability exists

#### Scenario: Administrator updates blacklist via deployment config
- **WHEN** the deployment administrator edits the blacklist configuration on the server
- **THEN** system loads the updated patterns without exposing the change to callers

### Requirement: Human approval workflow for Tier 2 commands
The system SHALL provide an approval workflow where a caller blocks and polls for a human decision, with a default 5-minute timeout that auto-denies on expiry.

#### Scenario: Approval granted within timeout
- **WHEN** a Tier 2 command is pending and a human approves within 5 minutes
- **THEN** system proceeds to execute the command and records the approval

#### Scenario: Approval times out
- **WHEN** no human responds to a pending Tier 2 command within the 5-minute timeout (configurable)
- **THEN** system auto-denies the request, does not execute, and returns a timeout-denial result

#### Scenario: Approval explicitly denied
- **WHEN** a human rejects a pending Tier 2 command
- **THEN** system does not execute and returns a denial result with the reason

### Requirement: Pre-supplied allowed commands for automation
The system SHALL allow a caller to pre-supply a list of permitted command patterns at session start so trusted automation can bypass the Tier 2 approval prompt for those specific patterns.

#### Scenario: Pre-approved pattern skips prompt
- **WHEN** a session is opened with a pre-supplied allowed list and a Tier 2 command matches one of those patterns
- **THEN** system executes without requesting human approval and logs that a pre-approval was used

#### Scenario: Pre-approved list does not cover Tier 1
- **WHEN** a pre-supplied allowed list would match a Tier 1 destructive command
- **THEN** system still hard-blocks the Tier 1 command, ignoring the pre-approval

### Requirement: Session-scoped approval exemption
The system SHALL, upon approval of a Tier 2 command, exempt exactly the triggered blacklist regex for the remainder of the current session only.

#### Scenario: Approved regex is exempt for rest of session
- **WHEN** a Tier 2 command is approved and a later command in the same session matches the same triggered regex
- **THEN** system executes the later command without re-prompting for approval

#### Scenario: Exemption does not cross sessions
- **WHEN** a new session matches a regex that was approved in a previous session
- **THEN** system requires fresh approval because exemptions are session-scoped

#### Scenario: Exemption is regex-scoped, not text-fuzzy
- **WHEN** a later command modifies a different file but triggers a different blacklist regex than the approved one
- **THEN** system treats it as a new Tier 2 command requiring its own approval

### Requirement: Security audit log in local SQLite
The system SHALL persist security-relevant events to a local SQLite database, recording at minimum the command text, timestamp, and initiating user, plus host and decision outcome.

#### Scenario: Record a blocked Tier 1 command
- **WHEN** a Tier 1 command is hard-blocked
- **THEN** system writes a SQLite record with command, timestamp, initiating user, host, and outcome "blocked"

#### Scenario: Record an approved Tier 2 command
- **WHEN** a Tier 2 command is approved and executed
- **THEN** system writes a SQLite record with command, timestamp, initiating user, host, approver, and outcome "approved"

#### Scenario: Record an auto-denied command
- **WHEN** a Tier 2 approval times out and is auto-denied
- **THEN** system writes a SQLite record with command, timestamp, initiating user, host, and outcome "auto_denied"

### Requirement: Configurable blacklist or whitelist policy mode
The system SHALL support a configurable `policy_mode` that selects whether the main command gate operates as a blacklist (default-allow) or a whitelist (default-deny), defaulting to blacklist. The Tier 1 destructive blacklist SHALL always apply first regardless of mode.

#### Scenario: Tier 1 always enforced before mode evaluation
- **WHEN** any command is submitted under any `policy_mode`
- **THEN** system first evaluates the Tier 1 destructive blacklist and hard-blocks a match before applying the selected mode, even if the command would otherwise be whitelisted

#### Scenario: Blacklist mode is the default
- **WHEN** no `policy_mode` is configured
- **THEN** system operates in blacklist mode: commands are allowed unless they match a Tier 1 or Tier 2 pattern

#### Scenario: Blacklist mode allows unlisted commands
- **WHEN** `policy_mode` is blacklist and a command matches no Tier 1 or Tier 2 pattern
- **THEN** system allows the command to execute

#### Scenario: Whitelist mode denies unlisted commands
- **WHEN** `policy_mode` is whitelist and a command does not match any whitelist entry
- **THEN** system denies the command and returns a policy violation indicating whitelist mode

#### Scenario: Whitelist mode allows listed commands
- **WHEN** `policy_mode` is whitelist and a command matches a whitelist entry and is not a Tier 1 match
- **THEN** system allows the command to execute

#### Scenario: Whitelist mode still hard-blocks Tier 1
- **WHEN** `policy_mode` is whitelist and a command matches both a whitelist entry and a Tier 1 destructive pattern
- **THEN** system hard-blocks the command because Tier 1 enforcement precedes whitelist allowance

#### Scenario: Policy mode and lists are administrator-managed only
- **WHEN** a caller attempts to change `policy_mode` or edit the whitelist/blacklist through any MCP tool
- **THEN** system rejects the attempt; these are configured only by the MCP deployment administrator
