"""
aloha_sales_db.py  —  SQLite persistence layer for Aloha historical sales data
                      (aggregate net sales by location, not per-item).

Schema
------
  aloha_sales_data(
    id                     INTEGER PK AUTOINCREMENT,
    business_date          TEXT NOT NULL,           -- YYYY-MM-DD
    location               TEXT NOT NULL,           -- Aloha RestaurantID as string e.g. "1001"
    restaurant_name        TEXT NOT NULL DEFAULT '',
    net_sales              REAL NOT NULL DEFAULT 0,
    olo_net_sales          REAL NOT NULL DEFAULT 0,
    delivery_net_sales     REAL NOT NULL DEFAULT 0,
    catering_net_sales     REAL NOT NULL DEFAULT 0,
    gng_net_sales          REAL NOT NULL DEFAULT 0,
    togo_net_sales         REAL NOT NULL DEFAULT 0,
    curbside_net_sales     REAL NOT NULL DEFAULT 0,
    curbside_olo_net_sales REAL NOT NULL DEFAULT 0
  )
  UNIQUE INDEX on (business_date, location)

Usage
-----
  from aloha_sales_db import initialize_db, upsert_date, query_date_range, get_stats
"""

import os
import sqlite3
from typing import List, Optional

import pandas as pd

# Default path — alongside the menu aloha.db
ALOHA_SALES_DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "aloha", "aloha_sales.db"
)

_SALES_COLS = [
    "net_sales", "olo_net_sales", "delivery_net_sales", "catering_net_sales",
    "gng_net_sales", "togo_net_sales", "curbside_net_sales", "curbside_olo_net_sales",
]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS aloha_sales_data (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    business_date          TEXT    NOT NULL,
    location               TEXT    NOT NULL,
    restaurant_name        TEXT    NOT NULL DEFAULT '',
    net_sales              REAL    NOT NULL DEFAULT 0,
    olo_net_sales          REAL    NOT NULL DEFAULT 0,
    delivery_net_sales     REAL    NOT NULL DEFAULT 0,
    catering_net_sales     REAL    NOT NULL DEFAULT 0,
    gng_net_sales          REAL    NOT NULL DEFAULT 0,
    togo_net_sales         REAL    NOT NULL DEFAULT 0,
    curbside_net_sales     REAL    NOT NULL DEFAULT 0,
    curbside_olo_net_sales REAL    NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aloha_sales_unique
    ON aloha_sales_data(business_date, location);

CREATE INDEX IF NOT EXISTS idx_aloha_sales_date
    ON aloha_sales_data(business_date);

CREATE INDEX IF NOT EXISTS idx_aloha_sales_loc_date
    ON aloha_sales_data(location, business_date);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Connection + Schema
# ──────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str = ALOHA_SALES_DB_PATH) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
    return conn


def initialize_db(db_path: str = ALOHA_SALES_DB_PATH) -> None:
    """Create the database file and schema if they don't already exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_CREATE_SQL)


def db_available(db_path: str = ALOHA_SALES_DB_PATH) -> bool:
    """Return True if the database file exists and has at least one row."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM aloha_sales_data LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────────

def upsert_date(
    df: pd.DataFrame,
    business_date: str,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> int:
    """
    Replace ALL rows for *business_date* with the rows in *df*.

    The DataFrame should have columns matching the SQL source:
    RestaurantID, RestaurantName, NetSales, OLONetSales, DeliveryNetSales,
    CateringNetSales, GNGNetSales, ToGoNetSales, CurbSideNetSales,
    CurbSideOLONetSales.

    Returns the number of rows inserted.
    """
    initialize_db(db_path)

    # Map DataFrame column names (from SQL) to SQLite column names
    col_map = {
        "RestaurantID":          "location",
        "RestaurantName":        "restaurant_name",
        "NetSales":              "net_sales",
        "OLONetSales":           "olo_net_sales",
        "DeliveryNetSales":      "delivery_net_sales",
        "CateringNetSales":      "catering_net_sales",
        "GNGNetSales":           "gng_net_sales",
        "ToGoNetSales":          "togo_net_sales",
        "CurbSideNetSales":      "curbside_net_sales",
        "CurbSideOLONetSales":   "curbside_olo_net_sales",
    }

    records: List[tuple] = []
    for _, row in df.iterrows():
        location = str(row.get("RestaurantID", "") or "").strip()
        if not location:
            continue
        records.append((
            business_date,
            location,
            str(row.get("RestaurantName", "") or "").strip(),
            float(row.get("NetSales", 0) or 0),
            float(row.get("OLONetSales", 0) or 0),
            float(row.get("DeliveryNetSales", 0) or 0),
            float(row.get("CateringNetSales", 0) or 0),
            float(row.get("GNGNetSales", 0) or 0),
            float(row.get("ToGoNetSales", 0) or 0),
            float(row.get("CurbSideNetSales", 0) or 0),
            float(row.get("CurbSideOLONetSales", 0) or 0),
        ))

    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM aloha_sales_data WHERE business_date = ?",
            (business_date,),
        )
        if records:
            conn.executemany(
                "INSERT INTO aloha_sales_data"
                "(business_date, location, restaurant_name,"
                " net_sales, olo_net_sales, delivery_net_sales, catering_net_sales,"
                " gng_net_sales, togo_net_sales, curbside_net_sales, curbside_olo_net_sales)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
    return len(records)


def delete_date(business_date: str, db_path: str = ALOHA_SALES_DB_PATH) -> int:
    """Delete all rows for the given business_date. Returns rows deleted."""
    if not db_available(db_path):
        return 0
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM aloha_sales_data WHERE business_date = ?", (business_date,)
        )
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────────

def date_exists(business_date: str, db_path: str = ALOHA_SALES_DB_PATH) -> bool:
    """Return True if any rows exist for the given business_date."""
    if not db_available(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM aloha_sales_data WHERE business_date = ? LIMIT 1",
                (business_date,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def get_available_dates(db_path: str = ALOHA_SALES_DB_PATH) -> List[str]:
    """Return a sorted list of YYYY-MM-DD strings with data in the DB."""
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT business_date FROM aloha_sales_data ORDER BY business_date"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def query_date_range(
    start_date: str,
    end_date: str,
    locations: Optional[List[str]] = None,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> pd.DataFrame:
    """
    Query Aloha sales data for a date range, aggregated by location.

    Returns DataFrame with columns:
        location, restaurant_name, net_sales, olo_net_sales, delivery_net_sales,
        catering_net_sales, gng_net_sales, togo_net_sales, curbside_net_sales,
        curbside_olo_net_sales
    (sales are summed across all dates in range per location)
    """
    empty_cols = ["location", "restaurant_name"] + _SALES_COLS
    if not db_available(db_path):
        return pd.DataFrame(columns=empty_cols)

    params: List = [start_date, end_date]
    where_clauses = ["business_date BETWEEN ? AND ?"]

    if locations:
        ph = ",".join("?" * len(locations))
        where_clauses.append(f"location IN ({ph})")
        params.extend(locations)

    sql = (
        "SELECT"
        "  location,"
        "  restaurant_name,"
        "  SUM(net_sales) AS net_sales,"
        "  SUM(olo_net_sales) AS olo_net_sales,"
        "  SUM(delivery_net_sales) AS delivery_net_sales,"
        "  SUM(catering_net_sales) AS catering_net_sales,"
        "  SUM(gng_net_sales) AS gng_net_sales,"
        "  SUM(togo_net_sales) AS togo_net_sales,"
        "  SUM(curbside_net_sales) AS curbside_net_sales,"
        "  SUM(curbside_olo_net_sales) AS curbside_olo_net_sales "
        "FROM aloha_sales_data "
        "WHERE " + " AND ".join(where_clauses) + " "
        "GROUP BY location, restaurant_name"
    )

    try:
        with get_connection(db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except Exception as exc:
        raise RuntimeError(f"Aloha Sales DB query failed: {exc}") from exc


def query_date_range_daily(
    start_date: str,
    end_date: str,
    locations: Optional[List[str]] = None,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> pd.DataFrame:
    """
    Query Aloha sales data for a date range with daily granularity.

    Returns DataFrame with columns:
        business_date, location, restaurant_name, net_sales, ...
    (one row per location per date)
    """
    empty_cols = ["business_date", "location", "restaurant_name"] + _SALES_COLS
    if not db_available(db_path):
        return pd.DataFrame(columns=empty_cols)

    params: List = [start_date, end_date]
    where_clauses = ["business_date BETWEEN ? AND ?"]

    if locations:
        ph = ",".join("?" * len(locations))
        where_clauses.append(f"location IN ({ph})")
        params.extend(locations)

    sql = (
        "SELECT"
        "  business_date,"
        "  location,"
        "  restaurant_name,"
        "  net_sales,"
        "  olo_net_sales,"
        "  delivery_net_sales,"
        "  catering_net_sales,"
        "  gng_net_sales,"
        "  togo_net_sales,"
        "  curbside_net_sales,"
        "  curbside_olo_net_sales "
        "FROM aloha_sales_data "
        "WHERE " + " AND ".join(where_clauses) + " "
        "ORDER BY business_date, location"
    )

    try:
        with get_connection(db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except Exception as exc:
        raise RuntimeError(f"Aloha Sales DB daily query failed: {exc}") from exc


def get_stats(db_path: str = ALOHA_SALES_DB_PATH) -> dict:
    """
    Return summary statistics about the database contents.

    Keys: total_rows, min_date, max_date, date_count, location_count, db_size_mb
    """
    empty = {
        "total_rows": 0,
        "min_date": None,
        "max_date": None,
        "date_count": 0,
        "location_count": 0,
        "db_size_mb": 0.0,
    }
    if not os.path.exists(db_path):
        return empty
    try:
        with get_connection(db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM aloha_sales_data").fetchone()[0]
            if total == 0:
                return empty
            dates = conn.execute(
                "SELECT MIN(business_date), MAX(business_date), COUNT(DISTINCT business_date)"
                " FROM aloha_sales_data"
            ).fetchone()
            locs = conn.execute(
                "SELECT COUNT(DISTINCT location) FROM aloha_sales_data WHERE location != ''"
            ).fetchone()[0]
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        return {
            "total_rows": total,
            "min_date": dates[0],
            "max_date": dates[1],
            "date_count": dates[2],
            "location_count": locs,
            "db_size_mb": round(size_mb, 2),
        }
    except Exception:
        return empty
