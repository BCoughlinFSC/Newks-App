"""
monkey_db.py — SQLite layer for Monkey Media (catering) data.

Replaces the SQL Server Monkey_Production database.
Tables: stores, accounts, clients_and_leads, orders, order_items,
        items, drivers, payments, refunds
"""

import os
import sqlite3
import logging
from typing import Optional, List, Dict, Any
from datetime import date, datetime

logger = logging.getLogger(__name__)

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MONKEY_DB_PATH = os.path.join(_DB_DIR, "monkey.db")

# ── Schema ──────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stores (
    store_id        INTEGER PRIMARY KEY,
    region_id       INTEGER,
    region_name     TEXT,
    store_name      TEXT,
    store_code      TEXT,
    store_manager   TEXT,
    store_manager_email TEXT,
    phone           TEXT,
    fax             TEXT,
    store_email     TEXT,
    address_1       TEXT,
    address_2       TEXT,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    latitude        REAL,
    longitude       REAL,
    accepts_online_orders INTEGER,
    status          TEXT DEFAULT 'Active'
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id      INTEGER PRIMARY KEY,
    house_account   INTEGER,
    company_name    TEXT,
    accounts_payable_contact TEXT,
    email_address   TEXT,
    phone_number    TEXT,
    address_1       TEXT,
    city            TEXT,
    zip_code        TEXT,
    state           TEXT,
    discount_percentage REAL,
    tax_exempt      INTEGER,
    payment_terms   INTEGER,
    marketing_group TEXT,
    status          TEXT DEFAULT 'active',
    is_deleted      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clients_and_leads (
    client_id       INTEGER PRIMARY KEY,
    is_lead         INTEGER,
    account_name    TEXT,
    account_id      INTEGER,
    first_name      TEXT,
    last_name       TEXT,
    email_address   TEXT,
    phone_number    TEXT,
    cell_number     TEXT,
    address1        TEXT,
    city            TEXT,
    zip_code        TEXT,
    state           TEXT,
    catering_sales_manager TEXT,
    preferred_store_id INTEGER,
    preferred_region_store_name TEXT,
    on_mailing_list INTEGER,
    status          TEXT,
    date_added      TEXT,
    is_deleted      INTEGER DEFAULT 0,
    company_name    TEXT,
    marketing_group TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        INTEGER PRIMARY KEY,
    is_promo        INTEGER,
    billing_id      INTEGER,
    billing_name    TEXT,
    client_id       INTEGER,
    client_name     TEXT,
    store_id        INTEGER,
    region_store_name TEXT,
    order_date      TEXT,
    date_reqd       TEXT,
    delivery_type   TEXT,
    order_class     TEXT,
    number_of_guests TEXT,
    payment_method  TEXT,
    order_type      TEXT,
    entered_by      TEXT,
    confirmed_by    TEXT,
    confirmed_by_time TEXT,
    store_accept_time TEXT,
    subtotal        REAL,
    discount        REAL,
    discount_code   TEXT,
    gratuity        REAL,
    tax             REAL,
    tax_exempt      INTEGER,
    delivery_fee    REAL,
    total           REAL,
    status          TEXT,
    paid            INTEGER,
    paid_date       TEXT,
    locked          INTEGER,
    deleted         INTEGER DEFAULT 0,
    pos_order_id    TEXT,
    pos_post_time   TEXT,
    driver_id       INTEGER,
    coupon_name     TEXT,
    discount_coupon_amount REAL,
    ezcater_order_id TEXT,
    delivery_partner TEXT,
    api_vendor_name TEXT,
    order_note      TEXT,
    delivery_instruction TEXT,
    order_source    TEXT,
    order_last_modified_datetime TEXT,
    setup_fee       REAL,
    center_fee      REAL,
    rental_fee      REAL,
    additional_gratuity REAL,
    revenue_source  INTEGER
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id   INTEGER PRIMARY KEY,
    order_id        INTEGER,
    menu_item_id    INTEGER,
    item_name       TEXT,
    quantity        REAL,
    menu_item_price REAL,
    item_discount   REAL,
    item_total      REAL,
    special_instructions TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS items (
    item_id         INTEGER PRIMARY KEY,
    item_class      TEXT,
    item_category   TEXT,
    item_group      TEXT,
    item_name       TEXT,
    department      TEXT,
    unit            TEXT,
    price           REAL,
    taxable         INTEGER,
    sell_on_web     INTEGER,
    status          TEXT DEFAULT 'Active'
);

CREATE TABLE IF NOT EXISTS drivers (
    driver_id       INTEGER PRIMARY KEY,
    is_store_driver INTEGER,
    region_id       INTEGER,
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    store_id        INTEGER,
    is_store_default_driver TEXT,
    delivery_partner TEXT,
    status          INTEGER
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id      INTEGER PRIMARY KEY,
    process_id      INTEGER,
    order_id        INTEGER,
    account_id      INTEGER,
    client_id       INTEGER,
    amount          REAL,
    payment_date    TEXT,
    method          TEXT,
    check_no        TEXT,
    locked          INTEGER
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id       INTEGER PRIMARY KEY,
    process_id      INTEGER,
    account_id      INTEGER,
    account_name    TEXT,
    order_id        INTEGER,
    amount          REAL,
    tax_refund      REAL,
    total           REAL,
    method          TEXT,
    refund_date     TEXT,
    note            TEXT,
    refund_by       TEXT,
    check_no        TEXT,
    locked          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_orders_store_id ON orders(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_date_reqd ON orders(date_reqd);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_delivery_type ON orders(delivery_type);
CREATE INDEX IF NOT EXISTS idx_orders_entered_by ON orders(entered_by);
CREATE INDEX IF NOT EXISTS idx_orders_delivery_partner ON orders(delivery_partner);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order_id ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_clients_account_id ON clients_and_leads(account_id);
"""


def get_connection(db_path: str = MONKEY_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_initialized = False

def initialize_db(db_path: str = MONKEY_DB_PATH):
    global _initialized
    if _initialized:
        return
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA_SQL)
    # Migrate: add columns that may be missing from older schema
    _migrate_add_columns(conn, "stores", {
        "fax": "TEXT", "store_email": "TEXT",
        "address_1": "TEXT", "address_2": "TEXT",
    })
    conn.close()
    _initialized = True


def _migrate_add_columns(conn, table: str, columns: dict):
    """Add missing columns to an existing table (idempotent)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, col_type in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            logger.info("Added column %s.%s", table, col)


def db_available(db_path: str = MONKEY_DB_PATH) -> bool:
    try:
        if not os.path.exists(db_path):
            return False
        conn = get_connection(db_path)
        row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()
        conn.close()
        return row[0] > 0
    except Exception:
        return False


def get_stats(db_path: str = MONKEY_DB_PATH) -> dict:
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        stats = {}
        for table in ['orders', 'order_items', 'accounts', 'clients_and_leads',
                       'items', 'stores', 'drivers', 'payments', 'refunds']:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[table] = row[0]

        # Date range
        dr = conn.execute(
            "SELECT MIN(date_reqd), MAX(date_reqd) FROM orders WHERE deleted = 0"
        ).fetchone()
        stats['date_range'] = {'min': dr[0], 'max': dr[1]} if dr else {}

        # DB size
        stats['db_size_mb'] = round(os.path.getsize(db_path) / 1024 / 1024, 1)
        return stats
    finally:
        conn.close()


# ── Report Functions (matching SQL Server SPs) ──────────────────────


def store_ids_by_scope(scope: str, db_path: str = MONKEY_DB_PATH) -> Optional[List[int]]:
    """Resolve a store scope to Monkey store_ids, or None for "no filter".

    The SSRS subscriptions these reports replace scoped themselves with
    get_StoresIncluded(grouping 9, group 79=Corporate / group 8=Franchise).
    We can't reach contacts.dbo.internal_StoreGroupMembers from here, but
    Monkey tags each store's region_name ("... - Company Restaurants" vs the
    franchise group names) — the same signal _period_catering_core already
    uses to scope Period Catering by Week to company restaurants.

    scope: 'company' | 'franchise' | 'all' (anything else = all).
    Returns None when the scope is 'all' OR when nothing matched, so a naming
    change in Monkey can never silently empty a report — it degrades to
    all-stores instead.
    """
    scope = (scope or "all").strip().lower()
    if scope not in ("company", "franchise"):
        return None

    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT store_id, COALESCE(region_name, '') FROM stores"
        ).fetchall()
    finally:
        conn.close()

    company = {sid for sid, region in rows if "company" in region.lower()}
    if not company:
        logger.warning("store_ids_by_scope(%s): no store matched 'company' in "
                       "region_name — falling back to all stores", scope)
        return None

    if scope == "company":
        return sorted(company)
    franchise = sorted({sid for sid, _ in rows} - company)
    return franchise or None


def rpt_sales_by_channel_by_weekday(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: new01_sp_rpt_Monkey_Sales_by_Channel_by_Weekday
    Catering sales by channel (Online, Store, Voice, CSM, Marketplace) and weekday.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                CASE strftime('%w', DATE(date_reqd))
                    WHEN '0' THEN 'Sunday'
                    WHEN '1' THEN 'Monday'
                    WHEN '2' THEN 'Tuesday'
                    WHEN '3' THEN 'Wednesday'
                    WHEN '4' THEN 'Thursday'
                    WHEN '5' THEN 'Friday'
                    WHEN '6' THEN 'Saturday'
                END AS weekday,
                CASE
                    WHEN entered_by LIKE '%Online%' OR order_source LIKE '%online%'
                        OR api_vendor_name IS NOT NULL THEN 'Online'
                    WHEN entered_by LIKE '%CSM%' OR entered_by LIKE '%NSC%' THEN 'CSM'
                    WHEN ezcater_order_id IS NOT NULL AND ezcater_order_id != ''
                        THEN 'Marketplace'
                    WHEN entered_by LIKE '%Voice%' THEN 'Voice'
                    ELSE 'Store'
                END AS channel,
                COUNT(*) AS order_count,
                COALESCE(SUM(subtotal), 0) AS subtotal,
                COALESCE(SUM(total), 0) AS total
            FROM orders
            WHERE DATE(date_reqd) BETWEEN ? AND ?
              AND deleted = 0
              AND status NOT IN ('Cancelled', 'Void')
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND store_id IN ({ph})"
            params.extend(store_ids)
        sql += " GROUP BY weekday, channel ORDER BY weekday, channel"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"weekday": r[0], "channel": r[1], "orderCount": r[2],
             "subtotal": round(r[3], 2), "total": round(r[4], 2)}
            for r in rows
        ]
    finally:
        conn.close()


# Channel classification for "Sales by Channel by Weekday" — driven by how the
# order was entered (entered_by) plus marketplace order_source. Verified against
# the Monkey export: marketplace=order_source, Online/Store are literal
# entered_by values, Call Center = TCOT-prefixed agents, everything else named
# is a CSM. (Sales Team was 0 across the whole export; kept as its own bucket
# for anyone entered_by a Sales Team member — currently none matched.)
_CHANNEL_CASE_SQL = """
    CASE
        WHEN LOWER(COALESCE(o.order_source, '')) = 'marketplace' THEN 'marketplace'
        WHEN TRIM(o.entered_by) = 'Online' THEN 'online'
        WHEN TRIM(o.entered_by) = 'Store'  THEN 'store'
        WHEN o.entered_by LIKE 'TCOT%'
             OR o.entered_by LIKE 'COT %'   THEN 'callCenter'
        ELSE 'csm'
    END
"""

# Display order + labels, matching the export's column order.
SALES_CHANNELS = [
    ("online", "Online"),
    ("store", "Store"),
    ("csm", "CSM"),
    ("salesTeam", "Sales Team"),
    ("callCenter", "Call Center"),
    ("marketplace", "Marketplace"),
]

_WEEKDAY_NAMES = {
    "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday",
}


def rpt_sales_by_channel(
    start_date: str, end_date: str,
    group_by: str = "system",
    db_path: str = MONKEY_DB_PATH,
) -> Dict:
    """
    Monkey "Sales by Channel by Weekday" — daily catering sales split into the
    six channels (Online, Store, CSM, Sales Team, Call Center, Marketplace),
    with per-day order counts and subtotal dollars.

    `group_by`: 'system' (all stores combined), 'market' (by Monkey region_name),
    or 'store' (per store). Returns groups, each with per-date rows + a total row.
    """
    initialize_db(db_path)
    group_expr = {
        "system": "'System Wide'",
        "market": "COALESCE(s.region_name, '(No Market)')",
        "store":  "COALESCE(s.store_name, '(No Store)')",
    }.get(group_by, "'System Wide'")

    conn = get_connection(db_path)
    try:
        sql = f"""
            SELECT {group_expr} AS grp,
                   DATE(o.date_reqd) AS d,
                   {_CHANNEL_CASE_SQL} AS channel,
                   COUNT(*) AS n,
                   COALESCE(SUM(COALESCE(o.subtotal, 0) - COALESCE(o.discount, 0)), 0) AS amt
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(o.date_reqd) BETWEEN ? AND ?
              AND o.deleted = 0
              AND LOWER(TRIM(o.status)) = 'confirmed'
              AND COALESCE(LOWER(TRIM(o.order_type)), '') <> 'quote'
            GROUP BY grp, d, channel
        """
        rows = conn.execute(sql, [start_date, end_date]).fetchall()
    finally:
        conn.close()

    from collections import defaultdict

    def _blank_cell():
        return {f"{k}Count": 0 for k, _ in SALES_CHANNELS} | \
               {k: 0.0 for k, _ in SALES_CHANNELS} | {"total": 0.0}

    # grp -> date -> cell
    grouped: Dict[str, Dict[str, dict]] = defaultdict(lambda: defaultdict(_blank_cell))
    for grp, d, channel, n, amt in rows:
        cell = grouped[grp][d]
        cell[f"{channel}Count"] += n
        cell[channel] += amt or 0.0
        cell["total"] += amt or 0.0

    out_groups = []
    for grp in sorted(grouped.keys()):
        date_map = grouped[grp]
        day_rows = []
        total_cell = _blank_cell()
        for d in sorted(date_map.keys()):
            cell = date_map[d]
            row = {"date": d, "dayOfWeek": _weekday_name(d)}
            for k, _ in SALES_CHANNELS:
                row[f"{k}Count"] = cell[f"{k}Count"]
                row[k] = round(cell[k], 2)
                total_cell[f"{k}Count"] += cell[f"{k}Count"]
                total_cell[k] += cell[k]
            row["total"] = round(cell["total"], 2)
            total_cell["total"] += cell["total"]
            day_rows.append(row)
        totals = {"date": "Total", "dayOfWeek": ""}
        for k, _ in SALES_CHANNELS:
            totals[f"{k}Count"] = total_cell[f"{k}Count"]
            totals[k] = round(total_cell[k], 2)
        totals["total"] = round(total_cell["total"], 2)
        out_groups.append({"name": grp, "rows": day_rows, "totals": totals})

    return {
        "groupBy": group_by,
        "channels": [{"key": k, "label": lbl} for k, lbl in SALES_CHANNELS],
        "groups": out_groups,
    }


def _weekday_name(date_str: str) -> str:
    from datetime import date
    try:
        y, m, d = (int(x) for x in date_str[:10].split("-"))
        # date.weekday(): Mon=0..Sun=6 → convert to strftime %w (Sun=0)
        return _WEEKDAY_NAMES[str((date(y, m, d).weekday() + 1) % 7)]
    except Exception:
        return ""


def rpt_delivery_counts_by_partner(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_Delivery_Counts_By_Delivery_Partner
    Delivery order counts by delivery partner per store per date.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                o.store_id,
                s.store_name,
                DATE(o.date_reqd) AS business_date,
                COALESCE(
                    CASE
                        WHEN o.delivery_partner LIKE '%DoorDash%' THEN 'DoorDash'
                        WHEN o.delivery_partner LIKE '%Deliver That%' THEN 'Deliver That'
                        WHEN o.delivery_partner LIKE '%ezDispatch%' OR o.delivery_partner LIKE '%EZDispatch%'
                            THEN 'ezDispatch'
                        ELSE o.delivery_partner
                    END,
                    'Store Driver'
                ) AS partner,
                COUNT(*) AS order_count
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE o.delivery_type = 'delivery'
              AND o.date_reqd BETWEEN ? AND ?
              AND o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " GROUP BY o.store_id, s.store_name, business_date, partner"
        sql += " ORDER BY o.store_id, business_date, partner"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "storeName": r[1], "date": r[2],
             "partner": r[3], "orderCount": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_catering_by_handoff_mode(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_MSI_Catering_Orders_by_Handoff_Mode
    Catering orders by pickup vs delivery per store.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                o.store_id,
                s.store_name,
                o.delivery_type,
                COUNT(*) AS order_count,
                COALESCE(SUM(o.subtotal), 0) AS subtotal,
                COALESCE(SUM(o.total), 0) AS total
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(o.date_reqd) BETWEEN ? AND ?
              AND o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " GROUP BY o.store_id, s.store_name, o.delivery_type"
        sql += " ORDER BY o.store_id, o.delivery_type"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "storeName": r[1], "deliveryType": r[2],
             "orderCount": r[3], "subtotal": round(r[4], 2), "total": round(r[5], 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_client_stats(db_path: str = MONKEY_DB_PATH) -> List[Dict]:
    """
    Replaces: sp_rpt_Monkey_Client_Stats
    All clients with at least one order — shows totals and last order.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                c.client_id,
                c.first_name || ' ' || c.last_name AS client_name,
                c.email_address,
                c.company_name,
                c.preferred_region_store_name,
                COUNT(o.order_id) AS total_orders,
                MAX(o.date_reqd) AS last_order_date,
                s2.store_name AS last_order_store
            FROM clients_and_leads c
            JOIN orders o ON o.client_id = c.client_id AND o.deleted = 0
            LEFT JOIN stores s2 ON o.store_id = s2.store_id
            WHERE c.is_deleted = 0
            GROUP BY c.client_id
            HAVING total_orders >= 1
            ORDER BY total_orders DESC
            LIMIT 5000
        """).fetchall()
        return [
            {"clientId": r[0], "clientName": r[1], "email": r[2],
             "company": r[3], "preferredStore": r[4], "totalOrders": r[5],
             "lastOrderDate": r[6], "lastOrderStore": r[7]}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_top_clients_by_sales(
    min_total: float = 500.0,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_Top_1000_Highest_Sales
    Clients with lifetime sales over threshold.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                c.client_id,
                c.first_name || ' ' || c.last_name AS client_name,
                c.email_address,
                c.company_name,
                COALESCE(SUM(o.subtotal), 0) AS lifetime_subtotal,
                COALESCE(SUM(o.tax), 0) AS lifetime_tax,
                COALESCE(SUM(o.gratuity), 0) AS lifetime_tip,
                COALESCE(SUM(o.discount), 0) AS lifetime_discount,
                COALESCE(SUM(o.total), 0) AS lifetime_total,
                COUNT(o.order_id) AS order_count,
                ROUND(COALESCE(SUM(o.total), 0) / MAX(COUNT(o.order_id), 1), 2) AS avg_order
            FROM clients_and_leads c
            JOIN orders o ON o.client_id = c.client_id
                AND o.deleted = 0
                AND o.status NOT IN ('Cancelled', 'Void')
            WHERE c.is_deleted = 0
            GROUP BY c.client_id
            HAVING lifetime_total > ?
            ORDER BY lifetime_total DESC
            LIMIT 1000
        """, (min_total,)).fetchall()
        return [
            {"clientId": r[0], "clientName": r[1], "email": r[2],
             "company": r[3], "subtotal": round(r[4], 2), "tax": round(r[5], 2),
             "tip": round(r[6], 2), "discount": round(r[7], 2),
             "total": round(r[8], 2), "orderCount": r[9], "avgOrder": r[10]}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_discounts_by_user(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_Monkey_Discounts_by_User
    Discounts applied to orders by entering/editing agent.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                o.store_id,
                s.store_name,
                DATE(o.date_reqd) AS business_date,
                o.entered_by,
                o.order_id,
                o.client_name,
                o.discount,
                o.discount_code,
                o.subtotal,
                o.total
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(o.date_reqd) BETWEEN ? AND ?
              AND o.deleted = 0
              AND o.discount > 0
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY o.store_id, o.date_reqd, o.entered_by"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "storeName": r[1], "date": r[2],
             "enteredBy": r[3], "orderId": r[4], "clientName": r[5],
             "discount": round(r[6], 2), "discountCode": r[7],
             "subtotal": round(r[8], 2), "total": round(r[9], 2)}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_refunds_by_user(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_Monkey_Refunds_by_User  (Monkey "Refunds by User" report)

    Columns match the Monkey export exactly:
      Refunded By, Refund Date, Restaurant Name, Order Number, Customer Name,
      Refund Notes, Payment Method, Order Total, Refund Total,
      Refund Sub Total, Refund Tax Amount.

    Refund money split (from the Monkey refunds table):
      refundSubTotal = r.amount      (pre-tax portion refunded)
      refundTaxAmount = r.tax_refund (tax portion refunded)
      refundTotal     = r.total      (= amount + tax_refund)
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                r.refund_by                     AS refunded_by,
                DATE(r.refund_date)             AS refund_date,
                s.store_name                    AS restaurant_name,
                r.order_id                      AS order_number,
                COALESCE(o.client_name, r.account_name) AS customer_name,
                r.note                          AS refund_notes,
                r.method                        AS payment_method,
                o.total                         AS order_total,
                r.total                         AS refund_total,
                r.amount                        AS refund_sub_total,
                r.tax_refund                    AS refund_tax_amount,
                o.store_id                      AS store_id
            FROM refunds r
            LEFT JOIN orders o ON r.order_id = o.order_id
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(r.refund_date) BETWEEN ? AND ?
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY r.refund_date, s.store_name, r.refund_by"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"refundedBy": r[0], "refundDate": r[1], "restaurantName": r[2],
             "orderNumber": r[3], "customerName": r[4], "refundNotes": r[5],
             "paymentMethod": r[6],
             "orderTotal": round(r[7] or 0, 2),
             "refundTotal": round(r[8] or 0, 2),
             "refundSubTotal": round(r[9] or 0, 2),
             "refundTaxAmount": round(r[10] or 0, 2),
             "storeId": r[11]}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_orders_due_by_store(
    biz_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Monkey "Orders Due For Today - Summary by Store".

    One row per store for the given required date: number of live orders and
    total dollars. The Monkey export labels each store as
    "<date> - <store#> - <store name>"; we return the pieces plus a composed
    label so the UI can match that format.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                o.store_id,
                s.store_code,
                COALESCE(s.store_name, o.region_store_name) AS store_name,
                COUNT(*)                    AS order_count,
                COALESCE(SUM(o.total), 0)   AS total_dollars
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(o.date_reqd) = DATE(?)
              AND o.deleted = 0
              AND LOWER(TRIM(o.status)) = 'confirmed'
              AND COALESCE(LOWER(TRIM(o.order_type)), '') <> 'quote'
        """
        params: list = [biz_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " GROUP BY o.store_id, s.store_code, store_name"
        sql += " ORDER BY s.store_code, store_name"

        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            store_id, store_code, store_name, order_count, total_dollars = r
            num = (store_code or "").strip() if store_code else ""
            label_store = f"{num} - {store_name}" if num else (store_name or "")
            out.append({
                "storeId": store_id,
                "storeNumber": num,
                "storeName": store_name or "",
                "restaurant": f"{biz_date} - {label_store}",
                "orderCount": order_count,
                "totalDollars": round(total_dollars, 2),
            })
        return out
    finally:
        conn.close()


def _sunday_of(date_str: str) -> str:
    """Return the fiscal week label for a date: the Monday–Sunday week's ENDING
    Sunday, as 'YYYY-MM-DD' — matching the fiscal_calendar's weeks and the SSRS
    week labels (e.g. Mon 01-26 … Sun 02-01 → "2026-02-01")."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in date_str[:10].split("-"))
    dt = date(y, m, d)
    # date.weekday(): Mon=0 .. Sun=6 → days forward to the week's ending Sunday
    return (dt + timedelta(days=(6 - dt.weekday()) % 7)).isoformat()


def rpt_period_catering_by_week(
    start_date: str, end_date: str,
    areadir_by_store_number: Optional[Dict[int, str]] = None,
    basis: str = "subtotal",
    db_path: str = MONKEY_DB_PATH,
    company_only: bool = True,
) -> List[Dict]:
    """
    Calendar-mode "Period Catering by Week": Sunday-start weeks spanning
    [start_date, end_date], prior year = same weekday 364 days back.
    Thin wrapper over _period_catering_core.
    """
    from datetime import date, timedelta
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    cy_date_to_week: Dict[str, str] = {}
    py_date_to_week: Dict[str, str] = {}
    d = d0
    while d <= d1:
        wk = _sunday_of(d.isoformat())
        cy_date_to_week[d.isoformat()] = wk
        py_date_to_week[(d - timedelta(days=364)).isoformat()] = wk
        d += timedelta(days=1)
    return _period_catering_core(cy_date_to_week, py_date_to_week,
                                 areadir_by_store_number, basis, db_path,
                                 company_only=company_only)


def rpt_period_catering_by_week_fiscal(
    fiscal_dates: List[tuple],
    areadir_by_store_number: Optional[Dict[int, str]] = None,
    basis: str = "subtotal",
    db_path: str = MONKEY_DB_PATH,
    company_only: bool = True,
) -> List[Dict]:
    """
    Fiscal-mode "Period Catering by Week", matching the SSRS report.

    `fiscal_dates` is a list of (cy_date, ly_date, fiscal_period) triples from
    the fiscal_calendar for one fiscal year. Weeks are the fiscal Mon–Sun weeks
    (label = ending Sunday); prior year uses the authoritative ly_date mapping.

    SSRS quirk reproduced: a fiscal week that straddles a PERIOD boundary is
    counted once per period it touches (so a week spanning two periods is ×2).
    We compute a per-week multiplier = number of distinct fiscal periods among
    the week's days and pass it to the core.
    """
    from collections import defaultdict
    from datetime import date, timedelta

    # First pass: week labels (ending Sundays), authoritative ly_date per day,
    # and the fiscal periods each week touches (for the ×N period multiplier).
    ly_of: Dict[str, str] = {}
    week_periods: Dict[str, set] = defaultdict(set)
    week_labels: set = set()
    for row in fiscal_dates:
        cy_date, ly_date = row[0], row[1]
        period = row[2] if len(row) > 2 else None
        if not cy_date:
            continue
        wk = _sunday_of(cy_date)
        week_labels.add(wk)
        if ly_date:
            ly_of[cy_date[:10]] = ly_date[:10]
        if period is not None:
            week_periods[wk].add(period)

    # Expand every week to its full Mon–Sun span, so boundary weeks include the
    # trailing days that fall in the prior/next calendar year (e.g. the week
    # ending 2026-01-04 pulls in Dec 29–31, 2025 — which SSRS counts).
    cy_date_to_week: Dict[str, str] = {}
    py_date_to_week: Dict[str, str] = {}
    for wk in week_labels:
        sunday = date.fromisoformat(wk)
        for i in range(7):
            day = (sunday - timedelta(days=i)).isoformat()
            cy_date_to_week[day] = wk
            ly = ly_of.get(day) or (
                date.fromisoformat(day) - timedelta(days=364)).isoformat()
            py_date_to_week[ly] = wk

    week_multiplier = {wk: max(1, len(ps)) for wk, ps in week_periods.items()}
    return _period_catering_core(cy_date_to_week, py_date_to_week,
                                 areadir_by_store_number, basis, db_path,
                                 company_only=company_only,
                                 week_multiplier=week_multiplier)


def _period_catering_core(
    cy_date_to_week: Dict[str, str],
    py_date_to_week: Dict[str, str],
    areadir_by_store_number: Optional[Dict[int, str]] = None,
    basis: str = "subtotal",
    db_path: str = MONKEY_DB_PATH,
    company_only: bool = True,
    week_multiplier: Optional[Dict[str, int]] = None,
) -> List[Dict]:
    """
    Core weekly-catering aggregator. `cy_date_to_week` / `py_date_to_week` map a
    business date ('YYYY-MM-DD') to the CY week label (Sunday) it belongs to;
    PY dates map to the CY week they should be compared against. This lets the
    caller define weeks either by calendar or by fiscal calendar.

    `company_only`: when True (default), include only stores that map to an Area
    Director — matching the SSRS report, which is scoped to company stores and
    has no "Unassigned" group. Franchise/unmapped stores are excluded.

    Returns week blocks (chronological) with per-Area-Director rows and per-week
    totals — same shape as the SSRS export.
    """
    initialize_db(db_path)
    areadir_by_store_number = areadir_by_store_number or {}
    week_multiplier = week_multiplier or {}
    money_col = "o.total" if basis == "total" else "o.subtotal"

    conn = get_connection(db_path)
    try:
        # store_id -> (store_number, store_name, region_name)
        store_meta: Dict[int, tuple] = {}
        for sid, code, name, region in conn.execute(
            "SELECT store_id, store_code, store_name, region_name FROM stores"
        ).fetchall():
            store_meta[sid] = (code, name, region)

        # Company-store set: SSRS scopes this report to company restaurants,
        # which Monkey marks in region_name (e.g. "… - Company Restaurants")
        # vs franchise groups. Fall back to "all stores" if nothing matches, so
        # a naming difference can't blank the report.
        company_store_ids = {
            sid for sid, (_c, _n, region) in store_meta.items()
            if region and "company" in str(region).lower()
        }
        if company_only and not company_store_ids:
            logger.warning("Period-by-Week: no company stores matched region_name — "
                           "showing all stores")
            company_only = False

        def _sum_by_week(date_to_week: Dict[str, str]) -> Dict[tuple, float]:
            """Sum confirmed, non-quote order dollars into (week_label, store_id)."""
            if not date_to_week:
                return {}
            lo, hi = min(date_to_week), max(date_to_week)
            sql = f"""
                SELECT DATE(o.date_reqd) AS d, o.store_id, COALESCE(SUM({money_col}), 0)
                FROM orders o
                WHERE DATE(o.date_reqd) BETWEEN ? AND ?
                  AND o.deleted = 0
                  AND LOWER(TRIM(o.status)) = 'confirmed'
                  AND COALESCE(LOWER(TRIM(o.order_type)), '') <> 'quote'
                GROUP BY d, o.store_id
            """
            acc: Dict[tuple, float] = {}
            for d, sid, amt in conn.execute(sql, [lo, hi]).fetchall():
                wk = date_to_week.get(d)
                if wk is None:
                    continue
                acc[(wk, sid)] = acc.get((wk, sid), 0.0) + (amt or 0.0)
            return acc

        cy = _sum_by_week(cy_date_to_week)
        py = _sum_by_week(py_date_to_week)
    finally:
        conn.close()

    # Which (week, store) cells to emit. SSRS lists EVERY company store on every
    # week (even $0), so build the full grid of company stores × all weeks; then
    # a store with no orders that week still shows as $0. Non-scoped mode falls
    # back to only the (week, store) pairs that actually have dollars.
    all_weeks = set(cy_date_to_week.values()) | set(py_date_to_week.values())
    if company_only:
        keys = {(wk, sid) for wk in all_weeks for sid in company_store_ids}
    else:
        keys = set(cy) | set(py)

    # week_start -> areadir -> restaurant_name -> {cy, py, storeNumber}
    from collections import defaultdict
    weeks: Dict[str, Dict[str, Dict[str, Dict]]] = defaultdict(lambda: defaultdict(dict))

    for (week_start, sid) in keys:
        if company_only and sid not in company_store_ids:
            # SSRS scopes this report to company restaurants; skip franchise.
            continue
        code, name, _region = store_meta.get(sid, (None, None, None))
        # store number: prefer numeric store_code
        store_num = None
        if code is not None:
            try:
                store_num = int(str(code).strip())
            except (ValueError, TypeError):
                store_num = None
        areadir = areadir_by_store_number.get(store_num) if store_num is not None else None
        areadir = areadir or "Unassigned"
        rname = name or (f"Store {store_num}" if store_num else f"Store {sid}")
        cell = weeks[week_start][areadir].setdefault(
            rname, {"cy": 0.0, "py": 0.0, "storeNumber": store_num}
        )
        cell["cy"] += cy.get((week_start, sid), 0.0) or 0.0
        cell["py"] += py.get((week_start, sid), 0.0) or 0.0

    def _pct(cy_v: float, py_v: float) -> float:
        if py_v and py_v != 0:
            return round((cy_v - py_v) / py_v * 100, 2)
        return 0.0

    out = []
    for week_start in sorted(weeks.keys()):  # chronological, like the SSRS sheets
        dir_map = weeks[week_start]
        # SSRS counts a period-straddling week once per period it touches.
        mult = week_multiplier.get(week_start, 1)
        rows = []
        wk_cy = wk_py = 0.0
        # Area directors alphabetical; restaurants within by CY desc (matches export)
        for areadir in sorted(dir_map.keys(), key=lambda d: (d == "Unassigned", d.lower())):
            rest_map = dir_map[areadir]
            for rname, cell in sorted(rest_map.items(), key=lambda kv: kv[1]["cy"], reverse=True):
                cy_v = round(cell["cy"] * mult, 2)
                py_v = round(cell["py"] * mult, 2)
                wk_cy += cy_v
                wk_py += py_v
                rows.append({
                    "areaDirName": areadir,
                    "restaurantName": rname,
                    "storeNumber": cell["storeNumber"],
                    "currentYear": cy_v,
                    "priorYear": py_v,
                    "netDifference": round(cy_v - py_v, 2),
                    "percentageChange": _pct(cy_v, py_v),
                })
        wk_cy = round(wk_cy, 2)
        wk_py = round(wk_py, 2)
        out.append({
            "weekStart": week_start,
            "totals": {
                "currentYear": wk_cy,
                "priorYear": wk_py,
                "netDifference": round(wk_cy - wk_py, 2),
                "percentageChange": _pct(wk_cy, wk_py),
            },
            "rows": rows,
        })
    return out


def rpt_catering_over_500(
    year: int, month: int,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: sp_rpt_Catering_Over_500_Month
    Clients who spent over $500 in a given month, with YTD totals.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        month_start = f"{year}-{month:02d}-01"
        if month == 12:
            month_end = f"{year + 1}-01-01"
        else:
            month_end = f"{year}-{month + 1:02d}-01"
        ytd_start = f"{year}-01-01"

        rows = conn.execute("""
            SELECT
                c.client_id,
                c.first_name || ' ' || c.last_name AS client_name,
                c.email_address,
                c.phone_number,
                c.company_name,
                c.on_mailing_list,
                SUM(CASE WHEN o.date_reqd >= ? AND o.date_reqd < ? THEN o.total ELSE 0 END) AS month_total,
                SUM(CASE WHEN o.date_reqd >= ? THEN o.total ELSE 0 END) AS ytd_total,
                COUNT(CASE WHEN o.date_reqd >= ? AND o.date_reqd < ? THEN 1 END) AS month_orders,
                MAX(o.date_reqd) AS last_order_date
            FROM clients_and_leads c
            JOIN orders o ON o.client_id = c.client_id
                AND o.deleted = 0
                AND o.status NOT IN ('Cancelled', 'Void')
            WHERE c.is_deleted = 0
            GROUP BY c.client_id
            HAVING month_total > 500
            ORDER BY month_total DESC
        """, (month_start, month_end, ytd_start, month_start, month_end)).fetchall()
        return [
            {"clientId": r[0], "clientName": r[1], "email": r[2],
             "phone": r[3], "company": r[4], "onMailingList": bool(r[5]),
             "monthTotal": round(r[6], 2), "ytdTotal": round(r[7], 2),
             "monthOrders": r[8], "lastOrderDate": r[9]}
            for r in rows
        ]
    finally:
        conn.close()


def rpt_order_notes(
    start_date: str, end_date: str,
    store_ids: Optional[List[int]] = None,
    db_path: str = MONKEY_DB_PATH,
) -> List[Dict]:
    """
    Replaces: new01_sp_report_order_notes
    Orders with notes populated.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                o.store_id,
                s.store_name,
                DATE(o.date_reqd) AS business_date,
                o.order_id,
                o.client_name,
                o.order_note
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE DATE(o.date_reqd) BETWEEN ? AND ?
              AND o.deleted = 0
              AND o.order_note IS NOT NULL
              AND TRIM(o.order_note) != ''
        """
        params = [start_date, end_date]
        if store_ids:
            ph = ",".join("?" * len(store_ids))
            sql += f" AND o.store_id IN ({ph})"
            params.extend(store_ids)
        sql += " ORDER BY o.date_reqd DESC, o.store_id"

        rows = conn.execute(sql, params).fetchall()
        return [
            {"storeId": r[0], "storeName": r[1], "date": r[2],
             "orderId": r[3], "clientName": r[4], "orderNote": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


# ── Alert Functions ─────────────────────────────────────────────────


def alert_orders_not_accepted(db_path: str = MONKEY_DB_PATH) -> List[Dict]:
    """
    Replaces: new01_sp_alert_OrdersnotAccepted
    Orders due within 24 hours that haven't been accepted.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                o.order_id, o.store_id, s.store_name,
                o.date_reqd, o.client_name, o.total, o.status
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE o.store_accept_time IS NULL
              AND o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
              AND o.date_reqd <= datetime('now', '+24 hours')
              AND o.date_reqd >= datetime('now', '-1 day')
            ORDER BY o.date_reqd
        """).fetchall()
        return [
            {"orderId": r[0], "storeId": r[1], "storeName": r[2],
             "dateRequired": r[3], "clientName": r[4], "total": r[5], "status": r[6]}
            for r in rows
        ]
    finally:
        conn.close()


def alert_orders_not_posted(db_path: str = MONKEY_DB_PATH) -> List[Dict]:
    """
    Replaces: new01_sp_alert_OrdersnotPosted
    Confirmed orders with no POS order ID.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                o.order_id, o.store_id, s.store_name,
                o.date_reqd, o.client_name, o.total,
                o.confirmed_by, o.status
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE (o.pos_order_id IS NULL OR TRIM(o.pos_order_id) = '')
              AND o.confirmed_by IS NOT NULL
              AND o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
              AND o.date_reqd >= datetime('now', '-30 days')
            ORDER BY o.date_reqd DESC
        """).fetchall()
        return [
            {"orderId": r[0], "storeId": r[1], "storeName": r[2],
             "dateRequired": r[3], "clientName": r[4], "total": r[5],
             "confirmedBy": r[6], "status": r[7]}
            for r in rows
        ]
    finally:
        conn.close()


def alert_unconfirmed_orders(db_path: str = MONKEY_DB_PATH) -> List[Dict]:
    """
    Replaces: new01_sp_alert_Unconfirmed_Orders
    Online orders within last 7 days that remain unconfirmed.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT
                o.order_id, o.store_id, s.store_name,
                o.date_reqd, o.client_name, o.total, o.order_type
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE o.confirmed_by IS NULL
              AND o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
              AND o.order_type = 'Online'
              AND o.date_reqd >= datetime('now', '-7 days')
            ORDER BY o.date_reqd
        """).fetchall()
        return [
            {"orderId": r[0], "storeId": r[1], "storeName": r[2],
             "dateRequired": r[3], "clientName": r[4], "total": r[5],
             "orderType": r[6]}
            for r in rows
        ]
    finally:
        conn.close()


def lock_the_day_summary(biz_date: str, store_id: int | None = None,
                         db_path: str = MONKEY_DB_PATH) -> dict:
    """
    Single-date view of orders that block accounting from locking the day.
    Returns summary counts and blocker detail rows for the given date.
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        store_clause = "AND o.store_id = ?" if store_id else ""
        params: list = [biz_date]
        if store_id:
            params.append(store_id)

        rows = conn.execute(f"""
            SELECT
                o.order_id,
                COALESCE(s.store_name, '')                     AS store_name,
                COALESCE(o.client_name, '')                    AS client_name,
                COALESCE(o.total, 0)                           AS total,
                o.order_type,
                o.confirmed_by,
                o.store_accept_time,
                o.pos_order_id,
                o.status,
                o.store_id,
                COALESCE(s.store_email, '')                    AS store_email,
                COALESCE(s.store_manager_email, '')            AS store_manager_email,
                o.order_date,
                o.date_reqd
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.store_id
            WHERE o.deleted = 0
              AND o.status NOT IN ('Cancelled', 'Void')
              AND DATE(o.date_reqd) = DATE(?)
              {store_clause}
            ORDER BY o.order_id
        """, params).fetchall()

        result = {
            "date": biz_date,
            "totalOrders": 0,
            "unconfirmed": 0,
            "notAccepted": 0,
            "notPosted": 0,
            "blockers": [],
        }

        for r in rows:
            order_id, store_name, client_name, total, \
                order_type, confirmed_by, store_accept_time, pos_order_id, status, \
                store_id_val, store_email, store_mgr_email, \
                order_date, date_reqd = r

            result["totalOrders"] += 1

            issues = []
            if confirmed_by is None and (order_type or "").lower() == "online":
                issues.append("Unconfirmed")
                result["unconfirmed"] += 1
            if store_accept_time is None:
                issues.append("Not Accepted")
                result["notAccepted"] += 1
            if confirmed_by is not None and (pos_order_id is None or (pos_order_id or "").strip() == ""):
                issues.append("Not Posted")
                result["notPosted"] += 1

            if issues:
                result["blockers"].append({
                    "orderId": order_id,
                    "storeId": store_id_val,
                    "storeName": store_name,
                    "clientName": client_name,
                    "total": total,
                    "issue": ", ".join(issues),
                    "storeEmail": store_email,
                    "storeManagerEmail": store_mgr_email,
                    "orderDate": order_date,
                    "dateRequired": date_reqd,
                })

        result["ready"] = (result["unconfirmed"] == 0
                           and result["notAccepted"] == 0
                           and result["notPosted"] == 0)

        return result
    finally:
        conn.close()


# ── CSV Import ──────────────────────────────────────────────────────

# Column mappings for headerless CSVs from SSMS
# Order must match the SQL Server column ordinal position

STORES_COLUMNS = [
    'UniqueID', 'store_id', 'region_id', 'region_name', 'store_name', 'store_code',
    'store_manager', 'store_manager_email', 'phone', 'fax', 'store_email',
    'address_1', 'address_2', 'city', 'state', 'zip',
    'delivery_service', 'delivery_fee_taxable', 'gratuity_fee_taxable',
    'setup_fee_taxable', 'center_fee_taxable',
    'current_delivery_fee_tax_rate', 'current_gratuity_fee_tax_rate',
    'current_setup_fee_tax_rate', 'current_center_fee_tax_rate',
    'add_date', 'rating', 'latitude', 'longitude', 'note',
    'accepts_online_orders', 'pos_ip', 'pos_ip_verified', 'store_key',
    'pos_sync_start_date', 'status',
]

ACCOUNTS_COLUMNS = [
    'account_id', 'house_account', 'company_name', 'accounts_payable_contact',
    'email_address', 'phone_number', 'fax_number', 'address_1', 'address_2',
    'city', 'zip_code', 'state', 'discount_percentage',
    'discount_percentage_dollar_threshold', 'tax_exempt', 'requires_po',
    'payment_terms', 'overdue_balance_limit', 'marketing_group', 'status',
    'is_deleted', 'billing_external_note', 'billing_internal_note',
    'tax_exempt_number',
]

DRIVERS_COLUMNS = [
    'driver_id', 'is_store_driver', 'region_id', 'first_name', 'last_name',
    'phone', 'employee_number', 'status', 'pos_operator_id',
    'pos_operator_job_code', 'store_id', 'is_store_default_driver',
    'delivery_partner',
]

PAYMENTS_COLUMNS = [
    'UniqueID', 'payment_id', 'process_id', 'order_id', 'account_id',
    'client_id', 'amount', 'payment_date', 'method', 'check_no', 'locked',
]

REFUNDS_COLUMNS = [
    'UniqueID', 'refund_id', 'process_id', 'account_id', 'account_name',
    'order_id', 'amount', 'tax_refund', 'total', 'method',
    'refund_date', 'note', 'refund_by', 'check_no', 'locked',
]

ITEMS_COLUMNS = [
    'UniqueID', 'item_id', 'item_class', 'item_category',
    'item_group_abbreviation', 'item_group', 'item_name', 'department',
    'unit', 'rounding', 'round_to', 'is_production_item',
    'serving_description', 'detail_description', 'image_url', 'price',
    'min_order_qty', 'taxable', 'sell_on_web', 'requires_label',
    'is_tray', 'is_box', 'is_flat', 'is_phantom', 'status',
]


def _clean_val(val):
    """Convert NULL strings and empty to None."""
    if val is None or val == 'NULL' or val == '':
        return None
    return val.strip() if isinstance(val, str) else val


def _safe_float(val):
    try:
        v = _clean_val(val)
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    try:
        v = _clean_val(val)
        if v is None:
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def import_csv(table: str, csv_path: str, columns: list, db_path: str = MONKEY_DB_PATH):
    """Import a headerless CSV into the specified table using the column mapping."""
    import csv as csv_mod

    initialize_db(db_path)
    conn = get_connection(db_path)

    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv_mod.reader(f)
        rows = []
        for raw_row in reader:
            if len(raw_row) < len(columns):
                continue
            row_dict = {columns[i]: _clean_val(raw_row[i]) for i in range(len(columns))}
            rows.append(row_dict)

    # Get the columns that exist in our schema
    cursor = conn.execute(f"PRAGMA table_info({table})")
    db_cols = {r[1] for r in cursor.fetchall()}
    insert_cols = [c for c in columns if c in db_cols]

    if not insert_cols:
        logger.warning("No matching columns for table %s", table)
        conn.close()
        return 0

    ph = ",".join("?" * len(insert_cols))
    col_names = ",".join(insert_cols)

    conn.execute(f"DELETE FROM {table}")
    count = 0
    for row_dict in rows:
        vals = [row_dict.get(c) for c in insert_cols]
        try:
            conn.execute(f"INSERT INTO {table} ({col_names}) VALUES ({ph})", vals)
            count += 1
        except Exception as e:
            if count == 0:
                logger.warning("Insert error on first row of %s: %s", table, e)

    conn.commit()
    conn.close()
    logger.info("Imported %d rows into monkey.%s", count, table)
    return count
