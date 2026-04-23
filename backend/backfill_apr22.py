"""
backfill_apr22.py — repoll 2026-04-22 for all stores.

WHY THIS EXISTS
---------------
The PHP nightly puller missed 4/22 completely — SSRS Flash shows ~$1,011
for the entire chain (should be ~$679K).  This script polls Toast directly
via the Python client, writes fresh data to hstItem, rebuilds the rollup
for 4/22, and updates the Royalty table through 4/22.

PROCESSING ORDER
----------------
1. Truncate hstItem_temp (fresh slate)
2. Poll all live stores concurrently, DELETE old + write fresh per store
3. Run sp_Update_Sold_Items_From_Check (merge temp → hstItem)
4. Delete stale rollup rows + rebuild tbl_SalesDataByDayPart for 4/22
5. Run sp_tbl_Royalty_Values for 2026-04-13 → 2026-04-22

Safe to re-run: DELETE + fresh INSERT is idempotent.
"""

import asyncio
import logging
import os
import sys
import time
import traceback

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
log = logging.getLogger("backfill_apr22")
logging.getLogger("httpx").setLevel(logging.WARNING)

CONCURRENCY    = 8
STORE_GROUPING = 9
STORE_GROUP    = 243

DATE_SQL      = "2026-04-22"
ROYALTY_START = "2026-04-13"
ROYALTY_END   = "2026-04-22"


# ─── Per-store worker ────────────────────────────────────────────────────────

async def process_store(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    counter: dict,
    total: int,
    store_num: int,
    guid: str,
    name: str,
) -> tuple:
    date_yyyymmdd = DATE_SQL.replace("-", "")
    start = time.time()

    try:
        async with sem:
            orders, errors = await fetch_orders_with_retry(
                client, guid, store_num, date_yyyymmdd
            )
            if not orders and errors:
                return (store_num, False, f"fetch failed: {errors[0]}")
            if not orders:
                counter["done"] += 1
                log.info("[%d/%d] ✓ %s (%s) — 0 orders (empty day)",
                         counter["done"], total, store_num, name)
                return (store_num, True, "no orders")

            parsed = parse_orders(orders, guid, store_num, date_yyyymmdd)

            conn = db.get_toast_connection()
            try:
                db.upsert_orders(conn, parsed["orders"])
                db.upsert_checks(conn, parsed["checks"])
                db.upsert_items(
                    conn, parsed["items"],
                    skip_truncate=True,
                    skip_proc=True,
                )
            finally:
                conn.close()

    except Exception as e:
        tb = traceback.format_exc()
        log.error("[x] %s failed: %s: %s\n%s",
                  store_num, type(e).__name__, e, tb)
        return (store_num, False, f"error: {type(e).__name__}: {e}")

    dur = time.time() - start
    counter["done"] += 1
    log.info(
        "[%d/%d] ✓ %s (%s) — %d checks, %d items, %.1fs",
        counter["done"], total, store_num, name,
        len(parsed["checks"]), len(parsed["items"]), dur,
    )
    return (store_num, True, f"{len(parsed['checks'])} checks")


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    overall_start = time.time()

    log.info("=" * 70)
    log.info("BACKFILL 2026-04-22")
    log.info("=" * 70)

    # ── Load stores ──────────────────────────────────────────────────────────
    log.info("Loading live Toast store map…")
    conn = db.get_toast_connection()
    try:
        raw_stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()

    stores = [
        {"store_num": int(s["store_num"]), "guid": s["guid"], "name": s.get("name", "")}
        for s in raw_stores
        if s.get("store_num") and s.get("guid")
    ]
    log.info("Found %d live stores", len(stores))

    # ── Step 1: Truncate hstItem_temp ────────────────────────────────────────
    log.info("Truncating dbo.hstItem_temp…")
    conn = db.get_toast_connection()
    try:
        conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
        conn.commit()
    finally:
        conn.close()

    # ── Step 2: Poll all stores ──────────────────────────────────────────────
    log.info("Polling %d stores for %s…", len(stores), DATE_SQL)
    total   = len(stores)
    sem     = asyncio.Semaphore(CONCURRENCY)
    counter = {"done": 0}

    async with httpx.AsyncClient(timeout=120.0) as client:
        tasks = [
            process_store(client, sem, counter, total,
                          s["store_num"], s["guid"], s["name"])
            for s in stores
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    successes = [r for r in results if r[1]]
    failures  = [r for r in results if not r[1]]

    log.info("Fetch done — %d/%d succeeded, %d failed",
             len(successes), total, len(failures))
    if failures:
        log.warning("Failed stores:")
        for store_num, _ok, msg in failures:
            log.warning("  Store %s: %s", store_num, msg)

    # ── Step 3: Merge items ──────────────────────────────────────────────────
    log.info("-" * 70)
    log.info("Merging items (sp_Update_Sold_Items_From_Check)…")
    try:
        conn = db.get_toast_connection()
        try:
            db.run_items_merge_proc(conn)
        finally:
            conn.close()
    except Exception as e:
        log.error("Items merge failed: %s", e)

    # ── Step 4: Rebuild rollup ───────────────────────────────────────────────
    log.info("-" * 70)
    log.info("Deleting stale rollup rows and rebuilding for %s…", DATE_SQL)
    try:
        conn = db.get_toast_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                "WHERE DateOfBusiness = ?",
                DATE_SQL,
            )
            deleted = cur.rowcount
            conn.commit()
            log.info("Deleted %d stale rollup rows", deleted)
            db.run_rollup(
                conn,
                start_date=DATE_SQL,
                end_date=DATE_SQL,
                store_grouping=STORE_GROUPING,
                store_group=STORE_GROUP,
            )
        finally:
            conn.close()
    except Exception as e:
        log.error("Rollup rebuild failed: %s", e)

    # Verify rollup total
    try:
        conn = db.get_toast_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*), ISNULL(SUM(NetSales), 0) "
                "FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                "WHERE DateOfBusiness = ?",
                DATE_SQL,
            ).fetchone()
            rollup_rows = row[0] if row else 0
            rollup_net  = float(row[1]) if row else 0.0
            log.info("Rollup for %s: %d rows, $%s net sales",
                     DATE_SQL, rollup_rows, f"{rollup_net:,.2f}")
        finally:
            conn.close()
    except Exception as e:
        log.error("Could not verify rollup: %s", e)
        rollup_rows, rollup_net = 0, 0.0

    # ── Step 5: Royalty rebuild ──────────────────────────────────────────────
    log.info("-" * 70)
    log.info("Rebuilding royalty table for %s → %s…", ROYALTY_START, ROYALTY_END)
    try:
        conn = db.get_toast_connection()
        try:
            conn.execute(
                "EXEC dev_aloha.dbo.sp_tbl_Royalty_Values "
                "@StartDate = ?, @EndDate = ?",
                ROYALTY_START, ROYALTY_END,
            )
            conn.commit()
            log.info("sp_tbl_Royalty_Values complete")
        finally:
            conn.close()
    except Exception as e:
        log.error("Royalty rebuild failed: %s", e)

    # ── Final summary ────────────────────────────────────────────────────────
    total_dur = time.time() - overall_start
    print("\n" + "=" * 70)
    print("APR 22 BACKFILL COMPLETE")
    print("=" * 70)
    print(f"  Date        : {DATE_SQL}")
    print(f"  Stores      : {total}  ({len(successes)} OK, {len(failures)} failed)")
    print(f"  Rollup rows : {rollup_rows}")
    print(f"  Rollup net  : ${rollup_net:,.2f}")
    print(f"  Wall clock  : {total_dur/60:.1f} minutes")
    if failures:
        print("\nFailed stores:")
        for store_num, _ok, msg in failures:
            print(f"  Store {store_num}: {msg}")
    print("")
    print(f"  Royalty rebuilt : {ROYALTY_START} → {ROYALTY_END}")
    print("  Re-run SSRS Flash + Royalty Reports to verify.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
