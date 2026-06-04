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
            for k, v in data.items():
                if k in protected or k not in valid_cols:
                    continue
                sets.append(f"{k} = ?")
                values.append(v if v not in ("", None) else None)
            if stamp_by:
                sets.append(f"{stamp_by} = ?")
                values.append(updated_by)
            if stamp_at:
                sets.append(f"{stamp_at} = datetime('now')")
            values.append(store_id)
            sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {fk_column} = ?"
            cur = conn.execute(sql, tuple(values))
            conn.commit()
            return cur.rowcount > 0

        # INSERT path — supply FK + whatever payload columns are valid.
        cols: List[str] = [fk_column]
        values_ins: List[Any] = [store_id]
        for k, v in data.items():
            if k in protected or k == fk_column or k not in valid_cols:
                continue
            cols.append(k)
            values_ins.append(v if v not in ("", None) else None)
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


def get_store_full(store_id: int) -> Optional[Dict[str, Any]]:
    """Return Store + Physical + Technical + closures + delivery partners
    in one payload. Lets the edit modal hydrate all sections from a single
    round-trip."""
    base = get_store(store_id)
    if not base:
        return None
    physical          = get_store_physical(store_id)        or {}
    technical         = get_store_technical(store_id)       or {}
    closures          = list_store_closures(store_id)
    delivery_partners = list_delivery_partners_by_store(store_id)
    return {
        "store":             base,
        "physical":          physical,
        "technical":         technical,
        "closures":          closures,
        "closure_count":     len(closures),
        "delivery_partners": delivery_partners,
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
    for k, v in data.items():
        if k in protected or k not in valid_cols:
            continue
        sets.append(f"{k} = ?")
        values.append(v if v not in ("", None) else None)
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
    supplied_pk = data.get(pk)
    if supplied_pk not in (None, ""):
        cols.append(pk)
        values.append(supplied_pk)
    for k, v in data.items():
        if k == pk or k in protected_for_insert or k not in valid_cols:
            continue
        cols.append(k)
        values.append(v if v not in ("", None) else None)
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
