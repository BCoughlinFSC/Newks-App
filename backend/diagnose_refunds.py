#!/usr/bin/env python3
"""
diagnose_refunds.py — work out WHY "Monkey — Refunds by User" came back empty.

Three candidate explanations, and this tells them apart:
  A. There genuinely were no Corporate refunds in the window. Nothing to fix.
  B. Refunds exist, but their parent orders are missing from the local Monkey
     DB. rpt_refunds_by_user LEFT JOINs refunds -> orders and the store filter
     reads o.store_id, so a refund with no matching order has store_id = NULL
     and is dropped by ANY scope filter. This filter is new as of 2026-08-19.
  C. The refunds table isn't syncing at all.

    python diagnose_refunds.py                  # defaults to last completed Mon-Sun
    python diagnose_refunds.py 2026-08-10 2026-08-16
"""
import os
import sys
from datetime import date, timedelta

import monkey_db


def last_full_week(today: date):
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
    return last_sunday - timedelta(days=6), last_sunday


def main() -> int:
    if not os.path.exists(monkey_db.MONKEY_DB_PATH):
        print(f"No Monkey DB at {monkey_db.MONKEY_DB_PATH}")
        return 1

    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        s, e = last_full_week(date.today())
        start, end = s.isoformat(), e.isoformat()

    conn = monkey_db.get_connection()
    try:
        q = lambda sql, p=(): conn.execute(sql, p).fetchall()

        print("=" * 70)
        print(f"REFUNDS DIAGNOSTIC — window {start} .. {end}")
        print("=" * 70)

        total, lo, hi = q("SELECT COUNT(*), MIN(DATE(refund_date)), "
                          "MAX(DATE(refund_date)) FROM refunds")[0]
        print(f"\nrefunds table: {total:,} rows, {lo} .. {hi}")
        if not total:
            print("  -> (C) refunds table is EMPTY. Run: python sync_monkey.py --full")
            return 2

        print("\nRefunds per week, last 8 weeks (is the sync current?):")
        for wk, n in q("""SELECT DATE(refund_date, 'weekday 0', '-6 days') AS wk, COUNT(*)
                          FROM refunds
                          WHERE DATE(refund_date) >= DATE('now', '-56 days')
                          GROUP BY wk ORDER BY wk"""):
            print(f"    week of {wk}: {n:>4}")

        in_window = q("SELECT COUNT(*) FROM refunds "
                      "WHERE DATE(refund_date) BETWEEN ? AND ?", (start, end))[0][0]
        print(f"\nRefunds dated inside the window: {in_window}")
        if not in_window:
            print("  -> (A) No refunds at all that week. The empty report is CORRECT;")
            print("         SSRS would have had nothing to send either.")
            return 0

        # Does each refund resolve to an order, and therefore a store?
        rows = q("""SELECT COUNT(*),
                           SUM(CASE WHEN o.order_id IS NULL THEN 1 ELSE 0 END),
                           SUM(CASE WHEN o.store_id IS NULL THEN 1 ELSE 0 END)
                    FROM refunds r
                    LEFT JOIN orders o ON r.order_id = o.order_id
                    WHERE DATE(r.refund_date) BETWEEN ? AND ?""", (start, end))[0]
        n, no_order, no_store = rows
        print(f"  of those: {no_order} have NO matching order row, "
              f"{no_store} resolve to NO store_id")

        company = set(monkey_db.store_ids_by_scope("company") or [])
        detail = q("""SELECT o.store_id, s.store_code, s.store_name,
                             COALESCE(s.region_name,''), COUNT(*)
                      FROM refunds r
                      LEFT JOIN orders o ON r.order_id = o.order_id
                      LEFT JOIN stores s ON o.store_id = s.store_id
                      WHERE DATE(r.refund_date) BETWEEN ? AND ?
                      GROUP BY o.store_id ORDER BY COUNT(*) DESC""", (start, end))
        print("\nWhere those refunds actually land:")
        corp_n = 0
        for sid, code, name, region, cnt in detail:
            if sid is None:
                tag = "  <-- NO ORDER ROW -> dropped by any scope filter"
            elif sid in company:
                tag = "  <-- CORPORATE"
                corp_n += cnt
            else:
                tag = "  (franchise / excluded)"
            print(f"    {cnt:>3}  store={code or sid or '?'}  {name or '(unknown)'} "
                  f"[{region or 'no region'}]{tag}")

        print(f"\nCorporate refunds in window: {corp_n}")
        print(f"What the report returns   : "
              f"{len(monkey_db.rpt_refunds_by_user(start, end, store_ids=sorted(company)))}")
        print(f"Unscoped (all stores)     : "
              f"{len(monkey_db.rpt_refunds_by_user(start, end))}")

        print()
        if corp_n:
            print("-> Corporate refunds EXIST but the report is empty: real bug, send me this.")
            return 2
        if no_order:
            print(f"-> (B) {no_order} refund(s) have no parent order locally, so the scope")
            print("       filter drops them. Fix the sync (python sync_monkey.py --full)")
            print("       or loosen the filter. Send me this output.")
            return 2
        print("-> (A) Refunds exist that week but NONE are Corporate. Empty report is")
        print("       CORRECT for scope=company. Confirm against Monday's SSRS email:")
        print("       if that one had rows, SSRS's StoreGroup 79 differs from ours.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
