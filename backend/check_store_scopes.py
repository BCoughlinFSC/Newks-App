#!/usr/bin/env python3
"""
check_store_scopes.py — confirm the Corporate/Franchise split actually works
before enabling the Orders Due subscriptions.

The SSRS subscriptions scoped themselves with StoreGroup 79 (Corporate) and 8
(Franchise), resolved through contacts.dbo.internal_StoreGroupMembers. The app
can't reach that table, so monkey_db.store_ids_by_scope() derives the same split
from Monkey's own stores.region_name — it looks for the word "company".

If Monkey ever renames those regions, the helper falls back to "all stores"
rather than returning nothing. That fallback is SAFE (over-reports rather than
silently emptying a report) but it would make the Corporate and Franchise
emails identical. This script tells you which case you're in.

    python check_store_scopes.py
"""
import sys
from collections import Counter

import monkey_db


def main() -> int:
    if not monkey_db.db_available():
        print("Monkey DB not available — run sync_monkey.py first.")
        return 1

    conn = monkey_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT store_id, store_code, store_name, COALESCE(region_name, '') "
            "FROM stores ORDER BY region_name, store_code"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No stores in the Monkey DB.")
        return 1

    print(f"{len(rows)} stores in the Monkey DB.\n")
    print("Region names as Monkey reports them:")
    for region, n in sorted(Counter(r[3] for r in rows).items()):
        flag = "  <-- counted as COMPANY" if "company" in region.lower() else ""
        print(f"   {n:4d}  {region or '(blank)'}{flag}")

    company   = monkey_db.store_ids_by_scope("company")
    franchise = monkey_db.store_ids_by_scope("franchise")

    print()
    if company is None:
        print("!! NO region name contains 'company'.")
        print("!! store_ids_by_scope() is falling back to ALL STORES, so the")
        print("!! Corporate and Franchise emails would be IDENTICAL.")
        print("!! Send me the region names above and I'll fix the matching rule.")
        return 2

    by_id = {r[0]: (r[1], r[2]) for r in rows}
    print(f"Corporate scope : {len(company)} stores")
    for sid in company[:8]:
        code, name = by_id.get(sid, ("?", "?"))
        print(f"     {code or '?':>6}  {name}")
    if len(company) > 8:
        print(f"     ... and {len(company) - 8} more")

    print(f"\nFranchise scope : {0 if franchise is None else len(franchise)} stores")
    for sid in (franchise or [])[:8]:
        code, name = by_id.get(sid, ("?", "?"))
        print(f"     {code or '?':>6}  {name}")
    if franchise and len(franchise) > 8:
        print(f"     ... and {len(franchise) - 8} more")

    print()
    if franchise is None:
        print("Every store matched 'company' — the Franchise email would be empty.")
        print("Check that against what SSRS actually sends before enabling it.")
        return 2

    overlap = set(company) & set(franchise)
    if overlap:
        print(f"!! {len(overlap)} store(s) in BOTH scopes — that shouldn't happen.")
        return 2

    print(f"Split looks clean: {len(company)} Corporate + {len(franchise)} Franchise "
          f"= {len(company) + len(franchise)} operating stores, no overlap.")
    print("Compare those counts against yesterday's SSRS emails before enabling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
