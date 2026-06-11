"""
toast_menu_raw_db.py — Local SQLite cache of Toast menu-analytics rows.

Why this exists
---------------
Menu reports re-fetch the same Toast Analytics data on every run, and those
calls are the slowest part of the pipeline (~30-60s per week × multiple
groupBys × 89 stores). By backfilling raw rows once into this DB, any
subsequent menu report within the backfilled date range can read locally
in milliseconds and skip Toast entirely.

Schema
------
  toast_menu_raw(
    business_date    TEXT NOT NULL,           -- 'YYYY-MM-DD'
    restaurant_guid  TEXT NOT NULL,
    menu_item_guid   TEXT NOT NULL,
    group_by_kind    TEXT NOT NULL,           -- 'MENU_ITEM' | 'MODIFIER'
    menu_item_name   TEXT,
    restaurant_name  TEXT,
    quantity_sold    REAL,
    gross_sales      REAL,
    net_sales        REAL,
    discounts        REAL,
    is_modifier      INTEGER,
    raw_json         TEXT                     -- full Toast row, for fields we
                                              -- haven't pulled into columns
  )
  PRIMARY KEY (business_date, restaurant_guid, menu_item_guid, group_by_kind)

  toast_menu_backfill_log(
    business_date    TEXT PRIMARY KEY,        -- one row per fully-backfilled day
    backfilled_at    TEXT NOT NULL,           -- ISO timestamp
    row_count        INTEGER NOT NULL,
    stores_count     INTEGER NOT NULL,
    groupby_kinds    TEXT NOT NULL            -- 'MENU_ITEM,MODIFIER'
  )

Per the design discussion (2026-05-12): backfill is immutable. Once a date
is logged, re-querying it is a no-op unless `force=True` is passed to the
backfill runner.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "toast_menu_raw.db"
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS toast_menu_raw (
    business_date    TEXT NOT NULL,
    restaurant_guid  TEXT NOT NULL,
    menu_item_guid   TEXT NOT NULL,
    group_by_kind    TEXT NOT NULL,
    menu_item_name   TEXT,
    restaurant_name  TEXT,
    quantity_sold    REAL,
    gross_sales      REAL,
    net_sales        REAL,
    discounts        REAL,
    is_modifier      INTEGER,
    raw_json         TEXT,
    PRIMARY KEY (business_date, restaurant_guid, menu_item_guid, group_by_kind)
);

CREATE INDEX IF NOT EXISTS idx_tmr_date
    ON toast_menu_raw(business_date);

CREATE INDEX IF NOT EXISTS idx_tmr_store_date
    ON toast_menu_raw(restaurant_guid, business_date);

CREATE TABLE IF NOT EXISTS toast_menu_backfill_log (
    business_date    TEXT PRIMARY KEY,
    backfilled_at    TEXT NOT NULL,
    row_count        INTEGER NOT NULL,
    stores_count     INTEGER NOT NULL,
    groupby_kinds    TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode lets a long-running write (e.g. mid-backfill insert) coexist
    # with concurrent reads from menu-report requests. Without WAL, SQLite's
    # default rollback journal serializes readers and writers, so a report
    # request can stall waiting on the backfill's transaction to finish.
    # PRAGMA is per-connection but the journal mode change is persistent on
    # the DB file, so setting it on every connection is harmless after the
    # first run.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # safe under WAL, faster
    except sqlite3.Error:
        pass
    return conn


def initialize_db() -> None:
    """Create tables + indexes if they don't exist. Idempotent."""
    with _connect() as conn:
        conn.executescript(_CREATE_SQL)
        conn.commit()


def db_available() -> bool:
    try:
        return os.path.exists(DB_PATH)
    except Exception:
        return False


# ─── Coverage / stats ─────────────────────────────────────────────────────────

def get_stats() -> Dict[str, Any]:
    """Summary for the admin UI."""
    if not db_available():
        return {
            "available": False,
            "row_count": 0,
            "date_range": None,
            "backfilled_days": 0,
            "db_size_mb": 0.0,
        }
    with _connect() as conn:
        row_count = conn.execute("SELECT COUNT(*) FROM toast_menu_raw").fetchone()[0]
        cur = conn.execute("SELECT MIN(business_date), MAX(business_date) FROM toast_menu_raw")
        min_d, max_d = cur.fetchone()
        backfilled_days = conn.execute("SELECT COUNT(*) FROM toast_menu_backfill_log").fetchone()[0]
    try:
        size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    return {
        "available":        True,
        "row_count":        row_count,
        "date_range":       {"min": min_d, "max": max_d} if min_d else None,
        "backfilled_days":  backfilled_days,
        "db_size_mb":       round(size_mb, 2),
    }


def dates_covered(start_date: str, end_date: str) -> List[str]:
    """Return business_dates within [start, end] that have backfill-log entries."""
    if not db_available():
        return []
    with _connect() as conn:
        cur = conn.execute(
            "SELECT business_date FROM toast_menu_backfill_log "
            "WHERE business_date BETWEEN ? AND ? ORDER BY business_date",
            (start_date, end_date),
        )
        return [r[0] for r in cur.fetchall()]


def dates_missing(start_date: str, end_date: str) -> List[str]:
    """Return business_dates in [start, end] *without* backfill-log entries."""
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except ValueError:
        return []
    if ed < sd:
        return []
    expected = set()
    d = sd
    while d <= ed:
        expected.add(d.isoformat())
        d += timedelta(days=1)
    have = set(dates_covered(start_date, end_date))
    return sorted(expected - have)


def has_full_coverage(start_date: str, end_date: str) -> bool:
    """True iff every day in [start, end] has a backfill-log entry."""
    return len(dates_missing(start_date, end_date)) == 0


# ─── Inserts ──────────────────────────────────────────────────────────────────

# Subset of Toast Analytics row fields we extract into typed columns. Anything
# else lives in raw_json untouched.
_ROW_COL_KEYS = (
    "menuItemGuid", "menuItemName", "restaurantGuid", "restaurantName",
    "businessDate", "quantitySold", "grossSales", "netSales", "discounts",
)


def _row_to_tuple(row: Dict[str, Any], group_by_kind: str, menu_item_guid_set: set) -> Optional[Tuple]:
    business_date = row.get("businessDate")
    restaurant_guid = row.get("restaurantGuid") or ""
    menu_item_guid = row.get("menuItemGuid") or row.get("modifierGuid") or ""
    if not business_date or not menu_item_guid or not restaurant_guid:
        return None
    # Toast returns YYYYMMDD; normalize to YYYY-MM-DD for indexing.
    bd = str(business_date)
    if len(bd) == 8 and bd.isdigit():
        bd = f"{bd[0:4]}-{bd[4:6]}-{bd[6:8]}"
    is_modifier = 1 if menu_item_guid not in menu_item_guid_set else 0
    return (
        bd,
        restaurant_guid,
        menu_item_guid,
        group_by_kind,
        row.get("menuItemName") or "",
        row.get("restaurantName") or "",
        float(row.get("quantitySold") or 0),
        float(row.get("grossSales") or 0) if row.get("grossSales") is not None else None,
        float(row.get("netSales") or 0) if row.get("netSales") is not None else None,
        float(row.get("discounts") or 0) if row.get("discounts") is not None else None,
        is_modifier,
        json.dumps(row, separators=(",", ":")),
    )


def insert_rows(rows: Iterable[Dict[str, Any]], group_by_kind: str, menu_item_guids: Optional[set] = None) -> int:
    """
    Bulk-insert Toast Analytics rows. INSERT OR REPLACE on the composite PK
    so re-runs of the same (date, store, item, kind) overwrite cleanly.

    menu_item_guids: set of GUIDs from a MENU_ITEM-groupBy fetch, used to
                     mark MODIFIER-groupBy rows whose guid isn't a known
                     menu-item as is_modifier=1.
    """
    initialize_db()
    menu_item_guids = menu_item_guids or set()
    tuples = []
    for r in rows:
        t = _row_to_tuple(r, group_by_kind, menu_item_guids)
        if t is not None:
            tuples.append(t)
    if not tuples:
        return 0
    with _connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO toast_menu_raw
              (business_date, restaurant_guid, menu_item_guid, group_by_kind,
               menu_item_name, restaurant_name, quantity_sold,
               gross_sales, net_sales, discounts, is_modifier, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuples,
        )
        conn.commit()
    return len(tuples)


def mark_dates_backfilled(
    business_dates: Iterable[str],
    *,
    groupby_kinds: str = "MENU_ITEM,MODIFIER",
    backfilled_at: Optional[str] = None,
) -> int:
    """Upsert backfill-log entries. row_count / stores_count derived from data."""
    initialize_db()
    ts = backfilled_at or datetime.now().isoformat(timespec="seconds")
    written = 0
    with _connect() as conn:
        for bd in business_dates:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM toast_menu_raw WHERE business_date = ?", (bd,)
            ).fetchone()[0]
            stores_count = conn.execute(
                "SELECT COUNT(DISTINCT restaurant_guid) FROM toast_menu_raw WHERE business_date = ?", (bd,)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT OR REPLACE INTO toast_menu_backfill_log
                    (business_date, backfilled_at, row_count, stores_count, groupby_kinds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (bd, ts, row_count, stores_count, groupby_kinds),
            )
            written += 1
        conn.commit()
    return written


def clear_range(start_date: str, end_date: str) -> int:
    """Delete raw rows AND backfill-log entries within [start, end]. Returns rows deleted."""
    if not db_available():
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM toast_menu_raw WHERE business_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        deleted = cur.rowcount or 0
        conn.execute(
            "DELETE FROM toast_menu_backfill_log WHERE business_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        conn.commit()
    return deleted


# ─── Queries the report endpoint uses ─────────────────────────────────────────

def query_rows_for_report(
    start_date: str,
    end_date: str,
    restaurant_guids: Optional[List[str]] = None,
    group_by_kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return rows shaped like Toast Analytics output, so the existing pivot can
    consume them without modification.

    The pivot reads: businessDate, restaurantGuid, restaurantName,
    menuItemGuid, menuItemName, quantitySold, netSales. We hand all of those
    back from typed columns; if a caller needs an obscure raw field,
    raw_json is there.

    netSales matters for dedup correctness: the report's exact-row dedup
    keys on (restaurantGuid, businessDate, menuItemGuid, quantitySold,
    netSales). Without netSales, a drink's standalone sales and combo-
    modifier sales that coincide on quantity at a store/day collapse into
    one row (~210 drinks/day lost vs Toast PMIX, validated 2026-05-01) —
    while true duplicates (pairing-choice echo mirrors) match on dollars
    too and still dedup.
    """
    if not db_available():
        return []
    sql = (
        "SELECT business_date, restaurant_guid, menu_item_guid, group_by_kind, "
        "       menu_item_name, restaurant_name, quantity_sold, net_sales "
        "FROM toast_menu_raw "
        "WHERE business_date BETWEEN ? AND ?"
    )
    params: List[Any] = [start_date, end_date]
    if restaurant_guids:
        placeholders = ",".join("?" for _ in restaurant_guids)
        sql += f" AND restaurant_guid IN ({placeholders})"
        params.extend(restaurant_guids)
    if group_by_kind:
        sql += " AND group_by_kind = ?"
        params.append(group_by_kind)
    rows: List[Dict[str, Any]] = []
    with _connect() as conn:
        for r in conn.execute(sql, params):
            rows.append({
                "businessDate":   r["business_date"],
                "restaurantGuid": r["restaurant_guid"],
                "menuItemGuid":   r["menu_item_guid"],
                "menuItemName":   r["menu_item_name"] or "",
                "restaurantName": r["restaurant_name"] or "",
                "quantitySold":   r["quantity_sold"] or 0,
                "netSales":       r["net_sales"],
                "_groupByKind":   r["group_by_kind"],
            })
    return rows


def menu_item_guids_for_range(start_date: str, end_date: str) -> set:
    """Return the set of MENU_ITEM guids present in [start, end] — used by the
    pivot to distinguish menu items from modifiers."""
    if not db_available():
        return set()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT DISTINCT menu_item_guid FROM toast_menu_raw "
            "WHERE business_date BETWEEN ? AND ? AND group_by_kind = 'MENU_ITEM'",
            (start_date, end_date),
        )
        return {r[0] for r in cur.fetchall() if r[0]}


def coverage_summary(start_date: str, end_date: str) -> Dict[str, Any]:
    """Per-day row counts + total — for the admin UI 'what's backfilled' panel."""
    if not db_available():
        return {"available": False, "days": [], "total_rows": 0}
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT l.business_date, l.backfilled_at, l.row_count, l.stores_count, l.groupby_kinds
            FROM toast_menu_backfill_log l
            WHERE l.business_date BETWEEN ? AND ?
            ORDER BY l.business_date
            """,
            (start_date, end_date),
        )
        days = [dict(r) for r in cur.fetchall()]
        total_rows = sum(int(d.get("row_count") or 0) for d in days)
    return {
        "available":  True,
        "days":       days,
        "total_rows": total_rows,
    }
