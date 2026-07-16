"""Tier-2 human approval flow (design.md Decision 11 / security-controls spec).

When a command lands in ``pending_approval``, the caller **blocks and polls**
for a human decision with a default 5-minute timeout; on expiry the request is
**auto-denied**. A single mechanism serves both interactive humans and headless
automation (which can instead pre-supply allow patterns at session open).

This module owns only the *pending-request registry and waiting*; the
approver-facing surface (how a human says yes/no) is intentionally decoupled —
an admin CLI / small web page / API can call :meth:`ApprovalManager.resolve`.
For automated tests and headless runs, an ``auto_decider`` callback can decide
immediately.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from logging import getLogger
from typing import Any, Awaitable, Callable

from .config import settings

logger = getLogger(__name__)


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    AUTO_DENIED = "auto_denied"


@dataclass
class ApprovalRequest:
    request_id: str
    command: str
    matched_pattern: str
    session_id: str | None
    host: str | None
    runas: str | None
    initiator: str | None
    state: ApprovalState = ApprovalState.PENDING
    approver: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "command": self.command,
            "matched_pattern": self.matched_pattern,
            "session_id": self.session_id,
            "host": self.host,
            "runas": self.runas,
            "initiator": self.initiator,
            "state": self.state.value,
            "approver": self.approver,
        }


# An auto-decider returns True (approve), False (deny), or None (leave pending).
AutoDecider = Callable[[ApprovalRequest], "bool | None | Awaitable[bool | None]"]


class ApprovalManager:
    """Registry of pending Tier-2 approvals with blocking-poll resolution."""

    def __init__(self, auto_decider: AutoDecider | None = None) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()
        self._auto_decider = auto_decider

    async def request_approval(
        self,
        *,
        command: str,
        matched_pattern: str,
        session_id: str | None = None,
        host: str | None = None,
        runas: str | None = None,
        initiator: str | None = None,
        timeout: float | None = None,
    ) -> ApprovalRequest:
        """Register a pending request and block until resolved or timed out.

        On timeout the request is auto-denied. Returns the resolved request
        (its ``state`` reflects the outcome).
        """
        req = ApprovalRequest(
            request_id=uuid.uuid4().hex,
            command=command,
            matched_pattern=matched_pattern,
            session_id=session_id,
            host=host,
            runas=runas,
            initiator=initiator,
        )
        async with self._lock:
            self._pending[req.request_id] = req

        # Optional immediate decision (headless automation / tests).
        if self._auto_decider is not None:
            verdict = self._auto_decider(req)
            if asyncio.iscoroutine(verdict):
                verdict = await verdict
            if verdict is True:
                await self.resolve(req.request_id, approve=True, approver="auto")
            elif verdict is False:
                await self.resolve(req.request_id, approve=False, approver="auto")

        budget = (
            timeout if timeout is not None else settings.approval_timeout_seconds
        )
        try:
            await asyncio.wait_for(req.event.wait(), timeout=budget)
        except asyncio.TimeoutError:
            async with self._lock:
                if req.state == ApprovalState.PENDING:
                    req.state = ApprovalState.AUTO_DENIED
                    logger.info(
                        "approval %s auto-denied after %ss", req.request_id, budget
                    )
                self._pending.pop(req.request_id, None)
            return req

        async with self._lock:
            self._pending.pop(req.request_id, None)
        return req

    async def resolve(
        self, request_id: str, *, approve: bool, approver: str | None = None
    ) -> bool:
        """Approve or deny a pending request. Returns True if it was pending."""
        async with self._lock:
            req = self._pending.get(request_id)
            if req is None or req.state != ApprovalState.PENDING:
                return False
            req.state = ApprovalState.APPROVED if approve else ApprovalState.DENIED
            req.approver = approver
            req.event.set()
            return True

    async def list_pending(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [r.to_dict() for r in self._pending.values() if r.state == ApprovalState.PENDING]
