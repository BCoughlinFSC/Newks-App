"""
migrate_orders_to_blob.py  —  One-shot migration of orders.db → Azure Blob Storage.

Reads the legacy SQLite orders cache and uploads each (restaurant, date) as a
gzipped JSON blob to the configured Azure Blob container.

Usage
-----
  python migrate_orders_to_blob.py                     # dry-run summary
  python migrate_orders_to_blob.py --run               # migrate everything
  python migrate_orders_to_blob.py --run --resume      # skip already-uploaded
  python migrate_orders_to_blob.py --run --restaurant-guid <guid>
  python migrate_orders_to_blob.py --run --start 20240101 --end 20240630

Env vars required:
  AZURE_STORAGE_CONNECTION_STRING
  AZURE_ORDERS_CONTAINER  (optional, default: toast-orders)

This is a READ-ONLY operation on orders.db — the SQLite file is not modified.
After verifying the blob upload matches source counts, you can delete orders.db
manually to reclaim disk space.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Iterator, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import toast_orders_blob  # noqa: E402

SQLITE_PATH = os.path.join(SCRIPT_DIR, "data", "toast_orders", "orders.db")


def iter_date_groups(
    conn: sqlite3.Connection,
    restaurant_guid: str | None,
    start: str | None,
    end: str | None,
) -> Iterator[Tuple[str, str, int]]:
    """Yield (restaurant_guid, business_date, row_count) from import_status."""
    where = ["status = 'completed'"]
    params: list = []
    if restaurant_guid:
        where.append("restaurant_guid = ?")
        params.append(restaurant_guid)
    if start:
        where.append("business_date >= ?")
        params.append(start)
    if end:
        where.append("business_date <= ?")
        params.append(end)
    sql = (
        "SELECT restaurant_guid, business_date, order_count"
        " FROM import_status WHERE " + " AND ".join(where) +
        " ORDER BY business_date, restaurant_guid"
    )
    for row in conn.execute(sql, params):
        yield row[0], row[1], int(row[2] or 0)


def fetch_orders(conn: sqlite3.Connection, rguid: str, bdate: str) -> list:
    """Load and parse all order JSON blobs for one (restaurant, date)."""
    rows = conn.execute(
        "SELECT order_json FROM orders WHERE restaurant_guid = ? AND business_date = ?",
        (rguid, bdate),
    ).fetchall()
    out = []
    for (raw,) in rows:
        try:
            out.append(json.loads(raw))
        except Exception:
            pass
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true", help="Actually upload (default: dry-run summary)")
    p.add_argument("--resume", action="store_true", help="Skip (restaurant,date) pairs already in blob")
    p.add_argument("--restaurant-guid", default=None, help="Migrate only one restaurant")
    p.add_argument("--start", default=None, help="Start business_date (YYYYMMDD inclusive)")
    p.add_argument("--end", default=None, help="End business_date (YYYYMMDD inclusive)")
    p.add_argument("--sqlite-path", default=SQLITE_PATH, help="Override SQLite file path")
    args = p.parse_args()

    if not os.path.exists(args.sqlite_path):
        print(f"[error] SQLite file not found: {args.sqlite_path}")
        return 1

    size_mb = os.path.getsize(args.sqlite_path) / (1024 * 1024)
    print(f"Source : {args.sqlite_path} ({size_mb:.1f} MB)")

    conn = sqlite3.connect(args.sqlite_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Dry-run: report scope and exit
    groups = list(iter_date_groups(conn, args.restaurant_guid, args.start, args.end))
    total_groups = len(groups)
    total_orders = sum(n for _, _, n in groups)
    print(f"Scope  : {total_groups} (restaurant,date) groups, {total_orders:,} orders")

    if not args.run:
        print("\n[dry-run] Pass --run to execute the migration.")
        return 0

    # Ensure blob target is configured
    if not toast_orders_blob.db_available():
        print("[error] Azure Blob target is not reachable. Check AZURE_STORAGE_CONNECTION_STRING.")
        return 1
    toast_orders_blob.initialize_db()
    print(f"Target : container '{toast_orders_blob.DEFAULT_CONTAINER}'")

    # Optional resume — query existing blobs once
    existing: set = set()
    if args.resume:
        print("Resume : scanning existing blobs...")
        for row in toast_orders_blob.get_cached_dates(args.restaurant_guid):
            existing.add((row["restaurant_guid"], row["business_date"]))
        print(f"Resume : {len(existing)} already uploaded, will skip")

    print()
    uploaded = 0
    skipped = 0
    failed = 0
    uploaded_orders = 0
    start_ts = time.time()

    for i, (rguid, bdate, expected_count) in enumerate(groups, 1):
        if args.resume and (rguid, bdate) in existing:
            skipped += 1
            continue

        try:
            orders = fetch_orders(conn, rguid, bdate)
            n = toast_orders_blob.store_orders(rguid, bdate, orders)
            uploaded += 1
            uploaded_orders += n
        except Exception as exc:
            failed += 1
            print(f"[fail] {rguid[:8]}|{bdate}: {exc}")
            continue

        if i % 25 == 0 or i == total_groups:
            elapsed = time.time() - start_ts
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (total_groups - i) / rate if rate > 0 else 0
            print(
                f"[{i}/{total_groups}] uploaded={uploaded} skipped={skipped} "
                f"failed={failed} orders={uploaded_orders:,} "
                f"rate={rate:.1f}/s eta={remaining/60:.1f}min"
            )

    conn.close()

    print()
    print(f"Done. uploaded={uploaded} skipped={skipped} failed={failed} orders={uploaded_orders:,}")
    if failed:
        print("[warning] Some groups failed. Re-run with --resume to retry.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
