"""
aloha_db.py  —  SQLite persistence layer for Aloha historical sales data.

Schema
------
  aloha_data(
    id             INTEGER PK AUTOINCREMENT,
    business_date  TEXT NOT NULL,          -- YYYY-MM-DD
    location       TEXT NOT NULL DEFAULT '',-- Aloha store ID e.g. "1042"
    item           TEXT NOT NULL DEFAULT '',-- Item long name
    quantity       REAL NOT NULL DEFAULT 0,
    sales          REAL NOT NULL DEFAULT 0,
    order_mode     TEXT NOT NULL DEFAULT 'Unknown'
  )
  UNIQUE INDEX on (business_date, location, item, order_mode)
  INDEX         on (business_date)
  INDEX         on (business_date, location)

Usage
-----
  from aloha_db import initialize_db, upsert_date, query_date_range, get_stats
"""

import os
import sqlite3
from typing import List, Optional

import pandas as pd

# Default path — same directory as the CSV files
ALOHA_DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "aloha", "aloha.db"
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS aloha_data (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_date TEXT    NOT NULL,
    location      TEXT    NOT NULL DEFAULT '',
    item          TEXT    NOT NULL DEFAULT '',
    quantity      REAL    NOT NULL DEFAULT 0,
    sales         REAL    NOT NULL DEFAULT 0,
    order_mode    TEXT    NOT NULL DEFAULT 'Unknown'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aloha_unique
    ON aloha_data(business_date, location, item, order_mode);

CREATE INDEX IF NOT EXISTS idx_aloha_date
    ON aloha_data(business_date);

CREATE INDEX IF NOT EXISTS idx_aloha_date_loc
    ON aloha_data(business_date, location);

CREATE INDEX IF NOT EXISTS idx_aloha_loc_date
    ON aloha_data(location, business_date);

CREATE INDEX IF NOT EXISTS idx_aloha_order_mode
    ON aloha_data(order_mode);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Connection + Schema
# ──────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str = ALOHA_DB_PATH) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection (thread-safe for read-heavy workloads)."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
    return conn


def initialize_db(db_path: str = ALOHA_DB_PATH) -> None:
    """Create the database file and schema if they don't already exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_CREATE_SQL)


def db_available(db_path: str = ALOHA_DB_PATH) -> bool:
    """Return True if the database file exists and has at least one row."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM aloha_data LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────────

def upsert_date(
    df: pd.DataFrame,
    business_date: str,
    db_path: str = ALOHA_DB_PATH,
) -> int:
    """
    Replace ALL rows for *business_date* with the rows in *df*.

    The DataFrame must contain an 'Item' column.  'Quantity', 'Sales',
    'Location', and 'OrderMode' are optional and default to safe values.

    Returns the number of rows inserted.
    """
    initialize_db(db_path)

    records: List[tuple] = []
    for _, row in df.iterrows():
        item = str(row.get("Item", "") or "").strip()
        if not item:
            continue
        records.append((
            business_date,
            str(row.get("Location", "") or "").strip(),
            item,
            float(row.get("Quantity", 0) or 0),
            float(row.get("Sales", 0) or 0),
            str(row.get("OrderMode", "Unknown") or "Unknown").strip() or "Unknown",
        ))

    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM aloha_data WHERE business_date = ?",
            (business_date,),
        )
        if records:
            conn.executemany(
                "INSERT INTO aloha_data"
                "(business_date, location, item, quantity, sales, order_mode)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                records,
            )
    return len(records)


def delete_date(business_date: str, db_path: str = ALOHA_DB_PATH) -> int:
    """Delete all rows for the given business_date. Returns rows deleted."""
    if not db_available(db_path):
        return 0
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM aloha_data WHERE business_date = ?", (business_date,)
        )
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────────

def date_exists(business_date: str, db_path: str = ALOHA_DB_PATH) -> bool:
    """Return True if any rows exist for the given business_date."""
    if not db_available(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM aloha_data WHERE business_date = ? LIMIT 1",
                (business_date,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def get_available_dates(db_path: str = ALOHA_DB_PATH) -> List[str]:
    """Return a sorted list of YYYY-MM-DD strings with data in the DB."""
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT business_date FROM aloha_data ORDER BY business_date"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def query_date_range(
    start_date: str,
    end_date: str,
    locations: Optional[List[str]] = None,
    order_modes: Optional[List[str]] = None,
    db_path: str = ALOHA_DB_PATH,
) -> pd.DataFrame:
    """
    Query Aloha data for a date range.

    Parameters
    ----------
    start_date, end_date : str
        YYYY-MM-DD bounds (inclusive).
    locations : list[str] | None
        Aloha store IDs to include.  None = all locations.
    order_modes : list[str] | None
        OrderMode values to include.  None = all modes.

    Returns
    -------
    pd.DataFrame with columns:
        AlohaItem, Quantity, Sales, Location, OrderMode
    (quantities and sales are summed across all matching rows)
    """
    if not db_available(db_path):
        return pd.DataFrame(columns=["AlohaItem", "Quantity", "Sales", "Location", "OrderMode"])

    params: List = [start_date, end_date]
    where_clauses = ["business_date BETWEEN ? AND ?"]

    if locations:
        ph = ",".join("?" * len(locations))
        where_clauses.append(f"location IN ({ph})")
        params.extend(locations)

    if order_modes:
        ph = ",".join("?" * len(order_modes))
        where_clauses.append(f"order_mode IN ({ph})")
        params.extend(order_modes)

    sql = (
        "SELECT"
        "  item          AS AlohaItem,"
        "  SUM(quantity) AS Quantity,"
        "  SUM(sales)    AS Sales,"
        "  location      AS Location,"
        "  order_mode    AS OrderMode "
        "FROM aloha_data "
        "WHERE " + " AND ".join(where_clauses) + " "
        "GROUP BY item, location, order_mode"
    )

    try:
        with get_connection(db_path) as conn:
            df = pd.read_sql_query(sql, conn, params=params)
        return df
    except Exception as exc:
        raise RuntimeError(f"Aloha DB query failed: {exc}") from exc


def get_distinct_order_modes(db_path: str = ALOHA_DB_PATH) -> List[str]:
    """Return all distinct OrderMode values stored in the DB."""
    if not db_available(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT order_mode FROM aloha_data ORDER BY order_mode"
            ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def get_stats(db_path: str = ALOHA_DB_PATH) -> dict:
    """
    Return summary statistics about the database contents.

    Keys: total_rows, min_date, max_date, date_count, location_count,
          order_mode_count, db_size_mb
    """
    empty = {
        "total_rows": 0,
        "min_date": None,
        "max_date": None,
        "date_count": 0,
        "location_count": 0,
        "order_mode_count": 0,
        "db_size_mb": 0.0,
    }
    if not os.path.exists(db_path):
        return empty
    try:
        with get_connection(db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM aloha_data").fetchone()[0]
            if total == 0:
                return empty
            dates = conn.execute(
                "SELECT MIN(business_date), MAX(business_date), COUNT(DISTINCT business_date)"
                " FROM aloha_data"
            ).fetchone()
            locs = conn.execute(
                "SELECT COUNT(DISTINCT location) FROM aloha_data WHERE location != ''"
            ).fetchone()[0]
            modes = conn.execute(
                "SELECT COUNT(DISTINCT order_mode) FROM aloha_data"
            ).fetchone()[0]
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        return {
            "total_rows": total,
            "min_date": dates[0],
            "max_date": dates[1],
            "date_count": dates[2],
            "location_count": locs,
            "order_mode_count": modes,
            "db_size_mb": round(size_mb, 2),
        }
    except Exception:
        return empty
