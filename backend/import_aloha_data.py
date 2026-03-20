"""
Import Aloha data into YoY storage (backend/data/aloha/).

From file (single date):
  python import_aloha_data.py "C:\\path\\to\\myReport.csv" 2024-11-01

From SQL Server (requires ALOHA_SQL_* in .env):
  python import_aloha_data.py --sql --sql-start 2024-02-01 --sql-end 2024-12-31
  python import_aloha_data.py --sql-last-days 90
  python import_aloha_data.py --sql --sql-start 2024-01-01 --sql-end 2024-01-31 --overwrite

Import fiscal calendar from CalendarV2:
  python import_aloha_data.py --fiscal-calendar --sql-start 2024-01-01 --sql-end 2027-12-31
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
import shutil

from dotenv import load_dotenv

from aloha_sql_importer import import_aloha_sql_last_days, import_aloha_sql_range

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YOY_DATA_DIR = os.path.join(SCRIPT_DIR, "data", "aloha")
YOY_INDEX_FILE = os.path.join(YOY_DATA_DIR, "index.json")

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

def ensure_storage():
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    if not os.path.exists(YOY_INDEX_FILE):
        with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
            f.write('{"files": {}}')

def load_index():
    ensure_storage()
    with open(YOY_INDEX_FILE, "r", encoding="utf-8") as f:
        return f.read()

def save_index(content: str):
    with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(content)

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Aloha data into YoY storage.")
    parser.add_argument("file_path", nargs="?", help="Path to CSV/XLSX file")
    parser.add_argument("business_date", nargs="?", help="Business date YYYY-MM-DD")

    parser.add_argument("--sql", action="store_true", help="Import from SQL Server (default last 730 days)")
    parser.add_argument("--sql-start", dest="sql_start", help="Start date YYYY-MM-DD")
    parser.add_argument("--sql-end", dest="sql_end", help="End date YYYY-MM-DD")
    parser.add_argument("--sql-last-days", dest="sql_last_days", type=int, help="Import last N days")
    parser.add_argument("--batch-days", dest="batch_days", type=int, default=31, help="Batch size in days")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing dates")
    parser.add_argument("--fiscal-calendar", dest="fiscal_calendar", action="store_true",
                        help="Import fiscal calendar from CalendarV2 (use with --sql-start/--sql-end)")
    return parser.parse_args()


def _import_from_file(file_path: str, business_date: date) -> None:
    if not os.path.exists(file_path):
        print(f"Error: file not found: {file_path}")
        sys.exit(1)

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in [".csv", ".xlsx", ".xls"]:
        print("Error: only .csv, .xlsx, or .xls files are supported")
        sys.exit(1)

    ensure_storage()
    target_name = f"aloha_{business_date.isoformat()}{ext}"
    target_path = os.path.join(YOY_DATA_DIR, target_name)
    shutil.copyfile(file_path, target_path)

    # Update index
    import json
    with open(YOY_INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)
    index.setdefault("files", {})[business_date.isoformat()] = {
        "path": target_path,
        "original_name": os.path.basename(file_path),
        "uploaded_at": datetime.now().isoformat()
    }
    with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    print(f"Imported Aloha data for {business_date.isoformat()}")
    print(f"Stored as: {target_path}")


def _import_from_sql(args: argparse.Namespace) -> None:
    if args.sql_last_days:
        result = import_aloha_sql_last_days(
            args.sql_last_days,
            overwrite=args.overwrite,
            batch_days=args.batch_days
        )
        print(f"Imported last {args.sql_last_days} days via SQL")
    else:
        if args.sql_end:
            end_date = date.fromisoformat(args.sql_end)
        else:
            end_date = date.today()

        if args.sql_start:
            start_date = date.fromisoformat(args.sql_start)
        else:
            start_date = end_date - timedelta(days=729)

        result = import_aloha_sql_range(
            start_date,
            end_date,
            batch_days=args.batch_days,
            overwrite=args.overwrite
        )
        print(f"Imported SQL range: {start_date.isoformat()} to {end_date.isoformat()}")

    print(f"Written dates: {len(result.get('written_dates', []))}")
    print(f"Skipped dates: {len(result.get('skipped_dates', []))}")


def _import_fiscal_calendar(args: argparse.Namespace) -> None:
    from aloha_sales_importer import import_fiscal_calendar

    if args.sql_end:
        end_date = date.fromisoformat(args.sql_end)
    else:
        end_date = date.today() + timedelta(days=365)

    if args.sql_start:
        start_date = date.fromisoformat(args.sql_start)
    else:
        start_date = end_date - timedelta(days=365 * 4)

    print(f"Importing fiscal calendar: {start_date.isoformat()} to {end_date.isoformat()}")
    count = import_fiscal_calendar(start_date, end_date)
    print(f"Imported {count} fiscal calendar rows")


def main():
    args = _parse_args()

    if args.fiscal_calendar:
        try:
            _import_fiscal_calendar(args)
        except Exception as exc:
            print(f"Error importing fiscal calendar: {exc}")
            sys.exit(1)
        return

    sql_mode = args.sql or args.sql_start or args.sql_end or args.sql_last_days
    if sql_mode:
        try:
            _import_from_sql(args)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        return

    if not args.file_path or not args.business_date:
        print("Usage: python import_aloha_data.py <file_path> <YYYY-MM-DD>")
        print("   or: python import_aloha_data.py --sql [--sql-start YYYY-MM-DD] [--sql-end YYYY-MM-DD]")
        print("   or: python import_aloha_data.py --sql-last-days N")
        sys.exit(1)

    try:
        business_date = date.fromisoformat(args.business_date)
    except ValueError:
        print("Error: business_date must be YYYY-MM-DD")
        sys.exit(1)

    _import_from_file(args.file_path, business_date)

if __name__ == "__main__":
    main()
