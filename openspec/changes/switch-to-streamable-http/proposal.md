## Why

The server currently exposes MCP only over the legacy SSE transport (GET
handshake + `POST /sse/messages/?session_id=`). Newer MCP clients (e.g. Hermes)
default to Streamable HTTP and cannot complete the SSE handshake — they issue
`POST /sse` and get 401, so they cannot connect at all. The MCP specification
now positions Streamable HTTP as the standard transport and treats SSE as
backwards-compatibility only. Moving to Streamable HTTP restores compatibility
with modern clients and aligns the server with the current spec.

## What Changes

- **BREAKING**: Replace the SSE transport with MCP Streamable HTTP as the sole
  transport. The endpoint stays at the configured mount path but changes
  semantics: a single path handling `GET`/`POST`/`DELETE` instead of an SSE
  handshake plus a separate `/messages/` POST path.
- **BREAKING**: The `?session_id=` query parameter is removed. Session
  continuity moves to the `Mcp-Session-Id` HTTP header, per the Streamable HTTP
  spec.
- Upgrade `fastapi-mcp` `0.3.3 → 0.4.0` (the release that introduces
  `mount_http()`) and let it pull the newer `mcp` SDK it requires.
- Replace the project's custom `mount()` override with a Streamable-HTTP mount
  (`mount_http()`), and remove the SSE-specific `is_auth_session()` /
  `sse_transport` internals.
- Rework the `check_api_key` middleware: keep Bearer `API_KEY` gateway
  validation but drop the SSE `session_id`-based bypass, adapting to how the
  HTTP transport carries session identity.
- Update deployment docs, `.env.example`, `docker-compose.yml`, and the
  `base_path` default (currently `/sse`) to reflect the HTTP endpoint.

## Capabilities

### New Capabilities
<!-- None. Transport is an existing concern; no new capability is introduced. -->

### Modified Capabilities
- `session-management`: The transport-level session mechanism changes from
  SSE (`?session_id=` query param tied to the SSE read-stream registry) to
  Streamable HTTP (`Mcp-Session-Id` header managed by the HTTP session
  manager). Logical command sessions (`open_session`/`execute`/`close_session`)
  are unchanged; only the wire-level MCP session/transport requirements change.

## Impact

- **Code**: `jumpserver_mcp_server/server.py` — `JumpServerOpenapiMCP`
  (custom `mount()` and `is_auth_session()`), the module-level mount call, and
  the `check_api_key` middleware. Bearer-token passthrough into
  `experimental_capabilities.session_token` is revisited.
- **Dependencies**: `fastapi-mcp==0.4.0` (was `0.3.3`) plus the newer `mcp`
  SDK; `pyproject.toml` pin updated.
- **APIs / wire protocol**: MCP endpoint semantics change (Streamable HTTP,
  `Mcp-Session-Id` header). The MCP gateway `API_KEY` Bearer requirement is
  retained.
- **Clients**: Existing SSE-based client configs (e.g. the Mac Claude Code
  entry pointing at `/mcp` as SSE) must be re-added as Streamable HTTP.
- **Config / deploy**: `base_path` default, `.env.example`, compose, and
  `docs/setup.md` updated for the HTTP endpoint.
- **JumpServer**: No change — this is purely the MCP-facing transport.
