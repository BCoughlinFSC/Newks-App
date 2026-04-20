"""
backfill_sales_apr17_19.py
One-off: repoll all live Toast stores for 2026-04-17, 04-18, 04-19.

Writes directly to SQL Server (no Kerberos / PostgreSQL needed).
Runs serially — DO NOT run other repolls at the same time (shared staging table).

Usage:
    cd backend
    python backfill_sales_apr17_19.py

Estimated time: ~2-4 minutes per store × 97 stores = 3-6 hours worst case.
High-volume stores (1003, 1011, etc.) will be slower due to more pages.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import toast_puller_db as db
from toast_puller import pull_store_for_date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATES = ["2026-04-17", "2026-04-18", "2026-04-19"]


async def main():
    # Get all live Toast stores
    conn = db.get_toast_connection()
    try:
        stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()

    log.info("Found %d live Toast stores to repoll for %s", len(stores), DATES)

    successes = []
    failures = []

    for i, store in enumerate(stores, 1):
        store_num = store["store_num"]
        guid = store["guid"]
        name = store.get("name", "")
        log.info("─── Store %d/%d: %s %s ───", i, len(stores), store_num, name)

        for bdate in DATES:
            try:
                result = await pull_store_for_date(
                    store_num=store_num,
                    business_date=bdate,
                    restaurant_guid=guid,
                    skip_rollup=True,   # Run rollup once at the end
                )
                if result.success:
                    log.info("  ✓ %s: %d checks, %d items (%.1fs)",
                             bdate, result.checks_count, result.items_count, result.duration_sec)
                    successes.append((store_num, bdate))
                else:
                    log.error("  ✗ %s: %s", bdate, result.errors)
                    failures.append((store_num, bdate, result.errors))
            except Exception as e:
                log.error("  ✗ %s: unexpected error — %s", bdate, e)
                failures.append((store_num, bdate, [str(e)]))

    # ── Run rollup once for the full date range ──────────────────────────────
    log.info("=" * 60)
    log.info("Running sp_toast_SalesDataByDayPart for 04-17 → 04-19 ...")
    conn = db.get_toast_connection()
    try:
        db.run_rollup(
            conn,
            start_date="2026-04-17",
            end_date="2026-04-19",
            store_grouping=9,   # storeGrouping=9 = individual stores → all
            store_group=243,    # group 243 = all Toast stores
        )
        log.info("Rollup complete.")
    except Exception as e:
        log.error("Rollup failed: %s", e)
    finally:
        conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("BACKFILL COMPLETE")
    print("=" * 60)
    print(f"  Stores processed : {len(stores)}")
    print(f"  Store+date pairs : {len(stores) * len(DATES)}")
    print(f"  Successes        : {len(successes)}")
    print(f"  Failures         : {len(failures)}")
    if failures:
        print("\nFailed store/date pairs:")
        for store_num, bdate, errs in failures:
            print(f"  Store {store_num} / {bdate}: {errs[0] if errs else 'unknown'}")
    print("=" * 60)
    print("\nNext step: trigger SQL Agent job 'Populate tbl_LaborDataByDayPart' in SSMS:")
    print("  EXEC msdb.dbo.sp_start_job N'Populate tbl_LaborDataByDayPart';")


if __name__ == "__main__":
    asyncio.run(main())
