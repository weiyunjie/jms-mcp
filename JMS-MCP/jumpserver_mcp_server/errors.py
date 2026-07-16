"""Error classification for the JumpServer MCP.

All operational failures are surfaced to callers as one of five distinct,
machine-distinguishable categories (design.md Decision 14) so an agent can
branch on *why* something failed:

- ``jumpserver_unreachable``  — could not reach the JumpServer HTTP API
  (retryable; the API layer retries before raising this).
- ``target_unreachable``      — JumpServer reached, but the target host was
  unreachable / connection-level failure (Ansible ``dark`` bucket).
- ``permission_denied``       — JumpServer rejected the operation for the
  runas user at execution time (RBAC, discovered lazily, never pre-checked).
- ``command_blocked``         — local security policy blocked the command
  (Tier-1 hard block, whitelist deny, or Tier-2 auto-deny).
- ``connection_interrupted``  — an in-flight command's status is unknown
  (poll timeout / job failure mid-execution). NEVER auto-retried.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    JUMPSERVER_UNREACHABLE = "jumpserver_unreachable"
    TARGET_UNREACHABLE = "target_unreachable"
    PERMISSION_DENIED = "permission_denied"
    COMMAND_BLOCKED = "command_blocked"
    CONNECTION_INTERRUPTED = "connection_interrupted"


class JumpServerMCPError(Exception):
    """Base error carrying a machine-distinguishable category and detail."""

    category: ErrorCategory

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.category.value,
            "message": self.message,
            "detail": self.detail,
        }


class JumpServerUnreachableError(JumpServerMCPError):
    category = ErrorCategory.JUMPSERVER_UNREACHABLE


class TargetUnreachableError(JumpServerMCPError):
    category = ErrorCategory.TARGET_UNREACHABLE


class PermissionDeniedError(JumpServerMCPError):
    category = ErrorCategory.PERMISSION_DENIED


class CommandBlockedError(JumpServerMCPError):
    category = ErrorCategory.COMMAND_BLOCKED


class ConnectionInterruptedError(JumpServerMCPError):
    category = ErrorCategory.CONNECTION_INTERRUPTED
