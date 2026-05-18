"""
sync_mapping_excel_to_db.py — Sync Updated Mapping Table.xlsx → toast_config.db.

The Toast→Aloha mapping is split across two SQLite tables:
  - mli_aloha_mapping       — authoritative store for the two USER-managed
                              columns: aloha_plu, aloha_item_name.
  - mli_plu_attribute_map   — Toast-sourced columns (consolidated_cat, EE tags,
                              etc.) rebuilt by sync_toast_config --step
                              attribute-map. Mirrors the two aloha columns
                              so existing report queries keep working.

The xlsx (`Updated Mapping Table.xlsx` in the project root) is the
git-tracked source of truth for the user-managed Aloha columns ONLY. Toast
columns in the xlsx are informational and may be stale relative to the
latest Toast sync — this script no longer overwrites Toast-sourced columns
in mli_plu_attribute_map.

Run after `git pull` brings in a new xlsx, or run sync_toast_config.py
--step attribute-map after editing tags in Toastweb. Either order is safe;
neither destroys the other's data.

Usage (from project root or backend/):
    python backend/sync_mapping_excel_to_db.py
    python sync_mapping_excel_to_db.py            # if cwd is backend/

Then restart uvicorn so the in-process cache reloads.
"""

import os
import re
import sys
from typing import Dict, List

import pandas as pd

import toast_config_db


_RE_GUEST_COUNT = re.compile(r"^Guest\s*Count\s+([\d.]+)$", re.IGNORECASE)
_RE_EE = re.compile(r"^EE\s*([\d.]+)$", re.IGNORECASE)
_RE_CONSOLIDATED = re.compile(r"^\*\s*Consolidated\s", re.IGNORECASE)
_RE_LTO = re.compile(r"LTO", re.IGNORECASE)


def _find_mapping_xlsx() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    candidates = [
        os.path.join(project_root, "Updated Mapping Table.xlsx"),
        os.path.join(here, "..", "Updated Mapping Table.xlsx"),
        os.path.join(os.getcwd(), "Updated Mapping Table.xlsx"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise SystemExit(
        f"Could not find Updated Mapping Table.xlsx. Looked in:\n  "
        + "\n  ".join(candidates)
    )


def _parse_tags(tags_value, gc_value, ee_value):
    """
    Parse the Excel Tags + GuestCountTag + EntreEquivTag cells into the four
    fields SQLite stores separately: recipe, guest_count_tag, entree_equiv_tag,
    lto_tag. Mirrors the classification in toast_config_builder.build_attribute_map.
    """
    recipe = ""
    guest_count_tag = 0.0
    entree_equiv_tag = 0.0
    lto_tag = ""

    if pd.notna(tags_value):
        for raw in str(tags_value).split(","):
            name = raw.strip()
            if not name:
                continue
            if _RE_CONSOLIDATED.match(name) or name == "Catering":
                continue
            m = _RE_GUEST_COUNT.match(name)
            if m:
                try:
                    guest_count_tag = float(m.group(1))
                except ValueError:
                    pass
                continue
            m = _RE_EE.match(name)
            if m:
                try:
                    entree_equiv_tag = float(m.group(1))
                except ValueError:
                    pass
                continue
            if _RE_LTO.search(name):
                lto_tag = name
                continue
            if not recipe:
                recipe = name

    if guest_count_tag == 0.0 and pd.notna(gc_value):
        m = _RE_GUEST_COUNT.match(str(gc_value).strip())
        if m:
            try:
                guest_count_tag = float(m.group(1))
            except ValueError:
                pass

    if entree_equiv_tag == 0.0 and pd.notna(ee_value):
        m = _RE_EE.match(str(ee_value).strip())
        if m:
            try:
                entree_equiv_tag = float(m.group(1))
            except ValueError:
                pass

    return recipe, guest_count_tag, entree_equiv_tag, lto_tag


def _to_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _to_int(v) -> int:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def main() -> int:
    xlsx_path = _find_mapping_xlsx()
    print(f"Reading {xlsx_path}")
    # Force MultiLocationId / item_guid to string so 19-digit integer IDs
    # don't get rounded through float64 by pandas. Toast MLIs exceed float53
    # precision; reading as default dtype silently loses the last 2-3 digits.
    df = pd.read_excel(xlsx_path, dtype={
        "MultiLocationId": str,
        "item_guid": str,
    })

    required = {
        "MultiLocationId", "ToastPLU", "AlohaPLU", "Toast_Item_Name",
        "AlohaItemName", "ConsolidatedCat", "item_guid", "Tags",
        "GuestCountTag", "EntreEquivTag",
    }
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"Excel is missing columns: {sorted(missing)}")

    # Strategy for item_guid resolution (3-level fallback):
    #   1. Use the xlsx item_guid cell if it's a real UUID.
    #   2. Else look up the real Toast UUID by (MultiLocationId, ToastPLU) in
    #      cfg_menu_items. Most xlsx rows have a usable MLI + PLU but a blank
    #      item_guid — this promotes them to the real UUID so they share a
    #      key with rows that Toast sync (--step attribute-map) produces.
    #   3. Else fall back to a synthetic placeholder ("x:<name>"). These end
    #      up reported as "orphan" rows — items in the xlsx that Toast
    #      doesn't currently know about. Worth surfacing for cleanup.
    guid_lookup = toast_config_db.get_real_guid_lookup()
    rows: List[Dict] = []
    skipped = 0
    real_guid_from_xlsx = 0
    real_guid_from_lookup = 0
    synthetic_guids = 0
    orphans: List[Dict] = []
    for _, r in df.iterrows():
        toast_name = _to_str(r["Toast_Item_Name"])
        if not toast_name:
            skipped += 1
            continue
        recipe, gc, ee, lto = _parse_tags(r["Tags"], r["GuestCountTag"], r["EntreEquivTag"])
        mli = _to_str(r["MultiLocationId"])
        toast_plu = _to_int(r["ToastPLU"])
        item_guid = _to_str(r["item_guid"])
        if item_guid:
            real_guid_from_xlsx += 1
        else:
            norm = toast_config_db._normalize_mli(mli)
            looked = guid_lookup.get((norm, toast_plu)) if norm and toast_plu else None
            if looked:
                # Promote BOTH mli and item_guid to the cfg-side canonical
                # versions, otherwise build_attribute_map (which uses cfg's
                # full-precision MLI) would create a duplicate sibling row.
                mli, item_guid = looked
                real_guid_from_lookup += 1
            else:
                item_guid = "x:" + " ".join(toast_name.lower().split())
                synthetic_guids += 1
                orphans.append({
                    "MLI": mli,
                    "ToastPLU": toast_plu,
                    "Toast_Item_Name": toast_name,
                    "AlohaItemName": _to_str(r["AlohaItemName"]),
                })
        rows.append({
            "multi_location_id": mli,
            "item_guid":         item_guid,
            "toast_plu":         toast_plu,
            "aloha_plu":         _to_int(r["AlohaPLU"]),
            "toast_item_name":   toast_name,
            "aloha_item_name":   _to_str(r["AlohaItemName"]),
            "consolidated_cat":  _to_str(r["ConsolidatedCat"]),
            "recipe":            recipe,
            "guest_count_tag":   gc,
            "entree_equiv_tag":  ee,
            "lto_tag":           lto,
        })

    # Dedupe on the SQLite UNIQUE key (multi_location_id, item_guid). Excel
    # currently has ~93 exact-duplicate rows; INSERT OR REPLACE would silently
    # collapse them, but doing it explicitly here lets us report the count.
    seen = set()
    deduped: List[Dict] = []
    for r in rows:
        key = (r["multi_location_id"], r["item_guid"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    duplicates_dropped = len(rows) - len(deduped)

    db_path = toast_config_db.TOAST_CONFIG_DB_PATH
    print(f"Target DB: {db_path}")
    attr_before = toast_config_db.get_stats(db_path).get("attribute_map", 0)
    aloha_before = _count_table(db_path, "mli_aloha_mapping")

    written = toast_config_db.update_mapping_table_rows(deduped, db_path)
    attr_after = toast_config_db.get_stats(db_path).get("attribute_map", 0)
    aloha_after = _count_table(db_path, "mli_aloha_mapping")

    print(f"Rows in Excel: {len(df)}   parsed: {len(rows)}   blank-name skipped: {skipped}")
    print()
    print("item_guid resolution:")
    print(f"  from xlsx cell directly       : {real_guid_from_xlsx}")
    print(f"  looked up via (MLI, ToastPLU) : {real_guid_from_lookup}")
    print(f"  synthetic 'x:...' fallback    : {synthetic_guids}  (Toast doesn't know these items)")
    print()
    print(f"Duplicate rows dropped (same MLI+item_guid): {duplicates_dropped}")
    print(f"mli_aloha_mapping     : {aloha_before} -> {aloha_after}  (authoritative aloha)")
    print(f"mli_plu_attribute_map : {attr_before} -> {attr_after}  (placeholders added for new MLIs)")
    print(f"Payload rows: {written}")

    if orphans:
        print()
        print(f"=== ORPHAN ROWS ({len(orphans)}) ===")
        print("xlsx rows where (MLI, ToastPLU) didn't match anything in cfg_menu_items.")
        print("These got synthetic 'x:...' guids. Likely items removed from Toast or never")
        print("added there. Worth reviewing in the Mapping Table UI.")
        for o in orphans[:25]:
            print(f"  MLI={o['MLI']}  ToastPLU={o['ToastPLU']}  '{o['Toast_Item_Name']}' -> '{o['AlohaItemName']}'")
        if len(orphans) > 25:
            print(f"  ... and {len(orphans) - 25} more.")
    print()
    print("Done. Restart uvicorn so the in-process cache reloads.")
    print("Toast-sourced columns (consolidated_cat, EE tags, etc.) were NOT changed —")
    print("run `python sync_toast_config.py --step attribute-map` if you also need")
    print("those refreshed.")
    return 0


def _count_table(db_path: str, table: str) -> int:
    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
