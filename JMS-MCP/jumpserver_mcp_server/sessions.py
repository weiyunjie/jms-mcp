"""Logical session management (design.md Decision 8/9).

A "session" here is NOT a live connection — execution runs through stateless
ops jobs (spike 0.1). A session is a *context record*::

    { session_id, asset_id, runas, host_name, approved_regexes,
      preapproved_patterns, last_active_at }

``open_session`` creates the context (resolving the runas user via the
user-connection layer), ``execute`` dispatches one ops job per command using
that context (running it through the security policy + approval + audit
pipeline), and ``close_session`` discards it. ``run`` is the open+execute+close
one-shot wrapper.

Lifecycle:
- **Idle timeout** (default 15m, configurable): a context with no command for
  the idle period is discarded; later use of its id returns "session expired".
- **Concurrency cap** (default 10, configurable): caps concurrent *in-flight
  ops jobs*, not open contexts. ``execute`` calls beyond the cap receive a
  "queued" status instead of blocking forever.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

import httpx

from .approval import ApprovalManager, ApprovalState
from .audit import AuditStore
from .command_execution import CommandResult, CommandRunner
from .config import settings
from .errors import CommandBlockedError, JumpServerMCPError
from .host_discovery import HostDiscovery
from .ops_executor import OpsJobExecutor
from .security_policy import Decision, PolicyEngine
from .user_connection import UserConnectionResolver

logger = getLogger(__name__)


@dataclass
class SessionContext:
    session_id: str
    asset_id: str
    runas: str
    host_name: str | None = None
    approved_regexes: set[str] = field(default_factory=set)
    preapproved_patterns: list[str] = field(default_factory=list)
    last_active_at: float = field(default_factory=time.monotonic)
    initiating_user: str | None = None

    def touch(self) -> None:
        self.last_active_at = time.monotonic()


class SessionExpiredError(JumpServerMCPError):
    # Reuse the connection_interrupted-ish surface but keep its own message.
    from .errors import ErrorCategory

    category = ErrorCategory.CONNECTION_INTERRUPTED


class SessionManager:
    """Owns logical session contexts and the per-command execution pipeline."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        policy: PolicyEngine | None = None,
        audit: AuditStore | None = None,
        approvals: ApprovalManager | None = None,
        idle_timeout: float | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        self._client = client
        self._executor = OpsJobExecutor(client)
        self._runner = CommandRunner(self._executor)
        self._discovery = HostDiscovery(client)
        self._resolver = UserConnectionResolver(self._discovery)
        self._policy = policy or PolicyEngine()
        self._audit = audit or AuditStore()
        self._approvals = approvals or ApprovalManager()
        self._idle_timeout = (
            idle_timeout if idle_timeout is not None
            else settings.session_idle_timeout_seconds
        )
        self._max_concurrent = (
            max_concurrent if max_concurrent is not None
            else settings.max_concurrent_jobs
        )
        self._sessions: dict[str, SessionContext] = {}
        self._inflight = 0
        self._lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    def _expire_idle(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, ctx in self._sessions.items()
            if now - ctx.last_active_at > self._idle_timeout
        ]
        for sid in expired:
            logger.info("session %s expired (idle > %ss)", sid, self._idle_timeout)
            self._sessions.pop(sid, None)

    async def open_session(
        self,
        host: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
        preapproved_patterns: list[str] | None = None,
        initiating_user: str | None = None,
    ) -> dict[str, Any]:
        """Create a logical session context bound to host + runas.

        If the host has multiple users and none was specified, returns a
        selection payload (``needs_selection``) instead of creating a context.
        """
        self._expire_idle()
        asset_id, host_name = await self._resolve_host(host)
        if asset_id is None:
            return {
                "needs_selection": False,
                "error": "host_not_found",
                "message": (
                    f"No JumpServer asset matched {host!r}. Use host discovery "
                    "to find the asset id, hostname, or IP."
                ),
            }

        resolution = await self._resolver.resolve(
            asset_id, user_id=user_id, username=username
        )
        if resolution.needs_selection:
            payload = resolution.to_dict()
            payload["host_name"] = host_name
            return payload
        if resolution.runas is None:
            # No usable account (e.g. asset has none, or specified user missing).
            payload = resolution.to_dict()
            payload["host_name"] = host_name
            return payload

        runas = resolution.runas
        session_id = uuid.uuid4().hex
        ctx = SessionContext(
            session_id=session_id,
            asset_id=asset_id,
            runas=runas,
            host_name=host_name,
            preapproved_patterns=list(preapproved_patterns or []),
            initiating_user=initiating_user,
        )
        self._sessions[session_id] = ctx
        return {
            "session_id": session_id,
            "asset_id": asset_id,
            "runas": runas,
            "host_name": ctx.host_name,
            "account": resolution.account,
            "needs_selection": False,
        }

    async def _resolve_host(self, host: str) -> tuple[str | None, str | None]:
        """Map a host identifier (asset id, hostname, or IP) to (asset_id, name).

        A 32/36-char hex/UUID is treated as an asset id directly; otherwise the
        value is searched against JumpServer hosts (hostname or IP).
        """
        stripped = host.replace("-", "")
        if len(stripped) == 32 and all(c in "0123456789abcdefABCDEF" for c in stripped):
            summary = await self._discovery.get_host(host)
            if summary is not None:
                return host, summary.get("name")
            return host, None
        matches = await self._discovery.search_hosts(host, limit=1)
        if matches:
            return matches[0].get("id"), matches[0].get("name")
        return None, None

    def close_session(self, session_id: str) -> dict[str, Any]:
        existed = self._sessions.pop(session_id, None) is not None
        return {"session_id": session_id, "closed": existed}

    def _get_context(self, session_id: str) -> SessionContext:
        self._expire_idle()
        ctx = self._sessions.get(session_id)
        if ctx is None:
            raise SessionExpiredError(
                f"session {session_id} expired or not found",
                detail={"session_id": session_id},
            )
        return ctx

    # --- execution ---------------------------------------------------------

    async def execute(
        self,
        session_id: str,
        command: str,
        *,
        timeout: int | None = None,
        chdir: str | None = None,
        approval_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Evaluate, (maybe) approve, then dispatch ``command`` for a session."""
        ctx = self._get_context(session_id)
        ctx.touch()

        gate = await self._gate_command(ctx, command, approval_timeout=approval_timeout)
        if gate is not None:
            return gate  # blocked / pending-denied / queued

        result = await self._dispatch(ctx, command, timeout=timeout, chdir=chdir)
        ctx.touch()
        return result

    async def _gate_command(
        self,
        ctx: SessionContext,
        command: str,
        *,
        approval_timeout: float | None,
    ) -> dict[str, Any] | None:
        """Run the security policy + approval flow.

        Returns a response dict if the command must NOT execute (blocked or
        denied), or ``None`` if it is cleared to run.
        """
        verdict = self._policy.evaluate(
            command,
            exempt_patterns=ctx.approved_regexes,
            preapproved_patterns=ctx.preapproved_patterns,
        )
        if verdict.decision is Decision.BLOCK:
            self._audit.record(
                command=command, host=ctx.host_name or ctx.asset_id,
                initiator=ctx.initiating_user, runas=ctx.runas,
                outcome="blocked", tier=verdict.tier,
                matched=verdict.matched_pattern,
                session_id=ctx.session_id, detail=verdict.message,
            )
            raise CommandBlockedError(verdict.message, detail=verdict.to_dict())

        if verdict.decision is Decision.PENDING_APPROVAL:
            self._audit.record(
                command=command, host=ctx.host_name or ctx.asset_id,
                initiator=ctx.initiating_user, runas=ctx.runas,
                outcome="pending_approval", tier=verdict.tier,
                matched=verdict.matched_pattern, session_id=ctx.session_id,
            )
            req = await self._approvals.request_approval(
                command=command, host=ctx.host_name or ctx.asset_id,
                initiator=ctx.initiating_user, runas=ctx.runas,
                matched_pattern=verdict.matched_pattern,
                session_id=ctx.session_id,
                timeout=approval_timeout,
            )
            self._audit.record(
                command=command, host=ctx.host_name or ctx.asset_id,
                initiator=ctx.initiating_user, runas=ctx.runas,
                outcome=req.state.value, tier=verdict.tier,
                matched=verdict.matched_pattern, approver=req.approver,
                session_id=ctx.session_id,
            )
            if req.state is not ApprovalState.APPROVED:
                return {
                    "status": req.state.value,  # auto_denied / denied
                    "command": command,
                    "matched_pattern": verdict.matched_pattern,
                    "approval_id": req.request_id,
                }
            # Approved: exempt the exact triggered regex for the rest of session.
            if verdict.matched_pattern:
                ctx.approved_regexes.add(verdict.matched_pattern)
        return None

    async def _dispatch(
        self,
        ctx: SessionContext,
        command: str,
        *,
        timeout: int | None,
        chdir: str | None,
    ) -> dict[str, Any]:
        """Concurrency-gated dispatch of the command as an ops job."""
        async with self._lock:
            if self._inflight >= self._max_concurrent:
                return {
                    "status": "queued",
                    "command": command,
                    "message": (
                        f"at concurrency cap ({self._max_concurrent} in-flight "
                        "ops jobs); retry shortly"
                    ),
                    "inflight": self._inflight,
                }
            self._inflight += 1
        try:
            result: CommandResult = await self._runner.run(
                command=command,
                asset_id=ctx.asset_id,
                runas=ctx.runas,
                host_name=ctx.host_name,
                timeout=timeout,
                chdir=chdir,
            )
        finally:
            async with self._lock:
                self._inflight -= 1

        self._audit.record(
            command=command, host=ctx.host_name or ctx.asset_id,
            initiator=ctx.initiating_user, runas=ctx.runas,
            outcome="executed", session_id=ctx.session_id,
            detail=f"exit_code={result.exit_code}",
        )
        payload = result.to_dict()
        payload["session_id"] = ctx.session_id
        return payload

    # --- one-shot ----------------------------------------------------------

    async def run(
        self,
        host: str,
        command: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
        preapproved_patterns: list[str] | None = None,
        initiating_user: str | None = None,
        timeout: int | None = None,
        chdir: str | None = None,
        approval_timeout: float | None = None,
    ) -> dict[str, Any]:
        """open + execute + close for a single command."""
        opened = await self.open_session(
            host, user_id=user_id, username=username,
            preapproved_patterns=preapproved_patterns,
            initiating_user=initiating_user,
        )
        if opened.get("needs_selection"):
            return opened
        session_id = opened["session_id"]
        try:
            return await self.execute(
                session_id, command, timeout=timeout, chdir=chdir,
                approval_timeout=approval_timeout,
            )
        finally:
            self.close_session(session_id)

    @property
    def active_count(self) -> int:
        self._expire_idle()
        return len(self._sessions)
