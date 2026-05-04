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
import sqlite3
import sys
from typing import Optional

import httpx

# Reuse main.py's auth helpers + config
import main as backend_main
import toast_config_db


def _stores_with_item(item_guid: str) -> list:
    """Look up which store_guids carry this item via cfg_menu_items."""
    db = toast_config_db.TOAST_CONFIG_DB_PATH
    try:
        with sqlite3.connect(db) as c:
            return [
                {"store_guid": r[0], "store_name": r[1] or ""}
                for r in c.execute(
                    """SELECT DISTINCT cmi.store_guid,
                              COALESCE(cmi2.store_num, '')
                       FROM cfg_menu_items cmi
                       LEFT JOIN cfg_menu_items cmi2 ON cmi2.store_guid = cmi.store_guid
                       WHERE cmi.item_guid = ?""", (item_guid,)
                ).fetchall()
            ]
    except Exception:
        return []


async def _get_active_locations(client: httpx.AsyncClient) -> list:
    """Pull active locations from the Toast ERA API (matches sync_toast_config)."""
    # Lazy import — sync_toast_config has its own ERA auth helpers we can reuse.
    from sync_toast_config import _get_active_locations as _f
    return await _f(client)


async def probe(item_guid: str, restaurant_guid: Optional[str]) -> None:
    base = backend_main.TOAST_STANDARD_API_BASE_URL

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Resolve restaurant_guid: explicit arg → SQLite lookup → ERA API list.
        if not restaurant_guid:
            stores = _stores_with_item(item_guid)
            if stores:
                print(f"Found {len(stores)} store(s) carrying this item in SQLite:")
                for s in stores[:10]:
                    print(f"  - {s['store_guid']}")
                restaurant_guid = stores[0]["store_guid"]
                print(f"\nProbing first one: {restaurant_guid}")
            else:
                print("Item not in cfg_menu_items; falling back to ERA active locations…")
                try:
                    locs = await _get_active_locations(client)
                except Exception as e:
                    print(f"ERROR: ERA locations call failed: {e}")
                    print("Pass a restaurant_guid as the 2nd arg to skip the lookup.")
                    return
                if not locs:
                    print("ERROR: no active restaurants found"); return
                restaurant_guid = locs[0].get("restaurantGuid") or locs[0].get("guid")
                print(f"Using first active restaurant: {restaurant_guid}")

        headers = await backend_main.get_standard_api_auth_headers()
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
