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

import json
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

# Feature flag for the SQL-Server sync-back outbox. When OFF (default),
# write paths behave exactly as before — local SQLite is the only target.
# When ON, every INSERT/UPDATE/DELETE also appends to contacts_outbox so
# the dev-jan02 drainer cron can replay it against SQL Server.
# Set CONTACTS_OUTBOX_ENABLED=1 in .env to enable. Default OFF so Phase 1
# can be deployed dark and Phase 2 (the drainer) can be tested first.
OUTBOX_ENABLED = os.getenv("CONTACTS_OUTBOX_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


# ── App-local tables: outbox queue + sync requests ──────────────────────────
# These two tables are CLOUDWAYS-ONLY state (they do not exist in SQL Server)
# and must survive a mirror refresh. import_contacts_db() recreates them in
# the freshly-uploaded file and copies the live rows across before swapping.
APP_LOCAL_SCHEMA_SQL = """
-- contacts_outbox: durable queue of edits to replay against SQL Server.
-- Every write helper appends a row here when CONTACTS_OUTBOX_ENABLED=1.
-- The dev-jan02 drainer cron fetches pending rows, replays them,
-- and posts back success/failure. Failed rows can be retried.
--
-- applied_status: NULL = pending, 'success' = applied, 'error' = failed
-- payload is JSON: {col: value, col: value, ...}
CREATE TABLE IF NOT EXISTS contacts_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    operation       TEXT NOT NULL,        -- INSERT | UPDATE | DELETE
    table_name      TEXT NOT NULL,
    pk_col          TEXT NOT NULL,
    pk_val          TEXT NOT NULL,        -- stored as TEXT for flexibility
    payload_json    TEXT,                 -- {} for DELETE
    source_user     TEXT,                 -- LastUpdatedBy stamp
    applied_at      TEXT,
    applied_status  TEXT,                 -- NULL | 'success' | 'error'
    error_message   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON contacts_outbox(applied_status, created_at)
    WHERE applied_status IS NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_table
    ON contacts_outbox(table_name, pk_val);

-- contacts_sync_requests: queue for the "Sync from SQL" UI button.
-- The UI inserts a pending row; the dev-jan02 drainer polls
-- GET /api/contacts/sync-request, and once the outbox is fully drained
-- it exports SQL-PROD -> contacts.db and uploads it via
-- POST /api/contacts/import-db, which marks the request fulfilled.
CREATE TABLE IF NOT EXISTS contacts_sync_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at  TEXT NOT NULL,
    requested_by  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | fulfilled | failed
    fulfilled_at  TEXT,
    note          TEXT
);

-- StoreOLOVendor: per-store OLO Vendor ID (e.g. Oxford MS = 33030).
--
-- This is a CLOUDWAYS-ONLY, APP-OWNED attribute — it does NOT exist in the
-- SQL Server `contacts` schema. It used to live in SQL Server's
-- OLO.dbo.OLOVendors, populated by a licensed ZappySys SSIS package on
-- NEWKS-SQL whose sole remaining consumer was the SSRS "Restaurant Contact
-- Information Sheet" report. That report now lives in this app, so the OLO
-- Vendor ID lives here too and the ETL + its paid ZappySys dependency can be
-- retired.
--
-- Because it is app-owned it is listed in import_contacts_db()'s PRESERVE set
-- (never dropped on a mirror refresh) and its edits are NOT recorded to the
-- outbox (there is no SQL Server column to replay them into). Seed it once
-- with backend/seed_olo_vendor_ids.py.
CREATE TABLE IF NOT EXISTS StoreOLOVendor (
    StoreID       INTEGER PRIMARY KEY,
    OLOVendorID   TEXT,
    LastUpdatedBy TEXT,
    LastUpdated   TEXT
);
"""


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

        -- StorePhysical: 1:1 satellite of Store (square footage, seats, drive-thru).
        -- Audit columns are LastEdited/LastEditedBy here (Don's older naming
        -- convention) — NOT LastUpdated/LastUpdatedBy like everywhere else.
        -- Exporter rebuilds the table with the full SQL Server column list.
        CREATE TABLE IF NOT EXISTS StorePhysical (
            StoreID      INTEGER PRIMARY KEY,
            LastEditedBy TEXT,
            LastEdited   TEXT
        );

        -- StoreTechnical: 1:1 satellite — POS versions, IPs, network kit.
        -- ⚠ The PK is StoreTechnicalID and the FK to Store is the lowercase
        -- `storeid` column (matches SQL Server schema). Exporter rebuilds.
        CREATE TABLE IF NOT EXISTS StoreTechnical (
            StoreTechnicalID INTEGER PRIMARY KEY,
            storeid          INTEGER,
            LastUpdatedBy    TEXT,
            LastUpdated      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_storetechnical_fk ON StoreTechnical(storeid);

        -- StoreClosure: 1:many — full history of every closure window.
        -- Read-only in v1; closure-history editing UI comes later.
        CREATE TABLE IF NOT EXISTS StoreClosure (
            StoreClosureID INTEGER PRIMARY KEY,
            StoreID        INTEGER,
            LastUpdatedBy  TEXT,
            LastUpdated    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_storeclosure_fk ON StoreClosure(StoreID);

        """)
        # App-local tables (outbox queue + sync requests) live in a separate
        # constant so import_contacts_db() can recreate them inside a freshly
        # uploaded mirror file before copying the live queue across.
        conn.executescript(APP_LOCAL_SCHEMA_SQL)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a Row to a plain dict — JSON-serializable."""
    return {k: row[k] for k in row.keys()}


# ──────────────────────────────────────────────────────────────────────────────
# Outbox — every write helper calls _record_outbox() so the dev-jan02 drainer
# cron can replay changes against SQL Server. No-ops when CONTACTS_OUTBOX_ENABLED
# is unset, so Phase 1 can be deployed before Phase 2 (the drainer) exists.
#
# Catches all exceptions: outbox failure should NEVER fail the underlying user
# operation. If we can't record an outbox row, log it loudly but let the user's
# edit succeed locally — better to have a missed sync row than a broken UI.
# ──────────────────────────────────────────────────────────────────────────────

def _record_outbox(operation: str, table_name: str, pk_col: str, pk_val: Any,
                   payload: Optional[Dict[str, Any]], source_user: Optional[str],
                   conn: Optional[sqlite3.Connection] = None) -> None:
    """Append a row to contacts_outbox. No-op if outbox is disabled.

    operation:   'INSERT' | 'UPDATE' | 'DELETE'
    table_name:  SQL Server table name (e.g. 'Store', 'Company')
    pk_col:      primary-key column name (e.g. 'StoreID')
    pk_val:      primary-key value for this row
    payload:     {col: value, ...} of changed columns; empty {} for DELETE
    source_user: who made the edit (will be the LastUpdatedBy on SQL Server)
    conn:        if supplied, write outbox row through this connection so it
                 commits atomically with the underlying user write. If None,
                 we open a separate connection (best-effort).
    """
    if not OUTBOX_ENABLED:
        return
    try:
        payload_json = json.dumps(payload or {}, default=str)
        sql = """
            INSERT INTO contacts_outbox
              (created_at, operation, table_name, pk_col, pk_val,
               payload_json, source_user)
            VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)
        """
        args = (operation, table_name, pk_col, str(pk_val),
                payload_json, source_user)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with get_connection() as c:
                c.execute(sql, args)
                c.commit()
    except Exception as exc:
        # Never propagate — outbox failure must not break the user edit.
        logger.error("outbox record failed (op=%s table=%s pk=%s): %s",
                     operation, table_name, pk_val, exc, exc_info=True)


def get_outbox_stats() -> Dict[str, Any]:
    """Snapshot of outbox queue health — feeds the status banner."""
    try:
        with get_connection() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts_outbox WHERE applied_status IS NULL"
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts_outbox WHERE applied_status = 'error'"
            ).fetchone()["n"]
            applied = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts_outbox WHERE applied_status = 'success'"
            ).fetchone()["n"]
            last_applied = conn.execute(
                "SELECT MAX(applied_at) AS t FROM contacts_outbox WHERE applied_status = 'success'"
            ).fetchone()["t"]
            oldest_pending = conn.execute(
                "SELECT MIN(created_at) AS t FROM contacts_outbox WHERE applied_status IS NULL"
            ).fetchone()["t"]
        return {
            "enabled":         OUTBOX_ENABLED,
            "pending":         pending,
            "failed":          failed,
            "applied":         applied,
            "last_applied_at": last_applied,
            "oldest_pending":  oldest_pending,
        }
    except Exception as exc:
        logger.warning("get_outbox_stats failed: %s", exc)
        return {"enabled": OUTBOX_ENABLED, "pending": 0, "failed": 0, "applied": 0,
                "last_applied_at": None, "oldest_pending": None, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────────────
# Sync-from-SQL: request queue + fresh-mirror import.
# The Cloudways box can't reach SQL-PROD, so "sync from SQL" is a two-step
# dance: the UI queues a request here, and the dev-jan02 drainer fulfills it
# (export SQL Server → upload the fresh contacts.db → import_contacts_db).
# ──────────────────────────────────────────────────────────────────────────────

def create_sync_request(requested_by: Optional[str]) -> Dict[str, Any]:
    """Queue a mirror-refresh request. Idempotent: if one is already pending,
    return it instead of stacking duplicates."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM contacts_sync_requests WHERE status = 'pending' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if existing:
            out = _row_to_dict(existing)
            out["already_pending"] = True
            return out
        cur = conn.execute(
            "INSERT INTO contacts_sync_requests (requested_at, requested_by) "
            "VALUES (datetime('now'), ?)",
            (requested_by,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM contacts_sync_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        out = _row_to_dict(row)
        out["already_pending"] = False
        return out


def get_sync_request_status() -> Dict[str, Any]:
    """Current pending request (if any) + the most recent fulfilled one —
    feeds both the UI status display and the drainer's poll."""
    with get_connection() as conn:
        pending = conn.execute(
            "SELECT * FROM contacts_sync_requests WHERE status = 'pending' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        last = conn.execute(
            "SELECT * FROM contacts_sync_requests WHERE status = 'fulfilled' "
            "ORDER BY fulfilled_at DESC LIMIT 1"
        ).fetchone()
    return {
        "pending":        _row_to_dict(pending) if pending else None,
        "last_fulfilled": _row_to_dict(last) if last else None,
    }


def fulfill_sync_requests(note: Optional[str] = None) -> int:
    """Mark every pending sync request fulfilled (the import satisfies all
    of them at once). Returns the number of rows updated."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE contacts_sync_requests "
            "SET status = 'fulfilled', fulfilled_at = datetime('now'), note = ? "
            "WHERE status = 'pending'",
            (note,),
        )
        conn.commit()
        return cur.rowcount


def import_contacts_db(uploaded_path: str) -> Dict[str, Any]:
    """Refresh the live mirror's data tables from a freshly-exported
    contacts.db, IN PLACE — without swapping the database file.

    WAL-safe by design. The previous version did sqlite3 .backup() INTO the
    live, open database; on Cloudways that blocked on a write lock for the
    full 5-min nginx timeout (the classic WAL-swap-while-uvicorn-is-running
    gotcha) and never completed. Instead we ATTACH the uploaded export and,
    inside ONE short transaction on the live file, DROP+recreate+refill each
    data table from the export. Same file (no swap, no -wal/-shm dance),
    one write-lock acquisition, commits in well under a second for a ~1 MB
    mirror. The app-local tables (contacts_outbox, contacts_sync_requests)
    are NEVER touched, so the queue survives.

    Steps:
      1. Validate the upload (expected tables + sane Store count) — refuse a
         truncated/empty export rather than wiping the mirror.
      2. In one transaction on the live DB: for every data table in the
         export, DROP the live copy and recreate it from the export's own
         CREATE statement, then INSERT its rows.
      3. Re-run initialize_schema() to restore indexes + guarantee the
         app-local tables exist.
    """
    # Tables that live ONLY on Cloudways and must never be refreshed/dropped.
    # StoreOLOVendor is app-owned (no SQL Server counterpart) — dropping it on a
    # mirror refresh would wipe the OLO Vendor IDs that used to come from the
    # now-retired SQL Server OLO ETL.
    PRESERVE = {"contacts_outbox", "contacts_sync_requests", "sqlite_sequence",
                "StoreOLOVendor"}

    # ── 1. validate the upload (read-only) ──
    src = sqlite3.connect(uploaded_path)
    try:
        up_tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        required = {"Store", "AreaDir", "Company"}
        missing = required - set(up_tables)
        if missing:
            raise ValueError(f"uploaded db is missing required tables: {sorted(missing)}")
        store_rows = src.execute("SELECT COUNT(*) FROM Store").fetchone()[0]
        if store_rows < 50:
            raise ValueError(
                f"uploaded db has only {store_rows} Store rows — looks like a "
                "bad/partial export, refusing to replace the live mirror")
    finally:
        src.close()

    refresh_tables = [t for t in up_tables if t not in PRESERVE]

    # ── 2. in-place transactional refresh on the live DB ──
    conn = sqlite3.connect(CONTACTS_DB_PATH, timeout=60)
    refreshed: Dict[str, int] = {}
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS src", (uploaded_path,))
        conn.execute("BEGIN IMMEDIATE")
        for t in refresh_tables:
            create_sql = conn.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?",
                (t,),
            ).fetchone()[0]
            conn.execute(f'DROP TABLE IF EXISTS main."{t}"')
            conn.execute(create_sql)                                  # recreate in main from export's schema
            conn.execute(f'INSERT INTO main."{t}" SELECT * FROM src."{t}"')
            refreshed[t] = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE src")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ── 3. restore indexes + ensure app-local tables still exist ──
    # The transaction above already committed the refreshed data — the import
    # has SUCCEEDED at this point. initialize_schema() is purely supplementary
    # (rebuilds the app's named performance indexes and backfills the app-local
    # tables for a cold-start DB). Its CREATE INDEX statements hardcode columns
    # like Store(StoreState); if a future export ever drops such a column it
    # would raise here — but the data is already safe and the app-local tables
    # are preserved/never dropped, so a failure must NOT surface as a failed
    # import. Guard it: log and move on, leaving data intact.
    try:
        initialize_schema()
    except sqlite3.OperationalError as exc:
        logger.warning(
            "import_contacts_db: data refreshed OK, but rebuilding indexes/skeleton "
            "hit a schema mismatch (%s) — likely an export schema change. Data is "
            "intact; some performance indexes may be missing.", exc)

    logger.info("import_contacts_db: refreshed %d tables in place (%d Store rows)",
                len(refreshed), store_rows)
    return {
        "store_rows":      store_rows,
        "tables_refreshed": sorted(refreshed),
        "row_counts":      refreshed,
    }


def list_outbox_rows(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """List outbox rows for the admin page.
    status: 'pending', 'success', 'error', or None for all."""
    with get_connection() as conn:
        if status == 'pending':
            where = "WHERE applied_status IS NULL"
        elif status in ('success', 'error'):
            where = f"WHERE applied_status = '{status}'"
        else:
            where = ""
        rows = conn.execute(
            f"SELECT * FROM contacts_outbox {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def claim_pending_outbox(batch_size: int = 100) -> List[Dict[str, Any]]:
    """Drainer-side: return the next batch of pending rows + bump their
    attempt counter. The drainer applies each row, then calls
    mark_outbox_applied() to record the result."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts_outbox "
            "WHERE applied_status IS NULL "
            "ORDER BY created_at LIMIT ?",
            (batch_size,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE contacts_outbox SET attempts = attempts + 1 "
                f"WHERE id IN ({placeholders})",
                tuple(ids),
            )
            conn.commit()
        return [_row_to_dict(r) for r in rows]


def mark_outbox_applied(row_id: int, success: bool, error_message: Optional[str] = None) -> bool:
    """Drainer posts back: row N succeeded or failed."""
    status = 'success' if success else 'error'
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE contacts_outbox "
            "SET applied_status = ?, applied_at = datetime('now'), error_message = ? "
            "WHERE id = ?",
            (status, error_message, row_id),
        )
        conn.commit()
        return cur.rowcount > 0


def retry_outbox_row(row_id: int) -> bool:
    """Manual admin action: reset a failed row back to pending. Also
    resets the attempt counter so the drainer gets its full retry budget
    on the next run."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE contacts_outbox "
            "SET applied_status = NULL, "
            "    applied_at = NULL, "
            "    error_message = NULL, "
            "    attempts = 0 "
            "WHERE id = ? AND applied_status = 'error'",
            (row_id,),
        )
        conn.commit()
        return cur.rowcount > 0


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
        _record_outbox("INSERT", "AreaDir", "AreaDirID", new_id, {
            "AreaDirName":      full_name,
            "AreaDirFirstName": data.get("AreaDirFirstName") or None,
            "AreaDirLastName":  data.get("AreaDirLastName")  or None,
            "AreaDirEmail":     data.get("AreaDirEmail")     or None,
            "AreaDirSMGEmail":  data.get("AreaDirSMGEmail")  or None,
            "AreaDirCFPW":      data.get("AreaDirCFPW")      or None,
            "AreaDirCFID":      data.get("AreaDirCFID")      or None,
            "CompanyID":        data.get("CompanyID"),
        }, updated_by, conn=conn)
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
        if affected > 0:
            _record_outbox("UPDATE", "AreaDir", "AreaDirID", areadir_id, {
                "AreaDirName":      full_name,
                "AreaDirFirstName": data.get("AreaDirFirstName") or None,
                "AreaDirLastName":  data.get("AreaDirLastName")  or None,
                "AreaDirEmail":     data.get("AreaDirEmail")     or None,
                "AreaDirSMGEmail":  data.get("AreaDirSMGEmail")  or None,
                "AreaDirCFPW":      data.get("AreaDirCFPW")      or None,
                "AreaDirCFID":      data.get("AreaDirCFID")      or None,
                "CompanyID":        data.get("CompanyID"),
            }, updated_by, conn=conn)
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
        if affected > 0:
            _record_outbox("DELETE", "AreaDir", "AreaDirID", areadir_id, {},
                           updated_by, conn=conn)
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


# ──────────────────────────────────────────────────────────────────────────────
# Ownership — Company-owned vs Franchisee
#
# contacts.dbo.Store has no plain "is this ours?" bit. The signal is spread
# across two FK lookups (Company = the Corp / Franchisee designation,
# OwnerShip = the legal entity that owns the store) plus
# backend/store_ownership.json, which the admin "Store Information Excel"
# upload refreshes and which the Flash / WPY / legacy pages already treat as
# authoritative for Company-vs-Franchise filtering.
#
# resolve_store_ownership() folds all three into one label so the Stores list
# can render a single column. Precedence:
#   1. A literal designation in the Company / OwnerShip name ("Company",
#      "Franchise") — that IS the answer, no guessing involved.
#   2. store_ownership.json, keyed by StoreID — curated, Excel-backed.
#   3. Entity-name hints (Newco Dining LLC = ours) — heuristic last resort.
# Anything unresolved comes back as None so the UI can show "—" rather than
# guessing a store into the wrong bucket.
# ──────────────────────────────────────────────────────────────────────────────

OWNERSHIP_JSON_PATH = os.path.join(os.path.dirname(__file__), "store_ownership.json")

# Entity names that mean "Newk's owns it". Every company store sits under
# Newco Dining LLC today; the extra spellings are here so a rename or an older
# export degrades to "unknown" rather than silently reading as a franchisee.
_COMPANY_ENTITY_HINTS = (
    "newco dining", "newk's franchise co", "newks franchise co",
    "company restaurant", "company store",
)
_COMPANY_WORDS = ("company", "company owned", "company-owned", "corp",
                  "corporate", "corporate owned", "corporate-owned")

_ownership_json_cache: Dict[str, Any] = {"mtime": None, "data": {}}


def _classify_entity_name(name: Any) -> Optional[str]:
    """'Newco Dining LLC' -> 'Company', 'Temcomp LLC' -> None, 'Franchise' ->
    'Franchise'. None means "this name doesn't tell us" — the caller falls
    through to the next source."""
    n = str(name or "").strip().lower()
    if not n:
        return None
    if n in _COMPANY_WORDS or any(h in n for h in _COMPANY_ENTITY_HINTS):
        return "Company"
    # Checked AFTER the company hints on purpose: "Newk's Franchise Company"
    # is us, not a franchisee.
    if "franchis" in n:
        return "Franchise"
    return None


def _load_ownership_json() -> Dict[str, Dict[str, Any]]:
    """store_ownership.json, reloaded when the file changes on disk (the
    admin Excel upload rewrites it under a running uvicorn)."""
    try:
        mtime = os.path.getmtime(OWNERSHIP_JSON_PATH)
    except OSError:
        return {}
    if _ownership_json_cache["mtime"] != mtime:
        try:
            with open(OWNERSHIP_JSON_PATH, "r", encoding="utf-8") as fh:
                _ownership_json_cache["data"] = json.load(fh) or {}
            _ownership_json_cache["mtime"] = mtime
        except Exception as exc:
            logger.warning("store_ownership.json unreadable: %s", exc)
            return {}
    return _ownership_json_cache["data"]


def _lookup_name_map(table: str, pk: str, label_col: str) -> Dict[Any, str]:
    """{id: name} for a lookup table, or {} if the table isn't in the mirror."""
    with get_connection() as conn:
        try:
            rows = conn.execute(f"SELECT {pk}, {label_col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            return {}
    return {r[0]: r[1] for r in rows if r[0] is not None}


def resolve_store_ownership(rows: List[Dict[str, Any]]) -> None:
    """Stamp OwnershipType / OwnershipEntity / OwnershipSource onto each store
    dict, in place. One pass over the lookup tables for the whole list — no
    per-store queries."""
    cols = set(_store_columns())
    companies = (_lookup_name_map("Company", "CompanyID", "CompanyName")
                 if "StoreCompany" in cols else {})
    owners = (_lookup_name_map("OwnerShip", "OwnerShipID", "OwnerShipName")
              if "StoreOwnershipID" in cols else {})
    by_id = _load_ownership_json()

    for r in rows:
        company_name = companies.get(r.get("StoreCompany"))
        owner_name = owners.get(r.get("StoreOwnershipID"))
        js = by_id.get(str(r.get("StoreID"))) or {}

        entity = owner_name or company_name or js.get("companyName") or None
        otype = _classify_entity_name(owner_name) or _classify_entity_name(company_name)
        source = "contacts" if otype else None

        if not otype:
            jt = str(js.get("ownershipType") or "").strip().lower()
            if jt.startswith("company") or jt.startswith("corp"):
                otype, source = "Company", "store_ownership.json"
            elif jt.startswith("franchis"):
                otype, source = "Franchise", "store_ownership.json"

        r["OwnershipType"] = otype
        r["OwnershipEntity"] = entity
        r["OwnershipSource"] = source


def list_stores() -> List[Dict[str, Any]]:
    """Return every Store with every column, plus the derived Ownership*
    fields (see resolve_store_ownership). The list view picks which columns
    to show; edit modal renders them all."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM Store ORDER BY LOWER(COALESCE(StoreName, ''))"
        ).fetchall()
        stores = [_row_to_dict(r) for r in rows]
    try:
        resolve_store_ownership(stores)
    except Exception as exc:
        # Never let the ownership column take the whole directory down.
        logger.warning("resolve_store_ownership failed: %s", exc, exc_info=True)
    return stores


def list_comping_store_ids() -> List[str]:
    """
    Return the StoreIDs (as strings) of every store whose master
    StoreIsComping flag is set. This is the single source of truth for the
    Daily Sales Flash "comp restaurants" set — mirrors the SQL-PROD
    contacts.dbo.Store.StoreIsComping bit that Ops maintains in the app's
    store-detail page. Truthy convention matches the frontend flagOn():
    1 / '1' / true / 'true' / 'Y'.
    Returns [] if the column is absent (older schema) rather than erroring.
    """
    if "StoreIsComping" not in _store_columns():
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT StoreID, StoreIsComping FROM Store"
        ).fetchall()
    ids: List[str] = []
    for r in rows:
        v = r["StoreIsComping"]
        if isinstance(v, str):
            v = v.strip().lower()
        if v in (1, True, "1", "true", "y"):
            ids.append(str(r["StoreID"]))
    return ids


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
    # Build the outbox payload alongside the SQL — record what we actually
    # changed (sans protected/unknown cols) so the SQL Server replay matches.
    outbox_payload: Dict[str, Any] = {}
    for k, v in data.items():
        if k in STORE_PROTECTED_COLUMNS or k not in valid_cols:
            continue
        sets.append(f"{k} = ?")
        normalized = v if v not in ("", None) else None
        values.append(normalized)
        outbox_payload[k] = normalized
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
        if affected > 0 and outbox_payload:
            _record_outbox("UPDATE", "Store", "StoreID", store_id,
                           outbox_payload, updated_by, conn=conn)
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
    outbox_payload: Dict[str, Any] = {"StoreID": int(data["StoreID"])}
    for k, v in data.items():
        if k in ("StoreID", "LastUpdated") or k not in valid_cols:
            continue
        cols.append(k)
        normalized = v if v not in ("", None) else None
        values.append(normalized)
        outbox_payload[k] = normalized
    cols.append("LastUpdatedBy")
    values.append(updated_by)
    cols.append("LastUpdated")
    placeholders = ", ".join("?" * (len(values))) + ", datetime('now')"
    col_list = ", ".join(cols)
    sql = f"INSERT INTO Store ({col_list}) VALUES ({placeholders})"
    with get_connection() as conn:
        conn.execute(sql, tuple(values))
        _record_outbox("INSERT", "Store", "StoreID", int(data["StoreID"]),
                       outbox_payload, updated_by, conn=conn)
        conn.commit()
        return int(data["StoreID"])


# ──────────────────────────────────────────────────────────────────────────────
# Store satellites — StorePhysical (1:1), StoreTechnical (1:1), StoreClosure (1:many)
#
# Same column-agnostic pattern as Store: introspect the SQLite schema and
# operate on whatever columns the exporter dropped in. StorePhysical and
# StoreTechnical are both keyed off StoreID — Physical uses StoreID directly
# as PK, Technical has its own StoreTechnicalID PK with a lowercase `storeid`
# FK column (matches SQL Server schema verbatim).
# ──────────────────────────────────────────────────────────────────────────────

def _table_columns(table: str) -> List[str]:
    """Return the columns actually present in the given SQLite table."""
    with get_connection() as conn:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return [r["name"] for r in rows]
        except sqlite3.OperationalError:
            return []


# Per-table audit column naming. SQL Server's contacts schema is inconsistent
# here: StorePhysical uses LastEdited/LastEditedBy (Don's older convention)
# while StoreTechnical, Store, AreaDir, etc. use LastUpdated/LastUpdatedBy.
# We respect whatever the target table actually has so the schema stays
# faithful to SQL Server (matters for the eventual sync-back step).
#   (audit_by_col, audit_at_col)
_SATELLITE_AUDIT = {
    "StorePhysical":  ("LastEditedBy",  "LastEdited"),
    "StoreTechnical": ("LastUpdatedBy", "LastUpdated"),
}

# Per-table protected columns — never accepted from user payloads.
# Includes PK, FK-to-Store, and both audit columns.
_SATELLITE_PROTECTED = {
    "StorePhysical":  {"StoreID", "LastEdited", "LastEditedBy"},
    "StoreTechnical": {"StoreTechnicalID", "storeid", "LastUpdated", "LastUpdatedBy"},
}


def _upsert_satellite(table: str, store_id: int, data: Dict[str, Any], updated_by: str,
                      fk_column: str) -> bool:
    """
    Generic upsert for a 1:1 satellite table keyed off Store.
    - If a row exists for this store, UPDATE its non-protected columns.
    - Otherwise INSERT a fresh row with the supplied data.

    Audit columns (LastEdited/LastEditedBy for Physical, LastUpdated/
    LastUpdatedBy for Technical) are stamped server-side per table.
    Unknown/protected columns are silently dropped, matching the Store
    pattern — keeps a stale frontend from breaking a save.
    """
    valid_cols = set(_table_columns(table))
    if not valid_cols:
        logger.warning("Satellite table %s missing — exporter hasn't created it yet", table)
        return False
    protected = _SATELLITE_PROTECTED.get(table, {"LastUpdated", "LastUpdatedBy"})
    audit_by, audit_at = _SATELLITE_AUDIT.get(table, ("LastUpdatedBy", "LastUpdated"))

    # Only stamp audit columns that actually exist in the destination table.
    # Defends against schema drift / older exports.
    stamp_by = audit_by if audit_by in valid_cols else None
    stamp_at = audit_at if audit_at in valid_cols else None

    with get_connection() as conn:
        existing = conn.execute(
            f"SELECT 1 FROM {table} WHERE {fk_column} = ? LIMIT 1",
            (store_id,),
        ).fetchone()

        if existing:
            sets: List[str] = []
            values: List[Any] = []
            outbox_payload: Dict[str, Any] = {}
            for k, v in data.items():
                if k in protected or k not in valid_cols:
                    continue
                sets.append(f"{k} = ?")
                normalized = v if v not in ("", None) else None
                values.append(normalized)
                outbox_payload[k] = normalized
            if stamp_by:
                sets.append(f"{stamp_by} = ?")
                values.append(updated_by)
            if stamp_at:
                sets.append(f"{stamp_at} = datetime('now')")
            values.append(store_id)
            sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {fk_column} = ?"
            cur = conn.execute(sql, tuple(values))
            if cur.rowcount > 0 and outbox_payload:
                # Outbox key = the FK column / value used by SQL Server to
                # locate the row (StoreID for Physical, storeid for Technical).
                _record_outbox("UPDATE", table, fk_column, store_id,
                               outbox_payload, updated_by, conn=conn)
            conn.commit()
            return cur.rowcount > 0

        # INSERT path — supply FK + whatever payload columns are valid.
        cols: List[str] = [fk_column]
        values_ins: List[Any] = [store_id]
        outbox_payload = {fk_column: store_id}
        for k, v in data.items():
            if k in protected or k == fk_column or k not in valid_cols:
                continue
            cols.append(k)
            normalized = v if v not in ("", None) else None
            values_ins.append(normalized)
            outbox_payload[k] = normalized
        # Stamp audit columns inline at INSERT time.
        if stamp_by:
            cols.append(stamp_by)
            values_ins.append(updated_by)
        placeholders_parts = ["?"] * len(values_ins)
        if stamp_at:
            cols.append(stamp_at)
            placeholders_parts.append("datetime('now')")
        placeholders = ", ".join(placeholders_parts)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        conn.execute(sql, tuple(values_ins))
        _record_outbox("INSERT", table, fk_column, store_id,
                       outbox_payload, updated_by, conn=conn)
        conn.commit()
        return True


def get_store_physical(store_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        try:
            row = conn.execute(
                "SELECT * FROM StorePhysical WHERE StoreID = ?", (store_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        except sqlite3.OperationalError:
            return None


def update_store_physical(store_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    return _upsert_satellite("StorePhysical", store_id, data, updated_by, fk_column="StoreID")


def get_store_technical(store_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        try:
            # StoreTechnical FK is lowercase `storeid` per SQL Server schema.
            row = conn.execute(
                "SELECT * FROM StoreTechnical WHERE storeid = ?", (store_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        except sqlite3.OperationalError:
            return None


def update_store_technical(store_id: int, data: Dict[str, Any], updated_by: str) -> bool:
    return _upsert_satellite("StoreTechnical", store_id, data, updated_by, fk_column="storeid")


def list_store_closures(store_id: int) -> List[Dict[str, Any]]:
    """Return all closure history rows for a store. Read-only in v1."""
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM StoreClosure WHERE StoreID = ? ORDER BY StoreClosureID DESC",
                (store_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []


def list_general_managers_by_store(store_id: int) -> List[Dict[str, Any]]:
    """Return all GeneralManagers rows for a store. Most stores have 1 GM,
    but the schema allows multiple. Returns [] if the table doesn't exist
    yet or has no rows for this store."""
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM GeneralManagers WHERE StoreID = ? "
                "ORDER BY LOWER(COALESCE(GeneralMgrName, LastName, ''))",
                (store_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("list_general_managers_by_store(%s) failed: %s",
                           store_id, exc)
            return []


def list_delivery_partners_by_store(store_id: int) -> List[Dict[str, Any]]:
    """Return all DeliveryPartnersByStore rows for a store. Read-only in v1
    — editing UI (add/remove a partner) comes in a future round.

    Defensive about column names: we know it has a StoreID FK and probably
    DelivPartID, but other columns vary. SELECT * lets the frontend render
    whatever's there. If the table doesn't exist, return [] gracefully."""
    with get_connection() as conn:
        try:
            # Filter by StoreID — that's the one column we're confident about
            # since it's the FK to Store and matches the documented relationship.
            rows = conn.execute(
                "SELECT * FROM DeliveryPartnersByStore WHERE StoreID = ?",
                (store_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("list_delivery_partners_by_store(%s) failed: %s",
                           store_id, exc)
            return []


# ──────────────────────────────────────────────────────────────────────────────
# StoreOLOVendor — app-owned per-store OLO Vendor ID (see APP_LOCAL_SCHEMA_SQL).
#
# NOT synced to SQL Server (no column there) so these helpers never touch the
# outbox. The table is in the PRESERVE set so it survives a mirror refresh.
# ──────────────────────────────────────────────────────────────────────────────

def get_store_olo_vendor(store_id: int) -> Optional[str]:
    """Return the OLO Vendor ID string for a store, or None if unset."""
    with get_connection() as conn:
        try:
            row = conn.execute(
                "SELECT OLOVendorID FROM StoreOLOVendor WHERE StoreID = ?",
                (store_id,),
            ).fetchone()
            return row["OLOVendorID"] if row else None
        except sqlite3.OperationalError:
            return None


def get_olo_vendor_map() -> Dict[int, str]:
    """Return {StoreID: OLOVendorID} for every store that has one — feeds the
    Contact Information report so it doesn't do an N+1 per-store lookup."""
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT StoreID, OLOVendorID FROM StoreOLOVendor "
                "WHERE OLOVendorID IS NOT NULL AND OLOVendorID <> ''"
            ).fetchall()
            return {int(r["StoreID"]): r["OLOVendorID"] for r in rows}
        except sqlite3.OperationalError:
            return {}


def set_store_olo_vendor(store_id: int, olo_vendor_id: Optional[str],
                         updated_by: str) -> bool:
    """Upsert (or clear) a store's OLO Vendor ID.

    A blank/None value deletes the row rather than storing an empty string,
    keeping get_olo_vendor_map() clean. Returns True on any successful write.
    App-owned data: intentionally does NOT record to the outbox.
    """
    normalized = (str(olo_vendor_id).strip() if olo_vendor_id is not None else "")
    with get_connection() as conn:
        if normalized == "":
            conn.execute("DELETE FROM StoreOLOVendor WHERE StoreID = ?", (store_id,))
            conn.commit()
            return True
        # SQLite UPSERT — insert or update the single row for this store.
        conn.execute(
            """
            INSERT INTO StoreOLOVendor (StoreID, OLOVendorID, LastUpdatedBy, LastUpdated)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(StoreID) DO UPDATE SET
                OLOVendorID   = excluded.OLOVendorID,
                LastUpdatedBy = excluded.LastUpdatedBy,
                LastUpdated   = datetime('now')
            """,
            (store_id, normalized, updated_by),
        )
        conn.commit()
        return True


def _resolve_lookup_full(name: str, row_id: Any) -> Optional[Dict[str, Any]]:
    """Look up a single lookup-table row by PK, return the FULL row dict
    (not just id/label). Used by get_store_full() so the profile page can
    show emails / phones / addresses for resolved FKs without N+1 fetches.
    None on missing id or table-not-loaded — caller falls back gracefully.
    """
    if row_id is None or row_id == "":
        return None
    spec = _lookup_spec(name)
    if not spec:
        return None
    table, pk = spec["table"], spec["pk"]
    with get_connection() as conn:
        try:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE {pk} = ? LIMIT 1", (row_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        except sqlite3.OperationalError:
            return None


def get_store_full(store_id: int) -> Optional[Dict[str, Any]]:
    """Return Store + Physical + Technical + closures + delivery partners
    + GMs + resolved contact dicts in one payload. Lets the profile page
    render rich contact info (emails/phones/addresses for AreaDir,
    Primary, Billing, Catering Mgr, Company, Ownership) without making
    a separate round-trip for each FK."""
    base = get_store(store_id)
    if not base:
        return None
    # OLO Vendor ID is app-owned (StoreOLOVendor table), not a Store column.
    # Fold it into the store dict so the edit form + profile page can treat it
    # like any other Store field for display/populate purposes.
    base["OLOVendorID"] = get_store_olo_vendor(store_id)
    physical          = get_store_physical(store_id)        or {}
    technical         = get_store_technical(store_id)       or {}
    closures          = list_store_closures(store_id)
    delivery_partners = list_delivery_partners_by_store(store_id)
    general_managers  = list_general_managers_by_store(store_id)

    # Resolve each FK to its full row so the frontend can show emails
    # and phones, not just names. Each value is either a dict or None.
    contacts_resolved = {
        "company":         _resolve_lookup_full("company",        base.get("StoreCompany")),
        "ownership":       _resolve_lookup_full("ownership",      base.get("StoreOwnershipID")),
        "area_director":   _resolve_lookup_full("areadir",        base.get("StoreAreaDirID")),
        "fbc":             _resolve_lookup_full("fbc",            base.get("StoreFBCID")),
        "fmm":             _resolve_lookup_full("fmm",            base.get("StoreFMMID")),
        "catering_mgr":    _resolve_lookup_full("cateringmgr",    base.get("StoreCateringMgrID")),
        "primary_contact": _resolve_lookup_full("primarycontact", base.get("StorePrimaryContact")),
        "billing":         _resolve_lookup_full("billing",        base.get("StoreBillingID")),
        "sysco":           _resolve_lookup_full("sysco",          base.get("StoreSyscoID")),
        "dma":             _resolve_lookup_full("dma",            base.get("StoreDMAID")),
    }

    return {
        "store":             base,
        "physical":          physical,
        "technical":         technical,
        "closures":          closures,
        "closure_count":     len(closures),
        "delivery_partners": delivery_partners,
        "general_managers":  general_managers,
        "contacts":          contacts_resolved,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Lookups — populate FK dropdowns in the edit UIs.
#
# Each entry: (table, id_col, name_col). The exporter brings these tables
# down from SQL Server; the frontend asks /api/contacts/lookups/{name}
# for the (id, label) pairs to render in <select> options.
# ──────────────────────────────────────────────────────────────────────────────

LOOKUPS: Dict[str, Dict[str, str]] = {
    "company": {
        "table": "Company", "pk": "CompanyID", "label_col": "CompanyName",
        "title": "Companies",
        "description": "Corp / Franchisee designations.",
    },
    "areadir": {
        # Note: AreaDir has its own bespoke CRUD page (/contacts-areadir.html)
        # so this entry exists primarily to feed the FK dropdown. The generic
        # page will still work if invoked, but the dedicated one is richer.
        "table": "AreaDir", "pk": "AreaDirID", "label_col": "AreaDirName",
        "title": "Area Directors",
        "description": "FBC names, emails, SMG distribution lists.",
    },
    "fbc": {
        "table": "FBC", "pk": "FBCID", "label_col": "FBCName",
        "title": "FBCs",
        "description": "Franchise Business Consultants directory.",
    },
    "fmm": {
        "table": "FMM", "pk": "FMMID", "label_col": "FMMName",
        "title": "FMMs",
        "description": "Field Marketing Managers directory.",
    },
    "dma": {
        "table": "DMA", "pk": "DMAID", "label_col": "DMAName",
        "title": "DMAs",
        "description": "Designated Market Areas (Nielsen regions).",
    },
    "ownership": {
        "table": "OwnerShip", "pk": "OwnerShipID", "label_col": "OwnerShipName",
        "title": "Ownership",
        "description": "Corp / Franchisee ownership entities.",
    },
    "cateringmgr": {
        "table": "CateringMgr", "pk": "CateringMgrID", "label_col": "CateringMgrName",
        "title": "Catering Managers",
        "description": "Per-region catering point of contact.",
    },
    "billing": {
        "table": "BillingContacts", "pk": "BillingID", "label_col": "BillingName",
        "title": "Billing Contacts",
        "description": "Accounts-payable contacts by company.",
    },
    "primarycontact": {
        "table": "PrimaryContacts", "pk": "PrimaryID", "label_col": "PrimaryName",
        "title": "Primary Contacts",
        "description": "Primary store / company contacts.",
    },
    "sysco": {
        "table": "SyscoMarket", "pk": "SyscoID", "label_col": "SyscoMarket",
        "title": "Sysco Markets",
        "description": "Sysco distribution market directory.",
    },

    # ── Vendors & services (round 5) ───────────────────────────────────
    # Best-guess PK / label column names based on Don's naming conventions.
    # If a guess is wrong, list_lookup() returns [] gracefully and the
    # dropdown will fall back to numeric IDs until we fix it.

    "ccprocessor": {
        "table": "CCProcessors", "pk": "CCProcessorID", "label_col": "CCProcessorName",
        "title": "CC Processors",
        "description": "Credit-card processor accounts.",
    },
    "phonevendor": {
        # FK column in StoreTechnical is PhoneVendorID — PK should match.
        "table": "PhoneVendors", "pk": "PhoneVendorID", "label_col": "PhoneVendorName",
        "title": "Phone Vendors",
        "description": "Phone-system vendor directory.",
    },
    "firewall": {
        # FK column in StoreTechnical is FireWallProvider (no ID suffix —
        # the SQL Server schema is inconsistent). Best guess for the PK.
        "table": "FireWallProvider", "pk": "FireWallProviderID", "label_col": "FireWallProviderName",
        "title": "Firewall Providers",
        "description": "Firewall vendor directory.",
    },
    "deliverypartner": {
        "table": "DeliveryPartners", "pk": "DelivPartID", "label_col": "DelivPartName",
        "title": "Delivery Partners",
        "description": "DoorDash, UberEats, etc. — vendor directory only. Per-store mapping comes from DeliveryPartnersByStore.",
    },
    "gm": {
        # SQL Server schema uses UniqueID (not GMID) as PK and GeneralMgrName
        # as the display label. Other columns (FirstName/LastName/EmailAddress/
        # CellPhone/StoreID) auto-render via the generic CRUD page.
        "table": "GeneralManagers", "pk": "UniqueID", "label_col": "GeneralMgrName",
        "title": "General Managers",
        "description": "Store GM directory.",
    },

    # ── StoreTechnical FK lookups (round 5) ────────────────────────────
    # Powers the dropdowns in the Technical section of the Store modal.

    "postype": {
        "table": "POSType", "pk": "POSTypeID", "label_col": "POSTypeName",
        "title": "POS Types",
        "description": "POS-system types (Aloha, Toast, etc.) — referenced by Store.fkPOSTypeID.",
    },
    "kiosktype": {
        "table": "KioskType", "pk": "KioskTypeID", "label_col": "KioskTypeName",
        "title": "Kiosk Types",
        "description": "Self-order kiosk hardware models — referenced by Store.fkKioskTypeID.",
    },
    "hsiservicetype": {
        # In SQL Server: PK is HSIServiceID, label column is HSIServiceType
        # (yes, the label column has the same name as the table — quirky but
        # consistent with Don's SyscoMarket pattern where the label = table).
        "table": "HSIServiceType", "pk": "HSIServiceID", "label_col": "HSIServiceType",
        "title": "HSI Service Types",
        "description": "Internet service plan types — referenced by Store.FKHSIServiceType.",
    },
    "cameravendor": {
        "table": "CameraVendors", "pk": "CameraVendorID", "label_col": "CameraVendorName",
        "title": "Camera Vendors",
        "description": "Security-camera vendor directory.",
    },
    "avsoftwaretype": {
        # SQL Server table is AVSoftwareTypes (plural) even though the FK
        # column in StoreTechnical is AVSoftwareTypeID (singular). PK/label
        # column names are best-guess; verify after re-export.
        "table": "AVSoftwareTypes", "pk": "AVSoftwareTypeID", "label_col": "AVSoftwareTypeName",
        "title": "A/V Software Types",
        "description": "Anti-virus software types — referenced by Store.AVSoftwareTypeID.",
    },
}


def _lookup_spec(name: str) -> Optional[Dict[str, str]]:
    return LOOKUPS.get((name or "").lower())


def list_lookup(name: str) -> List[Dict[str, Any]]:
    """Return [{id, label}] for the named lookup. Empty list on any error
    (missing table, schema drift) so a stale frontend gracefully degrades
    to the old number input instead of crashing the form."""
    spec = _lookup_spec(name)
    if not spec:
        return []
    table, id_col, name_col = spec["table"], spec["pk"], spec["label_col"]
    with get_connection() as conn:
        try:
            rows = conn.execute(
                f"SELECT {id_col} AS id, {name_col} AS label "
                f"FROM {table} "
                f"WHERE {id_col} IS NOT NULL "
                f"ORDER BY LOWER(COALESCE({name_col}, ''))"
            ).fetchall()
            return [{"id": r["id"], "label": r["label"]} for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("lookup %s failed (%s.%s/%s): %s",
                           name, table, id_col, name_col, exc)
            return []


# ──────────────────────────────────────────────────────────────────────────────
# Generic CRUD for lookup tables — powers /contacts-lookup.html?table=key
#
# The columns and audit-column naming vary per table (SQL Server inconsistency),
# so every helper here introspects PRAGMA table_info at runtime. Protected
# columns (PK + audit) are never accepted from the payload; audit columns are
# stamped server-side. Unknown columns are silently dropped so a stale frontend
# doesn't break a save.
# ──────────────────────────────────────────────────────────────────────────────

# Common audit column pairs we've seen in Don's schema. Whichever pair exists
# in the target table is what we stamp.
_AUDIT_COLUMN_CANDIDATES = [
    ("LastUpdatedBy", "LastUpdated"),
    ("LastEditedBy",  "LastEdited"),
]


def _detect_audit_columns(valid_cols: set) -> tuple:
    """Return (audit_by_col, audit_at_col) — whichever pair exists in the
    table, or (None, None) if neither is present."""
    for by_col, at_col in _AUDIT_COLUMN_CANDIDATES:
        if by_col in valid_cols or at_col in valid_cols:
            return (by_col if by_col in valid_cols else None,
                    at_col if at_col in valid_cols else None)
    return (None, None)


def get_lookup_meta(name: str) -> Optional[Dict[str, Any]]:
    """Return descriptor + column schema for the named lookup table.

    Used by the generic CRUD page to build the list view + edit modal
    dynamically. Empty `columns` list signals the SQLite table doesn't
    exist yet (export not run).
    """
    spec = _lookup_spec(name)
    if not spec:
        return None
    table = spec["table"]
    with get_connection() as conn:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            rows = []
    columns = [
        {
            "name":    r["name"],
            "type":    r["type"],
            "notnull": bool(r["notnull"]),
            "pk":      bool(r["pk"]),
        }
        for r in rows
    ]
    return {
        "key":         name.lower(),
        "table":       table,
        "pk":          spec["pk"],
        "label_col":   spec["label_col"],
        "title":       spec["title"],
        "description": spec["description"],
        "columns":     columns,
    }


def list_lookup_rows(name: str) -> List[Dict[str, Any]]:
    """Return every row in the named lookup table, ordered by label column."""
    spec = _lookup_spec(name)
    if not spec:
        return []
    table, label_col = spec["table"], spec["label_col"]
    with get_connection() as conn:
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} "
                f"ORDER BY LOWER(COALESCE({label_col}, ''))"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("list_lookup_rows %s failed: %s", name, exc)
            return []


def get_lookup_row(name: str, row_id: Any) -> Optional[Dict[str, Any]]:
    """Single-row fetch by PK."""
    spec = _lookup_spec(name)
    if not spec:
        return None
    table, pk = spec["table"], spec["pk"]
    with get_connection() as conn:
        try:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE {pk} = ?", (row_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        except sqlite3.OperationalError:
            return None


def update_lookup_row(name: str, row_id: Any, data: Dict[str, Any],
                      updated_by: str) -> bool:
    """Update non-protected columns. Audit cols stamped server-side."""
    spec = _lookup_spec(name)
    if not spec:
        return False
    table, pk = spec["table"], spec["pk"]
    valid_cols = set(_table_columns(table))
    if not valid_cols:
        return False
    audit_by, audit_at = _detect_audit_columns(valid_cols)
    protected = {pk} | {c for c in (audit_by, audit_at) if c}

    sets: List[str] = []
    values: List[Any] = []
    outbox_payload: Dict[str, Any] = {}
    for k, v in data.items():
        if k in protected or k not in valid_cols:
            continue
        sets.append(f"{k} = ?")
        normalized = v if v not in ("", None) else None
        values.append(normalized)
        outbox_payload[k] = normalized
    if audit_by:
        sets.append(f"{audit_by} = ?")
        values.append(updated_by)
    if audit_at:
        sets.append(f"{audit_at} = datetime('now')")
    if not sets:
        return False  # nothing valid to update
    values.append(row_id)
    sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {pk} = ?"
    with get_connection() as conn:
        cur = conn.execute(sql, tuple(values))
        if cur.rowcount > 0 and outbox_payload:
            _record_outbox("UPDATE", table, pk, row_id,
                           outbox_payload, updated_by, conn=conn)
        conn.commit()
        return cur.rowcount > 0


def insert_lookup_row(name: str, data: Dict[str, Any], updated_by: str) -> Any:
    """Insert a new row. PK may or may not be supplied:
      - If the payload includes the PK and it's not None, use it
      - Otherwise rely on SQLite's INTEGER PRIMARY KEY auto-increment
    Returns the resulting row's PK value.
    """
    spec = _lookup_spec(name)
    if not spec:
        raise ValueError(f"Unknown lookup '{name}'")
    table, pk = spec["table"], spec["pk"]
    valid_cols = set(_table_columns(table))
    if not valid_cols:
        raise RuntimeError(f"Table {table} doesn't exist yet (run exporter first)")
    audit_by, audit_at = _detect_audit_columns(valid_cols)
    protected_for_insert = {c for c in (audit_by, audit_at) if c}

    cols: List[str] = []
    values: List[Any] = []
    outbox_payload: Dict[str, Any] = {}
    supplied_pk = data.get(pk)
    if supplied_pk not in (None, ""):
        cols.append(pk)
        values.append(supplied_pk)
        outbox_payload[pk] = supplied_pk
    for k, v in data.items():
        if k == pk or k in protected_for_insert or k not in valid_cols:
            continue
        cols.append(k)
        normalized = v if v not in ("", None) else None
        values.append(normalized)
        outbox_payload[k] = normalized
    if audit_by:
        cols.append(audit_by)
        values.append(updated_by)
    placeholders = ["?"] * len(values)
    if audit_at:
        cols.append(audit_at)
        placeholders.append("datetime('now')")
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
    with get_connection() as conn:
        cur = conn.execute(sql, tuple(values))
        new_id = supplied_pk if supplied_pk not in (None, "") else cur.lastrowid
        # Include the new PK in the outbox payload so SQL Server INSERT
        # picks it up (matters when SQLite auto-assigned an INTEGER PK).
        outbox_payload[pk] = new_id
        _record_outbox("INSERT", table, pk, new_id,
                       outbox_payload, updated_by, conn=conn)
        conn.commit()
        return new_id


def delete_lookup_row(name: str, row_id: Any, updated_by: str) -> bool:
    """Delete a row. Stamps audit-by first (matches Don's trigger-friendly
    pattern, so a future SQL-Server sync-back can capture the deleter)."""
    spec = _lookup_spec(name)
    if not spec:
        return False
    table, pk = spec["table"], spec["pk"]
    valid_cols = set(_table_columns(table))
    if not valid_cols:
        return False
    audit_by, audit_at = _detect_audit_columns(valid_cols)
    with get_connection() as conn:
        # Stamp audit cols (if they exist) before the delete fires.
        if audit_by or audit_at:
            stamps, vals = [], []
            if audit_by:
                stamps.append(f"{audit_by} = ?")
                vals.append(updated_by)
            if audit_at:
                stamps.append(f"{audit_at} = datetime('now')")
            vals.append(row_id)
            conn.execute(
                f"UPDATE {table} SET {', '.join(stamps)} WHERE {pk} = ?",
                tuple(vals),
            )
        cur = conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (row_id,))
        if cur.rowcount > 0:
            _record_outbox("DELETE", table, pk, row_id, {},
                           updated_by, conn=conn)
        conn.commit()
        return cur.rowcount > 0


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
# Restaurant Contact Information report
#
# Rebuilds the legacy SSRS "Restaurant Contact Information Sheet" entirely from
# the app's own contacts mirror + the app-owned StoreOLOVendor table + the
# app's Toast location mapping. This is what lets the SQL Server OLO ETL (and
# its licensed ZappySys SSIS package on NEWKS-SQL) be retired — the report no
# longer touches SQL Server at all.
#
# 26 columns, matching the SSRS layout / the "Restaurant Contact Information
# Sheet" xlsx export the office has always used.
# ──────────────────────────────────────────────────────────────────────────────

# Column order + header labels for the report (drives both the JSON payload
# key order and the CSV/print export). Kept in one place so the frontend table
# and any CSV stay in lock-step with the SSRS original.
CONTACT_REPORT_COLUMNS = [
    ("restaurant_id",         "Restaurant ID"),
    ("restaurant_name",       "Restaurant Name"),
    ("olo_vendor_id",         "OLO Vendor ID"),
    ("toast_guid",            "Toast GUID"),
    ("restaurant_status",     "Restaurant Status"),
    ("ownership",             "Ownership"),
    ("group_name",            "Group Name"),
    ("address",               "Address"),
    ("city",                  "City"),
    ("state",                 "State"),
    ("zip",                   "Zip"),
    ("phone",                 "Phone"),
    ("restaurant_email",      "Restaurant Email"),
    ("general_manager",       "General Manager"),
    ("gm_email",              "GM Email"),
    ("gm_cell_phone",         "GM Cell Phone"),
    ("primary_contact",       "Primary Contact"),
    ("primary_contact_email", "Primary Contact Email"),
    ("primary_contact_phone", "Primary Contact Phone"),
    ("area_director",         "Area Director Name"),
    ("area_director_email",   "Area Director Email"),
    ("billing_name",          "Billing Name"),
    ("billing_email",         "Billing Email"),
    ("billing_phone",         "Billing Phone"),
    ("sysco_market",          "Sysco Market"),
    ("dma",                   "DMA"),
    ("firewall_provider",     "Firewall Provider"),
]


def _load_lookup_full_by_pk(name: str) -> Dict[Any, Dict[str, Any]]:
    """Return {pk_value: full_row_dict} for a lookup table — one query, so the
    report can resolve FKs without an N+1 per store. Empty on any error."""
    spec = _lookup_spec(name)
    if not spec:
        return {}
    table, pk = spec["table"], spec["pk"]
    out: Dict[Any, Dict[str, Any]] = {}
    with get_connection() as conn:
        try:
            for r in conn.execute(f"SELECT * FROM {table}").fetchall():
                d = _row_to_dict(r)
                out[d.get(pk)] = d
        except sqlite3.OperationalError as exc:
            logger.warning("report: lookup %s (%s) load failed: %s", name, table, exc)
    return out


def _all_gms_by_store() -> Dict[int, List[Dict[str, Any]]]:
    """Return {StoreID: [gm_row, ...]} for every store in one query."""
    out: Dict[int, List[Dict[str, Any]]] = {}
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM GeneralManagers "
                "ORDER BY LOWER(COALESCE(GeneralMgrName, LastName, ''))"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("report: GeneralManagers load failed: %s", exc)
            return out
    for r in rows:
        d = _row_to_dict(r)
        sid = d.get("StoreID")
        if sid is None:
            continue
        out.setdefault(int(sid), []).append(d)
    return out


def _all_store_technical_by_id() -> Dict[int, Dict[str, Any]]:
    """Return {StoreID: StoreTechnical row} for every store in one query.
    The FK to Store is the lowercase `storeid` column (SQL Server schema)."""
    out: Dict[int, Dict[str, Any]] = {}
    with get_connection() as conn:
        try:
            rows = conn.execute("SELECT * FROM StoreTechnical").fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("report: StoreTechnical load failed: %s", exc)
            return out
    for r in rows:
        d = _row_to_dict(r)
        sid = d.get("storeid")
        if sid is None:
            continue
        out[int(sid)] = d
    return out


def _all_store_physical_by_id() -> Dict[int, Dict[str, Any]]:
    """Return {StoreID: StorePhysical row} for every store in one query.
    StorePhysical is keyed directly on StoreID (its PK)."""
    out: Dict[int, Dict[str, Any]] = {}
    with get_connection() as conn:
        try:
            rows = conn.execute("SELECT * FROM StorePhysical").fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("report: StorePhysical load failed: %s", exc)
            return out
    for r in rows:
        d = _row_to_dict(r)
        sid = d.get("StoreID")
        if sid is None:
            continue
        out[int(sid)] = d
    return out


def _toast_guid_map() -> Dict[int, str]:
    """Return {StoreID: Toast restaurant GUID}.

    Combines two sources so no store is dropped: the app's canonical
    Aloha→Toast location mapping (data/aloha/aloha_location_mapping.json,
    keyed guid→id) as the base, then overlays any GUID column the Store
    mirror happens to carry (more authoritative when present).
    """
    out: Dict[int, str] = {}

    # 1) Base: the location mapping file (guid -> store id string).
    path = os.path.join(os.path.dirname(__file__), "data", "aloha",
                        "aloha_location_mapping.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for guid, sid in (json.load(fh) or {}).items():
                try:
                    out[int(sid)] = guid
                except (ValueError, TypeError):
                    continue
    except (OSError, ValueError) as exc:
        logger.warning("report: location mapping load failed: %s", exc)

    # 2) Overlay: a Store-table GUID column, if this mirror has one.
    guid_col = next((c for c in _store_columns() if "guid" in c.lower()), None)
    if guid_col:
        with get_connection() as conn:
            try:
                rows = conn.execute(
                    f"SELECT StoreID, {guid_col} AS guid FROM Store "
                    f"WHERE {guid_col} IS NOT NULL AND {guid_col} <> ''"
                ).fetchall()
                for r in rows:
                    try:
                        out[int(r["StoreID"])] = r["guid"]
                    except (ValueError, TypeError):
                        continue
            except sqlite3.OperationalError:
                pass
    return out


def _pick(row: Optional[Dict[str, Any]], *keys: str) -> Optional[str]:
    """Return the first non-empty value among keys in row, else None."""
    if not row:
        return None
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _report_date(v: Any) -> Optional[str]:
    """Normalize a stored datetime/date string to YYYY-MM-DD for a report cell.
    Treats 1900/9999 sentinel dates (Don's SQL Server placeholders) as blank."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.startswith("1900") or s.startswith("9999"):
        return None
    return s[:10]  # ISO 'YYYY-MM-DD...' -> date portion


def _yes_no(v: Any) -> Optional[str]:
    """Render a bit/flag column as Yes/No; blank when unset."""
    if v in (None, ""):
        return None
    return "Yes" if v in (1, True, "1", "true", "True") else "No"


def get_contact_information_report() -> Dict[str, Any]:
    """Build the full Restaurant Contact Information dataset.

    Returns {"columns": [{key, label}...], "rows": [{key: value, ...}...]}.
    One pass over Store + a handful of bulk lookup loads (no N+1).
    """
    stores = list_stores()

    company_by  = _load_lookup_full_by_pk("company")
    owner_by    = _load_lookup_full_by_pk("ownership")
    areadir_by  = _load_lookup_full_by_pk("areadir")
    primary_by  = _load_lookup_full_by_pk("primarycontact")
    billing_by  = _load_lookup_full_by_pk("billing")
    sysco_by    = _load_lookup_full_by_pk("sysco")
    dma_by      = _load_lookup_full_by_pk("dma")

    gms_by      = _all_gms_by_store()
    olo_by      = get_olo_vendor_map()
    guid_by     = _toast_guid_map()
    tech_by     = _all_store_technical_by_id()
    # Firewall provider id -> name, resolved the same way the rest of the app
    # does (via the 'firewall' lookup spec) so the label column stays central.
    firewall_labels = {r["id"]: r["label"] for r in list_lookup("firewall")}

    def _addr(s: Dict[str, Any]) -> Optional[str]:
        parts = [s.get("StoreAdd1"), s.get("StoreAdd2"), s.get("StoreAdd3")]
        parts = [str(p).strip() for p in parts if p not in (None, "") and str(p).strip()]
        return ", ".join(parts) if parts else None

    def _gm_name(g: Dict[str, Any]) -> Optional[str]:
        return (g.get("GeneralMgrName")
                or " ".join(x for x in [g.get("FirstName"), g.get("LastName")] if x).strip()
                or None)

    rows: List[Dict[str, Any]] = []
    for s in stores:
        sid = s.get("StoreID")
        company = company_by.get(s.get("StoreCompany"))
        owner   = owner_by.get(s.get("StoreOwnershipID"))
        areadir = areadir_by.get(s.get("StoreAreaDirID"))
        primary = primary_by.get(s.get("StorePrimaryContact"))
        billing = billing_by.get(s.get("StoreBillingID"))
        sysco   = sysco_by.get(s.get("StoreSyscoID"))
        dma     = dma_by.get(s.get("StoreDMAID"))
        gms     = gms_by.get(int(sid), []) if sid is not None else []
        gm      = gms[0] if gms else None
        tech    = tech_by.get(int(sid)) if sid is not None else None
        fw_id   = tech.get("FireWallProvider") if tech else None

        rows.append({
            "restaurant_id":         sid,
            "restaurant_name":       s.get("StoreName"),
            "olo_vendor_id":         olo_by.get(int(sid)) if sid is not None else None,
            "toast_guid":            guid_by.get(int(sid)) if sid is not None else None,
            "restaurant_status":     s.get("StoreStatus"),
            "ownership":             _pick(company, "CompanyName"),
            "group_name":            _pick(owner, "OwnerShipName"),
            "address":               _addr(s),
            "city":                  s.get("StoreCity"),
            "state":                 s.get("StoreState"),
            "zip":                   s.get("StoreZip"),
            "phone":                 s.get("StorePhone"),
            "restaurant_email":      _pick(s, "StoreEmail", "StoreEmail2"),
            "general_manager":       _gm_name(gm) if gm else None,
            "gm_email":              _pick(gm, "EmailAddress", "Email"),
            "gm_cell_phone":         _pick(gm, "CellPhone", "Phone"),
            "primary_contact":       _pick(primary, "PrimaryName"),
            "primary_contact_email": _pick(primary, "PrimaryEmail", "EmailAddress", "Email"),
            "primary_contact_phone": _pick(primary, "PrimaryPhone", "Phone"),
            "area_director":         _pick(areadir, "AreaDirName"),
            "area_director_email":   _pick(areadir, "AreaDirEmail", "AreaDirSMGEmail"),
            "billing_name":          _pick(billing, "BillingName"),
            "billing_email":         _pick(billing, "BillingEmail", "EmailAddress", "Email"),
            "billing_phone":         _pick(billing, "BillingPhone", "Phone"),
            "sysco_market":          _pick(sysco, "SyscoMarket"),
            "dma":                   _pick(dma, "DMAName"),
            "firewall_provider":     firewall_labels.get(fw_id),
        })

    # Sort by numeric Restaurant ID where possible (falls back to string).
    def _sort_key(r):
        v = r.get("restaurant_id")
        try:
            return (0, int(v))
        except (ValueError, TypeError):
            return (1, str(v))
    rows.sort(key=_sort_key)

    return {
        "columns": [{"key": k, "label": lbl} for k, lbl in CONTACT_REPORT_COLUMNS],
        "rows":    rows,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Technical & Physical Info report
#
# Sister report to the Contact Information Sheet — the store's setup/build data
# from the StoreTechnical + StorePhysical satellites (plus a few Store columns),
# with FK columns (POS type, phone/camera vendor) resolved to names.
# ──────────────────────────────────────────────────────────────────────────────

TECH_PHYS_REPORT_COLUMNS = [
    ("restaurant_id",     "Restaurant ID"),
    ("restaurant_name",   "Restaurant Name"),
    # StoreTechnical
    ("pos_type",          "POS Type"),
    ("toast_pos_version", "Toast POS Version"),
    ("toast_pay_version", "Toast Pay Version"),
    ("isp_name",          "ISP Name"),
    ("bandwidth",         "Bandwidth"),
    ("external_ip",       "External IP"),
    ("phone_vendor",      "Phone Vendor"),
    ("camera_vendor",     "Camera Vendor"),
    # StorePhysical
    ("square_feet",       "Square Feet"),
    ("indoor_seats",      "Indoor Seats"),
    ("exterior_seats",    "Exterior Seats"),
    ("drive_thru",        "Drive-Thru"),
    ("pos_terminals",     "POS Terminals"),
    # Store
    ("toast_go_live",     "Toast Go-Live"),
    ("open_date",         "Open Date"),
    ("sells_alcohol",     "Sells Alcohol"),
    ("catering_email",    "Catering Email"),
    ("catering_phone",    "Catering Phone"),
    ("region_id",         "Region ID"),
    ("district_id",       "District ID"),
]


def get_technical_physical_report() -> Dict[str, Any]:
    """Build the Technical & Physical Info dataset (per store).

    Returns {"columns": [{key,label}...], "rows": [...]}, sorted by Restaurant
    ID. One pass over Store + bulk satellite/lookup loads (no N+1)."""
    stores  = list_stores()
    tech_by = _all_store_technical_by_id()
    phys_by = _all_store_physical_by_id()

    postype_labels = {r["id"]: r["label"] for r in list_lookup("postype")}
    phone_labels   = {r["id"]: r["label"] for r in list_lookup("phonevendor")}
    camera_labels  = {r["id"]: r["label"] for r in list_lookup("cameravendor")}

    def _phone_vendor(t: Dict[str, Any]) -> Optional[str]:
        # Named vendor via FK, else the free-text "other" field if present.
        return phone_labels.get(t.get("PhoneVendorID")) or _pick(t, "OtherPhoneVendor")

    rows: List[Dict[str, Any]] = []
    for s in stores:
        sid = s.get("StoreID")
        t   = tech_by.get(int(sid)) if sid is not None else None
        p   = phys_by.get(int(sid)) if sid is not None else None
        t   = t or {}
        p   = p or {}
        rows.append({
            "restaurant_id":     sid,
            "restaurant_name":   s.get("StoreName"),
            "pos_type":          postype_labels.get(t.get("fkPOSTypeID")),
            "toast_pos_version": _pick(t, "ToastPOSVersion"),
            "toast_pay_version": _pick(t, "ToastPayVersion"),
            "isp_name":          _pick(t, "ISPName"),
            "bandwidth":         _pick(t, "Bandwidth"),
            "external_ip":       _pick(t, "ExternalIP"),
            "phone_vendor":      _phone_vendor(t),
            "camera_vendor":     camera_labels.get(t.get("CameraVendorID")),
            "square_feet":       _pick(p, "SquareFeet"),
            "indoor_seats":      _pick(p, "IndoorSeats"),
            "exterior_seats":    _pick(p, "ExteriorSeats"),
            "drive_thru":        _pick(p, "DriveThru"),
            "pos_terminals":     _pick(p, "NumPOSTerms"),
            "toast_go_live":     _report_date(s.get("ToastLive")),
            "open_date":         _report_date(s.get("StoreOpenDate")),
            "sells_alcohol":     _yes_no(s.get("StoreAlcFlag")),
            "catering_email":    _pick(s, "StoreCateringEmail"),
            "catering_phone":    _pick(s, "StoreCateringPhone"),
            "region_id":         _pick(s, "StoreRegionID"),
            "district_id":       _pick(s, "StoreDistrictID"),
        })

    def _sort_key(r):
        v = r.get("restaurant_id")
        try:
            return (0, int(v))
        except (ValueError, TypeError):
            return (1, str(v))
    rows.sort(key=_sort_key)

    return {
        "columns": [{"key": k, "label": lbl} for k, lbl in TECH_PHYS_REPORT_COLUMNS],
        "rows":    rows,
    }


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
