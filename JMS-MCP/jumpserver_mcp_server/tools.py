"""Hand-written MCP tool definitions + dispatcher (0.3 coexistence decision).

These capability tools sit alongside the auto-generated OpenAPI tools. They are
defined as explicit ``types.Tool`` objects and appended in ``handle_list_tools``
(same pattern as ``FIND_LARGE_LOG_TOOL``), and dispatched by exact name in
``handle_call_tool`` before falling through to the generic API path.

Naming (verb-noun, no ``operationId`` style, to avoid collisions):
``discover_hosts``, ``resolve_users``, ``open_session``, ``execute``,
``close_session``, ``run_command``, ``batch_execute``.

A single module-level :class:`ToolContext` holds the shared httpx client and the
process-wide :class:`SessionManager`, so session contexts persist across tool
calls within the server process.
"""

from __future__ import annotations

import json
from logging import getLogger
from typing import Any

import httpx
import mcp.types as types

from .batch import BatchExecutor
from .config import missing_config_hint
from .errors import JumpServerMCPError
from .host_discovery import HostDiscovery
from .sessions import SessionExpiredError, SessionManager
from .user_connection import UserConnectionResolver

logger = getLogger(__name__)


def _text(payload: Any) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


DISCOVER_HOSTS = types.Tool(
    name="discover_hosts",
    description=(
        "Search JumpServer host assets by hostname or IP (and optionally filter "
        "by OS / asset type / group). Returns each host's asset id plus its "
        "candidate runas accounts — the inputs open_session/run_command need."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Hostname, IP, or CIDR subnet to search."},
            "os_type": {"type": "string", "description": "Filter by OS (e.g. linux)."},
            "asset_type": {"type": "string", "description": "Filter by asset type."},
            "group": {"type": "string", "description": "Filter by node/group path."},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "default": 0, "minimum": 0},
            "with_accounts": {
                "type": "boolean",
                "default": True,
                "description": "Populate runas_candidates per host (extra API calls).",
            },
        },
    },
)

RESOLVE_USERS = types.Tool(
    name="resolve_users",
    description=(
        "Resolve the connecting (runas) user for an asset. With a user_id/"
        "username, maps it to the runas value; with none and multiple accounts, "
        "returns the candidate list to choose from."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "asset_id": {"type": "string", "description": "JumpServer asset id."},
            "user_id": {"type": "string", "description": "Account id to use (optional)."},
            "username": {"type": "string", "description": "Account username to use (optional)."},
        },
        "required": ["asset_id"],
    },
)

OPEN_SESSION = types.Tool(
    name="open_session",
    description=(
        "Open a logical session context bound to a host + runas user. Returns a "
        "session_id for execute/close_session. If the host has multiple users and "
        "none is given, returns the candidate list instead (needs_selection)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Asset id, hostname, or IP."},
            "user_id": {"type": "string"},
            "username": {"type": "string"},
            "preapproved_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Regex patterns trusted automation may run without Tier-2 prompts.",
            },
            "initiating_user": {"type": "string", "description": "Caller identity for audit."},
        },
        "required": ["host"],
    },
)

EXECUTE = types.Tool(
    name="execute",
    description=(
        "Execute a shell command in an open session. Runs the security policy "
        "(Tier-1 hard block / Tier-2 approval / whitelist), then dispatches an "
        "ops job and returns merged stdout+stderr, the real exit code, and "
        "execution metadata."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Per-command timeout (seconds)."},
            "chdir": {"type": "string", "description": "Working directory for the command."},
        },
        "required": ["session_id", "command"],
    },
)

CLOSE_SESSION = types.Tool(
    name="close_session",
    description="Close a logical session context and release its resources.",
    inputSchema={
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
    },
)

RUN_COMMAND = types.Tool(
    name="run_command",
    description=(
        "One-shot convenience: open a session, execute a single command, and "
        "close it. Same security pipeline as execute. Use for ad-hoc commands "
        "where you don't need a persistent session."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Asset id, hostname, or IP."},
            "command": {"type": "string"},
            "user_id": {"type": "string"},
            "username": {"type": "string"},
            "preapproved_patterns": {"type": "array", "items": {"type": "string"}},
            "initiating_user": {"type": "string"},
            "timeout": {"type": "integer"},
            "chdir": {"type": "string"},
        },
        "required": ["host", "command"],
    },
)

BATCH_EXECUTE = types.Tool(
    name="batch_execute",
    description=(
        "Run one command across many hosts in parallel (each host independent, "
        "no rollback). Returns an 'N succeeded, M failed' summary with per-host "
        "detail. Large aggregate results spill to a compressed download file. "
        "Each host passes the same security policy as run_command."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Asset ids, hostnames, or IPs to run on.",
            },
            "command": {"type": "string"},
            "user_id": {"type": "string"},
            "username": {"type": "string"},
            "preapproved_patterns": {"type": "array", "items": {"type": "string"}},
            "initiating_user": {"type": "string"},
            "timeout": {"type": "integer"},
            "chdir": {"type": "string"},
        },
        "required": ["hosts", "command"],
    },
)


HANDWRITTEN_TOOLS: list[types.Tool] = [
    DISCOVER_HOSTS,
    RESOLVE_USERS,
    OPEN_SESSION,
    EXECUTE,
    CLOSE_SESSION,
    RUN_COMMAND,
    BATCH_EXECUTE,
]

HANDWRITTEN_TOOL_NAMES = frozenset(t.name for t in HANDWRITTEN_TOOLS)


class ToolContext:
    """Process-wide holder for the shared client + session manager."""

    def __init__(self, base_url: str, auth: httpx.Auth | None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, verify=False, auth=auth, timeout=120, trust_env=False
        )
        self._discovery = HostDiscovery(self._client)
        self._resolver = UserConnectionResolver(self._discovery)
        self._sessions = SessionManager(self._client)
        self._batch = BatchExecutor(self._sessions)

    @property
    def discovery(self) -> HostDiscovery:
        return self._discovery

    @property
    def resolver(self) -> UserConnectionResolver:
        return self._resolver

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Route a hand-written tool call to its handler, formatting errors."""
        hint = missing_config_hint()
        if hint is not None:
            return _text({"error": "not_configured", "message": hint})
        try:
            return await self._dispatch(name, arguments)
        except (JumpServerMCPError, SessionExpiredError) as exc:
            return _text(exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as data
            logger.exception("tool %s failed", name)
            return _text({"error": "internal_error", "message": repr(exc)})

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        args = arguments or {}
        if name == "discover_hosts":
            with_accounts = args.get("with_accounts", True)
            method = (
                self._discovery.search_hosts_with_accounts
                if with_accounts
                else self._discovery.search_hosts
            )
            hosts = await method(
                args.get("query"),
                os_type=args.get("os_type"),
                asset_type=args.get("asset_type"),
                group=args.get("group"),
                limit=int(args.get("limit", 50)),
                offset=int(args.get("offset", 0)),
            )
            return _text({"count": len(hosts), "hosts": hosts})

        if name == "resolve_users":
            resolution = await self._resolver.resolve(
                args["asset_id"],
                user_id=args.get("user_id"),
                username=args.get("username"),
            )
            return _text(resolution.to_dict())

        if name == "open_session":
            result = await self._sessions.open_session(
                args["host"],
                user_id=args.get("user_id"),
                username=args.get("username"),
                preapproved_patterns=args.get("preapproved_patterns"),
                initiating_user=args.get("initiating_user"),
            )
            return _text(result)

        if name == "execute":
            result = await self._sessions.execute(
                args["session_id"],
                args["command"],
                timeout=args.get("timeout"),
                chdir=args.get("chdir"),
            )
            return _text(result)

        if name == "close_session":
            return _text(self._sessions.close_session(args["session_id"]))

        if name == "run_command":
            result = await self._sessions.run(
                args["host"],
                args["command"],
                user_id=args.get("user_id"),
                username=args.get("username"),
                preapproved_patterns=args.get("preapproved_patterns"),
                initiating_user=args.get("initiating_user"),
                timeout=args.get("timeout"),
                chdir=args.get("chdir"),
            )
            return _text(result)

        if name == "batch_execute":
            result = await self._batch.run_batch(
                list(args["hosts"]),
                args["command"],
                user_id=args.get("user_id"),
                username=args.get("username"),
                preapproved_patterns=args.get("preapproved_patterns"),
                initiating_user=args.get("initiating_user"),
                timeout=args.get("timeout"),
                chdir=args.get("chdir"),
            )
            return _text(result)

        return _text({"error": "unknown_tool", "message": f"no handler for {name}"})
