"""
diagnose_recipe_tag.py - pinpoint WHERE a stale recipe tag is still coming from.

Run on the box that owns the live toast_config.db:

    cd backend
    python diagnose_recipe_tag.py "half"            # match item name substring
    python diagnose_recipe_tag.py "half" TurkeyBreast   # also highlight one tag

For every menu item whose name matches, it prints the three data sources the
menu-performance export walks for the "recipe" column, so we can see which one
still holds the removed tag:

  [A] cfg_menu_items.item_tags   - JSON column, per store (feeds mli_plu_attribute_map)
  [B] cfg_map_item_tags+cfg_tags - the mapping table, PER STORE  (tier-2: get_live_item_tag_attrs)
  [C] mli_plu_attribute_map      - rebuilt attribute map          (tier-3: Excel/UI)
  [D] get_live_item_tag_attrs()  - exactly what tier-2 returns for the item

Tier-1 (live Toast V3 API) is NOT shown here - if A/B/C/D are all clean but the
export still shows the tag, the culprit is the live fetch_toast_menus cache and
the fix is an X-Force-Refresh run, not a data change.
"""
import json
import sqlite3
import sys

import toast_config_db as db


def main():
    name_sub = sys.argv[1] if len(sys.argv) > 1 else "half"
    highlight = sys.argv[2] if len(sys.argv) > 2 else None
    path = db.TOAST_CONFIG_DB_PATH

    def mark(s: str) -> str:
        if highlight and highlight.lower() in (s or "").lower():
            return f">>> {s} <<<"
        return s

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    items = conn.execute(
        """SELECT DISTINCT item_guid, item_name, multi_location_id
             FROM cfg_menu_items
            WHERE lower(item_name) LIKE ? AND item_guid != ''
            ORDER BY item_name""",
        (f"%{name_sub.lower()}%",),
    ).fetchall()

    if not items:
        print(f"No menu items matching {name_sub!r}.")
        return

    live = db.get_live_item_tag_attrs(path)  # tier-2, exactly as the export sees it
    print(f"Found {len(items)} distinct item(s) matching {name_sub!r}\n")

    for it in items:
        guid, iname, mli = it["item_guid"], it["item_name"], it["multi_location_id"]
        print("=" * 78)
        print(f"{iname}   guid={guid}   mli={mli}")

        # [A] item_tags JSON per store
        rows = conn.execute(
            "SELECT store_guid, item_tags FROM cfg_menu_items WHERE item_guid=?",
            (guid,),
        ).fetchall()
        json_tagsets = {}
        for r in rows:
            try:
                names = tuple(sorted(t.get("name", "") for t in json.loads(r["item_tags"] or "[]")))
            except (TypeError, ValueError):
                names = ("<bad json>",)
            json_tagsets.setdefault(names, []).append(r["store_guid"])
        print(f"  [A] item_tags JSON - {len(rows)} store-rows, {len(json_tagsets)} distinct tag sets:")
        for names, stores in json_tagsets.items():
            print(f"        {mark(', '.join(names) or '(none)')}   x{len(stores)} stores")

        # [B] mapping table per store
        maprows = conn.execute(
            """SELECT mit.store_guid, t.tag_name
                 FROM cfg_map_item_tags mit
                 JOIN cfg_tags t ON t.store_guid=mit.store_guid AND t.tag_guid=mit.tag_guid
                WHERE mit.item_guid=? AND t.tag_name!=''
                ORDER BY mit.store_guid, t.tag_name""",
            (guid,),
        ).fetchall()
        per_store = {}
        for r in maprows:
            per_store.setdefault(r["store_guid"], []).append(r["tag_name"])
        print(f"  [B] cfg_map_item_tags - {len(per_store)} stores carry a mapping:")
        # collapse identical tag sets across stores for readability
        collapsed = {}
        for store, tags in per_store.items():
            collapsed.setdefault(tuple(sorted(tags)), []).append(store)
        for tags, stores in collapsed.items():
            shown = ", ".join(mark(t) for t in tags)
            print(f"        {shown or '(none)'}   x{len(stores)} stores  e.g. {stores[0]}")

        # [C] attribute map
        amr = conn.execute(
            "SELECT recipe, consolidated_cat FROM mli_plu_attribute_map WHERE item_guid=?",
            (guid,),
        ).fetchall()
        for r in amr:
            print(f"  [C] mli_plu_attribute_map recipe={mark(r['recipe'] or '(empty)')}  consol={r['consolidated_cat']!r}")
        if not amr:
            print("  [C] mli_plu_attribute_map: no row")

        # [D] tier-2 result as the export consumes it
        d = live.get(guid, {})
        print(f"  [D] get_live_item_tag_attrs -> recipe={mark(d.get('recipe') or '(empty)')}  consol={d.get('consolidated_cat')!r}")

    conn.close()


if __name__ == "__main__":
    main()
