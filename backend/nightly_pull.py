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

# Default concurrency for the parallel API-fetch phase. Toast's published
# rate limits are generous; 5 concurrent stores keeps us well under any
# realistic cap while shaving runtime ~5x. Override per-run with
# --concurrency or the TOAST_PULL_CONCURRENCY env var.
DEFAULT_CONCURRENCY = int(os.getenv("TOAST_PULL_CONCURRENCY", "5"))


# ─── Phase 1: parallel API fetch (no DB writes) ───────────────────────────────
#
# Why split fetch from write: the write path uses hstItem_temp, a SHARED
# staging table that gets TRUNCATEd per-store. If we ran multiple writes
# concurrently, store A's TRUNCATE would wipe store B's just-loaded rows.
# So we fetch all stores in parallel (the slow part), then write them out
# one at a time (the safe part).

async def fetch_store_data(
    client: httpx.AsyncClient,
    store_guid: str,
    store_num: int,
    store_name: str,
    date_sql: str,
) -> dict:
    """Hit Toast API for one (store, date). Returns a dict with parsed
    payloads ready for the write phase. No DB writes happen here.

    On API failure returns an error dict — caller decides what to do.
    """
    date_yyyymmdd = date_sql.replace("-", "")
    result = {
        "store_num":  store_num,
        "store_guid": store_guid,
        "store_name": store_name,
        "date_sql":   date_sql,
        "ok":         False,
        "orders":     0,
        "checks":     0,
        "payments":   0,
        "items":      0,
        "error":      None,
        "parsed":     None,  # populated on success
    }

    try:
        orders, errors = await fetch_orders_with_retry(
            client, store_guid, store_num, date_yyyymmdd
        )
        if errors and not orders:
            result["error"] = str(errors[0])
            log.error("  [%s] API fetch failed: %s", store_num, errors[0])
            return result

        if not orders:
            # Closed-day case. Mark ok so write phase still runs its DELETE
            # (clears any stale rows from a prior partial run).
            result["ok"] = True
            result["parsed"] = {"orders": [], "checks": [], "payments": [], "items": []}
            return result

        parsed = parse_orders(orders, store_guid, store_num, date_yyyymmdd)
        result["orders"]   = len(parsed["orders"])
        result["checks"]   = len(parsed["checks"])
        result["payments"] = len(parsed["payments"])
        result["items"]    = len(parsed["items"])
        result["parsed"]   = parsed
        result["ok"]       = True
        return result
    except Exception as e:
        result["error"] = str(e)
        log.error("  [%s] %s — Fetch exception: %s",
                  store_num, store_name, e, exc_info=True)
        return result


# ─── Phase 2: serialized DB write (uses hstItem_temp staging) ─────────────────

def write_store_data(fetched: dict) -> dict:
    """Write one store's parsed payload to SQL Server. Runs sequentially
    across stores because hstItem_temp is shared. Returns a result dict
    with insert counts (compatible shape with the old pull_store result).
    """
    store_num  = fetched["store_num"]
    store_name = fetched["store_name"]
    date_sql   = fetched["date_sql"]
    parsed     = fetched["parsed"] or {}

    out = {
        "store_num":  store_num,
        "store_name": store_name,
        "date_sql":   date_sql,
        "ok":         False,
        "orders":     fetched.get("orders", 0),
        "checks":     fetched.get("checks", 0),
        "payments":   fetched.get("payments", 0),
        "items":      0,
        "error":      None,
    }

    # If the fetch failed, surface that error and don't touch the DB —
    # safer than the old behavior, which DELETEd before fetching and
    # could leave stores empty on API failure.
    if not fetched.get("ok"):
        out["error"] = fetched.get("error") or "fetch failed"
        return out

    try:
        # Delete stale rows (always — closed-day stores still benefit from
        # clearing partial leftover rows from a prior failed run).
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

        if not parsed.get("orders") and not parsed.get("items"):
            log.info("  [%s] %s — 0 orders (closed).", store_num, store_name)
            out["ok"] = True
            return out

        # Truncate the shared staging table — guarded by sequential
        # iteration in the caller.
        conn = db.get_toast_connection()
        try:
            conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
            conn.commit()
        finally:
            conn.close()

        # Write parsed payload
        conn = db.get_toast_connection()
        try:
            if parsed.get("orders"):
                db.upsert_orders(conn, parsed["orders"])
            if parsed.get("checks"):
                db.upsert_checks(conn, parsed["checks"])
            # Always run — upsert_payments DELETEs stale rows for this
            # store/date even when there are zero new payments (cash-only day).
            db.upsert_payments(conn, parsed.get("payments", []),
                               store_num=store_num, biz_date=date_sql)
            if parsed.get("items"):
                db.upsert_items(conn, parsed["items"], skip_truncate=True, skip_proc=True)
        finally:
            conn.close()

        # Direct copy temp → hstItem (no encrypted SP, no inflation)
        conn = db.get_toast_connection()
        try:
            inserted = db.direct_copy_items_from_temp(conn, store_num, date_sql)
            out["items"] = inserted
            log.info("  [%s] %s — %d orders / %d checks / %d payments / %d items inserted.",
                     store_num, store_name,
                     out["orders"], out["checks"], out["payments"], inserted)
        finally:
            conn.close()

        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
        log.error("  [%s] %s — Write exception: %s",
                  store_num, store_name, e, exc_info=True)
    return out


# ─── Backward-compat wrapper (still callable by other scripts) ───────────────
#
# A few existing repoll scripts in the repo call pull_store(...) directly.
# Keep it working by chaining fetch → write in the original sequential shape.

async def pull_store(
    client: httpx.AsyncClient,
    store_guid: str,
    store_num: int,
    store_name: str,
    date_sql: str,
) -> dict:
    fetched = await fetch_store_data(client, store_guid, store_num, store_name, date_sql)
    return write_store_data(fetched)


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
        help="Skip the tbl_SalesDataByDayPart rebuild (use for fast hst-table-only "
             "tests). The DayPartID fix always runs regardless — skipping it is "
             "what broke the Flash Report channel breakdown for May 4-29 2026.",
    )
    ap.add_argument(
        "--skip-royalty", action="store_true",
        help="Skip sp_tbl_Royalty_Values rebuild.",
    )
    ap.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
        help=f"Max concurrent Toast API fetches (default {DEFAULT_CONCURRENCY}, "
             f"or $TOAST_PULL_CONCURRENCY if set). Set to 1 to run fully sequentially.",
    )
    args = ap.parse_args(argv)

    # Guard against contradictory inputs
    if args.target_date and args.days_back:
        ap.error("Use EITHER a positional target_date OR --days-back, not both.")
    if args.days_back < 0:
        ap.error("--days-back must be 0 or greater.")
    if args.concurrency < 1:
        ap.error("--concurrency must be 1 or greater.")
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
    if args.concurrency > 1:
        log.info("  Concurrency: %d parallel API fetches", args.concurrency)
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
    #
    # Per-date flow has two phases:
    #   Phase 1 (PARALLEL): fetch from Toast API for all stores at once,
    #                       capped at args.concurrency concurrent fetches
    #   Phase 2 (SEQUENTIAL): write each store's payload to SQL Server one
    #                         at a time, because hstItem_temp is shared
    sem = asyncio.Semaphore(args.concurrency)

    async def fetch_with_sem(s, date_sql):
        async with sem:
            return await fetch_store_data(
                client,
                store_guid=str(s["guid"]),
                store_num=int(s["store_num"]),
                store_name=s.get("name", f"Store {s['store_num']}"),
                date_sql=date_sql,
            )

    async with httpx.AsyncClient(timeout=120.0) as client:
        for d in target_dates:
            log.info("-" * 70)
            log.info("Pulling %s (%d stores, %d concurrent)…",
                     d, len(stores), args.concurrency)
            date_start = time.time()

            # Phase 1: parallel fetch — return_exceptions=True keeps one
            # store's failure from cancelling the others mid-batch.
            fetch_tasks = [fetch_with_sem(s, d) for s in stores]
            fetched_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            fetch_dur = time.time() - date_start
            log.info("  Fetch phase done in %.1fs (%d store payloads).",
                     fetch_dur, len(fetched_results))

            # Phase 2: sequential write — also handles exceptions surfaced
            # by gather() (rare; usually only if a coroutine itself crashed).
            date_failures = 0
            write_start = time.time()
            for s, fr in zip(stores, fetched_results):
                if isinstance(fr, Exception):
                    failures.append({
                        "store_num":  int(s["store_num"]),
                        "store_name": s.get("name", f"Store {s['store_num']}"),
                        "ok":         False,
                        "error":      f"fetch raised {type(fr).__name__}: {fr}",
                    })
                    date_failures += 1
                    continue
                write_result = write_store_data(fr)
                if not write_result["ok"]:
                    failures.append(write_result)
                    date_failures += 1
            write_dur = time.time() - write_start
            log.info("  Write phase done in %.1fs (%d failures).",
                     write_dur, date_failures)
            per_date_summary.append((d, date_failures))

    # ── Rollup + royalty: ONE call covering the full range ───────────────
    # The existing procs already accept start/end date parameters, so a single
    # wide call handles a multi-date repoll without N round-trips.
    # Fix DayPartIDs BEFORE rollup so channel breakdown is correct.
    #
    # The DayPartID fix runs UNCONDITIONALLY — even with --skip-rollup.
    # It's one cheap idempotent UPDATE, and skipping it is how the May 4-29
    # 2026 incident happened: a backfill ran with --skip-rollup, left
    # hstItem.DayPartID NULL for 4.2M rows, and the rollup's DayPartID-joined
    # MERGEs silently dropped $8.46M of sales from every Flash Report channel
    # column (NULL never matches a MERGE join). Totals stayed right; every
    # order-mode line went dark. See fix_daypartid_may2026.sql.
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
        log.error("DayPartID fix FAILED: %s — Flash Report channel breakdown "
                  "will silently lose these dates until fix_daypart_ids is "
                  "re-run. Do not ignore this error.", e)

    rollup_ok = None  # None = skipped, True/False = attempted
    if args.skip_rollup:
        log.info("-" * 70)
        log.info("Rollup rebuild SKIPPED (--skip-rollup).")
    else:
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

    # ── Exit status ───────────────────────────────────────────────────────
    # Three outcomes, so the Integrations dashboard stops crying wolf when a
    # couple of flaky/closed stores miss but the reporting data is fine:
    #   0 SUCCESS — every store-day landed, rollup + royalty OK
    #   2 PARTIAL — rollup + royalty OK (or skipped) but some store-days missed;
    #               data is still usable for reporting (misses listed above)
    #   1 FAILED  — rollup or royalty FAILED, or a systemic share of stores
    #               failed (>25%) — something actually needs attention.
    # run_and_record.py + integrations_agent.py map exit 2 -> the amber
    # "partial" badge. NOTE: rollup/royalty failure is now RED even with zero
    # store misses (it previously exited 0 — a real false-green).
    attempts = len(stores) * len(target_dates)
    systemic = bool(attempts) and (len(failures) / attempts) > 0.25
    if rollup_ok is False or royalty_ok is False or systemic:
        why = []
        if rollup_ok is False:  why.append("rollup FAILED")
        if royalty_ok is False: why.append("royalty FAILED")
        if systemic:            why.append(f"{len(failures)}/{attempts} store-days failed (>25%)")
        log.error("RESULT: FAILED — %s.", "; ".join(why))
        sys.exit(1)
    if failures:
        log.warning("RESULT: PARTIAL — %d of %d store-days missed; rollup/royalty OK, "
                    "data usable for reporting.", len(failures), attempts)
        sys.exit(2)
    log.info("RESULT: SUCCESS — all %d store-days landed; rollup/royalty OK.", attempts)


if __name__ == "__main__":
    asyncio.run(main())
