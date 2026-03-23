"""
punchh_db.py — SQLite layer for Punchh loyalty data.

Replaces the SQL Server Punchh database.
Tables: checkin, coupon, guest_data, redemption
"""

import os
import sqlite3
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PUNCHH_DB_PATH = os.path.join(_DB_DIR, "punchh.db")

# Dollar value per loyalty point
POINT_VALUE = 0.006

# ── Schema ──────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkin (
    checkin_id          BIGINT PRIMARY KEY,
    user_id             BIGINT,
    store_number        TEXT,
    points_earned       REAL,
    points_spent        INT,
    receipt_amount      REAL,
    date_of_business    TEXT,
    channel             TEXT,
    location_name       TEXT,
    receipt_date_ts     TEXT,
    receipt_date_ts_utc TEXT,
    checkin_type        TEXT,
    status              TEXT,
    state               TEXT
);

CREATE TABLE IF NOT EXISTS coupon (
    coupon_id           BIGINT PRIMARY KEY,
    campaign            INT,
    user_id             TEXT,
    store_number        TEXT,
    campaign_name       TEXT,
    coupon_type         TEXT,
    status              TEXT,
    discount            REAL,
    receipt_payable     REAL,
    subtotal_amount     REAL,
    date_of_business    TEXT,
    processed_time      TEXT,
    coupon_code         TEXT,
    location_name       TEXT
);

CREATE TABLE IF NOT EXISTS guest_data (
    user_id                             BIGINT PRIMARY KEY,
    first_name                          TEXT,
    last_name                           TEXT,
    email                               TEXT,
    phone                               TEXT,
    total_points                        TEXT,
    total_checkins                      INT,
    total_spend                         REAL,
    membership_level                    TEXT,
    favorite_location_name              TEXT,
    favorite_location_store_number      INT,
    signup_channel                      TEXT,
    guest_signup_date                   TEXT,
    first_checkin                       TEXT,
    last_checkin                        TEXT,
    user_status                         TEXT,
    marketing_email_subscription_status TEXT,
    birthday                            TEXT,
    zip_code                            TEXT,
    gender                              TEXT
);

CREATE TABLE IF NOT EXISTS redemption (
    redemption_id       INT PRIMARY KEY,
    user_id             INT,
    date                TEXT,
    store_number        TEXT,
    amount              REAL,
    redeemable_name     TEXT,
    redeemable_id       TEXT,
    redemption_type     TEXT,
    channel             TEXT,
    date_of_business    TEXT,
    discount_amount     REAL,
    receipt_payable     REAL,
    receipt_subtotal    REAL,
    location_name       TEXT,
    first_name          TEXT,
    last_name           TEXT,
    user_email          TEXT
);

CREATE INDEX IF NOT EXISTS idx_checkin_store ON checkin(store_number);
CREATE INDEX IF NOT EXISTS idx_checkin_dob ON checkin(date_of_business);
CREATE INDEX IF NOT EXISTS idx_checkin_user ON checkin(user_id);

CREATE INDEX IF NOT EXISTS idx_coupon_store ON coupon(store_number);
CREATE INDEX IF NOT EXISTS idx_coupon_dob ON coupon(date_of_business);
CREATE INDEX IF NOT EXISTS idx_coupon_user ON coupon(user_id);

CREATE INDEX IF NOT EXISTS idx_guest_user ON guest_data(user_id);

CREATE INDEX IF NOT EXISTS idx_redemption_store ON redemption(store_number);
CREATE INDEX IF NOT EXISTS idx_redemption_dob ON redemption(date_of_business);
CREATE INDEX IF NOT EXISTS idx_redemption_user ON redemption(user_id);
"""


def get_connection(db_path: str = PUNCHH_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_initialized = False


def initialize_db(db_path: str = PUNCHH_DB_PATH):
    global _initialized
    if _initialized:
        return
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA_SQL)
    conn.close()
    _initialized = True


def db_available(db_path: str = PUNCHH_DB_PATH) -> bool:
    try:
        if not os.path.exists(db_path):
            return False
        conn = get_connection(db_path)
        row = conn.execute("SELECT COUNT(*) FROM checkin").fetchone()
        conn.close()
        return row[0] > 0
    except Exception:
        return False


def get_stats(db_path: str = PUNCHH_DB_PATH) -> dict:
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        stats = {}
        for table in ['checkin', 'coupon', 'guest_data', 'redemption']:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[table] = row[0]

        # Date ranges
        for table, col in [('checkin', 'date_of_business'),
                           ('coupon', 'date_of_business'),
                           ('redemption', 'date_of_business')]:
            dr = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {table}").fetchone()
            stats[f'{table}_range'] = {'min': dr[0], 'max': dr[1]} if dr and dr[0] else {}

        stats['db_size_mb'] = round(os.path.getsize(db_path) / 1024 / 1024, 1)
        return stats
    finally:
        conn.close()


# ── Report Functions (matching Punchh SPs) ─────────────────────────


def _store_filter(sql: str, params: list, store_numbers: Optional[List[str]], col: str = "store_number") -> str:
    """Append store number filter to SQL if provided."""
    if store_numbers:
        ph = ",".join("?" * len(store_numbers))
        sql += f" AND {col} IN ({ph})"
        params.extend(store_numbers)
    return sql


def rpt_daily_point_balance(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Daily points earned vs spent by store, with net points and dollar value.
    Replaces: sp_rpt_Punchh_DailyPointBalance
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                store_number,
                location_name,
                date_of_business,
                COALESCE(SUM(points_earned), 0) AS total_earned,
                COALESCE(SUM(points_spent), 0) AS total_spent
            FROM checkin
            WHERE date_of_business BETWEEN ? AND ?
        """
        params: list = [start_date, end_date]
        sql = _store_filter(sql, params, store_numbers)
        sql += " GROUP BY store_number, location_name, date_of_business"
        sql += " ORDER BY store_number, date_of_business"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "storeNumber": r[0],
                "locationName": r[1],
                "dateOfBusiness": r[2],
                "pointsEarned": round(r[3], 2),
                "pointsSpent": round(r[4], 2),
                "netPoints": round(r[3] - r[4], 2),
                "earnedDollarValue": round(r[3] * POINT_VALUE, 2),
                "spentDollarValue": round(r[4] * POINT_VALUE, 2),
                "netDollarValue": round((r[3] - r[4]) * POINT_VALUE, 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def rpt_coupon_dollars_counts(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Daily coupon redemption dollars and counts by store.
    Excludes items that also appear in redemption as 'FREE %'.
    Replaces: sp_rpt_Punchh_CouponDollarsCounts
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                c.store_number,
                c.location_name,
                c.date_of_business,
                COUNT(*) AS coupon_count,
                COALESCE(SUM(c.discount), 0) AS total_discount
            FROM coupon c
            WHERE c.date_of_business BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM redemption r
                  WHERE r.user_id = CAST(c.user_id AS INTEGER)
                    AND r.date_of_business = c.date_of_business
                    AND r.store_number = c.store_number
                    AND r.redeemable_name LIKE 'FREE %'
              )
        """
        params: list = [start_date, end_date]
        sql = _store_filter(sql, params, store_numbers, col="c.store_number")
        sql += " GROUP BY c.store_number, c.location_name, c.date_of_business"
        sql += " ORDER BY c.store_number, c.date_of_business"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "storeNumber": r[0],
                "locationName": r[1],
                "dateOfBusiness": r[2],
                "couponCount": r[3],
                "totalDiscount": round(r[4], 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def rpt_coupon_summary(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Coupon redemption dollars and counts by store, rolled up across date range.
    Excludes items that also appear in redemption as 'FREE %'.
    Replaces: sp_rpt_Punchh_CouponSummary
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                c.store_number,
                c.location_name,
                COUNT(*) AS coupon_count,
                COALESCE(SUM(c.discount), 0) AS total_discount
            FROM coupon c
            WHERE c.date_of_business BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM redemption r
                  WHERE r.user_id = CAST(c.user_id AS INTEGER)
                    AND r.date_of_business = c.date_of_business
                    AND r.store_number = c.store_number
                    AND r.redeemable_name LIKE 'FREE %'
              )
        """
        params: list = [start_date, end_date]
        sql = _store_filter(sql, params, store_numbers, col="c.store_number")
        sql += " GROUP BY c.store_number, c.location_name"
        sql += " ORDER BY c.store_number"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "storeNumber": r[0],
                "locationName": r[1],
                "couponCount": r[2],
                "totalDiscount": round(r[3], 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def rpt_redeemable_dollars_counts(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Daily free-item reward redemptions (WHERE redeemable_name LIKE 'FREE %').
    Replaces: sp_rpt_Punchh_RedeemableDollarsCounts
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                store_number,
                location_name,
                date_of_business,
                COUNT(*) AS redemption_count,
                COALESCE(SUM(discount_amount), 0) AS total_discount
            FROM redemption
            WHERE date_of_business BETWEEN ? AND ?
              AND redeemable_name LIKE 'FREE %'
        """
        params: list = [start_date, end_date]
        sql = _store_filter(sql, params, store_numbers)
        sql += " GROUP BY store_number, location_name, date_of_business"
        sql += " ORDER BY store_number, date_of_business"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "storeNumber": r[0],
                "locationName": r[1],
                "dateOfBusiness": r[2],
                "redemptionCount": r[3],
                "totalDiscount": round(r[4], 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def rpt_redeemable_summary(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Free-item reward redemptions rolled up across date range by store.
    Replaces: sp_rpt_Punchh_RedeemableSummary
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                store_number,
                location_name,
                COUNT(*) AS redemption_count,
                COALESCE(SUM(discount_amount), 0) AS total_discount
            FROM redemption
            WHERE date_of_business BETWEEN ? AND ?
              AND redeemable_name LIKE 'FREE %'
        """
        params: list = [start_date, end_date]
        sql = _store_filter(sql, params, store_numbers)
        sql += " GROUP BY store_number, location_name"
        sql += " ORDER BY store_number"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "storeNumber": r[0],
                "locationName": r[1],
                "redemptionCount": r[2],
                "totalDiscount": round(r[3], 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def rpt_summary_rollup(
    start_date: str, end_date: str,
    store_numbers: Optional[List[str]] = None,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Comprehensive loyalty summary by store: points earned/spent, net points,
    dollar values, check-in counts, redemption counts/dollars, coupon counts/dollars.
    Replaces: sp_rpt_Punchh_SummaryRollup
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        # Points and check-ins from checkin table
        checkin_sql = """
            SELECT
                store_number,
                location_name,
                COALESCE(SUM(points_earned), 0) AS points_earned,
                COALESCE(SUM(points_spent), 0) AS points_spent,
                COUNT(*) AS checkin_count
            FROM checkin
            WHERE date_of_business BETWEEN ? AND ?
        """
        checkin_params: list = [start_date, end_date]
        checkin_sql = _store_filter(checkin_sql, checkin_params, store_numbers)
        checkin_sql += " GROUP BY store_number, location_name"

        checkin_rows = conn.execute(checkin_sql, checkin_params).fetchall()
        store_data: Dict[str, dict] = {}
        for r in checkin_rows:
            store_data[r[0]] = {
                "storeNumber": r[0],
                "locationName": r[1],
                "pointsEarned": round(r[2], 2),
                "pointsSpent": round(r[3], 2),
                "netPoints": round(r[2] - r[3], 2),
                "earnedDollarValue": round(r[2] * POINT_VALUE, 2),
                "spentDollarValue": round(r[3] * POINT_VALUE, 2),
                "netDollarValue": round((r[2] - r[3]) * POINT_VALUE, 2),
                "checkinCount": r[4],
                "redemptionCount": 0,
                "redemptionDiscount": 0.0,
                "couponCount": 0,
                "couponDiscount": 0.0,
            }

        # Redemptions (FREE % items)
        redeem_sql = """
            SELECT
                store_number,
                location_name,
                COUNT(*) AS cnt,
                COALESCE(SUM(discount_amount), 0) AS total_discount
            FROM redemption
            WHERE date_of_business BETWEEN ? AND ?
              AND redeemable_name LIKE 'FREE %'
        """
        redeem_params: list = [start_date, end_date]
        redeem_sql = _store_filter(redeem_sql, redeem_params, store_numbers)
        redeem_sql += " GROUP BY store_number, location_name"

        for r in conn.execute(redeem_sql, redeem_params).fetchall():
            sn = r[0]
            if sn not in store_data:
                store_data[sn] = {
                    "storeNumber": sn, "locationName": r[1],
                    "pointsEarned": 0, "pointsSpent": 0, "netPoints": 0,
                    "earnedDollarValue": 0, "spentDollarValue": 0, "netDollarValue": 0,
                    "checkinCount": 0, "redemptionCount": 0, "redemptionDiscount": 0.0,
                    "couponCount": 0, "couponDiscount": 0.0,
                }
            store_data[sn]["redemptionCount"] = r[2]
            store_data[sn]["redemptionDiscount"] = round(r[3], 2)

        # Coupons (excluding FREE % overlap)
        coupon_sql = """
            SELECT
                c.store_number,
                c.location_name,
                COUNT(*) AS cnt,
                COALESCE(SUM(c.discount), 0) AS total_discount
            FROM coupon c
            WHERE c.date_of_business BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM redemption r
                  WHERE r.user_id = CAST(c.user_id AS INTEGER)
                    AND r.date_of_business = c.date_of_business
                    AND r.store_number = c.store_number
                    AND r.redeemable_name LIKE 'FREE %'
              )
        """
        coupon_params: list = [start_date, end_date]
        coupon_sql = _store_filter(coupon_sql, coupon_params, store_numbers, col="c.store_number")
        coupon_sql += " GROUP BY c.store_number, c.location_name"

        for r in conn.execute(coupon_sql, coupon_params).fetchall():
            sn = r[0]
            if sn not in store_data:
                store_data[sn] = {
                    "storeNumber": sn, "locationName": r[1],
                    "pointsEarned": 0, "pointsSpent": 0, "netPoints": 0,
                    "earnedDollarValue": 0, "spentDollarValue": 0, "netDollarValue": 0,
                    "checkinCount": 0, "redemptionCount": 0, "redemptionDiscount": 0.0,
                    "couponCount": 0, "couponDiscount": 0.0,
                }
            store_data[sn]["couponCount"] = r[2]
            store_data[sn]["couponDiscount"] = round(r[3], 2)

        return sorted(store_data.values(), key=lambda x: x["storeNumber"])
    finally:
        conn.close()


def rpt_google_ads_export(
    start_date: str, end_date: str,
    db_path: str = PUNCHH_DB_PATH,
) -> List[Dict]:
    """
    Export loyalty data for Google Ads attribution: email, phone, name,
    conversion time, conversion value from guest_data joined to checkin.
    Replaces: sp_rpt_Punchh_GoogleAdsExport
    """
    initialize_db(db_path)
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT
                g.email,
                g.phone,
                g.first_name,
                g.last_name,
                c.receipt_date_ts AS conversion_time,
                c.receipt_amount AS conversion_value
            FROM checkin c
            JOIN guest_data g ON c.user_id = g.user_id
            WHERE c.date_of_business BETWEEN ? AND ?
              AND g.email IS NOT NULL
              AND TRIM(g.email) != ''
            ORDER BY c.date_of_business, c.receipt_date_ts
        """
        rows = conn.execute(sql, [start_date, end_date]).fetchall()
        return [
            {
                "email": r[0],
                "phone": r[1],
                "firstName": r[2],
                "lastName": r[3],
                "conversionTime": r[4],
                "conversionValue": round(r[5], 2) if r[5] else 0.0,
            }
            for r in rows
        ]
    finally:
        conn.close()


# ── CSV Import ──────────────────────────────────────────────────────

# Column mappings for headerless CSVs exported from SSMS
# Order must match the SQL Server column ordinal position

CHECKIN_COLUMNS = [
    'checkin_id', 'user_id', 'approved', 'bar_code', 'count_as_visit',
    'expired_at', 'external_uid', 'first_punchh_at_business',
    'first_punchh_at_location', 'checkin_type', 'ip_address', 'latitude',
    'location', 'store_number', 'longitude', 'points_earned', 'points_spent',
    'qr_decoded', 'receipt_amount', 'receipt_date', 'receipt_date_ts',
    'receipt_date_ts_utc', 'date_of_business', 'receipt_number', 'receipt_url',
    'status', 'channel', 'gift_reason', 'user_agent', 'checkin_scan_time',
    'checkin_scan_time_ts', 'state', 'location_name', 'scheduled_expiry_on',
    'gifted_for_id', 'gifted_for_type',
]

COUPON_COLUMNS = [
    'campaign', 'user_id', 'location', 'location_name', 'store_number',
    'campaign_name', 'status', 'coupon_type', 'coupon_id',
    'redemption_receipt_number', 'discount', 'receipt_payable',
    'subtotal_amount', 'processed_at', 'date_of_business', 'processed_time',
    'processed_at_utc', 'coupon_code',
]

GUEST_DATA_COLUMNS = [
    'user_id', 'first_name', 'last_name', 'email', 'secondary_email',
    'external_source_id', 'external_source', 'membership_level', 'device_type',
    'total_points', 'gender', 'first_checkin', 'last_checkin', 'total_checkins',
    'total_spend', 'marketing_email_subscription_status',
    'marketing_pn_subscription_status', 'sms_subscription_status', 'birthday',
    'favorite_location_id', 'guest_signup_date', 'phone', 'address', 'zip_code',
    'loyalty_points_earned', 'total_banked_rewards',
    'total_card_based_redemptions', 'push_notification_token_available',
    'user_status', 'email_confirmed_at', 'guest_type', 'signup_channel',
    'anniversary', 'created_at', 'updated_at', 'favorite_location_name',
    'favorite_location_store_number', 'date_of_last_earned_points',
    'referral_code',
]

REDEMPTION_COLUMNS = [
    'user_id', 'redemption_id', 'date', 'location_id', 'store_number',
    'redemption_code_number', 'amount', 'status', 'latitude', 'longitude',
    'gps_accuracy', 'ip_address', 'user_agent', 'redeemable_id',
    'redeemable_name', 'reward_id', 'expiring_on', 'redemption_code_status',
    'redemption_receipt_number', 'receipt_date_time', 'receipt_date_time_utc',
    'employee_id', 'receipt_payable', 'receipt_subtotal',
    'additional_properties', 'location_name', 'redemption_type', 'channel',
    'date_of_business', 'processed_time', 'first_name', 'last_name',
    'points_amount_requested', 'reward_origin', 'user_email', 'discount_amount',
]


def _clean_val(val):
    """Convert NULL strings and empty to None."""
    if val is None or val == 'NULL' or val == '':
        return None
    return val.strip() if isinstance(val, str) else val


def import_csv(table: str, csv_path: str, columns: list, db_path: str = PUNCHH_DB_PATH):
    """Import a headerless CSV into the specified table using the column mapping."""
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
    logger.info("Imported %d rows into punchh.%s", count, table)
    return count
