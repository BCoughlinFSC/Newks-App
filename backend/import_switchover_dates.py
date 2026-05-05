"""
import_switchover_dates.py — load "Toast Switchover Dates.xlsx" into
toast_config.db (toast_switchover_dates table).

Usage:
    python import_switchover_dates.py [path/to/Toast\ Switchover\ Dates.xlsx]

With no argument, looks for `Toast Switchover Dates.xlsx` next to this script.
Re-running wipes the table and re-loads from the spreadsheet so DB stays in
sync with the Excel source of truth.
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
        xlsx_path = os.path.join(os.path.dirname(__file__),
                                 "Toast Switchover Dates.xlsx")

    if not os.path.exists(xlsx_path):
        print(f"ERROR: file not found: {xlsx_path}")
        return 1

    n = toast_config_db.import_switchover_dates_from_excel(xlsx_path)
    print(f"Imported {n} switchover dates into "
          f"{toast_config_db.TOAST_CONFIG_DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
