"""Push a local SQLite DB to the Cloudways server via the admin upload endpoint."""
import httpx
import sys
import os
import argparse

BASE_URL = "https://phpstack-1474989-6237770.cloudwaysapps.com"

DB_PATHS = {
    "aloha_sales": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "aloha", "aloha_sales.db"),
    "crunchtime": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crunchtime.db"),
    "punchh": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "punchh.db"),
    "monkey": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "monkey.db"),
}

def main():
    parser = argparse.ArgumentParser(description="Push a SQLite DB to the server")
    parser.add_argument("db_name", choices=list(DB_PATHS.keys()), help="Which DB to push")
    parser.add_argument("--token", required=True, help="Admin JWT token")
    args = parser.parse_args()

    db_path = DB_PATHS[args.db_name]
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found")
        sys.exit(1)

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"Uploading {args.db_name} ({size_mb:.1f} MB) to {BASE_URL}...")

    with open(db_path, "rb") as f:
        client = httpx.Client(timeout=300.0)
        resp = client.post(
            f"{BASE_URL}/api/admin/upload-db?db_name={args.db_name}",
            headers={"Authorization": f"Bearer {args.token}"},
            files={"file": ("db.sqlite", f, "application/octet-stream")},
        )

    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")

if __name__ == "__main__":
    main()
