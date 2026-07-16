# 0.2 / 0.3 — Existing-Code Alignment

## 0.2 Inventory of reusable pieces (`JMS-MCP/jumpserver_mcp_server/`)

| Piece | Location | Reuse plan |
|---|---|---|
| Access-Key (HTTP Signature) + Bearer auth | `server.py` `JumpServerAccessKeyAuth`, `BearerAuth`, `build_jumpserver_auth()` | **Reuse as-is.** Already reads from `config.py`/env. Move into a shared `jumpserver_client` module so non-server code can import without triggering the module-level swagger fetch (the spike had to work around that import side effect). |
| Typed settings loader | `config.py` `Settings(BaseSettings)` | **Extend.** Add the new typed config (idle timeout, pool size, max output, approval timeout, `policy_mode`, audit DB path) — task 1.5. |
| OpenAPI → MCP tool generation | `server.py` `JumpServerOpenapiMCP.setup_server` via `convert_openapi_to_mcp_tools` | **Reuse.** Auto-generated read tools (`assets_hosts_list`, etc.) stay. |
| `ensure_core_tools` | `server.py` | **Reuse.** Already registers `/assets/hosts/`, `/assets/assets/`, `/ops/jobs/`, `/ops/job-execution/task-detail/{task_id}/`. Host discovery (Section 2) builds on `/assets/hosts/`. |
| Ops-job create + poll loop | `server.py` `_execute_large_log_tool` | **Generalize → reusable helper** (task 1.4). This is the core execution primitive. Current version hardcodes the asset, runas, and the read-only log command. |
| Output parsing | `readonly_tools.py` `parse_large_log_output` | **Generalize.** It already implements the only working output-retrieval pattern (stdout→stderr + `exit 1` → `summary.failures`), confirmed by spike 0.1. |
| `list_tools` / `call_tool` wiring + `FIND_LARGE_LOG_TOOL` pattern | `server.py` `handle_list_tools`, `handle_call_tool` | **Reuse pattern.** Hand-written tools are appended to the auto-generated list and dispatched by name before falling through to the generic API path. |
| Existing tests | `tests/test_readonly_log_tool.py` | **Keep.** Model new unit tests on these. |

Known fixed-asset constants in `readonly_tools.py` (`TARGET_ASSET_ID`, `TARGET_ASSET_ADDRESS`, `TARGET_RUNAS`) are the spike target and a usable default for first-light testing, but new tools must accept asset/runas as parameters (Sections 2–5), not hardcode them.

## 0.3 How hand-written tools coexist with auto-generated OpenAPI tools

**Decision:** Keep the existing hybrid model and formalize it.

- **Auto-generated tools** (from swagger) cover read-only discovery (`assets_hosts_list`, `assets_hosts_read`, `assets_assets_list`, …). They keep their JumpServer-derived `operationId` names.
- **Hand-written tools** (the new capability tools — host discovery wrapper, user resolution, `open_session`/`execute`/`close_session`/`run`, single-command exec, batch) are defined as explicit `types.Tool` objects and **appended** in `handle_list_tools` (same pattern as `FIND_LARGE_LOG_TOOL` today).
- **Naming:** hand-written tools use a clear verb-noun scheme without the auto-generated `operationId` style, to avoid collisions: `discover_hosts`, `resolve_users`, `open_session`, `execute`, `close_session`, `run_command`, `batch_execute`. (`run` from the spec is exposed as `run_command` to avoid ambiguity.)
- **Dispatch:** `handle_call_tool` checks hand-written tool names first (by exact match), then falls through to the generic `_execute_api_tool` path for auto-generated tools. This is already how `FIND_LARGE_LOG_TOOL` is handled.
- **Filtering:** no change to `_filter_tools`; hand-written tools bypass it since they are added after filtering.
- **Module split:** to keep `server.py` manageable and importable without side effects, new logic lands in dedicated modules (`jumpserver_client.py`, `ops_executor.py`, `config.py` ext, `policy.py`, `sessions.py`, `output.py`, `audit.py`, `tools.py`) and `server.py` wires them into `list_tools`/`call_tool`.
