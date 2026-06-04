"""
contacts_db.py — SQLite layer for the in-app Contacts section.

Replaces the legacy forms.nfc.local PHP CRUD pages that used to write
directly to SQL-PROD's `contacts` database. Cloudways (where this app
runs) cannot reach the internal sql-prod host, so we keep our own
SQLite mirror at backend/data/contacts/contacts.db.

Initial data:
  - Dump from SQL Server with backend/contacts_export_from_sqlserver.py
  - SCP the resulting contacts.db onto Cloudways once.
  - Re-export periodically if SQL Server is still being written to by
    other systems (Yext closure feed, etc.) — see sysco_repoll_runbook
    or whatever runbook documents the refresh cadence.

Future:
  - Once SSRS reports + scheduled jobs are migrated away from the SQL
    Server contacts DB, this SQLite file becomes the sole source of
    truth and the SQL Server DB can be retired.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONTACTS_DB_PATH = os.getenv(
    "CONTACTS_SQLITE_PATH",
    os.path.join(os.path.dirname(__file__), "data", "contacts", "contacts.db"),
)


@contextmanager
def get_connection():
    """Yield a sqlite3 connection in WAL mode (concurrent-read-friendly)."""
    os.makedirs(os.path.dirname(CONTACTS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CONTACTS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_available() -> bool:
    """Cheap connectivity smoke-test for health checks."""
    try:
        if not os.path.exists(CONTACTS_DB_PATH):
            return False
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception as exc:
        logger.warning("contacts_db unavailable: %s", exc)
        return False


def initialize_schema() -> None:
    """
    Create empty tables if they don't exist. Called on app startup so
    the UI doesn't 500 before the first SQL Server export has been done
    — the API just returns empty lists until then.

    Schema mirrors SQL-PROD's contacts.dbo.* with one tweak: SQLite
    INTEGER PRIMARY KEY auto-increments by default, so no explicit
    IDENTITY/SEQUENCE needed.
    """
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS AreaDir (
            AreaDirID         INTEGER PRIMARY KEY,
            AreaDirName       TEXT,
            AreaDirFirstName  TEXT,
            AreaDirLastName   TEXT,
            AreaDirEmail      TEXT,
            AreaDirSMGEmail   TEXT,
            AreaDirCFPW       TEXT,
            AreaDirCFID       TEXT,
            CompanyID         INTEGER,
            LastUpdatedBy     TEXT,
            LastUpdated       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_areadir_name ON AreaDir(AreaDirName);

        CREATE TABLE IF NOT EXISTS Company (
            CompanyID    INTEGER PRIMARY KEY,
            CompanyName  TEXT,
            -- Other columns get added by the SQL Server exporter dynamically
            LastUpdatedBy TEXT,
            LastUpdated   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_company_name ON Company(CompanyName);

        -- Store master directory (62 columns in SQL Server). The exporter
        -- recreates this table on each export, so the "skeleton" defined
        -- here only matters before the first export — keeps the UI from
        -- 500'ing when the DB is otherwise empty.
        CREATE TABLE IF NOT EXISTS Store (
            StoreID       INTEGER PRIMARY KEY,
            StoreName     TEXT,
            StoreCity     TEXT,
            StoreState    TEXT,
            StoreStatus   TEXT,
            StoreOpenDate TEXT,
            StoreCloseDate TEXT,
            StoreCompany  INTEGER,
            StoreAreaDirID INTEGER,
            LastUpdatedBy TEXT,
            LastUpdated   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_store_name ON Store(StoreName);
        CREATE INDEX IF NOT EXISTS idx_store_state ON Store(StoreState);
        """)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a Row to a plain dict — JSON-serializable."""
    return {k: row[k] for k in row.keys()}


# ──────────────────────────────────────────────────────────────────────────────
# AreaDir — same API as before, SQLite under the hood
# ──────────────────────────────────────────────────────────────────────────────

AREADIR_COLUMNS = (
    "AreaDirID, AreaDirName, AreaDirFirstName, AreaDirLastName, "
    "AreaDirEmail, AreaDirSMGEmail, AreaDirCFPW, AreaDirCFID, "
    "CompanyID, LastUpdatedBy, LastUpdated"
)


def _compose_full_name(data: Dict[str, Any]) -> Optional[str]:
    explicit = (data.get("AreaDirName") or "").strip()
    if explicit:
        return explicit
    first = (data.get("AreaDirFirstName") or "").strip()
    last  = (data.get("AreaDirLastName")  or "").strip()
    composed = f"{first} {last}".strip()
    return composed or None


def list_area_directors() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT {AREADIR_COLUMNS}
            FROM   AreaDir
            ORDER BY LOWER(COALESCE(AreaDirName, AreaDirLastName, ''))
        """).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_area_director(areadir_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {AREADIR_COLUMNS} FROM AreaDir WHERE AreaDirID = ?",
            (areadir_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def insert_area_director(data: Dict[str, Any], updated_by: str) -> int:
    """Insert + return new AreaDirID."""
    full_name = _compose_full_name(data)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO AreaDir
              (AreaDirName, AreaDirFirstName, AreaDirLastName,
               AreaDirEmail, AreaDirSMGEmail, AreaDirCFPW, AreaDirCFID,
               CompanyID, LastUpdatedBy, LastUpdated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                full_name,
                data.get("AreaDirFirstName") or None,
                data.get("AreaDirLastName")  or None,
                data.get("AreaDirEmail")     or None,
                data.get("AreaDirSMGEmail")  or None,
                data.get("AreaDirCFPW")      or None,
                data.get("AreaDirCFID")      or None,
                data.get("CompanyID"),
                updated_by,
            ),
        )
        new_id = cur.lastrowid
        conn.commit()
        return int(new_id)


def update_area_director(areadir_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    full_name = _compose_full_name(data)
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE AreaDir SET
              AreaDirName      = ?,
              AreaDirFirstName = ?,
              AreaDirLastName  = ?,
              AreaDirEmail     = ?,
              AreaDirSMGEmail  = ?,
              AreaDirCFPW      = ?,
              AreaDirCFID      = ?,
              CompanyID        = ?,
              LastUpdatedBy    = ?,
              LastUpdated      = datetime('now')
            WHERE AreaDirID = ?
            """,
            (
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
            ),
        )
        affected = cur.rowcount
        conn.commit()
        return affected > 0


def delete_area_director(areadir_id: int, updated_by: str) -> bool:
    """Soft-audit pattern: stamp who deleted, THEN delete. The SQL Server
    side has triggers that read LastUpdatedBy before the DELETE fires
    — preserving that pattern keeps our future sync-back step simple."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE AreaDir SET LastUpdatedBy = ?, LastUpdated = datetime('now') WHERE AreaDirID = ?",
            (updated_by, areadir_id),
        )
        cur = conn.execute("DELETE FROM AreaDir WHERE AreaDirID = ?", (areadir_id,))
        affected = cur.rowcount
        conn.commit()
        return affected > 0


# ──────────────────────────────────────────────────────────────────────────────
# Store — master directory (62 columns in SQL Server).
#
# Different shape from AreaDir: too many columns to hand-list, so the CRUD
# helpers are column-agnostic — they introspect what columns the SQLite
# table actually has and operate on whatever's there. This means the
# exporter can add/remove columns and the CRUD keeps working without code
# changes. Some columns are protected from user edits (StoreID itself,
# LastUpdated auto-stamp) and excluded from UPDATE/INSERT.
# ──────────────────────────────────────────────────────────────────────────────

# Columns we never let the user edit directly. The PK is auto-managed,
# LastUpdated/LastUpdatedBy are stamped server-side.
STORE_PROTECTED_COLUMNS = {"StoreID", "LastUpdated"}


def _store_columns() -> List[str]:
    """Return the column names actually present in the Store SQLite table."""
    with get_connection() as conn:
        rows = conn.execute("PRAGMA table_info(Store)").fetchall()
        return [r["name"] for r in rows]


def list_stores() -> List[Dict[str, Any]]:
    """Return every Store with every column. The list view picks which
    columns to show; edit modal renders them all."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM Store ORDER BY LOWER(COALESCE(StoreName, ''))"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_store(store_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM Store WHERE StoreID = ?", (store_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def update_store(store_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    """
    Update whatever columns the caller supplied. Unknown columns (not in
    the SQLite schema) are silently dropped, so a stale frontend won't
    fail a save. Protected columns (StoreID, LastUpdated) are ignored.
    LastUpdatedBy + LastUpdated are always stamped.
    """
    valid_cols = set(_store_columns())
    sets: List[str] = []
    values: List[Any] = []
    for k, v in data.items():
        if k in STORE_PROTECTED_COLUMNS or k not in valid_cols:
            continue
        sets.append(f"{k} = ?")
        # Treat empty string as NULL — matches SQL Server's typical behavior
        # for our nullable VARCHAR columns and keeps the data tidy.
        values.append(v if v not in ("", None) else None)
    if not sets:
        # Nothing to do; still bump the audit stamp so we record "user
        # touched this record" if they opened + saved a no-op edit.
        sets.append("LastUpdatedBy = ?")
        values.append(updated_by)
    else:
        sets.append("LastUpdatedBy = ?")
        values.append(updated_by)
    sets.append("LastUpdated = datetime('now')")
    values.append(store_id)
    sql = f"UPDATE Store SET {', '.join(sets)} WHERE StoreID = ?"
    with get_connection() as conn:
        cur = conn.execute(sql, tuple(values))
        affected = cur.rowcount
        conn.commit()
        return affected > 0


def insert_store(data: Dict[str, Any], updated_by: str) -> int:
    """Insert + return new StoreID. Caller must supply StoreID (it isn't
    auto-incremented in SQL Server — RestaurantIDs are externally
    assigned)."""
    if not data.get("StoreID"):
        raise ValueError("StoreID is required when inserting a new store")
    valid_cols = set(_store_columns())
    cols = ["StoreID"]
    values = [int(data["StoreID"])]
    for k, v in data.items():
        if k in ("StoreID", "LastUpdated") or k not in valid_cols:
            continue
        cols.append(k)
        values.append(v if v not in ("", None) else None)
    cols.append("LastUpdatedBy")
    values.append(updated_by)
    cols.append("LastUpdated")
    placeholders = ", ".join("?" * (len(values))) + ", datetime('now')"
    col_list = ", ".join(cols)
    sql = f"INSERT INTO Store ({col_list}) VALUES ({placeholders})"
    with get_connection() as conn:
        conn.execute(sql, tuple(values))
        conn.commit()
        return int(data["StoreID"])


# ──────────────────────────────────────────────────────────────────────────────
# Companies — referenced by AreaDir.CompanyID; populates the dropdown
# ──────────────────────────────────────────────────────────────────────────────

def list_companies() -> List[Dict[str, Any]]:
    """Return CompanyID + CompanyName for the dropdown. Returns empty
    list if the table isn't there yet (pre-first-export)."""
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT CompanyID, CompanyName FROM Company ORDER BY LOWER(COALESCE(CompanyName, ''))"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("Company table missing or unreadable: %s", exc)
            return []


# ──────────────────────────────────────────────────────────────────────────────
# Stats / metadata for the UI to show "last synced" etc.
# ──────────────────────────────────────────────────────────────────────────────

def get_metadata() -> Dict[str, Any]:
    """Return basic info about the local SQLite mirror — handy for the
    UI to show 'Last synced from SQL Server: X' once we have a sync job."""
    if not db_available():
        return {"available": False, "path": CONTACTS_DB_PATH}
    info = {"available": True, "path": CONTACTS_DB_PATH, "tables": {}}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for r in rows:
            table = r["name"]
            try:
                count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                info["tables"][table] = count
            except Exception:
                info["tables"][table] = None
    try:
        info["file_mtime"] = os.path.getmtime(CONTACTS_DB_PATH)
    except OSError:
        info["file_mtime"] = None
    return info
