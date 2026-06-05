"""
pick_export_jobs.py — SQLite-backed job registry for async long-running jobs.

Originally built for the pick export, now generic — used by any endpoint that
needs the start/poll/cancel pattern (pick export AND /api/reports/run).

Backed by SQLite (not in-memory) so it works correctly across multiple uvicorn
workers if the deployment ever moves off --workers 1.

Schema:
    jobs(
        id            TEXT PRIMARY KEY,         -- uuid hex
        status        TEXT NOT NULL,            -- running | done | error | cancelled
        progress_json TEXT,                     -- {"done": N, "total": M}
        result_json   TEXT,                     -- final payload (status=done only)
        error         TEXT,                     -- error message (status=error/cancelled)
        created_at    REAL NOT NULL,            -- unix seconds
        finished_at   REAL                      -- unix seconds (done/error/cancelled only)
    )

Usage:
    create(job_id)
    update_progress(job_id, done, total)
    finish_done(job_id, result_dict)
    finish_error(job_id, "message")
    cancel(job_id)               # mark cancelled; running task should bail
    is_cancelled(job_id) -> bool # cheap check for running task to call between units of work
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


def cancel(job_id: str) -> bool:
    """
    Mark a running job as cancelled. The running task is expected to poll
    is_cancelled() between units of work and bail when it sees True.
    Returns True if the job was running and got marked cancelled, False otherwise.
    """
    if not os.path.exists(JOBS_DB_PATH):
        return False
    try:
        with _conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status = 'cancelled', error = ?, finished_at = ?"
                " WHERE id = ? AND status = 'running'",
                ("cancelled by client", time.time(), job_id),
            )
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def is_cancelled(job_id: str) -> bool:
    """
    Cheap synchronous check for the running task to see whether it should
    bail out early. Returns True only if the job exists AND status='cancelled'.
    Safe to call frequently; the SQLite read is fast (single row by primary key).
    """
    if not os.path.exists(JOBS_DB_PATH):
        return False
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return bool(row and row[0] == "cancelled")
    except Exception:
        return False


def get_status_only(job_id: str) -> Optional[Dict[str, Any]]:
    """Lightweight job lookup that NEVER deserializes the result payload.
    Use this for /status polling so the response stays small (KB, not MB)
    even for jobs with huge results — nginx in front of Cloudways will
    silently truncate large response bodies if they exceed the proxy
    buffer or proxy_read_timeout. Pair with get_raw_result() to fetch
    the actual data in a separate, dedicated request."""
    if not os.path.exists(JOBS_DB_PATH):
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT id, status, progress_json, error, created_at, finished_at,"
                " CASE WHEN result_json IS NULL THEN 0 ELSE length(result_json) END"
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
    return {
        "id":               row[0],
        "status":           row[1],
        "progress":         progress,
        "error":            row[3],
        "created_at":       row[4],
        "finished_at":      row[5],
        "result_size":      int(row[6] or 0),    # bytes of result_json
        "result_available": bool(row[6] and row[1] == "done"),
    }


def get_raw_result(job_id: str) -> Optional[str]:
    """Return the raw JSON string of result_json from SQLite, with no
    intermediate Python deserialization. The caller can stream it
    straight to the HTTP response — saves both memory + CPU for large
    payloads. Returns None if the job doesn't exist or has no result yet."""
    if not os.path.exists(JOBS_DB_PATH):
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT result_json FROM jobs WHERE id = ?", (job_id,),
            ).fetchone()
    except Exception:
        return None
    return row[0] if row and row[0] else None


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
    """Delete done/error/cancelled jobs older than retention_seconds. Returns rows deleted."""
    if not os.path.exists(JOBS_DB_PATH):
        return 0
    cutoff = time.time() - retention_seconds
    try:
        with _conn() as c:
            cur = c.execute(
                "DELETE FROM jobs WHERE status IN ('done', 'error', 'cancelled')"
                " AND finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
    except Exception:
        return 0
