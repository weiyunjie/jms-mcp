## Context

The MCP server is built on FastAPI + `fastapi-mcp`. Version `0.3.3` (currently
pinned) ships only the SSE transport via `fastapi_mcp.transport.sse`. The
project subclasses `FastApiMCP` as `JumpServerOpenapiMCP` and **overrides
`mount()`** to register two routes:

- `GET {mount_path}` — SSE handshake (`connect_sse`), which also stuffs the
  caller's `Authorization` header into
  `experimental_capabilities.session_token` so downstream JumpServer calls can
  reuse it.
- `POST {mount_path}/messages/` — the SSE message channel, addressed by a
  `?session_id=<uuid>` query parameter.

A global `check_api_key` HTTP middleware guards everything: if a
`?session_id=` query param is present it validates it against
`mcp.is_auth_session()` (which inspects `sse_transport._read_stream_writers`);
otherwise it enforces the `Authorization: Bearer <API_KEY>` gateway check.

The default `base_path` is `/sse`. Modern clients (Hermes and others) default
to **Streamable HTTP**: they `POST` to the mount path directly instead of doing
an SSE `GET` handshake, so they receive 401/404 and never connect. Verified
against the running server: `curl -H "Authorization: Bearer <key>" GET /sse`
succeeds and streams `event: endpoint`, while every Hermes `POST /sse` is
rejected.

`fastapi-mcp 0.4.0` (confirmed via its release notes and source) adds
`mount_http()` implementing the MCP Streamable HTTP spec, deprecates `mount()`,
and introduces stateful session management for HTTP. The HTTP endpoint is a
single `api_route` handling `GET`/`POST`/`DELETE`, backed by
`FastApiHttpSessionManager.handle_fastapi_request()`, with auth wired through
FastAPI `dependencies` rather than manual header parsing inside the handler.

## Goals / Non-Goals

**Goals:**
- Serve MCP exclusively over Streamable HTTP so modern clients connect without
  transport-specific configuration.
- Preserve the existing MCP gateway auth: callers still present
  `Authorization: Bearer <API_KEY>`.
- Preserve all seven logical tools and the JumpServer Access Key / Ops Job
  execution path unchanged.
- Keep the deployment surface stable (same container, same port, mount at a
  documented path) and update docs/config accordingly.

**Non-Goals:**
- Retaining SSE. This change intentionally removes the SSE transport
  (`mount_sse()` is available in 0.4.0 but is out of scope; SSE is dropped).
- Adding OAuth / the fastapi-mcp `AuthConfig` proxy flow. The simple Bearer
  gateway check is retained.
- Changing the JumpServer-facing authentication (Access Key HMAC signing) or
  any tool behavior.
- Changing the chunked-base64 Ops Job output capture.

## Decisions

### D1: Upgrade to `fastapi-mcp==0.4.0` and use `mount_http()`
`mount_http()` is the officially recommended, spec-compliant Streamable HTTP
mount. **Alternative considered**: hand-rolling a Streamable HTTP transport on
top of the raw `mcp` SDK to avoid the dependency bump. Rejected — it
duplicates what 0.4.0 provides, and 0.4.0 is a clean minor upgrade. The upgrade
also pulls a newer `mcp` SDK; pin `fastapi-mcp==0.4.0` and let its dependency
resolution choose the compatible `mcp` version, then lock what resolves.

### D2: Replace the custom `mount()` override rather than keep overriding
The current subclass overrides `mount()` purely to (a) register SSE routes and
(b) inject `Authorization` into `experimental_capabilities`. With HTTP,
`mount_http()` already registers the correct route and manages sessions.
**Decision**: drop the `mount()` override and call `mcp.mount_http(mount_path=...)`.
Remove `is_auth_session()` and the `sse_transport` attribute. **Alternative**:
override `mount_http()` to re-inject the auth header — deferred to D3 depending
on whether tools still need the caller token.

### D3: Bearer gateway auth via middleware, not the SSE session bypass
Keep the `check_api_key` middleware enforcing `Authorization: Bearer <API_KEY>`
on the mount path, but **remove the `?session_id=` bypass branch** (it depended
on SSE internals). Streamable HTTP carries session identity in the
`Mcp-Session-Id` header, managed inside `FastApiHttpSessionManager`; the gateway
check only needs to validate the static `API_KEY` bearer on incoming requests.
Confirm during implementation whether the initialize handshake and subsequent
requests both carry the `Authorization` header (they do for a static-bearer
client) so no request is spuriously rejected. **Open item** tracked below for
the `experimental_capabilities.session_token` passthrough (see D4).

### D4: Handle the caller-token passthrough
Today the SSE handler copies the caller `Authorization` into
`experimental_capabilities.session_token`, and `server.py:390` can reuse it as
the JumpServer `Authorization` when no Access Key auth is configured. In this
deployment JumpServer auth comes from the Access Key pair (server-side),
**not** the caller token, so this passthrough is not required for correct
operation here. **Decision**: preserve current behavior only if trivially
portable to the HTTP handler; otherwise drop the passthrough and rely on the
configured Access Key, documenting the removal. Validate that Access-Key-based
JumpServer calls still succeed end-to-end.

### D5: Change `base_path` default `/sse → /mcp`
The path no longer denotes SSE. **Decision**: default `base_path=/mcp` for
clarity and to match the documented endpoint. Existing deployments overriding
`base_path` via env are unaffected; document the rename.

## Risks / Trade-offs

- **[Breaking: existing SSE clients stop working]** → The Mac Claude Code entry
  and any other SSE config must be re-added as Streamable HTTP. Documented in
  the migration plan and README; unavoidable given SSE is being removed.
- **[Dependency bump regressions]** (`fastapi-mcp` 0.3.3→0.4.0 + newer `mcp`)
  → Run the full test suite; smoke-test tool listing and one live read-only
  command end-to-end before merging. Pin exact resolved versions.
- **[Auth middleware over-blocks HTTP handshake]** — if the HTTP transport
  issues internal requests without the bearer, the middleware could 401 them.
  → Verify the initialize/POST/GET/DELETE cycle against the running server with
  a real bearer; adjust the middleware matcher to the mount path only.
- **[Losing the caller-token passthrough]** (D4) → Confirmed non-critical for
  Access-Key deployments; verify end-to-end and document. If a deployment
  relied on caller-token JumpServer auth, that path is dropped.
- **[`mount()` deprecation churn]** → 0.4.0 only deprecates (not removes)
  `mount()`, but we stop using it entirely, so no runtime warning in our path.

## Migration Plan

1. Bump `fastapi-mcp` to `0.4.0` in `pyproject.toml`; reinstall; record the
   `mcp` version that resolves and pin it.
2. Refactor `server.py`: remove the `mount()` override, `is_auth_session()`,
   and `sse_transport`; call `mcp.mount_http(mount_path=...)`.
3. Update `check_api_key` to drop the `session_id` bypass and guard the HTTP
   mount path with the Bearer `API_KEY` check.
4. Resolve D4 (keep or drop token passthrough) and implement accordingly.
5. Change `base_path` default to `/mcp`; update `.env.example`,
   `docker-compose.yml`, `docs/setup.md`, and README connection instructions.
6. Update/extend tests for the new transport and middleware behavior.
7. Rebuild the image, restart, smoke-test: tool listing + one read-only
   command via a Streamable HTTP client.
8. Re-add the client entry (`claude mcp add ... --transport http`) and confirm
   connection.

**Rollback**: revert the `pyproject.toml` pin and `server.py`/config changes
(single commit) to restore the SSE transport; re-add SSE client configs.

## Open Questions

- Which exact `mcp` SDK version does `fastapi-mcp==0.4.0` resolve to in this
  environment, and does it need an explicit floor in `pyproject.toml`?
- Does any current consumer depend on the `experimental_capabilities.session_token`
  passthrough (D4)? Assumed no for Access-Key deployments — confirm before
  removing.
- Should `mount_sse()` be retained in parallel for a deprecation window? Current
  scope says no (full replacement); revisit only if a client cannot move.
