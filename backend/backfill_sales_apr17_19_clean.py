"""
backfill_sales_apr17_19_clean.py — clean repoll for 2026-04-17 through 04-21.

WHY THIS EXISTS
---------------
These dates have corrupted DiscPric data in hstItem.  The PHP puller
wrote rows via PostgreSQL staging where DiscPric > Price (impossible for
discounted items).  A targeted UPDATE by SelectionID couldn't touch them
because the PHP puller assigns different SelectionIDs than the Python client
gets directly from the Toast API.

FIX STRATEGY
------------
db.upsert_items() already runs:
    DELETE FROM hstItem WHERE StoreID = <guid> AND DateOfBusiness = <date>
before it writes any fresh rows into hstItem_temp.  That wipe removes ALL
corrupted PHP rows for the store/date — regardless of SelectionID — and
replaces them with correct Python data where DiscPric = price.

PROCESSING ORDER
----------------
For each date in DATES:
  1. Truncate hstItem_temp (fresh slate)
  2. Poll all live stores concurrently, DELETE old + write fresh per store
  3. Run sp_Update_Sold_Items_From_Check once (merge temp → hstItem)
  4. Delete stale rollup rows + rebuild tbl_SalesDataByDayPart for that date

After all dates:
  5. Run sp_tbl_Royalty_Values for ROYALTY_START → ROYALTY_END

Safe to re-run: DELETE + fresh INSERT is idempotent.

PAUSE / RESUME
--------------
Progress is saved to backfill_checkpoint.json after each date completes.
If the script is stopped (Ctrl+C or crash), re-running it automatically
skips already-completed dates and picks up from where it left off.

  Ctrl+C            — finishes the current date, saves checkpoint, then stops
  --reset           — clears the checkpoint and starts from the beginning
"""

import argparse
import asyncio
import json
import logging
import os
import signal
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
log = logging.getLogger("backfill_apr17_19_clean")
logging.getLogger("httpx").setLevel(logging.WARNING)

CONCURRENCY    = 8
STORE_GROUPING = 9
STORE_GROUP    = 243

# Dates to clean-repoll (order matters — processed sequentially)
DATES = ["2026-04-17", "2026-04-18", "2026-04-19", "2026-04-20", "2026-04-21", "2026-04-22"]

# Royalty rebuild covers the full affected window
ROYALTY_START = "2026-04-13"
ROYALTY_END   = "2026-04-22"

CHECKPOINT_FILE = os.path.join(SCRIPT_DIR, "backfill_checkpoint.json")

# ─── Pause / resume support ──────────────────────────────────────────────────

_stop_requested = False


def _handle_sigint(sig, frame):
    global _stop_requested
    if _stop_requested:
        log.warning("Force quit — exiting immediately.")
        sys.exit(1)
    log.warning("Stop requested — will finish the current date then pause.")
    log.warning("Press Ctrl+C again to force quit immediately.")
    _stop_requested = True


signal.signal(signal.SIGINT, _handle_sigint)


def load_checkpoint() -> set:
    """Return the set of dates already completed (from checkpoint file)."""
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
        completed = set(data.get("completed_dates", []))
        if completed:
            log.info("Checkpoint found — already completed: %s", ", ".join(sorted(completed)))
            log.info("Skipping those dates. Run with --reset to start over.")
        return completed
    except Exception as e:
        log.warning("Could not read checkpoint file: %s — starting fresh.", e)
        return set()


def save_checkpoint(completed_dates: set) -> None:
    """Save completed dates to the checkpoint file."""
    try:
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({
                "completed_dates": sorted(completed_dates),
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, indent=2)
    except Exception as e:
        log.warning("Could not save checkpoint: %s", e)


def clear_checkpoint() -> None:
    """Delete the checkpoint file (used with --reset)."""
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        log.info("Checkpoint cleared — will run all dates from scratch.")


# ─── Per-store worker ────────────────────────────────────────────────────────

async def process_store(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    counter: dict,
    total: int,
    date_sql: str,
    store_num: int,
    guid: str,
    name: str,
) -> tuple:
    date_yyyymmdd = date_sql.replace("-", "")
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
                # skip_truncate=True  → do NOT clear hstItem_temp (cleared once up front)
                # skip_proc=True      → do NOT run the merge proc per store (run once at end)
                # upsert_items still DELETEs hstItem rows for this store/date before inserting
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


# ─── One full date pass ──────────────────────────────────────────────────────

async def run_date(
    client: httpx.AsyncClient,
    date_sql: str,
    stores: list,
) -> dict:
    """Poll all stores for one date, merge items, rebuild rollup. Returns summary dict."""
    log.info("")
    log.info("=" * 70)
    log.info("DATE: %s", date_sql)
    log.info("=" * 70)

    # Truncate hstItem_temp — fresh slate for this date's run
    log.info("Truncating dbo.hstItem_temp…")
    conn = db.get_toast_connection()
    try:
        conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
        conn.commit()
    finally:
        conn.close()

    total   = len(stores)
    sem     = asyncio.Semaphore(CONCURRENCY)
    counter = {"done": 0}
    date_start = time.time()

    tasks = [
        process_store(
            client, sem, counter, total,
            date_sql, s["store_num"], s["guid"], s["name"],
        )
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

    # Items merge
    log.info("-" * 70)
    log.info("Merging items for %s…", date_sql)
    try:
        conn = db.get_toast_connection()
        try:
            db.run_items_merge_proc(conn)
        finally:
            conn.close()
    except Exception as e:
        log.error("Items merge failed: %s", e)

    # Rollup rebuild for this date
    log.info("-" * 70)
    log.info("Deleting stale rollup rows and rebuilding for %s…", date_sql)
    try:
        conn = db.get_toast_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                "WHERE DateOfBusiness = ?",
                date_sql,
            )
            deleted = cur.rowcount
            conn.commit()
            log.info("Deleted %d stale rollup rows for %s", deleted, date_sql)
            db.run_rollup(
                conn,
                start_date=date_sql,
                end_date=date_sql,
                store_grouping=STORE_GROUPING,
                store_group=STORE_GROUP,
            )
        finally:
            conn.close()
    except Exception as e:
        log.error("Rollup rebuild failed for %s: %s", date_sql, e)

    # Verify rollup total
    try:
        conn = db.get_toast_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*), ISNULL(SUM(NetSales), 0) "
                "FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                "WHERE DateOfBusiness = ?",
                date_sql,
            ).fetchone()
            rollup_rows = row[0] if row else 0
            rollup_net  = float(row[1]) if row else 0.0
            log.info("Rollup for %s: %d rows, $%s net sales",
                     date_sql, rollup_rows, f"{rollup_net:,.2f}")
        finally:
            conn.close()
    except Exception as e:
        log.error("Could not verify rollup for %s: %s", date_sql, e)
        rollup_rows, rollup_net = 0, 0.0

    dur = time.time() - date_start
    return {
        "date": date_sql,
        "stores": total,
        "successes": len(successes),
        "failures": len(failures),
        "failed_stores": [(r[0], r[2]) for r in failures],
        "rollup_rows": rollup_rows,
        "rollup_net": rollup_net,
        "wall_minutes": dur / 60,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    # ── Parse args ───────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="Backfill Toast sales data.")
    parser.add_argument("--reset", action="store_true",
                        help="Clear checkpoint and re-run all dates from scratch.")
    args = parser.parse_args()

    if args.reset:
        clear_checkpoint()

    overall_start = time.time()

    # ── Load checkpoint ──────────────────────────────────────────────────────
    completed_dates = load_checkpoint()
    remaining = [d for d in DATES if d not in completed_dates]

    if not remaining:
        log.info("All dates already completed per checkpoint.")
        log.info("Run with --reset to re-run everything from scratch.")
        return

    log.info("Dates to process: %s", ", ".join(remaining))

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

    date_summaries = []
    paused = False

    async with httpx.AsyncClient(timeout=120.0) as client:
        for date_sql in remaining:
            if _stop_requested:
                log.info("Stop flag set — pausing before %s.", date_sql)
                paused = True
                break

            summary = await run_date(client, date_sql, stores)
            date_summaries.append(summary)

            # Save checkpoint after each successful date
            completed_dates.add(date_sql)
            save_checkpoint(completed_dates)
            log.info("Checkpoint saved — %s complete.", date_sql)

            if _stop_requested:
                log.info("Stop flag set — pausing after %s.", date_sql)
                paused = True
                break

    if paused:
        still_remaining = [d for d in DATES if d not in completed_dates]
        print("\n" + "=" * 70)
        print("BACKFILL PAUSED")
        print("=" * 70)
        print(f"  Completed : {', '.join(sorted(completed_dates))}")
        print(f"  Remaining : {', '.join(still_remaining)}")
        print("")
        print("  Re-run the script to continue from where you left off:")
        print("    python backfill_sales_apr17_19_clean.py")
        print("")
        print("  Or start over from scratch:")
        print("    python backfill_sales_apr17_19_clean.py --reset")
        print("=" * 70)
        return

    # ── Royalty rebuild ──────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 70)
    log.info("Rebuilding royalty table for %s → %s…", ROYALTY_START, ROYALTY_END)
    log.info("=" * 70)
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
    print("APR 17-22 CLEAN BACKFILL COMPLETE")
    print("=" * 70)
    print(f"{'Date':<14} {'Stores':>6} {'OK':>5} {'Fail':>5} {'Rollup $':>14}")
    print("-" * 50)
    for s in date_summaries:
        print(f"{s['date']:<14} {s['stores']:>6} {s['successes']:>5} "
              f"{s['failures']:>5} ${s['rollup_net']:>13,.2f}")
    print("-" * 50)
    print(f"  Total wall clock : {total_dur/60:.1f} minutes")

    any_failures = any(s["failures"] > 0 for s in date_summaries)
    if any_failures:
        print("\nFailed stores:")
        for s in date_summaries:
            for store_num, msg in s["failed_stores"]:
                print(f"  {s['date']}  Store {store_num}: {msg}")

    print("")
    print("Royalty report rebuilt for", ROYALTY_START, "→", ROYALTY_END)
    print("Re-run SSRS Flash + Royalty Reports to verify all stores.")
    print("=" * 70)

    # Clear checkpoint now that everything is done
    clear_checkpoint()
    log.info("Checkpoint cleared — backfill fully complete.")


if __name__ == "__main__":
    asyncio.run(main())
