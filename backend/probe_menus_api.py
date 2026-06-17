"""
probe_menus_api.py — Does Toast's /menus/v2/menus actually return a given item?

Settles the question: "Is item X missing from cfg_menu_items because the Menus
API never returns it (a Toast/menu-visibility gap), or because our builder
doesn't traverse to it (a code gap)?"

It fetches the SAME endpoint, with the SAME auth and fallback path the real sync
uses (imported straight from sync_toast_config), then:
  * searches the RAW JSON for the item (definitive — independent of how we parse)
  * reports WHERE it appears (top-level menuItem / nested subgroup / modifier ref)
  * prints the menu structure, including whether nested subgroups exist — which
    is exactly what the current builder loop (one level deep) would miss.

Run on Cloudways (needs the Toast creds in backend/.env):
  cd /home/master/applications/wpftndhcuj/public_html/backend
  venv/bin/python probe_menus_api.py \
      --plu 30014013 --name "Strawberry Boba" \
      --guid 80738766-8832-40a1-a01f-bf0b4fc9b3e4
Options:
  --restaurant-guid <guid>   probe a single store instead of scanning
  --max-stores N             how many active stores to scan (default 8)
"""

import argparse
import asyncio
import json

import httpx

from sync_toast_config import (
    _get_standard_auth_headers,
    _get_active_locations,
    _fetch_menus_with_fallback,
)


def _walk_groups(group, depth=0):
    """Yield (depth, group) for a group AND all nested subgroups (recursive)."""
    yield depth, group
    for sub in (group.get("menuGroups") or []):
        yield from _walk_groups(sub, depth + 1)


def _structure(menus_data):
    n_top_groups = nested = n_items = 0
    for menu in (menus_data.get("menus") or []):
        for top in (menu.get("menuGroups") or []):
            n_top_groups += 1
            for depth, group in _walk_groups(top):
                if depth > 0:
                    nested += 1
                n_items += len(group.get("menuItems") or [])
    return {
        "menus": len(menus_data.get("menus") or []),
        "top_menuGroups": n_top_groups,
        "nested_subgroups": nested,
        "menuItems_incl_nested": n_items,
        "modifierGroupReferences": len(menus_data.get("modifierGroupReferences") or {}),
        "modifierOptionReferences": len(menus_data.get("modifierOptionReferences") or {}),
    }


def _locate(menus_data, terms):
    """Human-readable paths where any term appears in the parsed structure."""
    hits = []
    tl = [t.lower() for t in terms if t]

    def matches(obj):
        h = json.dumps(obj, default=str).lower()
        return any(t in h for t in tl)

    for mi, menu in enumerate(menus_data.get("menus") or []):
        for top in (menu.get("menuGroups") or []):
            for depth, group in _walk_groups(top):
                for item in (group.get("menuItems") or []):
                    if matches(item):
                        hits.append(
                            f"menus[{mi}] > group '{group.get('name','?')}' "
                            f"(subgroup depth {depth}) > menuItem '{item.get('name')}' "
                            f"guid={item.get('guid')} plu={item.get('plu')} "
                            f"mli={item.get('multiLocationId')}"
                        )
    for gref, mg in (menus_data.get("modifierGroupReferences") or {}).items():
        for opt in (mg.get("modifierOptions") or []):
            if matches(opt):
                hits.append(
                    f"modifierGroupReferences[{gref}] '{mg.get('name','?')}' > "
                    f"option '{opt.get('name')}' guid={opt.get('guid')} "
                    f"plu={opt.get('plu')} mli={opt.get('multiLocationId')}"
                )
    for oref, opt in (menus_data.get("modifierOptionReferences") or {}).items():
        if matches(opt):
            hits.append(
                f"modifierOptionReferences[{oref}] '{opt.get('name')}' "
                f"guid={opt.get('guid')} plu={opt.get('plu')} "
                f"mli={opt.get('multiLocationId')}"
            )
    return hits


async def main(args):
    terms = [t for t in [args.guid, args.name, str(args.plu) if args.plu else "", args.mli] if t]
    if not terms:
        print("Give at least one of --guid / --name / --plu / --mli to search for.")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        if args.restaurant_guid:
            locations = [{"restaurantGuid": args.restaurant_guid, "restaurantName": "(specified)"}]
        else:
            locations = await _get_active_locations(client)

        scan = locations[: args.max_stores]
        print(f"Searching for {terms}")
        print(f"Probing {len(scan)} of {len(locations)} active stores "
              f"(same endpoint/auth as the real sync)\n")

        for i, loc in enumerate(scan):
            guid = loc["restaurantGuid"]
            name = loc.get("restaurantName", "")
            headers = await _get_standard_auth_headers()
            headers["Toast-Restaurant-External-ID"] = guid

            menus = await _fetch_menus_with_fallback(client, headers)
            if not menus:
                print(f"[{i}] {name} {guid}: no menu data returned")
                await asyncio.sleep(3)
                continue

            raw = json.dumps(menus, default=str).lower()
            present = any(t.lower() in raw for t in terms)
            print(f"[{i}] {name} {guid}: present_in_raw_json={present}  structure={_structure(menus)}")

            hits = _locate(menus, terms)
            for h in hits:
                print(f"      WHERE: {h}")

            if present:
                print("\n>>> The Menus API DOES return this item for this store.")
                if not hits:
                    print(">>> But our builder's traversal never reaches it — it lives somewhere we")
                    print(">>> don't walk (likely a nested subgroup or an un-referenced modifier). CODE GAP.")
                else:
                    print(">>> If the path above shows 'subgroup depth > 0', the builder only walks one")
                    print(">>> level deep, so it's dropping it. CODE GAP we can fix in toast_config_builder.")
                return

            await asyncio.sleep(3)

        print("\n>>> Not found in any probed store's raw Menus API response.")
        print(">>> If you scanned enough stores (try --max-stores 30), that's a genuine API gap")
        print(">>> and the Toast Web item export is the way in.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--guid", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--plu", type=int, default=0)
    ap.add_argument("--mli", default="")
    ap.add_argument("--restaurant-guid", default="")
    ap.add_argument("--max-stores", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main(args))
