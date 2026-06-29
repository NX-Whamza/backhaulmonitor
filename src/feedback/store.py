"""SQLite-backed feedback store for diagnosis accuracy tracking."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "feedback.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    hostname    TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    severity    TEXT    NOT NULL,
    band_ghz    INTEGER,
    off_target_db   REAL,
    baseline_delta  REAL,
    rain_rate       REAL,
    accurate    INTEGER NOT NULL,   -- 1 = yes, 0 = no
    comment     TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON feedback(verdict);
CREATE INDEX IF NOT EXISTS idx_feedback_hostname ON feedback(hostname);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def check_recent_feedback(
    hostname: str, verdict: str, window_seconds: int = 86400,
) -> Optional[dict[str, Any]]:
    """Check if feedback was already submitted for this hostname + verdict recently.

    Returns the existing record if found within the window, None otherwise.
    """
    cutoff = time.time() - window_seconds
    with _connect() as conn:
        row = conn.execute(
            """SELECT id, ts, accurate, comment
               FROM feedback
               WHERE hostname = ? AND verdict = ? AND ts > ?
               ORDER BY ts DESC LIMIT 1""",
            (hostname, verdict, cutoff),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "ts": row["ts"],
        "accurate": bool(row["accurate"]),
        "comment": row["comment"],
    }


def save_feedback(
    hostname: str,
    verdict: str,
    confidence: float,
    severity: str,
    accurate: bool,
    band_ghz: Optional[int] = None,
    off_target_db: Optional[float] = None,
    baseline_delta: Optional[float] = None,
    rain_rate: Optional[float] = None,
    comment: Optional[str] = None,
) -> int:
    """Save a single feedback record. Returns the row id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO feedback
               (ts, hostname, verdict, confidence, severity,
                band_ghz, off_target_db, baseline_delta, rain_rate,
                accurate, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                hostname,
                verdict,
                confidence,
                severity,
                band_ghz,
                off_target_db,
                baseline_delta,
                rain_rate,
                1 if accurate else 0,
                comment,
            ),
        )
        return cur.lastrowid


def get_verdict_accuracy(verdict: str) -> dict[str, Any]:
    """Return accuracy stats for a given verdict type.

    Returns:
        {
            "verdict": str,
            "total": int,
            "accurate": int,
            "inaccurate": int,
            "accuracy_pct": float | None,   # None when total == 0
        }
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*)              AS total,
                 SUM(accurate)         AS accurate,
                 SUM(1 - accurate)     AS inaccurate
               FROM feedback
               WHERE verdict = ?""",
            (verdict,),
        ).fetchone()

    total = row["total"] or 0
    accurate = row["accurate"] or 0
    inaccurate = row["inaccurate"] or 0
    return {
        "verdict": verdict,
        "total": total,
        "accurate": accurate,
        "inaccurate": inaccurate,
        "accuracy_pct": round(accurate / total * 100, 1) if total > 0 else None,
    }


def get_all_accuracy() -> list[dict[str, Any]]:
    """Return accuracy breakdown for every verdict that has feedback."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT
                 verdict,
                 COUNT(*)          AS total,
                 SUM(accurate)     AS accurate,
                 SUM(1 - accurate) AS inaccurate
               FROM feedback
               GROUP BY verdict
               ORDER BY total DESC"""
        ).fetchall()

    return [
        {
            "verdict": r["verdict"],
            "total": r["total"],
            "accurate": r["accurate"] or 0,
            "inaccurate": r["inaccurate"] or 0,
            "accuracy_pct": round((r["accurate"] or 0) / r["total"] * 100, 1),
        }
        for r in rows
    ]


def blend_confidence(raw_confidence: float, verdict: str,
                     prior: float = 0.7, prior_weight: float = 10) -> float:
    """Bayesian-blend raw engine confidence with historical accuracy.

    With zero feedback the result equals raw_confidence (no change).
    As reports accumulate the historical accuracy pulls the score
    up or down.

    Args:
        raw_confidence: 0-100 from the diagnosis engine.
        verdict: verdict string to look up.
        prior: assumed accuracy before any data (0-1).
        prior_weight: how many virtual "prior" observations.

    Returns:
        Adjusted confidence (0-100), rounded to nearest int.
    """
    stats = get_verdict_accuracy(verdict)
    n = stats["total"]
    if n == 0:
        return raw_confidence

    observed = stats["accurate"] / n  # 0-1
    # Bayesian posterior mean
    blended_accuracy = (prior_weight * prior + n * observed) / (prior_weight + n)
    # Scale raw confidence: multiply by (blended / prior) ratio
    # so 100% historical accuracy boosts, <prior pulls down
    scale = blended_accuracy / prior
    adjusted = raw_confidence * scale
    return round(min(max(adjusted, 1), 100))
