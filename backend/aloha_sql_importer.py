import os
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import pyodbc

try:
    from aloha_db import upsert_date as _db_upsert, date_exists as _db_date_exists
    _SQLITE_AVAILABLE = True
except ImportError:
    _SQLITE_AVAILABLE = False

YOY_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "aloha")
YOY_INDEX_FILE = os.path.join(YOY_DATA_DIR, "index.json")


def ensure_storage() -> None:
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    if not os.path.exists(YOY_INDEX_FILE):
        with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
            f.write('{"files": {}}')


def load_index() -> Dict:
    ensure_storage()
    with open(YOY_INDEX_FILE, "r", encoding="utf-8") as f:
        import json
        return json.load(f)


def save_index(index: Dict) -> None:
    ensure_storage()
    with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
        import json
        json.dump(index, f, indent=2)


def _get_env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_connection_string() -> str:
    server = os.getenv("ALOHA_SQL_SERVER")
    database = os.getenv("ALOHA_SQL_DATABASE")
    username = os.getenv("ALOHA_SQL_USERNAME")
    password = os.getenv("ALOHA_SQL_PASSWORD")
    driver = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted = _get_env_bool("ALOHA_SQL_TRUSTED_CONNECTION", False)
    encrypt = _get_env_bool("ALOHA_SQL_ENCRYPT", True)
    trust_server_cert = _get_env_bool("ALOHA_SQL_TRUST_SERVER_CERT", False)

    if not server or not database:
        raise ValueError("Missing ALOHA_SQL_SERVER or ALOHA_SQL_DATABASE in environment")

    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    if trust_server_cert:
        parts.append("TrustServerCertificate=yes")
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        if not username or not password:
            raise ValueError("Missing ALOHA_SQL_USERNAME or ALOHA_SQL_PASSWORD in environment")
        parts.append(f"UID={username}")
        parts.append(f"PWD={password}")
    return ";".join(parts)


def _get_query_template() -> str:
    query = os.getenv("ALOHA_SQL_QUERY")
    if not query:
        raise ValueError("Missing ALOHA_SQL_QUERY in environment")
    return query


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    # Case-insensitive matching for expected columns
    col_map = {}
    for col in df.columns:
        col_lower = str(col).strip().lower()
        if col_lower in {"item", "alohaitem", "itemname", "menuitem"}:
            col_map[col] = "Item"
        elif col_lower in {"quantity", "qty", "count", "units"}:
            col_map[col] = "Quantity"
        elif col_lower in {"sales", "net", "amount", "net_sales", "netsales"}:
            col_map[col] = "Sales"
        elif col_lower in {"businessdate", "date", "bizdate"}:
            col_map[col] = "BusinessDate"
        elif col_lower in {"location", "store", "restaurant", "locationname", "storeid", "site", "unit"}:
            col_map[col] = "Location"
        elif col_lower in {"ordermode", "order_mode", "ordermode_printname", "ordertypename",
                           "ordermodename", "order_mode_name", "ordertype", "order_type"}:
            # Prefer name over ID: drop any prior OrderMode mapping so this name wins
            for k in list(col_map):
                if col_map[k] == "OrderMode":
                    del col_map[k]
            col_map[col] = "OrderMode"
        elif col_lower in {"fkordermodeid", "fk_order_mode_id", "ordermodeid", "order_mode_id"}:
            if "OrderMode" not in col_map.values():
                col_map[col] = "OrderMode"

    df = df.rename(columns=col_map)
    # Coerce OrderMode to string so numeric IDs (e.g. FKOrderModeId) work as display values
    if "OrderMode" in df.columns:
        df["OrderMode"] = df["OrderMode"].astype(str).str.strip()
    required = {"Item", "Quantity", "Sales", "BusinessDate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"SQL query must return columns: {sorted(required)}. Missing: {sorted(missing)}")
    return df


def fetch_aloha_sql_data(start_date: date, end_date: date) -> pd.DataFrame:
    query = _get_query_template()
    formatted_query = query.format(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat()
    )
    conn_str = _build_connection_string()

    with pyodbc.connect(conn_str) as conn:
        df = pd.read_sql_query(formatted_query, conn)

    df = _normalize_columns(df)
    df["BusinessDate"] = pd.to_datetime(df["BusinessDate"], errors="coerce").dt.date
    df = df[df["BusinessDate"].notna()]
    return df


def _write_aloha_csv_for_date(df: pd.DataFrame, business_date: date, overwrite: bool) -> Optional[str]:
    ensure_storage()
    date_iso = business_date.isoformat()
    target_name = f"aloha_{date_iso}.csv"
    target_path = os.path.join(YOY_DATA_DIR, target_name)

    # Skip if both CSV and SQLite already have data for this date (and overwrite=False)
    csv_exists = os.path.exists(target_path)
    db_exists  = _SQLITE_AVAILABLE and _db_date_exists(date_iso)
    if (csv_exists or db_exists) and not overwrite:
        return None

    export_df = df.copy()
    # Build column list in a consistent order: Item, Quantity, Sales, [Location], [OrderMode]
    cols = ["Item", "Quantity", "Sales"]
    if "Location" in export_df.columns:
        cols.append("Location")
    if "OrderMode" in export_df.columns:
        cols.append("OrderMode")
    export_df = export_df[cols]
    export_df.to_csv(target_path, index=False)

    # ── Also write to SQLite (dual-write: CSV for backup, SQLite for fast queries) ──
    if _SQLITE_AVAILABLE:
        try:
            _db_upsert(df, date_iso)
        except Exception as exc:
            # SQLite write failure is non-fatal — CSV is the source of truth fallback
            import warnings
            warnings.warn(f"SQLite write failed for {date_iso}: {exc}")

    index = load_index()
    index.setdefault("files", {})[date_iso] = {
        "path": target_path,
        "original_name": target_name,
        "uploaded_at": datetime.now().isoformat()
    }
    save_index(index)
    return target_path


def import_aloha_sql_range(
    start_date: date,
    end_date: date,
    batch_days: int = 31,
    overwrite: bool = False
) -> Dict[str, List[str]]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if batch_days <= 0:
        raise ValueError("batch_days must be positive")

    written: List[str] = []
    skipped: List[str] = []

    current = start_date
    while current <= end_date:
        batch_end = min(end_date, current + timedelta(days=batch_days - 1))
        df = fetch_aloha_sql_data(current, batch_end)
        if df.empty:
            current = batch_end + timedelta(days=1)
            continue

        for day, day_df in df.groupby("BusinessDate"):
            # Build group-by key: Location (if present) + Item + OrderMode (if present)
            group_cols = []
            if "Location" in day_df.columns:
                group_cols.append("Location")
            group_cols.append("Item")
            if "OrderMode" in day_df.columns:
                group_cols.append("OrderMode")
            day_df = (
                day_df.groupby(group_cols, dropna=False)[["Quantity", "Sales"]]
                .sum()
                .reset_index()
            )
            path = _write_aloha_csv_for_date(day_df, day, overwrite=overwrite)
            if path:
                written.append(day.isoformat())
            else:
                skipped.append(day.isoformat())

        current = batch_end + timedelta(days=1)

    return {"written_dates": sorted(set(written)), "skipped_dates": sorted(set(skipped))}


def import_aloha_sql_last_days(days: int, overwrite: bool = False, batch_days: int = 31) -> Dict[str, List[str]]:
    if days <= 0:
        raise ValueError("days must be positive")
    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)
    return import_aloha_sql_range(start_date, end_date, batch_days=batch_days, overwrite=overwrite)
