"""
export_current_rollup.py
========================
Exports the current state of tbl_SalesDataByDayPart (what SSRS reads)
for 4/13–4/22 to a CSV, plus a focused Albany spot-check.

Run:
    python3.11 export_current_rollup.py

Outputs:
    current_rollup_4_13_4_22.csv  — per store per day net sales
    albany_detail_4_13_4_22.csv   — Albany (1154) daily breakdown vs hstItem
"""

import csv
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import toast_puller_db as db

START_DATE     = "2026-04-13"
END_DATE       = "2026-04-22"
ALBANY_STORE   = 1154
MODIFIERS_GUID = "a04745a1-7118-4cd9-9212-bc70971713f0"

OUT_ROLLUP = os.path.join(SCRIPT_DIR, "current_rollup_4_13_4_22.csv")
OUT_ALBANY = os.path.join(SCRIPT_DIR, "albany_detail_4_13_4_22.csv")


def sep(w=70): print("=" * w)


def main():
    sep()
    print("  Newk's — Current Rollup Export + Albany Spot-Check")
    sep()
    print()

    conn = db.get_toast_connection()
    print("  Connected to SQL-PROD.")
    print()

    # ── 1. Full rollup export ─────────────────────────────────────────────────
    print(f"  Querying tbl_SalesDataByDayPart {START_DATE} → {END_DATE}…")
    rows = conn.execute("""
        SELECT
            RestaurantID,
            DateOfBusiness,
            SUM(NetSales)   AS NetSales,
            SUM(CheckCount) AS CheckCount,
            COUNT(*)        AS DayPartRows
        FROM dev_aloha.dbo.tbl_SalesDataByDayPart
        WHERE DateOfBusiness BETWEEN ? AND ?
        GROUP BY RestaurantID, DateOfBusiness
        ORDER BY RestaurantID, DateOfBusiness
    """, START_DATE, END_DATE).fetchall()

    # Collect all dates in range for pivot
    all_dates = sorted(set(str(r[1]) for r in rows))
    store_data = {}   # store_id -> {date -> {net, checks, dp_rows}}

    for r in rows:
        sid       = str(r[0])
        biz_date  = str(r[1])
        net       = float(r[2]) if r[2] is not None else 0.0
        checks    = float(r[3]) if r[3] is not None else 0.0
        dp_rows   = int(r[4])   if r[4] is not None else 0
        if sid not in store_data:
            store_data[sid] = {}
        store_data[sid][biz_date] = {
            "net": net, "checks": checks, "dp_rows": dp_rows
        }

    # Write flat CSV: one row per store per date
    with open(OUT_ROLLUP, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "RestaurantID", "DateOfBusiness", "NetSales",
            "CheckCount", "DayPartRows", "Flag"
        ])
        for sid in sorted(store_data.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            for biz_date in all_dates:
                d = store_data[sid].get(biz_date)
                if d is None:
                    writer.writerow([sid, biz_date, "", "", "", "MISSING FROM ROLLUP"])
                else:
                    flag = ""
                    if d["dp_rows"] > 4:
                        flag = f"DUPLICATE DAYPARTS ({d['dp_rows']} rows)"
                    writer.writerow([
                        sid, biz_date,
                        f"{d['net']:.2f}",
                        f"{d['checks']:.0f}",
                        d["dp_rows"],
                        flag,
                    ])

    print(f"  Written: {OUT_ROLLUP}")
    print(f"  Stores: {len(store_data)}  |  Dates: {len(all_dates)}")

    # Chain totals per date
    print()
    print(f"  {'Date':<14} {'StoreCount':>10}  {'ChainTotal':>14}")
    print(f"  {'-'*14} {'-'*10}  {'-'*14}")
    for biz_date in all_dates:
        day_stores = sum(1 for sid in store_data if biz_date in store_data[sid])
        day_total  = sum(
            store_data[sid][biz_date]["net"]
            for sid in store_data
            if biz_date in store_data[sid]
        )
        print(f"  {biz_date:<14} {day_stores:>10}  ${day_total:>13,.2f}")

    # ── 2. Albany spot-check ──────────────────────────────────────────────────
    print()
    sep("-", 70)
    print(f"  Albany (1154) spot-check {START_DATE} → {END_DATE}")
    sep("-", 70)
    print()

    # 2a. tbl_SalesDataByDayPart for Albany
    albany_rollup = {}
    for biz_date in all_dates:
        d = store_data.get(str(ALBANY_STORE), {}).get(biz_date)
        albany_rollup[biz_date] = d["net"] if d else None

    # 2b. hstItem direct for Albany (standard filter)
    hst_rows = conn.execute("""
        SELECT
            DateOfBusiness,
            SUM(DiscPric)  AS TotalDiscPric,
            COUNT(*)       AS ItemCount
        FROM toast.dbo.hstItem
        WHERE StoreNum       = ?
          AND DateOfBusiness BETWEEN ? AND ?
          AND Voided         = 0
          AND Deferred       = 0
          AND ModificationID = '0'
          AND (SalesCategoryGUID IS NULL
               OR SalesCategoryGUID <> ?)
        GROUP BY DateOfBusiness
        ORDER BY DateOfBusiness
    """, ALBANY_STORE, START_DATE, END_DATE).fetchall()

    hst_by_date = {str(r[0]): (float(r[1]) if r[1] else 0.0, int(r[2]))
                   for r in hst_rows}

    # 2c. hstCheck direct for Albany
    chk_rows = conn.execute("""
        SELECT
            DateOfBusiness,
            SUM(Amount)    AS TotalAmount,
            COUNT(*)       AS CheckCnt
        FROM toast.dbo.hstCheck
        WHERE StoreNum       = ?
          AND DateOfBusiness BETWEEN ? AND ?
          AND Voided         = 0
          AND Deleted        = 0
        GROUP BY DateOfBusiness
        ORDER BY DateOfBusiness
    """, ALBANY_STORE, START_DATE, END_DATE).fetchall()

    chk_by_date = {str(r[0]): (float(r[1]) if r[1] else 0.0, int(r[2]))
                   for r in chk_rows}

    print(f"  {'Date':<14} {'Rollup':>12}  {'hstItem':>12}  {'hstCheck':>12}  "
          f"{'R-I Diff':>10}  {'Checks':>7}  Notes")
    print(f"  {'-'*14} {'-'*12}  {'-'*12}  {'-'*12}  {'-'*10}  {'-'*7}  -----")

    albany_csv_rows = []
    for biz_date in all_dates:
        rollup_val = albany_rollup.get(biz_date)
        hst_val, item_cnt = hst_by_date.get(biz_date, (None, 0))
        chk_val, chk_cnt  = chk_by_date.get(biz_date, (None, 0))

        r_str   = f"${rollup_val:,.2f}"  if rollup_val is not None else "MISSING"
        h_str   = f"${hst_val:,.2f}"    if hst_val    is not None else "NO DATA"
        c_str   = f"${chk_val:,.2f}"    if chk_val    is not None else "NO DATA"
        diff    = (rollup_val - hst_val) if (rollup_val is not None and hst_val is not None) else None
        d_str   = f"${diff:+,.2f}"      if diff is not None else "n/a"

        notes = []
        if rollup_val is None:
            notes.append("MISSING FROM ROLLUP")
        if hst_val is None:
            notes.append("NO hstItem DATA")
        if diff is not None and abs(diff) > 5:
            notes.append(f"Rollup vs hstItem gap ${diff:+,.2f}")
        if rollup_val is not None and hst_val is not None and rollup_val > hst_val * 1.05:
            notes.append("ROLLUP > hstItem BY >5% — check for PHP inflation")

        note_str = ";  ".join(notes) if notes else ""
        print(f"  {biz_date:<14} {r_str:>12}  {h_str:>12}  {c_str:>12}  "
              f"{d_str:>10}  {chk_cnt:>7}  {note_str}")

        albany_csv_rows.append({
            "Date":          biz_date,
            "Rollup":        f"{rollup_val:.2f}" if rollup_val is not None else "",
            "hstItem":       f"{hst_val:.2f}"    if hst_val    is not None else "",
            "hstCheck":      f"{chk_val:.2f}"    if chk_val    is not None else "",
            "RollupVshstItem": f"{diff:.2f}"     if diff       is not None else "",
            "CheckCount":    chk_cnt,
            "Notes":         note_str,
        })

    with open(OUT_ALBANY, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "Date", "Rollup", "hstItem", "hstCheck",
            "RollupVshstItem", "CheckCount", "Notes"
        ])
        writer.writeheader()
        writer.writerows(albany_csv_rows)

    print()
    print(f"  Written: {OUT_ALBANY}")

    conn.close()
    print()
    sep()
    print("  Done.")
    sep()
    print()


def sep(char="=", w=70):
    print(char * w)


if __name__ == "__main__":
    main()
