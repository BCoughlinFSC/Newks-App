"""
contacts_db.py — SQL Server connection + CRUD helpers for the `contacts` DB.

Mirrors the existing `dev_aloha` SQL Server connection pattern used by
toast_puller_db.py — same host (sql-prod), same pyodbc driver, just a
different database name and (optionally) a different user.

Purpose: back the new in-app "Contacts" section that replaces Don Wilson's
legacy PHP CRUD pages at http://forms.nfc.local/. The data stays in SQL
Server (so existing SSRS reports, audit triggers, and scheduled jobs
keep working), and we just build a better UI on top of it.

Connection params (env-overridable):
  ALOHA_SQL_SERVER         — defaults to sql-prod (already in .env)
  ALOHA_SQL_USERNAME       — reused from the dev_aloha connection
  ALOHA_SQL_PASSWORD       — reused from the dev_aloha connection
  ALOHA_SQL_DRIVER         — ODBC driver name
  CONTACTS_SQL_DATABASE    — defaults to 'contacts'
  CONTACTS_SQL_USERNAME    — override if 'reports' user isn't the same
  CONTACTS_SQL_PASSWORD    —   as the ALOHA_SQL_USERNAME service account

Audit: every write sets LastUpdatedBy / LastUpdated so the existing
SQL Server triggers (TR_*_AUDIT → AuditDataChanges) record who changed
what. The new app supplies the current user's display name from the
authenticated session.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pyodbc

logger = logging.getLogger(__name__)

SQL_SERVER   = os.getenv("ALOHA_SQL_SERVER", "sql-prod")
SQL_DATABASE = os.getenv("CONTACTS_SQL_DATABASE", "contacts")
SQL_USERNAME = os.getenv("CONTACTS_SQL_USERNAME", os.getenv("ALOHA_SQL_USERNAME", ""))
SQL_PASSWORD = os.getenv("CONTACTS_SQL_PASSWORD", os.getenv("ALOHA_SQL_PASSWORD", ""))
SQL_DRIVER   = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SQL_TRUSTED  = (os.getenv("ALOHA_SQL_TRUSTED_CONNECTION", "false").lower() == "true")


def _conn_string() -> str:
    parts = [
        f"DRIVER={{{SQL_DRIVER}}}",
        f"SERVER={SQL_SERVER}",
        f"DATABASE={SQL_DATABASE}",
    ]
    if SQL_TRUSTED:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={SQL_USERNAME}")
        parts.append(f"PWD={SQL_PASSWORD}")
    return ";".join(parts) + ";"


@contextmanager
def get_connection():
    """Yield a pyodbc connection to the contacts DB. Closes on exit."""
    conn = pyodbc.connect(_conn_string())
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_available() -> bool:
    """Cheap connectivity smoke-test — used by health checks and the UI."""
    try:
        with get_connection() as conn:
            conn.cursor().execute("SELECT 1").fetchone()
        return True
    except Exception as exc:
        logger.warning("contacts_db unavailable: %s", exc)
        return False


def _rows_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# AreaDir — proof-of-concept CRUD
#
# Mirrors the legacy /var/www/html/contacts/areadir/index.php exactly:
#   - INSERT auto-composes AreaDirName from First + Last if not supplied
#   - DELETE first UPDATEs LastUpdatedBy so the audit trigger captures who
#   - All other writes stamp LastUpdatedBy + LastUpdated = GETDATE()
# ──────────────────────────────────────────────────────────────────────────────

AREADIR_COLUMNS = (
    "AreaDirID, AreaDirName, AreaDirFirstName, AreaDirLastName, "
    "AreaDirEmail, AreaDirSMGEmail, AreaDirCFPW, AreaDirCFID, "
    "CompanyID, LastUpdatedBy, LastUpdated"
)


def _compose_full_name(data: Dict[str, Any]) -> Optional[str]:
    """Use AreaDirName if supplied; otherwise '<First> <Last>' trimmed."""
    explicit = (data.get("AreaDirName") or "").strip()
    if explicit:
        return explicit
    first = (data.get("AreaDirFirstName") or "").strip()
    last  = (data.get("AreaDirLastName")  or "").strip()
    composed = f"{first} {last}".strip()
    return composed or None


def list_area_directors() -> List[Dict[str, Any]]:
    """All Area Directors, sorted by displayed name (case-insensitive)."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT {AREADIR_COLUMNS}
            FROM   contacts.dbo.AreaDir
            ORDER BY LOWER(COALESCE(AreaDirName, AreaDirLastName, ''))
        """)
        return _rows_to_dicts(cur)


def get_area_director(areadir_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {AREADIR_COLUMNS} FROM contacts.dbo.AreaDir WHERE AreaDirID = ?",
            areadir_id,
        )
        rows = _rows_to_dicts(cur)
        return rows[0] if rows else None


def insert_area_director(data: Dict[str, Any], updated_by: str) -> int:
    """Insert + return the new AreaDirID."""
    full_name = _compose_full_name(data)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contacts.dbo.AreaDir
              (AreaDirName, AreaDirFirstName, AreaDirLastName,
               AreaDirEmail, AreaDirSMGEmail, AreaDirCFPW, AreaDirCFID,
               CompanyID, LastUpdatedBy, LastUpdated)
            OUTPUT INSERTED.AreaDirID
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
            """,
            full_name,
            data.get("AreaDirFirstName") or None,
            data.get("AreaDirLastName")  or None,
            data.get("AreaDirEmail")     or None,
            data.get("AreaDirSMGEmail")  or None,
            data.get("AreaDirCFPW")      or None,
            data.get("AreaDirCFID")      or None,
            data.get("CompanyID"),
            updated_by,
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return int(new_id)


def update_area_director(areadir_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    """Returns True if a row was actually updated."""
    full_name = _compose_full_name(data)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE contacts.dbo.AreaDir SET
                AreaDirName      = ?,
                AreaDirFirstName = ?,
                AreaDirLastName  = ?,
                AreaDirEmail     = ?,
                AreaDirSMGEmail  = ?,
                AreaDirCFPW      = ?,
                AreaDirCFID      = ?,
                CompanyID        = ?,
                LastUpdatedBy    = ?,
                LastUpdated      = GETDATE()
            WHERE AreaDirID = ?
            """,
            full_name,
            data.get("AreaDirFirstName") or None,
            data.get("AreaDirLastName")  or None,
            data.get("AreaDirEmail")     or None,
            data.get("AreaDirSMGEmail")  or None,
            data.get("AreaDirCFPW")      or None,
            data.get("AreaDirCFID")      or None,
            data.get("CompanyID"),
            updated_by,
            areadir_id,
        )
        affected = cur.rowcount
        conn.commit()
        return affected > 0


def delete_area_director(areadir_id: int, updated_by: str) -> bool:
    """
    Delete an Area Director. Stamps LastUpdatedBy first so the SQL Server
    audit trigger records who initiated the delete (otherwise the trigger
    just sees a DELETE with no actor context). Mirrors Don's pattern.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE contacts.dbo.AreaDir SET LastUpdatedBy = ? WHERE AreaDirID = ?",
            updated_by, areadir_id,
        )
        cur.execute(
            "DELETE FROM contacts.dbo.AreaDir WHERE AreaDirID = ?",
            areadir_id,
        )
        affected = cur.rowcount
        conn.commit()
        return affected > 0


# ──────────────────────────────────────────────────────────────────────────────
# Lookup tables (Companies — referenced by AreaDir.CompanyID, used for dropdowns)
# ──────────────────────────────────────────────────────────────────────────────

def list_companies() -> List[Dict[str, Any]]:
    """For populating the CompanyID dropdown on the AreaDir edit form."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT CompanyID, CompanyName
                FROM   contacts.dbo.Company
                ORDER BY CompanyName
            """)
            return _rows_to_dicts(cur)
        except Exception as exc:
            logger.warning("Could not load Company list: %s", exc)
            return []
