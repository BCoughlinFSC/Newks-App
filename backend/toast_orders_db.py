"""
toast_orders_db.py  —  SQLite persistence layer for cached Toast Orders API data.

Schema
------
  orders(
    id                INTEGER PK AUTOINCREMENT,
    order_guid        TEXT NOT NULL,
    restaurant_guid   TEXT NOT NULL,
    business_date     TEXT NOT NULL,          -- YYYYMMDD
    order_json        TEXT NOT NULL,          -- Full JSON blob
    created_at        TEXT DEFAULT now
  )
  UNIQUE INDEX on (restaurant_guid, business_date, order_guid)

  import_status(
    id                INTEGER PK AUTOINCREMENT,
    restaurant_guid   TEXT NOT NULL,
    business_date     TEXT NOT NULL,          -- YYYYMMDD
    status            TEXT DEFAULT 'in_progress',  -- in_progress | completed | failed
    order_count       INTEGER DEFAULT 0,
    started_at        TEXT DEFAULT now,
    completed_at      TEXT,
    error_message     TEXT
  )
  UNIQUE INDEX on (restaurant_guid, business_date)

Usage
-----
  from toast_orders_db import initialize_db, store_orders, is_date_cached, get_cached_orders
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

ORDERS_DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "toast_orders", "orders.db"
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_guid       TEXT    NOT NULL,
    restaurant_guid  TEXT    NOT NULL,
    business_date    TEXT    NOT NULL,
    order_json       TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_unique
    ON orders(restaurant_guid, business_date, order_guid);

CREATE INDEX IF NOT EXISTS idx_orders_restaurant_date
    ON orders(restaurant_guid, business_date);

CREATE INDEX IF NOT EXISTS idx_orders_date
    ON orders(business_date);

CREATE TABLE IF NOT EXISTS import_status (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_guid  TEXT    NOT NULL,
    business_date    TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'in_progress',
    order_count      INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    error_message    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_import_status_unique
    ON import_status(restaurant_guid, business_date);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Connection + Schema
# ──────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str = ORDERS_DB_PATH) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection (thread-safe for read-heavy workloads)."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
    return conn


def initialize_db(db_path: str = ORDERS_DB_PATH) -> None:
    """Create the database file and schema if they don't already exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_CREATE_SQL)


def db_available(db_path: str = ORDERS_DB_PATH) -> bool:
    """Return True if the database file exists."""
    return os.path.exists(db_path)


# ──────────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────────

def mark_import_started(
    restaurant_guid: str,
    business_date: str,
    db_path: str = ORDERS_DB_PATH,
) -> None:
    """Mark a (restaurant, date) as import in-progress. Overwrites any existing status."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO import_status (restaurant_guid, business_date, status, started_at)"
            " VALUES (?, ?, 'in_progress', datetime('now'))"
            " ON CONFLICT(restaurant_guid, business_date)"
            " DO UPDATE SET status='in_progress', started_at=datetime('now'),"
            "   completed_at=NULL, error_message=NULL, order_count=0",
            (restaurant_guid, business_date),
        )


def store_orders(
    restaurant_guid: str,
    business_date: str,
    orders: List[Dict],
    db_path: str = ORDERS_DB_PATH,
) -> int:
    """
    Atomically replace all orders for (restaurant, date) and mark import completed.
    Returns the number of orders stored.
    """
    initialize_db(db_path)

    records = []
    for order in orders:
        guid = order.get("guid") or order.get("orderGuid") or ""
        if not guid:
            continue
        records.append((
            guid,
            restaurant_guid,
            business_date,
            json.dumps(order, separators=(",", ":")),
        ))

    with get_connection(db_path) as conn:
        # Delete old orders for this (restaurant, date)
        conn.execute(
            "DELETE FROM orders WHERE restaurant_guid = ? AND business_date = ?",
            (restaurant_guid, business_date),
        )
        # Insert new orders
        if records:
            conn.executemany(
                "INSERT INTO orders (order_guid, restaurant_guid, business_date, order_json)"
                " VALUES (?, ?, ?, ?)",
                records,
            )
        # Update import status
        conn.execute(
            "INSERT INTO import_status (restaurant_guid, business_date, status, order_count, completed_at)"
            " VALUES (?, ?, 'completed', ?, datetime('now'))"
            " ON CONFLICT(restaurant_guid, business_date)"
            " DO UPDATE SET status='completed', order_count=?, completed_at=datetime('now'),"
            "   error_message=NULL",
            (restaurant_guid, business_date, len(records), len(records)),
        )

    return len(records)


def mark_import_failed(
    restaurant_guid: str,
    business_date: str,
    error_msg: str,
    db_path: str = ORDERS_DB_PATH,
) -> None:
    """Mark a (restaurant, date) import as failed."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO import_status (restaurant_guid, business_date, status, error_message, completed_at)"
            " VALUES (?, ?, 'failed', ?, datetime('now'))"
            " ON CONFLICT(restaurant_guid, business_date)"
            " DO UPDATE SET status='failed', error_message=?, completed_at=datetime('now')",
            (restaurant_guid, business_date, error_msg, error_msg),
        )


def delete_date(
    restaurant_guid: str,
    business_date: str,
    db_path: str = ORDERS_DB_PATH,
) -> int:
    """Delete orders + import status for one (restaurant, date). Returns rows deleted."""
    if not db_available(db_path):
        return 0
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM orders WHERE restaurant_guid = ? AND business_date = ?",
            (restaurant_guid, business_date),
        )
        conn.execute(
            "DELETE FROM import_status WHERE restaurant_guid = ? AND business_date = ?",
            (restaurant_guid, business_date),
        )
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────────

def is_date_cached(
    restaurant_guid: str,
    business_date: str,
    db_path: str = ORDERS_DB_PATH,
) -> bool:
    """True only if import_status row exists with status='completed'."""
    if not db_available(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM import_status"
                " WHERE restaurant_guid = ? AND business_date = ? AND status = 'completed'"
                " LIMIT 1",
                (restaurant_guid, business_date),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def get_cached_orders(
    restaurant_guid: str,
    business_date: str,
    db_path: str = ORDERS_DB_PATH,
) -> List[Dict]:
    """Return parsed order dicts for a completed (restaurant, date). Returns [] if not cached."""
    if not db_available(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT order_json FROM orders"
                " WHERE restaurant_guid = ? AND business_date = ?",
                (restaurant_guid, business_date),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception:
        return []


def get_cached_dates(
    restaurant_guid: Optional[str] = None,
    db_path: str = ORDERS_DB_PATH,
) -> List[Dict]:
    """Return list of completed imports: {restaurant_guid, business_date, order_count, completed_at}."""
    if not db_available(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            if restaurant_guid:
                rows = conn.execute(
                    "SELECT restaurant_guid, business_date, order_count, completed_at"
                    " FROM import_status WHERE status = 'completed' AND restaurant_guid = ?"
                    " ORDER BY business_date DESC",
                    (restaurant_guid,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT restaurant_guid, business_date, order_count, completed_at"
                    " FROM import_status WHERE status = 'completed'"
                    " ORDER BY business_date DESC",
                ).fetchall()
        return [
            {"restaurant_guid": r[0], "business_date": r[1], "order_count": r[2], "completed_at": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def get_failed_imports(db_path: str = ORDERS_DB_PATH) -> List[Dict]:
    """Return all import_status rows with status='failed'."""
    if not db_available(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT restaurant_guid, business_date, error_message, completed_at"
                " FROM import_status WHERE status = 'failed'"
                " ORDER BY completed_at DESC",
            ).fetchall()
        return [
            {"restaurant_guid": r[0], "business_date": r[1], "error_message": r[2], "completed_at": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def get_stats(db_path: str = ORDERS_DB_PATH) -> Dict[str, Any]:
    """
    Summary: total_orders, total_restaurants, min_date, max_date, dates_cached,
             db_size_mb, last_import_at, failed_count.
    """
    empty: Dict[str, Any] = {
        "total_orders": 0,
        "total_restaurants": 0,
        "min_date": None,
        "max_date": None,
        "dates_cached": 0,
        "db_size_mb": 0.0,
        "last_import_at": None,
        "failed_count": 0,
    }
    if not db_available(db_path):
        return empty
    try:
        with get_connection(db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if total == 0:
                failed = conn.execute(
                    "SELECT COUNT(*) FROM import_status WHERE status = 'failed'"
                ).fetchone()[0]
                empty["failed_count"] = failed
                return empty

            stats_row = conn.execute(
                "SELECT MIN(business_date), MAX(business_date),"
                "  COUNT(DISTINCT restaurant_guid || '|' || business_date),"
                "  COUNT(DISTINCT restaurant_guid)"
                " FROM orders"
            ).fetchone()

            last_import = conn.execute(
                "SELECT MAX(completed_at) FROM import_status WHERE status = 'completed'"
            ).fetchone()[0]

            failed = conn.execute(
                "SELECT COUNT(*) FROM import_status WHERE status = 'failed'"
            ).fetchone()[0]

        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        return {
            "total_orders": total,
            "total_restaurants": stats_row[3],
            "min_date": stats_row[0],
            "max_date": stats_row[1],
            "dates_cached": stats_row[2],
            "db_size_mb": round(size_mb, 2),
            "last_import_at": last_import,
            "failed_count": failed,
        }
    except Exception:
        return empty
