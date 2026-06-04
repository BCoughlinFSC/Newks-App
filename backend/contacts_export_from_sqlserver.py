"""
contacts_export_from_sqlserver.py — One-time (or periodic) dump of the
SQL-PROD `contacts` database into a local SQLite file.

Why this exists: Cloudways (where the Newk's reporting app runs) can't
reach the internal sql-prod host, so we can't talk to SQL Server from
the app directly. Instead, we mirror the contacts data into SQLite,
SCP that file onto Cloudways, and let the app read/write its own copy.

Where this runs: dev-jan02 (which IS on the internal Newks network and
can reach sql-prod). After running, SCP the resulting contacts.db to
Cloudways at backend/data/contacts/contacts.db.

USAGE:
    # Quick one-table sanity check
    python contacts_export_from_sqlserver.py --tables AreaDir,Company

    # Full export of every table we use in the new UI (default)
    python contacts_export_from_sqlserver.py

    # Custom output path
    python contacts_export_from_sqlserver.py --output /tmp/contacts.db

ENV (in same .env you already use on dev-jan02 or via shell exports):
    CONTACTS_SOURCE_HOST     defaults to sql-prod.nfc.local
    CONTACTS_SOURCE_USER     defaults to 'reports'  (Don's read account)
    CONTACTS_SOURCE_PASSWORD required — get from KeePass

The script auto-detects each table's column types from SQL Server's
INFORMATION_SCHEMA and maps them to SQLite types (TEXT, INTEGER, REAL,
or BLOB). PRIMARY KEY is inferred from the table-level constraint.
Existing rows in the target SQLite table are DROPPED before each
export so the dump is a clean snapshot.
"""

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Tables to mirror. Add to this list as we extend the Contacts UI to
# cover more parts of the SQL Server contacts schema.
DEFAULT_TABLES = [
    "AreaDir",
    "Company",
    # Future:
    # "Store", "StorePhysical", "StoreTechnical", "StoreClosure", "StoreSize",
    # "BillingContacts", "PrimaryContacts", "CateringMgr", "GeneralManagers",
    # "CCProcessors", "FireWallProvider", "DeliveryPartners", "DeliveryPartnersByStore",
    # "PhoneVendors", "OwnerShip", "CateringGoals", "CateringBudgeted",
    # "POSType", "MenuType", "TimeZone",  -- lookups
]

# SQL Server data type → SQLite affinity
TYPE_MAP = {
    "int":         "INTEGER",
    "bigint":      "INTEGER",
    "smallint":    "INTEGER",
    "tinyint":     "INTEGER",
    "bit":         "INTEGER",
    "decimal":     "REAL",
    "numeric":     "REAL",
    "money":       "REAL",
    "smallmoney":  "REAL",
    "float":       "REAL",
    "real":        "REAL",
    "char":        "TEXT",
    "varchar":     "TEXT",
    "nchar":       "TEXT",
    "nvarchar":    "TEXT",
    "text":        "TEXT",
    "ntext":       "TEXT",
    "uniqueidentifier": "TEXT",
    "datetime":    "TEXT",  # ISO 8601 string in SQLite
    "datetime2":   "TEXT",
    "smalldatetime": "TEXT",
    "date":        "TEXT",
    "time":        "TEXT",
    "binary":      "BLOB",
    "varbinary":   "BLOB",
    "image":       "BLOB",
}


def _connect_sql_server():
    """pymssql connection to SQL-PROD contacts DB.

    Why pymssql instead of pyodbc: pymssql is pure-Python (bundles its
    own FreeTDS), so it installs via `pip install pymssql` with no
    system ODBC driver or sudo required. pyodbc needs unixODBC plus a
    Microsoft-supplied SQL Server driver, which is awkward on
    locked-down Linux boxes like dev-jan02.
    """
    try:
        import pymssql
    except ImportError:
        sys.exit("ERROR: pymssql not installed. Run: pip3 install pymssql")

    host     = os.getenv("CONTACTS_SOURCE_HOST",     "sql-prod.nfc.local")
    user     = os.getenv("CONTACTS_SOURCE_USER",     "reports")
    password = os.getenv("CONTACTS_SOURCE_PASSWORD", "")

    if not password:
        sys.exit("ERROR: CONTACTS_SOURCE_PASSWORD env var not set "
                 "(get it from KeePass — Don used the 'reports' SQL user)")

    logger.info("Connecting to SQL Server: %s as %s", host, user)
    return pymssql.connect(
        server=host,
        user=user,
        password=password,
        database="contacts",
        timeout=30,
        login_timeout=15,
    )


def _introspect_table(src_cur, table_name: str) -> Tuple[List[Dict], Optional[str]]:
    """
    Return ([{name, sql_type, sqlite_type, max_length, nullable}, ...], pk_column_name)
    for the given table in the contacts schema.

    Uses pymssql's default tuple cursor: row[0]=column_name, row[1]=data_type, etc.
    Placeholder syntax is %s (pymssql), not ? (pyodbc).
    """
    # Columns
    src_cur.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
        FROM   contacts.INFORMATION_SCHEMA.COLUMNS
        WHERE  TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """, (table_name,))
    cols = []
    for row in src_cur.fetchall():
        col_name, data_type, max_len, is_nullable = row[0], row[1], row[2], row[3]
        sql_type = (data_type or "").lower()
        cols.append({
            "name":        col_name,
            "sql_type":    sql_type,
            "sqlite_type": TYPE_MAP.get(sql_type, "TEXT"),
            "max_length":  max_len,
            "nullable":    (is_nullable == "YES"),
        })

    # Primary key
    src_cur.execute("""
        SELECT COL.COLUMN_NAME
        FROM   contacts.INFORMATION_SCHEMA.TABLE_CONSTRAINTS TC
        JOIN   contacts.INFORMATION_SCHEMA.KEY_COLUMN_USAGE COL
          ON   TC.CONSTRAINT_NAME = COL.CONSTRAINT_NAME
         AND   TC.TABLE_NAME = COL.TABLE_NAME
        WHERE  TC.TABLE_NAME = %s
          AND  TC.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY COL.ORDINAL_POSITION
    """, (table_name,))
    pk_rows = src_cur.fetchall()
    pk = pk_rows[0][0] if pk_rows else None
    return cols, pk


def _create_sqlite_table(dst_conn, table_name: str, cols: List[Dict], pk: Optional[str]) -> None:
    """Drop + recreate the target SQLite table with matching column types."""
    dst_conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    col_defs = []
    for c in cols:
        col_def = f"{c['name']} {c['sqlite_type']}"
        if pk and c["name"] == pk:
            col_def += " PRIMARY KEY"
        if not c["nullable"] and not (pk and c["name"] == pk):
            # Skip NOT NULL on the PK (auto-handled); honor on other cols
            col_def += " NOT NULL"
        col_defs.append(col_def)
    create_sql = f"CREATE TABLE {table_name} (\n    " + ",\n    ".join(col_defs) + "\n)"
    logger.debug("CREATE: %s", create_sql)
    dst_conn.execute(create_sql)


def _copy_rows(src_cur, dst_conn, table_name: str, cols: List[Dict]) -> int:
    """Copy all rows from SQL Server table → SQLite table. Returns row count."""
    col_names = [c["name"] for c in cols]
    col_list = ", ".join(col_names)
    placeholders = ", ".join("?" * len(col_names))

    src_cur.execute(f"SELECT {col_list} FROM contacts.dbo.{table_name}")
    rows = src_cur.fetchall()

    # Convert pyodbc Row → tuple of values (handling datetime, Decimal, etc.)
    payload = []
    for r in rows:
        out_row = []
        for v in r:
            if v is None:
                out_row.append(None)
            elif isinstance(v, datetime):
                out_row.append(v.isoformat(sep=" ", timespec="seconds"))
            elif hasattr(v, "__float__"):  # Decimal etc.
                try:
                    out_row.append(float(v))
                except Exception:
                    out_row.append(str(v))
            else:
                out_row.append(v)
        payload.append(tuple(out_row))

    if payload:
        dst_conn.executemany(
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",
            payload,
        )
    return len(payload)


def export_tables(output_path: str, table_names: List[str]) -> Dict[str, int]:
    """Run the full export. Returns {table_name: row_count}."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    src = _connect_sql_server()
    src_cur = src.cursor()

    dst = sqlite3.connect(output_path)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")

    summary: Dict[str, int] = {}
    try:
        for table in table_names:
            logger.info("─── Exporting %s ───", table)
            cols, pk = _introspect_table(src_cur, table)
            if not cols:
                logger.warning("Table %s has no columns (does it exist?). Skipping.", table)
                summary[table] = 0
                continue
            _create_sqlite_table(dst, table, cols, pk)
            count = _copy_rows(src_cur, dst, table, cols)
            dst.commit()
            logger.info("  copied %d rows (pk=%s, cols=%d)", count, pk, len(cols))
            summary[table] = count
    finally:
        src.close()
        dst.close()

    return summary


def main():
    p = argparse.ArgumentParser(description="Export contacts tables from SQL Server to SQLite.")
    p.add_argument(
        "--tables",
        default=",".join(DEFAULT_TABLES),
        help="Comma-separated table names. Default: " + ",".join(DEFAULT_TABLES),
    )
    p.add_argument(
        "--output",
        default=os.path.join(os.path.dirname(__file__), "data", "contacts", "contacts.db"),
        help="Output SQLite path. Default: backend/data/contacts/contacts.db",
    )
    args = p.parse_args()

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    logger.info("Output: %s", args.output)
    logger.info("Tables: %s", tables)

    summary = export_tables(args.output, tables)

    logger.info("=" * 60)
    logger.info("Export complete:")
    for table, count in summary.items():
        logger.info("  %-25s %6d rows", table, count)
    logger.info("=" * 60)
    logger.info("Next: scp %s root@cloudways:/home/master/applications/wpftndhcuj/public_html/backend/data/contacts/contacts.db", args.output)


if __name__ == "__main__":
    main()
