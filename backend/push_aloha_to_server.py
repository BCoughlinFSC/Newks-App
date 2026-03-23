#!/usr/bin/env python3
"""
Push Aloha PLU/name data from local Excel mapping to the server's toast_config.db.

Usage:
    python push_aloha_to_server.py [--server URL] [--token TOKEN]

Reads "Updated Mapping Table.xlsx" from project root and POSTs the Aloha
mappings to /api/admin/toast-config/backfill-aloha.
"""
import argparse
import json
import os
import sys

import pandas as pd
import httpx

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
MAPPING_FILE = os.path.join(PROJECT_ROOT, "Updated Mapping Table.xlsx")

DEFAULT_SERVER = "https://phpstack-1474989-6237770.cloudwaysapps.com"


def main():
    parser = argparse.ArgumentParser(description="Push Aloha data to server")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Server URL")
    parser.add_argument("--token", default=os.getenv("ADMIN_TOKEN", ""), help="Admin JWT token")
    args = parser.parse_args()

    if not os.path.exists(MAPPING_FILE):
        print(f"ERROR: Mapping file not found: {MAPPING_FILE}")
        sys.exit(1)

    if not args.token:
        print("ERROR: --token required (or set ADMIN_TOKEN env var)")
        print("  Get a token by logging into the admin dashboard")
        sys.exit(1)

    # Read Excel
    df = pd.read_excel(MAPPING_FILE)
    mappings = []
    for _, row in df.iterrows():
        mli = str(row.get("MultiLocationId", "")).strip()
        aloha_plu = row.get("AlohaPLU", 0)
        aloha_name = str(row.get("AlohaItemName", "")).strip()

        try:
            aloha_plu = int(float(aloha_plu)) if pd.notna(aloha_plu) else 0
        except (ValueError, TypeError):
            aloha_plu = 0

        if pd.isna(aloha_name) or aloha_name == "nan":
            aloha_name = ""

        if mli and (aloha_plu or aloha_name):
            mappings.append({
                "multi_location_id": mli,
                "aloha_plu": aloha_plu,
                "aloha_item_name": aloha_name,
            })

    print(f"Read {len(mappings)} Aloha mappings from Excel")

    # Push to server
    url = f"{args.server.rstrip('/')}/api/admin/toast-config/backfill-aloha"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
    }

    print(f"Pushing to {url}...")
    resp = httpx.post(url, json={"mappings": mappings}, headers=headers, timeout=60.0)

    if resp.status_code == 200:
        result = resp.json()
        print(f"SUCCESS: {result['updated']} rows updated out of {result['submitted']} submitted")
    else:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
