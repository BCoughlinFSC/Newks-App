#!/usr/bin/env python3
"""
sync_monkey.py — Sync Monkey Media data from Azure SQL to local SQLite.

Connects directly to the Monkey Media cloud database (Azure SQL) and
imports orders, accounts, clients, stores, drivers, items, payments,
refunds, and order_items into monkey.db.

Usage:
    python sync_monkey.py                    # Full sync (all tables)
    python sync_monkey.py --tables orders    # Sync only orders
    python sync_monkey.py --days 7           # Only orders from last N days
    python sync_monkey.py --since 2026-03-01 # Only orders since date

Environment variables (from .env):
    MONKEY_DB_SERVER   prod-monkeymedia-monkeysee.database.windows.net
    MONKEY_DB_NAME     dev308_monkeymedia_ca_01
    MONKEY_DB_USER     monkeysee_newks
    MONKEY_DB_PASS     <password>
"""

import os
import sys
import argparse
import logging
import time
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv

try:
    import pymssql
    HAS_PYMSSQL = True
except ImportError:
    HAS_PYMSSQL = False

try:
    import pyodbc
    HAS_PYODBC = True
except ImportError:
    HAS_PYODBC = False

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sync_monkey")

# ── Azure SQL connection ────────────────────────────────────────────

def get_azure_connection():
    server = os.getenv("MONKEY_DB_SERVER")
    database = os.getenv("MONKEY_DB_NAME")
    user = os.getenv("MONKEY_DB_USER")
    password = os.getenv("MONKEY_DB_PASS")

    if not all([server, database, user, password]):
        raise RuntimeError(
            "Missing MONKEY_DB_* env vars. Need: MONKEY_DB_SERVER, "
            "MONKEY_DB_NAME, MONKEY_DB_USER, MONKEY_DB_PASS"
        )

    # Try pymssql first (no ODBC driver needed), then pyodbc
    if HAS_PYMSSQL:
        try:
            conn = pymssql.connect(
                server=server,
                user=user,
                password=password,
                database=database,
                port=1433,
                login_timeout=30,
                as_dict=False,
            )
            logger.info("Connected to Azure SQL via pymssql")
            return conn
        except Exception as e:
            logger.warning("pymssql failed: %s", e)

    if HAS_PYODBC:
        drivers = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "FreeTDS",
        ]
        available = [d for d in pyodbc.drivers() if d in drivers] or ["FreeTDS"]

        last_err = None
        for driver in available:
            try:
                if driver == "FreeTDS":
                    conn_str = (
                        f"DRIVER={{{driver}}};"
                        f"SERVER={server};"
                        f"PORT=1433;"
                        f"DATABASE={database};"
                        f"UID={user};"
                        f"PWD={password};"
                        f"TDS_Version=7.4;"
                    )
                else:
                    conn_str = (
                        f"DRIVER={{{driver}}};"
                        f"SERVER={server},1433;"
                        f"DATABASE={database};"
                        f"UID={user};"
                        f"PWD={password};"
                        f"Encrypt=yes;"
                        f"TrustServerCertificate=yes;"
                        f"Connection Timeout=30;"
                    )
                conn = pyodbc.connect(conn_str, timeout=30)
                logger.info("Connected to Azure SQL via pyodbc (%s)", driver)
                return conn
            except Exception as e:
                last_err = e
        raise RuntimeError(f"pyodbc failed: {last_err}")

    raise RuntimeError("Neither pymssql nor pyodbc available")


# ── Table sync definitions ──────────────────────────────────────────

TABLES = {
    "stores": {
        "query": "SELECT * FROM msr.stores",
        "pk": "store_id",
    },
    "accounts": {
        "query": "SELECT * FROM msr.accounts",
        "pk": "account_id",
    },
    "clients_and_leads": {
        "query": "SELECT * FROM msr.clients_and_leads",
        "pk": "client_id",
    },
    "items": {
        "query": "SELECT * FROM msr.items",
        "pk": "item_id",
    },
    "drivers": {
        "query": "SELECT * FROM msr.drivers",
        "pk": "driver_id",
    },
    "payments": {
        "query": "SELECT * FROM msr.payments",
        "pk": "payment_id",
    },
    "refunds": {
        "query": "SELECT * FROM msr.refunds",
        "pk": "refund_id",
    },
    "orders": {
        "query": "SELECT * FROM msr.orders",
        "pk": "order_id",
        "date_col": "order_last_modified_datetime",
    },
    "order_items": {
        "query": "SELECT * FROM msr.order_items",
        "pk": "order_item_id",
    },
}


# ── Column mapping (Azure → SQLite) ────────────────────────────────

def get_sqlite_columns(sqlite_conn, table):
    """Get set of column names in the SQLite table."""
    cursor = sqlite_conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def sync_table(azure_conn, sqlite_conn, table, config, since=None):
    """Sync a single table from Azure SQL to SQLite."""
    query = config["query"]

    # Apply date filter for incremental sync
    if since and "date_col" in config:
        date_col = config["date_col"]
        query += f" WHERE {date_col} >= ?"
        logger.info("Syncing %s (since %s)...", table, since)
        azure_cursor = azure_conn.cursor()
        azure_cursor.execute(query, since)
    else:
        logger.info("Syncing %s (full)...", table)
        azure_cursor = azure_conn.cursor()
        azure_cursor.execute(query)

    # Get column names from Azure result
    azure_cols = [desc[0] for desc in azure_cursor.description]
    sqlite_cols = get_sqlite_columns(sqlite_conn, table)

    # Map: only columns that exist in both
    common_cols = [c for c in azure_cols if c.lower().replace(" ", "_") in {s.lower() for s in sqlite_cols}]
    # Build mapping: azure col name → sqlite col name
    sqlite_col_lower = {s.lower(): s for s in sqlite_cols}
    col_map = []
    for ac in azure_cols:
        sc = ac.lower().replace(" ", "_")
        if sc in sqlite_col_lower:
            col_map.append((ac, sqlite_col_lower[sc]))

    if not col_map:
        logger.warning("No matching columns for %s. Azure: %s, SQLite: %s",
                       table, azure_cols[:5], list(sqlite_cols)[:5])
        return 0

    azure_indices = [azure_cols.index(ac) for ac, _ in col_map]
    sqlite_names = [sc for _, sc in col_map]

    ph = ",".join("?" * len(sqlite_names))
    col_str = ",".join(sqlite_names)

    # For full sync, clear and reload
    if since is None:
        sqlite_conn.execute(f"DELETE FROM {table}")

    # Batch insert
    batch = []
    count = 0
    batch_size = 5000

    for row in azure_cursor:
        vals = []
        for idx in azure_indices:
            v = row[idx]
            # Convert datetime to string for SQLite
            if isinstance(v, datetime):
                v = v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            elif v is not None and not isinstance(v, (int, float, str)):
                v = str(v)
            vals.append(v)
        batch.append(vals)

        if len(batch) >= batch_size:
            if since:
                # Upsert for incremental
                pk = config["pk"]
                if pk in sqlite_names:
                    pk_idx = sqlite_names.index(pk)
                    for b in batch:
                        try:
                            sqlite_conn.execute(
                                f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", b
                            )
                        except Exception:
                            pass
                else:
                    sqlite_conn.executemany(
                        f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", batch
                    )
            else:
                sqlite_conn.executemany(
                    f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", batch
                )
            count += len(batch)
            batch = []
            if count % 50000 == 0:
                logger.info("  %s: %d rows...", table, count)

    # Final batch
    if batch:
        if since:
            for b in batch:
                try:
                    sqlite_conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", b
                    )
                except Exception:
                    pass
        else:
            sqlite_conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph})", batch
            )
        count += len(batch)

    sqlite_conn.commit()
    logger.info("  %s: %d rows synced", table, count)
    return count


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Monkey Media Azure SQL → SQLite")
    parser.add_argument("--tables", nargs="+", help="Specific tables to sync",
                        choices=list(TABLES.keys()))
    parser.add_argument("--days", type=int, help="Incremental: sync orders from last N days")
    parser.add_argument("--since", help="Incremental: sync orders since date (YYYY-MM-DD)")
    parser.add_argument("--full", action="store_true", help="Force full sync of all tables")
    args = parser.parse_args()

    import monkey_db
    monkey_db.initialize_db()

    tables_to_sync = args.tables or list(TABLES.keys())

    since = None
    if args.days:
        since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.since:
        since = args.since

    if args.full:
        since = None

    t0 = time.time()
    logger.info("Starting Monkey sync (%s tables)...", len(tables_to_sync))

    try:
        azure_conn = get_azure_connection()
    except Exception as e:
        logger.error("Failed to connect to Azure SQL: %s", e)
        sys.exit(1)

    sqlite_conn = monkey_db.get_connection()
    total_rows = 0

    try:
        for table in tables_to_sync:
            config = TABLES[table]
            # Only apply 'since' to tables that have a date column
            table_since = since if "date_col" in config else None
            if since and table_since is None and table != "orders":
                # For non-date tables, only do full sync if explicitly requested
                if args.days or args.since:
                    logger.info("Skipping %s (no date column, use --full for full sync)", table)
                    continue
            rows = sync_table(azure_conn, sqlite_conn, table, config, since=table_since)
            total_rows += rows
    finally:
        azure_conn.close()
        sqlite_conn.close()

    elapsed = time.time() - t0
    logger.info("Sync complete: %d total rows in %.1fs", total_rows, elapsed)

    # Print stats
    stats = monkey_db.get_stats()
    logger.info("monkey.db stats:")
    for k, v in stats.items():
        logger.info("  %s: %s", k, v)


if __name__ == "__main__":
    main()
