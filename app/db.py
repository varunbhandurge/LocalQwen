"""
Local SQLite persistence for ba-account-swarm.

Uses Python's built-in ``sqlite3`` — no ORM, no cloud dependency.
All tables are created automatically on first import if they don't exist.

Tables
------
account_context  – key/value store keyed per caller (user / channel).
hitl_approvals   – Human‑in‑the‑Loop approval requests and outcomes.
swarm_audit_logs – append‑only swarm execution trace.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

_DB_PATH = Path(__file__).resolve().parent.parent / "ba_account_swarm.db"


def get_db_path() -> Path:
    """Return the configured database path (overridable via env)."""
    env = _read_env_dotenv()
    override = env.get("DB_PATH", "").strip()
    return Path(override) if override else _DB_PATH


def _read_env_dotenv() -> Dict[str, str]:
    """Minimal .env reader — avoids requiring python‑dotenv as a dep."""
    from pathlib import Path as _P

    env: Dict[str, str] = {}
    for p in (_P(".env"), _P(Path(__file__).resolve().parent.parent / ".env")):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    return env


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create tables if they do not already exist."""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
    finally:
        conn.close()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    caller      TEXT NOT NULL,
    key_name    TEXT NOT NULL,
    value_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL   NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(caller, key_name)
);

CREATE TABLE IF NOT EXISTS hitl_approvals (
    id          TEXT PRIMARY KEY,
    caller      TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    description TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL CHECK(status IN ('pending','approved','Denied','expired')),
    requested_at  REAL NOT NULL DEFAULT (strftime('%s','now')),
    answered_at REAL,
    answerer   TEXT,
    expires_at REAL
);

CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_approvals(status);

CREATE TABLE IF NOT EXISTS swarm_audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    timestamp   REAL NOT NULL DEFAULT (strftime('%s','now')),
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_swarm_session ON swarm_audit_logs(session_id);
"""


# --------------------------------------------------------------------------- #
# Account‑context helpers                                                     #
# --------------------------------------------------------------------------- #

def save_context(caller: str, key: str, value: Any, db_path: Optional[Path] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO account_context (caller,key_name,value_json,updated_at)"
            " VALUES (?,?,?,?)",
            (caller, key, json.dumps(value), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_context(caller: str, key: str, default: Any = None, db_path: Optional[Path] = None) -> Any:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM account_context WHERE caller=? AND key_name=?",
            (caller, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else default
    finally:
        conn.close()


def delete_context(caller: str, key: str, db_path: Optional[Path] = None) -> bool:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM account_context WHERE caller=? AND key_name=?",
            (caller, key),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# HITL helpers                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class HitlRequest:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    caller: str = ""
    tool_name: str = ""
    description: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    requested_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    answerer: Optional[str] = None
    expires_at: float = 0.0


def create_hitl_request(
    caller: str,
    tool_name: str,
    description: str,
    payload: Optional[Dict[str, Any]] = None,
    expires_min: int = 60,
    db_path: Optional[Path] = None,
) -> HitlRequest:
    h = HitlRequest(
        caller=caller, tool_name=tool_name, description=description,
        payload=payload or {}, expires_at=time.time() + expires_min * 60,
    )
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO hitl_approvals VALUES (?,?,?,?,?,?,?,?,?,?)",
            (h.id, h.caller, h.tool_name, h.description, json.dumps(h.payload), h.status,
             h.requested_at, None, h.answerer, h.expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return h


def hitl_get_pending(caller: str = "", db_path: Optional[Path] = None) -> List[HitlRequest]:
    conn = _connect(db_path)
    try:
        where = "WHERE status='pending' AND expires_at > ?" if not caller else (
            "WHERE status='pending' AND expires_at > ? AND caller=?"
        )
        params: List[Any] = [time.time()] + ([caller] if caller else [])
        rows = conn.execute(
            f"SELECT * FROM hitl_approvals {where} ORDER BY requested_at DESC", params,
        ).fetchall()
        return [_row_to_hitl(r) for r in rows]
    finally:
        conn.close()


def hitl_update(answer_id: str, status: str, answerer: str = "", db_path: Optional[Path] = None) -> bool:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE hitl_approvals SET status=?, answered_at=?, answerer=? WHERE id=?",
            (status, time.time(), answerer, answer_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _row_to_hitl(row: sqlite3.Row) -> HitlRequest:
    return HitlRequest(
        id=row["id"], caller=row["caller"], tool_name=row["tool_name"],
        description=row["description"], payload=json.loads(row["payload"]),
        status=row["status"], requested_at=row["requested_at"],
        answered_at=row["answered_at"], answerer=row["answerer"],
        expires_at=row["expires_at"],
    )


# --------------------------------------------------------------------------- #
# Audit‑log helpers                                                           #
# --------------------------------------------------------------------------- #

def append_audit(
    session_id: str,
    agent: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO swarm_audit_logs (session_id,agent,action,details) VALUES (?,?,?,?,?)",
            (session_id, agent, action, json.dumps(details or {})),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[no-any-return]
    finally:
        conn.close()


def get_audit_logs(session_id: str, limit: int = 100, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM swarm_audit_logs WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
