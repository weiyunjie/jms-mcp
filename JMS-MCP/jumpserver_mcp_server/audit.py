"""Local SQLite audit store (design.md Decision 12 / security-controls spec).

Records security-relevant events — command text, timestamp, initiating user,
host, and decision outcome — to a local SQLite file. Zero-dependency and
file-based; complements (does not replace) JumpServer's own audit trail.

Outcomes recorded: ``allowed``, ``blocked`` (Tier-1 / whitelist deny),
``pending_approval``, ``approved``, ``denied``, ``auto_denied``, ``executed``,
``error``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from logging import getLogger
from typing import Any

from .config import settings

logger = getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    initiator    TEXT,
    host         TEXT,
    runas        TEXT,
    command      TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    tier         INTEGER,
    matched      TEXT,
    approver     TEXT,
    session_id   TEXT,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_outcome ON audit_events(outcome);
"""


class AuditStore:
    """Thread-safe SQLite audit log.

    A single connection guarded by a lock — writes are infrequent (one per
    security decision) so contention is a non-issue, and this keeps the store
    safe to share across the async server's threads.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or settings.audit_db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(
        self,
        *,
        command: str,
        outcome: str,
        initiator: str | None = None,
        host: str | None = None,
        runas: str | None = None,
        tier: int | None = None,
        matched: str | None = None,
        approver: str | None = None,
        session_id: str | None = None,
        detail: str | None = None,
    ) -> int:
        """Insert one audit event; returns its row id."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_events "
                "(ts, initiator, host, runas, command, outcome, tier, matched, "
                " approver, session_id, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, initiator, host, runas, command, outcome, tier, matched,
                    approver, session_id, detail,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent audit events (newest first)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, initiator, host, runas, command, outcome, tier, "
                "matched, approver, session_id, detail "
                "FROM audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
