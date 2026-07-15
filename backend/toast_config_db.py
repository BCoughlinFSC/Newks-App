"""
toast_config_db.py  —  SQLite persistence layer for Toast menu configuration data.

Replaces SQL Server tables: cfg_menu_items, cfg_tags, cfg_map_item_tags,
cfg_Sales_Cats, cfg_map_PLU_Toast_to_Aloha, and the master MLI_PLU_Attribute_Map.

Schema
------
  cfg_menu_items        — Menu items from Toast Menus V3 API
  cfg_tags              — Tag definitions with consolidated category IDs
  cfg_map_item_tags     — Item-to-tag assignments
  cfg_sales_cats        — Sales categories
  cfg_map_plu_toast_to_aloha  — Toast PLU → Aloha PLU cross-reference
  consolidated_category_ids   — Hardcoded business rule from sp_merge_menu_cfg_tables
  mli_plu_attribute_map       — Master reference table (SP #1 output)

Usage
-----
  from toast_config_db import initialize_db, upsert_menu_items, get_attribute_map, get_stats
"""

import json
import logging
import os
import re
import sqlite3
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Default path — parallel to other data directories
TOAST_CONFIG_DB_PATH = os.path.join(
    os.path.dirname(__file__), "data", "toast_config", "toast_config.db"
)

# ──────────────────────────────────────────────────────────────────────────────
# Consolidated Category IDs — extracted from sp_merge_menu_cfg_tables
# Note: "* Consolidated  Soups" has a double space (matches the SP exactly)
# ──────────────────────────────────────────────────────────────────────────────

CONSOLIDATED_CATEGORY_IDS = {
    "* Consolidated  Soups": 9002,
    "* Consolidated 2oz 4oz Dressings": 9097,
    "* Consolidated Beer": 9007,
    "* Consolidated Bulk GnG": 9011,
    "* Consolidated Catering": 27,
    "* Consolidated Desserts": 9010,
    "* Consolidated Extras and Misc": 9012,
    "* Consolidated Modifiers": 9099,
    "* Consolidated NA Bev": 9006,
    "* Consolidated Non Food": 9014,
    "* Consolidated Pasta and Mac": 9003,
    "* Consolidated Pizza": 9001,
    "* Consolidated Salads": 9005,
    "* Consolidated Sandwiches": 9004,
    "* Consolidated Sides": 9009,
    "* Consolidated Wine and Spirits": 9008,
    "* Consolidated Wraps": 9017,
}

# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cfg_menu_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    store_guid            TEXT NOT NULL,
    store_num             INTEGER,
    item_name             TEXT NOT NULL DEFAULT '',
    item_pos_name         TEXT NOT NULL DEFAULT '',
    item_guid             TEXT NOT NULL DEFAULT '',
    item_price            REAL DEFAULT 0,
    item_pricing_strategy TEXT DEFAULT '',
    item_is_deferred      INTEGER DEFAULT 0,
    item_is_discountable  INTEGER DEFAULT 1,
    sales_cat_guid        TEXT DEFAULT '',
    sales_cat             TEXT DEFAULT '',
    item_plu              INTEGER DEFAULT 0,
    item_sku              TEXT DEFAULT '',
    item_kitchen_name     TEXT DEFAULT '',
    guest_count           REAL DEFAULT 0,
    master_id             TEXT DEFAULT '',
    multi_location_id     TEXT NOT NULL DEFAULT '',
    item_is_combo_parent  INTEGER DEFAULT 0,
    item_tags             TEXT DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_mi_unique
    ON cfg_menu_items(store_guid, multi_location_id, sales_cat_guid);

CREATE INDEX IF NOT EXISTS idx_cfg_mi_mli
    ON cfg_menu_items(multi_location_id);

CREATE INDEX IF NOT EXISTS idx_cfg_mi_store
    ON cfg_menu_items(store_guid);

CREATE TABLE IF NOT EXISTS cfg_tags (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    store_guid        TEXT NOT NULL,
    tag_guid          TEXT NOT NULL,
    tag_name          TEXT NOT NULL DEFAULT '',
    master_id         TEXT DEFAULT '',
    multi_location_id TEXT DEFAULT '',
    consol_cat_id     INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_tags_unique
    ON cfg_tags(store_guid, tag_guid);

CREATE INDEX IF NOT EXISTS idx_cfg_tags_name
    ON cfg_tags(tag_name);

CREATE TABLE IF NOT EXISTS cfg_map_item_tags (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    store_guid        TEXT NOT NULL,
    item_guid         TEXT NOT NULL,
    tag_guid          TEXT NOT NULL,
    item_plu          INTEGER DEFAULT 0,
    multi_location_id TEXT DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_mit_unique
    ON cfg_map_item_tags(store_guid, multi_location_id, tag_guid);

CREATE TABLE IF NOT EXISTS cfg_sales_cats (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    store_guid        TEXT NOT NULL,
    category_guid     TEXT NOT NULL,
    category_name     TEXT NOT NULL DEFAULT '',
    master_id         TEXT DEFAULT '',
    multi_location_id TEXT DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_sc_unique
    ON cfg_sales_cats(store_guid, category_guid);

CREATE TABLE IF NOT EXISTS cfg_map_plu_toast_to_aloha (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    multi_location_id TEXT NOT NULL,
    toast_plu         INTEGER NOT NULL DEFAULT 0,
    aloha_plu         INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_plu_unique
    ON cfg_map_plu_toast_to_aloha(multi_location_id);

CREATE TABLE IF NOT EXISTS consolidated_category_ids (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name  TEXT NOT NULL UNIQUE,
    consol_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mli_plu_attribute_map (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    multi_location_id TEXT NOT NULL,
    item_guid         TEXT NOT NULL DEFAULT '',
    toast_plu         INTEGER DEFAULT 0,
    aloha_plu         INTEGER DEFAULT 0,
    toast_item_name   TEXT NOT NULL DEFAULT '',
    aloha_item_name   TEXT NOT NULL DEFAULT '',
    consolidated_cat  TEXT NOT NULL DEFAULT '',
    recipe            TEXT NOT NULL DEFAULT '',
    guest_count_tag   REAL DEFAULT 0,
    entree_equiv_tag  REAL DEFAULT 0,
    lto_tag           TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mli_unique
    ON mli_plu_attribute_map(multi_location_id, item_guid);

CREATE INDEX IF NOT EXISTS idx_mli_consol
    ON mli_plu_attribute_map(consolidated_cat);

-- ────────────────────────────────────────────────────────────────────────────
-- plu_ranges  —  Aloha & Toast PLU range → "Range Name" lookup table
-- Sourced from the "Item PLU Ranges.xlsx" export (Newk's master ranges file).
-- Used to stamp every menu performance row with an "Item Classification"
-- (Aloha-style, broader) and "Item Subclass" (Toast-style, more granular).
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plu_ranges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_type    TEXT NOT NULL,           -- 'Aloha' | 'Toast'
    range_name  TEXT NOT NULL,
    range_start INTEGER NOT NULL,
    range_end   INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_plu_ranges_unique
    ON plu_ranges(pos_type, range_start, range_end);

CREATE INDEX IF NOT EXISTS idx_plu_ranges_lookup
    ON plu_ranges(pos_type, range_start);

-- ────────────────────────────────────────────────────────────────────────────
-- toast_switchover_dates  —  per-store cutover from Aloha to Toast
-- Used by the menu performance YoY pipeline to decide whether the Last Year
-- (LY) data for a given store should come from Aloha (pre-switchover) or
-- Toast (post-switchover). Populated from Toast Switchover Dates.xlsx via
-- import_switchover_dates.py.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS toast_switchover_dates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    store_guid      TEXT NOT NULL UNIQUE,
    switchover_date TEXT NOT NULL  -- ISO yyyy-mm-dd
);

CREATE INDEX IF NOT EXISTS idx_switchover_lookup
    ON toast_switchover_dates(store_guid);

-- ────────────────────────────────────────────────────────────────────────────
-- aloha_items_master  —  Canonical Aloha item list (PLU + name + category).
--
-- Source: "Items by Category.xlsx" in the project root. One row per Aloha
-- PLU. Populated by backend/import_aloha_items.py; refreshed by dropping
-- in a new file and re-running the script.
--
-- Used by the Mapping Table UI to drive a searchable dropdown when picking
-- aloha_item_name. Selecting a name auto-fills aloha_plu so the two stay
-- consistent and the user can't typo into a non-existent item.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aloha_items_master (
    aloha_plu       INTEGER PRIMARY KEY,
    aloha_item_name TEXT NOT NULL,
    consol_id       INTEGER,
    consol_cat      TEXT NOT NULL DEFAULT '',
    item_range      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_aloha_items_consol
    ON aloha_items_master(consol_cat);

CREATE INDEX IF NOT EXISTS idx_aloha_items_name
    ON aloha_items_master(aloha_item_name);
"""


# ──────────────────────────────────────────────────────────────────────────────
# Connection + Schema
# ──────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str = TOAST_CONFIG_DB_PATH) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
    return conn


def initialize_db(db_path: str = TOAST_CONFIG_DB_PATH) -> None:
    """Create the database file, schema, and seed consolidated category IDs."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(_CREATE_SQL)
        _seed_consolidated_category_ids(conn)


def _seed_consolidated_category_ids(conn: sqlite3.Connection) -> None:
    """Insert the 17 hardcoded consolidated category ID mappings if not present."""
    for tag_name, consol_id in CONSOLIDATED_CATEGORY_IDS.items():
        conn.execute(
            "INSERT OR IGNORE INTO consolidated_category_ids (tag_name, consol_id)"
            " VALUES (?, ?)",
            (tag_name, consol_id),
        )


def db_available(db_path: str = TOAST_CONFIG_DB_PATH) -> bool:
    """Return True if the database file exists and has menu items."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT 1 FROM cfg_menu_items LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Writes — cfg_menu_items
# ──────────────────────────────────────────────────────────────────────────────

def upsert_menu_items(
    items: List[dict],
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """
    Upsert menu items for a store. Each dict should have keys matching
    the cfg_menu_items columns (snake_case). item_tags should be a JSON string.
    Returns the number of rows upserted.
    """
    if not items:
        return 0
    initialize_db(db_path)
    rows = []
    for item in items:
        rows.append((
            store_guid,
            item.get("store_num", 0),
            item.get("item_name", ""),
            item.get("item_pos_name", ""),
            item.get("item_guid", ""),
            item.get("item_price", 0),
            item.get("item_pricing_strategy", ""),
            int(item.get("item_is_deferred", 0)),
            int(item.get("item_is_discountable", 1)),
            item.get("sales_cat_guid", ""),
            item.get("sales_cat", ""),
            item.get("item_plu", 0),
            item.get("item_sku", ""),
            item.get("item_kitchen_name", ""),
            item.get("guest_count", 0),
            item.get("master_id", ""),
            item.get("multi_location_id", ""),
            int(item.get("item_is_combo_parent", 0)),
            item.get("item_tags", "[]"),
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO cfg_menu_items (
                store_guid, store_num, item_name, item_pos_name, item_guid,
                item_price, item_pricing_strategy, item_is_deferred,
                item_is_discountable, sales_cat_guid, sales_cat, item_plu,
                item_sku, item_kitchen_name, guest_count, master_id,
                multi_location_id, item_is_combo_parent, item_tags
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(store_guid, multi_location_id, sales_cat_guid)
            DO UPDATE SET
                store_num=excluded.store_num,
                item_name=excluded.item_name,
                item_pos_name=excluded.item_pos_name,
                item_guid=excluded.item_guid,
                item_price=excluded.item_price,
                item_pricing_strategy=excluded.item_pricing_strategy,
                item_is_deferred=excluded.item_is_deferred,
                item_is_discountable=excluded.item_is_discountable,
                sales_cat=excluded.sales_cat,
                item_plu=excluded.item_plu,
                item_sku=excluded.item_sku,
                item_kitchen_name=excluded.item_kitchen_name,
                guest_count=excluded.guest_count,
                master_id=excluded.master_id,
                item_is_combo_parent=excluded.item_is_combo_parent,
                item_tags=excluded.item_tags
            """,
            rows,
        )
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes — cfg_tags
# ──────────────────────────────────────────────────────────────────────────────

def upsert_tags(
    tags: List[dict],
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """Upsert tag definitions for a store. Applies consolidated category IDs."""
    if not tags:
        return 0
    initialize_db(db_path)

    # Look up consolidated category IDs
    consol_map = get_consolidated_cat_map(db_path)

    rows = []
    for tag in tags:
        tag_name = tag.get("tag_name", "")
        consol_id = consol_map.get(tag_name)
        rows.append((
            store_guid,
            tag.get("tag_guid", ""),
            tag_name,
            tag.get("master_id", ""),
            tag.get("multi_location_id", ""),
            consol_id,
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO cfg_tags (
                store_guid, tag_guid, tag_name, master_id,
                multi_location_id, consol_cat_id
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(store_guid, tag_guid)
            DO UPDATE SET
                tag_name=excluded.tag_name,
                master_id=excluded.master_id,
                multi_location_id=excluded.multi_location_id,
                consol_cat_id=excluded.consol_cat_id
            """,
            rows,
        )
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes — cfg_map_item_tags
# ──────────────────────────────────────────────────────────────────────────────

def upsert_item_tags(
    mappings: List[dict],
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """Upsert item-to-tag assignments for a store."""
    if not mappings:
        return 0
    initialize_db(db_path)
    rows = []
    for m in mappings:
        rows.append((
            store_guid,
            m.get("item_guid", ""),
            m.get("tag_guid", ""),
            m.get("item_plu", 0),
            m.get("multi_location_id", ""),
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO cfg_map_item_tags (
                store_guid, item_guid, tag_guid, item_plu, multi_location_id
            ) VALUES (?,?,?,?,?)
            ON CONFLICT(store_guid, multi_location_id, tag_guid)
            DO UPDATE SET
                item_guid=excluded.item_guid,
                item_plu=excluded.item_plu
            """,
            rows,
        )
    return len(rows)


def replace_item_tags_for_items(
    mappings: List[dict],
    seen_item_guids: List[str],
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """
    Authoritatively replace the tag assignments for a store, but ONLY for the
    items we actually received fresh data about in this sync (``seen_item_guids``).

    This fixes the "removed tag never disappears" bug: ``upsert_item_tags`` only
    ever inserts/updates, so a tag removed in Toastweb left an orphan row in
    cfg_map_item_tags forever — and get_live_item_tag_attrs() (tier-2 fallback in
    the menu performance report) would keep resurrecting it (e.g. a half-pizza
    still showing a "TurkeyBreast" recipe after the recipe tag was cleared).

    Safety: we delete tag rows only for item_guids present in *this* payload, so
    a partial/incomplete Toast menu response can never wipe good data for items
    it happened to omit. Items with zero tags after removal are included in
    ``seen_item_guids`` (so their orphaned rows are cleared) even though they
    contribute no rows to ``mappings``. All in one transaction.

    Returns the number of mapping rows inserted.
    """
    initialize_db(db_path)

    # Deduplicate + drop empties; nothing to scope the delete to → no-op.
    guids = sorted({g for g in (seen_item_guids or []) if g})
    if not guids:
        return upsert_item_tags(mappings, store_guid, db_path)

    insert_rows = [
        (
            store_guid,
            m.get("item_guid", ""),
            m.get("tag_guid", ""),
            m.get("item_plu", 0),
            m.get("multi_location_id", ""),
        )
        for m in (mappings or [])
    ]

    with get_connection(db_path) as conn:
        # Purge existing assignments for exactly the items in this payload.
        # Chunk the IN-list to stay under SQLite's variable limit.
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM cfg_map_item_tags "
                f"WHERE store_guid = ? AND item_guid IN ({ph})",
                [store_guid, *chunk],
            )
        # Re-insert the current set. INSERT OR REPLACE guards against any
        # duplicate (mli, tag_guid) within the same payload.
        if insert_rows:
            conn.executemany(
                """INSERT OR REPLACE INTO cfg_map_item_tags (
                    store_guid, item_guid, tag_guid, item_plu, multi_location_id
                ) VALUES (?,?,?,?,?)""",
                insert_rows,
            )
    return len(insert_rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes — cfg_sales_cats
# ──────────────────────────────────────────────────────────────────────────────

def upsert_sales_cats(
    cats: List[dict],
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """Upsert sales categories for a store."""
    if not cats:
        return 0
    initialize_db(db_path)
    rows = []
    for c in cats:
        rows.append((
            store_guid,
            c.get("category_guid", ""),
            c.get("category_name", ""),
            c.get("master_id", ""),
            c.get("multi_location_id", ""),
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO cfg_sales_cats (
                store_guid, category_guid, category_name,
                master_id, multi_location_id
            ) VALUES (?,?,?,?,?)
            ON CONFLICT(store_guid, category_guid)
            DO UPDATE SET
                category_name=excluded.category_name,
                master_id=excluded.master_id,
                multi_location_id=excluded.multi_location_id
            """,
            rows,
        )
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes — cfg_map_plu_toast_to_aloha
# ──────────────────────────────────────────────────────────────────────────────

def upsert_plu_crossref(
    rows_in: List[dict],
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """Upsert Toast PLU → Aloha PLU cross-reference."""
    if not rows_in:
        return 0
    initialize_db(db_path)
    rows = []
    for r in rows_in:
        rows.append((
            r.get("multi_location_id", ""),
            r.get("toast_plu", 0),
            r.get("aloha_plu", 0),
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO cfg_map_plu_toast_to_aloha (
                multi_location_id, toast_plu, aloha_plu
            ) VALUES (?,?,?)
            ON CONFLICT(multi_location_id)
            DO UPDATE SET
                toast_plu=excluded.toast_plu,
                aloha_plu=excluded.aloha_plu
            """,
            rows,
        )
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes — mli_plu_attribute_map
# ──────────────────────────────────────────────────────────────────────────────

def upsert_attribute_map(
    rows_in: List[dict],
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """Upsert rows into the master attribute map."""
    if not rows_in:
        return 0
    initialize_db(db_path)
    rows = []
    for r in rows_in:
        rows.append((
            r.get("multi_location_id", ""),
            r.get("item_guid", ""),
            r.get("toast_plu", 0),
            r.get("aloha_plu", 0),
            r.get("toast_item_name", ""),
            r.get("aloha_item_name", ""),
            r.get("consolidated_cat", ""),
            r.get("recipe", ""),
            r.get("guest_count_tag", 0),
            r.get("entree_equiv_tag", 0),
            r.get("lto_tag", ""),
        ))

    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO mli_plu_attribute_map (
                multi_location_id, item_guid, toast_plu, aloha_plu,
                toast_item_name, aloha_item_name, consolidated_cat,
                recipe, guest_count_tag, entree_equiv_tag, lto_tag
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(multi_location_id, item_guid)
            DO UPDATE SET
                toast_plu=excluded.toast_plu,
                aloha_plu=excluded.aloha_plu,
                toast_item_name=excluded.toast_item_name,
                aloha_item_name=excluded.aloha_item_name,
                consolidated_cat=excluded.consolidated_cat,
                recipe=excluded.recipe,
                guest_count_tag=excluded.guest_count_tag,
                entree_equiv_tag=excluded.entree_equiv_tag,
                lto_tag=excluded.lto_tag
            """,
            rows,
        )
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────────

def get_consolidated_cat_map(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, int]:
    """Return {tag_name: consol_id} from the consolidated_category_ids table."""
    if not os.path.exists(db_path):
        return dict(CONSOLIDATED_CATEGORY_IDS)
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT tag_name, consol_id FROM consolidated_category_ids"
            ).fetchall()
        return {r[0]: r[1] for r in rows} if rows else dict(CONSOLIDATED_CATEGORY_IDS)
    except Exception:
        return dict(CONSOLIDATED_CATEGORY_IDS)


def get_attribute_map(
    multi_location_ids: Optional[List[str]] = None,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> pd.DataFrame:
    """
    Return the master attribute map as a DataFrame.
    Optionally filter to specific multi_location_ids.
    """
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        params: List = []
        sql = "SELECT * FROM mli_plu_attribute_map"
        if multi_location_ids:
            ph = ",".join("?" * len(multi_location_ids))
            sql += f" WHERE multi_location_id IN ({ph})"
            params.extend(multi_location_ids)
        with get_connection(db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except Exception as exc:
        logger.warning(f"Failed to read attribute map: {exc}")
        return pd.DataFrame()


def get_menu_items_by_store(
    store_guid: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> List[dict]:
    """Return all menu items for a store as a list of dicts."""
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM cfg_menu_items WHERE store_guid = ?",
                (store_guid,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_all_menu_items(db_path: str = TOAST_CONFIG_DB_PATH) -> pd.DataFrame:
    """Return all cfg_menu_items rows as a DataFrame (deduplicated by MLI)."""
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        sql = """
            SELECT DISTINCT multi_location_id, item_guid, item_name,
                   item_plu, item_tags, sales_cat_guid, guest_count
            FROM cfg_menu_items
            WHERE multi_location_id != ''
        """
        with get_connection(db_path) as conn:
            return pd.read_sql_query(sql, conn)
    except Exception as exc:
        logger.warning(f"Failed to read menu items: {exc}")
        return pd.DataFrame()


def get_stats(db_path: str = TOAST_CONFIG_DB_PATH) -> dict:
    """Return summary statistics about the database contents."""
    empty = {
        "menu_items": 0,
        "tags": 0,
        "item_tags": 0,
        "sales_cats": 0,
        "plu_crossref": 0,
        "attribute_map": 0,
        "stores": 0,
        "db_size_mb": 0.0,
    }
    if not os.path.exists(db_path):
        return empty
    try:
        with get_connection(db_path) as conn:
            counts = {}
            for table in ["cfg_menu_items", "cfg_tags", "cfg_map_item_tags",
                          "cfg_sales_cats", "cfg_map_plu_toast_to_aloha",
                          "mli_plu_attribute_map"]:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = row[0] if row else 0
            stores = conn.execute(
                "SELECT COUNT(DISTINCT store_guid) FROM cfg_menu_items"
            ).fetchone()
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        return {
            "menu_items": counts["cfg_menu_items"],
            "tags": counts["cfg_tags"],
            "item_tags": counts["cfg_map_item_tags"],
            "sales_cats": counts["cfg_sales_cats"],
            "plu_crossref": counts["cfg_map_plu_toast_to_aloha"],
            "attribute_map": counts["mli_plu_attribute_map"],
            "stores": stores[0] if stores else 0,
            "db_size_mb": round(size_mb, 2),
        }
    except Exception:
        return empty


# ──────────────────────────────────────────────────────────────────────────────
# Mapping Table helpers (replaces Excel read/write)
# ──────────────────────────────────────────────────────────────────────────────

_MAPPING_COLUMNS = [
    "multi_location_id", "item_guid", "toast_plu", "aloha_plu",
    "toast_item_name", "aloha_item_name", "consolidated_cat",
    "recipe", "guest_count_tag", "entree_equiv_tag", "lto_tag",
]


def get_mapping_table_rows(db_path: str = TOAST_CONFIG_DB_PATH) -> List[dict]:
    """
    Return all mli_plu_attribute_map rows as list of dicts.
    Drop-in replacement for _load_mapping_table_excel().
    """
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM mli_plu_attribute_map ORDER BY toast_item_name"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"Failed to read mapping table: {exc}")
        return []


def update_mapping_table_rows(
    rows: List[dict],
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """
    Replace all rows in mli_plu_attribute_map with the provided list.
    Used by PUT /api/mapping-table to save user edits from the UI.
    Returns the number of rows written.
    """
    if not rows:
        return 0
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM mli_plu_attribute_map")
        insert_rows = []
        for r in rows:
            insert_rows.append((
                r.get("multi_location_id", ""),
                r.get("item_guid", ""),
                r.get("toast_plu", 0),
                r.get("aloha_plu", 0),
                r.get("toast_item_name", ""),
                r.get("aloha_item_name", ""),
                r.get("consolidated_cat", ""),
                r.get("recipe", ""),
                r.get("guest_count_tag", 0),
                r.get("entree_equiv_tag", 0),
                r.get("lto_tag", ""),
            ))
        conn.executemany(
            """INSERT OR REPLACE INTO mli_plu_attribute_map (
                multi_location_id, item_guid, toast_plu, aloha_plu,
                toast_item_name, aloha_item_name, consolidated_cat,
                recipe, guest_count_tag, entree_equiv_tag, lto_tag
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            insert_rows,
        )
    return len(insert_rows)


# ──────────────────────────────────────────────────────────────────────────────
# Writes/reads — aloha_items_master (canonical Aloha item list for UI dropdown)
# ──────────────────────────────────────────────────────────────────────────────

def get_aloha_items(
    consol_cat: Optional[str] = None,
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> List[dict]:
    """
    Return the canonical Aloha items list. Optionally filter by consol_cat
    (e.g. "* Consolidated NA Bev") to scope the dropdown to a single
    consolidated category in the Mapping Table UI.

    Returns rows ordered by aloha_item_name for stable display.
    """
    if not os.path.exists(db_path):
        return []
    try:
        with get_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if consol_cat:
                rows = conn.execute(
                    "SELECT aloha_plu, aloha_item_name, consol_id, consol_cat, item_range "
                    "  FROM aloha_items_master WHERE consol_cat = ? "
                    " ORDER BY aloha_item_name",
                    (consol_cat,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT aloha_plu, aloha_item_name, consol_id, consol_cat, item_range "
                    "  FROM aloha_items_master "
                    " ORDER BY aloha_item_name"
                ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def replace_aloha_items_master(
    rows_in: List[dict],
    db_path: str = TOAST_CONFIG_DB_PATH,
) -> int:
    """
    Replace the full aloha_items_master table from a freshly-parsed xlsx.
    Used by backend/import_aloha_items.py as a full snapshot reload.
    """
    initialize_db(db_path)
    cleaned = []
    for r in rows_in:
        plu = r.get("aloha_plu")
        name = r.get("aloha_item_name", "")
        if plu is None or not name:
            continue
        try:
            plu_int = int(plu)
        except (ValueError, TypeError):
            continue
        cleaned.append((
            plu_int,
            str(name).strip(),
            int(r.get("consol_id") or 0) or None,
            (r.get("consol_cat") or "").strip(),
            (r.get("item_range") or "").strip(),
        ))
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM aloha_items_master")
        if cleaned:
            conn.executemany(
                """INSERT INTO aloha_items_master (
                    aloha_plu, aloha_item_name, consol_id, consol_cat, item_range
                ) VALUES (?,?,?,?,?)""",
                cleaned,
            )
    return len(cleaned)


def get_toast_items_with_tags(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, dict]:
    """
    Build the same {normalized_name: item_dict} structure that _poll_all_toast_items()
    produces from the live Toast API, but using data already in toast_config.db.

    Each item_dict has keys: name, guid, mli, plu, tags (list of tag names).
    Deduplicates by multi_location_id, picking the first occurrence.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        with get_connection(db_path) as conn:
            # Get all distinct menu items (one per MLI)
            items = conn.execute("""
                SELECT DISTINCT
                    m.multi_location_id,
                    m.item_guid,
                    m.item_name,
                    m.item_plu
                FROM cfg_menu_items m
                WHERE m.multi_location_id != ''
                  AND m.item_name != ''
            """).fetchall()

            # Get all item→tag mappings joined with tag names
            tag_rows = conn.execute("""
                SELECT
                    mit.multi_location_id,
                    t.tag_name
                FROM cfg_map_item_tags mit
                JOIN cfg_tags t ON mit.store_guid = t.store_guid
                                AND mit.tag_guid = t.tag_guid
                WHERE mit.multi_location_id != ''
                  AND t.tag_name != ''
            """).fetchall()

        # Build MLI → set of tag names
        mli_tags: Dict[str, set] = {}
        for mli, tag_name in tag_rows:
            mli_tags.setdefault(mli, set()).add(tag_name)

        # Build normalized lookup
        result: Dict[str, dict] = {}
        seen_mli: set = set()
        for mli, guid, name, plu in items:
            if mli in seen_mli:
                continue
            seen_mli.add(mli)
            norm = name.strip().lower()
            if not norm:
                continue
            tags = sorted(mli_tags.get(mli, set()))
            result[norm] = {
                "name": name.strip(),
                "guid": guid or "",
                "mli": mli or "",
                "plu": str(plu) if plu else "",
                "tags": tags,
            }
        return result
    except Exception as exc:
        logger.warning(f"Failed to build toast items with tags: {exc}")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Per-item attribute lookup (consolidated category + EE tag)
# ──────────────────────────────────────────────────────────────────────────────

def get_item_attribute_lookup(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, Dict[str, object]]:
    """
    Return {item_guid: {"consolidated_cat": str, "entree_equiv_tag": float,
                        "recipe": str, "guest_count_tag": float,
                        "toast_plu": int, "aloha_plu": int}}
    AND a parallel {normalized_toast_item_name: {...}} fallback, merged into one dict
    keyed by both the guid and the normalized name. The menu performance Excel export
    uses this to stamp each row with its consolidated category, EE tag,
    Guest Count tag, Toast PLU, and Aloha PLU.

    Lookup priority for a given row: try guid first, then normalized name.
    Returns empty dict if DB unavailable.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT item_guid, toast_item_name, consolidated_cat,
                          entree_equiv_tag, recipe, guest_count_tag,
                          toast_plu, aloha_plu
                   FROM mli_plu_attribute_map"""
            ).fetchall()
        lookup: Dict[str, Dict[str, object]] = {}
        for guid, tname, cat, ee, recipe, gct, tplu, aplu in rows:
            entry = {
                "consolidated_cat": cat or "",
                "entree_equiv_tag": float(ee) if ee is not None else 0.0,
                "recipe": recipe or "",
                "guest_count_tag": float(gct) if gct is not None else 0.0,
                "toast_plu": int(tplu) if tplu is not None else 0,
                "aloha_plu": int(aplu) if aplu is not None else 0,
            }
            if guid:
                # Skip the synthetic "x:" placeholder guids written by
                # sync_mapping_excel_to_db.py — those won't match any real Toast guid.
                if not str(guid).startswith("x:"):
                    lookup[guid] = entry
            if tname:
                norm = " ".join(str(tname).strip().lower().split())
                if norm and norm not in lookup:
                    lookup[norm] = entry
        return lookup
    except Exception as exc:
        logger.warning(f"Failed to build item attribute lookup: {exc}")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Toast-to-Aloha lookup maps (replaces SalesConsolidator Excel dependency)
# ──────────────────────────────────────────────────────────────────────────────

def get_toast_to_aloha_maps(db_path: str = TOAST_CONFIG_DB_PATH) -> Optional[Dict[str, dict]]:
    """
    Build the same lookup maps that SalesConsolidator builds from Excel,
    but sourced from mli_plu_attribute_map in SQLite.

    Returns dict with keys:
      - toast_to_aloha_map:      {item_guid → aloha_item_name}
      - toast_name_to_aloha_map: {normalized_toast_name → aloha_item_name}
      - toast_category_map:      {item_guid → consolidated_cat}
      - aloha_consolidation_map: {aloha_item_name → {toast_guids, toast_names, category}}

    Returns None if DB not available or has no Aloha data.
    """
    if not os.path.exists(db_path):
        return None
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT multi_location_id, item_guid, toast_plu,
                          aloha_plu, toast_item_name, aloha_item_name,
                          consolidated_cat
                   FROM mli_plu_attribute_map
                   WHERE item_guid != ''"""
            ).fetchall()

        if not rows:
            return None

        toast_to_aloha: Dict[str, str] = {}
        toast_name_to_aloha: Dict[str, str] = {}
        toast_category: Dict[str, str] = {}
        aloha_consolidation: Dict[str, dict] = {}

        has_aloha = False
        for mli, guid, tplu, aplu, tname, aname, cat in rows:
            # Category map — always populated
            if guid:
                toast_category[guid] = cat or ""

            # Aloha maps — only if aloha_item_name is present
            if aname:
                has_aloha = True
                toast_to_aloha[guid] = aname

                if tname:
                    norm = " ".join(str(tname).strip().lower().split())
                    if norm:
                        toast_name_to_aloha[norm] = aname

                if aname not in aloha_consolidation:
                    aloha_consolidation[aname] = {
                        "toast_guids": [],
                        "toast_names": [],
                        "category": cat or "",
                    }
                aloha_consolidation[aname]["toast_guids"].append(guid)
                if tname:
                    aloha_consolidation[aname]["toast_names"].append(tname)

        if not has_aloha:
            return None

        return {
            "toast_to_aloha_map": toast_to_aloha,
            "toast_name_to_aloha_map": toast_name_to_aloha,
            "toast_category_map": toast_category,
            "aloha_consolidation_map": aloha_consolidation,
        }
    except Exception as exc:
        logger.warning(f"Failed to build Toast-to-Aloha maps: {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Live tag-derived item attributes (sourced from Toast Menus API tags)
# ──────────────────────────────────────────────────────────────────────────────
# Toast tags carry several signals that we surface as separate columns in the
# menu performance Excel. Pulled live from cfg_map_item_tags + cfg_tags so the
# columns reflect whatever's currently in Toastweb — no Excel re-sync needed.
#
# Tag taxonomy (audited against a real DB on 2026-05-04):
#   '*' prefix             → Consolidated Category   (e.g. "* Consolidated Soups")
#   'EE <number>'          → Entree Equiv Tag         (e.g. "EE 1", "EE .5")
#   'Guest Count <number>' → Guest Count Tag          (e.g. "Guest Count 0.5")
#   '<YYYY>LTO<n>'         → LTO marker, ignored      (e.g. "2025LTO4", "2026LTO1")
#   'Catering Menu'        → category, ignored
#   anything else          → Recipe Tag               (e.g. "ChickenSalad")

_EE_TAG_RE  = re.compile(r"^EE\s+(\.?\d+(?:\.\d+)?)$",         re.IGNORECASE)
_GC_TAG_RE  = re.compile(r"^Guest\s+Count\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)
_LTO_TAG_RE = re.compile(r"^\d{4}LTO\d*$",                     re.IGNORECASE)


def get_live_item_tag_attrs(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, Dict[str, object]]:
    """
    Return {item_guid: {"recipe": str, "consolidated_cat": str,
                        "entree_equiv_tag": float, "guest_count_tag": float}}
    sourced live from the Toast Menus API tag data. Multiple-tag handling:

      - recipe         → sorted comma-joined  ("LTO4, GrilledSteak")
      - consolidated_cat → sorted comma-joined ("* Consolidated Pizza, * Consolidated Salads")
      - EE / GC         → max numeric value found (multiples = data error)

    Returns {} when DB is unavailable.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        with get_connection(db_path) as conn:
            # DISTINCT is critical — cfg_map_item_tags has the same
            # (item_guid, tag_guid) row per store, so without dedup the join
            # returns tens of millions of rows on a real install.
            rows = conn.execute(
                """SELECT DISTINCT mit.item_guid, ct.tag_name
                   FROM cfg_map_item_tags mit
                   JOIN cfg_tags ct ON ct.tag_guid = mit.tag_guid
                   WHERE ct.tag_name != ''
                     AND ct.tag_name != 'Catering Menu'"""
            ).fetchall()
    except Exception as exc:
        logger.warning(f"Failed to load live item tag attrs: {exc}")
        return {}

    recipes: Dict[str, set] = {}
    consol:  Dict[str, set] = {}
    ee:      Dict[str, float] = {}
    gc:      Dict[str, float] = {}

    for guid, name in rows:
        if not guid or not name:
            continue
        s = name.strip()
        if s.startswith("*"):
            consol.setdefault(guid, set()).add(name)
            continue
        m = _EE_TAG_RE.match(s)
        if m:
            try:
                v = float(m.group(1))
                if v > ee.get(guid, 0.0):
                    ee[guid] = v
            except ValueError:
                pass
            continue
        m = _GC_TAG_RE.match(s)
        if m:
            try:
                v = float(m.group(1))
                if v > gc.get(guid, 0.0):
                    gc[guid] = v
            except ValueError:
                pass
            continue
        # Skip LTO-year markers — they're season/promo flags, not recipes.
        if _LTO_TAG_RE.match(s):
            continue
        # Anything left is a recipe tag.
        recipes.setdefault(guid, set()).add(name)

    out: Dict[str, Dict[str, object]] = {}
    all_guids = set(recipes) | set(consol) | set(ee) | set(gc)
    for g in all_guids:
        out[g] = {
            "recipe":           ", ".join(sorted(recipes.get(g, []))),
            "consolidated_cat": ", ".join(sorted(consol.get(g, []))),
            "entree_equiv_tag": ee.get(g, 0.0),
            "guest_count_tag":  gc.get(g, 0.0),
        }
    return out


def get_live_toast_plu_map(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, int]:
    """
    Return {item_guid: toast_plu} sourced live from cfg_menu_items (populated
    by the Toast Menus API sync). Items with item_plu == 0 or NULL are skipped.

    When the same item_guid appears across multiple stores, values may differ
    in theory but are identical in practice; we keep the max non-zero value.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT item_guid, MAX(item_plu)
                   FROM cfg_menu_items
                   WHERE item_guid != '' AND item_plu IS NOT NULL AND item_plu != 0
                   GROUP BY item_guid"""
            ).fetchall()
    except Exception as exc:
        logger.warning(f"Failed to load live toast PLU map: {exc}")
        return {}
    return {g: int(p) for g, p in rows if g and p}


# ──────────────────────────────────────────────────────────────────────────────
# PLU Range Classification (Aloha + Toast)
# ──────────────────────────────────────────────────────────────────────────────
# Sourced from "Item PLU Ranges.xlsx" (Newk's master ranges export). The Aloha
# ranges give us a broad-stroke "Item Classification" (e.g. "Soups", "Pizza"),
# the Toast ranges give a more granular "Item Subclass" (e.g. "Entrée Bowl Soup").

def import_plu_ranges_from_excel(
    xlsx_path: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
    replace: bool = True,
) -> int:
    """
    Read "Item PLU Ranges.xlsx" and load it into the plu_ranges table.

    Expected sheet layout (the export file's first sheet):
      Row 0: title  ("Item PLU Ranges")
      Row 1: "Report generated at: ..."
      Row 2: header  ("POS Type" | "Range Name" | "Range Start" | "Range End")
      Row 3+: data

    Returns the number of rows inserted. When replace=True (default), wipes
    the table first so re-imports stay in sync with the Excel source of truth.
    """
    initialize_db(db_path)
    df = pd.read_excel(xlsx_path, header=2)
    df = df.dropna(subset=["POS Type", "Range Name", "Range Start", "Range End"])

    rows = []
    for _, r in df.iterrows():
        try:
            rows.append((
                str(r["POS Type"]).strip(),
                str(r["Range Name"]).strip(),
                int(r["Range Start"]),
                int(r["Range End"]),
            ))
        except (ValueError, TypeError):
            continue  # skip malformed rows silently

    with get_connection(db_path) as conn:
        if replace:
            conn.execute("DELETE FROM plu_ranges")
        conn.executemany(
            """INSERT OR IGNORE INTO plu_ranges
               (pos_type, range_name, range_start, range_end)
               VALUES (?, ?, ?, ?)""",
            rows,
        )
    logger.info(f"Imported {len(rows)} PLU ranges from {xlsx_path}")
    return len(rows)


def get_plu_range_classifier(db_path: str = TOAST_CONFIG_DB_PATH):
    """
    Return a callable `classify(pos_type, plu) -> str` that resolves a PLU to
    its Range Name.  Loads all ranges into memory once for fast lookups during
    a report build (typical: ~350 rows, classifying thousands of items).

    Algorithm: ranges are stored sorted by start; lookup uses bisect to find
    the rightmost range whose start <= plu, then checks `plu <= end`. Returns
    "" when there's no match or DB is unavailable.
    """
    if not os.path.exists(db_path):
        return lambda pos_type, plu: ""
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT pos_type, range_start, range_end, range_name
                   FROM plu_ranges
                   ORDER BY pos_type, range_start"""
            ).fetchall()
    except Exception as exc:
        logger.warning(f"Failed to load plu_ranges: {exc}")
        return lambda pos_type, plu: ""

    # Bucket per pos_type as parallel sorted lists for bisect.
    import bisect
    starts: Dict[str, List[int]] = {}
    payload: Dict[str, List[tuple]] = {}  # (end, name)
    for pos_type, rstart, rend, rname in rows:
        starts.setdefault(pos_type, []).append(int(rstart))
        payload.setdefault(pos_type, []).append((int(rend), str(rname)))

    def classify(pos_type: str, plu) -> str:
        if not plu:
            return ""
        try:
            plu_int = int(plu)
        except (ValueError, TypeError):
            return ""
        s = starts.get(pos_type)
        p = payload.get(pos_type)
        if not s:
            return ""
        idx = bisect.bisect_right(s, plu_int) - 1
        if idx < 0:
            return ""
        rend, rname = p[idx]
        return rname if plu_int <= rend else ""

    return classify


# Toast PLU prefixes that "wrap" an underlying Aloha PLU. Stripping these and
# re-classifying against the Aloha ranges lets us assign an Aloha-style
# "Item Classification" to Toast items that have no explicit aloha_plu in
# mli_plu_attribute_map. The Aloha ranges file's own footer documents these:
#   300200-300210  Toast Parent Items - POS
#   400200-400210  Toast Parent Items - OLO
_TOAST_TO_ALOHA_PLU_PREFIXES = (300, 400, 600, 700)


def derive_aloha_plu_from_toast(toast_plu) -> int:
    """
    Best-effort: turn a Toast PLU into the underlying Aloha PLU by stripping
    a known leading prefix. Returns 0 if no prefix matches or input is empty.

    This is heuristic — Toast PLUs were originally minted by adding a 3-digit
    prefix to the matching Aloha PLU (300=POS, 400=OLO, etc.). When an item's
    explicit aloha_plu is missing from mli_plu_attribute_map, this gives us a
    reasonable fallback for classification.
    """
    if not toast_plu:
        return 0
    try:
        n = int(toast_plu)
    except (ValueError, TypeError):
        return 0
    s = str(n)
    for prefix in _TOAST_TO_ALOHA_PLU_PREFIXES:
        ps = str(prefix)
        if s.startswith(ps) and len(s) > len(ps):
            try:
                return int(s[len(ps):])
            except ValueError:
                continue
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Toast switchover dates (per-store Aloha → Toast cutover)
# ──────────────────────────────────────────────────────────────────────────────
# Stored in Toast Switchover Dates.xlsx (in-repo, two columns: Store GUID,
# Switchover Date). Populated into the toast_switchover_dates SQLite table by
# import_switchover_dates.py. Used by the menu performance YoY pipeline to
# decide whether each store's Last Year data should come from Aloha
# (pre-switchover) or Toast (post-switchover).

def import_switchover_dates_from_excel(
    xlsx_path: str,
    db_path: str = TOAST_CONFIG_DB_PATH,
    replace: bool = True,
) -> int:
    """
    Read 'Toast Switchover Dates.xlsx' and load into toast_switchover_dates.

    Expected sheet layout:
      Row 0: header  (Store GUID | Switchover Date)
      Row 1+: data

    Switchover Date can be a Python date, datetime, or ISO yyyy-mm-dd string.
    Empty rows are skipped. Returns number of rows inserted.
    """
    initialize_db(db_path)
    df = pd.read_excel(xlsx_path)
    df = df.dropna(subset=["Store GUID", "Switchover Date"])

    rows = []
    for _, r in df.iterrows():
        guid = str(r["Store GUID"]).strip()
        raw  = r["Switchover Date"]
        # Coerce to ISO yyyy-mm-dd
        try:
            if hasattr(raw, "strftime"):
                iso = raw.strftime("%Y-%m-%d")
            else:
                iso = str(raw).strip()[:10]
            # Validate
            import datetime as _dt
            _dt.date.fromisoformat(iso)
        except Exception:
            logger.warning(f"Skipping switchover row with bad date: {guid} {raw!r}")
            continue
        if guid:
            rows.append((guid, iso))

    with get_connection(db_path) as conn:
        if replace:
            conn.execute("DELETE FROM toast_switchover_dates")
        conn.executemany(
            """INSERT OR REPLACE INTO toast_switchover_dates
               (store_guid, switchover_date) VALUES (?, ?)""",
            rows,
        )
    logger.info(f"Imported {len(rows)} switchover dates from {xlsx_path}")
    return len(rows)


def get_switchover_date_map(db_path: str = TOAST_CONFIG_DB_PATH) -> Dict[str, str]:
    """
    Return {store_guid: 'YYYY-MM-DD'} of every populated switchover date.
    Empty dict when DB unavailable or table empty — both equivalent to "no
    store has cut over yet" for the LY pipeline's purposes.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        with get_connection(db_path) as conn:
            return {g: d for g, d in conn.execute(
                "SELECT store_guid, switchover_date FROM toast_switchover_dates"
            )}
    except Exception as exc:
        logger.warning(f"Failed to load switchover dates: {exc}")
        return {}
