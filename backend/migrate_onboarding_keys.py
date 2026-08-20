"""
migrate_onboarding_keys.py — carry recorded task completions across the master
track's key change.

The master track used to key tasks positionally (mstr_001 … mstr_144). It is now
keyed by a hash of the activity text, so re-ordering the workbook or inserting a
row no longer shifts every key. Any completion already recorded against an old
key would be orphaned by that change; this script rewrites those rows using the
old->new map that build_master_from_workbook.py emits.

    python backend/migrate_onboarding_keys.py --dry-run     # report only
    python backend/migrate_onboarding_keys.py               # apply

Run it ONCE, on the server, after deploying the new catalog_data.json and before
anyone opens the dashboard. It is idempotent — a second run finds nothing to do —
and it backs the database up first.

Old keys with no counterpart in the workbook (activities that were dropped when
the workbook became the source of truth) are reported and left alone: their rows
stay in task_status, harmlessly ignored, rather than being deleted. If one turns
out to matter, its history is still there to map by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION_PATH = os.path.join(HERE, "data", "onboarding", "key_migration.json")
DEFAULT_DB = os.getenv(
    "ONBOARDING_SQLITE_PATH",
    os.path.join(HERE, "data", "onboarding", "onboarding.db"),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--map", default=MIGRATION_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no database at {args.db} — nothing to migrate (fresh install)")
        return 0
    if not os.path.exists(args.map):
        print(f"key map not found: {args.map}\n"
              f"run build_master_from_workbook.py first", file=sys.stderr)
        return 1

    with open(args.map, encoding="utf-8") as f:
        data = json.load(f)
    mapping = data.get("master", {})
    unmatched = {u["key"]: u.get("title") for u in data.get("unmatched", [])}

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    total_moved = total_stranded = total_collision = 0
    plan = []
    for table in ("task_status", "task_questions"):
        if table not in tables:
            continue
        rows = conn.execute(
            f"SELECT opening_id, task_key, COUNT(*) n FROM {table} "
            f"WHERE task_key LIKE 'mstr_%' GROUP BY opening_id, task_key"
        ).fetchall()
        for r in rows:
            old = r["task_key"]
            if old not in mapping:
                if old in unmatched:
                    total_stranded += r["n"]
                    plan.append((table, "STRANDED", old, unmatched.get(old) or ""))
                continue
            new = mapping[old]
            if table == "task_status":
                clash = conn.execute(
                    "SELECT 1 FROM task_status WHERE opening_id=? AND task_key=?",
                    (r["opening_id"], new),
                ).fetchone()
                if clash:
                    # The new key already has a row (a re-run, or two old rows
                    # collapsing into one). Keep whichever is marked done.
                    total_collision += 1
                    plan.append((table, "MERGE", f"{old} -> {new}",
                                 f"opening {r['opening_id']}"))
                    continue
            total_moved += r["n"]
            plan.append((table, "MOVE", f"{old} -> {new}", f"opening {r['opening_id']}"))

    print(f"database : {args.db}")
    print(f"map      : {len(mapping)} old keys -> new keys\n")
    for table, action, what, detail in plan:
        print(f"  [{action:8}] {table:15} {what}  {detail}")
    if not plan:
        print("  (nothing to do — no old-style master keys present)")
    print(f"\nmove {total_moved} rows · merge {total_collision} · "
          f"strand {total_stranded} (activity no longer in the workbook)")

    # Stranded keys are reported every run but are never written to, so a run
    # with nothing but strandings is a no-op — don't take a backup for it.
    if args.dry_run or not (total_moved or total_collision):
        print("\n--dry-run: nothing written" if args.dry_run
              else "\nnothing to migrate — already up to date")
        conn.close()
        return 0

    backup = args.db + ".pre-keymigration"
    shutil.copy2(args.db, backup)
    print(f"\nbacked up to {backup}")

    try:
        conn.execute("BEGIN")
        for table in ("task_status", "task_questions"):
            if table not in tables:
                continue
            for old, new in mapping.items():
                if table == "task_status":
                    # Drop rows whose target already exists, then rename the rest —
                    # avoids a UNIQUE violation on (opening_id, task_key).
                    conn.execute(
                        """DELETE FROM task_status
                            WHERE task_key = ?
                              AND opening_id IN (SELECT opening_id FROM task_status
                                                  WHERE task_key = ?)""",
                        (old, new),
                    )
                conn.execute(
                    f"UPDATE {table} SET task_key = ? WHERE task_key = ?", (new, old)
                )
        conn.commit()
        print(f"migrated {total_moved} rows")
    except Exception as exc:
        conn.rollback()
        print(f"FAILED, rolled back: {exc}", file=sys.stderr)
        print(f"database is unchanged; backup also at {backup}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
