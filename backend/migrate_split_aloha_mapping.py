"""
migrate_split_aloha_mapping.py — One-shot, idempotent migration that creates
the mli_aloha_mapping table and backfills it from the existing
mli_plu_attribute_map.aloha_plu / aloha_item_name columns.

After this migration runs:
  - mli_aloha_mapping is the authoritative store for the two user-managed
    Aloha columns. UI saves, xlsx syncs, and build_attribute_map all
    reference it.
  - mli_plu_attribute_map still has aloha_plu / aloha_item_name columns, but
    they become a derived mirror updated from mli_aloha_mapping. Existing
    report queries that SELECT from mli_plu_attribute_map keep working
    unchanged.

Safety: this script is additive only. It does not drop or modify any existing
table. Re-running it is a no-op for rows already migrated and will pick up any
new rows since the last run.

Usage (on the Cloudways server, from the backend dir):
    venv/bin/python migrate_split_aloha_mapping.py
    venv/bin/python migrate_split_aloha_mapping.py --dry-run
    venv/bin/python migrate_split_aloha_mapping.py --db-path /path/to/toast_config.db
"""

import argparse
import os
import sqlite3
import sys

import toast_config_db


def _ensure_aloha_table(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_aloha_mapping_name
            ON mli_aloha_mapping(aloha_item_name)
        """
    )


def _backfill(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Copy rows that have any aloha data from mli_plu_attribute_map → mli_aloha_mapping."""
    # Pull only rows that carry actual aloha data; empty/zero rows would just
    # be noise in the new table.
    src = conn.execute(
        """
        SELECT multi_location_id, item_guid, aloha_plu, aloha_item_name
          FROM mli_plu_attribute_map
         WHERE (aloha_plu IS NOT NULL AND aloha_plu != 0)
            OR (aloha_item_name IS NOT NULL AND aloha_item_name != '')
        """
    ).fetchall()

    existing = {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute(
            "SELECT multi_location_id, item_guid, aloha_plu, aloha_item_name FROM mli_aloha_mapping"
        ).fetchall()
    }

    to_insert = []
    skipped_existing_same = 0
    skipped_existing_diff = 0
    for mli, guid, aplu, aname in src:
        aplu = aplu or 0
        aname = aname or ""
        key = (mli, guid)
        if key in existing:
            if existing[key] == (aplu, aname):
                skipped_existing_same += 1
            else:
                # Already migrated but with different values — leave the new
                # table alone since it's now the authority.
                skipped_existing_diff += 1
            continue
        to_insert.append((mli, guid, int(aplu), aname))

    if not dry_run and to_insert:
        conn.executemany(
            """INSERT INTO mli_aloha_mapping (
                multi_location_id, item_guid, aloha_plu, aloha_item_name
            ) VALUES (?,?,?,?)""",
            to_insert,
        )

    return {
        "src_rows_with_aloha": len(src),
        "already_in_target_unchanged": skipped_existing_same,
        "already_in_target_differs": skipped_existing_diff,
        "to_insert": len(to_insert),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=toast_config_db.TOAST_CONFIG_DB_PATH,
                   help="Path to toast_config.db (default: backend/data/toast_config/toast_config.db)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show counts without inserting anything.")
    args = p.parse_args()

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        raise SystemExit(f"toast_config.db not found at {db_path}")

    print(f"DB: {db_path}")
    print(f"Dry run: {args.dry_run}")
    print()

    # Make sure the table exists before reading from it (idempotent).
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not args.dry_run:
            _ensure_aloha_table(conn)
            conn.commit()
        else:
            # For dry-run, only create if missing so the subsequent SELECT
            # against mli_aloha_mapping doesn't error. We rollback at the end.
            _ensure_aloha_table(conn)

        # Verify mli_plu_attribute_map exists; if not, the DB is empty/fresh
        # and there's nothing to backfill.
        try:
            attr_count = conn.execute(
                "SELECT COUNT(*) FROM mli_plu_attribute_map"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            print("mli_plu_attribute_map does not exist — nothing to migrate.")
            return 0

        print(f"mli_plu_attribute_map rows: {attr_count}")
        stats = _backfill(conn, dry_run=args.dry_run)
        print(f"Rows with non-empty aloha data:       {stats['src_rows_with_aloha']}")
        print(f"  already in mli_aloha_mapping (same): {stats['already_in_target_unchanged']}")
        print(f"  already in mli_aloha_mapping (diff): {stats['already_in_target_differs']}")
        print(f"  to insert:                           {stats['to_insert']}")

        if args.dry_run:
            conn.rollback()
            print()
            print("DRY RUN — no changes committed.")
        else:
            conn.commit()
            final_count = conn.execute(
                "SELECT COUNT(*) FROM mli_aloha_mapping"
            ).fetchone()[0]
            print()
            print(f"mli_aloha_mapping now has {final_count} rows.")
            print("Migration complete.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
