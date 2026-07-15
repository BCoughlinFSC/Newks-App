"""
toast_config_builder.py  —  Business logic for building Toast config data in SQLite.

Replaces SQL Server stored procedures:
  SP #1: sp_tbl_MLI_PLU_Attribute_Map  → build_attribute_map()
  SP #2: sp_merge_menu_cfg_tables      → merge_menu_config_from_api()

Usage
-----
  from toast_config_builder import merge_menu_config_from_api, build_attribute_map
"""

import json
import logging
import re
from typing import Dict, List, Optional

import toast_config_db

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# SP #2: merge_menu_config_from_api
# Replaces sp_merge_menu_cfg_tables — extracts menu config from Toast V3
# API response and persists to toast_config.db
# ──────────────────────────────────────────────────────────────────────────────

def merge_menu_config_from_api(
    menus_data: dict,
    store_guid: str,
    store_num: int = 0,
    db_path: str = toast_config_db.TOAST_CONFIG_DB_PATH,
) -> dict:
    """
    Parse a Toast Menus V3 API response and upsert into toast_config.db.

    Parameters
    ----------
    menus_data : dict
        Raw JSON from /menus/v3/menus endpoint.
    store_guid : str
        Restaurant GUID.
    store_num : int
        Numeric store ID (0 if unknown).

    Returns
    -------
    dict with counts: {menu_items, tags, item_tags, sales_cats}
    """
    toast_config_db.initialize_db(db_path)

    menu_items: List[dict] = []
    tags_seen: Dict[str, dict] = {}  # tag_guid -> tag dict (dedup)
    item_tags: List[dict] = []
    sales_cats_seen: Dict[str, dict] = {}  # category_guid -> cat dict (dedup)

    # ── Process menu items from menus[].menuGroups[].menuItems[] ──
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                _extract_menu_item(
                    item, store_guid, store_num, menu_items,
                    tags_seen, item_tags, sales_cats_seen
                )

    # ── Process modifiers from modifierGroupReferences ──
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    for _group_guid, mod_group in modifier_group_refs.items():
        for mod_option in mod_group.get("modifierOptions", []):
            _extract_menu_item(
                mod_option, store_guid, store_num, menu_items,
                tags_seen, item_tags, sales_cats_seen
            )

    # ── Process modifierOptionReferences ──
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    for _option_id, mod_option in modifier_option_refs.items():
        # These use "guid" field for the actual GUID
        if not mod_option.get("guid"):
            mod_option["guid"] = str(_option_id)
        _extract_menu_item(
            mod_option, store_guid, store_num, menu_items,
            tags_seen, item_tags, sales_cats_seen
        )

    # ── Upsert everything ──
    n_items = toast_config_db.upsert_menu_items(menu_items, store_guid, db_path)
    n_tags = toast_config_db.upsert_tags(list(tags_seen.values()), store_guid, db_path)
    # Every item/modifier guid we saw in this payload — the authoritative scope
    # for pruning stale tag rows. Using this (rather than only the guids that
    # still have tags) ensures an item stripped of ALL tags in Toastweb also
    # gets its orphaned cfg_map_item_tags rows cleared.
    seen_item_guids = [mi["item_guid"] for mi in menu_items if mi.get("item_guid")]
    n_item_tags = toast_config_db.replace_item_tags_for_items(
        item_tags, seen_item_guids, store_guid, db_path
    )
    n_cats = toast_config_db.upsert_sales_cats(list(sales_cats_seen.values()), store_guid, db_path)

    result = {
        "menu_items": n_items,
        "tags": n_tags,
        "item_tags": n_item_tags,
        "sales_cats": n_cats,
    }
    logger.info(
        f"merge_menu_config_from_api({store_guid}): "
        f"{n_items} items, {n_tags} tags, {n_item_tags} item-tags, {n_cats} sales cats"
    )
    return result


def _extract_menu_item(
    item: dict,
    store_guid: str,
    store_num: int,
    menu_items: List[dict],
    tags_seen: Dict[str, dict],
    item_tags: List[dict],
    sales_cats_seen: Dict[str, dict],
) -> None:
    """Extract a single menu item/modifier into the accumulator lists."""
    item_guid = item.get("guid", "")
    if not item_guid:
        return

    display_name = item.get("displayName") or item.get("name") or ""
    kitchen_name = item.get("kitchenName") or ""
    multi_location_id = str(item.get("multiLocationId") or "")
    plu = item.get("plu") or 0
    try:
        plu = int(plu)
    except (ValueError, TypeError):
        plu = 0

    # Sales category
    sc = item.get("salesCategory") or {}
    sc_guid = sc.get("guid") or ""
    sc_name = sc.get("name") or ""
    if sc_guid and sc_guid not in sales_cats_seen:
        sales_cats_seen[sc_guid] = {
            "category_guid": sc_guid,
            "category_name": sc_name,
        }

    # Item tags — build JSON string and collect tag definitions + mappings
    raw_tags = item.get("itemTags", [])
    tag_list_json = []
    for tag in raw_tags:
        tag_guid = tag.get("guid", "")
        tag_name = tag.get("name", "")
        if not tag_guid:
            continue
        tag_list_json.append({"guid": tag_guid, "name": tag_name})

        # Collect unique tags
        if tag_guid not in tags_seen:
            tags_seen[tag_guid] = {
                "tag_guid": tag_guid,
                "tag_name": tag_name,
            }

        # Item-tag mapping
        item_tags.append({
            "item_guid": item_guid,
            "tag_guid": tag_guid,
            "item_plu": plu,
            "multi_location_id": multi_location_id,
        })

    menu_items.append({
        "store_num": store_num,
        "item_name": display_name,
        "item_pos_name": display_name,
        "item_guid": item_guid,
        "item_price": float(item.get("price") or 0),
        "item_pricing_strategy": item.get("pricingStrategy") or "",
        "item_is_deferred": int(item.get("isDeferred") or False),
        "item_is_discountable": int(item.get("isDiscountable", True)),
        "sales_cat_guid": sc_guid,
        "sales_cat": sc_name,
        "item_plu": plu,
        "item_sku": item.get("sku") or "",
        "item_kitchen_name": kitchen_name,
        "guest_count": 0,  # Updated by build_attribute_map
        "master_id": str(item.get("masterId") or ""),
        "multi_location_id": multi_location_id,
        "item_is_combo_parent": int(item.get("isComboParent") or False),
        "item_tags": json.dumps(tag_list_json),
    })


# ──────────────────────────────────────────────────────────────────────────────
# SP #1: build_attribute_map
# Replaces sp_tbl_MLI_PLU_Attribute_Map — parses item_tags JSON and builds
# the master MLI → PLU/category/recipe/EE/guest-count lookup table.
# ──────────────────────────────────────────────────────────────────────────────

# Regex patterns matching the SQL Server tag parsing logic
_RE_GUEST_COUNT = re.compile(r"^Guest\s*Count\s+([\d.]+)$", re.IGNORECASE)
_RE_EE = re.compile(r"^EE\s*([\d.]+)$", re.IGNORECASE)
_RE_CONSOLIDATED = re.compile(r"^\*\s*Consolidated\s", re.IGNORECASE)
_RE_LTO = re.compile(r"LTO", re.IGNORECASE)


def build_attribute_map(
    db_path: str = toast_config_db.TOAST_CONFIG_DB_PATH,
) -> int:
    """
    Build the mli_plu_attribute_map table from cfg_menu_items data.

    For each unique (multi_location_id, item_guid), parses item_tags JSON
    to extract:
      - consolidated_cat: tags matching "* Consolidated ..." or "Catering"
      - guest_count_tag: tags matching "Guest Count X.X"
      - entree_equiv_tag: tags matching "EE X.X"
      - lto_tag: tags containing "LTO"
      - recipe: any other tag (not Guest/Cons/EE/LTO)

    Also joins to cfg_map_plu_toast_to_aloha for Aloha PLU cross-reference.

    Returns the number of rows upserted.
    """
    toast_config_db.initialize_db(db_path)

    # Read all menu items (deduplicated by MLI)
    items_df = toast_config_db.get_all_menu_items(db_path)
    if items_df.empty:
        logger.warning("build_attribute_map: no menu items in toast_config.db")
        return 0

    # Load PLU cross-reference
    plu_map = _load_plu_crossref(db_path)

    # Preserve user-edited Aloha columns across rebuild — without this lookup,
    # build_attribute_map would write aloha_item_name="" and clobber every
    # manual edit made via the Mapping Table UI or sync_mapping_excel_to_db.py.
    # Only used as a fallback for (mli, item_guid) keys that already have a row.
    existing_aloha = _load_existing_aloha_overrides(db_path)

    rows: List[dict] = []
    seen = set()

    for _, item in items_df.iterrows():
        mli = str(item.get("multi_location_id", ""))
        item_guid = str(item.get("item_guid", ""))
        if not mli:
            continue
        key = (mli, item_guid)
        if key in seen:
            continue
        seen.add(key)

        toast_plu = int(item.get("item_plu", 0) or 0)
        toast_item_name = str(item.get("item_name", ""))

        # Parse item_tags JSON
        tags_json = item.get("item_tags", "[]")
        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        except (json.JSONDecodeError, TypeError):
            tags = []

        consolidated_cat = ""
        recipe = ""
        guest_count_tag = 0.0
        entree_equiv_tag = 0.0
        lto_tag = ""

        for tag in tags:
            name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
            if not name:
                continue

            # Consolidated category: "* Consolidated ..." or "Catering"
            if _RE_CONSOLIDATED.match(name) or name == "Catering":
                consolidated_cat = name
                continue

            # Guest count: "Guest Count 1.0"
            m = _RE_GUEST_COUNT.match(name)
            if m:
                try:
                    guest_count_tag = float(m.group(1))
                except ValueError:
                    pass
                continue

            # Entree equivalent: "EE 2.0" or "EE2.0"
            m = _RE_EE.match(name)
            if m:
                try:
                    entree_equiv_tag = float(m.group(1))
                except ValueError:
                    pass
                continue

            # LTO
            if _RE_LTO.search(name):
                lto_tag = name
                continue

            # Everything else is a recipe tag
            if not recipe:
                recipe = name

        # Aloha columns: prefer the existing values in mli_plu_attribute_map
        # (set by user edits via PUT /api/mapping-table or sync_mapping_excel_to_db).
        # Fall back to the PLU cross-reference / empty string only for genuinely
        # new (mli, item_guid) keys that don't have a row yet.
        prior = existing_aloha.get(key)
        if prior is not None:
            aloha_plu = prior["aloha_plu"] or plu_map.get(mli, 0)
            aloha_item_name = prior["aloha_item_name"]
        else:
            aloha_plu = plu_map.get(mli, 0)
            aloha_item_name = ""

        rows.append({
            "multi_location_id": mli,
            "item_guid": item_guid,
            "toast_plu": toast_plu,
            "aloha_plu": aloha_plu,
            "toast_item_name": toast_item_name,
            "aloha_item_name": aloha_item_name,
            "consolidated_cat": consolidated_cat,
            "recipe": recipe,
            "guest_count_tag": guest_count_tag,
            "entree_equiv_tag": entree_equiv_tag,
            "lto_tag": lto_tag,
        })

    count = toast_config_db.upsert_attribute_map(rows, db_path)
    logger.info(f"build_attribute_map: {count} rows upserted into mli_plu_attribute_map")
    return count


def _load_existing_aloha_overrides(db_path: str) -> Dict[tuple, dict]:
    """
    Snapshot the user-managed Aloha columns from mli_plu_attribute_map keyed by
    (multi_location_id, item_guid). Used by build_attribute_map to preserve
    UI / xlsx-sync edits when rebuilding from Toast menu data.

    Empty dict on first run (table not yet created) or any read error.
    """
    try:
        with toast_config_db.get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT multi_location_id, item_guid, aloha_plu, aloha_item_name"
                "  FROM mli_plu_attribute_map"
            ).fetchall()
        return {
            (str(r[0]), str(r[1])): {
                "aloha_plu":       int(r[2] or 0),
                "aloha_item_name": str(r[3] or ""),
            }
            for r in rows
        }
    except Exception:
        return {}


def _load_plu_crossref(db_path: str) -> Dict[str, int]:
    """Load {multi_location_id: aloha_plu} from the PLU cross-reference table."""
    try:
        with toast_config_db.get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT multi_location_id, aloha_plu FROM cfg_map_plu_toast_to_aloha"
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
