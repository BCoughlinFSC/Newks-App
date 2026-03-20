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
    curbside_olo_net_sales REAL NOT NULL DEFAULT 0,
    dinein_net_sales       REAL NOT NULL DEFAULT 0,
    callin_net_sales       REAL NOT NULL DEFAULT 0,
    olo_callin_net_sales   REAL NOT NULL DEFAULT 0,
    check_count            REAL NOT NULL DEFAULT 0,
    checks_minus_catering  REAL NOT NULL DEFAULT 0
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
    "dinein_net_sales", "callin_net_sales", "olo_callin_net_sales",
    "check_count", "checks_minus_catering",
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
    curbside_olo_net_sales REAL    NOT NULL DEFAULT 0,
    dinein_net_sales       REAL    NOT NULL DEFAULT 0,
    callin_net_sales       REAL    NOT NULL DEFAULT 0,
    olo_callin_net_sales   REAL    NOT NULL DEFAULT 0,
    check_count            REAL    NOT NULL DEFAULT 0,
    checks_minus_catering  REAL    NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aloha_sales_unique
    ON aloha_sales_data(business_date, location);

CREATE INDEX IF NOT EXISTS idx_aloha_sales_date
    ON aloha_sales_data(business_date);

CREATE INDEX IF NOT EXISTS idx_aloha_sales_loc_date
    ON aloha_sales_data(location, business_date);

CREATE TABLE IF NOT EXISTS fiscal_calendar (
    date         TEXT PRIMARY KEY,   -- YYYY-MM-DD (current-year date)
    ly_date      TEXT NOT NULL,      -- YYYY-MM-DD (prior-year date per fiscal calendar)
    fiscal_year  INTEGER,
    fiscal_period INTEGER,
    fiscal_week  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_fiscal_cal_ly
    ON fiscal_calendar(ly_date);

CREATE TABLE IF NOT EXISTS store_info (
    location         TEXT PRIMARY KEY,   -- Aloha RestaurantID as string
    restaurant_name  TEXT NOT NULL DEFAULT '',
    company          TEXT NOT NULL DEFAULT '',   -- e.g. "Newco Dining LLC"
    comping          TEXT NOT NULL DEFAULT 'N',  -- 'Y' or 'N'
    comping_date     TEXT,                       -- YYYY-MM-DD or NULL
    open_date        TEXT                        -- YYYY-MM-DD or NULL
);
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


_MIGRATION_COLS = [
    ("dinein_net_sales", "REAL NOT NULL DEFAULT 0"),
    ("callin_net_sales", "REAL NOT NULL DEFAULT 0"),
    ("olo_callin_net_sales", "REAL NOT NULL DEFAULT 0"),
    ("check_count", "REAL NOT NULL DEFAULT 0"),
    ("checks_minus_catering", "REAL NOT NULL DEFAULT 0"),
]


def initialize_db(db_path: str = ALOHA_SALES_DB_PATH) -> None:
    """Create the database file and schema if they don't already exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_CREATE_SQL)
        # Migrate: add columns that may not exist yet
        existing = {row[1] for row in conn.execute("PRAGMA table_info(aloha_sales_data)").fetchall()}
        for col_name, col_type in _MIGRATION_COLS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE aloha_sales_data ADD COLUMN {col_name} {col_type}")
        conn.commit()


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
            float(row.get("DineInNetSales", 0) or 0),
            float(row.get("CallInNetSales", 0) or 0),
            float(row.get("OLOCallInNetSales", 0) or 0),
            float(row.get("CheckCount", 0) or 0),
            float(row.get("ChecksMinusCatering", 0) or 0),
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
                " gng_net_sales, togo_net_sales, curbside_net_sales, curbside_olo_net_sales,"
                " dinein_net_sales, callin_net_sales, olo_callin_net_sales,"
                " check_count, checks_minus_catering)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    sum_cols = ", ".join(f"SUM({c}) AS {c}" for c in _SALES_COLS)
    sql = (
        f"SELECT location, restaurant_name, {sum_cols} "
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
        f"SELECT business_date, location, restaurant_name, {', '.join(_SALES_COLS)} "
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


# ──────────────────────────────────────────────────────────────────────────────
# Fiscal Calendar
# ──────────────────────────────────────────────────────────────────────────────

def upsert_fiscal_calendar(
    rows: list,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> int:
    """
    Insert/replace fiscal calendar rows.

    Each row is a dict with keys: date, ly_date, fiscal_year, fiscal_period, fiscal_week.
    Returns the number of rows inserted.
    """
    initialize_db(db_path)
    records = [
        (r["date"], r["ly_date"], r.get("fiscal_year"), r.get("fiscal_period"), r.get("fiscal_week"))
        for r in rows
    ]
    with get_connection(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fiscal_calendar"
            "(date, ly_date, fiscal_year, fiscal_period, fiscal_week)"
            " VALUES (?, ?, ?, ?, ?)",
            records,
        )
    return len(records)


def get_fiscal_ly_dates(
    start_date: str,
    end_date: str,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> dict:
    """
    Look up fiscal calendar prior-year dates for a date range.

    Returns dict mapping current-year date (YYYY-MM-DD) -> prior-year date (YYYY-MM-DD).
    Only returns dates that exist in the fiscal_calendar table.
    """
    initialize_db(db_path)
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT date, ly_date FROM fiscal_calendar"
                " WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def fiscal_calendar_available(db_path: str = ALOHA_SALES_DB_PATH) -> bool:
    """Return True if the fiscal_calendar table has at least one row."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM fiscal_calendar LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


def get_fiscal_calendar_stats(db_path: str = ALOHA_SALES_DB_PATH) -> dict:
    """Return min_date, max_date, row_count for fiscal_calendar table."""
    empty = {"min_date": None, "max_date": None, "row_count": 0}
    if not os.path.exists(db_path):
        return empty
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM fiscal_calendar"
            ).fetchone()
        if not row or row[2] == 0:
            return empty
        return {"min_date": row[0], "max_date": row[1], "row_count": row[2]}
    except Exception:
        return empty


# ──────────────────────────────────────────────────────────────────────────────
# Store Info
# ──────────────────────────────────────────────────────────────────────────────

def upsert_store_info(
    rows: list,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> int:
    """
    Insert/replace store info rows.

    Each row is a dict with keys: location, restaurant_name, company, comping, comping_date, open_date.
    Returns the number of rows inserted.
    """
    initialize_db(db_path)
    records = [
        (
            str(r.get("location", r.get("RestaurantID", ""))).strip(),
            str(r.get("restaurant_name", r.get("RestaurantName", ""))).strip(),
            str(r.get("company", r.get("Company", ""))).strip(),
            str(r.get("comping", r.get("Comping", "N"))).strip().upper()[:1],
            r.get("comping_date", r.get("CompingDate")) or None,
            r.get("open_date", r.get("OpenDate")) or None,
        )
        for r in rows
    ]
    with get_connection(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO store_info"
            "(location, restaurant_name, company, comping, comping_date, open_date)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            records,
        )
    return len(records)


def get_store_info(
    locations: Optional[List[str]] = None,
    db_path: str = ALOHA_SALES_DB_PATH,
) -> dict:
    """
    Return store info as dict keyed by location.

    Each value is a dict: {restaurant_name, company, comping, comping_date, open_date}
    """
    initialize_db(db_path)
    try:
        with get_connection(db_path) as conn:
            if locations:
                ph = ",".join("?" * len(locations))
                rows = conn.execute(
                    f"SELECT location, restaurant_name, company, comping, comping_date, open_date "
                    f"FROM store_info WHERE location IN ({ph})",
                    locations,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT location, restaurant_name, company, comping, comping_date, open_date "
                    "FROM store_info ORDER BY CAST(location AS INTEGER)"
                ).fetchall()
        return {
            r[0]: {
                "restaurant_name": r[1],
                "company": r[2],
                "comping": r[3],
                "comping_date": r[4],
                "open_date": r[5],
            }
            for r in rows
        }
    except Exception:
        return {}


def store_info_available(db_path: str = ALOHA_SALES_DB_PATH) -> bool:
    """Return True if the store_info table has at least one row."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM store_info LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False
