"""
migrate_to_sqlite.py  —  One-time migration of all Aloha CSV files to SQLite.

Reads every aloha_YYYY-MM-DD.csv from backend/data/aloha/ (or from index.json),
then inserts the data into the SQLite database at backend/data/aloha/aloha.db.

Existing rows for a date are replaced (safe to re-run).

Usage
-----
  cd backend
  venv/Scripts/python -X utf8 migrate_to_sqlite.py [--dry-run]

Options
-------
  --dry-run   Scan files and report what would be migrated, without writing.
  --limit N   Migrate at most N dates (for testing).
"""
import os
import sys
import json
import glob
import time
import argparse
from datetime import datetime

import pandas as pd

# ── Path setup ───────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from aloha_db import (
    initialize_db,
    upsert_date,
    get_stats,
    ALOHA_DB_PATH,
)

DATA_DIR   = os.path.join(SCRIPT_DIR, "data", "aloha")
INDEX_FILE = os.path.join(DATA_DIR, "index.json")


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_file_list() -> list[tuple[str, str]]:
    """
    Return a sorted list of (date_iso, file_path) tuples.
    Sources index.json first; falls back to glob over the directory.
    """
    pairs: dict[str, str] = {}

    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                idx = json.load(f)
            for date_iso, entry in idx.get("files", {}).items():
                path = entry.get("path", "")
                if path and os.path.exists(path):
                    pairs[date_iso] = path
        except Exception as e:
            print(f"  Warning: could not read index.json: {e}")

    # Fill gaps via glob (handles manually-dropped CSV files)
    for p in glob.glob(os.path.join(DATA_DIR, "aloha_????-??-??.csv")):
        basename = os.path.basename(p)          # aloha_YYYY-MM-DD.csv
        date_iso = basename[6:16]               # YYYY-MM-DD
        if date_iso not in pairs:
            pairs[date_iso] = p

    return sorted(pairs.items())


def read_csv_safe(path: str) -> pd.DataFrame | None:
    """Read a CSV and return a normalised DataFrame, or None on failure."""
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception as e:
        print(f"    ERROR reading {path}: {e}")
        return None

    df.columns = [c.strip() for c in df.columns]

    if "Item" not in df.columns:
        print(f"    SKIP — no 'Item' column")
        return None

    for col, default in [("Quantity", "0"), ("Sales", "0"),
                         ("Location", ""), ("OrderMode", "Unknown")]:
        if col not in df.columns:
            df[col] = default

    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)
    df["Sales"]    = pd.to_numeric(df["Sales"],    errors="coerce").fillna(0)
    return df


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Aloha CSVs → SQLite")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report without writing")
    parser.add_argument("--limit", type=int, default=0,
                        help="Migrate at most N dates (0 = all)")
    args = parser.parse_args()

    file_list = load_file_list()
    total = len(file_list)

    if args.limit:
        file_list = file_list[:args.limit]
        print(f"--limit {args.limit}: processing {len(file_list)} of {total} dates")

    print(f"\nAloha CSV → SQLite migration")
    print(f"  Source dir : {DATA_DIR}")
    print(f"  Target DB  : {ALOHA_DB_PATH}")
    print(f"  Dates found: {total}")
    if args.dry_run:
        print("  Mode       : DRY RUN (no writes)\n")
    else:
        print("  Mode       : LIVE\n")
        initialize_db()

    written   = 0
    skipped   = 0
    errors    = 0
    total_rows = 0
    t0 = time.time()

    for i, (date_iso, path) in enumerate(file_list):
        prefix = f"[{i+1:4d}/{len(file_list)}] {date_iso}"

        df = read_csv_safe(path)
        if df is None:
            skipped += 1
            continue

        n_rows = len(df)

        if args.dry_run:
            print(f"  {prefix} — {n_rows} rows  ({os.path.basename(path)})")
            written += 1
            total_rows += n_rows
            continue

        try:
            inserted = upsert_date(df, date_iso)
            written   += 1
            total_rows += inserted

            # Progress every 50 dates
            if (i + 1) % 50 == 0 or i == 0 or i == len(file_list) - 1:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed else 0
                eta  = (len(file_list) - i - 1) / rate if rate else 0
                print(f"  {prefix} — {inserted:5d} rows  "
                      f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)")

        except Exception as exc:
            print(f"  {prefix} — ERROR: {exc}")
            errors += 1

    elapsed = time.time() - t0
    print(f"\n{'DRY RUN ' if args.dry_run else ''}Migration complete in {elapsed:.1f}s")
    print(f"  Written : {written} dates")
    print(f"  Skipped : {skipped} dates")
    print(f"  Errors  : {errors} dates")
    print(f"  Rows    : {total_rows:,}")

    if not args.dry_run:
        stats = get_stats()
        print(f"\nDB stats:")
        print(f"  Total rows : {stats['total_rows']:,}")
        print(f"  Date range : {stats['min_date']} → {stats['max_date']}")
        print(f"  Dates      : {stats['date_count']}")
        print(f"  Locations  : {stats['location_count']}")
        print(f"  DB size    : {stats['db_size_mb']} MB")


if __name__ == "__main__":
    main()
