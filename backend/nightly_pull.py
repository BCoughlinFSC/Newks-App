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

  python nightly_pull.py --days-back 3         # repoll yesterday + 3 prior days
                                               # (4 distinct dates). Catches late
                                               # voids / next-day adjustments.
  python nightly_pull.py --days-back 7         # weekly catch-up (8 distinct dates)

WHY --days-back EXISTS
----------------------
Restaurants regularly adjust prior-day sales: voids/comps entered the next
morning, managers reconciling shifts, etc. The default 1-day pull only
captures yesterday's state at 3am, missing anything edited later. Running
a rolling window each night ensures the recent past stays accurate.

Recommended cron pattern:
  Mon-Sat: --days-back 3   (yesterday + 3 prior = 4 dates)
  Sunday : --days-back 7   (yesterday + 7 prior = 8 dates, weekly safety net)

When --days-back is set, the rollup + royalty rebuilds happen ONCE at the
end for the full date range (single proc call with start/end dates) rather
than once per date. Per-store API pulls run sequentially, oldest→newest,
so if the run dies partway we've at least caught up the oldest gaps first.

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
        "payments":   0,
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
        result["orders"]   = len(parsed["orders"])
        result["checks"]   = len(parsed["checks"])
        result["payments"] = len(parsed["payments"])
        result["items"]    = len(parsed["items"])

        # Write to SQL Server
        conn = db.get_toast_connection()
        try:
            if parsed["orders"]:
                db.upsert_orders(conn, parsed["orders"])
            if parsed["checks"]:
                db.upsert_checks(conn, parsed["checks"])
            # Always run — upsert_payments DELETEs stale rows for this store/date
            # even when there are zero new payments (100% cash day).
            db.upsert_payments(conn, parsed["payments"],
                               store_num=store_num, biz_date=date_sql)
            if parsed["items"]:
                db.upsert_items(conn, parsed["items"], skip_truncate=True, skip_proc=True)
        finally:
            conn.close()

        # Direct copy temp → hstItem (no encrypted SP, no inflation)
        conn = db.get_toast_connection()
        try:
            inserted = db.direct_copy_items_from_temp(conn, store_num, date_sql)
            log.info("  [%s] %s — %d orders / %d checks / %d payments / %d items inserted.",
                     store_num, store_name,
                     result["orders"], result["checks"], result["payments"], inserted)
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
        help="Business date YYYY-MM-DD (default: yesterday). Cron-friendly. "
             "Mutually exclusive with --days-back.",
    )
    ap.add_argument(
        "--days-back", type=int, default=0, metavar="N",
        help="Repoll yesterday and N prior days (N+1 distinct dates total). "
             "Catches late voids / next-day adjustments. "
             "Example: --days-back 3 pulls yesterday + 3 prior = 4 dates. "
             "Mutually exclusive with a positional target_date.",
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
    args = ap.parse_args(argv)

    # Guard against contradictory inputs
    if args.target_date and args.days_back:
        ap.error("Use EITHER a positional target_date OR --days-back, not both.")
    if args.days_back < 0:
        ap.error("--days-back must be 0 or greater.")
    return args


def _date_range(end_date: date, days_back: int) -> list[str]:
    """Return YYYY-MM-DD strings from oldest → newest, ending at end_date.
    days_back=0 returns just [end_date]; days_back=3 returns 4 dates."""
    return [
        (end_date - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days_back, -1, -1)  # e.g. 3,2,1,0 → oldest first
    ]


async def main():
    overall_start = time.time()
    args = _parse_args(sys.argv[1:])

    # ── Resolve the date range to pull ────────────────────────────────────
    # Three modes:
    #   1. Positional target_date → single date (back-compat / manual reruns)
    #   2. --days-back N → N+1 dates ending yesterday (rolling repoll)
    #   3. Neither → yesterday only (original default cron behavior)
    if args.target_date:
        try:
            date.fromisoformat(args.target_date)
        except ValueError:
            print(f"ERROR: Invalid date '{args.target_date}'. Use YYYY-MM-DD format.")
            sys.exit(1)
        target_dates: list[str] = [args.target_date]
    else:
        yesterday = date.today() - timedelta(days=1)
        target_dates = _date_range(yesterday, args.days_back)

    range_start = target_dates[0]
    range_end   = target_dates[-1]

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
    range_str = (range_start if range_start == range_end
                 else f"{range_start}..{range_end} ({len(target_dates)} dates)")
    if store_filter:
        log.info("NIGHTLY PULL — %s — STORES %s (filtered)", range_str,
                 ",".join(str(s) for s in sorted(store_filter)))
    else:
        log.info("NIGHTLY PULL — %s — ALL STORES", range_str)
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

    failures: list[dict] = []
    per_date_summary: list[tuple[str, int]] = []   # [(date, failure_count_for_date)]

    # ── Pull each date, oldest → newest ───────────────────────────────────
    # Order matters: if the run dies partway, oldest dates are already done,
    # so the next run can pick up from yesterday onward.
    async with httpx.AsyncClient(timeout=120.0) as client:
        for d in target_dates:
            log.info("-" * 70)
            log.info("Pulling %s (%d stores)…", d, len(stores))
            date_failures = 0
            for s in stores:
                result = await pull_store(
                    client,
                    store_guid=str(s["guid"]),
                    store_num=int(s["store_num"]),
                    store_name=s.get("name", f"Store {s['store_num']}"),
                    date_sql=d,
                )
                if not result["ok"]:
                    failures.append(result)
                    date_failures += 1
            per_date_summary.append((d, date_failures))

    # ── Rollup + royalty: ONE call covering the full range ───────────────
    # The existing procs already accept start/end date parameters, so a single
    # wide call handles a multi-date repoll without N round-trips.
    # Fix DayPartIDs BEFORE rollup so channel breakdown is correct.
    rollup_ok = None  # None = skipped, True/False = attempted
    if args.skip_rollup:
        log.info("-" * 70)
        log.info("DayPartID fix + rollup rebuild SKIPPED (--skip-rollup).")
    else:
        log.info("-" * 70)
        log.info("Fixing DayPartIDs for %s..%s…", range_start, range_end)
        try:
            conn = db.get_toast_connection()
            try:
                fixed = db.fix_daypart_ids(conn, range_start, range_end)
                log.info("DayPartID fix: %d rows updated.", fixed)
            finally:
                conn.close()
        except Exception as e:
            log.error("DayPartID fix FAILED: %s", e)

        log.info("-" * 70)
        log.info("Rebuilding rollup for %s..%s…", range_start, range_end)
        rollup_ok = False
        try:
            conn = db.get_toast_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                    "WHERE DateOfBusiness BETWEEN ? AND ?",
                    range_start, range_end,
                )
                log.info("Deleted %d stale rollup rows.", cur.rowcount)
                conn.commit()
                db.run_rollup(
                    conn,
                    start_date=range_start,
                    end_date=range_end,
                    store_grouping=STORE_GROUPING,
                    store_group=STORE_GROUP,
                )
            finally:
                conn.close()
            rollup_ok = True
            log.info("Rollup complete.")
        except Exception as e:
            log.error("Rollup FAILED: %s", e)

    # Rebuild royalty for the full range
    royalty_ok = None  # None = skipped
    if args.skip_royalty:
        log.info("-" * 70)
        log.info("Royalty rebuild SKIPPED (--skip-royalty).")
    else:
        log.info("-" * 70)
        log.info("Rebuilding royalty for %s..%s…", range_start, range_end)
        royalty_ok = False
        try:
            conn = db.get_toast_connection()
            try:
                conn.execute(
                    "EXEC dev_aloha.dbo.sp_tbl_Royalty_Values @StartDate=?, @EndDate=?",
                    range_start, range_end,
                )
                conn.commit()
            finally:
                conn.close()
            royalty_ok = True
            log.info("Royalty complete.")
        except Exception as e:
            log.error("Royalty FAILED: %s", e)

    # ── Summary ───────────────────────────────────────────────────────────
    total_dur = time.time() - overall_start
    log.info("=" * 70)
    log.info("NIGHTLY PULL COMPLETE")
    log.info("  Range   : %s", range_str)
    if store_filter:
        log.info("  Filter  : --store %s",
                 ",".join(str(s) for s in sorted(store_filter)))
    log.info("  Stores  : %d × %d dates = %d store-day attempts / %d failed",
             len(stores), len(target_dates), len(stores) * len(target_dates), len(failures))
    if len(target_dates) > 1:
        for d, fc in per_date_summary:
            log.info("    %s : %d failure(s)", d, fc)
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
