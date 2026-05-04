"""
import_plu_ranges.py — load "Item PLU Ranges.xlsx" into toast_config.db.

Usage:
    python import_plu_ranges.py path/to/Item\\ PLU\\ Ranges.xlsx

Or with no argument it looks for an `Item PLU Ranges.xlsx` next to this script.
Re-running wipes the plu_ranges table and re-loads from the spreadsheet so the
DB stays in sync with the Excel source of truth.
"""
import logging
import os
import sys

import toast_config_db

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        xlsx_path = argv[1]
    else:
        xlsx_path = os.path.join(os.path.dirname(__file__), "Item PLU Ranges.xlsx")

    if not os.path.exists(xlsx_path):
        print(f"ERROR: file not found: {xlsx_path}")
        return 1

    n = toast_config_db.import_plu_ranges_from_excel(xlsx_path)
    print(f"Imported {n} PLU ranges into {toast_config_db.TOAST_CONFIG_DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
