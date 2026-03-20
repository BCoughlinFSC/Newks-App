"""
aloha_sales_importer.py  —  Import Aloha historical aggregate sales data from
                            SQL Server into local CSV + SQLite storage.

Mirrors aloha_sql_importer.py but for the tbl_SalesDataByDayPart table
(aggregate net sales per location per day, not per-item).

Reuses the same SQL Server connection config (ALOHA_SQL_SERVER, etc.) but
uses its own query env var: ALOHA_SALES_SQL_QUERY.
"""

import json
import os
import warnings
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pyodbc

from aloha_sql_importer import _build_connection_string

try:
    from aloha_sales_db import upsert_date as _db_upsert, date_exists as _db_date_exists
    _SQLITE_AVAILABLE = True
except ImportError:
    _SQLITE_AVAILABLE = False

SALES_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "aloha_sales")
SALES_INDEX_FILE = os.path.join(SALES_DATA_DIR, "index.json")

# Columns we expect from the SQL query (after GROUP BY across dayparts)
_EXPECTED_COLS = [
    "BusinessDate", "RestaurantID", "RestaurantName",
    "NetSales", "OLONetSales", "DeliveryNetSales", "CateringNetSales",
    "GNGNetSales", "ToGoNetSales", "CurbSideNetSales", "CurbSideOLONetSales",
    "DineInNetSales", "CallInNetSales", "OLOCallInNetSales",
    "CheckCount", "ChecksMinusCatering",
]

_SALES_VALUE_COLS = [
    "NetSales", "OLONetSales", "DeliveryNetSales", "CateringNetSales",
    "GNGNetSales", "ToGoNetSales", "CurbSideNetSales", "CurbSideOLONetSales",
    "DineInNetSales", "CallInNetSales", "OLOCallInNetSales",
    "CheckCount", "ChecksMinusCatering",
]

_CSV_COLS = [
    "RestaurantID", "RestaurantName",
    "NetSales", "OLONetSales", "DeliveryNetSales", "CateringNetSales",
    "GNGNetSales", "ToGoNetSales", "CurbSideNetSales", "CurbSideOLONetSales",
    "DineInNetSales", "CallInNetSales", "OLOCallInNetSales",
    "CheckCount", "ChecksMinusCatering",
]


def ensure_storage() -> None:
    os.makedirs(SALES_DATA_DIR, exist_ok=True)
    if not os.path.exists(SALES_INDEX_FILE):
        with open(SALES_INDEX_FILE, "w", encoding="utf-8") as f:
            f.write('{"files": {}}')


def load_index() -> Dict:
    ensure_storage()
    with open(SALES_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_index(index: Dict) -> None:
    ensure_storage()
    with open(SALES_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)


def _get_sales_query_template() -> str:
    query = os.getenv("ALOHA_SALES_SQL_QUERY")
    if not query:
        raise ValueError("Missing ALOHA_SALES_SQL_QUERY in environment")
    return query


def fetch_aloha_sales_data(start_date: date, end_date: date) -> pd.DataFrame:
    """Fetch aggregate sales data from SQL Server for the given date range."""
    query = _get_sales_query_template()
    formatted_query = query.format(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    conn_str = _build_connection_string()

    with pyodbc.connect(conn_str) as conn:
        df = pd.read_sql_query(formatted_query, conn)

    if df.empty:
        return df

    # Ensure BusinessDate is a date object
    if "BusinessDate" in df.columns:
        df["BusinessDate"] = pd.to_datetime(df["BusinessDate"], errors="coerce").dt.date
        df = df[df["BusinessDate"].notna()]

    # Ensure RestaurantID is a string
    if "RestaurantID" in df.columns:
        df["RestaurantID"] = df["RestaurantID"].astype(str).str.strip()

    # Fill NaN sales values with 0
    for col in _SALES_VALUE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    return df


def _write_sales_csv_for_date(
    df: pd.DataFrame, business_date: date, overwrite: bool
) -> Optional[str]:
    """Write one CSV + SQLite row-set for a single business date."""
    ensure_storage()
    date_iso = business_date.isoformat()
    target_name = f"aloha_sales_{date_iso}.csv"
    target_path = os.path.join(SALES_DATA_DIR, target_name)

    csv_exists = os.path.exists(target_path)
    db_exists = _SQLITE_AVAILABLE and _db_date_exists(date_iso)
    if (csv_exists or db_exists) and not overwrite:
        return None

    # Write CSV
    export_cols = [c for c in _CSV_COLS if c in df.columns]
    df[export_cols].to_csv(target_path, index=False)

    # Dual-write to SQLite (non-fatal)
    if _SQLITE_AVAILABLE:
        try:
            _db_upsert(df, date_iso)
        except Exception as exc:
            warnings.warn(f"SQLite sales write failed for {date_iso}: {exc}")

    # Update index
    index = load_index()
    index.setdefault("files", {})[date_iso] = {
        "path": target_path,
        "original_name": target_name,
        "uploaded_at": datetime.now().isoformat(),
    }
    save_index(index)
    return target_path


def import_aloha_sales_range(
    start_date: date,
    end_date: date,
    batch_days: int = 31,
    overwrite: bool = False,
) -> Dict[str, List[str]]:
    """
    Import Aloha sales data from SQL Server for the given date range.

    The SQL query already does GROUP BY DateOfBusiness, RestaurantID, RestaurantName
    and SUMs across dayparts, so we just need to split by date and write.

    Returns {"written_dates": [...], "skipped_dates": [...]}.
    """
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if batch_days <= 0:
        raise ValueError("batch_days must be positive")

    written: List[str] = []
    skipped: List[str] = []

    current = start_date
    while current <= end_date:
        batch_end = min(end_date, current + timedelta(days=batch_days - 1))
        df = fetch_aloha_sales_data(current, batch_end)
        if df.empty:
            current = batch_end + timedelta(days=1)
            continue

        for day, day_df in df.groupby("BusinessDate"):
            path = _write_sales_csv_for_date(day_df, day, overwrite=overwrite)
            if path:
                written.append(day.isoformat())
            else:
                skipped.append(day.isoformat())

        current = batch_end + timedelta(days=1)

    return {"written_dates": sorted(set(written)), "skipped_dates": sorted(set(skipped))}


def import_aloha_sales_last_days(
    days: int, overwrite: bool = False, batch_days: int = 31
) -> Dict[str, List[str]]:
    """Convenience wrapper: import the last N days of sales data."""
    if days <= 0:
        raise ValueError("days must be positive")
    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)
    return import_aloha_sales_range(start_date, end_date, batch_days=batch_days, overwrite=overwrite)


# ──────────────────────────────────────────────────────────────────────────────
# Fiscal Calendar import from CalendarV2
# ──────────────────────────────────────────────────────────────────────────────

def fetch_fiscal_calendar(start_date: date, end_date: date) -> List[Dict]:
    """
    Fetch fiscal calendar mappings from dev_aloha.dbo.CalendarV2.

    Returns list of dicts with keys: date, ly_date, fiscal_year, fiscal_period, fiscal_week.
    """
    conn_str = _build_connection_string()
    query = (
        "SELECT CONVERT(varchar(10), [Date], 120) AS [date],"
        "       CONVERT(varchar(10), LYDate, 120) AS ly_date,"
        "       FiscalYear AS fiscal_year,"
        "       FiscalPeriod AS fiscal_period,"
        "       FiscalWeek AS fiscal_week"
        " FROM dev_aloha.dbo.CalendarV2"
        f" WHERE [Date] >= '{start_date.isoformat()}'"
        f"   AND [Date] <= '{end_date.isoformat()}'"
        " ORDER BY [Date]"
    )
    with pyodbc.connect(conn_str) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def import_fiscal_calendar(start_date: date, end_date: date) -> int:
    """
    Import fiscal calendar from CalendarV2 into local SQLite.

    Returns number of rows imported.
    """
    from aloha_sales_db import upsert_fiscal_calendar

    rows = fetch_fiscal_calendar(start_date, end_date)
    if not rows:
        return 0
    return upsert_fiscal_calendar(rows)
