"""
crunchtime_db.py — SQLite layer for CrunchTime food cost / COGS data.

Replaces the SQL Server crunchtime database.
Tables: actual_costs, actual_costs_by_gl, cogs_by_product, gross_profit_by_item
"""

import os
import sqlite3
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CRUNCHTIME_DB_PATH = os.path.join(_DB_DIR, "crunchtime.db")

# ── Schema ──────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS actual_costs (
    store_id            TEXT,
    fiscal_period       TEXT,
    fiscal_week         TEXT,
    fiscal_week_start   TEXT,
    actual_cost         REAL,
    PRIMARY KEY (store_id, fiscal_week_start)
);

CREATE TABLE IF NOT EXISTS actual_costs_by_gl (
    store_id                    TEXT,
    reporting_period            TEXT,
    fiscal_week_start           TEXT,
    gl_code                     TEXT,
    beginning_inv_value         REAL,
    incoming_transfer_value     REAL,
    purchases_value             REAL,
    vendor_order_purchases_value REAL,
    outgoing_transfer_value     REAL,
    ending_inv_value            REAL,
    pbi_detail_amount           REAL,
    production_value            REAL,
    production_consumpt_value   REAL,
    actual_cost                 REAL,
    PRIMARY KEY (store_id, reporting_period, fiscal_week_start, gl_code)
);

CREATE TABLE IF NOT EXISTS cogs_by_product (
    location_code       TEXT,
    reporting_period    TEXT,
    category            TEXT,
    subcategory         TEXT,
    product_name        TEXT,
    at_variance_value   REAL,
    act_consumpt_value  REAL,
    beginning_inv_value REAL,
    ending_inv_value    REAL,
    inv_adjustment_value REAL,
    purchases_value     REAL,
    theoretical_value   REAL,
    PRIMARY KEY (location_code, reporting_period, category, subcategory, product_name)
);

CREATE TABLE IF NOT EXISTS gross_profit_by_item (
    store_id            TEXT,
    fiscal_week         TEXT,
    fiscal_week_start   TEXT,
    date_of_business    TEXT,
    item_id             TEXT,
    gross_profit        REAL,
    PRIMARY KEY (store_id, fiscal_week_start, date_of_business, item_id)
);

CREATE INDEX IF NOT EXISTS idx_ac_store ON actual_costs(store_id);
CREATE INDEX IF NOT EXISTS idx_ac_period ON actual_costs(fiscal_period);
CREATE INDEX IF NOT EXISTS idx_acgl_store ON actual_costs_by_gl(store_id);
CREATE INDEX IF NOT EXISTS idx_acgl_period ON actual_costs_by_gl(reporting_period);
CREATE INDEX IF NOT EXISTS idx_acgl_gl ON actual_costs_by_gl(gl_code);
CREATE INDEX IF NOT EXISTS idx_cogs_loc ON cogs_by_product(location_code);
CREATE INDEX IF NOT EXISTS idx_cogs_period ON cogs_by_product(reporting_period);
CREATE INDEX IF NOT EXISTS idx_cogs_cat ON cogs_by_product(category);
CREATE INDEX IF NOT EXISTS idx_gp_store ON gross_profit_by_item(store_id);
CREATE INDEX IF NOT EXISTS idx_gp_week ON gross_profit_by_item(fiscal_week_start);
"""


def get_connection(db_path: str = CRUNCHTIME_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_initialized = False


def initialize_db(db_path: str = CRUNCHTIME_DB_PATH):
    global _initialized
    if _initialized:
        return
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA_SQL)
    conn.close()
    _initialized = True


def db_available(db_path: str = CRUNCHTIME_DB_PATH) -> bool:
    try:
        if not os.path.exists(db_path):
            return False
        conn = get_connection(db_path)
        row = conn.execute("SELECT COUNT(*) FROM actual_costs").fetchone()
        conn.close()
        return row[0] > 0
    except Exception:
        return False


def get_stats(db_path: str = CRUNCHTIME_DB_PATH) -> dict:
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        stats = {}
        for table in ['actual_costs', 'actual_costs_by_gl', 'cogs_by_product', 'gross_profit_by_item']:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[table] = row[0]

        # Date ranges
        for table, col in [('actual_costs', 'fiscal_week_start'),
                           ('cogs_by_product', 'reporting_period')]:
            dr = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {table}").fetchone()
            stats[f'{table}_range'] = {'min': dr[0], 'max': dr[1]} if dr and dr[0] else {}

        stats['db_size_mb'] = round(os.path.getsize(db_path) / 1024 / 1024, 1)
        return stats
    finally:
        conn.close()


# ── Report Functions ────────────────────────────────────────────────


def rpt_actual_costs_by_period(
    fiscal_period: str = None,
    store_ids: Optional[List[str]] = None,
    db_path: str = CRUNCHTIME_DB_PATH,
) -> List[Dict]:
    """
    Replaces: Budget_Worksheet_COGS_Tabs (partial)
    Actual costs by store and fiscal week for a period.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT store_id, fiscal_period, fiscal_week, fiscal_week_start,
                   actual_cost
            FROM actual_costs
            WHERE 1=1
        """
        params = []
        if fiscal_period:
            sql += " AND fiscal_period = ?"
            params.append(fiscal_period)
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY store_id, fiscal_week_start"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "fiscalPeriod": r[1], "fiscalWeek": r[2],
             "fiscalWeekStart": r[3], "actualCost": round(r[4] or 0, 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_costs_by_gl_code(
    reporting_period: str = None,
    gl_code: str = None,
    store_ids: Optional[List[str]] = None,
    db_path: str = CRUNCHTIME_DB_PATH,
) -> List[Dict]:
    """
    Replaces: Budget_Worksheet_COGS_Tabs (GL detail)
    Actual costs broken out by GL code per store per period.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT store_id, reporting_period, fiscal_week_start, gl_code,
                   beginning_inv_value, purchases_value,
                   incoming_transfer_value, outgoing_transfer_value,
                   ending_inv_value, actual_cost
            FROM actual_costs_by_gl
            WHERE 1=1
        """
        params = []
        if reporting_period:
            sql += " AND reporting_period = ?"
            params.append(reporting_period)
        if gl_code:
            sql += " AND gl_code = ?"
            params.append(gl_code)
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY store_id, fiscal_week_start, gl_code"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "reportingPeriod": r[1], "fiscalWeekStart": r[2],
             "glCode": r[3], "beginningInv": round(r[4] or 0, 2),
             "purchases": round(r[5] or 0, 2),
             "incomingTransfer": round(r[6] or 0, 2),
             "outgoingTransfer": round(r[7] or 0, 2),
             "endingInv": round(r[8] or 0, 2),
             "actualCost": round(r[9] or 0, 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_cogs_by_product(
    reporting_period: str = None,
    category: str = None,
    store_ids: Optional[List[str]] = None,
    db_path: str = CRUNCHTIME_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_AvT_By_Product_by_Week / sp_DOMO_COGSByProduct
    COGS by product — actual vs theoretical variance.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT location_code, reporting_period, category, subcategory,
                   product_name, act_consumpt_value, theoretical_value,
                   at_variance_value, beginning_inv_value, ending_inv_value,
                   purchases_value, inv_adjustment_value
            FROM cogs_by_product
            WHERE 1=1
        """
        params = []
        if reporting_period:
            sql += " AND reporting_period = ?"
            params.append(reporting_period)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND location_code IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY location_code, category, subcategory, product_name"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"locationCode": r[0], "reportingPeriod": r[1], "category": r[2],
             "subcategory": r[3], "productName": r[4],
             "actualConsumption": round(r[5] or 0, 2),
             "theoretical": round(r[6] or 0, 2),
             "atVariance": round(r[7] or 0, 2),
             "beginningInv": round(r[8] or 0, 2),
             "endingInv": round(r[9] or 0, 2),
             "purchases": round(r[10] or 0, 2),
             "invAdjustment": round(r[11] or 0, 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_cogs_summary_by_category(
    reporting_period: str = None,
    store_ids: Optional[List[str]] = None,
    db_path: str = CRUNCHTIME_DB_PATH,
) -> List[Dict]:
    """
    COGS summary rolled up by category per store — for dashboard view.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT location_code, reporting_period, category,
                   SUM(act_consumpt_value) AS total_actual,
                   SUM(theoretical_value) AS total_theoretical,
                   SUM(at_variance_value) AS total_variance,
                   SUM(purchases_value) AS total_purchases
            FROM cogs_by_product
            WHERE 1=1
        """
        params = []
        if reporting_period:
            sql += " AND reporting_period = ?"
            params.append(reporting_period)
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND location_code IN ({ph})"
            params.extend(store_ids)
        sql += " GROUP BY location_code, reporting_period, category"
        sql += " ORDER BY location_code, category"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"locationCode": r[0], "reportingPeriod": r[1], "category": r[2],
             "totalActual": round(r[3] or 0, 2),
             "totalTheoretical": round(r[4] or 0, 2),
             "totalVariance": round(r[5] or 0, 2),
             "totalPurchases": round(r[6] or 0, 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_gross_profit_by_item(
    fiscal_week_start: str = None,
    store_ids: Optional[List[str]] = None,
    db_path: str = CRUNCHTIME_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_update_GrossProfitByItem (report view)
    Gross profit by menu item per store.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT store_id, fiscal_week, fiscal_week_start,
                   date_of_business, item_id, gross_profit
            FROM gross_profit_by_item
            WHERE 1=1
        """
        params = []
        if fiscal_week_start:
            sql += " AND fiscal_week_start = ?"
            params.append(fiscal_week_start)
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY store_id, date_of_business, item_id"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "fiscalWeek": r[1], "fiscalWeekStart": r[2],
             "dateOfBusiness": r[3], "itemId": r[4],
             "grossProfit": round(r[5] or 0, 2)}
            for r in rows
        ]
    finally:
        conn.close()


def get_available_periods(db_path: str = CRUNCHTIME_DB_PATH) -> List[Dict]:
    """Return list of distinct reporting periods with friendly labels and date ranges."""
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        # Get periods from cogs_by_product
        periods = conn.execute(
            "SELECT DISTINCT reporting_period FROM cogs_by_product "
            "ORDER BY reporting_period DESC"
        ).fetchall()

        # Get fiscal_week → date mapping from actual_costs
        week_dates = {}
        wd_rows = conn.execute(
            "SELECT fiscal_week, fiscal_week_start FROM actual_costs "
            "GROUP BY fiscal_week ORDER BY fiscal_week"
        ).fetchall()
        for r in wd_rows:
            week_dates[r[0]] = r[1]

        result = []
        for (p,) in periods:
            if not p:
                continue
            year = p[:4]
            week_num = int(p[4:])
            week_start = week_dates.get(p, '')
            # Build friendly label: "FY2026 Wk 9 (Feb 23)"
            label = f"FY{year} Wk {week_num}"
            if week_start:
                from datetime import datetime
                try:
                    dt = datetime.strptime(week_start, "%Y-%m-%d")
                    label += f" ({dt.strftime('%b %d')})"
                except Exception:
                    pass
            result.append({"value": p, "label": label, "weekStart": week_start})
        return result
    finally:
        conn.close()


def get_available_categories(db_path: str = CRUNCHTIME_DB_PATH) -> List[str]:
    """Return list of distinct COGS categories."""
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM cogs_by_product ORDER BY category"
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    finally:
        conn.close()


def get_available_gl_codes(db_path: str = CRUNCHTIME_DB_PATH) -> List[str]:
    """Return list of distinct GL codes."""
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT gl_code FROM actual_costs_by_gl ORDER BY gl_code"
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    finally:
        conn.close()


# ── CSV Import ──────────────────────────────────────────────────────

# Column mappings for headerless CSVs exported from SQL Server

ACTUAL_COSTS_COLUMNS = ['store_id', 'fiscal_period', 'fiscal_week', 'actual_cost', 'fiscal_week_start']

ACTUAL_COSTS_BY_GL_COLUMNS = [
    'store_id', 'reporting_period', 'fiscal_week_start', 'gl_code',
    'beginning_inv_value', 'incoming_transfer_value', 'purchases_value',
    'vendor_order_purchases_value', 'outgoing_transfer_value',
    'ending_inv_value', 'pbi_detail_amount', 'production_value',
    'production_consumpt_value', 'actual_cost',
]

COGS_BY_PRODUCT_COLUMNS = [
    'location_code', 'reporting_period', 'category', 'subcategory',
    'product_name', 'at_variance_value', 'act_consumpt_value',
    'beginning_inv_value', 'ending_inv_value', 'inv_adjustment_value',
    'purchases_value', 'theoretical_value',
]

GROSS_PROFIT_COLUMNS = [
    'store_id', 'fiscal_week', 'fiscal_week_start',
    'date_of_business', 'item_id', 'gross_profit',
]


def _clean_val(val):
    if val is None or val == 'NULL' or val == '':
        return None
    return val.strip() if isinstance(val, str) else val


def import_csv(table: str, csv_path: str, columns: list, db_path: str = CRUNCHTIME_DB_PATH):
    """Import a CSV into the specified table."""
    import csv as csv_mod

    initialize_db(db_path)
    conn = get_connection(db_path)

    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv_mod.reader(f)
        # Check if first row is a header
        first_row = next(reader)
        has_header = any(c.lower() in [col.lower() for col in columns] for c in first_row)

        rows_data = []
        if not has_header:
            # First row is data
            rows_data.append([_clean_val(v) for v in first_row])

        for raw_row in reader:
            if len(raw_row) >= len(columns):
                rows_data.append([_clean_val(raw_row[i]) for i in range(len(columns))])

    # Get columns that exist in schema
    cursor = conn.execute(f"PRAGMA table_info({table})")
    db_cols = {r[1] for r in cursor.fetchall()}
    insert_cols = [c for c in columns if c in db_cols]

    if not insert_cols:
        logger.warning("No matching columns for table %s", table)
        conn.close()
        return 0

    col_indices = [columns.index(c) for c in insert_cols]
    ph = ",".join("?" * len(insert_cols))
    col_str = ",".join(insert_cols)

    conn.execute(f"DELETE FROM {table}")
    count = 0
    for row_data in rows_data:
        vals = [row_data[i] if i < len(row_data) else None for i in col_indices]
        try:
            conn.execute(f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", vals)
            count += 1
        except Exception as e:
            if count == 0:
                logger.warning("Insert error on first row of %s: %s", table, e)

    conn.commit()
    conn.close()
    logger.info("Imported %d rows into crunchtime.%s", count, table)
    return count
