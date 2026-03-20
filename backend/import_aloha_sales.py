"""
Import Aloha aggregate sales data from SQL Server into local storage.

Usage:
  python import_aloha_sales.py --start 2024-01-01 --end 2026-03-20 --overwrite --batch-days 60
  python import_aloha_sales.py --last-days 90
"""
import argparse
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from aloha_sales_importer import import_aloha_sales_range, import_aloha_sales_last_days


def main():
    parser = argparse.ArgumentParser(description="Import Aloha sales data (tbl_SalesDataByDayPart)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--last-days", dest="last_days", type=int, help="Import last N days")
    parser.add_argument("--batch-days", dest="batch_days", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        if args.last_days:
            print(f"Importing last {args.last_days} days of sales data...")
            result = import_aloha_sales_last_days(args.last_days, overwrite=args.overwrite, batch_days=args.batch_days)
        else:
            end_date = date.fromisoformat(args.end) if args.end else date.today()
            start_date = date.fromisoformat(args.start) if args.start else end_date - timedelta(days=729)
            print(f"Importing sales data: {start_date} to {end_date} (batch={args.batch_days}, overwrite={args.overwrite})")
            result = import_aloha_sales_range(start_date, end_date, batch_days=args.batch_days, overwrite=args.overwrite)

        print(f"Written: {len(result.get('written_dates', []))} dates")
        print(f"Skipped: {len(result.get('skipped_dates', []))} dates")
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
