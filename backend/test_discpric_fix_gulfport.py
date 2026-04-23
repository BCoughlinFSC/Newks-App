"""
test_discpric_fix_gulfport.py

Polls ONLY Gulfport (1036) for 2026-04-17 with the corrected DiscPric logic
(DiscPric = price, not preDiscountPrice), then rebuilds the rollup for that date
and prints the result so you can verify against SSRS and Toast web.

Expected after fix:
  Toast web Net Sales (Gulfport 4/17) = $12,999.80
  SSRS should match (or be very close) after this runs.

Run:
  python test_discpric_fix_gulfport.py
"""

import asyncio
import logging
import os
import sys

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
log = logging.getLogger("test_fix_gulfport")
logging.getLogger("httpx").setLevel(logging.WARNING)

STORE_NUM      = 1036
DATE_SQL       = "2026-04-17"
STORE_GROUPING = 9
STORE_GROUP    = 243


async def main():
    # Look up Gulfport's GUID
    conn = db.get_toast_connection()
    try:
        stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()

    store_map = {int(s["store_num"]): s for s in stores if s.get("store_num")}
    if STORE_NUM not in store_map:
        print(f"ERROR: Store {STORE_NUM} not found in vw_live_toast_stores")
        return

    guid = store_map[STORE_NUM]["guid"]
    name = store_map[STORE_NUM].get("name", "")
    date_yyyymmdd = DATE_SQL.replace("-", "")

    log.info("=" * 60)
    log.info("TEST: Gulfport (%s) for %s", STORE_NUM, DATE_SQL)
    log.info("DiscPric fix: price only (preDiscountPrice ignored)")
    log.info("=" * 60)

    # ── Poll from Toast API ──────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=120.0) as client:
        orders, errors = await fetch_orders_with_retry(client, guid, STORE_NUM, date_yyyymmdd)

    if not orders and errors:
        print(f"ERROR: fetch failed — {errors[0]}")
        return

    log.info("Fetched %d orders", len(orders))
    parsed = parse_orders(orders, guid, STORE_NUM, date_yyyymmdd)
    log.info("Parsed: %d checks, %d items", len(parsed["checks"]), len(parsed["items"]))

    # ── Verify no bad rows in parsed items ──────────────────────────────
    bad = [(r["DiscPric"], r["Price"]) for r in parsed["items"] if r["DiscPric"] > r["Price"]]
    if bad:
        log.warning("WARNING: %d parsed items still have DiscPric > Price — fix may not be complete", len(bad))
    else:
        log.info("GOOD: 0 parsed items have DiscPric > Price")

    # ── Write to SQL Server ──────────────────────────────────────────────
    conn = db.get_toast_connection()
    try:
        # Truncate hstItem_temp (fresh slate for this one store)
        conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
        conn.commit()

        db.upsert_orders(conn, parsed["orders"])
        db.upsert_checks(conn, parsed["checks"])
        db.upsert_items(conn, parsed["items"], skip_truncate=True, skip_proc=True)
    finally:
        conn.close()

    # ── Run items merge proc ─────────────────────────────────────────────
    log.info("Merging items…")
    conn = db.get_toast_connection()
    try:
        db.run_items_merge_proc(conn)
    finally:
        conn.close()

    # ── Direct hstItem verification (proc-independent) ───────────────────
    # This is the ground truth.  If this matches Toast web, the DATA fix is
    # complete.  If the rollup still doesn't match, it means the proc
    # (sp_toast_SalesDataByDayPart) needs the Modifiers exclusion patch too
    # — run fix_sp_rollup_modifiers.py to apply that automatically.
    log.info("Direct hstItem net sales (Modifiers excluded)…")
    MODIFIERS_GUID = "a04745a1-7118-4cd9-9212-bc70971713f0"
    conn = db.get_toast_connection()
    try:
        drow = conn.execute(
            "SELECT "
            "  SUM(DiscPric) AS gross_net, "
            "  SUM(CASE WHEN SalesCategoryGUID = ? THEN DiscPric ELSE 0 END) AS mod_amt, "
            "  SUM(CASE WHEN SalesCategoryGUID = ? THEN 1 ELSE 0 END) AS mod_cnt "
            "FROM toast.dbo.hstItem "
            "WHERE StoreNum = ? AND DateOfBusiness = ? "
            "  AND Voided = 0 AND Deferred = 0 AND ModificationID = '0'",
            MODIFIERS_GUID, MODIFIERS_GUID, STORE_NUM, DATE_SQL,
        ).fetchone()
        gross_net  = float(drow[0] or 0)
        mod_amt    = float(drow[1] or 0)
        mod_cnt    = int(drow[2] or 0)
        direct_net = gross_net - mod_amt
    finally:
        conn.close()
    log.info(
        "hstItem gross (incl. Modifiers): $%.2f | Modifiers: $%.2f (%d items) | Net: $%.2f",
        gross_net, mod_amt, mod_cnt, direct_net,
    )

    # ── Verify hstItem bad rows are gone ────────────────────────────────
    conn = db.get_toast_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN DiscPric > Price THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN DiscPric > Price THEN DiscPric - Price ELSE 0 END) "
            "FROM toast.dbo.hstItem "
            "WHERE StoreNum = ? AND DateOfBusiness = ? AND Voided = 0 AND Deferred = 0",
            STORE_NUM, DATE_SQL,
        ).fetchone()
        item_rows   = row[0] or 0
        bad_rows    = row[1] or 0
        overstatement = float(row[2] or 0)
        log.info("hstItem after fix: %d rows, %d bad rows, $%.2f overstatement",
                 item_rows, bad_rows, overstatement)
    finally:
        conn.close()

    # ── Rebuild rollup for 4/17 ──────────────────────────────────────────
    log.info("Rebuilding rollup for %s…", DATE_SQL)
    conn = db.get_toast_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart WHERE DateOfBusiness = ?",
            DATE_SQL,
        )
        conn.commit()
        db.run_rollup(conn, DATE_SQL, DATE_SQL, STORE_GROUPING, STORE_GROUP)
    finally:
        conn.close()

    # ── Read Gulfport's rollup result ────────────────────────────────────
    conn = db.get_toast_connection()
    try:
        rows = conn.execute(
            "SELECT DayPartID, NetSales, CheckCount "
            "FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
            "WHERE RestaurantID = ? AND DateOfBusiness = ? "
            "ORDER BY DayPartID",
            STORE_NUM, DATE_SQL,
        ).fetchall()
        total_net = sum(float(r[1] or 0) for r in rows)
    finally:
        conn.close()

    # ── Print result ─────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"GULFPORT ({STORE_NUM}) — {DATE_SQL} — AFTER DISCPRIC + MODIFIERS FIX")
    print("=" * 65)

    print("\n  ROLLUP (sp_toast_SalesDataByDayPart → tbl_SalesDataByDayPart):")
    for r in rows:
        daypart = "Lunch" if r[0] == 1 else "Dinner" if r[0] == 3 else f"DayPart {r[0]}"
        print(f"    {daypart:<10} NetSales = ${float(r[1] or 0):>10,.2f}   ({r[2]} checks)")
    print(f"    {'TOTAL':<10} NetSales = ${total_net:>10,.2f}")

    print("\n  DIRECT hstItem calculation (proc-independent):")
    print(f"    Gross (incl. Modifiers)  : ${gross_net:>10,.2f}")
    print(f"    Modifiers ({mod_cnt:4d} items)   : ${mod_amt:>10,.2f}  (excluded)")
    print(f"    Net  (excl. Modifiers)   : ${direct_net:>10,.2f}")

    print("\n" + "-" * 65)
    print(f"  hstItem bad rows (DiscPric>Price): {bad_rows}")
    print(f"  hstItem overstatement            : ${overstatement:,.2f}")
    print("-" * 65)
    print(f"  Toast web Net Sales (expected)   : $12,999.80")

    diff_rollup  = total_net   - 12999.80
    diff_direct  = direct_net  - 12999.80
    print(f"  Rollup  diff vs Toast web        : ${diff_rollup:+,.2f}")
    print(f"  Direct  diff vs Toast web        : ${diff_direct:+,.2f}")
    print("")

    # Diagnose what's fixed and what isn't
    data_ok  = abs(diff_direct) < 1.00
    proc_ok  = abs(diff_rollup) < 1.00

    if data_ok and proc_ok:
        print("  ✓ BOTH DATA AND ROLLUP MATCH TOAST WEB — ALL FIXES CONFIRMED")
        print("  → Safe to run backfill_sales_apr17_19_clean.py for all stores.")
    elif data_ok and not proc_ok:
        print("  ✓ DATA is correct  (direct calc matches)")
        print("  ✗ ROLLUP is still off — sp_toast_SalesDataByDayPart needs the")
        print("    Modifiers exclusion patch.  Run fix_sp_rollup_modifiers.py.")
    elif not data_ok and not proc_ok:
        print("  ✗ Data is still off — check DiscPric fix in toast_puller.py.")
    else:
        print("  ? Unexpected state — rollup matches but direct calc doesn't.")

    print("=" * 65)
    if proc_ok:
        print("\nRefresh SSRS Flash Report for 4/17 and verify Gulfport = $12,999.80.")


if __name__ == "__main__":
    asyncio.run(main())
