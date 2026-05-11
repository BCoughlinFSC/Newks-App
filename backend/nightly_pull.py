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
    1. Delete the date's rows from hstItem / hstCheck / hstOrder
    2. Truncate hstItem_temp
    3. Fetch from Toast API
    4. Write orders → hstOrder, checks → hstCheck, items → hstItem_temp
    5. Direct copy hstItem_temp → hstItem (bypasses encrypted SP inflation)
  Then once for the day:
    6. fix_daypart_ids  ← MUST run before rollup for correct channel breakdown
    7. Rebuild rollup (tbl_SalesDataByDayPart)
    8. Rebuild royalty (sp_tbl_Royalty_Values) for that date only

USAGE
-----
  python nightly_pull.py                       # yesterday, all stores (cron mode)
  python nightly_pull.py 2026-05-10            # one date, all stores
  python nightly_pull.py 2026-05-10 --store 1076
                                               # one date, ONE store (fast SSRS test)
  python nightly_pull.py 2026-05-10 --store 1076,1069
                                               # one date, multiple stores
  python nightly_pull.py 2026-05-10 --store 1076 --skip-rollup --skip-royalty
                                               # fastest: hst tables only, no rollup

NOTES on --store mode
---------------------
  • DELETE is per-store, so other stores' data for the date is untouched.
  • Rollup rebuild (when not skipped) still pulls from ALL stores' hst rows
    for the date — your one-store pull combined with everyone else's existing
    rows.  Result: SSRS reflects fresh data for the tested store, stale for
    the rest.  That's the right shape for a single-store sanity check.
  • Use --skip-rollup if you just want to confirm the API pull populates
    hstItem/hstCheck/hstOrder correctly without touching SSRS-visible tables.

DO NOT run simultaneously with any repoll script.
hstItem_temp is a shared staging table — one process at a time.
"""

import argparse
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

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Pull Toast sales data into SQL Server for one date (default: yesterday).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "target_date", nargs="?",
        help="Business date YYYY-MM-DD (default: yesterday). Cron-friendly.",
    )
    ap.add_argument(
        "--store", "--stores", dest="store",
        help="Restrict pull to one or more store_num values (comma-separated). "
             "Example: --store 1076  or  --store 1076,1069",
    )
    ap.add_argument(
        "--skip-rollup", action="store_true",
        help="Skip DayPartID fix + tbl_SalesDataByDayPart rebuild (use for fast hst-table-only tests).",
    )
    ap.add_argument(
        "--skip-royalty", action="store_true",
        help="Skip sp_tbl_Royalty_Values rebuild.",
    )
    return ap.parse_args(argv)


async def main():
    overall_start = time.time()
    args = _parse_args(sys.argv[1:])

    if args.target_date:
        try:
            date.fromisoformat(args.target_date)
        except ValueError:
            print(f"ERROR: Invalid date '{args.target_date}'. Use YYYY-MM-DD format.")
            sys.exit(1)
        target_date = args.target_date
    else:
        target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Parse --store: comma-separated list of store_num ints
    store_filter: set[int] = set()
    if args.store:
        for token in args.store.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                store_filter.add(int(token))
            except ValueError:
                print(f"ERROR: --store value '{token}' is not an integer.")
                sys.exit(1)

    log.info("=" * 70)
    if store_filter:
        log.info("NIGHTLY PULL — %s — STORES %s (filtered)", target_date,
                 ",".join(str(s) for s in sorted(store_filter)))
    else:
        log.info("NIGHTLY PULL — %s — ALL STORES", target_date)
    if args.skip_rollup:
        log.info("  --skip-rollup set: hst tables only, NO rollup rebuild")
    if args.skip_royalty:
        log.info("  --skip-royalty set: NO royalty rebuild")
    log.info("=" * 70)

    # Load stores
    conn = db.get_toast_connection()
    try:
        raw_stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()

    stores = [s for s in raw_stores if s.get("guid") and s.get("store_num")]

    if store_filter:
        stores = [s for s in stores if int(s["store_num"]) in store_filter]
        missing = store_filter - {int(s["store_num"]) for s in stores}
        if missing:
            log.warning("--store values not found in vw_live_toast_stores: %s",
                        ",".join(str(m) for m in sorted(missing)))
        if not stores:
            log.error("No matching stores after --store filter — aborting.")
            sys.exit(1)
        log.info("Filtered to %d store(s).", len(stores))
    else:
        log.info("Found %d active Toast stores.", len(stores))

    if not stores:
        log.error("No stores found — aborting.")
        return

    failures = []

    # Pull selected stores
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

    # Fix DayPartIDs BEFORE rollup so channel breakdown is correct.
    # Skipped together with rollup since DayPartID fix is a prerequisite for the rollup.
    rollup_ok = None  # None = skipped, True/False = attempted
    if args.skip_rollup:
        log.info("-" * 70)
        log.info("DayPartID fix + rollup rebuild SKIPPED (--skip-rollup).")
    else:
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

    # Rebuild royalty for the target date only
    royalty_ok = None  # None = skipped
    if args.skip_royalty:
        log.info("-" * 70)
        log.info("Royalty rebuild SKIPPED (--skip-royalty).")
    else:
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
    if store_filter:
        log.info("  Filter  : --store %s",
                 ",".join(str(s) for s in sorted(store_filter)))
    log.info("  Stores  : %d total / %d failed", len(stores), len(failures))
    log.info("  Rollup  : %s", "SKIPPED" if rollup_ok is None
             else ("OK" if rollup_ok else "FAILED"))
    log.info("  Royalty : %s", "SKIPPED" if royalty_ok is None
             else ("OK" if royalty_ok else "FAILED"))
    log.info("  Runtime : %.1f minutes", total_dur / 60)
    log.info("=" * 70)

    if failures:
        log.warning("FAILED STORES (%d):", len(failures))
        for r in failures:
            log.warning("  Store %s — %s", r["store_num"], r["error"])
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
