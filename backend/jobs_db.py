"""
jobs_db.py — tiny SQLite job queue for the Integrations / Re-run dashboard.

The Cloudways app enqueues jobs (a Run button); the dev-jan02 agent claims them,
runs the allowlisted command, and posts the result back. One row per run.
`args` carries an optional parameter (e.g. a YYYY-MM-DD date for repolls).
See main.py  /api/integrations/*  and  backend/integrations_agent.py.
"""
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Optional

JOBS_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "jobs", "jobs.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'queued',   -- queued | running | success | partial | failed
    requested_by TEXT,
    args         TEXT,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    exit_code    INTEGER,
    log          TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: str = JOBS_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(db_path: str = JOBS_DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN args TEXT")  # migrate older tables
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()


def enqueue(job_key: str, requested_by: str = "", args: str = "", db_path: str = JOBS_DB_PATH) -> int:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO jobs (job_key, status, requested_by, args, created_at) "
            "VALUES (?, 'queued', ?, ?, ?)",
            (job_key, requested_by, args, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def claim_next(db_path: str = JOBS_DB_PATH) -> Optional[Dict]:
    """Atomically grab the oldest queued job, mark it running, return it (or None)."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, job_key, args FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (_now(), row["id"]))
        conn.execute("COMMIT")
        return {"id": int(row["id"]), "job_key": row["job_key"], "args": row["args"] or ""}


def post_result(job_id: int, status: str, exit_code: Optional[int], log: str,
                db_path: str = JOBS_DB_PATH) -> None:
    status = status if status in ("success", "partial", "failed") else "failed"
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, log=?, finished_at=? WHERE id=?",
            (status, exit_code, (log or "")[-20000:], _now(), job_id),
        )
        conn.commit()


def record_run(job_key: str, requested_by: str, status: str, exit_code: Optional[int],
               log: str, args: str = "", db_path: str = JOBS_DB_PATH) -> int:
    """Insert an ALREADY-FINISHED run (e.g. a cron job reporting in via the wrapper)
    so it shows in Recent Runs and updates the last-run badge."""
    initialize_db(db_path)
    status = status if status in ("success", "partial", "failed") else "failed"
    now = _now()
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO jobs (job_key, status, requested_by, args, created_at, started_at, finished_at, exit_code, log) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_key, status, requested_by, args, now, now, now, exit_code, (log or "")[-20000:]),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_recent(limit: int = 50, db_path: str = JOBS_DB_PATH) -> List[Dict]:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def last_run_by_key(db_path: str = JOBS_DB_PATH) -> Dict[str, Dict]:
    """Most-recent run per job_key (for the dashboard catalog's last-run column)."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT j.* FROM jobs j
            JOIN (SELECT job_key, MAX(id) AS mid FROM jobs GROUP BY job_key) m ON m.mid = j.id
        """).fetchall()
        return {r["job_key"]: dict(r) for r in rows}
