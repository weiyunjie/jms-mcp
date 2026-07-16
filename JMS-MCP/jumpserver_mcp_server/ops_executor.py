"""Reusable JumpServer Ops Job executor.

Generalizes the create+poll loop that previously lived inline in
``server._execute_large_log_tool`` into one helper every higher-level tool
(command execution, sessions, batch) can share.

Key behaviors:
- 3-retry policy on *JumpServer-unreachable* HTTP failures only (transport
  errors / 5xx on the API itself) — design.md Decision 14. Command execution
  is NEVER auto-retried here; only the API calls that reach JumpServer are.
- Poll budget: a job is polled until ``is_finished`` or the budget elapses.
  Exhausting the budget on an in-flight job is a ``connection_interrupted``
  (status unknown), not a success.
- Result classification maps the Ansible play-recap ``summary`` buckets
  (``ok`` / ``failures`` / ``dark`` / ``skipped``) onto our error categories.

Output parsing (the stdout-capture wrapper / chunked base64 reassembly from
spike-0.1-findings.md) lives in the command-execution layer, not here — this
helper only owns dispatch, polling, retry, and bucket classification.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

import httpx

from .config import settings
from .errors import (
    ConnectionInterruptedError,
    JumpServerUnreachableError,
    PermissionDeniedError,
    TargetUnreachableError,
)

logger = getLogger(__name__)

HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_SERVER_ERROR = 500


@dataclass
class OpsJobResult:
    """Outcome of a single polled ops job."""

    task_id: str | None
    job_id: str | None
    status: dict[str, Any] | None
    is_finished: bool
    is_success: bool
    summary: dict[str, Any] = field(default_factory=dict)
    time_cost: float | None = None
    raw_detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok_hosts(self) -> list[str]:
        return list(self.summary.get("ok") or [])

    @property
    def failures(self) -> dict[str, str]:
        return dict(self.summary.get("failures") or {})

    @property
    def dark(self) -> dict[str, str]:
        return dict(self.summary.get("dark") or {})


def build_ops_job_payload(
    *,
    name: str,
    args: str,
    asset_ids: list[str],
    runas: str,
    timeout: int | None = None,
    chdir: str | None = None,
    comment: str = "mcp ops job",
    runas_policy: str = "privileged_first",
) -> dict[str, Any]:
    """Construct an adhoc shell ops-job request body."""
    payload: dict[str, Any] = {
        "name": name,
        "type": "adhoc",
        "module": "shell",
        "args": args,
        "assets": asset_ids,
        "runas_policy": runas_policy,
        "runas": runas,
        "timeout": timeout if timeout is not None else settings.ops_job_timeout_seconds,
        "instant": True,
        "run_after_save": True,
        "use_parameter_define": False,
        "is_periodic": False,
        "comment": comment,
    }
    if chdir:
        payload["chdir"] = chdir
    return payload


class OpsJobExecutor:
    """Dispatch + poll JumpServer ops jobs over a shared httpx client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _post_with_retry(self, url: str, json: dict[str, Any]) -> httpx.Response:
        """POST with the 3-retry JumpServer-unreachable policy (1.7).

        Retries only transport errors and 5xx (JumpServer itself unhealthy).
        A 4xx is a deterministic rejection — surfaced immediately, not retried.
        """
        attempts = max(1, settings.jumpserver_unreachable_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.post(url, json=json)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "JumpServer POST %s transport error (attempt %d/%d): %r",
                    url, attempt, attempts, exc,
                )
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                continue
            if resp.status_code >= HTTP_SERVER_ERROR:
                last_exc = JumpServerUnreachableError(
                    f"JumpServer returned {resp.status_code} for {url}",
                    detail={"status_code": resp.status_code, "body": resp.text[:500]},
                )
                logger.warning(
                    "JumpServer POST %s -> %d (attempt %d/%d)",
                    url, resp.status_code, attempt, attempts,
                )
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                continue
            return resp
        if isinstance(last_exc, JumpServerUnreachableError):
            raise last_exc
        raise JumpServerUnreachableError(
            f"JumpServer unreachable after {attempts} attempts: {url}",
            detail={"cause": repr(last_exc)},
        )

    async def _get_with_retry(self, url: str) -> httpx.Response:
        attempts = max(1, settings.jumpserver_unreachable_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.get(url)
            except httpx.TransportError as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                continue
            if resp.status_code >= HTTP_SERVER_ERROR:
                last_exc = JumpServerUnreachableError(
                    f"JumpServer returned {resp.status_code} for {url}",
                    detail={"status_code": resp.status_code},
                )
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                continue
            return resp
        if isinstance(last_exc, JumpServerUnreachableError):
            raise last_exc
        raise JumpServerUnreachableError(
            f"JumpServer unreachable after {attempts} attempts: {url}",
            detail={"cause": repr(last_exc)},
        )

    async def dispatch(self, payload: dict[str, Any]) -> str:
        """Create an ops job; return its task_id."""
        resp = await self._post_with_retry("/ops/jobs/", json=payload)
        if resp.status_code == HTTP_FORBIDDEN:
            raise PermissionDeniedError(
                "JumpServer rejected the ops job (permission denied)",
                detail={"body": resp.text[:500]},
            )
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise JumpServerUnreachableError(
                f"Unexpected {resp.status_code} creating ops job",
                detail={"status_code": resp.status_code, "body": resp.text[:500]},
            )
        task_id = resp.json().get("task_id")
        if not task_id:
            raise JumpServerUnreachableError(
                "Ops job create returned no task_id",
                detail={"body": resp.text[:500]},
            )
        return task_id

    async def poll(
        self,
        task_id: str,
        *,
        max_attempts: int | None = None,
        interval: float | None = None,
    ) -> OpsJobResult:
        """Poll task-detail until finished or the poll budget is exhausted.

        Budget exhaustion on an unfinished job raises ``connection_interrupted``
        — the command's status is unknown and we never claim success.
        """
        attempts = max_attempts if max_attempts is not None else settings.ops_poll_max_attempts
        wait = interval if interval is not None else settings.ops_poll_interval_seconds
        detail: dict[str, Any] = {}
        for _ in range(max(1, attempts)):
            resp = await self._get_with_retry(
                f"/ops/job-execution/task-detail/{task_id}/"
            )
            if resp.status_code >= HTTP_BAD_REQUEST:
                raise JumpServerUnreachableError(
                    f"Unexpected {resp.status_code} reading task-detail",
                    detail={"status_code": resp.status_code},
                )
            detail = resp.json()
            if detail.get("is_finished"):
                return OpsJobResult(
                    task_id=task_id,
                    job_id=detail.get("job_id"),
                    status=detail.get("status"),
                    is_finished=True,
                    is_success=bool(detail.get("is_success")),
                    summary=detail.get("summary") or {},
                    time_cost=detail.get("time_cost"),
                    raw_detail=detail,
                )
            await asyncio.sleep(wait)
        raise ConnectionInterruptedError(
            f"execution status unknown, connection interrupted at task {task_id}",
            detail={"task_id": task_id, "poll_attempts": attempts},
        )

    async def run(self, payload: dict[str, Any], **poll_kwargs: Any) -> OpsJobResult:
        """Dispatch then poll a single ops job to completion."""
        task_id = await self.dispatch(payload)
        return await self.poll(task_id, **poll_kwargs)

    @staticmethod
    def classify_host_failure(result: OpsJobResult, host: str) -> None:
        """Raise the right error category for a host that did not land in ``ok``.

        ``dark`` (connection-level) -> target_unreachable.
        ``failures`` text mentioning permission -> permission_denied.
        Otherwise the caller treats it as a normal non-zero command exit.
        """
        if host in result.dark:
            raise TargetUnreachableError(
                f"target host {host} unreachable",
                detail={"host": host, "reason": result.dark.get(host)},
            )
        failure_text = result.failures.get(host, "")
        lowered = failure_text.lower()
        if "permission denied" in lowered or "not authorized" in lowered:
            raise PermissionDeniedError(
                f"permission denied on {host}",
                detail={"host": host, "reason": failure_text[:500]},
            )
