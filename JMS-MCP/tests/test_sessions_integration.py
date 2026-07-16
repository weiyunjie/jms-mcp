"""Integration tests with mocked JumpServer ops-job endpoints (tasks 9.2, 9.3, 9.6, 9.9).

These exercise the user-connection resolver, the command-execution wrapper +
chunked-output reassembly, and the session pipeline (policy + approval + audit +
concurrency) against a respx-mocked JumpServer that emulates the real ops-job
``summary.failures`` envelope discovered in spike 0.1.
"""

import base64

import httpx
import pytest
import respx

from jumpserver_mcp_server.approval import ApprovalManager
from jumpserver_mcp_server.audit import AuditStore
from jumpserver_mcp_server.command_execution import RC_MARKER, list_part_names
from jumpserver_mcp_server.errors import CommandBlockedError
from jumpserver_mcp_server.host_discovery import HostDiscovery
from jumpserver_mcp_server.security_policy import PolicyEngine
from jumpserver_mcp_server.sessions import SessionManager, SessionExpiredError
from jumpserver_mcp_server.user_connection import UserConnectionResolver

BASE = "http://jms.test/api/v1"
ASSET = "asset-1"
HOST_NAME = "web-01"

_HOST = {
    "id": ASSET,
    "name": HOST_NAME,
    "address": "10.0.0.5",
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


class FakeJumpServer:
    """Emulates ops-job create + task-detail, materializing the wrapper output.

    The dispatch wrapper builds base64(combined+RC) on the host and splits it
    into parts. We can't run shell here, so we emulate the *effect*: for a known
    command we return a single staged part whose content is the base64 we'd have
    produced, and report it through ``summary.failures`` exactly like the bastion.
    """

    def __init__(self, stdout: bytes = b"", exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self._tmp = "/tmp/jmsmcp.test"
        self._b64 = base64.b64encode(
            self.stdout + f"\n{RC_MARKER}{self.exit_code}\n".encode()
        ).decode()
        self._counter = 0

    def handle_create(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        args = body["args"]
        self._counter += 1
        task_id = f"task-{self._counter}"
        # Decide what failures payload the poll will return for this job.
        if "part." in args and "cat " in args:
            # part fetch: emit the whole base64 as one part
            payload = self._b64
        elif "rm -rf" in args:
            payload = ""  # never actually dispatched (blocked locally)
        elif "__JMSMCP_META__" in args:
            # dispatch wrapper: report 1 part
            payload = (
                f"__JMSMCP_META__:parts=1;size={len(self._b64)};tmp={self._tmp}"
            )
        else:
            payload = ""
        self._pending = {task_id: payload}
        return httpx.Response(201, json={"task_id": task_id})

    def handle_detail(self, request: httpx.Request) -> httpx.Response:
        task_id = request.url.path.rstrip("/").split("/")[-1]
        payload = getattr(self, "_pending", {}).get(task_id, "")
        failures = (
            {HOST_NAME: f"shell: {payload};non-zero return code"} if payload else {}
        )
        return httpx.Response(
            200,
            json={
                "task_id": task_id,
                "job_id": "job-x",
                "status": {"value": "success"},
                "is_finished": True,
                "is_success": True,
                "time_cost": 0.1,
                "summary": {"ok": [], "failures": failures, "dark": {}, "skipped": []},
            },
        )


def _mock_common(fake: FakeJumpServer) -> None:
    respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"results": [_HOST]})
    )
    respx.get(url__regex=rf"{BASE}/assets/hosts/.+/").mock(
        return_value=httpx.Response(200, json=_HOST)
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
        policy=kw.pop("policy", PolicyEngine()),
        audit=kw.pop("audit", AuditStore(":memory:")),
        approvals=kw.pop("approvals", ApprovalManager()),
        **kw,
    )


# --- user connection (9.2) -------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_resolver_single_account_autoselects():
    _mock_common(FakeJumpServer())
    async with httpx.AsyncClient(base_url=BASE) as client:
        resolver = UserConnectionResolver(HostDiscovery(client, cache_ttl=0))
        res = await resolver.resolve(ASSET)
    assert res.needs_selection is False
    assert res.runas == "ec2-user"


@pytest.mark.asyncio
@respx.mock
async def test_resolver_multiple_accounts_needs_selection():
    respx.get(f"{BASE}/accounts/accounts/").mock(
        return_value=httpx.Response(
            200,
            json={"results": [_ACCOUNT, dict(_ACCOUNT, id="acc-2", username="root")]},
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as client:
        resolver = UserConnectionResolver(HostDiscovery(client, cache_ttl=0))
        res = await resolver.resolve(ASSET)
    assert res.needs_selection is True
    assert len(res.candidates) == 2


# --- command execution end to end (9.3, 9.6) -------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_session_execute_recovers_stdout_and_exit_code():
    fake = FakeJumpServer(stdout=b"hello-world", exit_code=0)
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        opened = await mgr.open_session(ASSET)
        result = await mgr.execute(opened["session_id"], "echo hello-world")
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello-world"
    assert result["host"] == HOST_NAME


@pytest.mark.asyncio
@respx.mock
async def test_session_execute_nonzero_exit_code():
    fake = FakeJumpServer(stdout=b"boom", exit_code=3)
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        result = await mgr.run(ASSET, "false; echo boom")
    assert result["exit_code"] == 3


# --- security pipeline (9.4) -----------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_session_tier1_blocked_never_dispatches():
    fake = FakeJumpServer()
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        opened = await mgr.open_session(ASSET)
        with pytest.raises(CommandBlockedError):
            await mgr.execute(opened["session_id"], "rm -rf /")


@pytest.mark.asyncio
@respx.mock
async def test_session_tier2_autodenied_on_timeout():
    fake = FakeJumpServer()
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        opened = await mgr.open_session(ASSET)
        result = await mgr.execute(
            opened["session_id"], "rm -f /var/log/x.log", approval_timeout=0.05
        )
    assert result["status"] == "auto_denied"


@pytest.mark.asyncio
@respx.mock
async def test_session_tier2_preapproved_runs():
    fake = FakeJumpServer(stdout=b"ok", exit_code=0)
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        # Pre-approve the exact tier-2 regex for rm -f.
        result = await mgr.run(
            ASSET,
            "rm -f /var/log/x.log",
            preapproved_patterns=[r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*f"],
            approval_timeout=0.05,
        )
    assert result.get("exit_code") == 0


# --- session lifecycle (9.9) -----------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_closed_session_is_rejected():
    fake = FakeJumpServer()
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client)
        opened = await mgr.open_session(ASSET)
        mgr.close_session(opened["session_id"])
        with pytest.raises(SessionExpiredError):
            await mgr.execute(opened["session_id"], "echo hi")


@pytest.mark.asyncio
@respx.mock
async def test_idle_timeout_expires_session():
    fake = FakeJumpServer()
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client, idle_timeout=0.0)
        opened = await mgr.open_session(ASSET)
        # idle_timeout=0 means any elapsed time expires it.
        import time as _t

        _t.sleep(0.01)
        with pytest.raises(SessionExpiredError):
            await mgr.execute(opened["session_id"], "echo hi")


# --- mid-command interruption (9.3) ----------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_poll_budget_exhaustion_is_connection_interrupted():
    """An ops job that never finishes within the poll budget surfaces as
    connection_interrupted (status unknown) and is never claimed as success."""
    from jumpserver_mcp_server.errors import ConnectionInterruptedError
    from jumpserver_mcp_server.ops_executor import OpsJobExecutor

    respx.post(f"{BASE}/ops/jobs/").mock(
        return_value=httpx.Response(201, json={"task_id": "stuck-1"})
    )
    respx.get(url__regex=rf"{BASE}/ops/job-execution/task-detail/.+").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "stuck-1",
                "is_finished": False,  # never completes
                "summary": {"ok": [], "failures": {}, "dark": {}, "skipped": []},
            },
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as client:
        executor = OpsJobExecutor(client)
        task_id = await executor.dispatch({"args": "sleep 999"})
        with pytest.raises(ConnectionInterruptedError) as exc:
            await executor.poll(task_id, max_attempts=2, interval=0)
        assert "connection interrupted" in str(exc.value)


# --- concurrency cap with queueing (9.9) -----------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_concurrency_cap_returns_queued():
    """execute() beyond the in-flight cap returns a 'queued' status rather than
    dispatching the job."""
    fake = FakeJumpServer(stdout=b"hi")
    _mock_common(fake)
    async with httpx.AsyncClient(base_url=BASE) as client:
        mgr = _manager(client, max_concurrent=0)  # every dispatch is over cap
        opened = await mgr.open_session(ASSET)
        result = await mgr.execute(opened["session_id"], "echo hi")
        assert result["status"] == "queued"
        assert result["inflight"] == 0
