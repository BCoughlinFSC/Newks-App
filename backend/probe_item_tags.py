"""
probe_item_tags.py — direct Toast Menus API probe for a single item.

Bypasses ALL of our caches, SQLite tables, and Excel mappings. Hits the V3
metadata endpoint, then either V3 or V2 menus (whichever Toast lets us use),
finds the item by GUID, and prints every tag Toast actually returned.

Use when an EE / Recipe / Guest Count tag was added in Toastweb but isn't
showing in the Menu Performance report — this answers "is Toast's API
actually returning the tag yet, or is it still propagating?"

Usage:
    python probe_item_tags.py <item_guid> [restaurant_guid]

If restaurant_guid is omitted, probes the first active location returned by
/restaurants. Add more restaurants to scan multiple locations.
"""
import asyncio
import json
import os
import sys
from typing import Optional

import httpx

# Reuse main.py's auth helpers + config
import main as backend_main


async def _get_active_locations(client: httpx.AsyncClient) -> list:
    """Pull active restaurant_guids from Toast (mirrors sync_toast_config logic)."""
    headers = await backend_main.get_standard_api_auth_headers()
    base = backend_main.TOAST_STANDARD_API_BASE_URL
    resp = await client.get(f"{base}/restaurants/v1/restaurants",
                            headers=headers, timeout=30.0)
    resp.raise_for_status()
    return [r for r in resp.json() if r.get("isActive", True)]


async def probe(item_guid: str, restaurant_guid: Optional[str]) -> None:
    headers = await backend_main.get_standard_api_auth_headers()
    base = backend_main.TOAST_STANDARD_API_BASE_URL

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Resolve restaurant_guid if not provided
        if not restaurant_guid:
            locs = await _get_active_locations(client)
            if not locs:
                print("ERROR: no active restaurants found"); return
            restaurant_guid = locs[0].get("restaurantGuid") or locs[0].get("guid")
            print(f"Using first active restaurant: {restaurant_guid}")

        h = {**headers, "Toast-Restaurant-External-ID": restaurant_guid}

        # Step 1: V3 metadata (do we even have access?)
        print("\n--- V3 METADATA ---")
        meta_resp = await client.get(f"{base}/menus/v3/metadata", headers=h)
        print(f"  status: {meta_resp.status_code}")
        if meta_resp.status_code == 200:
            meta = meta_resp.json()
            print(f"  hash:    {meta.get('hash')}")
            print(f"  version: {meta.get('version')}")
            api_version = "V3"
        else:
            print(f"  body:    {meta_resp.text[:300]}")
            print("  → V3 metadata unavailable, will use V2")
            api_version = "V2"

        # Step 2: Pull menus
        path = "v3" if api_version == "V3" else "v2"
        print(f"\n--- {api_version} MENUS ---")
        menus_resp = await client.get(f"{base}/menus/{path}/menus", headers=h)
        print(f"  status: {menus_resp.status_code}")
        if menus_resp.status_code != 200:
            print(f"  body: {menus_resp.text[:500]}")
            return
        menus_data = menus_resp.json()

        # Step 3: Hunt for the item GUID anywhere in the payload
        print(f"\n--- SCANNING FOR {item_guid} ---")
        found = []

        # Top-level menu items
        for menu in menus_data.get("menus", []):
            for group in menu.get("menuGroups", []):
                for item in group.get("menuItems", []):
                    if item.get("guid") == item_guid:
                        found.append(("menuItem", item, menu.get("name", ""), group.get("name", "")))

        # Modifier group references
        for mg_id, mg in (menus_data.get("modifierGroupReferences") or {}).items():
            for opt in mg.get("modifierOptions", []) or []:
                if opt.get("guid") == item_guid:
                    found.append(("modifierGroupOption", opt, f"group_id={mg_id}", ""))

        # Modifier option references
        for mo_id, mo in (menus_data.get("modifierOptionReferences") or {}).items():
            if mo.get("guid") == item_guid:
                found.append(("modifierOptionRef", mo, f"option_id={mo_id}", ""))

        if not found:
            print(f"  ❌ Item NOT found in {api_version} menus payload at all.")
            print(f"     This means Toast's API doesn't currently expose this item")
            print(f"     for restaurant {restaurant_guid}. Possible causes:")
            print(f"       - Item is disabled in this location's menu")
            print(f"       - Tag changes were published to POS but not to Menus API")
            print(f"       - Different menu (e.g. OLO) excludes it")
            print(f"       - Toast hasn't propagated the change yet")
            return

        for kind, obj, ctx1, ctx2 in found:
            print(f"\n  ✓ Found as {kind}  ({ctx1}{(' / ' + ctx2) if ctx2 else ''})")
            print(f"    name: {obj.get('name')!r}")
            print(f"    plu:  {obj.get('plu')}")
            tags = obj.get("itemTags") or []
            if not tags:
                print(f"    tags: (none)")
            else:
                print(f"    tags ({len(tags)}):")
                for t in tags:
                    print(f"      - {t.get('name')!r}  ({t.get('guid')})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
