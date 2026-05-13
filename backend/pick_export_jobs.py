"""
pick_export_jobs.py — SQLite-backed job registry for async pick export jobs.

Used by the Menu Performance Excel export to kick off long-running pick
report computation as a background task and poll for status. Backed by
SQLite (not in-memory) so it works correctly across multiple uvicorn
workers — `start.sh` runs `--workers 2`, and in-process dicts would not
be shared across worker processes.

Schema:
    jobs(
        id            TEXT PRIMARY KEY,         -- uuid hex
        status        TEXT NOT NULL,            -- running | done | error
        progress_json TEXT,                     -- {"done": N, "total": M}
        result_json   TEXT,                     -- final payload (status=done only)
        error         TEXT,                     -- error message (status=error only)
        created_at    REAL NOT NULL,            -- unix seconds
        finished_at   REAL                      -- unix seconds (done/error only)
    )

Usage:
    create(job_id)
    update_progress(job_id, done, total)
    finish_done(job_id, result_dict)
    finish_error(job_id, "message")
    get(job_id) -> dict | None
    purge_old(retention_seconds=3600)
"""

import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional


JOBS_DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "pick_export_jobs.db"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(JOBS_DB_PATH, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _initialize() -> None:
    os.makedirs(os.path.dirname(JOBS_DB_PATH), exist_ok=True)
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                status        TEXT NOT NULL,
                progress_json TEXT,
                result_json   TEXT,
                error         TEXT,
                created_at    REAL NOT NULL,
                finished_at   REAL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, finished_at)")


def create(job_id: str) -> None:
    """Insert a new running job."""
    _initialize()
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, status, progress_json, created_at) VALUES (?, 'running', ?, ?)",
            (job_id, json.dumps({"done": 0, "total": 0}), time.time()),
        )


def update_progress(job_id: str, done: int, total: int) -> None:
    """Update the progress counter on a running job. No-op if job missing."""
    try:
        with _conn() as c:
            c.execute(
                "UPDATE jobs SET progress_json = ? WHERE id = ?",
                (json.dumps({"done": int(done), "total": int(total)}), job_id),
            )
    except Exception:
        # Progress updates are best-effort — never let a failed write kill the job.
        pass


def finish_done(job_id: str, result: Dict[str, Any]) -> None:
    """Mark the job done and store the result payload."""
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status = 'done', result_json = ?, finished_at = ? WHERE id = ?",
            (json.dumps(result, separators=(",", ":")), time.time(), job_id),
        )


def finish_error(job_id: str, error: str) -> None:
    """Mark the job errored with a message."""
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
            (str(error)[:2000], time.time(), job_id),
        )


def get(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job row as a dict, or None if no such job."""
    if not os.path.exists(JOBS_DB_PATH):
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT id, status, progress_json, result_json, error, created_at, finished_at"
                " FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    progress = None
    if row[2]:
        try:
            progress = json.loads(row[2])
        except Exception:
            progress = None
    result = None
    if row[3]:
        try:
            result = json.loads(row[3])
        except Exception:
            result = None
    return {
        "id": row[0],
        "status": row[1],
        "progress": progress,
        "result": result,
        "error": row[4],
        "created_at": row[5],
        "finished_at": row[6],
    }


def purge_old(retention_seconds: float = 3600.0) -> int:
    """Delete done/error jobs older than retention_seconds. Returns rows deleted."""
    if not os.path.exists(JOBS_DB_PATH):
        return 0
    cutoff = time.time() - retention_seconds
    try:
        with _conn() as c:
            cur = c.execute(
                "DELETE FROM jobs WHERE status IN ('done', 'error') AND finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
    except Exception:
        return 0
