"""
import_aloha_items.py — Load "Items by Category.xlsx" into aloha_items_master.

The xlsx has one sheet per Aloha consolidated category. Each sheet has the
columns:
    Category Name | Item Range | Item ID | Item Long Name

The sheet TITLE encodes the consolidated category ID, e.g.
    "Consolidated NA Bev (#9006)"  -> consol_id=9006

We map that ID back to the canonical "* Consolidated …" tag name via the
CONSOLIDATED_CATEGORY_IDS table in toast_config_db so the Mapping Table UI
can filter the Aloha dropdown to match the row's consolidated_cat.

Section header rows (long names like "***Dressing***", "*WheatSandwich*",
"**GrabNGo Bundle**") are skipped — they're visual dividers within Aloha,
not real items.

Usage (re-runnable; full snapshot replace):
    python backend/import_aloha_items.py
    python backend/import_aloha_items.py --xlsx "path/to/Items by Category.xlsx"
    python backend/import_aloha_items.py --dry-run
"""

import argparse
import os
import re
import sys
from typing import Dict, List, Optional

import pandas as pd

import toast_config_db


_SHEET_ID_RE = re.compile(r"#(\d+)\)?\s*$")  # captures "9006" from "Consolidated NA Bev (#9006)"


def _find_xlsx() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    candidates = [
        os.path.join(project_root, "Items by Category.xlsx"),
        os.path.join(here, "..", "Items by Category.xlsx"),
        os.path.join(os.getcwd(), "Items by Category.xlsx"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise SystemExit(
        "Could not find Items by Category.xlsx. Looked in:\n  "
        + "\n  ".join(candidates)
    )


def _build_id_to_cat_map() -> Dict[int, str]:
    """Reverse the CONSOLIDATED_CATEGORY_IDS dict: id -> canonical tag name."""
    return {v: k for k, v in toast_config_db.CONSOLIDATED_CATEGORY_IDS.items()}


def _parse_sheet_id(sheet_name: str) -> Optional[int]:
    m = _SHEET_ID_RE.search(sheet_name)
    return int(m.group(1)) if m else None


def _is_section_header_name(name: str) -> bool:
    """Names like '***Dressing***', '*WheatSandwich*', '**GrabNGo Bundle**' are
    visual dividers within Aloha's item list — not actual items."""
    if not name:
        return True
    s = str(name).strip()
    if not s:
        return True
    return s.startswith("*") and s.endswith("*")


def _parse_sheet(df: pd.DataFrame, sheet_name: str, id_to_cat: Dict[int, str]) -> List[dict]:
    """
    Extract item rows from one sheet. Returns a list of dicts ready for
    toast_config_db.replace_aloha_items_master().

    pandas sometimes parses Item ID as float when the column has any blanks;
    we coerce back to int. Drops header-rows and any row missing PLU or name.
    """
    consol_id = _parse_sheet_id(sheet_name)
    consol_cat = id_to_cat.get(consol_id, "") if consol_id else ""

    # Find header row in the sheet. Format varies — first row of the CATERING
    # sheet is a title ("Items by Category"), second is "Run date: ...", third
    # is the real column header. Locate it by looking for "Item ID".
    header_idx = None
    for i in range(min(len(df), 5)):
        row_vals = [str(v) if v is not None else "" for v in df.iloc[i].tolist()]
        if "Item ID" in row_vals:
            header_idx = i
            break
    if header_idx is None:
        return []

    # Re-read with the right header
    df2 = df.iloc[header_idx + 1:].copy()
    df2.columns = df.iloc[header_idx].tolist()

    out: List[dict] = []
    last_range = ""
    for _, row in df2.iterrows():
        rng = row.get("Item Range")
        if pd.notna(rng) and str(rng).strip():
            last_range = str(rng).strip()

        plu_raw = row.get("Item ID")
        name = row.get("Item Long Name")
        if plu_raw is None or pd.isna(plu_raw):
            continue
        if name is None or (isinstance(name, float) and pd.isna(name)):
            continue
        name = str(name).strip()
        if _is_section_header_name(name):
            continue
        try:
            plu = int(float(plu_raw))
        except (ValueError, TypeError):
            continue
        if plu <= 0:
            continue
        out.append({
            "aloha_plu": plu,
            "aloha_item_name": name,
            "consol_id": consol_id,
            "consol_cat": consol_cat,
            "item_range": last_range,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--xlsx", default=None, help="Path to Items by Category.xlsx (default: project root)")
    p.add_argument("--dry-run", action="store_true", help="Parse + summarize without writing")
    p.add_argument("--db-path", default=toast_config_db.TOAST_CONFIG_DB_PATH,
                   help="Path to toast_config.db (default: backend/data/toast_config/toast_config.db)")
    args = p.parse_args()

    xlsx_path = os.path.abspath(args.xlsx) if args.xlsx else _find_xlsx()
    if not os.path.exists(xlsx_path):
        raise SystemExit(f"xlsx not found: {xlsx_path}")

    print(f"Reading: {xlsx_path}")
    print(f"Target DB: {args.db_path}")
    print()

    id_to_cat = _build_id_to_cat_map()

    # Read every sheet so we can summarize the parse before writing.
    sheets = pd.read_excel(xlsx_path, sheet_name=None, header=None)

    all_items: List[dict] = []
    unmapped_sheets: List[str] = []
    print(f"{'Sheet':50s}  consol_id  consol_cat                       items")
    print("-" * 110)
    for sheet_name, df in sheets.items():
        items = _parse_sheet(df, sheet_name, id_to_cat)
        cid = _parse_sheet_id(sheet_name)
        cat = id_to_cat.get(cid, "") if cid else ""
        if cid and not cat:
            unmapped_sheets.append(sheet_name)
        print(f"{sheet_name:50s}  {str(cid or '-'):9s}  {cat:32s}  {len(items)}")
        all_items.extend(items)

    print("-" * 110)
    print(f"Total items parsed: {len(all_items)}")
    print()

    # Dedupe on aloha_plu (the primary key). Multiple sheets can reference the
    # same PLU; prefer the row whose consol_cat IS mapped to a canonical
    # "* Consolidated …" tag — otherwise the item won't filter into its real
    # category in the Mapping Table UI.
    by_plu: Dict[int, dict] = {}
    duplicates_skipped = 0
    for it in all_items:
        plu = it["aloha_plu"]
        existing = by_plu.get(plu)
        if existing is None:
            by_plu[plu] = it
            continue
        duplicates_skipped += 1
        # Replace existing if THIS one has a canonical consol_cat and the
        # previous one didn't.
        if not existing.get("consol_cat") and it.get("consol_cat"):
            by_plu[plu] = it
    deduped = list(by_plu.values())
    if duplicates_skipped:
        print(f"Duplicate PLUs collapsed: {duplicates_skipped}  (prefer mapped consol_cat)")

    if unmapped_sheets:
        print()
        print("Sheets with no canonical consolidated_cat mapping (consol_cat will be blank):")
        for s in unmapped_sheets:
            print(f"  - {s}")

    if args.dry_run:
        print()
        print("DRY RUN — no rows written.")
        return 0

    written = toast_config_db.replace_aloha_items_master(deduped, args.db_path)
    print()
    print(f"aloha_items_master now has {written} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
