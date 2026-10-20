"""
seed_mealcards.py - one-time seed of the app-owned meal card tables.

Loads three things the desktop scripts used to keep as files:

  1. The card roster and its per-period ticks, from
     "Gift Card _ Meal Card Numbers - Support Center 2026.xlsx" (sheet "2026":
     column A Name, column B Card Number, columns C.. one per period).
     Per-card target amounts are parsed out of the name the way the bot did,
     e.g. "Cynthia Baxter (Board Meeting $600.00)" -> 600.00.

  2. The authoritative card -> employee mapping, from MealCardsToAllPay.csv
     (the saved grid of Export_MealCardsToAllPay_MAPPING.sql against
     dev_aloha.dbo.MealCardsToAllPay). Optional but strongly preferred: without
     it the payroll report falls back to matching people by name.

  3. The name-matched fallback, from past "Monthly MealCard Adjustments for
     Payroll*.xlsx" exports. Each one covers only that month's people, so they
     are merged, not replaced.

WHERE THIS RUNS: on whichever box holds mealcard.db, from the backend/
directory:

    python seed_mealcards.py --roster "Gift Card _ Meal Card Numbers.xlsx" --dry-run
    python seed_mealcards.py --roster "...xlsx" --mapping MealCardsToAllPay.csv
    python seed_mealcards.py --roster "...xlsx" --payroll-seed "Monthly MealCard*.xlsx"

Re-running is safe: cards upsert by card number, period ticks upsert by
(card, year, period), and the card mapping is replaced wholesale.
"""

import argparse
import csv
import glob
import logging
import os
import sys

from openpyxl import load_workbook

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mealcard_db  # noqa: E402

def seed_roster(path: str, sheet: str, year: int, dry_run: bool) -> int:
    """Parse and import the roster. The Meal Cards page does the same thing
    through mealcard_db.parse_roster / import_roster, so the two routes cannot
    drift apart."""
    try:
        rows = mealcard_db.parse_roster(path, sheet)
    except mealcard_db.RosterError as e:
        raise SystemExit(str(e))

    for row in rows:
        logger.info("card %-16s %-34s target $%.2f",
                    row["card_number"], row["name"][:34], row["target_amount"])

    ticks = sum(len(r["periods"]) for r in rows)
    if not dry_run:
        mealcard_db.import_roster(rows, year, source=os.path.basename(path))
    logger.info("roster: %d cards, %d period ticks%s", len(rows), ticks,
                " (dry run)" if dry_run else "")
    return len(rows)


def seed_mapping(path: str, dry_run: bool) -> int:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            key = {k.strip().lower(): (v or "").strip() for k, v in raw.items()}
            card = mealcard_db.clean_card(key.get("cardnumber", ""))
            if not card:
                continue
            rows.append({
                "card_number": card,
                "employee_id": key.get("employeeid") or key.get("allpayid", ""),
                "entity": key.get("entity", ""),
                "name": key.get("name", ""),
            })
    if not rows:
        raise SystemExit(f"No CardNumber rows found in {path}")

    zeros = sum(1 for r in rows if r["employee_id"] == "0")
    logger.info("mapping: %d cards (%d with AllPayID = 0, excluded from payroll by design)",
                len(rows), zeros)
    if dry_run:
        return len(rows)
    return mealcard_db.replace_employee_map(rows, source=os.path.basename(path))


def seed_payroll_names(patterns: list, dry_run: bool) -> int:
    """Lift Name -> Employee ID out of previous payroll exports.

    Layout of those sheets: column A Employee ID, C Entity, F Name, with three
    title rows above the header.
    """
    paths = sorted({p for pattern in patterns for p in glob.glob(pattern)},
                   key=os.path.getmtime)   # oldest first, newest wins
    if not paths:
        logger.warning("no payroll exports matched %s", patterns)
        return 0

    rows = {}
    for path in paths:
        try:
            ws = load_workbook(path, data_only=True).worksheets[0]
        except Exception as e:
            logger.warning("skipping %s (%s)", os.path.basename(path), e.__class__.__name__)
            continue
        for r in range(1, ws.max_row + 1):
            eid, entity, name = ws.cell(r, 1).value, ws.cell(r, 3).value, ws.cell(r, 6).value
            if not eid or not name or str(eid).strip().lower() == "employee id":
                continue
            rows[str(name).strip()] = {
                "name": str(name).strip(),
                "employee_id": str(eid).strip(),
                "entity": str(entity or "").strip(),
            }

    logger.info("fallback: %d names from %d payroll export(s)", len(rows), len(paths))
    if dry_run:
        return len(rows)
    return mealcard_db.upsert_employee_names(
        list(rows.values()),
        source=f"{len(paths)} payroll export(s), newest {os.path.basename(paths[-1])}",
    )


def main():
    p = argparse.ArgumentParser(description="Seed the meal card tables from the old files.")
    p.add_argument("--roster", help="Gift Card / Meal Card Numbers xlsx")
    p.add_argument("--sheet", default=None, help="Roster sheet name (default: the year)")
    p.add_argument("--year", type=int, default=None, help="Year the ticks belong to")
    p.add_argument("--mapping", help="MealCardsToAllPay.csv exported from dev_aloha")
    p.add_argument("--payroll-seed", action="append", default=[],
                   help="Glob of past payroll exports for the name fallback (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="Show what would be written")
    args = p.parse_args()

    if not (args.roster or args.mapping or args.payroll_seed):
        p.error("nothing to do - pass at least one of --roster / --mapping / --payroll-seed")

    year = args.year or (int(args.sheet) if (args.sheet or "").isdigit() else None)
    sheet = args.sheet or (str(year) if year else None)

    mealcard_db.initialize_db()

    if args.roster:
        if not year or not sheet:
            p.error("--roster needs --year (and --sheet if it is not just the year)")
        seed_roster(args.roster, sheet, year, args.dry_run)
    if args.mapping:
        seed_mapping(args.mapping, args.dry_run)
    if args.payroll_seed:
        seed_payroll_names(args.payroll_seed, args.dry_run)

    summary = mealcard_db.mapping_summary()
    logger.info("employee mapping now: %s", summary["description"])
    if not summary["authoritative"]:
        logger.warning("No authoritative card mapping. Run Export_MealCardsToAllPay_MAPPING.sql "
                       "in SSMS, save the grid as MealCardsToAllPay.csv, and seed it with "
                       "--mapping - otherwise payroll rows are matched by name.")


if __name__ == "__main__":
    main()
