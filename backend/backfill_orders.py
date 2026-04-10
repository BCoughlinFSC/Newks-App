"""
Backfill Toast Orders data into the local SQLite cache.

Usage:
  python backfill_orders.py --start 2024-01-01 --end 2025-12-31
  python backfill_orders.py --start 2024-06-01 --end 2024-06-30 --overwrite
  python backfill_orders.py --start 2024-01-01 --end 2024-12-31 --restaurant-guid <guid>
  python backfill_orders.py --retry-failed

Requires TOAST_STANDARD_API_CLIENT_ID, TOAST_STANDARD_API_CLIENT_SECRET,
TOAST_API_CLIENT_ID, TOAST_API_CLIENT_SECRET (or TOAST_API_TOKEN) in .env.
"""

import argparse
import asyncio
import os
import sys
import time

import httpx
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import toast_orders_blob as toast_orders_db

# ── Config from .env ──────────────────────────────────────────────────────────

TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")
TOAST_API_TOKEN = os.getenv("TOAST_API_TOKEN")

TOAST_STANDARD_API_CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
TOAST_STANDARD_API_CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")
TOAST_STANDARD_API_BASE_URL = os.getenv("TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com")
TOAST_ORDERS_API_BASE_URL = os.getenv("TOAST_ORDERS_API_BASE_URL", TOAST_STANDARD_API_BASE_URL)

# ── Token cache ───────────────────────────────────────────────────────────────

_era_token_cache = {"token": None, "expires_at": 0.0}
_std_token_cache = {"token": None, "expires_at": 0.0}


def _parse_token_response(data):
    """Extract token and expiry from Toast auth response."""
    if isinstance(data, dict):
        if "token" in data and isinstance(data["token"], dict):
            token = data["token"].get("accessToken") or data["token"].get("token")
            expires_in = data["token"].get("expiresIn", 3600)
        else:
            token = data.get("accessToken") or data.get("token") or data.get("access_token")
            expires_in = data.get("expiresIn", 3600)
    else:
        token = data
        expires_in = 3600
    return token, expires_in


async def get_era_auth_headers(client: httpx.AsyncClient) -> dict:
    """Auth headers for ERA/Analytics API (used for locations endpoint)."""
    if TOAST_API_TOKEN:
        return {"Authorization": f"Bearer {TOAST_API_TOKEN}", "Content-Type": "application/json"}

    now = time.time()
    if _era_token_cache["token"] and now < _era_token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_era_token_cache['token']}", "Content-Type": "application/json"}

    auth_url = f"{TOAST_API_BASE_URL.rstrip('/')}/../../authentication/v1/authentication/login"
    # Simpler: use the known base
    auth_url = "https://ws-api.toasttab.com/authentication/v1/authentication/login"
    resp = await client.post(auth_url, json={
        "clientId": TOAST_API_CLIENT_ID,
        "clientSecret": TOAST_API_CLIENT_SECRET,
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"ERA auth failed: {resp.status_code} {resp.text[:300]}")

    token, expires_in = _parse_token_response(resp.json())
    if not token:
        raise RuntimeError("No token in ERA auth response")

    _era_token_cache["token"] = token
    _era_token_cache["expires_at"] = now + max(expires_in - 300, 60)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def get_standard_auth_headers(client: httpx.AsyncClient) -> dict:
    """Auth headers for Standard/Orders API."""
    if not TOAST_STANDARD_API_CLIENT_ID or not TOAST_STANDARD_API_CLIENT_SECRET:
        raise RuntimeError("TOAST_STANDARD_API_CLIENT_ID and TOAST_STANDARD_API_CLIENT_SECRET required")

    now = time.time()
    if _std_token_cache["token"] and now < _std_token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_std_token_cache['token']}", "Content-Type": "application/json"}

    auth_url = f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login"
    resp = await client.post(auth_url, json={
        "clientId": TOAST_STANDARD_API_CLIENT_ID,
        "clientSecret": TOAST_STANDARD_API_CLIENT_SECRET,
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Standard API auth failed: {resp.status_code} {resp.text[:300]}")

    token, expires_in = _parse_token_response(resp.json())
    if not token:
        raise RuntimeError("No token in Standard API auth response")

    _std_token_cache["token"] = token
    _std_token_cache["expires_at"] = now + max(expires_in - 300, 60)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── API Fetchers ──────────────────────────────────────────────────────────────

async def fetch_active_locations(client: httpx.AsyncClient) -> list:
    """Fetch all active restaurant locations from ERA API."""
    headers = await get_era_auth_headers(client)
    url = f"{TOAST_API_BASE_URL}/restaurants-information"
    resp = await client.get(url, headers=headers, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Locations API error: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected locations response: {type(data)}")
    return [
        {"guid": item["restaurantGuid"], "name": item.get("restaurantName", "?")}
        for item in data
        if item.get("active") and not item.get("archived") and not item.get("testMode")
    ]


async def fetch_orders_for_day(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> list:
    """Paginated fetch of all orders for one restaurant + one business date."""
    headers = await get_standard_auth_headers(client)
    headers["Toast-Restaurant-External-ID"] = restaurant_guid
    url = f"{TOAST_ORDERS_API_BASE_URL}/orders/v2/ordersBulk"
    all_orders = []
    page = 1
    page_size = 100
    while True:
        params = {"businessDate": business_date_yyyymmdd, "pageSize": page_size, "page": page}
        resp = await client.get(url, headers=headers, params=params, timeout=60.0)
        if resp.status_code != 200:
            raise RuntimeError(f"Orders API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, list):
            break
        all_orders.extend(data)
        if len(data) < page_size:
            break
        page += 1
        await asyncio.sleep(0.25)
    return all_orders


# ── Main Logic ────────────────────────────────────────────────────────────────

async def backfill(start_date, end_date, restaurant_guid=None, overwrite=False):
    """Backfill orders for all restaurants across a date range."""
    from datetime import timedelta

    toast_orders_db.initialize_db()

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Resolve restaurants
        if restaurant_guid:
            restaurants = [{"guid": restaurant_guid, "name": restaurant_guid[:8]}]
        else:
            print("Fetching active locations...")
            restaurants = await fetch_active_locations(client)
            print(f"Found {len(restaurants)} active restaurants")

        # Build date list
        dates = []
        d = start_date
        while d <= end_date:
            dates.append(d)
            d += timedelta(days=1)

        total_pairs = len(restaurants) * len(dates)
        done = 0
        cached = 0
        failed = 0

        for di, day in enumerate(dates, 1):
            bdate = day.strftime("%Y%m%d")
            for ri, rest in enumerate(restaurants, 1):
                done += 1
                rguid = rest["guid"]
                rname = rest["name"]

                if not overwrite and toast_orders_db.is_date_cached(rguid, bdate):
                    cached += 1
                    continue

                try:
                    toast_orders_db.mark_import_started(rguid, bdate)
                    orders = await fetch_orders_for_day(client, rguid, bdate)
                    count = toast_orders_db.store_orders(rguid, bdate, orders)
                    print(f"[{done}/{total_pairs}] {rname} | {day} — {count} orders")
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    failed += 1
                    toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                    print(f"[{done}/{total_pairs}] {rname} | {day} — FAILED: {exc}")

        print(f"\nDone! Processed: {done}, Skipped (cached): {cached}, Failed: {failed}")


async def retry_failed():
    """Retry all failed imports."""
    toast_orders_db.initialize_db()
    failed = toast_orders_db.get_failed_imports()
    if not failed:
        print("No failed imports to retry.")
        return

    print(f"Retrying {len(failed)} failed imports...")

    async with httpx.AsyncClient(timeout=120.0) as client:
        success = 0
        still_failed = 0
        for i, item in enumerate(failed, 1):
            rguid = item["restaurant_guid"]
            bdate = item["business_date"]
            try:
                toast_orders_db.mark_import_started(rguid, bdate)
                orders = await fetch_orders_for_day(client, rguid, bdate)
                count = toast_orders_db.store_orders(rguid, bdate, orders)
                success += 1
                print(f"[{i}/{len(failed)}] {rguid[:8]} | {bdate} — {count} orders (recovered)")
                await asyncio.sleep(0.5)
            except Exception as exc:
                still_failed += 1
                toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                print(f"[{i}/{len(failed)}] {rguid[:8]} | {bdate} — still failing: {exc}")

        print(f"\nRetry done! Recovered: {success}, Still failing: {still_failed}")


def _parse_args():
    parser = argparse.ArgumentParser(description="Backfill Toast Orders into local SQLite cache.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--restaurant-guid", dest="restaurant_guid", help="Single restaurant GUID (default: all)")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch already cached dates")
    parser.add_argument("--retry-failed", dest="retry_failed", action="store_true", help="Retry all failed imports")
    return parser.parse_args()


def main():
    args = _parse_args()

    if args.retry_failed:
        asyncio.run(retry_failed())
        return

    if not args.start or not args.end:
        print("Error: --start and --end are required (unless using --retry-failed)")
        sys.exit(1)

    from datetime import datetime
    try:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"Error: invalid date format: {e}")
        sys.exit(1)

    if start > end:
        print(f"Error: start ({start}) must be before end ({end})")
        sys.exit(1)

    print(f"Backfilling orders: {start} to {end}")
    if args.restaurant_guid:
        print(f"Restaurant: {args.restaurant_guid}")
    else:
        print("Restaurant: ALL active locations")
    if args.overwrite:
        print("Mode: OVERWRITE existing cached dates")
    print()

    asyncio.run(backfill(start, end, args.restaurant_guid, args.overwrite))


if __name__ == "__main__":
    main()
