"""Performance / throughput validation (task 9.10).

These are bounded micro-benchmarks against a respx-mocked JumpServer with a
small injected per-request latency. They validate two properties that matter
for the design, not absolute wall-clock numbers (which depend on the host):

1. The session concurrency cap actually bounds in-flight ops jobs.
2. A batch over many hosts runs *in parallel* — total time is far below the
   sum of per-host latencies — so the executor is not accidentally serial.

Kept fast (sub-second) so they can run in CI; they assert relative behavior
with generous margins rather than brittle exact timings.
"""

import asyncio
import base64
import time

import httpx
import pytest
import respx

from jumpserver_mcp_server.approval import ApprovalManager
from jumpserver_mcp_server.audit import AuditStore
from jumpserver_mcp_server.batch import BatchExecutor
from jumpserver_mcp_server.command_execution import RC_MARKER
from jumpserver_mcp_server.security_policy import PolicyEngine
from jumpserver_mcp_server.sessions import SessionManager

BASE = "http://jms.test/api/v1"
PER_REQUEST_LATENCY = 0.02  # 20ms injected into every ops-job call


def _b64_payload(stdout: bytes = b"ok", exit_code: int = 0) -> str:
    return base64.b64encode(stdout + f"\n{RC_MARKER}{exit_code}\n".encode()).decode()


class LatentJumpServer:
    """Mock that injects a fixed latency and emulates the single-part wrapper."""

    def __init__(self, latency: float = PER_REQUEST_LATENCY):
        self.latency = latency
        self._counter = 0
        self._pending: dict[str, str] = {}
        self.max_concurrent_seen = 0
        self._inflight = 0
        self._b64 = _b64_payload()
        self._tmp = "/tmp/jmsmcp.perf"

    async def handle_create(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        self._inflight += 1
        self.max_concurrent_seen = max(self.max_concurrent_seen, self._inflight)
        try:
            await asyncio.sleep(self.latency)
            body = _json.loads(request.content)
            args = body["args"]
            self._counter += 1
            task_id = f"task-{self._counter}"
            if "part." in args and "cat " in args:
                payload = self._b64
            elif "__JMSMCP_META__" in args:
                payload = f"__JMSMCP_META__:parts=1;size={len(self._b64)};tmp={self._tmp}"
            else:
                payload = ""
            self._pending[task_id] = payload
            return httpx.Response(201, json={"task_id": task_id})
        finally:
            self._inflight -= 1

    async def handle_detail(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.latency)
        task_id = request.url.path.rstrip("/").split("/")[-1]
        payload = self._pending.get(task_id, "")
        failures = {"web-01": f"shell: {payload};non-zero return code"} if payload else {}
        return httpx.Response(
            200,
            json={
                "task_id": task_id,
                "job_id": "job-x",
                "is_finished": True,
                "is_success": True,
                "status": {"value": "success"},
                "time_cost": 0.01,
                "summary": {"ok": [], "failures": failures, "dark": {}, "skipped": []},
            },
        )


_HOST = {
    "id": "asset-1",
    "name": "web-01",
    "address": "10.0.0.5",
    "platform": {"name": "Linux", "type": "linux"},
    "type": {"value": "linux"},
    "accounts_amount": 1,
}
_ACCOUNT = {"id": "acc-1", "name": "ec2-user", "username": "ec2-user"}


def _mock(fake: LatentJumpServer) -> None:
    respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"results": [_HOST]})
    )
    respx.get(f"{BASE}/accounts/accounts/").mock(
        return_value=httpx.Response(200, json={"results": [_ACCOUNT]})
    )
    respx.post(f"{BASE}/ops/jobs/").mock(side_effect=fake.handle_create)
    respx.get(url__regex=rf"{BASE}/ops/job-execution/task-detail/.+").mock(
        side_effect=fake.handle_detail
    )


def _manager(client: httpx.AsyncClient, **kw) -> SessionManager:
    return SessionManager(
        client,
        policy=PolicyEngine(),
        audit=AuditStore(":memory:"),
        approvals=ApprovalManager(),
        **kw,
    )


@pytest.mark.asyncio
@respx.mock
async def test_batch_runs_in_parallel_not_serial():
    """A batch over N hosts must be far faster than N sequential runs."""
    fake = LatentJumpServer(latency=0.02)
    _mock(fake)
    n_hosts = 8
    hosts = [f"asset-{i}" for i in range(n_hosts)]
    async with httpx.AsyncClient(base_url=BASE) as client:
        batch = BatchExecutor(_manager(client))
        start = time.monotonic()
        out = await batch.run_batch(hosts, "echo hi")
        elapsed = time.monotonic() - start

    assert out["counts"]["succeeded"] == n_hosts
    # Each host = dispatch + detail + part-create + part-detail + cleanup ≈ 5
    # serial requests * 0.02s = 0.1s/host; 8 serial hosts would be ~0.8s.
    # Parallel should be a fraction of that. Generous ceiling to avoid flakiness.
    assert elapsed < 0.5, f"batch took {elapsed:.3f}s — looks serial, not parallel"


@pytest.mark.asyncio
@respx.mock
async def test_concurrency_cap_bounds_inflight_jobs():
    """The session manager must never exceed its configured concurrency cap."""
    fake = LatentJumpServer(latency=0.03)
    _mock(fake)
    cap = 3
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client, max_concurrent=cap)
        opened = await mgr.open_session("asset-1")
        sid = opened["session_id"]

        async def one():
            return await mgr.execute(sid, "echo hi")

        # Fire more than the cap at once; some should come back "queued".
        results = await asyncio.gather(*(one() for _ in range(cap * 3)))

    statuses = [r.get("status") for r in results]
    # Whatever ran did so within the cap.
    assert fake.max_concurrent_seen <= cap, (
        f"saw {fake.max_concurrent_seen} concurrent jobs, cap was {cap}"
    )
    # At least some calls were admitted (ran) — the cap doesn't deadlock.
    assert any(s == "ok" for s in statuses)
