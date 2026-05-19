"""
aloha_daypart_db.py — Aloha sales-by-daypart enrichment for the IPA export.

Lives in the same SQLite file as aloha_sales_data (aloha_sales.db) but in its
own table: aloha_sales_daypart, keyed by (business_date, location, daypart_id).

Source: dev_aloha.dbo.tbl_SalesDataByDayPart — see
backend/sql_server_sps/export_aloha_daypart_for_ipa.sql for the SSMS query that
produces the CSV this module imports.

This is a one-time historic backfill. Aloha data is the past — once imported,
the rows don't change. The app stays Toast-API-only for ongoing data.
"""

import os
import sqlite3
from typing import List, Optional, Tuple

# Same file as aloha_sales.db so it travels in one push to Cloudways
from aloha_sales_db import ALOHA_SALES_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS aloha_sales_daypart (
    business_date            TEXT    NOT NULL,   -- YYYY-MM-DD
    location                 TEXT    NOT NULL,   -- Aloha RestaurantID as text
    daypart_id               INTEGER NOT NULL,   -- 1=Lunch, 2=Off Hours, 3=Dinner, 0=Breakfast (per source)
    daypart                  TEXT    NOT NULL,   -- 'Breakfast' | 'Lunch' | 'Dinner' | 'Off Hours'

    net_sales                REAL    NOT NULL DEFAULT 0,
    gross_sales              REAL    NOT NULL DEFAULT 0,

    catering_net_sales       REAL    NOT NULL DEFAULT 0,
    ezcater_net_sales        REAL    NOT NULL DEFAULT 0,

    dinein_net_sales         REAL    NOT NULL DEFAULT 0,
    olo_dinein_net_sales     REAL    NOT NULL DEFAULT 0,
    togo_net_sales           REAL    NOT NULL DEFAULT 0,
    drivethru_net_sales      REAL    NOT NULL DEFAULT 0,
    olo_drivethru_net_sales  REAL    NOT NULL DEFAULT 0,
    callin_net_sales         REAL    NOT NULL DEFAULT 0,
    olo_callin_net_sales     REAL    NOT NULL DEFAULT 0,

    olo_net_sales            REAL    NOT NULL DEFAULT 0,
    curbside_net_sales       REAL    NOT NULL DEFAULT 0,
    curbside_olo_net_sales   REAL    NOT NULL DEFAULT 0,
    nwk_delivery_net_sales   REAL    NOT NULL DEFAULT 0,
    dispatch_olo_net_sales   REAL    NOT NULL DEFAULT 0,

    doordash_net_sales       REAL    NOT NULL DEFAULT 0,
    ubereats_net_sales       REAL    NOT NULL DEFAULT 0,
    grubhub_net_sales        REAL    NOT NULL DEFAULT 0,
    postmates_net_sales      REAL    NOT NULL DEFAULT 0,
    bitesquad_net_sales      REAL    NOT NULL DEFAULT 0,
    favor_net_sales          REAL    NOT NULL DEFAULT 0,
    foodsby_net_sales        REAL    NOT NULL DEFAULT 0,
    google_net_sales         REAL    NOT NULL DEFAULT 0,
    deliverclub_net_sales    REAL    NOT NULL DEFAULT 0,
    rails_olo_net_sales      REAL    NOT NULL DEFAULT 0,
    waitr_net_sales          REAL    NOT NULL DEFAULT 0,
    delivery_net_sales       REAL    NOT NULL DEFAULT 0,

    gng_net_sales            REAL    NOT NULL DEFAULT 0,

    check_count              INTEGER NOT NULL DEFAULT 0,
    checks_minus_catering    INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (business_date, location, daypart_id)
);

CREATE INDEX IF NOT EXISTS idx_aloha_dp_loc_date
    ON aloha_sales_daypart(location, business_date);

CREATE INDEX IF NOT EXISTS idx_aloha_dp_date
    ON aloha_sales_daypart(business_date);
"""


# Column order matches export_aloha_daypart_for_ipa.sql exactly.
# The CSV from SSMS has no header — we depend on this order.
CSV_COLUMNS: List[str] = [
    "business_date", "location", "daypart_id", "daypart",
    "net_sales", "gross_sales",
    "catering_net_sales", "ezcater_net_sales",
    "dinein_net_sales", "olo_dinein_net_sales",
    "togo_net_sales",
    "drivethru_net_sales", "olo_drivethru_net_sales",
    "callin_net_sales", "olo_callin_net_sales",
    "olo_net_sales",
    "curbside_net_sales", "curbside_olo_net_sales",
    "nwk_delivery_net_sales", "dispatch_olo_net_sales",
    "doordash_net_sales", "ubereats_net_sales", "grubhub_net_sales",
    "postmates_net_sales", "bitesquad_net_sales",
    "favor_net_sales", "foodsby_net_sales", "google_net_sales",
    "deliverclub_net_sales", "rails_olo_net_sales", "waitr_net_sales",
    "delivery_net_sales",
    "gng_net_sales",
    "check_count", "checks_minus_catering",
]


def get_connection(db_path: str = ALOHA_SALES_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize(db_path: str = ALOHA_SALES_DB_PATH) -> None:
    """Create the aloha_sales_daypart table + indexes if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)


def has_data(db_path: str = ALOHA_SALES_DB_PATH) -> bool:
    """Return True if the table exists and has at least one row."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM aloha_sales_daypart LIMIT 1").fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def get_daypart_rows(location: str, biz_date: str,
                     db_path: str = ALOHA_SALES_DB_PATH) -> List[dict]:
    """
    Return all daypart rows for (location, business_date) as a list of dicts.
    Empty list if no data (caller should fall back to the day-level aloha_sales_data).
    """
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM aloha_sales_daypart"
                " WHERE business_date = ? AND location = ?"
                " ORDER BY daypart_id",
                (biz_date, str(location)),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
