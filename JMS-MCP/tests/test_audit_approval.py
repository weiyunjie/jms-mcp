"""Tests for the SQLite audit store and Tier-2 approval flow (task 9.4)."""

import asyncio

import pytest

from jumpserver_mcp_server.approval import ApprovalManager, ApprovalState
from jumpserver_mcp_server.audit import AuditStore


# --- Audit store -------------------------------------------------------------

def test_audit_records_and_reads_back():
    store = AuditStore(":memory:")
    rid = store.record(
        command="rm -rf /", outcome="blocked", initiator="alice",
        host="web-01", runas="ec2-user", tier=1, matched=r"\brm\b",
    )
    assert rid > 0
    rows = store.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["command"] == "rm -rf /"
    assert row["outcome"] == "blocked"
    assert row["initiator"] == "alice"
    assert row["host"] == "web-01"
    assert row["tier"] == 1


def test_audit_newest_first():
    store = AuditStore(":memory:")
    store.record(command="first", outcome="executed")
    store.record(command="second", outcome="executed")
    rows = store.recent()
    assert rows[0]["command"] == "second"
    assert rows[1]["command"] == "first"


def test_audit_records_all_outcomes():
    store = AuditStore(":memory:")
    for outcome in ("blocked", "approved", "auto_denied", "executed"):
        store.record(command=f"cmd-{outcome}", outcome=outcome)
    outcomes = {r["outcome"] for r in store.recent()}
    assert outcomes == {"blocked", "approved", "auto_denied", "executed"}


# --- Approval flow -----------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_auto_approve():
    mgr = ApprovalManager(auto_decider=lambda req: True)
    req = await mgr.request_approval(command="rm -f x", matched_pattern=r"\brm\b")
    assert req.state is ApprovalState.APPROVED
    assert req.approver == "auto"


@pytest.mark.asyncio
async def test_approval_auto_deny():
    mgr = ApprovalManager(auto_decider=lambda req: False)
    req = await mgr.request_approval(command="rm -f x", matched_pattern=r"\brm\b")
    assert req.state is ApprovalState.DENIED


@pytest.mark.asyncio
async def test_approval_times_out_to_auto_denied():
    mgr = ApprovalManager()  # no decider -> nobody answers
    req = await mgr.request_approval(
        command="rm -f x", matched_pattern=r"\brm\b", timeout=0.1
    )
    assert req.state is ApprovalState.AUTO_DENIED


@pytest.mark.asyncio
async def test_approval_external_resolve():
    mgr = ApprovalManager()

    async def approve_soon():
        await asyncio.sleep(0.05)
        pending = await mgr.list_pending()
        await mgr.resolve(pending[0]["request_id"], approve=True, approver="bob")

    task = asyncio.create_task(approve_soon())
    req = await mgr.request_approval(
        command="rm -f x", matched_pattern=r"\brm\b", timeout=2.0
    )
    await task
    assert req.state is ApprovalState.APPROVED
    assert req.approver == "bob"


@pytest.mark.asyncio
async def test_resolve_unknown_request_returns_false():
    mgr = ApprovalManager()
    assert await mgr.resolve("nonexistent", approve=True) is False
