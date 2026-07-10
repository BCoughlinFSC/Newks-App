"""
backfill_py_aloha_2025.py
=========================
One-time PY backfill for the 6 stores whose 2026 Prior-Year (2025) was
UNDER-counted in the Cloudways app vs SSRS, because the app's aloha_sales_data
cache is short on their PRE-cutover (Aloha-era) 2025 history.

Toast has no data for those days, so the only source is dev_aloha's rollup
(tbl_SalesDataByDayPart) — the exact same table SSRS reads. We pull each
store's Aloha-era 2025 days (DateOfBusiness < ToastLive), map them to the app's
aloha_sales_data schema, and push them via /api/admin/aloha-sales/push-rows.

Only Aloha-era dates are pushed, so the app's Toast-era days (its canonical
ERA / Royalty-Net basis) are left untouched. The push upserts per (date,store),
so no other store is affected.

Run on the box that has the SQL-PROD .env (dev-jan02 / wherever toast_puller_db
works) AND network access to the Cloudways app:

    python3.11 backfill_py_aloha_2025.py --token <APP_ADMIN_TOKEN>            # dry-run preview
    python3.11 backfill_py_aloha_2025.py --token <APP_ADMIN_TOKEN> --push     # actually push

After it runs, re-pull the app Daily Flash for 2026-07-08 and the 6 stores'
Prior-YR should jump up to (near) the SSRS figures — only the accepted -364/-365
date-window shift will remain.
"""
import argparse
import os
import sys
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import httpx

import toast_puller_db as db

BASE_URL = "https://phpstack-1474989-6237770.cloudwaysapps.com"
PUSH_URL = f"{BASE_URL}/api/admin/aloha-sales/push-rows"
BATCH_SIZE = 500

# The 6 stores whose PY (2025) undercounts vs SSRS (Aloha-era 2025 gap).
# Verify the printed RestaurantName matches before pushing.
STORE_IDS = [
    1001,  # Oxford MS (University)
    1016,  # Oxford MS (Galleria)
    1011,  # Jackson MS
    1008,  # Birmingham AL (Downtown)
    1042,  # Hot Springs AR
    1187,  # Athens GA (Baxter Street)
]

YEAR_START = "2025-01-01"
YEAR_END = "2025-12-31"

# Sum of every delivery-provider column so the app's single delivery_net_sales
# matches the SSRS DeliveryNet channel exactly.
DELIVERY_SUM = " + ".join([
    "DeliveryNetSales", "DispatchOLONetSales", "NwkDeliveryNetSales", "RailsOLONetSales",
    "BiteSquadNetSales", "DoorDashNetSales", "PostmatesNetSales", "GoogleNetSales",
    "DeliverClubNetSales", "UberEatsNetSales", "WaitrNet", "FavorNet", "NwkDelivNetDisp",
    "DoorDashNetDisp", "PostmatesNetDisp", "LyftNetDisp", "DeliverLogicNetDisp",
    "GrubHubRailsNet", "DispatchSkipCart", "DispatchRailsAI", "FoodsbyNet",
])


def get_toast_live(conn, store_id, as_of):
    row = conn.execute(
        "SELECT ToastLive FROM dev_aloha.dbo.get_StoresIncluded(9, '4', ?) WHERE StoreID = ?",
        as_of, store_id,
    ).fetchone()
    return row[0] if row else None


def pull_aloha_days(conn, store_id, toast_live):
    """Per-day Aloha-era rollup rows for a store, mapped to the app schema."""
    sql = f"""
        SELECT
            RestaurantID, MAX(RestaurantName) AS RestaurantName, DateOfBusiness,
            SUM(NetSales)             AS net_sales,
            SUM(OLONetSales)          AS olo_net_sales,
            SUM({DELIVERY_SUM})       AS delivery_net_sales,
            SUM(CateringNetSales)     AS catering_net_sales,
            SUM(GNGNetSales)          AS gng_net_sales,
            SUM(ToGoNetSales)         AS togo_net_sales,
            SUM(CurbSideNetSales)     AS curbside_net_sales,
            SUM(CurbSideOLONetSales)  AS curbside_olo_net_sales,
            SUM(DineInNetSales)       AS dinein_net_sales,
            SUM(CallInnetsales)       AS callin_net_sales,
            SUM(OLOCallInnetsales)    AS olo_callin_net_sales,
            SUM(CheckCount)           AS check_count,
            SUM(ChecksMinusCatering)  AS checks_minus_catering
        FROM dev_aloha.dbo.tbl_SalesDataByDayPart WITH (NOLOCK)
        WHERE RestaurantID = ?
          AND DateOfBusiness >= ?
          AND DateOfBusiness <= ?
          AND DateOfBusiness < ?           -- Aloha-era only (before Toast go-live)
        GROUP BY RestaurantID, DateOfBusiness
        ORDER BY DateOfBusiness
    """
    cols = ["location", "restaurant_name", "business_date",
            "net_sales", "olo_net_sales", "delivery_net_sales", "catering_net_sales",
            "gng_net_sales", "togo_net_sales", "curbside_net_sales", "curbside_olo_net_sales",
            "dinein_net_sales", "callin_net_sales", "olo_callin_net_sales",
            "check_count", "checks_minus_catering"]
    rows = []
    for r in conn.execute(sql, store_id, YEAR_START, YEAR_END, toast_live).fetchall():
        d = dict(zip(cols, r))
        d["location"] = str(d["location"])
        d["business_date"] = str(d["business_date"])[:10]
        for k in cols[3:]:
            d[k] = float(d[k] or 0)
        rows.append(d)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="App admin bearer token")
    ap.add_argument("--push", action="store_true", help="Actually push (default: dry-run preview)")
    ap.add_argument("--as-of", default=date.today().isoformat(), help="get_StoresIncluded as-of date")
    args = ap.parse_args()

    conn = db.get_toast_connection()
    print("Connected to SQL-PROD.\n")

    all_rows = []
    for sid in STORE_IDS:
        tl = get_toast_live(conn, sid, args.as_of)
        if tl is None:
            print(f"  !! store {sid}: not returned by get_StoresIncluded — SKIPPED")
            continue
        rows = pull_aloha_days(conn, sid, tl)
        name = rows[0]["restaurant_name"] if rows else "(no Aloha-era rows)"
        net = sum(r["net_sales"] for r in rows)
        print(f"  {sid}  {name:32}  ToastLive={str(tl)[:10]}  "
              f"Aloha days={len(rows):3}  net=${net:,.2f}")
        all_rows.extend(rows)
    conn.close()

    print(f"\nTotal rows to push: {len(all_rows)}  "
          f"(dates {min((r['business_date'] for r in all_rows), default='-')} "
          f"to {max((r['business_date'] for r in all_rows), default='-')})")

    if not args.push:
        print("\nDRY RUN — nothing pushed. Re-run with --push to apply.")
        return

    client = httpx.Client(timeout=120.0)
    pushed = 0
    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i:i + BATCH_SIZE]
        resp = client.post(PUSH_URL, headers={"Authorization": f"Bearer {args.token}"},
                           json={"rows": batch})
        if resp.status_code == 200:
            pushed += resp.json().get("rows_upserted", len(batch))
            print(f"  batch {i // BATCH_SIZE + 1}: {resp.status_code} {resp.json()}")
        else:
            print(f"  batch {i // BATCH_SIZE + 1}: ERROR {resp.status_code} {resp.text[:200]}")
            break
    print(f"\nDone. Rows pushed: {pushed}")


if __name__ == "__main__":
    main()
