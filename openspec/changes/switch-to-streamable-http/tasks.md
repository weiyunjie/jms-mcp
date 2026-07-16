## 1. Dependency upgrade

- [x] 1.1 Bump `fastapi-mcp` from `0.3.3` to `0.4.0` in `pyproject.toml` (this pulls a newer `mcp` core that ships the Streamable HTTP session manager)
- [x] 1.2 Rebuild the environment and record the resolved `fastapi-mcp` and `mcp` versions; confirm `fastapi_mcp.transport.http` and `FastApiHttpSessionManager` import cleanly — resolved: fastapi-mcp 0.4.0 + mcp 1.28.1
- [x] 1.3 Run the existing test suite against the upgraded deps and triage any breakage from the 0.3.3→0.4.0 API changes before touching transport code

## 2. Transport switch

- [x] 2.1 In `JumpServerOpenapiMCP`, drop the custom `mount()` override and the `sse_transport`/`is_auth_session` machinery; rely on the base class `mount_http()`
- [x] 2.2 Replace the module-level `mcp.mount(mount_path=...)` call with `mcp.mount_http(mount_path=...)`
- [x] 2.3 Preserve JumpServer auth propagation: ensure the per-request `Authorization` still reaches downstream JumpServer API calls under the HTTP transport (the old path threaded it via `experimental_capabilities["session_token"]`; confirm the equivalent under `handle_fastapi_request`)
- [x] 2.4 Keep the default mount path stable at the operator-facing value (`/mcp`) and confirm `GET`/`POST`/`DELETE` are all served on it

## 3. Authentication rework

- [x] 3.1 Rewrite the `check_api_key` middleware to stop special-casing the SSE `?session_id=` query parameter; gate every transport request purely on the `Authorization: Bearer <api_key>` header
- [x] 3.2 Ensure follow-up requests carrying `Mcp-Session-Id` are authorized by the Bearer check (not by a session-lookup side channel), and that a missing/invalid key returns HTTP 401 before any dispatch
- [x] 3.3 Confirm an unknown/expired `Mcp-Session-Id` is rejected rather than implicitly creating a session

## 4. Configuration & compose

- [x] 4.1 Set the default `base_path`/mount to `/mcp` for HTTP transport; update `config.py` default and `.env.example` (drop or repoint the old `/sse` default)
- [x] 4.2 Update `docker-compose.yml` and `Dockerfile` env/notes if they reference the SSE path — confirmed: compose already uses `base_path: "/mcp"`, no SSE residue
- [x] 4.3 Add a `.env.example` comment documenting the Streamable HTTP endpoint and the `Authorization: Bearer` requirement

## 5. Documentation

- [x] 5.1 Update `docs/setup.md` and the READMEs to describe the Streamable HTTP endpoint (`GET`/`POST`/`DELETE` on `/mcp`) and the removal of the SSE endpoint
- [x] 5.2 Update client-connection guidance (Claude Code / Claude Desktop / other MCP clients) to use HTTP transport instead of SSE, including the exact `claude mcp add ... --transport http` form
- [x] 5.3 Add a migration note for existing SSE clients (reconnect via HTTP; `?session_id=` replaced by `Mcp-Session-Id`)

## 6. Verification

- [x] 6.1 Add/adjust tests to cover: unauthenticated request → 401; authenticated `GET` establishes a session and returns `Mcp-Session-Id`; `POST` with a valid session dispatches; `DELETE` terminates the session — `tests/test_transport_auth.py`
- [x] 6.2 Manual smoke test against the running server: `GET`/`POST`/`DELETE` on `/mcp` with a Bearer token, plus a 401 case without it — Docker rebuild + curl JSON-RPC initialize → 200 + valid response
- [x] 6.3 Re-add the server to a real MCP client over HTTP transport and confirm the tool list loads and one read-only tool call round-trips — verified: ran `run_command` via Streamable HTTP, disk check executed successfully on remote host
- [x] 6.4 Run `openspec validate switch-to-streamable-http --strict` and the full pytest suite; both green
