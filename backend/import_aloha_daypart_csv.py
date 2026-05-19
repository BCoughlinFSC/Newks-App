"""
import_aloha_daypart_csv.py — One-shot loader for the SSMS export.

Reads the CSV produced by sql_server_sps/export_aloha_daypart_for_ipa.sql
and bulk-inserts it into the aloha_sales_daypart table.

Usage:
    python import_aloha_daypart_csv.py <path_to_csv>

The CSV from SSMS has no header row; column order must match CSV_COLUMNS in
aloha_daypart_db.py (which mirrors the SELECT order in the .sql file).
UTF-8 BOM is tolerated.
"""

import csv
import os
import sqlite3
import sys
import time
from typing import List

import aloha_daypart_db


_NUMERIC_FIELDS = {
    "daypart_id", "check_count", "checks_minus_catering",
}


def _coerce_row(values: List[str]) -> tuple:
    """Convert raw CSV strings to the right types for SQLite insert."""
    out = []
    for col, raw in zip(aloha_daypart_db.CSV_COLUMNS, values):
        if col in ("business_date", "daypart"):
            out.append(raw)
        elif col == "location":
            out.append(str(raw))
        elif col in _NUMERIC_FIELDS:
            try:
                out.append(int(float(raw)) if raw not in ("", "NULL", "None") else 0)
            except ValueError:
                out.append(0)
        else:
            try:
                out.append(float(raw) if raw not in ("", "NULL", "None") else 0.0)
            except ValueError:
                out.append(0.0)
    return tuple(out)


def import_csv(csv_path: str, db_path: str = None, batch_size: int = 5000) -> dict:
    """Load the SSMS export CSV into aloha_sales_daypart. Returns stats dict."""
    if db_path is None:
        db_path = aloha_daypart_db.ALOHA_SALES_DB_PATH
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    aloha_daypart_db.initialize(db_path)

    expected_cols = len(aloha_daypart_db.CSV_COLUMNS)
    placeholders  = ",".join("?" * expected_cols)
    columns_sql   = ",".join(aloha_daypart_db.CSV_COLUMNS)
    insert_sql    = (
        f"INSERT OR REPLACE INTO aloha_sales_daypart ({columns_sql}) "
        f"VALUES ({placeholders})"
    )

    inserted = 0
    skipped_bad_width = 0
    t0 = time.time()
    batch = []

    with aloha_daypart_db.get_connection(db_path) as conn:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for raw in reader:
                if len(raw) != expected_cols:
                    skipped_bad_width += 1
                    continue
                batch.append(_coerce_row(raw))
                if len(batch) >= batch_size:
                    conn.executemany(insert_sql, batch)
                    inserted += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(insert_sql, batch)
                inserted += len(batch)
        conn.commit()

    elapsed = time.time() - t0
    return {
        "inserted":          inserted,
        "skipped_bad_width": skipped_bad_width,
        "elapsed_sec":       round(elapsed, 1),
        "db_path":           db_path,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_aloha_daypart_csv.py <path_to_csv>", file=sys.stderr)
        sys.exit(2)
    stats = import_csv(sys.argv[1])
    print(f"Imported {stats['inserted']:,} rows in {stats['elapsed_sec']}s "
          f"(skipped {stats['skipped_bad_width']} mis-widthed rows). "
          f"DB: {stats['db_path']}")
