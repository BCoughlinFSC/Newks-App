"""
dedup_synthetic_guids.py — One-shot cleanup that merges synthetic-guid rows
in mli_plu_attribute_map and mli_aloha_mapping into their real Toast UUID
counterparts.

Background
----------
Historically `sync_mapping_excel_to_db.py` invented placeholder item_guids
("x:<name>") for xlsx rows whose item_guid column was blank. Separately,
`sync_toast_config.py --step attribute-map` writes rows keyed by the real
Toast UUID from cfg_menu_items. Because the unique key is
(multi_location_id, item_guid), the same logical item ends up as two rows:

  Row A: item_guid = "x:half shrimp avocado salad..."   (xlsx-side data)
         aloha_plu, aloha_item_name populated
         consolidated_cat / EE / recipe blank or stale

  Row B: item_guid = "<real-uuid>"                       (Toast-side data)
         aloha_plu = 0, aloha_item_name = ""
         consolidated_cat / EE / recipe populated from Toast tags

YoY reports look up by real UUID and find Row B — no Aloha mapping. The
manually-curated Aloha data on Row A is unreachable through the canonical
join key.

This script:
  1. For every synthetic-guid row in mli_plu_attribute_map (item_guid LIKE 'x:%'),
     look up the real UUID from cfg_menu_items by (MLI, toast_plu).
  2. If found, merge: copy aloha_plu / aloha_item_name from the synthetic row
     onto the real-UUID row (in mli_aloha_mapping AND mli_plu_attribute_map),
     then delete the synthetic row from both tables.
  3. If not found, report the row as an "orphan" (item in xlsx but not in Toast).
     The synthetic row stays put so its data isn't lost.

Idempotent: re-running it finds zero synthetic rows the second time.
Reversible: restore from a pre-run DB backup.

Usage
-----
    # On Cloudways, from backend/ dir:
    venv/bin/python dedup_synthetic_guids.py --dry-run
    venv/bin/python dedup_synthetic_guids.py
"""

import argparse
import os
import sqlite3
import sys

import toast_config_db


def _ensure_aloha_table(conn: sqlite3.Connection) -> None:
    """Create mli_aloha_mapping if it's missing (safe to call repeatedly)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mli_aloha_mapping (
            multi_location_id TEXT NOT NULL,
            item_guid         TEXT NOT NULL DEFAULT '',
            aloha_plu         INTEGER NOT NULL DEFAULT 0,
            aloha_item_name   TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (multi_location_id, item_guid)
        )
        """
    )


def _load_synthetic_rows(conn: sqlite3.Connection):
    """Return all (mli, item_guid, toast_plu, toast_item_name, aloha_plu, aloha_item_name) rows
    in mli_plu_attribute_map whose item_guid starts with 'x:'."""
    return conn.execute(
        """
        SELECT multi_location_id, item_guid, toast_plu, toast_item_name,
               aloha_plu, aloha_item_name
          FROM mli_plu_attribute_map
         WHERE item_guid LIKE 'x:%'
        """
    ).fetchall()


def _load_real_guid_lookup(conn: sqlite3.Connection):
    """
    {(normalized_mli, toast_plu): (cfg_mli, real_item_guid)} from cfg_menu_items.

    The KEY uses a normalized (rounded) MLI so xlsx-float and Toast-string
    forms collapse together. The VALUE preserves cfg_menu_items' full-precision
    MLI so callers can write rows keyed by the SAME (MLI, item_guid) that
    build_attribute_map will use — preventing a duplicate row from sneaking
    past the unique index.

    Ambiguous (mli, plu) keys that map to multiple guids are dropped.
    """
    rows = conn.execute(
        """
        SELECT multi_location_id, item_plu, item_guid
          FROM cfg_menu_items
         WHERE multi_location_id != ''
           AND item_plu != 0
           AND item_guid != ''
        """
    ).fetchall()
    candidates = {}
    for mli, plu, guid in rows:
        norm = toast_config_db._normalize_mli(mli)
        if not norm:
            continue
        candidates.setdefault((norm, int(plu)), set()).add((mli, guid))
    out = {}
    for k, pairs in candidates.items():
        guids = {p[1] for p in pairs}
        if len(guids) == 1:
            out[k] = next(iter(pairs))
    return out


def dedup(conn: sqlite3.Connection, dry_run: bool):
    _ensure_aloha_table(conn)

    synthetic_rows = _load_synthetic_rows(conn)
    real_guid = _load_real_guid_lookup(conn)

    merged = 0
    orphans = []
    skipped_no_aloha = 0

    for mli, syn_guid, t_plu, t_name, a_plu, a_name in synthetic_rows:
        key = (toast_config_db._normalize_mli(mli), int(t_plu or 0))
        found = real_guid.get(key) if key[0] and key[1] else None
        if not found:
            orphans.append({
                "MLI": mli,
                "ToastPLU": t_plu,
                "Toast_Item_Name": t_name,
                "AlohaItemName": a_name,
                "AlohaPLU": a_plu,
            })
            continue
        cfg_mli, real = found

        # The synthetic row has Aloha data we want to preserve. If both
        # aloha_plu and aloha_item_name are blank/zero, there's no user-managed
        # data to migrate — we can just delete the synthetic row safely.
        has_aloha = (a_plu and a_plu != 0) or (a_name and a_name.strip())

        if not has_aloha:
            skipped_no_aloha += 1
            if not dry_run:
                conn.execute(
                    "DELETE FROM mli_plu_attribute_map WHERE multi_location_id=? AND item_guid=?",
                    (mli, syn_guid),
                )
                conn.execute(
                    "DELETE FROM mli_aloha_mapping WHERE multi_location_id=? AND item_guid=?",
                    (mli, syn_guid),
                )
            continue

        if dry_run:
            merged += 1
            continue

        # Use cfg_mli (full precision from Toast) — NOT the synthetic side's
        # float-rounded mli — as the key. This way any subsequent
        # build_attribute_map run inserts into the SAME row instead of
        # creating a sibling with a slightly different MLI string.
        conn.execute(
            """INSERT INTO mli_aloha_mapping (
                multi_location_id, item_guid, aloha_plu, aloha_item_name
            ) VALUES (?,?,?,?)
            ON CONFLICT(multi_location_id, item_guid)
            DO UPDATE SET
                aloha_plu=excluded.aloha_plu,
                aloha_item_name=excluded.aloha_item_name
            """,
            (cfg_mli, real, int(a_plu or 0), a_name or ""),
        )

        # Mirror onto the real-UUID row in mli_plu_attribute_map.
        cur = conn.execute(
            """UPDATE mli_plu_attribute_map
                  SET aloha_plu = ?, aloha_item_name = ?
                WHERE multi_location_id = ? AND item_guid = ?""",
            (int(a_plu or 0), a_name or "", cfg_mli, real),
        )
        if cur.rowcount == 0:
            conn.execute(
                """INSERT INTO mli_plu_attribute_map (
                    multi_location_id, item_guid, toast_plu, aloha_plu,
                    toast_item_name, aloha_item_name, consolidated_cat,
                    recipe, guest_count_tag, entree_equiv_tag, lto_tag
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (cfg_mli, real, int(t_plu or 0), int(a_plu or 0),
                 t_name or "", a_name or "", "", "", 0, 0, ""),
            )

        # Delete the synthetic row from both tables (keyed by the original
        # synthetic mli, NOT cfg_mli).
        conn.execute(
            "DELETE FROM mli_plu_attribute_map WHERE multi_location_id=? AND item_guid=?",
            (mli, syn_guid),
        )
        conn.execute(
            "DELETE FROM mli_aloha_mapping WHERE multi_location_id=? AND item_guid=?",
            (mli, syn_guid),
        )

        merged += 1

    return {
        "synthetic_rows_total": len(synthetic_rows),
        "merged": merged,
        "skipped_no_aloha_to_migrate": skipped_no_aloha,
        "orphans": orphans,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=toast_config_db.TOAST_CONFIG_DB_PATH,
                   help="Path to toast_config.db (default: backend/data/toast_config/toast_config.db)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show counts without modifying anything.")
    args = p.parse_args()

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        raise SystemExit(f"toast_config.db not found at {db_path}")

    print(f"DB: {db_path}")
    print(f"Dry run: {args.dry_run}")
    print()

    conn = sqlite3.connect(db_path)
    try:
        attr_before = conn.execute("SELECT COUNT(*) FROM mli_plu_attribute_map").fetchone()[0]
        print(f"mli_plu_attribute_map rows before: {attr_before}")
        try:
            aloha_before = conn.execute("SELECT COUNT(*) FROM mli_aloha_mapping").fetchone()[0]
            print(f"mli_aloha_mapping rows before    : {aloha_before}")
        except sqlite3.OperationalError:
            aloha_before = 0
            print("mli_aloha_mapping rows before    : (table missing — run migrate_split_aloha_mapping.py first)")
            return 1
        print()

        stats = dedup(conn, dry_run=args.dry_run)

        print(f"Synthetic rows total       : {stats['synthetic_rows_total']}")
        print(f"  merged into real UUID    : {stats['merged']}")
        print(f"  empty-aloha rows deleted : {stats['skipped_no_aloha_to_migrate']}")
        print(f"  orphans (no Toast match) : {len(stats['orphans'])}")
        print()

        if stats["orphans"]:
            print(f"=== ORPHAN ROWS ({len(stats['orphans'])}) — kept as synthetic guids ===")
            print("These xlsx items don't appear in cfg_menu_items by (MLI, ToastPLU).")
            print("Likely removed from Toast or never added. Review in the Mapping Table UI.")
            for o in stats["orphans"][:30]:
                print(f"  MLI={o['MLI']}  ToastPLU={o['ToastPLU']}  '{o['Toast_Item_Name']}' -> '{o['AlohaItemName']}'")
            if len(stats["orphans"]) > 30:
                print(f"  ... and {len(stats['orphans']) - 30} more.")
            print()

        if args.dry_run:
            conn.rollback()
            print("DRY RUN — no changes committed.")
        else:
            conn.commit()
            attr_after = conn.execute("SELECT COUNT(*) FROM mli_plu_attribute_map").fetchone()[0]
            aloha_after = conn.execute("SELECT COUNT(*) FROM mli_aloha_mapping").fetchone()[0]
            print(f"mli_plu_attribute_map rows after : {attr_after}  (delta {attr_after - attr_before:+d})")
            print(f"mli_aloha_mapping rows after     : {aloha_after}  (delta {aloha_after - aloha_before:+d})")
            print()
            print("Dedup complete.")
            print("Next: run `venv/bin/python sync_toast_config.py --step attribute-map`")
            print("to refresh Toast-sourced columns on any rows that were missing them.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
