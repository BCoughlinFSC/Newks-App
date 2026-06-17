"""
backfill_mapping_from_toast_export.py — Repair stranded legacy mapping rows
from Toast Web's full item export.

THE PROBLEM
-----------
Some Toast items are live but belong to an ARCHIVED modifier group (or are
otherwise off every API-visible menu). Toast's /menus/v2/menus endpoint never
returns them, so sync_toast_config.py can't create a real row for them. Their
only mapping-table row is the legacy Excel-import row:
    item_guid        = 'x:<name>'   (synthetic placeholder)
    multi_location_id = '1.1…e+18'  (precision destroyed by Excel)
…which still carries the hand-maintained Aloha mapping + consolidated category.

THE FIX
-------
Toast Web's Menu Item export (Items tab -> download) lists EVERY item, archived
or not, with its real GUID and Number. This script reads that CSV and, for each
legacy row, fills in the real item_guid + multi_location_id IN PLACE — leaving
the Aloha mapping, consolidated category, tags, and PLU untouched.

Matching legacy row -> CSV row: PLU+name (best), then unique name, then unique
PLU. The item_guid column is unambiguous (CSV 'GUID'); for the MLI we don't
guess — we CALIBRATE by checking the already-synced real rows (real GUID, clean
MLI) against the CSV's 'Item ID' vs 'Number' columns and use whichever the real
data agrees with.

USAGE (on Cloudways, from backend/ — needs the CSV uploaded next to it)
-----------------------------------------------------------------------
  # 1) get the CSV onto the box (from your laptop):
  #    scp "MenuItem_Export (2).csv" master_newksxl@165.227.79.143:/home/master/applications/wpftndhcuj/public_html/backend/
  python backfill_mapping_from_toast_export.py --csv "MenuItem_Export (2).csv"            # dry run
  python backfill_mapping_from_toast_export.py --csv "MenuItem_Export (2).csv" --apply    # write
After --apply, restart uvicorn (./restart.sh) so in-process caches rebuild.
"""

import argparse
import csv
import io
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional

import toast_config_db

DB_PATH = toast_config_db.TOAST_CONFIG_DB_PATH


# ── helpers ────────────────────────────────────────────────────────────────
def norm_name(name) -> str:
    return " ".join(str(name or "").strip().lower().split())


def to_int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def is_legacy(guid: str, mli: str) -> bool:
    """Legacy = synthetic 'x:' guid OR scientific-notation MLI (Excel scar)."""
    return str(guid or "").startswith("x:") or "e+" in str(mli or "").lower()


def _created(c) -> datetime:
    """Parse the export's 'Created Date' (M/D/YYYY). Unparseable -> earliest."""
    s = str(c.get("Created Date", "")).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def read_toast_csv(path: str) -> List[dict]:
    """Toast exports are Windows-1252 with a possible UTF-8 BOM. Decode robustly."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    return list(csv.DictReader(io.StringIO(text)))


def backup_db(db_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = f"{db_path}.pre_backfill_{stamp}.bak"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def fetch_mapping(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.execute(
        """SELECT id, multi_location_id, item_guid, toast_plu, toast_item_name,
                  aloha_item_name, aloha_plu, consolidated_cat
           FROM mli_plu_attribute_map"""
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--csv", required=True, help="Path to Toast Web MenuItem export CSV")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    ap.add_argument("--db-path", default=DB_PATH)
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"ERROR: CSV not found: {args.csv}")
        return 1
    if not os.path.exists(args.db_path):
        print(f"ERROR: DB not found: {args.db_path}")
        return 1

    csv_rows = read_toast_csv(args.csv)
    needed = {"GUID", "Name", "Item ID", "Number", "PLU", "Archived"}
    missing = needed - set(csv_rows[0].keys()) if csv_rows else needed
    if missing:
        print(f"ERROR: CSV missing columns: {sorted(missing)}")
        print(f"       got: {sorted(csv_rows[0].keys()) if csv_rows else '(empty)'}")
        return 1
    print(f"CSV rows: {len(csv_rows)}")

    # Indexes over the CSV
    csv_by_guid: Dict[str, dict] = {}
    csv_by_plu: Dict[int, List[dict]] = {}
    csv_by_name: Dict[str, List[dict]] = {}
    for c in csv_rows:
        g = str(c["GUID"]).strip()
        if g:
            csv_by_guid[g] = c
        p = to_int(c["PLU"])
        if p:
            csv_by_plu.setdefault(p, []).append(c)
        n = norm_name(c["Name"])
        if n:
            csv_by_name.setdefault(n, []).append(c)

    conn = sqlite3.connect(args.db_path)
    rows = fetch_mapping(conn)
    legacy = [r for r in rows if is_legacy(r["item_guid"], r["multi_location_id"])]
    real = [r for r in rows if not is_legacy(r["item_guid"], r["multi_location_id"])]
    print(f"Mapping rows: {len(rows)}  |  legacy: {len(legacy)}  |  real: {len(real)}")

    # ── CALIBRATE which CSV column is multi_location_id ──
    id_hits = num_hits = 0
    for r in real:
        c = csv_by_guid.get(str(r["item_guid"]).strip())
        if not c:
            continue
        mli = str(r["multi_location_id"]).strip()
        if mli and mli == str(c["Item ID"]).strip():
            id_hits += 1
        if mli and mli == str(c["Number"]).strip():
            num_hits += 1
    mli_col = "Item ID" if id_hits >= num_hits else "Number"
    print(f"\nMLI calibration vs real rows: 'Item ID' matched {id_hits}, "
          f"'Number' matched {num_hits}  ->  using CSV column '{mli_col}'")
    if id_hits == 0 and num_hits == 0:
        print("  ⚠ Could not calibrate (no real row GUIDs found in the CSV). "
              "Defaulting to 'Number'. Review the dry-run carefully before --apply.")
        mli_col = "Number"

    # Real rows already occupy these (mli, guid) keys — avoid UNIQUE collisions.
    real_keys = {(str(r["multi_location_id"]).strip(), str(r["item_guid"]).strip()) for r in real}

    # ── match each legacy row to a CSV item ──
    actions: List[dict] = []
    updates: List[tuple] = []          # (new_guid, new_mli, id)
    claimed_keys = set()               # (mli, guid) already assigned this run
    n_update = n_archived = n_flagged = n_orphan = n_collide = 0

    def pick(legacy_row) -> (Optional[dict], str):
        plu = to_int(legacy_row["toast_plu"])
        nm = norm_name(legacy_row["toast_item_name"])
        # Build the candidate pool, most specific match first.
        pool: List[dict] = []
        how = ""
        if plu:
            exact = [c for c in csv_by_plu.get(plu, []) if norm_name(c["Name"]) == nm]
            if exact:
                pool, how = exact, "plu+name"
        if not pool:
            byname = csv_by_name.get(nm, [])
            if byname:
                pool, how = byname, "name"
        if not pool and plu:
            byplu = csv_by_plu.get(plu, [])
            if byplu:
                pool, how = byplu, "plu"
        if not pool:
            return None, "orphan"
        if len(pool) == 1:
            return pool[0], how

        # Multiple candidates. Toast keeps version history: editing an item mints
        # a NEW guid but KEEPS its multiLocationId (the 'Number'). So if every
        # candidate shares one MLI, they're versions of ONE item — take the
        # newest guid (the MLI is identical either way). If they span several
        # MLIs, the item was re-created under a new master id; take the newest
        # and flag it ('*') for a human glance.
        newest = sorted(pool, key=lambda c: (_created(c), str(c["GUID"])), reverse=True)[0]
        distinct_mli = {str(c[mli_col]).strip() for c in pool}
        if len(distinct_mli) == 1:
            return newest, how + "/versions-newest"
        return newest, how + "/multi-mli-newest*"

    for lg in legacy:
        c, how = pick(lg)
        if not c:
            n_orphan += 1
            actions.append({
                "action": "NO_MATCH", "how": how, "legacy_id": lg["id"],
                "name": lg["toast_item_name"], "toast_plu": lg["toast_plu"],
                "new_guid": "", "new_mli": "", "archived": "", "note":
                    "not in the export — gone from Toast; left as-is for manual review",
            })
            continue

        new_guid = str(c["GUID"]).strip()
        new_mli = str(c[mli_col]).strip()
        archived = str(c["Archived"]).strip()
        key = (new_mli, new_guid)

        if key in real_keys or key in claimed_keys:
            n_collide += 1
            actions.append({
                "action": "SKIP_COLLISION", "how": how, "legacy_id": lg["id"],
                "name": lg["toast_item_name"], "toast_plu": lg["toast_plu"],
                "new_guid": new_guid, "new_mli": new_mli, "archived": archived,
                "note": "target (mli,guid) already used by another row — review/delete this dup manually",
            })
            continue

        claimed_keys.add(key)
        updates.append((new_guid, new_mli, lg["id"]))
        n_update += 1
        if archived.lower() == "yes":
            n_archived += 1
        if "*" in how:
            n_flagged += 1
        actions.append({
            "action": "FILL_KEYS", "how": how, "legacy_id": lg["id"],
            "name": lg["toast_item_name"], "toast_plu": lg["toast_plu"],
            "new_guid": new_guid, "new_mli": new_mli, "archived": archived,
            "note": f"set item_guid + multi_location_id (was guid='{lg['item_guid']}', mli='{lg['multi_location_id']}')",
        })

    print(f"\nPlan: fill real GUID+MLI on {n_update} legacy rows "
          f"({n_archived} archived-in-Toast; {n_flagged} multi-MLI picks flagged '*' to review), "
          f"{n_collide} collisions skipped, {n_orphan} no-match left as-is.")

    # Audit CSV next to this script
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "backfill_mapping_report.csv")
    with open(report, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[
            "action", "how", "legacy_id", "name", "toast_plu",
            "new_guid", "new_mli", "archived", "note"])
        w.writeheader()
        w.writerows(actions)
    print(f"Audit report: {report}")

    # Show a few sample fills so you can eyeball before applying
    samples = [a for a in actions if a["action"] == "FILL_KEYS"][:8]
    if samples:
        print("\nSample fills:")
        for a in samples:
            print(f"  [{a['how']:>14}] {a['name'][:48]:48}  plu={a['toast_plu']}  "
                  f"-> guid={a['new_guid']}  mli={a['new_mli']}  archived={a['archived']}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to execute.")
        return 0

    if not updates:
        print("\nNothing to update.")
        return 0

    bak = backup_db(args.db_path)
    print(f"\nBackup written: {bak}")
    with conn:
        conn.executemany(
            "UPDATE mli_plu_attribute_map SET item_guid = ?, multi_location_id = ? WHERE id = ?",
            updates)
    conn.close()
    print(f"APPLIED: {len(updates)} legacy rows repaired with real GUID + MLI.")
    print("Restart uvicorn (./restart.sh) so in-process caches rebuild.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
