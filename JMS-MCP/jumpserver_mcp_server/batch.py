"""Batch operations across multiple hosts (design.md Decision 5 / batch-operations spec).

Pure-parallel execution: each host runs the command independently through the
same security + execution pipeline (``SessionManager.run``), bounded only by the
session manager's concurrency cap. There is NO transaction/rollback — each host's
outcome is independent (spec "Parallel execution without rollback").

Features:
- per-host result collection with a ``"N succeeded, M failed"`` summary
- periodic progress as ``completed/total`` (default every 30s)
- cancellation: stop dispatching new hosts, report completed + not-executed,
  and mark in-flight hosts as ``connection_interrupted`` (status unknown)
- large aggregate results spilled to a compressed file with a download
  reference instead of inline (spec "Large batch result delivered as
  downloadable file" / task 7.4)
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings
from .errors import JumpServerMCPError
from .sessions import SessionManager

logger = getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], "None | Awaitable[None]"]


@dataclass
class BatchHostResult:
    host: str
    status: str  # "succeeded" | "failed" | "not_executed" | "interrupted"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class BatchHandle:
    """Live handle to a running batch; supports cooperative cancellation."""

    batch_id: str
    total: int
    cancelled: bool = False
    completed: int = 0
    results: dict[str, BatchHostResult] = field(default_factory=dict)

    def cancel(self) -> None:
        self.cancelled = True


class BatchExecutor:
    """Run a command across many hosts in parallel with progress + cancellation."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        progress_interval: float = 30.0,
        inline_limit_bytes: int | None = None,
        spill_dir: str | None = None,
    ) -> None:
        self._manager = manager
        self._progress_interval = progress_interval
        # Aggregate results larger than this are written to a compressed file.
        self._inline_limit = (
            inline_limit_bytes
            if inline_limit_bytes is not None
            else settings.batch_inline_limit_bytes
        )
        self._spill_dir = spill_dir or settings.batch_spill_dir
        self._handles: dict[str, BatchHandle] = {}

    def get_handle(self, batch_id: str) -> BatchHandle | None:
        return self._handles.get(batch_id)

    def cancel(self, batch_id: str) -> bool:
        handle = self._handles.get(batch_id)
        if handle is None:
            return False
        handle.cancel()
        return True

    async def run_batch(
        self,
        hosts: list[str],
        command: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
        preapproved_patterns: list[str] | None = None,
        initiating_user: str | None = None,
        timeout: int | None = None,
        chdir: str | None = None,
        progress_cb: ProgressCallback | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute ``command`` on every host in parallel; collect per-host results."""
        if not hosts:
            raise JumpServerMCPError("no hosts specified for batch operation")

        handle = BatchHandle(batch_id=batch_id or uuid.uuid4().hex, total=len(hosts))
        self._handles[handle.batch_id] = handle

        # In-flight tracking so cancellation can mark unfinished hosts correctly.
        in_flight: set[str] = set()

        async def run_one(host: str) -> None:
            if handle.cancelled:
                handle.results[host] = BatchHostResult(host=host, status="not_executed")
                return
            in_flight.add(host)
            try:
                res = await self._manager.run(
                    host,
                    command,
                    user_id=user_id,
                    username=username,
                    preapproved_patterns=preapproved_patterns,
                    initiating_user=initiating_user,
                    timeout=timeout,
                    chdir=chdir,
                )
                status = "succeeded"
                if isinstance(res, dict) and res.get("status") in {
                    "auto_denied", "denied", "queued",
                }:
                    status = "failed"
                handle.results[host] = BatchHostResult(
                    host=host, status=status, result=res
                )
            except JumpServerMCPError as exc:
                handle.results[host] = BatchHostResult(
                    host=host, status="failed", error=exc.to_dict()
                )
            except Exception as exc:  # noqa: BLE001
                handle.results[host] = BatchHostResult(
                    host=host, status="failed",
                    error={"error": "internal_error", "message": repr(exc)},
                )
            finally:
                in_flight.discard(host)
                handle.completed += 1

        async def progress_emitter() -> None:
            if progress_cb is None:
                return
            while handle.completed < handle.total and not handle.cancelled:
                await asyncio.sleep(self._progress_interval)
                await _maybe_await(
                    progress_cb,
                    {
                        "batch_id": handle.batch_id,
                        "completed": handle.completed,
                        "total": handle.total,
                        "progress": f"{handle.completed}/{handle.total}",
                    },
                )

        emitter = asyncio.create_task(progress_emitter())
        # Pure parallel: the SessionManager concurrency cap is the real throttle.
        await asyncio.gather(*(run_one(h) for h in hosts))
        emitter.cancel()

        # Mark any host never recorded (e.g. cancelled before dispatch).
        for host in hosts:
            if host not in handle.results:
                status = "interrupted" if host in in_flight else "not_executed"
                handle.results[host] = BatchHostResult(host=host, status=status)

        # Final progress tick.
        if progress_cb is not None:
            await _maybe_await(
                progress_cb,
                {
                    "batch_id": handle.batch_id,
                    "completed": handle.completed,
                    "total": handle.total,
                    "progress": f"{handle.completed}/{handle.total}",
                },
            )

        return self._summarize(handle)

    def _summarize(self, handle: BatchHandle) -> dict[str, Any]:
        succeeded = [h for h, r in handle.results.items() if r.status == "succeeded"]
        failed = [h for h, r in handle.results.items() if r.status == "failed"]
        not_exec = [h for h, r in handle.results.items() if r.status == "not_executed"]
        interrupted = [h for h, r in handle.results.items() if r.status == "interrupted"]

        per_host = {
            h: {
                "status": r.status,
                "result": r.result,
                "error": r.error,
            }
            for h, r in handle.results.items()
        }

        summary: dict[str, Any] = {
            "batch_id": handle.batch_id,
            "cancelled": handle.cancelled,
            "total": handle.total,
            "summary": f"{len(succeeded)} succeeded, {len(failed)} failed",
            "counts": {
                "succeeded": len(succeeded),
                "failed": len(failed),
                "not_executed": len(not_exec),
                "interrupted": len(interrupted),
            },
        }
        if handle.cancelled:
            summary["message"] = (
                "operation cancelled; completed hosts reported, remaining hosts "
                "were not executed"
            )
            summary["completed_hosts"] = succeeded + failed
            summary["not_executed_hosts"] = not_exec

        # Spill large aggregate result sets to a compressed file.
        encoded = json.dumps(per_host, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._inline_limit:
            download = self._spill(handle.batch_id, per_host)
            summary["results_download"] = download
            summary["results_inline"] = False
        else:
            summary["results"] = per_host
            summary["results_inline"] = True
        return summary

    def _spill(self, batch_id: str, per_host: dict[str, Any]) -> dict[str, Any]:
        """Write aggregate results to a gzip file; return a download reference."""
        out_dir = Path(self._spill_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"batch-{batch_id}-{int(time.time())}.json.gz"
        payload = json.dumps(per_host, ensure_ascii=False, indent=2).encode("utf-8")
        with gzip.open(path, "wb") as fh:
            fh.write(payload)
        return {
            "path": str(path),
            "format": "gzip-json",
            "bytes": path.stat().st_size,
            "note": (
                "Aggregate batch results exceeded the inline limit and were "
                "written to a compressed file."
            ),
        }


async def _maybe_await(cb: ProgressCallback, payload: dict[str, Any]) -> None:
    out = cb(payload)
    if asyncio.iscoroutine(out):
        await out
