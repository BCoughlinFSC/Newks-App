"""
sync_toast_config.py  —  CLI + importable pipeline for syncing Toast menu
configuration into toast_config.db.

Steps:
  1. menu-config:   Fetch V3 menus for all active locations → toast_config.db
  2. attribute-map:  Build the MLI-PLU attribute map from stored config
  3. all:            Both steps in order

Usage
-----
  cd backend
  python sync_toast_config.py --step all
  python sync_toast_config.py --step menu-config
  python sync_toast_config.py --step attribute-map
  python sync_toast_config.py --step menu-config --restaurant-guid <guid>
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

import toast_config_db
import toast_config_builder

logger = logging.getLogger(__name__)

# Toast API config (same env vars as main.py)
TOAST_STANDARD_API_BASE_URL = os.getenv("TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com")
TOAST_STANDARD_API_CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
TOAST_STANDARD_API_CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")
TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")
TOAST_API_TOKEN = os.getenv("TOAST_API_TOKEN")

# Derive auth base URL (same logic as main.py)
if "/era/v1" in TOAST_API_BASE_URL:
    _ERA_AUTH_BASE = TOAST_API_BASE_URL.replace("/era/v1", "")
else:
    _ERA_AUTH_BASE = TOAST_API_BASE_URL.rsplit("/", 1)[0] if "/" in TOAST_API_BASE_URL else TOAST_API_BASE_URL

# Token caches (separate for ERA and Standard APIs)
_era_token_cache = {"token": None, "expires_at": 0}
_token_cache = {"token": None, "expires_at": 0}


async def _get_standard_auth_headers() -> dict:
    """Authenticate with Toast Standard API and return headers."""
    if not TOAST_STANDARD_API_CLIENT_ID or not TOAST_STANDARD_API_CLIENT_SECRET:
        raise RuntimeError("TOAST_STANDARD_API_CLIENT_ID and TOAST_STANDARD_API_CLIENT_SECRET required")

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_token_cache['token']}", "Content-Type": "application/json"}

    auth_url = f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(auth_url, json={
            "clientId": TOAST_STANDARD_API_CLIENT_ID,
            "clientSecret": TOAST_STANDARD_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Standard API auth failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
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

        if not token:
            raise RuntimeError("No token received from Standard API authentication")

        _token_cache["token"] = token
        _token_cache["expires_at"] = now + max(expires_in - 300, 60)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _get_era_auth_headers() -> dict:
    """Authenticate with Toast ERA API and return headers (mirrors main.py get_auth_headers)."""
    # Direct token (no auth call needed)
    if TOAST_API_TOKEN:
        return {"Authorization": f"Bearer {TOAST_API_TOKEN}", "Content-Type": "application/json"}

    if not TOAST_API_CLIENT_ID or not TOAST_API_CLIENT_SECRET:
        raise RuntimeError("Either TOAST_API_TOKEN or both TOAST_API_CLIENT_ID and TOAST_API_CLIENT_SECRET required")

    now = time.time()
    if _era_token_cache["token"] and now < _era_token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_era_token_cache['token']}", "Content-Type": "application/json"}

    auth_url = f"{_ERA_AUTH_BASE}/authentication/v1/authentication/login"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(auth_url, json={
            "clientId": TOAST_API_CLIENT_ID,
            "clientSecret": TOAST_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"ERA API auth failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
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

        if not token:
            raise RuntimeError("No token received from ERA API authentication")

        _era_token_cache["token"] = token
        _era_token_cache["expires_at"] = now + max(expires_in - 300, 60)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _get_active_locations(client: httpx.AsyncClient) -> list:
    """Fetch all active, non-test locations from Toast ERA API."""
    headers = await _get_era_auth_headers()
    url = f"{TOAST_API_BASE_URL}/restaurants-information"
    resp = await client.get(url, headers=headers, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch locations: {resp.status_code}")
    data = resp.json()
    return [
        {"restaurantGuid": item["restaurantGuid"], "restaurantName": item["restaurantName"]}
        for item in data
        if item.get("active") and not item.get("archived") and not item.get("testMode")
    ]


async def _fetch_menus_with_fallback(
    client: httpx.AsyncClient,
    headers: dict,
    max_retries: int = 5,
) -> dict:
    """Fetch menus: try V3, fall back to V2, retry on 429 with exponential backoff."""
    v3_forbidden = False  # Once 403, skip V3 for this call

    for attempt in range(max_retries):
        # Try V3 (skip if already got 403)
        if not v3_forbidden:
            resp = await client.get(f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus", headers=headers)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = min(5 * (attempt + 1), 30)
                logger.info(f"    Rate limited (V3), waiting {wait}s...")
                await asyncio.sleep(wait)
                continue
            # 403 or other — mark V3 as forbidden, try V2
            v3_forbidden = True

        # Try V2
        resp2 = await client.get(f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus", headers=headers)
        if resp2.status_code == 200:
            v2_data = resp2.json()
            # V2 may return a dict with "menus" key (same as V3) or a bare list
            if isinstance(v2_data, dict):
                return v2_data
            elif isinstance(v2_data, list):
                return {"menus": v2_data}
            else:
                return {"menus": []}

        if resp2.status_code == 429:
            wait = min(5 * (attempt + 1), 30)
            logger.info(f"    Rate limited (V2), waiting {wait}s (attempt {attempt + 1}/{max_retries})...")
            await asyncio.sleep(wait)
            continue

        # Non-retryable error
        logger.warning(f"    V2={resp2.status_code}")
        return None

    return None


async def sync_menu_config(
    restaurant_guids: list = None,
    db_path: str = toast_config_db.TOAST_CONFIG_DB_PATH,
) -> dict:
    """
    Fetch V3 menus for all (or specified) locations and persist to toast_config.db.

    Returns {locations_synced, total_items, total_tags, errors}
    """
    toast_config_db.initialize_db(db_path)
    totals = {"locations_synced": 0, "total_items": 0, "total_tags": 0, "total_item_tags": 0, "errors": []}

    async with httpx.AsyncClient(timeout=60.0) as client:
        if restaurant_guids:
            locations = [{"restaurantGuid": g, "restaurantName": ""} for g in restaurant_guids]
        else:
            logger.info("Fetching all active locations...")
            locations = await _get_active_locations(client)
            logger.info(f"Found {len(locations)} active locations")

        for i, loc in enumerate(locations):
            guid = loc["restaurantGuid"]
            name = loc.get("restaurantName", "")

            # Rate-limit: small delay between locations to avoid 429
            if i > 0:
                await asyncio.sleep(0.5)

            try:
                headers = await _get_standard_auth_headers()
                headers["Toast-Restaurant-External-ID"] = guid

                # Try V3 menus first, fall back to V2 (same as main.py)
                menus_data = await _fetch_menus_with_fallback(client, headers)
                if not menus_data:
                    logger.warning(f"  [{guid}] {name}: could not fetch menus, skipping")
                    totals["errors"].append(f"{guid}: no menus data")
                    continue
                result = toast_config_builder.merge_menu_config_from_api(
                    menus_data, guid, db_path=db_path
                )

                totals["locations_synced"] += 1
                totals["total_items"] += result["menu_items"]
                totals["total_tags"] += result["tags"]
                totals["total_item_tags"] += result["item_tags"]
                logger.info(f"  [{guid}] {name}: {result['menu_items']} items, {result['tags']} tags")

            except Exception as exc:
                logger.error(f"  [{guid}] {name}: Error: {exc}")
                totals["errors"].append(f"{guid}: {exc}")

    return totals


async def sync_attribute_map(
    db_path: str = toast_config_db.TOAST_CONFIG_DB_PATH,
) -> int:
    """Build the MLI-PLU attribute map from stored menu config."""
    return toast_config_builder.build_attribute_map(db_path)


async def sync_all(
    restaurant_guids: list = None,
    db_path: str = toast_config_db.TOAST_CONFIG_DB_PATH,
) -> dict:
    """Run full sync: menu config then attribute map."""
    config_result = await sync_menu_config(restaurant_guids, db_path)
    attr_count = await sync_attribute_map(db_path)
    return {
        **config_result,
        "attribute_map_rows": attr_count,
    }


async def _main(args):
    if args.step == "menu-config":
        guids = [args.restaurant_guid] if args.restaurant_guid else None
        result = await sync_menu_config(guids)
        print(f"\nMenu config sync complete:")
        print(f"  Locations synced: {result['locations_synced']}")
        print(f"  Total items:      {result['total_items']}")
        print(f"  Total tags:       {result['total_tags']}")
        print(f"  Total item-tags:  {result['total_item_tags']}")
        if result["errors"]:
            print(f"  Errors:           {len(result['errors'])}")
            for e in result["errors"][:5]:
                print(f"    - {e}")

    elif args.step == "attribute-map":
        count = await sync_attribute_map()
        print(f"\nAttribute map built: {count} rows")

    elif args.step == "all":
        guids = [args.restaurant_guid] if args.restaurant_guid else None
        result = await sync_all(guids)
        print(f"\nFull sync complete:")
        print(f"  Locations synced:   {result['locations_synced']}")
        print(f"  Total items:        {result['total_items']}")
        print(f"  Total tags:         {result['total_tags']}")
        print(f"  Total item-tags:    {result['total_item_tags']}")
        print(f"  Attribute map rows: {result['attribute_map_rows']}")
        if result["errors"]:
            print(f"  Errors:             {len(result['errors'])}")

    elif args.step == "stats":
        stats = toast_config_db.get_stats()
        print(f"\ntoast_config.db stats:")
        for k, v in stats.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Sync Toast menu config to SQLite")
    parser.add_argument(
        "--step",
        choices=["menu-config", "attribute-map", "all", "stats"],
        default="all",
        help="Which sync step to run (default: all)",
    )
    parser.add_argument(
        "--restaurant-guid",
        type=str,
        default=None,
        help="Sync a single restaurant instead of all",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))
