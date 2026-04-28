"""
repoll_chain_apr27.py — Chain-wide repoll for ALL Toast stores, 4/27.

WHAT THIS DOES
--------------
Repolls every active Toast store (store group 243) for 4/27,
replacing any inflated or incorrect hstItem/hstCheck/hstOrder data with
clean data from the Toast API, then rebuilds the rollup and royalty tables.

DESIGN
------
  • Date-first loop: all stores are processed for 4/27.
  • Rollup is rebuilt ONCE per date (after all stores for that date complete).
  • DayPartID fix runs automatically after rollup — no SSMS step needed.
  • Royalty table rebuilt ONCE at the end covering 4/13-4/27.
  • Failures on individual stores/dates are logged and skipped; the chain
    continues. Failed store/dates appear in the final summary.
  • Uses direct_copy_items_from_temp — sp_Update_Sold_Items_From_Check
    is bypassed entirely (encrypted, causes ~8% inflation).

RESUME SUPPORT
--------------
  Progress is saved to repoll_chain_apr27_progress.json after every store/date
  and after every rollup rebuild. If the script is interrupted, re-run it
  and it will skip everything already completed and pick up where it left off.

  To force a complete re-run from scratch, delete repoll_chain_apr27_progress.json.

IMPORTANT: DO NOT run multiple instances simultaneously.
hstItem_temp is a shared staging table — one process at a time.

Runtime estimate: ~3-4 hours for a full chain (~97 stores × 1 date).
Safe to re-run: DELETE + fresh INSERT is idempotent.
"""

import asyncio
import json
import logging
import os
import sys
import time

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
log = logging.getLogger("repoll_chain")
logging.getLogger("httpx").setLevel(logging.WARNING)

DATES          = ["2026-04-27"]
ROYALTY_START  = "2026-04-13"
ROYALTY_END    = "2026-04-27"
STORE_GROUPING = 9
STORE_GROUP    = 243
STATE_FILE     = os.path.join(SCRIPT_DIR, "repoll_chain_apr27_progress.json")


# ─── Progress state helpers ───────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            log.info("Loaded progress file: %d store/dates done, %d rollups done.",
                     len(state.get("completed_store_dates", [])),
                     len(state.get("completed_date_rollups", [])))
            return state
        except Exception as e:
            log.warning("Could not read progress file (%s) — starting fresh.", e)
    return {
        "completed_store_dates": [],
        "completed_date_rollups": [],
        "royalty_done": False,
        "started_at": None,
    }


def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("Could not save progress file: %s", e)


def mark_store_date_done(state: dict, store_num: int, date_sql: str):
    key = f"{store_num}:{date_sql}"
    if key not in state["completed_store_dates"]:
        state["completed_store_dates"].append(key)
    save_state(state)


def mark_rollup_done(state: dict, date_sql: str):
    if date_sql not in state["completed_date_rollups"]:
        state["completed_date_rollups"].append(date_sql)
    save_state(state)


def is_store_date_done(state: dict, store_num: int, date_sql: str) -> bool:
    return f"{store_num}:{date_sql}" in state["completed_store_dates"]


def is_rollup_done(state: dict, date_sql: str) -> bool:
    return date_sql in state["completed_date_rollups"]


# ─── Process one store for one date ──────────────────────────────────────────

async def repoll_store_date(
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
        "date":       date_sql,
        "ok":         False,
        "orders":     0,
        "checks":     0,
        "items":      0,
        "net_sales":  0.0,
        "rollup_ok":  False,
        "skipped":    False,
        "error":      None,
    }

    try:
        # ── Delete stale rows ─────────────────────────────────────────────────
        conn = db.get_toast_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM toast.dbo.hstItem  WHERE StoreNum=? AND DateOfBusiness=?",
                store_num, date_sql,
            )
            deleted_items = cur.rowcount
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
        log.info("    [%s %s] Deleted %d stale hstItem rows.",
                 store_num, date_sql, deleted_items)

        # ── Truncate staging table ────────────────────────────────────────────
        conn = db.get_toast_connection()
        try:
            conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
            conn.commit()
        finally:
            conn.close()

        # ── Poll Toast API ────────────────────────────────────────────────────
        fetch_start = time.time()
        orders, errors = await fetch_orders_with_retry(
            client, store_guid, store_num, date_yyyymmdd
        )

        if errors and not orders:
            result["error"] = str(errors[0])
            log.error("    [%s %s] API fetch failed: %s",
                      store_num, date_sql, errors[0])
            return result

        if not orders:
            log.info("    [%s %s] 0 orders returned — treating as closed.",
                     store_num, date_sql)
            result["ok"] = True
            return result

        parsed = parse_orders(orders, store_guid, store_num, date_yyyymmdd)
        result["orders"] = len(parsed["orders"])
        result["checks"] = len(parsed["checks"])
        result["items"]  = len(parsed["items"])
        log.info("    [%s %s] Fetched %.1fs — %d orders / %d checks / %d items",
                 store_num, date_sql, time.time() - fetch_start,
                 result["orders"], result["checks"], result["items"])

        # ── Write to staging ──────────────────────────────────────────────────
        conn = db.get_toast_connection()
        try:
            if parsed["orders"]:
                db.upsert_orders(conn, parsed["orders"])
            if parsed["checks"]:
                db.upsert_checks(conn, parsed["checks"])
            if parsed["items"]:
                db.upsert_items(
                    conn, parsed["items"],
                    skip_truncate=True,
                    skip_proc=True,
                )
        finally:
            conn.close()

        # ── Direct copy temp → hstItem ────────────────────────────────────────
        conn = db.get_toast_connection()
        try:
            inserted = db.direct_copy_items_from_temp(conn, store_num, date_sql)
            log.info("    [%s %s] Direct copy complete: %d rows → hstItem.",
                     store_num, date_sql, inserted)
        finally:
            conn.close()

        result["ok"] = True

    except Exception as e:
        result["error"] = str(e)
        log.error("    [%s %s] Unexpected error: %s", store_num, date_sql, e,
                  exc_info=True)

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    overall_start = time.time()

    state    = load_state()
    resuming = bool(state["completed_store_dates"] or state["completed_date_rollups"])

    log.info("=" * 70)
    log.info("CHAIN-WIDE REPOLL — ALL TOAST STORES")
    log.info("Dates : %s → %s  (%d dates)", DATES[0], DATES[-1], len(DATES))
    if resuming:
        log.info("RESUMING from previous run — %d store/dates already done, "
                 "%d rollups already done.",
                 len(state["completed_store_dates"]),
                 len(state["completed_date_rollups"]))
        log.info("(Delete %s to force a full restart)", STATE_FILE)
    else:
        log.info("Starting fresh run.")
        state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state)
    log.info("=" * 70)

    # ── Load all live Toast stores ────────────────────────────────────────────
    log.info("Loading active Toast stores from store group %s…", STORE_GROUP)
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

    all_results = {}

    # ── Main loop: date-first ─────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=120.0) as client:
        for date_sql in DATES:

            all_stores_complete = all(
                is_store_date_done(state, int(s["store_num"]), date_sql)
                for s in stores
            )
            if all_stores_complete and is_rollup_done(state, date_sql):
                log.info("")
                log.info("DATE %s — fully complete (all stores + rollup). Skipping.",
                         date_sql)
                try:
                    conn = db.get_toast_connection()
                    try:
                        rows = conn.execute("""
                            SELECT RestaurantID, ISNULL(SUM(NetSales), 0)
                            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
                            WHERE DateOfBusiness = ?
                            GROUP BY RestaurantID
                        """, date_sql).fetchall()
                        net_by_store = {str(r[0]): float(r[1]) for r in rows}
                    finally:
                        conn.close()
                    for s in stores:
                        sn = int(s["store_num"])
                        all_results[(sn, date_sql)] = {
                            "ok": True, "skipped": True,
                            "net_sales": net_by_store.get(str(sn), 0.0),
                            "rollup_ok": True, "error": None,
                        }
                except Exception as e:
                    log.warning("Could not fetch net_sales for skipped date %s: %s",
                                date_sql, e)
                continue

            log.info("")
            log.info("=" * 70)
            log.info("DATE: %s — processing %d stores", date_sql, len(stores))
            log.info("=" * 70)
            date_start = time.time()

            for s in stores:
                store_num  = int(s["store_num"])
                store_guid = str(s["guid"])
                store_name = s.get("name", f"Store {store_num}")

                if is_store_date_done(state, store_num, date_sql):
                    log.info("  → %s  %s  [already done — skipping]",
                             store_num, store_name)
                    all_results[(store_num, date_sql)] = {
                        "ok": True, "skipped": True,
                        "net_sales": 0.0, "rollup_ok": False, "error": None,
                    }
                    continue

                log.info("  → %s  %s", store_num, store_name)
                result = await repoll_store_date(
                    client, store_guid, store_num, store_name, date_sql
                )
                all_results[(store_num, date_sql)] = result

                if result["ok"]:
                    mark_store_date_done(state, store_num, date_sql)
                    log.info("    [%s %s] Progress saved.", store_num, date_sql)
                else:
                    log.warning("    [%s %s] Failed — will NOT mark as done.",
                                store_num, date_sql)

            # ── Rebuild rollup ONCE for this date ─────────────────────────────
            if is_rollup_done(state, date_sql):
                log.info("-" * 70)
                log.info("Rollup for %s already done — skipping.", date_sql)
            else:
                log.info("-" * 70)
                log.info("Rebuilding rollup for %s…", date_sql)
                rollup_ok = False

                # ── Fix DayPartIDs BEFORE rollup so channel breakdown is correct
                log.info("  Fixing DayPartIDs for %s…", date_sql)
                try:
                    conn = db.get_toast_connection()
                    try:
                        fixed = db.fix_daypart_ids(conn, date_sql, date_sql)
                        log.info("  DayPartID fix: %d rows updated.", fixed)
                    finally:
                        conn.close()
                except Exception as e:
                    log.error("  DayPartID fix FAILED for %s: %s", date_sql, e)

                try:
                    conn = db.get_toast_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                            "WHERE DateOfBusiness = ?",
                            date_sql,
                        )
                        log.info("  Deleted %d stale rollup rows.", cur.rowcount)
                        conn.commit()
                        db.run_rollup(
                            conn,
                            start_date=date_sql,
                            end_date=date_sql,
                            store_grouping=STORE_GROUPING,
                            store_group=STORE_GROUP,
                        )
                    finally:
                        conn.close()
                    rollup_ok = True
                    mark_rollup_done(state, date_sql)
                    log.info("  Rollup rebuild complete for %s (%.1f min elapsed).",
                             date_sql, (time.time() - date_start) / 60)
                except Exception as e:
                    log.error("  Rollup rebuild FAILED for %s: %s", date_sql, e)

                # Capture rollup NetSales per store for summary
                if rollup_ok:
                    try:
                        conn = db.get_toast_connection()
                        try:
                            rows = conn.execute("""
                                SELECT RestaurantID, ISNULL(SUM(NetSales), 0)
                                FROM dev_aloha.dbo.tbl_SalesDataByDayPart
                                WHERE DateOfBusiness = ?
                                GROUP BY RestaurantID
                            """, date_sql).fetchall()
                            net_by_store = {str(r[0]): float(r[1]) for r in rows}
                        finally:
                            conn.close()
                        for s in stores:
                            sn  = int(s["store_num"])
                            k   = (sn, date_sql)
                            net = net_by_store.get(str(sn), 0.0)
                            if k in all_results:
                                all_results[k]["net_sales"] = net
                                all_results[k]["rollup_ok"] = True
                            log.info("  Rollup %s / %s = $%s",
                                     sn, date_sql, f"{net:,.2f}")
                    except Exception as e:
                        log.error("  Rollup spot-check failed for %s: %s",
                                  date_sql, e)

    # ── Rebuild royalty for full range ────────────────────────────────────────
    log.info("")
    log.info("=" * 70)
    if state.get("royalty_done"):
        log.info("Royalty rebuild already done — skipping.")
        royalty_ok = True
    else:
        log.info("Rebuilding royalty table %s → %s…", ROYALTY_START, ROYALTY_END)
        royalty_ok = False
        try:
            conn = db.get_toast_connection()
            try:
                conn.execute(
                    "EXEC dev_aloha.dbo.sp_tbl_Royalty_Values "
                    "@StartDate=?, @EndDate=?",
                    ROYALTY_START, ROYALTY_END,
                )
                conn.commit()
            finally:
                conn.close()
            royalty_ok = True
            state["royalty_done"] = True
            save_state(state)
            log.info("sp_tbl_Royalty_Values complete.")
        except Exception as e:
            log.error("Royalty rebuild FAILED: %s", e)

    # ── Final summary ─────────────────────────────────────────────────────────
    total_dur = time.time() - overall_start
    print()
    print("=" * 70)
    print("CHAIN-WIDE REPOLL COMPLETE")
    print(f"  Date      : 2026-04-27")
    print(f"  Stores    : {len(stores)}")
    print(f"  Royalty   : {'rebuilt' if royalty_ok else 'FAILED'} "
          f"({ROYALTY_START} → {ROYALTY_END})")
    print(f"  Wall clock: {total_dur / 60:.1f} minutes")
    print("=" * 70)
    print()

    store_nums  = [int(s["store_num"]) for s in stores]
    store_names = {int(s["store_num"]): s.get("name", "") for s in stores}

    header = f"  {'Store':<8}  {'Name':<32}  {'04-27':>10}"
    print(header)
    print("  " + "-" * 56)

    for sn in store_nums:
        r = all_results.get((sn, "2026-04-27"))
        if r is None:
            cell = "N/A"
        elif not r.get("ok"):
            cell = "FAILED"
        elif r.get("skipped") and not r.get("rollup_ok"):
            cell = "SKIP/NO RLU"
        elif not r.get("rollup_ok"):
            cell = "NO ROLLUP"
        else:
            net  = r.get("net_sales", 0.0)
            flag = "*" if r.get("skipped") else " "
            cell = f"${net:,.0f}{flag}"
        print(f"  {sn:<8}  {store_names[sn][:32]:<32}  {cell:>10}")

    print()
    failed = [(k, v) for k, v in all_results.items()
              if v is not None and not v.get("ok") and not v.get("skipped")]
    if failed:
        print(f"  ⚠  FAILURES ({len(failed)} store/date combinations):")
        for (sn, d), r in failed:
            print(f"     Store {sn} / {d} — {r.get('error', 'unknown error')}")
        print()
        print("  → Re-run this script to retry failed store/dates.")
    else:
        print("  ✓  All stores completed successfully.")

    remaining = sum(
        1 for d in DATES for s in stores
        if not is_store_date_done(state, int(s["store_num"]), d)
    )
    if remaining == 0 and royalty_ok:
        print()
        print("  Progress file can now be deleted:")
        print(f"    del \"{STATE_FILE}\"")

    print()
    print("  NEXT STEPS:")
    print("  1. Run SSRS Flash + Royalty Reports for 4/27")
    print("  2. DayPartID fix ran automatically — no SSMS step needed")
    print("=" * 70)
    print()


if __name__ == "__main__":
    asyncio.run(main())
