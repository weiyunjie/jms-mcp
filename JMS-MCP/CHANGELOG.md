# Release Notes — v0.1.0

First release of the JumpServer MCP server: programmatic, security-controlled
server management through JumpServer's Ops Job API, exposed as MCP tools.

## Highlights

- **Host discovery** — search assets by hostname / IP / CIDR, filter by OS /
  type / group, returns asset id + candidate runas accounts. Short-lived cache.
- **User (runas) resolution** — auto-select a sole account, map a supplied
  user/username, or return candidates for selection. RBAC is discovered lazily
  at execution time, never pre-checked.
- **Logical sessions** — `open_session` / `execute` / `close_session` plus a
  one-shot `run_command`. A session is a context record (host, runas,
  approved-regex set, last-active), not a live connection — each command is a
  separate Ops Job. 15-minute idle expiry + a concurrency cap with queueing.
- **Command execution** — recovers merged stdout+stderr, the real exit code,
  and binary-safe output via a chunked-base64 wrapper that works around the
  JumpServer Ops Job `summary` having no real output channel (see below).
- **Two-tier security policy** — permanent Tier-1 destructive hard-block floor
  under both modes; switchable `blacklist` (default-allow) / `whitelist`
  (default-deny) main gate; Tier-2 risky → human approval (blocking poll,
  5-minute auto-deny); pre-approved patterns + session-scoped exemptions.
- **Data handling** — three-stage encoding normalization to UTF-8, binary-stream
  detection, configurable output cap with truncation annotations.
- **Batch operations** — pure-parallel across hosts (no rollback), "N succeeded,
  M failed" summary with per-host detail, 30s progress ticks, cancellation, and
  compressed-file spill for large aggregate results.
- **Audit** — local SQLite log of every security decision (command, time,
  initiator, host, runas, outcome).

## Key implementation note (spike 0.1)

JumpServer's ad-hoc Ops Job `task-detail.summary` is an Ansible play-recap with
only host-status buckets — **no stdout/stderr/exit-code channel**, and the only
text-bearing field (`failures`) has a hard ~256 KiB ceiling above which the value
is silently dropped. Command output is therefore captured with a wrapper that
merges streams, tags the real exit code, base64-encodes, and splits into
sub-256 KiB parts fetched via separate jobs and reassembled client-side. See
`openspec/changes/jumpserver-mcp/spike-0.1-findings.md`.

## Configuration

Config is read from environment (or `.env`). Required: `JUMPSERVER_URL`, auth
(`ACCESS_KEY_ID`+`ACCESS_KEY_SECRET` or `API_TOKEN`), and the MCP gateway
`API_KEY`. See `docs/setup.md` for the full list and defaults.

## Known limitations

- Per-command output transport is bounded by the ~256 KiB JumpServer field; very
  large output is fetched in parts and capped at `MAX_OUTPUT_BYTES` (default
  100 MB) on reassembly.
- Approval surface is decoupled: the server registers pending Tier-2 requests but
  the approver-facing UI/CLI is left to the deployment.
- Batch scheduling (future/recurring) from the spec is not implemented in 0.1.
