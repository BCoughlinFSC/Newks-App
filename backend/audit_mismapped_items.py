"""
audit_mismapped_items.py — Compare a reviewed menu-item xlsx against the
production "Updated Mapping Table.xlsx" and produce an actionable diff report.

Background
----------
Kim periodically asks the menu team to review the Toast↔Aloha name pairs in the
production mapping table. They return a sheet (e.g. Menu_item_Names_Reviewed.xlsx)
with a "Mismatch Notes" column flagging two kinds of issues:

    ⚠ RED    – Different items: the Toast item is mapped to the WRONG Aloha
               item. Real bug — human must pick the correct Aloha item in the
               Mapping Table UI.
    ↔ YELLOW – Same item, naming convention differs: the mapping is correct,
               only the AlohaItemName display string is stylistically off.
               Optional cosmetic cleanup; cheap to ignore.

This script joins the reviewed sheet against the production xlsx on
normalized Toast_Item_Name, classifies each row, and prints a focused report
of the ⚠ RED rows with everything needed to find them in the UI:
ConsolidatedCat, Toast_Item_Name, current (wrong) AlohaItemName, ToastPLU,
AlohaPLU, MultiLocationId, item_guid.

Optional --rename-aloha mode applies the reviewer's "Aloha Item" string to
the AlohaItemName column for the ↔ YELLOW rows and writes a new xlsx
(Updated Mapping Table.renamed.xlsx) for you to eyeball before committing.

Usage
-----
    # audit only (default) — never writes anything
    python backend/audit_mismapped_items.py path/to/Menu_item_Names_Reviewed.xlsx

    # apply YELLOW cosmetic renames to a new xlsx (no overwrite)
    python backend/audit_mismapped_items.py path/to/Menu_item_Names_Reviewed.xlsx --rename-aloha
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

import pandas as pd


REVIEWED_COL_CAT      = "Consolidated Category"
REVIEWED_COL_NAME     = "Menu Item Name"
REVIEWED_COL_ALOHA    = "Aloha Item"
REVIEWED_COL_NOTES    = "Mismatch Notes"

PROD_COL_TOAST_NAME   = "Toast_Item_Name"
PROD_COL_ALOHA_NAME   = "AlohaItemName"
PROD_COL_CAT          = "ConsolidatedCat"
PROD_COL_TOAST_PLU    = "ToastPLU"
PROD_COL_ALOHA_PLU    = "AlohaPLU"
PROD_COL_MLI          = "MultiLocationId"
PROD_COL_GUID         = "item_guid"

RED_MARKER    = "⚠"
YELLOW_MARKER = "↔"


def _norm(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return re.sub(r"\s+", " ", str(s).strip()).lower()


def _find_prod_xlsx() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    candidates = [
        os.path.join(project_root, "Updated Mapping Table.xlsx"),
        os.path.join(here, "..", "Updated Mapping Table.xlsx"),
        os.path.join(os.getcwd(), "Updated Mapping Table.xlsx"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    raise SystemExit(
        "Could not find Updated Mapping Table.xlsx. Looked in:\n  "
        + "\n  ".join(candidates)
    )


def _classify(note: str) -> str:
    if not note:
        return "ok"
    if RED_MARKER in note:
        return "red"
    if YELLOW_MARKER in note:
        return "yellow"
    return "other"


def _load_reviewed(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    needed = {REVIEWED_COL_CAT, REVIEWED_COL_NAME, REVIEWED_COL_ALOHA, REVIEWED_COL_NOTES}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(f"Reviewed xlsx is missing columns: {sorted(missing)}")
    return df


def _load_prod(path: str) -> pd.DataFrame:
    # MultiLocationId is a 19-digit integer that exceeds float64 precision.
    # Force string dtype on the ID columns so we don't show rounded IDs in the
    # report (and don't lose precision if we ever write back).
    df = pd.read_excel(path, dtype={
        PROD_COL_MLI:       str,
        PROD_COL_TOAST_PLU: str,
        PROD_COL_ALOHA_PLU: str,
        PROD_COL_GUID:      str,
    })
    needed = {
        PROD_COL_TOAST_NAME, PROD_COL_ALOHA_NAME, PROD_COL_CAT,
        PROD_COL_TOAST_PLU, PROD_COL_ALOHA_PLU, PROD_COL_MLI, PROD_COL_GUID,
    }
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(f"Production xlsx is missing columns: {sorted(missing)}")
    return df


def _index_prod_by_toast_name(prod: pd.DataFrame) -> Dict[str, List[int]]:
    """Map normalized Toast_Item_Name → list of row indices (0-based in df)."""
    idx: Dict[str, List[int]] = defaultdict(list)
    for i, name in enumerate(prod[PROD_COL_TOAST_NAME]):
        k = _norm(name)
        if k:
            idx[k].append(i)
    return idx


def _fmt_id(v) -> str:
    """Render an MLI / PLU / guid cell without scientific-notation float drift."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    return str(v)


def _fmt_prod_row(prod: pd.DataFrame, i: int) -> dict:
    """Pull just the fields we need for the report from prod row i."""
    row = prod.iloc[i]
    return {
        "excel_row":         i + 2,  # +2: header row + 1-based
        "ConsolidatedCat":   row.get(PROD_COL_CAT, "") or "",
        "Toast_Item_Name":   row.get(PROD_COL_TOAST_NAME, "") or "",
        "AlohaItemName":     row.get(PROD_COL_ALOHA_NAME, "") or "",
        "ToastPLU":          _fmt_id(row.get(PROD_COL_TOAST_PLU, "")),
        "AlohaPLU":          _fmt_id(row.get(PROD_COL_ALOHA_PLU, "")),
        "MultiLocationId":   _fmt_id(row.get(PROD_COL_MLI, "")),
        "item_guid":         _fmt_id(row.get(PROD_COL_GUID, "")),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reviewed_xlsx", help="Path to the reviewed xlsx (e.g. Menu_item_Names_Reviewed.xlsx)")
    p.add_argument("--rename-aloha", action="store_true",
                   help="Apply YELLOW (cosmetic) renames to a NEW xlsx (does not overwrite the source).")
    p.add_argument("--out", default=None,
                   help="Output path for --rename-aloha (default: <prod>.renamed.xlsx alongside the source).")
    args = p.parse_args()

    reviewed_path = os.path.abspath(args.reviewed_xlsx)
    if not os.path.exists(reviewed_path):
        raise SystemExit(f"Reviewed xlsx not found: {reviewed_path}")

    prod_path = _find_prod_xlsx()
    print(f"Reviewed:   {reviewed_path}")
    print(f"Production: {prod_path}")
    print()

    reviewed = _load_reviewed(reviewed_path)
    prod = _load_prod(prod_path)

    prod_by_name = _index_prod_by_toast_name(prod)

    # Buckets
    red_rows:        List[Tuple[dict, dict]] = []   # (reviewed_dict, prod_row_dict)
    yellow_rows:     List[Tuple[dict, dict]] = []
    unmatched_red:   List[dict] = []                # reviewer said RED but no prod row
    unmatched_other: List[dict] = []                # reviewer said YELLOW/other, no prod row
    no_note:         int = 0

    # Track AlohaItemName edits we'd apply to the production xlsx
    edits: List[Tuple[int, str, str]] = []  # (prod_row_excel, old, new)

    for i, r in reviewed.iterrows():
        note = "" if pd.isna(r[REVIEWED_COL_NOTES]) else str(r[REVIEWED_COL_NOTES])
        cls = _classify(note)
        toast_name = "" if pd.isna(r[REVIEWED_COL_NAME]) else str(r[REVIEWED_COL_NAME])
        aloha_name = "" if pd.isna(r[REVIEWED_COL_ALOHA]) else str(r[REVIEWED_COL_ALOHA])
        cat = "" if pd.isna(r[REVIEWED_COL_CAT]) else str(r[REVIEWED_COL_CAT])

        rev = {
            "reviewed_row":      i + 2,
            "Consolidated Cat":  cat,
            "Menu Item Name":    toast_name,
            "Aloha Item":        aloha_name,
            "Note":              note,
        }

        if cls == "ok":
            no_note += 1
            continue

        key = _norm(toast_name)
        match_indices = prod_by_name.get(key, [])
        if not match_indices:
            (unmatched_red if cls == "red" else unmatched_other).append(rev)
            continue

        # If reviewer flagged the row, list each production row that shares
        # this Toast_Item_Name (multi-location items can have several).
        for mi in match_indices:
            prod_row = _fmt_prod_row(prod, mi)
            pair = (rev, prod_row)
            if cls == "red":
                red_rows.append(pair)
            elif cls == "yellow":
                yellow_rows.append(pair)
                # Queue cosmetic rename if production's AlohaItemName ≠ reviewer's Aloha Item
                if _norm(prod_row["AlohaItemName"]) != _norm(aloha_name) and aloha_name:
                    edits.append((mi, str(prod_row["AlohaItemName"] or ""), aloha_name))

    # ── Report ─────────────────────────────────────────────────────────────
    print(f"Reviewed rows total:           {len(reviewed)}")
    print(f"  rows with no Mismatch Note:  {no_note}")
    print(f"  ⚠ RED  (mapping error):       {len(red_rows)}")
    print(f"  ↔ YELLOW (naming differs):    {len(yellow_rows)}")
    print(f"  unmatched RED:               {len(unmatched_red)}  (RED-flagged but no production row)")
    print(f"  unmatched other:             {len(unmatched_other)}")
    print()

    if red_rows:
        print("=" * 78)
        print("⚠ RED — mapping errors. Open Admin & Data → Mapping Table and search")
        print("   for the Toast_Item_Name below, then correct the AlohaPLU/AlohaItemName.")
        print("=" * 78)
        for rev, pr in red_rows:
            print(f"  prod row {pr['excel_row']:>4}  |  {pr['ConsolidatedCat']}")
            print(f"     Toast_Item_Name : {pr['Toast_Item_Name']}")
            print(f"     wrong Aloha     : {pr['AlohaItemName']}  (PLU {pr['AlohaPLU']})")
            print(f"     MLI={pr['MultiLocationId']}  ToastPLU={pr['ToastPLU']}  item_guid={pr['item_guid']}")
            print(f"     reviewer note   : {rev['Note']}")
            print()

    if unmatched_red:
        print("=" * 78)
        print("⚠ UNMATCHED RED — reviewer flagged a mapping error but no production row")
        print("   carries this Toast_Item_Name. Either the item was renamed/removed")
        print("   upstream, or it's not in the mapping table yet. Investigate manually.")
        print("=" * 78)
        for rev in unmatched_red:
            print(f"  reviewed row {rev['reviewed_row']:>4}  |  {rev['Consolidated Cat']}")
            print(f"     Menu Item Name : {rev['Menu Item Name']}")
            print(f"     Aloha Item     : {rev['Aloha Item']}")
            print(f"     note           : {rev['Note']}")
            print()

    if unmatched_other:
        print("=" * 78)
        print(f"Unmatched (cosmetic): {len(unmatched_other)} YELLOW/other rows with no")
        print("production match. Likely renamed/removed. Not actionable as bugs.")
        print("=" * 78)

    if args.rename_aloha:
        if not edits:
            print("--rename-aloha: no YELLOW rows had a different AlohaItemName to apply.")
            return 0
        out_path = args.out or os.path.join(
            os.path.dirname(prod_path),
            os.path.splitext(os.path.basename(prod_path))[0] + ".renamed.xlsx",
        )
        # Apply edits
        new_prod = prod.copy()
        applied = 0
        for prod_idx, old, new in edits:
            current = new_prod.at[prod_idx, PROD_COL_ALOHA_NAME]
            if _norm(current) != _norm(new):
                new_prod.at[prod_idx, PROD_COL_ALOHA_NAME] = new
                applied += 1
        new_prod.to_excel(out_path, index=False)
        print(f"--rename-aloha: wrote {out_path}  ({applied} AlohaItemName edits)")
        print("Diff preview (first 25):")
        for prod_idx, old, new in edits[:25]:
            print(f"  prod row {prod_idx + 2:>4}  '{old}'  →  '{new}'")
        if len(edits) > 25:
            print(f"  … and {len(edits) - 25} more.")
        print()
        print("Review the file. If it looks right:")
        print(f"  1. mv \"{out_path}\" \"{prod_path}\"")
        print(f"  2. git add \"{prod_path}\" && git commit -m \"chore(mapping): cosmetic AlohaItemName cleanup\"")
        print(f"  3. git push, ssh to Cloudways, git pull, python backend/sync_mapping_excel_to_db.py, restart uvicorn.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
