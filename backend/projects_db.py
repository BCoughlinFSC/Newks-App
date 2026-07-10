"""
projects_db.py — SQLite layer for the in-app "Projects" dashboard.

A tiny internal ticket board so the ~6 people on the team can see what
projects everyone is working on. Deliberately simple: every field is
free-typed (the "assignee" is just text, NOT a foreign key to the
app's user accounts) so nobody has to be invited/provisioned first.

Storage: backend/data/projects/projects.db (WAL, like the other
app-local SQLite mirrors). App-owned — no SQL Server / outbox sync.
Any logged-in user can create/edit/delete cards; edits are stamped
with created_by / updated_by for a light audit trail.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECTS_DB_PATH = os.getenv(
    "PROJECTS_SQLITE_PATH",
    os.path.join(os.path.dirname(__file__), "data", "projects", "projects.db"),
)

# Allowed status values — enforced loosely (UI offers these; API coerces
# anything unknown to "Open" so a typo can't wedge a card into a state
# the board can't filter/display).
STATUSES = ["Open", "In Progress", "Blocked", "Done"]
PRIORITIES = ["Low", "Medium", "High"]


@contextmanager
def get_connection():
    """Yield a sqlite3 connection in WAL mode (concurrent-read-friendly)."""
    os.makedirs(os.path.dirname(PROJECTS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(PROJECTS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_available() -> bool:
    """Cheap connectivity smoke-test for health checks."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception as exc:
        logger.warning("projects_db unavailable: %s", exc)
        return False


def initialize_schema() -> None:
    """Create the projects table if it doesn't exist. Idempotent; called
    on app startup so the page works on a fresh deploy with no data."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                assignee    TEXT,                 -- free text, not a user FK
                status      TEXT NOT NULL DEFAULT 'Open',
                priority    TEXT NOT NULL DEFAULT 'Medium',
                due_date    TEXT,                 -- 'YYYY-MM-DD' or NULL
                notes       TEXT,
                created_by  TEXT,
                updated_by  TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
            """
        )
        conn.commit()


def _coerce(value: Optional[str], allowed: List[str], default: str) -> str:
    """Snap a free-typed value to the allowed set; fall back to default."""
    if not value:
        return default
    for a in allowed:
        if str(value).strip().lower() == a.lower():
            return a
    return default


def list_projects() -> List[Dict[str, Any]]:
    """All projects, newest activity first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row else None


def insert_project(data: Dict[str, Any], created_by: str) -> int:
    """Insert + return the new project id."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects
              (name, assignee, status, priority, due_date, notes,
               created_by, updated_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                (data.get("name") or "").strip(),
                (data.get("assignee") or "").strip() or None,
                _coerce(data.get("status"), STATUSES, "Open"),
                _coerce(data.get("priority"), PRIORITIES, "Medium"),
                (data.get("due_date") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                created_by,
                created_by,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_project(project_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    """Update an existing project. Returns True if a row changed."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE projects
               SET name = ?, assignee = ?, status = ?, priority = ?,
                   due_date = ?, notes = ?, updated_by = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (
                (data.get("name") or "").strip(),
                (data.get("assignee") or "").strip() or None,
                _coerce(data.get("status"), STATUSES, "Open"),
                _coerce(data.get("priority"), PRIORITIES, "Medium"),
                (data.get("due_date") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                updated_by,
                project_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_project(project_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        return cur.rowcount > 0
