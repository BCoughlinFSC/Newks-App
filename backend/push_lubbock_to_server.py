"""Push Lubbock (1188) aloha_sales rows to the server without overwriting the whole DB."""
import httpx
import argparse
import aloha_sales_db

BASE_URL = "https://phpstack-1474989-6237770.cloudwaysapps.com"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--location", default="1188")
    args = parser.parse_args()

    conn = aloha_sales_db.get_connection()
    rows = conn.execute(
        "SELECT * FROM aloha_sales_data WHERE location = ?",
        (args.location,)
    ).fetchall()
    col_names = [d[0] for d in conn.execute("PRAGMA table_info(aloha_sales_data)").fetchall()]
    conn.close()

    if not rows:
        print(f"No rows found for location {args.location}")
        return

    payload_rows = []
    for row in rows:
        row_dict = dict(zip([c[1] for c in aloha_sales_db.get_connection().execute("PRAGMA table_info(aloha_sales_data)").fetchall()], row))
        payload_rows.append(row_dict)

    print(f"Pushing {len(payload_rows)} rows for location {args.location}...")

    client = httpx.Client(timeout=60.0)
    resp = client.post(
        f"{BASE_URL}/api/admin/aloha-sales/push-rows",
        headers={"Authorization": f"Bearer {args.token}"},
        json={"rows": payload_rows},
    )

    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")

if __name__ == "__main__":
    main()
