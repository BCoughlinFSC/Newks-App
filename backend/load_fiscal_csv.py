"""One-time script to load fiscal calendar CSV into SQLite."""
import csv
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aloha_sales_db import upsert_fiscal_calendar, initialize_db, get_fiscal_calendar_stats

initialize_db()

csv_path = os.path.join(
    os.path.expanduser("~"),
    "OneDrive - Newk's Eatery", "Documents", "results.csv"
)
print(f"Reading: {csv_path}")

rows = []
with open(csv_path, "r", encoding="utf-8-sig") as f:
    # Try to detect delimiter
    first_line = f.readline()
    f.seek(0)
    if "\t" in first_line:
        delimiter = "\t"
    else:
        delimiter = ","

    # Check if first line is a header
    if first_line.strip().startswith("date"):
        reader = csv.DictReader(f, delimiter=delimiter)
        for r in reader:
            d = r.get("date", "").strip()
            ly = r.get("ly_date", "").strip()
            if d and ly:
                rows.append({
                    "date": d,
                    "ly_date": ly,
                    "fiscal_year": int(r["fiscal_year"].strip()) if r.get("fiscal_year", "").strip() else None,
                    "fiscal_period": int(r["fiscal_period"].strip()) if r.get("fiscal_period", "").strip() else None,
                    "fiscal_week": int(r["fiscal_week"].strip()) if r.get("fiscal_week", "").strip() else None,
                })
    else:
        reader = csv.reader(f, delimiter=delimiter)
        for r in reader:
            if len(r) >= 2 and r[0].strip():
                rows.append({
                    "date": r[0].strip(),
                    "ly_date": r[1].strip(),
                    "fiscal_year": int(r[2].strip()) if len(r) > 2 and r[2].strip() else None,
                    "fiscal_period": int(r[3].strip()) if len(r) > 3 and r[3].strip() else None,
                    "fiscal_week": int(r[4].strip()) if len(r) > 4 and r[4].strip() else None,
                })

print(f"Parsed {len(rows)} rows")
if rows:
    print(f"First: {rows[0]}")
    print(f"Last:  {rows[-1]}")
    count = upsert_fiscal_calendar(rows)
    print(f"Imported {count} rows into SQLite")
    stats = get_fiscal_calendar_stats()
    print(f"Stats: {stats}")
else:
    print("No rows parsed!")
