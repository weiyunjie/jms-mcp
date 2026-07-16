"""Batch operations tests (task 9.7): parallel execution, counts, spill, cancel."""

import base64
import json as _json

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


def _host(asset_id: str, name: str) -> dict:
    return {
        "id": asset_id,
        "name": name,
        "address": f"10.0.0.{asset_id[-1]}",
        "platform": {"name": "Linux", "type": "linux"},
        "type": {"value": "linux"},
        "accounts_amount": 1,
    }


_ACCOUNT = {
    "id": "acc-1",
    "name": "ec2-user",
    "username": "ec2-user",
    "privileged": False,
    "secret_type": {"value": "ssh_key"},
}

# Map asset id -> host name for the mock.
HOSTS = {"asset-1": "web-01", "asset-2": "web-02", "asset-3": "web-03"}


class MultiHostFake:
    """Ops-job mock that emits a per-host stdout via the failures envelope."""

    def __init__(self, stdout: bytes = b"done", exit_code: int = 0):
        self._b64 = base64.b64encode(
            stdout + f"\n{RC_MARKER}{exit_code}\n".encode()
        ).decode()
        self._counter = 0
        self._pending: dict[str, tuple[str, str]] = {}

    def handle_create(self, request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        args = body["args"]
        asset = body["assets"][0]
        host_name = HOSTS.get(asset, "web-01")
        self._counter += 1
        task_id = f"task-{self._counter}"
        if "part." in args and "cat " in args:
            payload = self._b64
        elif "__JMSMCP_META__" in args:
            payload = f"__JMSMCP_META__:parts=1;size={len(self._b64)};tmp=/tmp/t"
        else:
            payload = ""
        self._pending[task_id] = (host_name, payload)
        return httpx.Response(201, json={"task_id": task_id})

    def handle_detail(self, request: httpx.Request) -> httpx.Response:
        task_id = request.url.path.rstrip("/").split("/")[-1]
        host_name, payload = self._pending.get(task_id, ("web-01", ""))
        failures = (
            {host_name: f"shell: {payload};non-zero return code"} if payload else {}
        )
        return httpx.Response(
            200,
            json={
                "task_id": task_id,
                "job_id": "job-x",
                "is_finished": True,
                "is_success": True,
                "time_cost": 0.1,
                "summary": {"ok": [], "failures": failures, "dark": {}, "skipped": []},
            },
        )


def _mock(fake: MultiHostFake) -> None:
    def list_hosts(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("search", "")
        matches = [
            _host(aid, name) for aid, name in HOSTS.items() if q in (aid, name) or not q
        ]
        # search by exact asset id used as host ref
        if q in HOSTS:
            matches = [_host(q, HOSTS[q])]
        return httpx.Response(200, json={"results": matches})

    respx.get(f"{BASE}/assets/hosts/").mock(side_effect=list_hosts)
    respx.get(url__regex=rf"{BASE}/assets/hosts/.+/").mock(
        side_effect=lambda r: httpx.Response(
            200, json=_host(r.url.path.rstrip("/").split("/")[-1],
                            HOSTS.get(r.url.path.rstrip("/").split("/")[-1], "web-01"))
        )
    )
    respx.get(f"{BASE}/accounts/accounts/").mock(
        return_value=httpx.Response(200, json={"results": [_ACCOUNT]})
    )
    respx.post(f"{BASE}/ops/jobs/").mock(side_effect=fake.handle_create)
    respx.get(url__regex=rf"{BASE}/ops/job-execution/task-detail/.+").mock(
        side_effect=fake.handle_detail
    )


def _manager(client: httpx.AsyncClient) -> SessionManager:
    return SessionManager(
        client,
        policy=PolicyEngine(),
        audit=AuditStore(":memory:"),
        approvals=ApprovalManager(),
    )


@pytest.mark.asyncio
@respx.mock
async def test_batch_parallel_counts():
    _mock(MultiHostFake(stdout=b"ok", exit_code=0))
    async with httpx.AsyncClient(base_url=BASE) as client:
        batch = BatchExecutor(_manager(client), progress_interval=0.01)
        out = await batch.run_batch(["asset-1", "asset-2", "asset-3"], "echo ok")
    assert out["counts"]["succeeded"] == 3
    assert out["summary"] == "3 succeeded, 0 failed"
    assert out["results_inline"] is True


@pytest.mark.asyncio
@respx.mock
async def test_batch_progress_ticks():
    _mock(MultiHostFake())
    ticks = []
    async with httpx.AsyncClient(base_url=BASE) as client:
        batch = BatchExecutor(_manager(client), progress_interval=0.01)
        await batch.run_batch(
            ["asset-1", "asset-2"], "echo ok",
            progress_cb=lambda p: ticks.append(p["progress"]),
        )
    # final tick always emitted as completed/total
    assert ticks[-1] == "2/2"


@pytest.mark.asyncio
@respx.mock
async def test_batch_empty_hosts_errors():
    _mock(MultiHostFake())
    from jumpserver_mcp_server.errors import JumpServerMCPError

    async with httpx.AsyncClient(base_url=BASE) as client:
        batch = BatchExecutor(_manager(client))
        with pytest.raises(JumpServerMCPError):
            await batch.run_batch([], "echo ok")


@pytest.mark.asyncio
@respx.mock
async def test_batch_spill_to_compressed_file(tmp_path):
    _mock(MultiHostFake(stdout=b"x" * 100))
    async with httpx.AsyncClient(base_url=BASE) as client:
        batch = BatchExecutor(
            _manager(client),
            progress_interval=0.01,
            inline_limit_bytes=10,
            spill_dir=str(tmp_path),
        )
        out = await batch.run_batch(["asset-1", "asset-2"], "echo x")
    assert out["results_inline"] is False
    assert out["results_download"]["format"] == "gzip-json"
    # file actually exists and is gzip
    import gzip
    from pathlib import Path

    path = Path(out["results_download"]["path"])
    assert path.exists()
    decoded = _json.loads(gzip.open(path, "rb").read())
    assert set(decoded.keys()) == {"asset-1", "asset-2"}
