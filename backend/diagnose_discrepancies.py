"""
diagnose_discrepancies.py
=========================
Point-in-time diagnostic for two known Toast sales data gaps at Newk's Eatery.
Run from the backend directory (or from anywhere — sys.path is patched below).

    python diagnose_discrepancies.py

Sections
--------
  1 — Albany GA (Store 1154) royalty vs Flash overage for 4/13–4/19
  2 — Chain-level 4/20 gap (pre-backfill: $550,028 Flash vs $557,359 My App)
  3 — Persistent Toast > Newk's gap stores (1005, 1004, 1041, 1020, 1029, 1017)
      — category breakdown for 4/21 to identify non-Modifier categories driving gap

No argparse — just run top to bottom.
"""

import os
import sys

# ── sys.path bootstrap ────────────────────────────────────────────────────────
# Ensure the backend directory is on the path so toast_puller_db imports cleanly
# regardless of where the script is invoked from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import toast_puller_db as db  # noqa: E402  (import after path manipulation)

# ── Constants ─────────────────────────────────────────────────────────────────
MODIFIERS_GUID = "a04745a1-7118-4cd9-9212-bc70971713f0"

ALBANY_STORE = 1154
ALBANY_START = "2026-04-13"
ALBANY_END   = "2026-04-19"
ALBANY_ROYALTY_EXPECTED = 57_718.11
ALBANY_APP_EXPECTED     = 56_902.43

GAP_DATE     = "2026-04-20"
GAP_FLASH_EXPECTED = 550_028.00
GAP_APP_EXPECTED   = 557_359.00

CATEGORY_STORES = [
    (1005, "Hattiesburg"),
    (1004, "Tuscaloosa"),
    (1041, "Starkville"),
    (1020, "Tupelo"),
    (1029, "Huntsville Whitesburg"),
    (1017, "Huntsville"),
]
CATEGORY_DATE = "2026-04-21"

# ── Helpers ───────────────────────────────────────────────────────────────────

def sep(char="=", width=72):
    print(char * width)


def header(title):
    sep()
    print(f"  {title}")
    sep()
    print()


def subheader(title):
    print()
    print(f"  --- {title} ---")
    print()


def fmt_dollar(val):
    if val is None:
        return "$<NULL>"
    return f"${val:,.2f}"


def flag(condition, msg):
    """Print a WARNING line if condition is True."""
    if condition:
        print(f"  *** WARNING: {msg}")


# =============================================================================
# SECTION 1 — Albany GA (Store 1154) royalty vs Flash gap
# =============================================================================

def section_1(conn):
    header("SECTION 1 — Albany GA (Store 1154) | Royalty vs Flash Gap | 4/13–4/19")

    print(f"  Expected (Royalty Report):  {fmt_dollar(ALBANY_ROYALTY_EXPECTED)}")
    print(f"  My App (hstItem rollup):    {fmt_dollar(ALBANY_APP_EXPECTED)}")
    print(f"  Overage in Royalty Report:  {fmt_dollar(ALBANY_ROYALTY_EXPECTED - ALBANY_APP_EXPECTED)}")
    print()

    # ------------------------------------------------------------------
    # 1a. tbl_SalesDataByDayPart — NetSales by day
    # ------------------------------------------------------------------
    subheader("1a. tbl_SalesDataByDayPart — NetSales by DateOfBusiness (store 1154)")
    try:
        rows = conn.execute("""
            SELECT
                DateOfBusiness,
                SUM(NetSales)  AS TotalNetSales,
                COUNT(*)       AS DayPartRowCount
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            WHERE StoreNum = ?
              AND DateOfBusiness BETWEEN ? AND ?
            GROUP BY DateOfBusiness
            ORDER BY DateOfBusiness
        """, ALBANY_STORE, ALBANY_START, ALBANY_END).fetchall()

        rollup_grand_total = 0.0
        if rows:
            print(f"  {'Date':<14} {'NetSales':>14}  {'DayPartRows':>12}  Notes")
            print(f"  {'-'*14} {'-'*14}  {'-'*12}  -----")
            for r in rows:
                biz_date, net_sales, dp_rows = r
                net_sales = float(net_sales) if net_sales is not None else 0.0
                rollup_grand_total += net_sales
                duplicate_flag = "  <-- POSSIBLE DUPLICATE DAYPARTS" if dp_rows > 4 else ""
                print(f"  {str(biz_date):<14} {fmt_dollar(net_sales):>14}  {dp_rows:>12}{duplicate_flag}")
        else:
            print("  No rows returned from tbl_SalesDataByDayPart for this range.")

        print()
        print(f"  tbl_SalesDataByDayPart grand total:  {fmt_dollar(rollup_grand_total)}")
        print(f"  Royalty Report expected:             {fmt_dollar(ALBANY_ROYALTY_EXPECTED)}")
        rollup_vs_royalty = rollup_grand_total - ALBANY_ROYALTY_EXPECTED
        print(f"  Difference (rollup - royalty):       {fmt_dollar(rollup_vs_royalty)}")
        flag(abs(rollup_vs_royalty) > 1.00,
             f"tbl_SalesDataByDayPart total differs from Royalty by {fmt_dollar(rollup_vs_royalty)}")

    except Exception as exc:
        print(f"  ERROR running tbl_SalesDataByDayPart query: {exc}")
        rollup_grand_total = None

    # ------------------------------------------------------------------
    # 1b. hstItem direct rollup — DiscPric by day (standard Modifiers filter)
    # ------------------------------------------------------------------
    subheader("1b. hstItem direct rollup — SUM(DiscPric) by DateOfBusiness (store 1154)")
    try:
        rows = conn.execute("""
            SELECT
                DateOfBusiness,
                SUM(DiscPric) AS TotalDiscPric,
                COUNT(*)      AS ItemRowCount
            FROM toast.dbo.hstItem
            WHERE StoreNum = ?
              AND DateOfBusiness BETWEEN ? AND ?
              AND Voided        = 0
              AND Deferred      = 0
              AND ModificationID = '0'
              AND (SalesCategoryGUID IS NULL
                   OR SalesCategoryGUID != ?)
            GROUP BY DateOfBusiness
            ORDER BY DateOfBusiness
        """, ALBANY_STORE, ALBANY_START, ALBANY_END, MODIFIERS_GUID).fetchall()

        hst_grand_total = 0.0
        if rows:
            print(f"  {'Date':<14} {'DiscPric':>14}  {'ItemRows':>10}")
            print(f"  {'-'*14} {'-'*14}  {'-'*10}")
            for r in rows:
                biz_date, disc_pric, item_rows = r
                disc_pric = float(disc_pric) if disc_pric is not None else 0.0
                hst_grand_total += disc_pric
                print(f"  {str(biz_date):<14} {fmt_dollar(disc_pric):>14}  {item_rows:>10}")
        else:
            print("  No rows returned from hstItem for this range.")

        print()
        print(f"  hstItem grand total (excl. Modifiers): {fmt_dollar(hst_grand_total)}")
        print(f"  My App expected:                       {fmt_dollar(ALBANY_APP_EXPECTED)}")
        hst_vs_app = hst_grand_total - ALBANY_APP_EXPECTED
        print(f"  Difference (hstItem - My App):         {fmt_dollar(hst_vs_app)}")
        flag(abs(hst_vs_app) > 1.00,
             f"hstItem total differs from My App figure by {fmt_dollar(hst_vs_app)}")

        if rollup_grand_total is not None:
            rollup_vs_hst = rollup_grand_total - hst_grand_total
            print()
            print(f"  Rollup vs hstItem spread (rollup - hstItem): {fmt_dollar(rollup_vs_hst)}")
            flag(rollup_vs_hst > 1.00,
                 "Rollup is HIGHER than hstItem — suggests extra rows or a category in rollup "
                 "that hstItem filter excludes")
            flag(rollup_vs_hst < -1.00,
                 "Rollup is LOWER than hstItem — suggests rollup is missing days or stores")

    except Exception as exc:
        print(f"  ERROR running hstItem direct rollup query: {exc}")
        hst_grand_total = None

    # ------------------------------------------------------------------
    # 1c. Check for ModificationID != '0' rows (items that could be double-counted)
    # ------------------------------------------------------------------
    subheader("1c. hstItem rows with ModificationID != '0' (store 1154, Voided=0, Deferred=0)")
    try:
        rows = conn.execute("""
            SELECT
                ModificationID,
                COUNT(*)      AS RowCount,
                SUM(DiscPric) AS TotalDiscPric
            FROM toast.dbo.hstItem
            WHERE StoreNum = ?
              AND DateOfBusiness BETWEEN ? AND ?
              AND Voided        = 0
              AND Deferred      = 0
              AND ModificationID != '0'
            GROUP BY ModificationID
            ORDER BY COUNT(*) DESC
        """, ALBANY_STORE, ALBANY_START, ALBANY_END).fetchall()

        if rows:
            total_mod_sales = 0.0
            print(f"  {'ModificationID':<40} {'RowCount':>10}  {'TotalDiscPric':>14}")
            print(f"  {'-'*40} {'-'*10}  {'-'*14}")
            for r in rows:
                mod_id, row_count, total = r
                total = float(total) if total is not None else 0.0
                total_mod_sales += total
                print(f"  {str(mod_id):<40} {row_count:>10}  {fmt_dollar(total):>14}")
            print()
            print(f"  Total DiscPric in ModificationID != '0' rows: {fmt_dollar(total_mod_sales)}")
            flag(total_mod_sales > 100,
                 f"Non-zero ModificationID rows sum to {fmt_dollar(total_mod_sales)} — "
                 "these are EXCLUDED from My App but may be included in Royalty rollup")
        else:
            print("  No rows with ModificationID != '0' found — not the source of the gap.")

    except Exception as exc:
        print(f"  ERROR running ModificationID check: {exc}")

    print()


# =============================================================================
# SECTION 2 — 4/20 chain-level gap
# =============================================================================

def section_2(conn):
    header("SECTION 2 — Chain-Level Gap on 4/20 | Post-Backfill State")

    print(f"  Pre-backfill Flash total:   {fmt_dollar(GAP_FLASH_EXPECTED)}")
    print(f"  My App Newk's total:        {fmt_dollar(GAP_APP_EXPECTED)}")
    print(f"  Gap (My App - Flash):       {fmt_dollar(GAP_APP_EXPECTED - GAP_FLASH_EXPECTED)}")
    print()

    # ------------------------------------------------------------------
    # 2a. tbl_SalesDataByDayPart — 4/20 chain total (current state)
    # ------------------------------------------------------------------
    subheader("2a. tbl_SalesDataByDayPart — Current 4/20 chain total")
    rollup_total_420 = None
    try:
        row = conn.execute("""
            SELECT
                SUM(NetSales)       AS ChainTotal,
                COUNT(DISTINCT StoreNum) AS StoreCount
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            WHERE DateOfBusiness = ?
        """, GAP_DATE).fetchone()

        if row:
            rollup_total_420 = float(row[0]) if row[0] is not None else 0.0
            store_count_420  = int(row[1])   if row[1] is not None else 0
            print(f"  Chain total (4/20):  {fmt_dollar(rollup_total_420)}")
            print(f"  Store count (4/20):  {store_count_420}")
            print(f"  My App expected:     {fmt_dollar(GAP_APP_EXPECTED)}")
            diff = rollup_total_420 - GAP_APP_EXPECTED
            print(f"  Difference:          {fmt_dollar(diff)}")
            flag(diff < -100,
                 f"Rollup is still {fmt_dollar(abs(diff))} BELOW My App — backfill may be incomplete")
            flag(diff > 100,
                 f"Rollup is {fmt_dollar(diff)} ABOVE My App — over-write or duplicate rows?")
        else:
            print("  No rows returned for 4/20.")

    except Exception as exc:
        print(f"  ERROR: {exc}")

    # ------------------------------------------------------------------
    # 2b. 4/19 baseline store list
    # ------------------------------------------------------------------
    subheader("2b. tbl_SalesDataByDayPart — Store list 4/19 (baseline)")
    stores_419 = set()
    stores_419_totals = {}
    try:
        rows = conn.execute("""
            SELECT StoreNum, SUM(NetSales) AS NetSales
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            WHERE DateOfBusiness = '2026-04-19'
            GROUP BY StoreNum
            ORDER BY StoreNum
        """).fetchall()

        for r in rows:
            stores_419.add(r[0])
            stores_419_totals[r[0]] = float(r[1]) if r[1] is not None else 0.0

        print(f"  Stores present on 4/19: {len(stores_419)}")

    except Exception as exc:
        print(f"  ERROR fetching 4/19 baseline: {exc}")

    # ------------------------------------------------------------------
    # 2c. 4/20 per-store breakdown + missing-store check
    # ------------------------------------------------------------------
    subheader("2c. tbl_SalesDataByDayPart — 4/20 per-store NetSales")
    stores_420 = set()
    try:
        rows = conn.execute("""
            SELECT StoreNum, SUM(NetSales) AS NetSales
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            WHERE DateOfBusiness = ?
            GROUP BY StoreNum
            ORDER BY StoreNum
        """, GAP_DATE).fetchall()

        if rows:
            print(f"  {'StoreNum':>10}  {'NetSales':>14}  {'4/19 Baseline':>15}  {'Diff':>12}  Notes")
            print(f"  {'-'*10}  {'-'*14}  {'-'*15}  {'-'*12}  -----")
            for r in rows:
                store_num, net_sales = r[0], float(r[1]) if r[1] is not None else 0.0
                stores_420.add(store_num)
                baseline = stores_419_totals.get(store_num, 0.0)
                diff = net_sales - baseline
                note = ""
                if baseline == 0.0:
                    note = "  <-- NOT IN 4/19 BASELINE"
                elif abs(diff) > 500:
                    note = f"  <-- LARGE SWING vs 4/19"
                print(f"  {store_num:>10}  {fmt_dollar(net_sales):>14}  {fmt_dollar(baseline):>15}  {fmt_dollar(diff):>12}{note}")
        else:
            print("  No rows returned for 4/20.")

        print()
        missing_from_420 = stores_419 - stores_420
        if missing_from_420:
            print(f"  Stores in 4/19 rollup MISSING from 4/20 rollup ({len(missing_from_420)} stores):")
            for s in sorted(missing_from_420):
                baseline = stores_419_totals.get(s, 0.0)
                print(f"    StoreNum {s} — 4/19 NetSales was {fmt_dollar(baseline)}")
        else:
            print("  No stores from 4/19 are missing from 4/20 rollup.")

    except Exception as exc:
        print(f"  ERROR fetching 4/20 per-store data: {exc}")

    # ------------------------------------------------------------------
    # 2d. hstItem direct total for 4/20 (all stores, standard filter)
    # ------------------------------------------------------------------
    subheader("2d. hstItem direct rollup — 4/20 chain total (standard Modifiers filter)")
    try:
        row = conn.execute("""
            SELECT
                SUM(DiscPric)            AS ChainTotal,
                COUNT(DISTINCT StoreNum) AS StoreCount
            FROM toast.dbo.hstItem
            WHERE DateOfBusiness  = ?
              AND Voided           = 0
              AND Deferred         = 0
              AND ModificationID   = '0'
              AND (SalesCategoryGUID IS NULL
                   OR SalesCategoryGUID != ?)
        """, GAP_DATE, MODIFIERS_GUID).fetchone()

        if row:
            hst_chain_420 = float(row[0]) if row[0] is not None else 0.0
            hst_stores_420 = int(row[1]) if row[1] is not None else 0
            print(f"  hstItem chain total (4/20):  {fmt_dollar(hst_chain_420)}")
            print(f"  Distinct stores in hstItem:  {hst_stores_420}")
            print(f"  My App expected:             {fmt_dollar(GAP_APP_EXPECTED)}")
            hst_vs_app = hst_chain_420 - GAP_APP_EXPECTED
            print(f"  Difference (hstItem - App):  {fmt_dollar(hst_vs_app)}")
            flag(abs(hst_vs_app) > 100,
                 f"hstItem raw total differs from My App by {fmt_dollar(hst_vs_app)}")

            if rollup_total_420 is not None:
                rollup_vs_hst = rollup_total_420 - hst_chain_420
                print()
                print(f"  Rollup vs hstItem spread:    {fmt_dollar(rollup_vs_hst)}")
                flag(abs(rollup_vs_hst) > 100,
                     f"Rollup and hstItem disagree by {fmt_dollar(rollup_vs_hst)} — "
                     "rollup proc may need re-run or missing stores in hstItem")
        else:
            print("  No rows returned from hstItem for 4/20.")

    except Exception as exc:
        print(f"  ERROR running hstItem 4/20 chain query: {exc}")

    print()


# =============================================================================
# SECTION 3 — Persistent Toast > Newk's gap stores
# =============================================================================

def section_3(conn):
    header("SECTION 3 — Persistent Toast > Newk's Gap Stores | Category Breakdown | 4/21")

    print("  Stores investigated:")
    for store_num, store_name in CATEGORY_STORES:
        print(f"    {store_num} — {store_name}")
    print()
    print(f"  Date: {CATEGORY_DATE}")
    print(f"  Modifiers GUID (excluded from My App): {MODIFIERS_GUID}")
    print()

    for store_num, store_name in CATEGORY_STORES:
        subheader(f"Store {store_num} — {store_name}")

        try:
            rows = conn.execute("""
                SELECT
                    ISNULL(sc.Category_Name, 'NULL/Unknown') AS CategoryName,
                    i.SalesCategoryGUID,
                    COUNT(*)         AS ItemCount,
                    SUM(i.DiscPric)  AS TotalDiscPric,
                    SUM(i.Price)     AS TotalPrice
                FROM toast.dbo.hstItem i
                LEFT JOIN toast.dbo.cfg_Sales_Cats sc
                    ON i.SalesCategoryGUID = sc.Category_Guid
                WHERE i.StoreNum        = ?
                  AND i.DateOfBusiness  = ?
                  AND i.Voided          = 0
                  AND i.Deferred        = 0
                  AND i.ModificationID  = '0'
                GROUP BY i.SalesCategoryGUID, sc.Category_Name
                ORDER BY SUM(i.DiscPric) DESC
            """, store_num, CATEGORY_DATE).fetchall()

            if not rows:
                print(f"  No hstItem rows found for store {store_num} on {CATEGORY_DATE}.")
                continue

            total_all_cats    = 0.0
            total_excl_mods   = 0.0
            unusual_categories = []

            print(f"  {'Category':<35} {'GUID':<38} {'Items':>7}  {'DiscPric':>14}  {'Price':>14}  Notes")
            print(f"  {'-'*35} {'-'*38} {'-'*7}  {'-'*14}  {'-'*14}  -----")

            for r in rows:
                cat_name    = r[0] or "NULL/Unknown"
                cat_guid    = str(r[1]) if r[1] is not None else "NULL"
                item_count  = int(r[2])
                disc_pric   = float(r[3]) if r[3] is not None else 0.0
                price       = float(r[4]) if r[4] is not None else 0.0

                total_all_cats += disc_pric

                is_modifier_row = (cat_guid == MODIFIERS_GUID)
                if not is_modifier_row:
                    total_excl_mods += disc_pric

                notes = []
                if is_modifier_row:
                    notes.append("MODIFIERS (excluded from My App)")
                if disc_pric < -0.01:
                    notes.append("NEGATIVE DiscPric")
                    unusual_categories.append((cat_name, cat_guid, disc_pric))
                if disc_pric > 0 and price > 0 and disc_pric > price * 1.05:
                    notes.append("DiscPric > Price (unusual)")
                    unusual_categories.append((cat_name, cat_guid, disc_pric))

                note_str = ";  ".join(notes)
                # Truncate long category names for alignment
                cat_display  = cat_name[:34]
                guid_display = cat_guid[:37]
                print(f"  {cat_display:<35} {guid_display:<38} {item_count:>7}  "
                      f"{fmt_dollar(disc_pric):>14}  {fmt_dollar(price):>14}  {note_str}")

            print()
            print(f"  Total all categories:           {fmt_dollar(total_all_cats)}")
            print(f"  Total excl. Modifiers GUID:     {fmt_dollar(total_excl_mods)}")
            gap_from_modifier_excl = total_all_cats - total_excl_mods
            print(f"  Modifiers-only contribution:    {fmt_dollar(gap_from_modifier_excl)}")

            if unusual_categories:
                print()
                print(f"  Unusual categories (negative or DiscPric > Price):")
                for cat_name, cat_guid, disc_pric in unusual_categories:
                    print(f"    '{cat_name}' ({cat_guid}) => {fmt_dollar(disc_pric)}")

            # Flag if there's a non-Modifier category with significant positive DiscPric
            # that could explain the Toast > Newk's gap
            potentially_extra = [
                r for r in rows
                if str(r[1]) != MODIFIERS_GUID
                and (r[0] is None or r[0] == "NULL/Unknown")
                and float(r[3] or 0) > 50
            ]
            if potentially_extra:
                print()
                print("  *** Uncategorized (NULL GUID) items with significant DiscPric:")
                for r in potentially_extra:
                    print(f"      NULL/Unknown GUID — {int(r[2])} items — {fmt_dollar(float(r[3] or 0))}")
                print("      These are excluded from My App filter but may appear in Toast netSalesAmount.")

        except Exception as exc:
            print(f"  ERROR querying hstItem for store {store_num}: {exc}")

        print()


# =============================================================================
# Main
# =============================================================================

def main():
    print()
    sep("=")
    print("  Newk's Eatery — Toast Sales Data Discrepancy Diagnostic")
    print(f"  Run date: see system clock")
    sep("=")
    print()

    try:
        conn = db.get_toast_connection()
        print("  Connected to SQL-PROD (toast database).")
        print()
    except Exception as exc:
        print(f"FATAL: Could not connect to SQL Server: {exc}")
        print("Check that .env in the backend directory has valid ALOHA_SQL_* credentials.")
        sys.exit(1)

    # Each section is wrapped in a top-level try/except so one failure doesn't
    # prevent the others from running.

    try:
        section_1(conn)
    except Exception as exc:
        print(f"\nFATAL ERROR in Section 1 (Albany gap): {exc}\n")

    try:
        section_2(conn)
    except Exception as exc:
        print(f"\nFATAL ERROR in Section 2 (4/20 chain gap): {exc}\n")

    try:
        section_3(conn)
    except Exception as exc:
        print(f"\nFATAL ERROR in Section 3 (persistent gap stores): {exc}\n")

    try:
        conn.close()
    except Exception:
        pass

    sep()
    print("  Diagnostic complete.")
    sep()
    print()


if __name__ == "__main__":
    main()
