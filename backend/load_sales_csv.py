"""
Load Aloha sales data from a CSV export into the SQLite database.
Usage: python load_sales_csv.py "C:\\path\\to\\output - Sheet1.csv"
"""
import csv
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from aloha_sales_db import get_connection, initialize_db, ALOHA_SALES_DB_PATH

# CSV column -> SQLite column mapping
COL_MAP = {
    "BusinessDate": "business_date",
    "RestaurantID": "location",
    "RestaurantName": "restaurant_name",
    "NetSales": "net_sales",
    "OLONetSales": "olo_net_sales",
    "DeliveryNetSales": "delivery_net_sales",
    "CateringNetSales": "catering_net_sales",
    "GNGNetSales": "gng_net_sales",
    "ToGoNetSales": "togo_net_sales",
    "CurbSideNetSales": "curbside_net_sales",
    "CurbSideOLONetSales": "curbside_olo_net_sales",
    "DineInNetSales": "dinein_net_sales",
    "CallInNetSales": "callin_net_sales",
    "OLOCallInNetSales": "olo_callin_net_sales",
    "CheckCount": "check_count",
    "ChecksMinusCatering": "checks_minus_catering",
}

DB_COLS = [
    "business_date", "location", "restaurant_name",
    "net_sales", "olo_net_sales", "delivery_net_sales", "catering_net_sales",
    "gng_net_sales", "togo_net_sales", "curbside_net_sales", "curbside_olo_net_sales",
    "dinein_net_sales", "callin_net_sales", "olo_callin_net_sales",
    "check_count", "checks_minus_catering",
]


def load_csv(csv_path):
    initialize_db()
    conn = get_connection()

    placeholders = ", ".join(["?"] * len(DB_COLS))
    col_names = ", ".join(DB_COLS)
    sql = f"INSERT OR REPLACE INTO aloha_sales_data ({col_names}) VALUES ({placeholders})"

    count = 0
    batch = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vals = []
            for db_col in DB_COLS:
                # Find the CSV column for this DB column
                csv_col = None
                for ck, cv in COL_MAP.items():
                    if cv == db_col:
                        csv_col = ck
                        break
                raw = row.get(csv_col, "") or ""
                raw = raw.strip()
                if db_col in ("business_date", "location", "restaurant_name"):
                    vals.append(raw)
                else:
                    try:
                        vals.append(float(raw) if raw else 0.0)
                    except ValueError:
                        vals.append(0.0)
            batch.append(tuple(vals))

            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                conn.commit()
                count += len(batch)
                print(f"  {count:,} rows...")
                batch = []

    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        count += len(batch)

    conn.close()
    return count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python load_sales_csv.py <csv_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)

    print(f"Loading {csv_path} into {ALOHA_SALES_DB_PATH}...")
    n = load_csv(csv_path)
    print(f"Done! Loaded {n:,} rows.")
