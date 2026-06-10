"""
merge_legacy_mapping_rows.py — Merge legacy Excel-import rows into their real
Toast-GUID counterparts in mli_plu_attribute_map.

THE PROBLEM
-----------
The mapping table has duplicate rows for the same Toast item, created by two
different import paths that produce incompatible (multi_location_id, item_guid)
keys:

  * Legacy Excel import (sync_mapping_excel_to_db.py):
      - pd.read_excel() read the 19-digit MultiLocationId as float64, destroying
        precision ("1100000005890165927" became "1.100000005890166e+18")
      - rows with no GUID in the Excel got a synthetic "x:<normalized name>"
        placeholder item_guid
      - these rows carry the hand-maintained Aloha mappings
  * Toast API sync (sync_toast_config.py → build_attribute_map):
      - proper integer-string MLI + real Toast GUID
      - fresh tags from Toast (EE, Guest Count, recipe, consolidated cat)
      - blank Aloha columns (the preserve-overrides lookup keys on
        (mli, item_guid), which never matches the mangled legacy keys)

Result: the Aloha mapping is stranded on the legacy row, the tags live on the
real row, and name-keyed report lookups resolve to whichever row happens to
come first — so the menu performance export shows an Aloha name but no EE tag
(or vice versa) for the same item.

WHAT THIS SCRIPT DOES
---------------------
1. Identifies LEGACY rows: item_guid LIKE 'x:%' OR multi_location_id in
   scientific notation ('e+' / 'E+').
2. Matches each legacy row to REAL rows by (in order):
     a. same real item_guid (legacy row had a GUID, only the MLI was mangled)
     b. normalized toast_item_name
     c. toast_plu (non-zero)
3. Copies aloha_item_name / aloha_plu from the legacy row into every matched
   real row whose Aloha columns are blank. Conflicts (real row already has a
   different Aloha mapping) are reported; the real row's value wins.
4. Deletes merged legacy rows (they're duplicates poisoning name lookups).
   Unmatched legacy rows (item no longer in Toast) are KEPT — they're the only
   source for that name in historical reports — and listed in the report.
5. Writes a full audit CSV of every action next to this script.

USAGE (on the Cloudways server, from backend/)
----------------------------------------------
  python merge_legacy_mapping_rows.py             # dry run — report only
  python merge_legacy_mapping_rows.py --apply     # backup DB, then write

After --apply, restart uvicorn (./restart.sh) so in-process caches rebuild.
"""

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional

import toast_config_db

DB_PATH = toast_config_db.TOAST_CONFIG_DB_PATH


def norm_name(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def is_legacy(row: dict) -> bool:
    guid = str(row["item_guid"] or "")
    mli = str(row["multi_location_id"] or "")
    return guid.startswith("x:") or "e+" in mli.lower()


def fetch_rows(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.execute(
        """SELECT id, multi_location_id, item_guid, toast_plu, aloha_plu,
                  toast_item_name, aloha_item_name, consolidated_cat,
                  recipe, guest_count_tag, entree_equiv_tag, lto_tag
           FROM mli_plu_attribute_map"""
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def backup_db(db_path: str) -> str:
    """WAL-safe backup via the sqlite3 backup API (not a raw file copy)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = f"{db_path}.pre_merge_{stamp}.bak"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default is dry-run report only).")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    rows = fetch_rows(conn)

    legacy = [r for r in rows if is_legacy(r)]
    real = [r for r in rows if not is_legacy(r)]

    print(f"Total rows: {len(rows)}  |  legacy: {len(legacy)}  |  real: {len(real)}")
    if not legacy:
        print("Nothing to do — no legacy rows found.")
        return 0

    # Indexes over the real rows
    real_by_guid: Dict[str, List[dict]] = {}
    real_by_name: Dict[str, List[dict]] = {}
    real_by_plu: Dict[int, List[dict]] = {}
    for r in real:
        if r["item_guid"]:
            real_by_guid.setdefault(str(r["item_guid"]), []).append(r)
        n = norm_name(r["toast_item_name"])
        if n:
            real_by_name.setdefault(n, []).append(r)
        plu = int(r["toast_plu"] or 0)
        if plu:
            real_by_plu.setdefault(plu, []).append(r)

    actions: List[dict] = []   # audit log
    updates: List[tuple] = []  # (aloha_item_name, aloha_plu, real_id)
    deletes: List[int] = []    # legacy ids to delete
    n_copied = n_conflict = n_already = n_orphan = 0

    for lg in legacy:
        guid = str(lg["item_guid"] or "")
        # Match: real guid → name → plu
        matches: List[dict] = []
        how = ""
        if guid and not guid.startswith("x:") and guid in real_by_guid:
            matches, how = real_by_guid[guid], "guid"
        if not matches:
            n = norm_name(lg["toast_item_name"])
            if n and n in real_by_name:
                matches, how = real_by_name[n], "name"
        if not matches:
            plu = int(lg["toast_plu"] or 0)
            if plu and plu in real_by_plu:
                matches, how = real_by_plu[plu], "plu"

        if not matches:
            n_orphan += 1
            actions.append({
                "action": "KEEP_ORPHAN", "matched_by": "",
                "legacy_id": lg["id"], "legacy_name": lg["toast_item_name"],
                "legacy_aloha": lg["aloha_item_name"], "legacy_aloha_plu": lg["aloha_plu"],
                "real_id": "", "real_aloha_before": "", "note":
                    "no real row matches — item may be gone from Toast; kept for history",
            })
            continue

        lg_aloha = str(lg["aloha_item_name"] or "").strip()
        lg_aplu = int(lg["aloha_plu"] or 0)
        for rr in matches:
            rr_aloha = str(rr["aloha_item_name"] or "").strip()
            rr_aplu = int(rr["aloha_plu"] or 0)
            if lg_aloha and not rr_aloha:
                new_aplu = rr_aplu if rr_aplu else lg_aplu
                updates.append((lg_aloha, new_aplu, rr["id"]))
                n_copied += 1
                actions.append({
                    "action": "COPY_ALOHA", "matched_by": how,
                    "legacy_id": lg["id"], "legacy_name": lg["toast_item_name"],
                    "legacy_aloha": lg_aloha, "legacy_aloha_plu": lg_aplu,
                    "real_id": rr["id"], "real_aloha_before": rr_aloha,
                    "note": f"set aloha_item_name='{lg_aloha}', aloha_plu={new_aplu}",
                })
            elif lg_aloha and rr_aloha and norm_name(lg_aloha) != norm_name(rr_aloha):
                n_conflict += 1
                actions.append({
                    "action": "CONFLICT_REAL_WINS", "matched_by": how,
                    "legacy_id": lg["id"], "legacy_name": lg["toast_item_name"],
                    "legacy_aloha": lg_aloha, "legacy_aloha_plu": lg_aplu,
                    "real_id": rr["id"], "real_aloha_before": rr_aloha,
                    "note": "real row already mapped differently — kept real value",
                })
            else:
                n_already += 1
                actions.append({
                    "action": "NO_COPY_NEEDED", "matched_by": how,
                    "legacy_id": lg["id"], "legacy_name": lg["toast_item_name"],
                    "legacy_aloha": lg_aloha, "legacy_aloha_plu": lg_aplu,
                    "real_id": rr["id"], "real_aloha_before": rr_aloha,
                    "note": "same mapping or legacy had none",
                })
        deletes.append(lg["id"])

    print(f"\nPlan: copy Aloha onto {n_copied} real rows, "
          f"{n_conflict} conflicts (real wins), {n_already} no-ops, "
          f"delete {len(deletes)} legacy duplicates, keep {n_orphan} orphans.")

    # Audit CSV
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "merge_legacy_mapping_report.csv")
    with open(report, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[
            "action", "matched_by", "legacy_id", "legacy_name",
            "legacy_aloha", "legacy_aloha_plu", "real_id",
            "real_aloha_before", "note"])
        w.writeheader()
        w.writerows(actions)
    print(f"Audit report: {report}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to execute.")
        return 0

    bak = backup_db(DB_PATH)
    print(f"Backup written: {bak}")

    with conn:
        conn.executemany(
            "UPDATE mli_plu_attribute_map SET aloha_item_name = ?, aloha_plu = ? WHERE id = ?",
            updates)
        conn.executemany(
            "DELETE FROM mli_plu_attribute_map WHERE id = ?",
            [(i,) for i in deletes])
    conn.close()

    print(f"APPLIED: {len(updates)} rows updated, {len(deletes)} legacy rows deleted.")
    print("Restart uvicorn (./restart.sh) so in-process caches rebuild.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
