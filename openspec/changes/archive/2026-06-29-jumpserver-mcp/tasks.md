## 0. Spikes & Existing-Code Alignment

- [x] 0.1 SPIKE: Verify JumpServer Ops Job `summary` response format — capture real `/ops/job-execution/task-detail/{task_id}/` payloads for a normal command, a failing command, large output, and binary output; document what stdout/stderr/exit-code fields exist, who controls encoding, and whether output is pre-truncated
- [x] 0.2 Review existing daemon (`JMS-MCP/jumpserver_mcp_server/`) and inventory reusable pieces (auth, OpenAPI tool generation, `ensure_core_tools`, ops-job create+poll loop in `_execute_large_log_tool`)
- [x] 0.3 Decide how new hand-written tools coexist with the auto-generated OpenAPI tools (naming, filtering, list_tools wiring)

## 1. Project Setup

- [x] 1.1 Confirm/extend MCP server project structure (build on existing FastAPI + FastApiMCP daemon)
- [x] 1.2 Confirm JumpServer API client + API-token/Access-Key auth read from environment (existing `config.py` / `build_jumpserver_auth`)
- [x] 1.3 Add missing-config detection that returns a clear actionable hint when required env vars are absent
- [x] 1.4 Generalize the ops-job create + poll loop (from `_execute_large_log_tool`) into a reusable execution helper
- [x] 1.5 Create a typed configuration loader (idle timeout default 15m, pool size default 10, max output default 100MB, approval timeout default 5m, `policy_mode` default blacklist — all overridable)
- [x] 1.6 Set up logging and error-handling with the five error categories (`jumpserver_unreachable`, `target_unreachable`, `permission_denied`, `command_blocked`, `connection_interrupted`)
- [x] 1.7 Implement 3-retry policy for JumpServer-unreachable (HTTP API) calls only

## 2. Host Discovery Implementation

- [x] 2.1 Wire host queries over `/assets/hosts/` (already registered via `ensure_core_tools`)
- [x] 2.2 Implement hostname search (`search=` param)
- [x] 2.3 Implement IP address search
- [x] 2.4 Add filtering by additional identifiers (OS, asset type, group)
- [x] 2.5 Implement MCP tool for host discovery returning asset id + runas candidates
- [x] 2.6 Add caching layer for host lookup performance

## 3. User Connection Implementation

- [x] 3.1 Retrieve JumpServer-configured users (runas accounts) for a given asset
- [x] 3.2 Return the user list when multiple users exist and none is specified (let caller choose, then re-invoke with chosen user)
- [x] 3.3 Accept a pre-specified `user_id`/`username` mapped to the ops-job `runas` field for automation
- [x] 3.4 Do NOT pre-check RBAC; surface `permission_denied` only when JumpServer rejects at execution time
- [x] 3.5 Implement MCP tool for resolving/selecting the connecting (runas) user

## 4. Session Management Implementation (logical context, no live connection)

- [x] 4.1 Implement `open_session(host, user_id?) → session_id` creating a logical context {host, runas, approved-regex set, last-active} — NO persistent connection
- [x] 4.2 Implement `execute(session_id, command)` that POSTs a new ops job using the context's host + runas and polls for the result
- [x] 4.3 Implement `close_session(session_id)` clearing the context record
- [x] 4.4 Implement `run(host, user_id, command)` convenience wrapper (open+execute+close)
- [x] 4.5 Implement 15-minute idle expiry of the context with per-command timer reset (configurable)
- [x] 4.6 Implement concurrency cap on in-flight ops jobs (default 10, configurable) with queue-and-status responses for overflow

## 5. Command Execution Implementation

- [x] 5.1 Build single-command execution via the ops-job helper (module=shell, args, assets=[asset_id], runas)
- [x] 5.2 Implement command execution with timeout handling (ops-job `timeout` + poll budget)
- [x] 5.3 Capture stdout, stderr, exit code, and execution metadata from the job `summary` (per spike 0.1 findings)
- [x] 5.4 Handle mid-command interruption (poll timeout / job failure): return "execution status unknown, connection interrupted at command X" and never auto-retry
- [x] 5.5 Implement MCP tool for single-command execution

## 6. Security Controls Implementation

- [x] 6.1 Build regex policy engine editable only by the deployment administrator (no MCP tool may modify it)
- [x] 6.2 Implement `policy_mode` switch (blacklist default / whitelist) with Tier-1 destructive blacklist as a permanent floor in both modes
- [x] 6.3 Implement Tier-1 (destructive) detection → hard block with explanatory message, no override
- [x] 6.4 Implement Tier-2 (risky) detection → `pending_approval` status (blacklist mode)
- [x] 6.5 Implement whitelist-mode default-deny (non-whitelisted commands rejected; Tier-1 still hard-blocks)
- [x] 6.6 Implement blocking-poll approval flow with 5-minute default timeout and auto-deny on timeout
- [x] 6.7 Support caller-supplied pre-approved command patterns at `open_session`/`run` time
- [x] 6.8 Implement session-scoped exemption keyed to the exact triggered regex ("allow similar this session")
- [x] 6.9 Create local SQLite audit store recording command, timestamp, initiating user, host, and decision outcome

## 7. Data Handling Implementation

- [x] 7.1 Implement three-stage encoding pipeline (detect → fallback → normalize to UTF-8), adapted to the job `summary` format
- [x] 7.2 Implement binary-stream detection (tar, gzip/zip, openssl, compressed mysqldump, non-UTF-8 stdout) and avoid string handling
- [x] 7.3 Enforce configurable max single-output size (default 100MB) with truncation annotations before and after output
- [x] 7.4 Write large batch result sets to a compressed file and expose a download channel

## 8. Batch Operations Implementation

- [x] 8.1 Design batch operation request format
- [x] 8.2 Implement pure-parallel execution across hosts (one ops job per host, or multi-asset job per spike findings)
- [x] 8.3 Implement per-host result collection returning success/failure counts (e.g. "5 succeeded, 5 failed")
- [x] 8.4 Emit progress updates every 30s as completed/total
- [x] 8.5 Implement cancellation returning completed hosts and marking the rest as not executed
- [x] 8.6 Implement MCP tool for batch operations

## 9. Testing & Validation

- [x] 9.1 Write unit tests for host discovery module
- [x] 9.2 Write unit tests for user connection and logical session management
- [x] 9.3 Write unit tests for command execution and mid-command interruption handling
- [x] 9.4 Write unit tests for two-tier security controls, policy_mode switch, and approval flow
- [x] 9.5 Write unit tests for output encoding, binary detection, and truncation
- [x] 9.6 Write integration tests with mock JumpServer ops-job endpoints
- [x] 9.7 Test batch operations with parallel execution, progress, and cancellation
- [x] 9.8 Security testing: verify Tier-1 hard-block, Tier-2 approval, and whitelist default-deny
- [x] 9.9 Test concurrency limiting and session idle expiry
- [x] 9.10 Performance testing: validate latency and throughput

## 10. Documentation & Examples

- [x] 10.1 Write API reference for all MCP tools
- [x] 10.2 Create usage examples for each capability
- [x] 10.3 Document required environment variables and setup guide
- [x] 10.4 Write security best practices guide (policy_mode, blacklist tiers, approval, audit)
- [x] 10.5 Create batch operation examples
- [x] 10.6 Document error categories and troubleshooting

## 11. Deployment & Release

- [x] 11.1 Set up CI/CD pipeline
- [x] 11.2 Configure container/package distribution (existing Dockerfile)
- [x] 11.3 Create deployment automation
- [x] 11.4 Write release notes
- [x] 11.5 Tag and publish initial release
