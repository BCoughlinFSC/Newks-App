"""
nightly_pull.py — Automated nightly Toast data pull for all stores.

Replaces Don's PHP puller (dev-jan02 cron). Fetches yesterday's sales data
directly from the Toast API and writes to SQL Server — no PostgreSQL staging,
no sp_copy, no temp table snowball.

CRON SETUP (on dev-jan02)
--------------------------
  crontab -e
  0 3 * * * /opt/toast_backfill/backend/venv/bin/python /opt/toast_backfill/backend/nightly_pull.py >> /var/log/toast_nightly.log 2>&1

DISABLE PHP PULLER (once this is confirmed working)
-----------------------------------------------------
  crontab -e  (on dev-jan02, comment out the PHP cron line)

PIPELINE
--------
  For each store:
    1. Delete yesterday's rows from hstItem / hstCheck / hstOrder
    2. Truncate hstItem_temp
    3. Fetch from Toast API
    4. Write orders → hstOrder, checks → hstCheck, items → hstItem_temp
    5. Direct copy hstItem_temp → hstItem (bypasses encrypted SP inflation)
  Then once for the day:
    6. fix_daypart_ids  ← MUST run before rollup for correct channel breakdown
    7. Rebuild rollup (tbl_SalesDataByDayPart)
    8. Rebuild royalty (sp_tbl_Royalty_Values) for yesterday only

DO NOT run simultaneously with any repoll script.
hstItem_temp is a shared staging table — one process at a time.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import httpx

import toast_puller_db as db
from toast_puller import fetch_orders_with_retry, parse_orders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nightly_pull")
logging.getLogger("httpx").setLevel(logging.WARNING)

STORE_GROUPING = 9
STORE_GROUP    = 243


# ─── Process one store ────────────────────────────────────────────────────────

async def pull_store(
    client: httpx.AsyncClient,
    store_guid: str,
    store_num: int,
    store_name: str,
    date_sql: str,
) -> dict:
    date_yyyymmdd = date_sql.replace("-", "")
    result = {
        "store_num":  store_num,
        "store_name": store_name,
        "ok":         False,
        "orders":     0,
        "checks":     0,
        "items":      0,
        "error":      None,
    }

    try:
        # Delete stale rows
        conn = db.get_toast_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM toast.dbo.hstItem  WHERE StoreNum=? AND DateOfBusiness=?",
                store_num, date_sql,
            )
            cur.execute(
                "DELETE FROM toast.dbo.hstCheck WHERE StoreNum=? AND DateOfBusiness=?",
                store_num, date_sql,
            )
            cur.execute(
                "DELETE FROM toast.dbo.hstOrder WHERE StoreNum=? AND DateOfBusiness=?",
                store_num, date_sql,
            )
            conn.commit()
        finally:
            conn.close()

        # Truncate staging table
        conn = db.get_toast_connection()
        try:
            conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
            conn.commit()
        finally:
            conn.close()

        # Fetch from Toast API
        orders, errors = await fetch_orders_with_retry(
            client, store_guid, store_num, date_yyyymmdd
        )

        if errors and not orders:
            result["error"] = str(errors[0])
            log.error("  [%s] API fetch failed: %s", store_num, errors[0])
            return result

        if not orders:
            log.info("  [%s] %s — 0 orders (closed).", store_num, store_name)
            result["ok"] = True
            return result

        parsed = parse_orders(orders, store_guid, store_num, date_yyyymmdd)
        result["orders"] = len(parsed["orders"])
        result["checks"] = len(parsed["checks"])
        result["items"]  = len(parsed["items"])

        # Write to SQL Server
        conn = db.get_toast_connection()
        try:
            if parsed["orders"]:
                db.upsert_orders(conn, parsed["orders"])
            if parsed["checks"]:
                db.upsert_checks(conn, parsed["checks"])
            if parsed["items"]:
                db.upsert_items(conn, parsed["items"], skip_truncate=True, skip_proc=True)
        finally:
            conn.close()

        # Direct copy temp → hstItem (no encrypted SP, no inflation)
        conn = db.get_toast_connection()
        try:
            inserted = db.direct_copy_items_from_temp(conn, store_num, date_sql)
            log.info("  [%s] %s — %d orders / %d checks / %d items inserted.",
                     store_num, store_name, result["orders"], result["checks"], inserted)
        finally:
            conn.close()

        result["ok"] = True

    except Exception as e:
        result["error"] = str(e)
        log.error("  [%s] %s — Unexpected error: %s", store_num, store_name, e,
                  exc_info=True)

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    overall_start = time.time()
    target_date   = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    log.info("=" * 70)
    log.info("NIGHTLY PULL — %s", target_date)
    log.info("=" * 70)

    # Load stores
    conn = db.get_toast_connection()
    try:
        raw_stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()

    stores = [s for s in raw_stores if s.get("guid") and s.get("store_num")]
    log.info("Found %d active Toast stores.", len(stores))

    if not stores:
        log.error("No stores found — aborting.")
        return

    failures = []

    # Pull all stores
    async with httpx.AsyncClient(timeout=120.0) as client:
        for s in stores:
            result = await pull_store(
                client,
                store_guid=str(s["guid"]),
                store_num=int(s["store_num"]),
                store_name=s.get("name", f"Store {s['store_num']}"),
                date_sql=target_date,
            )
            if not result["ok"]:
                failures.append(result)

    # Fix DayPartIDs BEFORE rollup so channel breakdown is correct
    log.info("-" * 70)
    log.info("Fixing DayPartIDs for %s…", target_date)
    try:
        conn = db.get_toast_connection()
        try:
            fixed = db.fix_daypart_ids(conn, target_date, target_date)
            log.info("DayPartID fix: %d rows updated.", fixed)
        finally:
            conn.close()
    except Exception as e:
        log.error("DayPartID fix FAILED: %s", e)

    # Rebuild rollup
    log.info("-" * 70)
    log.info("Rebuilding rollup for %s…", target_date)
    rollup_ok = False
    try:
        conn = db.get_toast_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart WHERE DateOfBusiness = ?",
                target_date,
            )
            log.info("Deleted %d stale rollup rows.", cur.rowcount)
            conn.commit()
            db.run_rollup(
                conn,
                start_date=target_date,
                end_date=target_date,
                store_grouping=STORE_GROUPING,
                store_group=STORE_GROUP,
            )
        finally:
            conn.close()
        rollup_ok = True
        log.info("Rollup complete.")
    except Exception as e:
        log.error("Rollup FAILED: %s", e)

    # Rebuild royalty for yesterday only
    log.info("-" * 70)
    log.info("Rebuilding royalty for %s…", target_date)
    royalty_ok = False
    try:
        conn = db.get_toast_connection()
        try:
            conn.execute(
                "EXEC dev_aloha.dbo.sp_tbl_Royalty_Values @StartDate=?, @EndDate=?",
                target_date, target_date,
            )
            conn.commit()
        finally:
            conn.close()
        royalty_ok = True
        log.info("Royalty complete.")
    except Exception as e:
        log.error("Royalty FAILED: %s", e)

    # Summary
    total_dur = time.time() - overall_start
    log.info("=" * 70)
    log.info("NIGHTLY PULL COMPLETE")
    log.info("  Date    : %s", target_date)
    log.info("  Stores  : %d total / %d failed", len(stores), len(failures))
    log.info("  Rollup  : %s", "OK" if rollup_ok else "FAILED")
    log.info("  Royalty : %s", "OK" if royalty_ok else "FAILED")
    log.info("  Runtime : %.1f minutes", total_dur / 60)
    log.info("=" * 70)

    if failures:
        log.warning("FAILED STORES (%d):", len(failures))
        for r in failures:
            log.warning("  Store %s — %s", r["store_num"], r["error"])
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
