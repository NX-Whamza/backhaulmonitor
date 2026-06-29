"""Auth module — admin sessions + API key management."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config import settings

_DB_PATH = Path(__file__).resolve().parent.parent / "feedback.db"

_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT    NOT NULL UNIQUE,
    label    TEXT    NOT NULL,
    prefix   TEXT    NOT NULL,
    created  REAL   NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1
);
"""

# In-memory session store (cleared on restart — fine for single admin)
_sessions: dict[str, float] = {}
_SESSION_TTL = 86400  # 24 hours


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_auth_db() -> None:
    with _connect() as conn:
        conn.executescript(_AUTH_SCHEMA)


# ── Admin session ────────────────────────────────────────────────────

def check_login(username: str, password: str) -> Optional[str]:
    """Validate admin credentials. Returns a session token or None."""
    if not settings.admin_password:
        return None
    if username == settings.admin_username and password == settings.admin_password:
        token = secrets.token_urlsafe(32)
        _sessions[token] = time.time()
        return token
    return None


def validate_session(token: str) -> bool:
    """Check if a session token is valid and not expired."""
    if not token or token not in _sessions:
        return False
    if time.time() - _sessions[token] > _SESSION_TTL:
        _sessions.pop(token, None)
        return False
    return True


def end_session(token: str) -> None:
    _sessions.pop(token, None)


# ── API keys ─────────────────────────────────────────────────────────

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(label: str) -> dict[str, Any]:
    """Create a new API key. Returns the full key (shown once) + metadata."""
    raw_key = f"bhm_{secrets.token_urlsafe(32)}"
    prefix = raw_key[:8]
    key_hash = _hash_key(raw_key)
    now = time.time()

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO api_keys (key_hash, label, prefix, created, active) VALUES (?, ?, ?, ?, 1)",
            (key_hash, label, prefix, now),
        )
    return {
        "id": cur.lastrowid,
        "key": raw_key,
        "prefix": prefix,
        "label": label,
        "created": now,
    }


def validate_api_key(key: str) -> bool:
    """Check if an API key is valid and active."""
    key_hash = _hash_key(key)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key_hash = ? AND active = 1",
            (key_hash,),
        ).fetchone()
    return row is not None


def list_api_keys() -> list[dict[str, Any]]:
    """List all API keys (without the actual key — just prefix + metadata)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, prefix, label, created, active FROM api_keys ORDER BY created DESC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "prefix": r["prefix"],
            "label": r["label"],
            "created": r["created"],
            "active": bool(r["active"]),
        }
        for r in rows
    ]


def revoke_api_key(key_id: int) -> bool:
    """Revoke an API key by ID."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,)
        )
    return cur.rowcount > 0
