"""
Aloha SQL Schema Discovery
===========================
Run this to discover:
  1. Columns available in dbo.MenuMix (looking for order mode / type)
  2. Store/Location lookup tables (to build the location name → ID mapping)
  3. Sample of distinct values for key columns

Usage:
    python schema_discovery.py

Requires ALOHA_SQL_* settings in backend/.env
"""

import os
import sys
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc not installed. Run: pip install pyodbc")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas")
    sys.exit(1)


def _build_connection_string() -> str:
    server   = os.getenv("ALOHA_SQL_SERVER")
    database = os.getenv("ALOHA_SQL_DATABASE")
    username = os.getenv("ALOHA_SQL_USERNAME")
    password = os.getenv("ALOHA_SQL_PASSWORD")
    driver   = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted  = os.getenv("ALOHA_SQL_TRUSTED_CONNECTION", "false").lower() in ("1", "true", "yes")
    encrypt  = os.getenv("ALOHA_SQL_ENCRYPT", "true").lower() in ("1", "true", "yes")
    trust_cert = os.getenv("ALOHA_SQL_TRUST_SERVER_CERT", "false").lower() in ("1", "true", "yes")

    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    if trust_cert:
        parts.append("TrustServerCertificate=yes")
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={username}")
        parts.append(f"PWD={password}")
    return ";".join(parts)


def run_query(conn, sql: str, label: str) -> pd.DataFrame:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    try:
        df = pd.read_sql_query(sql, conn)
        print(df.to_string(index=False))
        return df
    except Exception as e:
        print(f"  ERROR: {e}")
        return pd.DataFrame()


def main():
    print("Connecting to Aloha SQL Server...")
    conn_str = _build_connection_string()
    try:
        conn = pyodbc.connect(conn_str)
        print("  ✓ Connected\n")
    except Exception as e:
        print(f"  ERROR connecting: {e}")
        sys.exit(1)

    # ── 1. Columns in dbo.MenuMix ──────────────────────────────────────────────
    run_query(conn, """
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'MenuMix'
        ORDER BY ORDINAL_POSITION
    """, "dbo.MenuMix COLUMNS")

    # ── 2. Sample MenuMix rows ─────────────────────────────────────────────────
    run_query(conn, """
        SELECT TOP 3 * FROM dbo.MenuMix ORDER BY DateOfBusiness DESC
    """, "dbo.MenuMix SAMPLE ROWS (top 3, most recent)")

    # ── 3. Look for order-type / order-mode columns ────────────────────────────
    run_query(conn, """
        SELECT COLUMN_NAME, TABLE_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME IN ('MenuMix','GndChk','OrderType','OrderMode','Ordertype')
          AND COLUMN_NAME LIKE '%order%' OR COLUMN_NAME LIKE '%type%'
              OR COLUMN_NAME LIKE '%mode%' OR COLUMN_NAME LIKE '%togo%'
              OR COLUMN_NAME LIKE '%delivery%' OR COLUMN_NAME LIKE '%dine%'
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """, "ORDER MODE / TYPE columns across key tables")

    # ── 4. All tables that might contain store/location info ───────────────────
    run_query(conn, """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
          AND (TABLE_NAME LIKE '%store%' OR TABLE_NAME LIKE '%location%'
               OR TABLE_NAME LIKE '%restaurant%' OR TABLE_NAME LIKE '%site%'
               OR TABLE_NAME LIKE '%unit%' OR TABLE_NAME = 'Store'
               OR TABLE_NAME LIKE '%Company%')
        ORDER BY TABLE_NAME
    """, "TABLES with store/location names")

    # ── 5. Try common store lookup table names ────────────────────────────────
    for tbl in ["dbo.Store", "dbo.Location", "dbo.Restaurant", "dbo.Site", "dbo.CompanyStore"]:
        run_query(conn, f"""
            SELECT TOP 5 * FROM {tbl}
        """, f"SAMPLE from {tbl}")

    # ── 6. Distinct Location IDs already in MenuMix ──────────────────────────
    run_query(conn, """
        SELECT DISTINCT m.fkstoreid AS AlohaLocationId, COUNT(*) AS DayCount
        FROM dbo.MenuMix m
        WHERE m.DateOfBusiness >= DATEADD(day, -90, GETDATE())
        GROUP BY m.fkstoreid
        ORDER BY m.fkstoreid
    """, "DISTINCT Aloha Location IDs in MenuMix (last 90 days)")

    # ── 7. Check GndChk table for order mode ─────────────────────────────────
    run_query(conn, """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'GndChk'
        ORDER BY ORDINAL_POSITION
    """, "dbo.GndChk COLUMNS (check for OrderType/Mode)")

    # ── 8. Distinct order types in GndChk (if the table exists) ──────────────
    run_query(conn, """
        SELECT TOP 20 *
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'OrderType'
        ORDER BY ORDINAL_POSITION
    """, "dbo.OrderType table columns (if exists)")

    run_query(conn, """
        SELECT TOP 20 * FROM dbo.OrderType
    """, "dbo.OrderType VALUES (if exists)")

    conn.close()
    print("\n✓ Discovery complete. Review output above and share with your developer.")
    print("  Key things to note:")
    print("  1. Does MenuMix have an OrderType/OrderMode column? (for per-channel breakdown)")
    print("  2. Is there a Store table with store names? (for location mapping)")
    print("  3. What are the distinct store IDs and their names?")


if __name__ == "__main__":
    main()
