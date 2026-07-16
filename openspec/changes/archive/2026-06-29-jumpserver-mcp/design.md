## Context

JumpServer is a widely-used open-source bastion host/jump server that manages SSH credentials, enforces access control, and provides audit trails for server access. Organizations use JumpServer to centralize authentication, authorization, and logging for infrastructure access.

The MCP (Model Context Protocol) is a standard for integrating LLM-powered agents with tools and data sources. This project creates an MCP server that exposes JumpServer capabilities, allowing AI agents and automation tools to programmatically discover, connect to, and manage servers through JumpServer's existing security framework.

**Constraints:**
- Must respect JumpServer's RBAC and audit logging
- Operations are executed through JumpServer-authenticated channels
- Command execution happens on target servers via JumpServer's configured transport (SSH, RDP, etc.)
- Security controls must be enforceable without requiring JumpServer source modifications

## Goals / Non-Goals

**Goals:**
- Enable AI agents and automation tools to discover servers by hostname/IP through JumpServer
- Support command execution on servers via JumpServer-configured credentials
- Provide interactive user selection when multiple credentials are available
- Allow pre-configuration of users for automated/non-interactive flows
- Batch operations across multiple servers with transaction-like semantics
- Enforce dangerous command detection and prevention
- Maintain full audit trail through JumpServer's logging

**Non-Goals:**
- Modifying JumpServer's core authentication or RBAC mechanisms
- Supporting protocols beyond what JumpServer currently offers (SSH, RDP, Telnet)
- Building a replacement for JumpServer's web UI or manual workflows
- Custom credential management (rely on JumpServer's configured assets/users)
- Real-time terminal multiplexing or interactive shell access

## Decisions

**Decision 1: Architecture - Local MCP Server vs. JumpServer Plugin**
- **Choice**: Local MCP server that communicates with JumpServer via HTTP API + SSH
- **Rationale**: 
  - MCP server runs independently, decoupling this tool from JumpServer updates
  - Can leverage JumpServer's REST API for host/user discovery and audit
  - SSH execution uses JumpServer's configured jump/bastion setup
  - Simpler deployment (one service vs. JumpServer plugin development)
- **Alternatives**: 
  - JumpServer plugin: tighter integration but requires JumpServer modification; updates would be blocked by JumpServer release cycles
  - Direct SSH from agent: loses JumpServer's audit trail and RBAC enforcement

**Decision 2: Authentication to JumpServer**
- **Choice**: Accept JumpServer API token (or username/password for fallback) as MCP tool parameter; securely store token in environment or config
- **Rationale**: 
  - Agents invoke MCP tools with credentials already in their context
  - Avoids separate credential management layer
  - Token can be rotated without MCP server restart
- **Alternatives**:
  - OAuth2 / OIDC: adds complexity for local automation use cases
  - Mutual TLS: requires certificate distribution

**Decision 3: Command Execution Transport — JumpServer Ops Job API**
- **Choice**: Execute commands by creating an ad-hoc Ops Job through JumpServer's REST API (`POST /ops/jobs/` with `module=shell`, `args=<command>`, `assets=[<asset_id>]`, `runas=<user>`, `instant=true`), then poll `GET /ops/job-execution/task-detail/{task_id}/` until finished and parse the `summary` for results. This matches the existing daemon implementation (the working `find_large_log_paths` tool already uses this path).
- **Rationale**:
  - Already proven in the existing codebase — reuses validated request/poll/parse machinery
  - Inherits JumpServer's audit trail (Ops Job execution history) and RBAC `runas` enforcement for free
  - Batch is natural: a single job can target multiple `assets`
  - No SSH channel to manage, authenticate, or keep alive
- **Consequences (important)**:
  - Ops Job is a **stateless request-poll model** — there is no persistent connection to reuse. This reshapes the session model (see Decision 8) into a *logical context*, not a live channel.
  - Output format is whatever JumpServer's `summary` returns; the output-handling rules (Decision 13) must adapt to that envelope rather than reading a raw stream (see spike task in tasks.md).
- **Alternatives**:
  - Direct SSH proxy / persistent channel: enables true connection reuse and interactive streaming, but bypasses Ops Job audit, requires SSH lifecycle management, and discards the existing working implementation
  - Direct SSH from agent: loses JumpServer audit trail and RBAC entirely

**Decision 4: Dangerous Command Enforcement**
- **Choice**: Configurable block-list of patterns (regex) + allowlist for specific commands; evaluated client-side before execution
- **Rationale**:
  - Prevents accidental destructive operations at the agent level
  - Configurable per-deployment (different rules for staging vs. production)
  - Fast, local evaluation
  - Can be overridden by explicit approval flow if needed
- **Alternatives**:
  - Server-side filtering in JumpServer: less flexible, requires JumpServer changes
  - Runtime sandboxing: overkill for initial version
  - No filtering: unsafe

**Decision 5: Batch Operations Model**
- **Choice**: Parallel execution with individual result collection; optional transaction flag for "all-or-nothing" semantics
- **Rationale**:
  - Agents can parallelize independent operations for speed
  - Per-server error handling without cascading failures
  - Optional transaction mode for destructive operations (e.g., upgrade all, or none)
- **Alternatives**:
  - Pure sequential: slower, less useful for batch use cases
  - Always-atomic: rigid, fails on single-server issue

**Decision 6: User Selection Strategy**
- **Choice**: Accept optional `user_id` or `username` parameter; if multiple users available and none specified, return list for agent selection
- **Rationale**:
  - Automation flows can pre-specify user, avoiding interactive prompts
  - Ad-hoc operations can ask the agent to choose, then re-invoke with selected user
  - Respects JumpServer's configured user permissions per asset
- **Alternatives**:
  - Always interactive: breaks automation
  - Always auto-select first: unpredictable, potentially wrong permissions

**Decision 7: Configuration via Environment Variables**
- **Choice**: JumpServer base URL and API token are read exclusively from environment variables (e.g., `JMS_BASE_URL`, `JMS_API_TOKEN`). If missing, every tool returns a clear, actionable hint instead of failing silently.
- **Rationale**:
  - Keeps secrets out of CLI args and tool parameters (avoids leaking into agent context/logs)
  - Standard pattern for MCP servers; easy to rotate without code changes
  - Explicit "not configured" hint helps users self-diagnose first-run setup
- **Alternatives**:
  - Config file: still viable as a future addition, but env vars are the baseline
  - Per-call token parameter: risks token ending up in agent transcripts

**Decision 8: Session Model — Logical Context, Not a Live Connection (Hybrid)**
- **Choice**: Because execution runs through stateless Ops Jobs (Decision 3), a "session" is a **logical context record**, not a held SSH connection. `open_session(host, user_id?) → session_id` creates an in-memory context `{ session_id, asset_id, runas_user, approved_regexes, last_active_at }`. `execute(session_id, command)` looks up that context and POSTs a **new** Ops Job using its bound asset + runas. `close_session(session_id)` discards the context. A convenience `run(host, user_id, command)` wraps open+execute+close for one-off commands using the same machinery.
- **Rationale**:
  - Ops Jobs have no reusable connection, so "session" cannot mean a live channel — but the abstraction is still needed
  - The context still anchors the things that genuinely need session identity: the bound runas user, the session-scoped approval exemptions (Decision 11), and concurrency accounting (Decision 9)
  - `run()` keeps simple one-shot calls ergonomic
- **Alternatives**:
  - Live SSH channel: no longer applicable — execution is via Ops Job, there is no channel to keep
  - Pure stateless (user_id per call): cannot carry session-level approval exemptions or bound-user context

**Decision 9: Session Lifecycle — Idle Timeout & Concurrency Limit**
- **Choice**: A logical session's context expires after 15 minutes of inactivity (configurable, default 15m); each `execute` resets the idle timer and expiry discards the context record (it does **not** tear down a connection, since there is none). A global limit caps **concurrent in-flight Ops Jobs** at 10 (configurable, default 10); `execute` calls beyond the cap queue and receive a "queued" status response. Note this limits concurrent executing jobs, not open session contexts (context records are cheap and not capped).
- **Rationale**:
  - Idle timeout reclaims context records (and any pending approval state) and bounds memory
  - The concurrency cap protects JumpServer and target servers from job storms (e.g., 50 agents at once) — applied at the job-dispatch level since that is the real resource
  - Queue-with-status keeps callers informed rather than failing outright
- **Alternatives**:
  - No timeout: leaks context records and stale approval grants
  - Capping open contexts instead of in-flight jobs: contexts are cheap; the real load is concurrent jobs hitting JumpServer
  - Reject instead of queue when at cap: less friendly to bursty automation

**Decision 10: Layered Command Policy — Permanent Tier-1 Floor + Switchable Blacklist/Whitelist Mode**
- **Choice**: Every command passes a layered evaluation. The policy config (admin-only, never editable via any MCP tool) defines:
  - **Layer 0 — Tier-1 Destructive floor (always on, mode-independent)**: a regex set for catastrophic operations (e.g. `rm -rf /`, `mkfs*`, `dd if=... of=/dev/sd*`). A match is a hard reject with no override path — this runs first in **both** modes, so even a command mistakenly added to a whitelist is still blocked here.
  - **Main gate — `policy_mode` config (default `blacklist`)**:
    - `blacklist` mode: default-allow. After Layer 0, a Tier-2 risky regex match (e.g. `rm -f /var/log/...`, `echo '' > file`, file modify/delete) returns `pending_approval`; everything else runs.
    - `whitelist` mode: default-deny. After Layer 0, the command must match the allowlist regex set to run; a non-match is rejected outright. (Tier-2 approval still applies to whitelisted-but-risky commands if configured.)
- **Rationale**:
  - Blacklist and whitelist are not mutually exclusive: the Tier-1 floor is a permanent safety net under both, while `policy_mode` only flips the default direction of the main gate
  - Whitelist mode suits high-security or strict-automation deployments; blacklist suits flexible general ops
  - Admin-only config prevents privilege escalation through the tool itself
- **Alternatives**:
  - Pure blacklist only: cannot lock down strict environments to an approved command set
  - Pure whitelist only: impractical for open-ended ad-hoc ops work
  - Single tier (no permanent floor): a whitelist slip could expose catastrophic commands

**Decision 11: Approval Flow (works for both humans and automation)**
- **Choice**: When a Tier-2 command hits approval:
  - The caller (agent) **blocks and polls** for the decision, with a default timeout of 5 minutes.
  - If no human responds within 5 minutes, the request is **auto-denied**.
  - Callers may **pre-supply an allowed-command list** at `open_session`/`run` time so trusted automation skips the prompt for those specific patterns.
  - On approval, the granted exemption is scoped to **the exact blacklist regex that was triggered**, valid only for the current session ("allow similar commands this session" = that one regex is exempted for this session).
- **Rationale**:
  - Single mechanism serves interactive humans and headless automation
  - 5-minute auto-deny prevents stuck automation
  - Regex-scoped exemption is predictable and auditable (vs. fuzzy "similar command" text matching)
- **Alternatives**:
  - Fire-and-forget pending (no polling): agent can't act on the outcome
  - Fuzzy textual "similar" matching: ambiguous, hard to audit, security risk

**Decision 12: Audit Storage — Local SQLite**
- **Choice**: A local SQLite database records security-relevant events: blocked/approved commands, the command text, timestamp, and the initiating user (plus host and decision outcome).
- **Rationale**:
  - Zero-dependency, file-based, easy to deploy alongside the MCP server
  - Sufficient for the audit record described; complements (not replaces) JumpServer's own logs
- **Alternatives**:
  - External DB (Postgres): heavier deploy for a local tool
  - Flat log files: harder to query for audit review

**Decision 13: Output Handling — Encoding, Binary, Large Output**
- **Choice**:
  - **Encoding**: never trust the declared encoding — detect, fall back, and normalize all text output to UTF-8 (three-stage: detect → fallback → emit UTF-8).
  - **Binary detection**: recognize binary streams (tar, gzip/zip, openssl, compressed mysqldump, or any non-UTF-8 stdout) and never treat them as strings.
  - **Large single output**: cap at a configurable maximum (default 100MB); when exceeded, return the first 100MB and clearly annotate before and after the output that it was truncated.
  - **Large batch result set**: when aggregate results are large, write them to a compressed file and expose a download channel rather than returning inline.
- **Rationale**:
  - Mixed-locale fleets (UTF-8/GBK/Latin-1) produce garbage if encoding is assumed
  - Streaming binaries as strings corrupts data and bloats context
  - Bounded output protects memory and agent context windows
- **Alternatives**:
  - Assume UTF-8: breaks on GBK/Latin-1 hosts
  - Unbounded output: memory and context blowups

**Decision 14: Error Classification & Mid-Command Disconnect**
- **Choice**: Errors are returned as distinct, machine-distinguishable categories: `jumpserver_unreachable`, `target_unreachable`, `permission_denied`, `command_blocked`, `connection_interrupted`. JumpServer-unreachable triggers 3 retries before erroring. The MCP **never auto-retries an interrupted in-flight command**; it returns "execution status unknown, connection interrupted at command X" and hands the retry decision to the agent/human (who must themselves verify completion before retrying).
- **Rationale**:
  - Agents need to branch on *why* something failed
  - SSH cannot guarantee exactly-once; silent retry risks double-executing non-idempotent commands
  - JumpServer connectivity (reaching the gateway) is safe to retry; command execution is not
- **Alternatives**:
  - One generic error: agent can't react appropriately
  - Auto-retry interrupted commands: dangerous for non-idempotent operations

## Risks / Trade-offs

**[Risk: Credential Exposure]** → MCP server holds JumpServer API token. Mitigation: Require token in environment variable or secure config file (not CLI args); rotate tokens regularly; audit token usage in JumpServer logs.

**[Risk: Command Injection in Dangerous Command Filter]** → Regex patterns might miss variants or be overly broad. Mitigation: Curate patterns carefully; start with conservative blocklist; log all blocked commands for audit; allow manual review of edge cases.

**[Risk: Network Latency for Large Batches]** → Executing 100+ commands in parallel may slow JumpServer or overwhelm target servers. Mitigation: Implement optional concurrency limits; batch size validation; provide guidance on batch sizing.

**[Risk: Audit Trail Fragmentation]** → Commands execute through JumpServer, but MCP logs are separate. Mitigation: Log all MCP invocations with correlation IDs; reference MCP logs in JumpServer audit review; provide export integration if needed.

**[Risk: User Confusion on Permission Errors]** → Agent may not know why a command was denied (JumpServer RBAC vs. dangerous command filter). Mitigation: Distinguish error types in response; provide detailed error messages; log reasoning for denials.

**[Trade-off: Flexibility vs. Security]** → More permissive command filters enable more use cases but increase risk. Mitigation: Separate "safe" and "unrestricted" modes; require explicit approval for unrestricted; default to safe.

## Migration Plan

1. **Phase 1 (Initial Release)**: Deploy MCP server to target environment; test with single server, single command. Verify JumpServer audit trail captures executions.
2. **Phase 2 (Stabilization)**: Enable batch operations; test with 5-10 servers. Collect feedback on command patterns that should be blocked/allowed.
3. **Phase 3 (Hardening)**: Refine security filters based on Phase 2 feedback; add transaction support for batch operations; document best practices.
4. **Rollback**: Stop MCP server; all previous manual workflows (direct SSH, JumpServer web UI) remain available. JumpServer is unmodified.

## Resolved Decisions (previously open)

- **Config**: env vars only (`JMS_BASE_URL`, `JMS_API_TOKEN`), with missing-config hints — see Decision 7.
- **Auth**: API token — see Decision 2/7.
- **JumpServer unreachable**: 3 retries then error — see Decision 14.
- **Traffic path**: Agent → JumpServer (HTTP API) for discovery + Agent → JumpServer (SSH jump) → Server for execution. Fixed, no direct SSH.
- **Session model**: stateful hybrid with `run()` shortcut — see Decision 8.
- **Permission check**: not pre-checked; discovered at execution time and returned as `permission_denied` — see Decision 14.
- **Dangerous command config**: regex blacklist, admin-edited only, two tiers — see Decision 10.
- **Approval flow**: blocking poll, 5-min auto-deny, pre-supplied allow list, regex-scoped session exemption — see Decision 11.
- **Audit storage**: local SQLite (command, time, user) — see Decision 12.
- **Output handling**: encoding normalize, binary detect, 100MB cap, compressed download for large batches — see Decision 13.
- **Batch**: pure parallel, partial results (N ok / M failed), 30s progress ticks, cancellable with partial report — see specs/batch-operations.

## Open Questions

- **Question**: What exactly does the JumpServer Ops Job `task-detail` `summary` payload contain — full stdout or already-truncated, who decides the encoding, and how is binary output represented in the JSON? (To be answered by the spike task before finalizing the output-handling implementation.)
  - **Impact**: Determines whether the data-handling encoding/binary/truncation logic operates on raw streams or must adapt to JumpServer's pre-processed `summary` format. The existing `parse_large_log_output` only reads `summary.failures`, so the success-path shape is still unverified.

- **Question**: Where does the human approver act on Tier-2 pending requests — a small web page, a CLI command, or a notification channel? (Decision 11 defines the *behavior* and timeout; the *approver-facing surface* is still to be designed.)
  - **Impact**: Determines an additional UI/CLI surface and how approvals are delivered.

- **Question**: What is the exact download channel for large compressed batch results (local file path returned to caller, a short-lived HTTP endpoint, or JumpServer-hosted)?
  - **Impact**: Affects deployment surface and cleanup/retention of result files.

- **Question**: Where does the SQLite audit DB live, and what is its retention/rotation policy?
  - **Impact**: Disk usage over time, audit completeness.
