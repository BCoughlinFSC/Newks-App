from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Dict, Tuple
import time
import sys
import json
import uuid
import hashlib
import csv
import io
import re
import copy

import asyncio
import os

import httpx
from httpx import HTTPStatusError
from fastapi import FastAPI, HTTPException, Query, Body, Depends, status, APIRouter, Response, UploadFile, File, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from jose import JWTError, jwt
import bcrypt
import pandas as pd

load_dotenv()

from pick3_reports import router as pick3_router, Pick3ReportRequest, Pick3DrillDownOrdersRequest, Pick3DrillDownAllStoresRequest
import pick_export_jobs  # also used as backing store for /api/reports/run/start async jobs
import ipa_export

# Allow importing root-level modules (sales_consolidator, hierarchical_reports)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from sales_consolidator import SalesConsolidator
from hierarchical_reports import HierarchicalReportGenerator
from aloha_sql_importer import import_aloha_sql_range, import_aloha_sql_last_days
import aloha_db
import aloha_sales_db
from aloha_sales_importer import import_aloha_sales_range, import_aloha_sales_last_days, import_fiscal_calendar
import toast_orders_blob as toast_orders_db
import toast_config_db
import toast_config_builder
import monkey_db
import crunchtime_db
import punchh_db
from olo_client import (
    list_order_batches,
    download_batch_zip,
    extract_orders_from_zip,
    aggregate_digital_orders_by_platform,
    aggregate_by_handoff_mode,
    aggregate_orders_placed_ahead,
    filter_batches_by_date_range,
)
from email_service import send_invite_email, send_password_reset_email
import toast_puller_db as repoll_db
import precompute_cache
import toast_menu_raw_db
from toast_puller import fetch_orders_with_retry, parse_orders

# ------------------
# Config
# ------------------

TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")
TOAST_API_TOKEN = os.getenv("TOAST_API_TOKEN")

TOAST_STANDARD_API_CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
TOAST_STANDARD_API_CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")
TOAST_STANDARD_API_BASE_URL = os.getenv("TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com")
TOAST_ORDERS_API_BASE_URL = os.getenv("TOAST_ORDERS_API_BASE_URL", TOAST_STANDARD_API_BASE_URL)
TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID = os.getenv("TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID")
TOAST_MENU_TAGS_PRIORITY_NAME = os.getenv("TOAST_MENU_TAGS_PRIORITY_NAME")

# Slack webhook configuration
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")  # Slack incoming webhook URL

# YoY storage configuration
YOY_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "aloha")
YOY_INDEX_FILE = os.path.join(YOY_DATA_DIR, "index.json")
ALOHA_LOCATION_MAPPING_FILE = os.path.join(YOY_DATA_DIR, "aloha_location_mapping.json")
ALOHA_ORDER_MODE_MAPPING_FILE = os.path.join(YOY_DATA_DIR, "aloha_order_mode_mapping.json")
TOAST_CHANNELS_FILE = os.path.join(YOY_DATA_DIR, "toast_channels.json")

_DEFAULT_TOAST_CHANNELS = ["Dine In", "Take Out", "Delivery", "OLO / Online", "Catering", "Drive Thru", "Other"]


def get_toast_channels() -> list:
    """Load Toast channel list from file, falling back to defaults."""
    if os.path.exists(TOAST_CHANNELS_FILE):
        try:
            with open(TOAST_CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(c).strip() for c in data if str(c).strip()]
        except Exception:
            pass
    return list(_DEFAULT_TOAST_CHANNELS)

MAPPING_FILE = os.path.join(ROOT_DIR, "Updated Mapping Table.xlsx")
FALLBACK_MAPPING_FILE = os.path.join(ROOT_DIR, "Fallback Updated Mapping.xlsx")

_consolidated_cat_cache: Dict[str, Dict[str, str]] = {}


def _normalize_item_name(value: str) -> str:
    return " ".join(str(value or "").lower().replace("&", "and").split())


def _mapping_lookup_keys(normalized_name: str) -> List[str]:
    """
    Return candidate keys for looking up consolidated/report_tag map.
    Tries full normalized name first, then base name with trailing " (xxx)" stripped.
    Also adds variant with " and " collapsed to "and" so mapping "Pepperoni&Mushroom..."
    (stored as pepperoniandmushroom...) matches Toast "Pepperoni & Mushroom..." (and vice versa).
    """
    if not normalized_name or not normalized_name.strip():
        return []
    keys = [normalized_name]
    # So Excel "Pepperoni&Mushroom Pizza" and Toast "Pepperoni & Mushroom Pizza" both match
    alt = normalized_name.replace(" and ", "and")
    if alt != normalized_name and alt not in keys:
        keys.append(alt)
    match = re.match(r"^(.+?)\s+\([^)]+\)\s*$", normalized_name)
    if match:
        base = match.group(1).strip()
        if base and base not in keys:
            keys.append(base)
        if base:
            base_alt = base.replace(" and ", "and")
            if base_alt not in keys:
                keys.append(base_alt)
    return keys


def _get_tag_from_mapping(
    normalized_name: str,
    report_tag_map: Dict[str, List[str]],
    consolidated_cat_map: Dict[str, str],
    consolidated_override: Dict[str, str],
    guid: str,
) -> List[str]:
    """Return all tags for an item. Tries override by GUID, then report_tag_map (list),
    then consolidated_cat_map (single value wrapped in list)."""
    tag = consolidated_override.get(guid)
    if tag:
        return [tag]
    for key in _mapping_lookup_keys(normalized_name):
        tags = report_tag_map.get(key)
        if tags:
            return tags
        cat = consolidated_cat_map.get(key)
        if cat:
            return [cat]
    return []


def _normalize_tag_name(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("*"):
        text = text.lstrip("*").strip()
    return text



def _is_olo_item(item_name: str) -> bool:
    """Returns True when the Toast item name contains 'OLO' (case-insensitive),
    indicating the item was sold through the online-ordering channel."""
    return "olo" in (item_name or "").lower()


def _humanize_item_name(normalized_name: str) -> str:
    """Turn normalized name (e.g. 'pepperoni and portabella pizza (entree)') into display form."""
    if not normalized_name:
        return normalized_name
    s = normalized_name.replace(" and ", " & ").strip()
    return s.title() if s else s


def get_consolidated_category_map() -> Dict[str, str]:
    """Toast_Item_Name (normalized) -> ConsolidatedCat. Uses cache."""
    _ensure_consolidated_maps()
    return _consolidated_cat_cache.get("data", {}).get("map", {})


def get_report_tag_map() -> Dict[str, List[str]]:
    """
    Toast_Item_Name (normalized) -> list of tags for report filtering.
    Parsed from the comma-separated 'Tags' column (synced from Toast item tags).
    Falls back to ConsolidatedCat if no Tags value.
    """
    _ensure_consolidated_maps()
    return _consolidated_cat_cache.get("data", {}).get("report_tag_map", {})


def get_consolidated_guid_map() -> Dict[str, str]:
    """
    Toast menu item/modifier GUID -> tag (for report filtering).
    Built from optional column 'item_guid', 'Toast_GUID', or 'GUID' in the mapping table.
    When present, lookup by GUID is tried first so items count even if name doesn't match.
    """
    _ensure_consolidated_maps()
    return _consolidated_cat_cache.get("data", {}).get("guid_to_tag", {})


def _ensure_consolidated_maps() -> None:
    """Load mapping file once and populate consolidated map + report_tag_map in cache.

    Tries toast_config.db (SQLite) first for the attribute map, then falls back
    to the Excel mapping files.

    The Tags column (comma-separated, synced from Toast item tags) is parsed into
    individual tags for report_tag_map.  ConsolidatedCat (also synced from Toast)
    feeds the consolidated_cat_map.
    """
    cache = _consolidated_cat_cache.get("data")

    # ── Try SQLite attribute map first ──
    if toast_config_db.db_available():
        # Freshness key: SQLite file mtime, not row count. Editing a cell in
        # the Mapping Table UI updates SQLite values without changing the
        # row count, so a row-count-based key would silently serve stale
        # data on any uvicorn worker that already populated the cache
        # before the save (the save handler only clears the cache on the
        # one worker that handled the PUT). mtime bumps on every write,
        # so all workers refresh automatically.
        sqlite_key = "__sqlite_mtime__"
        try:
            db_mtime = os.path.getmtime(toast_config_db.TOAST_CONFIG_DB_PATH)
        except OSError:
            db_mtime = 0.0
        if cache and cache.get("mtimes") == {sqlite_key: db_mtime}:
            return
        attr_df = toast_config_db.get_attribute_map()
        if not attr_df.empty:
            mapping: Dict[str, str] = {}
            report_tag_map: Dict[str, List[str]] = {}
            for _, row in attr_df.iterrows():
                name = _normalize_item_name(row.get("toast_item_name", ""))
                if not name:
                    continue
                consol = str(row.get("consolidated_cat", "")).strip()
                recipe = str(row.get("recipe", "")).strip()
                tag_list = []
                if consol:
                    tag_list.append(consol)
                if recipe:
                    tag_list.append(recipe)
                for key in _mapping_lookup_keys(name):
                    is_base_key = key != name
                    if is_base_key and "(" in name:
                        continue
                    if key not in mapping and consol:
                        mapping[key] = consol
                    if tag_list and key not in report_tag_map:
                        report_tag_map[key] = tag_list
            _consolidated_cat_cache["data"] = {
                "mtimes": {sqlite_key: db_mtime},
                "map": mapping,
                "report_tag_map": report_tag_map,
            }
            logger.info(f"Consolidated maps loaded from toast_config.db: {len(mapping)} items")
            return

    # ── Fall back to Excel mapping files ──
    mapping_files = [MAPPING_FILE, FALLBACK_MAPPING_FILE]
    missing = [path for path in mapping_files if not os.path.exists(path)]
    if len(missing) == len(mapping_files):
        logger.warning(f"Mapping files missing for consolidated categories: {missing}")
        return

    mtimes = {path: os.path.getmtime(path) for path in mapping_files if os.path.exists(path)}
    if cache and cache.get("mtimes") == mtimes:
        return

    name_col = "Toast_Item_Name"
    cat_col = "ConsolidatedCat"
    tags_col = "Tags"
    mapping: Dict[str, str] = {}
    report_tag_map: Dict[str, List[str]] = {}

    for path in mapping_files:
        if not os.path.exists(path):
            logger.warning(f"Mapping file missing for consolidated categories: {path}")
            continue
        try:
            df = pd.read_excel(path)
        except Exception as exc:
            logger.warning(f"Failed to read mapping file for consolidated categories: {path} ({exc})")
            continue

        if name_col not in df.columns:
            logger.warning(f"Mapping file missing required column: {path} ({name_col})")
            continue

        has_cat = cat_col in df.columns
        has_tags = tags_col in df.columns

        # Backwards compat: if Tags column doesn't exist yet, try legacy columns
        legacy_tag_col = None
        if not has_tags:
            for c in ["Tag", "ReportTag", "GuestCountTag"]:
                if c in df.columns:
                    legacy_tag_col = c
                    break

        for _, row in df.iterrows():
            name = _normalize_item_name(row.get(name_col))
            if not name:
                continue
            cat = str(row.get(cat_col)).strip() if has_cat and pd.notna(row.get(cat_col)) else ""

            # Parse individual tags from comma-separated Tags column
            tag_list: List[str] = []
            if has_tags and pd.notna(row.get(tags_col)):
                tag_list = [t.strip() for t in str(row.get(tags_col)).split(",") if t.strip()]
            elif legacy_tag_col and pd.notna(row.get(legacy_tag_col)):
                # Fallback to legacy single-tag column if Tags not available yet
                tag_list = [str(row.get(legacy_tag_col)).strip()]

            if not tag_list and cat:
                tag_list = [cat]

            # Only store base key (e.g. "pepperoni and portabella pizza") when the row has NO parenthetical,
            # so we don't apply "(Entree)" mapping to "(OLO Pairing Choice)" or other variants.
            name_has_paren = "(" in name
            for key in _mapping_lookup_keys(name):
                is_base_key = key != name
                if is_base_key and name_has_paren:
                    continue
                if key not in mapping and cat:
                    mapping[key] = cat
                if tag_list and key not in report_tag_map:
                    report_tag_map[key] = tag_list

    _consolidated_cat_cache["data"] = {"mtimes": mtimes, "map": mapping, "report_tag_map": report_tag_map}


def get_consolidated_override_map() -> Dict[str, str]:
    return {
        "81c4867f-b73c-41e3-abe8-a89f9e180919": "Consolidated Pizza",
        "ac99345a-f79d-4083-bb01-f56f0e44cf64": "Consolidated Pizza"
    }


async def _fetch_menu_rows_for_range(
    client: httpx.AsyncClient,
    restaurant_ids: List[str],
    start_date: date,
    end_date: date,
    group_by: List[str]
) -> List[Dict]:
    report_guid = await request_toast_report(
        client,
        start_date,
        end_date,
        restaurant_ids,
        "menu",
        group_by
    )
    return await fetch_toast_report(client, report_guid, "menu")


def _aggregate_missing_consolidated_rows(
    rows: List[Dict],
    source_type: str,
    consolidated_map: Dict[str, str]
) -> Dict[str, Dict]:
    aggregated: Dict[str, Dict] = {}
    for row in rows:
        guid = row.get("menuItemGuid") or ""
        name = row.get("menuItemName") or ""
        if not guid or not name:
            continue
        norm_name = _normalize_item_name(name)
        if norm_name and consolidated_map.get(norm_name):
            continue
        key = f"{guid}|{name}"
        if key not in aggregated:
            aggregated[key] = {
                "menuItemGuid": guid,
                "menuItemName": name,
                "totalQty": 0,
                "sourceType": source_type
            }
        aggregated[key]["totalQty"] += row.get("quantitySold", 0) or 0
    return aggregated


def _aggregate_all_rows(
    rows: List[Dict],
    source_type: str,
    consolidated_map: Dict[str, str]
) -> Dict[str, Dict]:
    aggregated: Dict[str, Dict] = {}
    for row in rows:
        guid = row.get("menuItemGuid") or ""
        name = row.get("menuItemName") or ""
        if not guid or not name:
            continue
        norm_name = _normalize_item_name(name)
        consolidated_cat = consolidated_map.get(norm_name)
        key = f"{guid}|{name}"
        if key not in aggregated:
            aggregated[key] = {
                "menuItemGuid": guid,
                "menuItemName": name,
                "normalizedName": norm_name,
                "consolidatedCat": consolidated_cat or "",
                "totalQty": 0,
                "sourceType": source_type
            }
        aggregated[key]["totalQty"] += row.get("quantitySold", 0) or 0
    return aggregated


def _safe_write_excel(df: pd.DataFrame, target_path: str) -> str:
    try:
        df.to_excel(target_path, index=False)
        return target_path
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(target_path)
        fallback_path = f"{base}_{timestamp}{ext}"
        df.to_excel(fallback_path, index=False)
        return fallback_path

def ensure_yoy_storage():
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    if not os.path.exists(YOY_INDEX_FILE):
        with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"files": {}}, f)

def load_yoy_index() -> Dict:
    ensure_yoy_storage()
    with open(YOY_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_yoy_index(data: Dict) -> None:
    ensure_yoy_storage()
    with open(YOY_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_yoy_consolidator() -> SalesConsolidator:
    # Prefer toast_config.db — no Excel dependency
    if toast_config_db.db_available():
        db_maps = toast_config_db.get_toast_to_aloha_maps()
        if db_maps:
            logger.info("YoY consolidator: using toast_config.db (%d Toast→Aloha mappings)",
                        len(db_maps["toast_to_aloha_map"]))
            return SalesConsolidator.from_db(db_maps)
    # Fallback to Excel
    if not os.path.exists(MAPPING_FILE):
        raise HTTPException(status_code=500, detail=f"Missing mapping file: {MAPPING_FILE}")
    return SalesConsolidator(MAPPING_FILE)

def get_aloha_location_mapping() -> Dict[str, str]:
    """
    Load Toast restaurant GUID -> Aloha Location ID mapping from
    backend/data/aloha/aloha_location_mapping.json (optional).
    Format: {"toast_restaurant_guid": "aloha_location_id", ...}
    """
    if not os.path.exists(ALOHA_LOCATION_MAPPING_FILE):
        return {}
    try:
        with open(ALOHA_LOCATION_MAPPING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Could not load Aloha location mapping: {e}")
        return {}


def get_aloha_order_mode_mapping() -> Dict[str, str]:
    """
    Load Aloha OrderMode -> Toast channel mapping from
    backend/data/aloha/aloha_order_mode_mapping.json (optional).
    Format: {"Dine In": "Dine In", "Delivery": "Delivery", ...}
    Returns empty dict if file not found.
    """
    if not os.path.exists(ALOHA_ORDER_MODE_MAPPING_FILE):
        return {}
    try:
        with open(ALOHA_ORDER_MODE_MAPPING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Could not load Aloha order mode mapping: {e}")
        return {}


def get_aloha_file_path(business_date: str) -> str:
    index = load_yoy_index()
    file_entry = index.get("files", {}).get(business_date)
    if not file_entry:
        raise HTTPException(status_code=404, detail=f"No Aloha data found for {business_date}")
    file_path = file_entry.get("path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Aloha file missing for {business_date}")
    return file_path

def get_aloha_file_path_if_exists(business_date: str) -> Optional[str]:
    """Return path to Aloha file for the given date if it exists, else None."""
    index = load_yoy_index()
    file_entry = index.get("files", {}).get(business_date)
    if not file_entry:
        return None
    file_path = file_entry.get("path")
    if not file_path or not os.path.exists(file_path):
        return None
    return file_path


def apply_yoy_aloha_sales(
    result: dict,
    report_type: str,
    raw_rows: list,
    report_config: "ReportConfig",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    """
    Enrich a sales_summary or metrics report with prior-year Aloha sales data.

    Parameters
    ----------
    result : dict
        The report result dict (from compute_sales_summary_by_location or
        pivot_metrics_by_location_and_date).
    report_type : str
        "sales_summary" or "metrics".
    raw_rows : list
        The raw ERA analytics rows (contain restaurantGuid for location mapping).
    report_config : ReportConfig
        Current report configuration (checked for yoyEnabled).
    start_date, end_date : date
        The current report date range (Aloha range = same dates, 1 year prior).
    """
    if not result or not report_config.yoyEnabled:
        return result
    if report_type not in ("sales_summary", "metrics"):
        return result
    if not start_date or not end_date:
        return result
    if not aloha_sales_db.db_available():
        return result

    yoy_mode = getattr(report_config, "yoyMode", "same_date")

    # Fiscal calendar mode: look up per-day LYDate mappings
    fiscal_date_map: Optional[Dict[str, str]] = None  # current_date -> ly_date
    if yoy_mode == "fiscal":
        fiscal_date_map = aloha_sales_db.get_fiscal_ly_dates(
            start_date.isoformat(), end_date.isoformat()
        )
        if not fiscal_date_map:
            logger.info("YoY sales: fiscal mode selected but no fiscal calendar data — falling back to same_date")
            yoy_mode = "same_date"
            fiscal_date_map = None

    # Prior-year date range
    if fiscal_date_map:
        ly_dates = sorted(fiscal_date_map.values())
        aloha_start = ly_dates[0]
        aloha_end = ly_dates[-1]
        yoy_offset = None  # not a fixed offset in fiscal mode
    else:
        yoy_offset = 364 if yoy_mode == "same_day" else 365
        aloha_start = (start_date - timedelta(days=yoy_offset)).isoformat()
        aloha_end = (end_date - timedelta(days=yoy_offset)).isoformat()

    # Build restaurant_name -> aloha_location_id lookup
    #   raw_rows have restaurantGuid + restaurantName
    #   aloha_location_mapping has toast_guid -> aloha_id
    loc_mapping = get_aloha_location_mapping()  # toast_guid -> aloha_id
    if not loc_mapping:
        logger.info("YoY sales: no aloha_location_mapping.json — skipping enrichment")
        return result

    # Build name -> aloha_id via the GUID bridge
    name_to_aloha: Dict[str, str] = {}
    for row in raw_rows:
        guid = row.get("restaurantGuid", "")
        name = row.get("restaurantName", "")
        if guid and name and guid in loc_mapping:
            name_to_aloha[name] = loc_mapping[guid]

    if not name_to_aloha:
        logger.info("YoY sales: no location matches between Toast GUIDs and Aloha mapping")
        return result

    # Build location filter from selected restaurants (if any)
    aloha_locations: Optional[List[str]] = None
    if report_config.restaurantIds:
        aloha_locations = [
            loc_mapping[guid] for guid in report_config.restaurantIds
            if guid in loc_mapping
        ]
        if not aloha_locations:
            return result

    # Also build aloha_id -> restaurant_name reverse map
    aloha_to_name: Dict[str, str] = {v: k for k, v in name_to_aloha.items()}

    if report_type == "sales_summary":
        return _enrich_sales_summary(result, aloha_start, aloha_end, aloha_locations, name_to_aloha, aloha_to_name, raw_rows, yoy_offset, fiscal_date_map)
    elif report_type == "metrics":
        return _enrich_metrics(result, start_date, end_date, aloha_locations, name_to_aloha, aloha_to_name, yoy_offset, fiscal_date_map)

    return result


def _enrich_sales_summary(
    result: dict,
    aloha_start: str,
    aloha_end: str,
    aloha_locations: Optional[List[str]],
    name_to_aloha: Dict[str, str],
    aloha_to_name: Dict[str, str],
    raw_rows: Optional[list] = None,
    yoy_offset: Optional[int] = 365,
    fiscal_date_map: Optional[Dict[str, str]] = None,
) -> dict:
    """Add priorYearNetSales and yoyChangePct to each sales summary row."""
    aloha_df = aloha_sales_db.query_date_range(aloha_start, aloha_end, locations=aloha_locations)
    if aloha_df.empty:
        result["yoySalesInfo"] = {
            "alohaStartDate": aloha_start, "alohaEndDate": aloha_end,
            "status": "no_data", "matchedLocations": 0,
        }
        return result

    # Build aloha_id -> sales lookups (total, catering, non-catering)
    aloha_by_loc: Dict[str, float] = {}          # total net sales
    aloha_catering_by_loc: Dict[str, float] = {}  # catering only
    aloha_noncat_by_loc: Dict[str, float] = {}    # non-catering (total minus catering)
    for _, arow in aloha_df.iterrows():
        loc = str(arow["location"])
        total_ns = float(arow["net_sales"])
        catering_ns = float(arow.get("catering_net_sales", 0) or 0)
        aloha_by_loc[loc] = total_ns
        aloha_catering_by_loc[loc] = catering_ns
        aloha_noncat_by_loc[loc] = total_ns - catering_ns

    # Map each table to its appropriate Aloha lookup
    table_lookups = {
        "summaryTable": aloha_by_loc,
        "cateringTable": aloha_catering_by_loc,
        "nonCateringTable": aloha_noncat_by_loc,
    }

    matched = 0
    total_prior = 0.0

    for table_key in ("summaryTable", "cateringTable", "nonCateringTable"):
        lookup = table_lookups[table_key]
        table = result.get(table_key, [])
        for row in table:
            r_name = row.get("restaurantName", "")
            aloha_id = name_to_aloha.get(r_name)
            if aloha_id and aloha_id in lookup:
                prior_ns = round(lookup[aloha_id], 2)
                row["priorYearNetSales"] = prior_ns
                current_ns = row.get("netSales", 0)
                row["yoyChange"] = round(current_ns - prior_ns, 2)
                row["yoyChangePct"] = round(((current_ns - prior_ns) / prior_ns) * 100, 1) if prior_ns else 0.0
                if table_key == "summaryTable":
                    matched += 1
                    total_prior += prior_ns

    # Totals
    for totals_key in ("summaryTotals", "cateringTotals", "nonCateringTotals"):
        totals = result.get(totals_key)
        if totals:
            # Sum prior-year net sales across all matched locations for this totals bucket
            table_key = totals_key.replace("Totals", "Table")
            table = result.get(table_key, [])
            prior_sum = sum(r.get("priorYearNetSales", 0) for r in table)
            totals["priorYearNetSales"] = round(prior_sum, 2)
            current_ns = totals.get("netSales", 0)
            totals["yoyChange"] = round(current_ns - prior_sum, 2)
            totals["yoyChangePct"] = round(((current_ns - prior_sum) / prior_sum) * 100, 1) if prior_sum else 0.0

    result["yoySalesInfo"] = {
        "alohaStartDate": aloha_start,
        "alohaEndDate": aloha_end,
        "status": "ok",
        "matchedLocations": matched,
        "totalPriorYearNetSales": round(total_prior, 2),
    }

    # ── Daily YoY breakdown ─────────────────────────────────────────────────
    if raw_rows:
        # 1. Aggregate Toast net sales by business date
        toast_daily: Dict[str, float] = {}
        for row in raw_rows:
            b_date = row.get("businessDate", "")
            if not b_date:
                continue
            toast_daily[b_date] = toast_daily.get(b_date, 0.0) + float(row.get("netSalesAmount") or 0)

        # 2. Query Aloha daily data for the prior year range
        aloha_daily_df = aloha_sales_db.query_date_range_daily(
            aloha_start, aloha_end, locations=aloha_locations
        )
        # Sum Aloha net_sales by date (across all locations)
        aloha_daily: Dict[str, float] = {}
        if not aloha_daily_df.empty:
            for _, arow in aloha_daily_df.iterrows():
                a_date = arow["business_date"]
                aloha_daily[a_date] = aloha_daily.get(a_date, 0.0) + float(arow["net_sales"])

        # 3. Build the daily breakdown list
        daily_breakdown = []
        for b_date in sorted(toast_daily.keys()):
            try:
                # ERA businessDate can be YYYYMMDD or YYYY-MM-DD
                if len(b_date) == 8 and "-" not in b_date:
                    d = date(int(b_date[:4]), int(b_date[4:6]), int(b_date[6:8]))
                else:
                    d = date.fromisoformat(b_date)
                if fiscal_date_map and d.isoformat() in fiscal_date_map:
                    aloha_date = fiscal_date_map[d.isoformat()]
                else:
                    aloha_date = (d - timedelta(days=yoy_offset or 365)).isoformat()
                day_of_week = d.strftime("%A")
            except (ValueError, IndexError):
                continue
            toast_ns = round(toast_daily[b_date], 2)
            aloha_ns = round(aloha_daily.get(aloha_date, 0), 2) if aloha_date in aloha_daily else None
            yoy_pct = None
            if aloha_ns and aloha_ns > 0:
                yoy_pct = round(((toast_ns - aloha_ns) / aloha_ns) * 100, 1)
            daily_breakdown.append({
                "businessDate": d.isoformat(),  # normalize to YYYY-MM-DD
                "dayOfWeek": day_of_week,
                "toastNetSales": toast_ns,
                "alohaDate": aloha_date,
                "alohaNetSales": aloha_ns,
                "yoyChangePct": yoy_pct,
            })

        result["dailyBreakdown"] = daily_breakdown

    return result


def _enrich_metrics(
    result: dict,
    start_date: date,
    end_date: date,
    aloha_locations: Optional[List[str]],
    name_to_aloha: Dict[str, str],
    aloha_to_name: Dict[str, str],
    yoy_offset: Optional[int] = 365,
    fiscal_date_map: Optional[Dict[str, str]] = None,
) -> dict:
    """Add prior-year Aloha net sales columns to the metrics pivot table."""
    if fiscal_date_map:
        ly_dates = sorted(fiscal_date_map.values())
        aloha_start = ly_dates[0]
        aloha_end = ly_dates[-1]
    else:
        aloha_start = (start_date - timedelta(days=yoy_offset or 365)).isoformat()
        aloha_end = (end_date - timedelta(days=yoy_offset or 365)).isoformat()

    aloha_df = aloha_sales_db.query_date_range_daily(aloha_start, aloha_end, locations=aloha_locations)
    if aloha_df.empty:
        result["yoySalesInfo"] = {
            "alohaStartDate": aloha_start, "alohaEndDate": aloha_end,
            "status": "no_data", "matchedLocations": 0,
        }
        return result

    # Build reverse fiscal map: ly_date -> current_date (for fiscal mode)
    _ly_to_current: Dict[str, str] = {}
    if fiscal_date_map:
        for cur_d, ly_d in fiscal_date_map.items():
            _ly_to_current[ly_d] = cur_d

    # Build lookup: current_date_key -> {aloha_id: net_sales}
    aloha_daily: Dict[str, Dict[str, float]] = {}
    for _, arow in aloha_df.iterrows():
        aloha_date = arow["business_date"]
        aloha_loc = str(arow["location"])
        ns = float(arow["net_sales"])
        # Map aloha date to the corresponding current-year date
        try:
            if fiscal_date_map and aloha_date in _ly_to_current:
                current_key = _ly_to_current[aloha_date].replace("-", "")
            else:
                a_date = date.fromisoformat(aloha_date)
                current_date = a_date + timedelta(days=yoy_offset or 365)
                current_key = current_date.strftime("%Y%m%d")
        except ValueError:
            continue
        if current_key not in aloha_daily:
            aloha_daily[current_key] = {}
        aloha_daily[current_key][aloha_loc] = ns

    # Enrich pivot rows
    pivot_table = result.get("pivotTable", [])
    for row in pivot_table:
        r_name = row.get("restaurantName", "")
        aloha_id = name_to_aloha.get(r_name)
        if not aloha_id:
            continue
        for date_key, loc_sales in aloha_daily.items():
            if aloha_id in loc_sales:
                row[f"{date_key}_aloha"] = round(loc_sales[aloha_id], 2)

    # Enrich totals
    totals = result.get("totals", [])
    for tot_row in totals:
        date_key = tot_row.get("date", "")
        if date_key in aloha_daily:
            total_aloha = sum(aloha_daily[date_key].values())
            tot_row["totalNetSalesAloha"] = round(total_aloha, 2)

    matched_locs = len({name_to_aloha[n] for n in name_to_aloha if name_to_aloha[n] in
                        {loc for daily in aloha_daily.values() for loc in daily}})
    result["yoySalesInfo"] = {
        "alohaStartDate": aloha_start,
        "alohaEndDate": aloha_end,
        "status": "ok",
        "matchedLocations": matched_locs,
        "datesWithData": len(aloha_daily),
    }
    return result


def _compute_ly_date_range(
    start_date: date,
    end_date: date,
    yoy_mode: str,
) -> Tuple[date, date, Optional[Dict[str, str]]]:
    """
    Determine the Last-Year date range mirroring the same logic
    apply_yoy_aloha_quantities uses, so Toast LY and Aloha LY agree on what
    "a year ago" means.

    Returns (ly_start, ly_end, fiscal_date_map_or_None).
    """
    fiscal_date_map: Optional[Dict[str, str]] = None
    if yoy_mode == "fiscal":
        fiscal_date_map = aloha_sales_db.get_fiscal_ly_dates(
            start_date.isoformat(), end_date.isoformat()
        )
        if not fiscal_date_map:
            yoy_mode = "same_date"
    if fiscal_date_map:
        ly_dates = sorted(fiscal_date_map.values())
        return (date.fromisoformat(ly_dates[0]),
                date.fromisoformat(ly_dates[-1]),
                fiscal_date_map)
    offset = 364 if yoy_mode == "same_day" else 365
    return (start_date - timedelta(days=offset),
            end_date - timedelta(days=offset),
            None)


async def apply_yoy_toast_quantities(
    result: dict,
    report_config: "ReportConfig",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    """
    Stamp `lyToastQty` on every breakdown row by querying Toast for the same
    period, one year prior, scoped to stores that were already on Toast then.

    Eligibility: a store contributes to LY Toast only when its switchover_date
    is on or before the LY range's start (fully post-switchover). Anything
    partial → the store contributes 0 here and falls through to Aloha LY
    (which the All Items + Recipe Mix Summary sheets already handle as a
    fallback via `effectiveLyQty = lyToastQty || alohaQty`).
    """
    if (not result or report_config.reportType != "menu"
            or not report_config.yoyEnabled
            or start_date is None or end_date is None):
        return result

    switchover_map = (toast_config_db.get_switchover_date_map()
                      if toast_config_db.db_available() else {})
    if not switchover_map:
        # No dates configured → nothing to do; column stays blank as in PR1.
        result.setdefault("yoyToastInfo", {"status": "skipped",
                                            "reason": "no switchover dates configured"})
        return result

    yoy_mode = getattr(report_config, "yoyMode", "same_date")
    ly_start, ly_end, _ = _compute_ly_date_range(start_date, end_date, yoy_mode)

    # Filter to the report's stores AND those whose switchover is on/before ly_start.
    report_stores = list(getattr(report_config, "restaurantIds", None) or [])
    eligible: List[str] = []
    for guid in report_stores:
        sd_str = switchover_map.get(guid)
        if not sd_str:
            continue
        try:
            sd = date.fromisoformat(sd_str)
        except ValueError:
            continue
        if sd <= ly_start:
            eligible.append(guid)

    if not eligible:
        result["yoyToastInfo"] = {
            "status": "no_eligible_stores",
            "lyStart": ly_start.isoformat(),
            "lyEnd": ly_end.isoformat(),
            "storesChecked": len(report_stores),
            "storesWithSwitchoverDate": sum(1 for g in report_stores if g in switchover_map),
        }
        return result

    # Request the same-period prior-year menu report for the eligible stores.
    # groupBy=["MENU_ITEM"] matches the rest of the menu pipeline (Toast's
    # Analytics API rejects "restaurantId" as a groupBy on the menu endpoint).
    rows: List[Dict] = []
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            report_guid = await request_toast_report(
                client, ly_start, ly_end, eligible, "menu",
                ["MENU_ITEM"],
            )
            rows = await fetch_toast_report(client, report_guid, "menu") or []
    except HTTPException as exc:
        logger.warning(f"LY Toast report fetch failed (status={exc.status_code}): {exc.detail}")
        result["yoyToastInfo"] = {
            "status": "error",
            "error": str(exc.detail)[:200],
            "lyStart": ly_start.isoformat(),
            "lyEnd": ly_end.isoformat(),
            "storesEligible": len(eligible),
        }
        return result
    except Exception as exc:
        logger.warning(f"LY Toast report fetch failed: {exc}")
        result["yoyToastInfo"] = {
            "status": "error",
            "error": str(exc)[:200],
            "lyStart": ly_start.isoformat(),
            "lyEnd": ly_end.isoformat(),
            "storesEligible": len(eligible),
        }
        return result

    # Aggregate by menu_item_guid (chain-wide LY total across eligible stores).
    ly_qty_by_guid: Dict[str, float] = {}
    for row in rows:
        guid = row.get("menuItemGuid") or ""
        if not guid:
            continue
        ly_qty_by_guid[guid] = ly_qty_by_guid.get(guid, 0.0) + (row.get("quantitySold") or 0)

    # Stamp on every breakdown row in the result. orderModeSplit rows share
    # object identity with itemBreakdownDaily rows in main.py's pivot output,
    # so this single pass covers all four lists.
    def _stamp(items):
        for it in items or []:
            g = it.get("menuItemGuid") or ""
            if g and g in ly_qty_by_guid:
                it["lyToastQty"] = ly_qty_by_guid[g]

    _stamp(result.get("itemBreakdown"))
    _stamp(result.get("itemBreakdownDaily"))
    oms = result.get("orderModeSplit") or {}
    for ch in ("inStore", "olo"):
        _stamp(oms.get(ch, {}).get("itemBreakdown"))
        _stamp(oms.get(ch, {}).get("itemBreakdownDaily"))

    result["yoyToastInfo"] = {
        "status": "ok",
        "lyStart": ly_start.isoformat(),
        "lyEnd": ly_end.isoformat(),
        "storesChecked": len(report_stores),
        "storesWithSwitchoverDate": sum(1 for g in report_stores if g in switchover_map),
        "storesEligible": len(eligible),
        "rowsReturned": len(rows),
        "itemsWithLyToast": len(ly_qty_by_guid),
    }
    return result


async def apply_yoy_aloha_quantities(
    result: dict,
    report_config: "ReportConfig",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    if not result or report_config.reportType != "menu" or not report_config.yoyEnabled:
        return result

    consolidator = get_yoy_consolidator()
    aloha_df = None
    aloha_start = None
    aloha_end = None

    if start_date is not None and end_date is not None:
        yoy_mode = getattr(report_config, "yoyMode", "same_date")

        # Fiscal calendar mode for menu reports
        fiscal_date_map: Optional[Dict[str, str]] = None
        if yoy_mode == "fiscal":
            fiscal_date_map = aloha_sales_db.get_fiscal_ly_dates(
                start_date.isoformat(), end_date.isoformat()
            )
            if not fiscal_date_map:
                yoy_mode = "same_date"  # fallback

        if fiscal_date_map:
            ly_dates = sorted(fiscal_date_map.values())
            aloha_start = date.fromisoformat(ly_dates[0])
            aloha_end = date.fromisoformat(ly_dates[-1])
        else:
            yoy_offset = 364 if yoy_mode == "same_day" else 365
            aloha_start = start_date - timedelta(days=yoy_offset)
            aloha_end = end_date - timedelta(days=yoy_offset)

        # ── Resolve location and order-mode filters BEFORE the query ──────────
        loc_filter: Optional[List[str]] = None
        if getattr(report_config, "restaurantIds", None):
            loc_mapping = get_aloha_location_mapping()
            if loc_mapping:
                allowed_aloha = [loc_mapping[rid] for rid in report_config.restaurantIds if rid in loc_mapping]
                if allowed_aloha:
                    loc_filter = allowed_aloha

        om_filter: Optional[List[str]] = None
        report_channel = getattr(report_config, "orderModeFilter", None)
        if report_channel:
            om_mapping = get_aloha_order_mode_mapping()
            if om_mapping:
                allowed_modes = [k for k, v in om_mapping.items() if v == report_channel]
                if allowed_modes:
                    om_filter = allowed_modes

        # ── Primary path: SQLite (single query, no per-file loop) ─────────────
        if aloha_db.db_available():
            try:
                aloha_df = await asyncio.to_thread(
                    aloha_db.query_date_range,
                    aloha_start.isoformat(),
                    aloha_end.isoformat(),
                    loc_filter,
                    om_filter,
                )
            except Exception as exc:
                logger.warning(f"SQLite YoY query failed, falling back to CSV: {exc}")
                aloha_df = None

        # ── Fallback path: per-date CSV loop (legacy / pre-SQLite data) ───────
        if aloha_df is None or aloha_df.empty:
            dfs = []
            d = aloha_start
            while d <= aloha_end:
                path = get_aloha_file_path_if_exists(d.isoformat())
                if path:
                    try:
                        df = await asyncio.to_thread(consolidator.load_aloha_data, path)
                        if not df.empty:
                            dfs.append(df)
                    except Exception as e:
                        logger.warning(f"Could not load Aloha file for {d}: {e}")
                d += timedelta(days=1)
            if dfs:
                aloha_df = pd.concat(dfs, ignore_index=True)
                # Apply filters that SQLite would have applied automatically
                if loc_filter and "Location" in aloha_df.columns:
                    aloha_df = aloha_df[aloha_df["Location"].astype(str).str.strip().isin(set(loc_filter))].copy()
                if om_filter and "OrderMode" in aloha_df.columns:
                    aloha_df = aloha_df[aloha_df["OrderMode"].astype(str).str.strip().isin(set(om_filter))].copy()

        if aloha_df is None or aloha_df.empty:
            raise HTTPException(
                status_code=400,
                detail=f"No Aloha data found for YoY range {aloha_start.isoformat()} to {aloha_end.isoformat()}. Import data for that range first."
            )

    elif report_config.alohaDate:
        # Legacy: single date
        try:
            aloha_date_obj = date.fromisoformat(report_config.alohaDate)
        except ValueError:
            raise HTTPException(status_code=400, detail="alohaDate must be YYYY-MM-DD")
        aloha_start = aloha_end = aloha_date_obj

        # Try SQLite first
        if aloha_db.db_available():
            try:
                aloha_df = await asyncio.to_thread(
                    aloha_db.query_date_range,
                    aloha_date_obj.isoformat(),
                    aloha_date_obj.isoformat(),
                )
            except Exception:
                aloha_df = None

        # Fallback to CSV
        if aloha_df is None or aloha_df.empty:
            aloha_file_path = get_aloha_file_path(aloha_date_obj.isoformat())
            aloha_df = await asyncio.to_thread(consolidator.load_aloha_data, aloha_file_path)
    else:
        raise HTTPException(
            status_code=400,
            detail="Year-over-Year comparison requires the report date range (Aloha range is computed as same range, 1 year prior)."
        )

    if aloha_df is None or aloha_df.empty:
        return result

    aloha_qty_by_name = (
        aloha_df.groupby("AlohaItem", dropna=False)["Quantity"].sum().to_dict()
    )
    aloha_qty_by_name_norm = {
        consolidator.normalize_name(name): qty
        for name, qty in aloha_qty_by_name.items()
        if consolidator.normalize_name(name)
    }
    aloha_name_by_norm = {
        consolidator.normalize_name(name): name
        for name in aloha_qty_by_name.keys()
        if consolidator.normalize_name(name)
    }
    toast_to_aloha = consolidator.toast_to_aloha_map
    toast_name_to_aloha = consolidator.toast_name_to_aloha_map
    match_stats = {
        "matchedByGuid": 0,
        "matchedByToastName": 0,
        "matchedByAlohaName": 0,
        "matchedTotal": 0
    }

    def lookup_aloha_qty(menu_item_guid: str, menu_item_name: str) -> Tuple[float, Optional[str], Optional[str]]:
        if menu_item_guid:
            aloha_name = toast_to_aloha.get(menu_item_guid)
            if aloha_name:
                qty = float(aloha_qty_by_name.get(aloha_name, 0))
                if qty:
                    match_stats["matchedByGuid"] += 1
                    match_stats["matchedTotal"] += 1
                return qty, aloha_name, "guid"
        if menu_item_name:
            normalized = consolidator.normalize_name(menu_item_name)
            aloha_name = toast_name_to_aloha.get(normalized)
            if aloha_name:
                qty = float(aloha_qty_by_name.get(aloha_name, 0))
                if qty:
                    match_stats["matchedByToastName"] += 1
                    match_stats["matchedTotal"] += 1
                return qty, aloha_name, "toastName"
            if normalized in aloha_qty_by_name_norm:
                qty = float(aloha_qty_by_name_norm.get(normalized, 0))
                if qty:
                    match_stats["matchedByAlohaName"] += 1
                    match_stats["matchedTotal"] += 1
                return qty, aloha_name_by_norm.get(normalized), "alohaName"
        return 0.0, None, None

    def enrich_rows(rows: list) -> None:
        for row in rows:
            guid = row.get("menuItemGuid")
            name = row.get("menuItemName")
            aloha_qty, aloha_name, source = lookup_aloha_qty(guid, name)
            row["alohaQty"] = aloha_qty
            if aloha_name:
                row["alohaItemName"] = aloha_name
                row["alohaDetails"] = [{"name": aloha_name, "qty": aloha_qty}]
            elif aloha_qty:
                row["alohaDetails"] = [{"name": "Matched Aloha Item", "qty": aloha_qty}]
            if source:
                row["alohaMatchSource"] = source

    enrich_rows(result.get("itemBreakdownDaily", []))
    enrich_rows(result.get("itemBreakdown", []))
    enrich_rows(result.get("itemTotals", []))

    # ── Per-channel YoY enrichment for OLO / In-Store split ─────────────────
    # The rows in orderModeSplit share the same objects as itemBreakdownDaily,
    # so the main enrich_rows above already stamped them with *total* Aloha qty
    # (all order modes combined).  Re-enrich each channel's rows with Aloha data
    # filtered to just that channel's order modes, so OLO and In-Store show the
    # correct channel-specific prior-year quantities instead of the same total.
    _split_debug: dict = {}
    _order_mode_split = result.get("orderModeSplit")

    # ── Diagnose why the block might be skipped ──────────────────────────────
    _sd_has_split    = bool(_order_mode_split)
    _sd_has_om_col   = "OrderMode" in aloha_df.columns
    _sd_om_filter    = om_filter  # None means no filter → block runs
    _sd_distinct_om  = sorted(aloha_df["OrderMode"].dropna().unique().tolist()) if _sd_has_om_col else []

    _split_debug["blockConditions"] = {
        "hasOrderModeSplit":   _sd_has_split,
        "hasOrderModeColumn":  _sd_has_om_col,
        "omFilterActive":      bool(_sd_om_filter),
        "omFilterValue":       _sd_om_filter,
        "distinctOrderModesInAloha": _sd_distinct_om,
        "alohaRowCount": int(len(aloha_df)),
    }

    if _sd_has_split and _sd_has_om_col and not _sd_om_filter:
        _split_debug["blockEntered"] = True
        _om_mapping = get_aloha_order_mode_mapping()
        _split_debug["omMappingFound"]  = bool(_om_mapping)
        _split_debug["omMappingCount"]  = len(_om_mapping) if _om_mapping else 0
        _split_debug["omMappingSample"] = dict(list(_om_mapping.items())[:15]) if _om_mapping else {}

        if _om_mapping:
            _OLO_CHANNELS     = {"OLO / Online", "Delivery"}
            # "In Store" is the label used in aloha_order_mode_mapping.json for Dine-In,
            # To Go, Curbside, Drive Thru, Call-In, POS 2, etc.
            # The legacy labels ("Dine In", "Take Out", etc.) are kept for any older
            # mapping files that may use them.
            _INSTORE_CHANNELS = {"In Store", "Dine In", "Take Out", "Drive Thru", "Catering", "Other"}

            _olo_modes     = {k for k, v in _om_mapping.items() if v in _OLO_CHANNELS}
            _instore_modes = {k for k, v in _om_mapping.items() if v in _INSTORE_CHANNELS}

            _split_debug["oloChannelLabels"]     = sorted(_OLO_CHANNELS)
            _split_debug["instoreChannelLabels"] = sorted(_INSTORE_CHANNELS)
            _split_debug["oloModes"]             = sorted(_olo_modes)
            _split_debug["instoreModes"]         = sorted(_instore_modes)

            def _make_split_lookup(df_split):
                _qty   = df_split.groupby("AlohaItem", dropna=False)["Quantity"].sum().to_dict()
                _qnorm = {consolidator.normalize_name(n): q for n, q in _qty.items() if consolidator.normalize_name(n)}
                _nnorm = {consolidator.normalize_name(n): n for n in _qty if consolidator.normalize_name(n)}
                def _look(guid, name):
                    if guid:
                        an = toast_to_aloha.get(guid)
                        if an:
                            return float(_qty.get(an, 0)), an, "guid"
                    if name:
                        norm = consolidator.normalize_name(name)
                        an = toast_name_to_aloha.get(norm)
                        if an:
                            return float(_qty.get(an, 0)), an, "toastName"
                        if norm in _qnorm:
                            return float(_qnorm.get(norm, 0)), _nnorm.get(norm), "alohaName"
                    return 0.0, None, None
                return _look

            def _enrich_split(rows, lkp):
                for row in rows:
                    aq, an, src = lkp(row.get("menuItemGuid"), row.get("menuItemName"))
                    row["alohaQty"] = aq
                    if an:
                        row["alohaItemName"] = an
                        row["alohaDetails"] = [{"name": an, "qty": aq}]
                    elif aq:
                        row["alohaDetails"] = [{"name": "Matched Aloha Item", "qty": aq}]
                    if src:
                        row["alohaMatchSource"] = src

            # ── Deep-copy split rows so they are independent of combined ────
            # The rows in orderModeSplit originally share the same dict objects
            # as itemBreakdown / itemTotals.  If we enrich splits in-place, the
            # combined rows get overwritten (and vice-versa on restore).
            # Deep-copying the split lists makes them fully independent, so each
            # channel gets its own alohaQty without affecting the combined view.
            for _ch_key in ("olo", "inStore"):
                _ch = _order_mode_split.get(_ch_key, {})
                for _list_key in ("itemBreakdownDaily", "itemBreakdown", "itemTotals"):
                    if _list_key in _ch:
                        _ch[_list_key] = copy.deepcopy(_ch[_list_key])

            # ── OLO ──────────────────────────────────────────────────────────
            if _olo_modes:
                _df_olo = aloha_df[aloha_df["OrderMode"].isin(_olo_modes)]
                _split_debug["oloAlohaRows"]   = int(len(_df_olo))
                _split_debug["oloAlohaItems"]  = int(_df_olo["AlohaItem"].nunique()) if not _df_olo.empty else 0
                if not _df_olo.empty:
                    _lkp_olo = _make_split_lookup(_df_olo)
                    _enrich_split(_order_mode_split.get("olo", {}).get("itemBreakdownDaily", []), _lkp_olo)
                    _enrich_split(_order_mode_split.get("olo", {}).get("itemBreakdown", []), _lkp_olo)
                else:
                    _split_debug["oloSkipped"] = "df_olo is empty after filtering"
            else:
                _split_debug["oloSkipped"] = "no Aloha order modes mapped to OLO channels"

            # ── In-Store ──────────────────────────────────────────────────────
            if _instore_modes:
                _df_instore = aloha_df[aloha_df["OrderMode"].isin(_instore_modes)]
                _split_debug["instoreAlohaRows"]  = int(len(_df_instore))
                _split_debug["instoreAlohaItems"] = int(_df_instore["AlohaItem"].nunique()) if not _df_instore.empty else 0
                if not _df_instore.empty:
                    _lkp_instore = _make_split_lookup(_df_instore)
                    _enrich_split(_order_mode_split.get("inStore", {}).get("itemBreakdownDaily", []), _lkp_instore)
                    _enrich_split(_order_mode_split.get("inStore", {}).get("itemBreakdown", []), _lkp_instore)
                else:
                    _split_debug["instoreSkipped"] = "df_instore is empty after filtering"
            else:
                _split_debug["instoreSkipped"] = "no Aloha order modes mapped to In-Store channels"

            # ── Post-enrichment sample ────────────────────────────────────────
            _olo_rows     = _order_mode_split.get("olo",     {}).get("itemBreakdown", [])
            _instore_rows = _order_mode_split.get("inStore", {}).get("itemBreakdown", [])
            _split_debug["oloItemCount"]      = len(_olo_rows)
            _split_debug["instoreItemCount"]  = len(_instore_rows)
            _split_debug["oloMatchedCount"]   = sum(1 for r in _olo_rows     if r.get("alohaQty"))
            _split_debug["instoreMatchedCount"] = sum(1 for r in _instore_rows if r.get("alohaQty"))
            _split_debug["oloSample"] = [
                {"name": r.get("menuItemName"), "guid": (r.get("menuItemGuid") or "")[:8],
                 "alohaQty": r.get("alohaQty"), "alohaName": r.get("alohaItemName"),
                 "matchSrc": r.get("alohaMatchSource")}
                for r in _olo_rows[:5]
            ]
            _split_debug["instoreSample"] = [
                {"name": r.get("menuItemName"), "guid": (r.get("menuItemGuid") or "")[:8],
                 "alohaQty": r.get("alohaQty"), "alohaName": r.get("alohaItemName"),
                 "matchSrc": r.get("alohaMatchSource")}
                for r in _instore_rows[:5]
            ]
            logger.info(
                f"[YoY split debug] oloModes={len(_olo_modes)} instoreModes={len(_instore_modes)} "
                f"oloAlohaRows={_split_debug.get('oloAlohaRows','?')} "
                f"instoreAlohaRows={_split_debug.get('instoreAlohaRows','?')} "
                f"oloItems={len(_olo_rows)} matched={_split_debug['oloMatchedCount']} "
                f"instoreItems={len(_instore_rows)} matched={_split_debug['instoreMatchedCount']}"
            )
        else:
            _split_debug["blockEntered"]  = True
            _split_debug["omMappingFound"] = False
            _split_debug["note"] = "Order mode mapping file not found or empty — no split enrichment performed"
    else:
        _split_debug["blockEntered"] = False
        _reasons = []
        if not _sd_has_split:   _reasons.append("result has no orderModeSplit key")
        if not _sd_has_om_col:  _reasons.append("aloha_df has no OrderMode column")
        if _sd_om_filter:       _reasons.append(f"om_filter is active: {_sd_om_filter}")
        _split_debug["skippedReasons"] = _reasons
        logger.info(f"[YoY split debug] block SKIPPED — reasons: {_reasons}")

    yoy_info = result.setdefault("yoyInfo", {})
    yoy_info["alohaDateRange"] = {"start": aloha_start.isoformat(), "end": aloha_end.isoformat()}
    yoy_info["alohaDate"] = aloha_start.isoformat()
    yoy_info["yoyMode"] = getattr(report_config, "yoyMode", "same_date")
    yoy_info["debug"] = {
        "alohaDistinctItems": int(len(aloha_qty_by_name)),
        "alohaNonZeroItems": int(sum(1 for qty in aloha_qty_by_name.values() if qty)),
        "matchedByGuid": match_stats["matchedByGuid"],
        "matchedByToastName": match_stats["matchedByToastName"],
        "matchedByAlohaName": match_stats["matchedByAlohaName"],
        "matchedTotal": match_stats["matchedTotal"],
        "splitDebug": _split_debug,
    }
    return result

# Authentication configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days
INVITE_EXPIRY_HOURS = 72  # Invite link valid for 72 hours
PASSWORD_RESET_EXPIRY_HOURS = 1  # Password reset link valid for 1 hour

# Base URL for building invite links (e.g. https://reports.newks.com)
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# Super Admin configuration (for initial setup)
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").lower()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "Super Admin")

# Super Admin configuration (for initial setup)
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").lower()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "Super Admin")

# Password hashing - using bcrypt directly
security = HTTPBearer()

if "/era/v1" in TOAST_API_BASE_URL:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.replace("/era/v1", "")
else:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.rsplit("/", 1)[0] if "/" in TOAST_API_BASE_URL else TOAST_API_BASE_URL

TOAST_AUTH_URL = f"{TOAST_AUTH_BASE_URL}/authentication/v1/authentication/login"

if not TOAST_API_TOKEN and (not TOAST_API_CLIENT_ID or not TOAST_API_CLIENT_SECRET):
    raise RuntimeError(
        "Either TOAST_API_TOKEN or both TOAST_API_CLIENT_ID and TOAST_API_CLIENT_SECRET must be set."
    )

_token_cache: Dict[str, any] = {"token": None, "expires_at": 0}
_standard_token_cache: Dict[str, any] = {"token": None, "expires_at": 0}
_menu_tags_cache: Dict[str, dict] = {}  # {restaurant_guid: {hash: ..., mapping: {...}}}
_menu_data_cache: Dict[str, dict] = {}  # {restaurant_guid: {data: ..., expires_at: ...}}
MENU_CACHE_TTL_SECONDS = 300
_menu_data_cache_v2: Dict[str, dict] = {}  # {restaurant_guid: {data: ..., expires_at: ...}}
MENU_V2_CACHE_TTL_SECONDS = 60 * 60 * 12
_price_group_cache: Dict[str, dict] = {}  # {restaurant_guid: {data: ..., expires_at: ...}}
PRICE_GROUP_CACHE_TTL_SECONDS = 300
_locations_cache: Dict[str, Any] = {"data": None, "expires_at": 0}  # single-tenant: cache all locations
LOCATIONS_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours
_sc_config_cache: Dict[str, Dict[str, Dict]] = {}  # {restaurant_guid: {sc_guid: {destination, gratuity, name}}}
_sc_daily_cache: Dict[str, float] = {}  # {f"{restaurant_guid}|{business_date_yyyymmdd}": restaurant_sc_total}
_DEFAULT_TIER_MAPPING_CANDIDATES = [
    os.path.join(ROOT_DIR, "Toast Price Tiers Mapping .xlsx"),
    os.path.join(os.path.dirname(__file__), "price_tier_mapping.csv"),
]
PRICE_TIER_MAPPING_FILE = os.getenv(
    "PRICE_TIER_MAPPING_FILE",
    next((path for path in _DEFAULT_TIER_MAPPING_CANDIDATES if os.path.exists(path)), _DEFAULT_TIER_MAPPING_CANDIDATES[0])
)

# ------------------
# Data Models
# ------------------

class Location(BaseModel):
    restaurantGuid: str
    restaurantName: str
    externalId: Optional[str] = None


class MissingConsolidatedExportResponse(BaseModel):
    success: bool
    output_file: str
    start_date: str
    end_date: str
    total_rows: int
    missing_rows: int


def prioritize_menu_tag_restaurants(restaurant_ids: List[str], locations: List["Location"]) -> List[str]:
    preferred_external = (TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID or "").strip()
    preferred_name = (TOAST_MENU_TAGS_PRIORITY_NAME or "").strip().lower()
    if not preferred_external and not preferred_name:
        return restaurant_ids

    matches: List[str] = []
    for loc in locations:
        if loc.restaurantGuid not in restaurant_ids:
            continue
        if preferred_external and str(loc.externalId or "").strip() == preferred_external:
            matches.append(loc.restaurantGuid)
            continue
        if preferred_name and preferred_name in (loc.restaurantName or "").lower():
            matches.append(loc.restaurantGuid)

    if not matches:
        return restaurant_ids

    ordered = matches + [rid for rid in restaurant_ids if rid not in matches]
    logger.info(f"Prioritized menu tag mapping restaurants: {matches} -> {ordered[:5]}")
    return ordered

class ReportConfig(BaseModel):
    reportType: str  # "metrics" or "menu"
    dateRangeType: str  # "last_7_days", "last_30_days", "custom", etc.
    customStartDate: Optional[str] = None
    customEndDate: Optional[str] = None
    restaurantIds: List[str] = []  # empty = all locations
    groupBy: List[str] = []  # Toast groupBy options
    itemTags: List[str] = []  # For menu reports - filter by item tags
    includeZeroSales: bool = False  # Include items with 0 sales in breakdown
    yoyEnabled: bool = False  # Show YoY (Aloha) quantities in menu reports
    yoyMode: str = "same_date"  # "same_date" (365 days), "same_day" (364 days / 52 weeks), or "fiscal" (Newk's fiscal calendar)
    alohaDate: Optional[str] = None  # YYYY-MM-DD for Aloha comparison
    orderModeFilter: Optional[str] = None  # e.g. "Dine In", "Delivery" — filters Aloha YoY data by channel
    debug: bool = False  # Include debugInfo in response for troubleshooting tag/mapping issues

class SavedReport(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    createdBy: str
    createdAt: str
    lastModified: str
    config: ReportConfig

class SavedReportCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    createdBy: str
    config: ReportConfig

class ItemTagsQuantitiesRequest(BaseModel):
    startBusinessDate: str  # YYYYMMDD
    endBusinessDate: str  # YYYYMMDD
    restaurantIds: List[str]
    daily: bool = False
    tagAttributionMode: str = "MULTI_COUNT"  # MULTI_COUNT | SPLIT | PRIMARY_ONLY

class YoYCompareRequest(BaseModel):
    toast_start_date: str  # YYYY-MM-DD
    toast_end_date: str  # YYYY-MM-DD
    aloha_date: str  # YYYY-MM-DD
    restaurantIds: List[str] = []

class AlohaSqlImportRequest(BaseModel):
    start_date: Optional[str] = None  # YYYY-MM-DD
    end_date: Optional[str] = None  # YYYY-MM-DD
    batch_days: int = 31
    overwrite: bool = False

class AlohaSqlRecentImportRequest(BaseModel):
    days: int = 7
    batch_days: int = 31
    overwrite: bool = False

class AlohaSalesImportRequest(BaseModel):
    start_date: Optional[str] = None  # YYYY-MM-DD
    end_date: Optional[str] = None  # YYYY-MM-DD
    batch_days: int = 31
    overwrite: bool = False

class AlohaSalesRecentImportRequest(BaseModel):
    days: int = 7
    batch_days: int = 31
    overwrite: bool = False

class HelpTicketRequest(BaseModel):
    name: str
    email: str
    subject: str
    priority: int  # 1=Low, 2=Medium, 3=High, 4=Urgent
    description: str

class LaborSummaryRequest(BaseModel):
    restaurantIds: List[str]
    startDate: str   # YYYY-MM-DD
    endDate: str     # YYYY-MM-DD

# ------------------
# V2 Operational Intelligence Models
# ------------------

class V2InsightRequest(BaseModel):
    """Request model for v2 insights endpoints"""
    dateRangeType: str  # "last_7_days", "last_30_days", "last_90_days", "custom"
    customStartDate: Optional[str] = None  # YYYY-MM-DD
    customEndDate: Optional[str] = None  # YYYY-MM-DD
    restaurantIds: List[str] = []  # empty = all locations

class ItemPerformanceResponse(BaseModel):
    """Normalized item performance data"""
    itemName: str
    tagName: Optional[str] = None
    quantity: int
    sales: float
    storeName: str
    date: str  # YYYYMMDD or YYYY-MM-DD

class TopMoversResponse(BaseModel):
    """Top movers insight data"""
    itemName: str
    changePercent: float
    period1Value: float
    period2Value: float
    direction: str  # "up", "down", "neutral"

class StoreVarianceResponse(BaseModel):
    """Store variance insight data"""
    storeName: str
    metric: str  # "netSales", "guestCount", etc.
    value: float
    variance: float  # percentage or absolute difference
    benchmark: float  # comparison value

class StoreRollupResponse(BaseModel):
    """Store rollup aggregated metrics"""
    storeName: str
    storeGuid: str
    netSales: float
    guestCount: int
    itemCount: int
    avgTicket: float
    dateRange: str  # "YYYY-MM-DD to YYYY-MM-DD"

class TrendDeltaResponse(BaseModel):
    """Period-over-period trend delta"""
    metric: str  # "netSales", "guestCount", etc.
    period1Value: float
    period2Value: float
    delta: float  # absolute change
    deltaPercent: float  # percentage change
    period1Label: str
    period2Label: str
    direction: str  # "up", "down", "neutral"

class AnomalyFlagResponse(BaseModel):
    """Anomaly detection flag"""
    storeName: str
    metric: str
    value: float
    expectedValue: float
    deviation: float  # standard deviations or percentage
    severity: str  # "low", "medium", "high"
    flagType: str  # "outlier", "spike", "drop", "trend_break"

class StoreGroup(BaseModel):
    """Store group for organizing locations"""
    id: str
    name: str
    description: Optional[str] = ""
    storeGuids: List[str] = []
    createdAt: str
    updatedAt: str

class StoreGroupCreate(BaseModel):
    """Request to create a store group"""
    name: str
    description: Optional[str] = ""
    storeGuids: List[str] = []

class DashboardWidget(BaseModel):
    """Dashboard widget data"""
    type: str  # "kpi", "chart", "table", "alert"
    title: str
    data: Dict
    config: Optional[Dict] = {}

class DashboardResponse(BaseModel):
    """Dashboard data response"""
    dashboardType: str  # "executive", "operations", "financial"
    widgets: List[DashboardWidget]
    lastUpdated: str
    dateRange: Optional[Dict[str, str]] = None  # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

# ------------------
# User Authentication Models
# ------------------

class User(BaseModel):
    id: str
    email: str
    name: str
    createdAt: str
    lastLogin: Optional[str] = None
    preferences: Optional[Dict] = {}  # User preferences (default locations, etc.)
    role: Optional[str] = "user"  # User role: "user" or "admin"
    assignedRestaurants: Optional[List[str]] = []  # Allowed restaurant GUIDs; [] = show all
    allowedPages: Optional[List[str]] = []  # Page keys user can access; [] = all pages
    status: Optional[str] = "active"  # "invited" or "active"

# Available report pages for page-level permissions
# Report keys match the nav link keys in shared-nav.js (Reports section only;
# Admin & Data items are role-gated, not page-gated).
AVAILABLE_PAGES = {
    # Sales & Performance
    "sales_summary":     "Sales Summary",
    "metrics":           "Net Sales",
    "netsales_weekly":   "Net Sales by Location – Weekly",
    "royalty":           "Royalty Report",
    "daily-flash":       "Daily Sales Flash",
    "wpy":               "Weekly Sales (WPY)",
    # Menu & Items
    "menu":              "Menu Performance",
    "menu-mix":          "Menu Mix Report",
    "pick3":             "Pick 3",
    "pick2":             "Pick 2",
    "full-pairings":     "Full Pairings",
    # Operations & Labor
    "guest_count":       "Guest Count",
    "tip_pool":          "Payroll Report",
    "cashier-vs-kiosk":  "Cashier vs Kiosk",
    "food-cost":         "Food Cost (COGS)",
    # Catering & Loyalty
    "catering":          "Catering Reports",
    "loyalty":           "Loyalty Reports",
}

class UserCreate(BaseModel):
    email: EmailStr
    name: str
    password: Optional[str] = None  # Optional — omit when using invite flow
    role: Optional[str] = "user"
    allowedPages: Optional[List[str]] = []

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    preferences: Optional[Dict] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    user: User

# ------------------
# Auth Helpers
# ------------------

async def get_auth_headers() -> Dict[str, str]:
    if TOAST_API_TOKEN:
        return {"Authorization": f"Bearer {TOAST_API_TOKEN}", "Content-Type": "application/json"}
    
    current_time = time.time()
    if _token_cache["token"] and current_time < _token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_token_cache['token']}", "Content-Type": "application/json"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_API_CLIENT_ID,
            "clientSecret": TOAST_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(TOAST_AUTH_URL, json=auth_body)
        if resp.status_code != 200:
            logger.error(f"Toast ERA auth failed: status={resp.status_code} body={resp.text[:500]}")
            raise HTTPException(status_code=resp.status_code, detail=f"Failed to authenticate: {resp.text}")
        
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
            logger.error(f"No token in Toast ERA auth response; keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
            raise HTTPException(status_code=500, detail="No token received from authentication")
        
        _token_cache["token"] = token
        _token_cache["expires_at"] = current_time + max(expires_in - 300, 60)
        
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

async def get_standard_api_auth_headers() -> Dict[str, str]:
    """Get authorization headers for Standard API (Menus API)"""
    if not TOAST_STANDARD_API_CLIENT_ID or not TOAST_STANDARD_API_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Standard API credentials required for Menus API access."
        )
    
    current_time = time.time()
    if _standard_token_cache["token"] and current_time < _standard_token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_standard_token_cache['token']}", "Content-Type": "application/json"}
    
    standard_auth_url = f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_STANDARD_API_CLIENT_ID,
            "clientSecret": TOAST_STANDARD_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(standard_auth_url, json=auth_body)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Failed to authenticate with Standard API: {resp.text}")
        
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
            raise HTTPException(status_code=500, detail="No token received from Standard API authentication")
        
        _standard_token_cache["token"] = token
        _standard_token_cache["expires_at"] = current_time + max(expires_in - 300, 60)
        
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ------------------
# Toast API Helpers
# ------------------

def to_business_date(d: date) -> str:
    return d.strftime("%Y%m%d")

def chunk_list(items: List[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]

async def get_all_active_locations(client: httpx.AsyncClient, include_test: bool = False, include_inactive: bool = False) -> List[Location]:
    """Get all active locations from Toast API with error handling and in-memory caching."""
    # In-process cache to avoid hammering Toast for every report / request.
    cache_key = f"data_t{include_test}_i{include_inactive}"
    now = time.time()
    cached = _locations_cache.get(cache_key)
    expires_at = _locations_cache.get(f"{cache_key}_expires_at", 0)
    if cached is not None and expires_at > now:
        return cached  # type: ignore[return-value]

    url = f"{TOAST_API_BASE_URL}/restaurants-information"

    try:
        headers = await get_auth_headers()
    except HTTPException:
        raise
    except Exception as e:
        detail = getattr(e, "detail", str(e)) or repr(e)
        logger.error(f"Failed to get auth headers for locations: {type(e).__name__}: {detail}")
        raise HTTPException(status_code=500, detail=f"Authentication failed: {detail}")

    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
    except httpx.TimeoutException:
        logger.error("Timeout fetching locations from Toast API")
        raise HTTPException(status_code=504, detail="Timeout fetching locations from Toast API. Please check your connection.")
    except Exception as e:
        logger.error(f"Network error fetching locations: {e}")
        raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")

    if resp.status_code != 200:
        error_msg = resp.text[:200] if resp.text else "Unknown error"
        logger.error(f"Toast API returned {resp.status_code} for locations: {error_msg}")
        raise HTTPException(status_code=resp.status_code, detail=f"Error fetching locations: {error_msg}")

    try:
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to parse locations response: {e}")
        raise HTTPException(status_code=500, detail="Invalid response from Toast API")

    if not isinstance(data, list):
        logger.error(f"Unexpected locations response format: {type(data)}")
        raise HTTPException(status_code=500, detail="Invalid response format from Toast API")

    active = [
        Location(
            restaurantGuid=item["restaurantGuid"],
            restaurantName=item["restaurantName"],
            externalId=item.get("externalId") or item.get("restaurantExternalId") or item.get("restaurantId"),
        )
        for item in data
        if (include_inactive or item.get("active")) and not item.get("archived") and (include_test or not item.get("testMode"))
    ]

    logger.info(f"Loaded {len(active)} active locations from Toast API (include_test={include_test})")
    _locations_cache[cache_key] = active
    _locations_cache[f"{cache_key}_expires_at"] = now + LOCATIONS_CACHE_TTL_SECONDS
    return active

async def request_toast_report(
    client: httpx.AsyncClient,
    start: date,
    end: date,
    restaurant_ids: List[str],
    report_type: str,
    group_by: List[str],
    max_attempts: int = 3,
    delay_seconds: float = 1.0,
) -> str:
    days_diff = (end - start).days + 1
    
    # Determine endpoint based on date range and report type
    if days_diff == 1:
        time_range = "day"
    elif days_diff <= 7:
        time_range = "week"
    elif days_diff <= 31:
        time_range = "month"
    elif days_diff <= 366:
        time_range = "year"
    else:
        time_range = None
    
    body = {
        "startBusinessDate": to_business_date(start),
        "endBusinessDate": to_business_date(end),
        "restaurantIds": restaurant_ids,
        "excludedRestaurantIds": [],
    }
    
    # Add groupBy if provided
    if group_by:
        body["groupBy"] = group_by
    
    # Choose endpoint
    endpoint_base = "metrics" if report_type in ("metrics", "sales_summary", "royalty") else "menu"

    if time_range and time_range in ["day", "week", "month", "year"]:
        url = f"{TOAST_API_BASE_URL}/{endpoint_base}/{time_range}"
    else:
        url = f"{TOAST_API_BASE_URL}/{endpoint_base}"
    
    logger.info(f"Requesting {report_type} report: URL={url}, Body={body}")
    
    last_resp = None
    for attempt in range(max_attempts):
        headers = await get_auth_headers()
        resp = await client.post(url, headers=headers, json=body)
        last_resp = resp

        if resp.status_code in [502, 503, 504]:
            if attempt < max_attempts - 1:
                logger.warning(f"Toast API returned {resp.status_code} creating report (attempt {attempt + 1}/{max_attempts}); retrying...")
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 8.0)
                continue
        
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail="Toast API rate limit exceeded. Please wait before trying again.")
        
        if resp.status_code != 200:
            error_msg = resp.text
            try:
                error_json = resp.json()
                error_msg = error_json.get("message", error_json.get("detail", error_msg))
            except:
                pass
            raise HTTPException(status_code=resp.status_code, detail=f"Error creating report: {error_msg}")
        
        try:
            data = resp.json()
        except ValueError:
            return resp.text.strip().strip('"')
        
        if isinstance(data, str):
            return data.strip().strip('"')
        
        return data.get("reportRequestGuid") or data.get("guid") or resp.text.strip().strip('"')
    
    if last_resp is not None:
        raise HTTPException(status_code=last_resp.status_code, detail=f"Error creating report: {last_resp.text}")
    raise HTTPException(status_code=500, detail="Error creating report: no response from Toast API")

async def fetch_toast_report(
    client: httpx.AsyncClient,
    report_guid: str,
    report_type: str,
    max_attempts: int = 60,
    delay_seconds: float = 1.0,
) -> list:
    endpoint_base = "metrics" if report_type in ("metrics", "sales_summary", "royalty") else "menu"
    url = f"{TOAST_API_BASE_URL}/{endpoint_base}/{report_guid}"
    params = {"fetchRestaurantNames": "true"}
    
    for attempt in range(max_attempts):
        headers = await get_auth_headers()
        resp = await client.get(url, headers=headers, params=params)
        
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"{report_type.capitalize()} report returned {len(data)} rows")
            return data
        
        if resp.status_code == 202:
            if attempt == 0:
                logger.info(f"Report {report_guid} is still processing, polling...")
            elif attempt % 5 == 0:  # Log every 5th attempt
                logger.info(f"Still waiting for report {report_guid} (attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(delay_seconds)
            # Exponential backoff
            delay_seconds = min(delay_seconds * 1.5, 10.0)
            continue
        
        if resp.status_code in [502, 503, 504]:
            logger.warning(f"Toast API returned {resp.status_code} retrieving report {report_guid} (attempt {attempt + 1}/{max_attempts}); retrying...")
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 1.5, 10.0)
            continue
        
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail="Toast API rate limit exceeded while retrieving report.")
        
        error_msg = resp.text
        try:
            error_json = resp.json()
            error_msg = error_json.get("message", error_json.get("detail", error_msg))
        except:
            pass
        
        raise HTTPException(status_code=resp.status_code, detail=f"Error retrieving report: {error_msg}")
    
    raise HTTPException(status_code=504, detail=f"Timed out waiting for report {report_guid}. Try narrowing the date range.")


async def _fetch_orders_from_toast_api(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> List[Dict]:
    """Fetch all orders for one restaurant and one business day via Orders API (paginated). Returns list of Order objects."""
    standard_headers = await get_standard_api_auth_headers()
    headers = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid,
    }
    url = f"{TOAST_ORDERS_API_BASE_URL}/orders/v2/ordersBulk"
    all_orders: List[Dict] = []
    page = 1
    page_size = 100
    while True:
        params = {"businessDate": business_date_yyyymmdd, "pageSize": page_size, "page": page}
        resp = await client.get(url, headers=headers, params=params, timeout=60.0)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Orders API error: {resp.text[:500]}",
            )
        data = resp.json()
        if not isinstance(data, list):
            break
        all_orders.extend(data)
        if len(data) < page_size:
            break
        page += 1
        await asyncio.sleep(0.25)
    return all_orders


async def _fetch_orders_for_restaurant_day(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> List[Dict]:
    """Fetch orders: from cache if available and not today, otherwise from Toast API."""
    today_yyyymmdd = date.today().strftime("%Y%m%d")

    # Today's data always live
    if business_date_yyyymmdd == today_yyyymmdd:
        _t0 = time.perf_counter()
        result = await _fetch_orders_from_toast_api(client, restaurant_guid, business_date_yyyymmdd)
        logger.info("PROFILE day src=live_today rguid=%s bd=%s secs=%.3f orders=%d",
                    restaurant_guid[:8], business_date_yyyymmdd, time.perf_counter() - _t0, len(result))
        return result

    # Check cache
    if toast_orders_db.is_date_cached(restaurant_guid, business_date_yyyymmdd):
        _t0 = time.perf_counter()
        cached = toast_orders_db.get_cached_orders(restaurant_guid, business_date_yyyymmdd)
        if cached:
            logger.info("PROFILE day src=cache rguid=%s bd=%s secs=%.3f orders=%d",
                        restaurant_guid[:8], business_date_yyyymmdd, time.perf_counter() - _t0, len(cached))
            return cached

    # Cache miss — fetch from API
    logger.debug("Orders cache MISS: %s|%s — fetching from Toast API",
                 restaurant_guid[:8], business_date_yyyymmdd)
    _t0 = time.perf_counter()
    orders = await _fetch_orders_from_toast_api(client, restaurant_guid, business_date_yyyymmdd)
    logger.info("PROFILE day src=api_miss rguid=%s bd=%s secs=%.3f orders=%d",
                restaurant_guid[:8], business_date_yyyymmdd, time.perf_counter() - _t0, len(orders))

    # Opportunistically cache the result (historical date)
    try:
        toast_orders_db.store_orders(restaurant_guid, business_date_yyyymmdd, orders)
    except Exception as exc:
        logger.warning("Failed to cache orders for %s|%s: %s",
                       restaurant_guid[:8], business_date_yyyymmdd, exc)

    return orders


async def _fetch_orders_for_restaurant_date_range(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    start_date: date,
    end_date: date,
) -> List[Dict]:
    """
    Fetch all orders for one restaurant for each business day in [start_date, end_date].
    Uses Orders API businessDate filter (one request per day) so returned orders match
    the same business-day logic as the menu report (Toast uses closeoutHour for business day).
    Note: Order objects also have closedDate (when payment status became CLOSED) if needed later.
    """
    all_orders: List[Dict] = []
    d = start_date
    while d <= end_date:
        day_orders = await _fetch_orders_for_restaurant_day(
            client, restaurant_guid, to_business_date(d)
        )
        all_orders.extend(day_orders)
        d += timedelta(days=1)
        if d <= end_date:
            await asyncio.sleep(0.12)
    logger.info(
        "Pick3 orders fetch (by businessDate): restaurant=%s start=%s end=%s days=%s total_orders=%s",
        restaurant_guid[:8] + "...",
        start_date.isoformat(),
        end_date.isoformat(),
        (end_date - start_date).days + 1,
        len(all_orders),
    )
    return all_orders


async def _fetch_service_charge_configs(
    client: httpx.AsyncClient,
    restaurant_guid: str,
) -> Dict[str, Dict]:
    """
    Fetch and cache service charge configurations for one restaurant from Toast Config API.

    Returns dict of {sc_guid: {"destination": str, "gratuity": bool, "name": str}}
    Destination values: RESTAURANT, SERVER, TOAST, THIRD_PARTY

    Results are cached indefinitely per restaurant_guid (SC configs rarely change).
    On any error returns {} so callers can short-circuit without crashing.
    """
    global _sc_config_cache
    if restaurant_guid in _sc_config_cache:
        return _sc_config_cache[restaurant_guid]

    standard_headers = await get_standard_api_auth_headers()
    headers = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid,
    }
    url = f"{TOAST_STANDARD_API_BASE_URL}/config/v2/serviceCharges"
    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            logger.warning(
                "SC config fetch failed for restaurant %s: HTTP %s — SCs will be treated as 0",
                restaurant_guid[:8] + "...",
                resp.status_code,
            )
            _sc_config_cache[restaurant_guid] = {}
            return {}
        sc_list = resp.json()
        configs: Dict[str, Dict] = {}
        if isinstance(sc_list, list):
            for sc in sc_list:
                guid = sc.get("guid", "")
                if guid:
                    configs[guid] = {
                        "destination": sc.get("destination", "RESTAURANT"),
                        "gratuity": bool(sc.get("gratuity", False)),
                        "name": sc.get("name", ""),
                    }
        _sc_config_cache[restaurant_guid] = configs
        non_rest = [g for g, c in configs.items() if c.get("destination") != "RESTAURANT" and not c.get("gratuity")]
        logger.info(
            "SC configs cached for restaurant %s: %d total, %d non-restaurant",
            restaurant_guid[:8] + "...",
            len(configs),
            len(non_rest),
        )
        return configs
    except Exception as exc:
        logger.warning("SC config fetch error for %s: %s", restaurant_guid[:8] + "...", exc)
        _sc_config_cache[restaurant_guid] = {}
        return {}


async def _compute_restaurant_service_charges(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> float:
    """
    Compute total restaurant-destination service charges for one restaurant on one business day.

    Toast's netSalesAmount INCLUDES service charges that go to the restaurant (e.g. delivery
    fees, catering fees).  Newk's must pay those fees out to delivery providers, so they are
    NOT Newk's revenue.  We subtract them:

        Newk's Net Sales = Toast netSalesAmount − result of this function

    Charges included: ALL where destination == 'RESTAURANT', regardless of the gratuity flag.
    Toast includes every restaurant-destination charge in netSalesAmount, so every one must
    be removed to arrive at Newk's true revenue.
    Charges going to SERVER, TOAST, or THIRD_PARTY are never in Toast netSalesAmount
    and are therefore not included here.

    NOTE: destination is a top-level field on AppliedServiceCharge.  The nested
    serviceCharge object is a stub reference (guid + entityType only) with no other fields.
    """
    global _sc_daily_cache
    cache_key = f"{restaurant_guid}|{business_date_yyyymmdd}"
    if cache_key in _sc_daily_cache:
        cached_val = _sc_daily_cache[cache_key]
        logger.debug(
            "SC cache HIT for %s|%s: returning cached %.2f",
            restaurant_guid[:8] + "...",
            business_date_yyyymmdd,
            cached_val,
        )
        return cached_val

    # Fetch all orders for this business day
    orders = await _fetch_orders_for_restaurant_day(client, restaurant_guid, business_date_yyyymmdd)

    # Walk orders → checks → appliedServiceCharges and sum restaurant-destination SC amounts.
    total_restaurant_sc: float = 0.0
    dest_counts: Dict[str, int] = {}   # destination → charge count (for logging)
    for order in orders:
        if order.get("voided"):
            continue
        for check in order.get("checks", []):
            if check.get("voided"):
                continue
            for applied_sc in check.get("appliedServiceCharges", []):
                charge_amount = float(applied_sc.get("chargeAmount") or 0)
                if charge_amount <= 0:
                    continue

                # destination is a top-level field on AppliedServiceCharge (not on the stub
                # nested serviceCharge reference).  Default to RESTAURANT when absent.
                # Any charge going to the restaurant is included in Toast's netSalesAmount,
                # so we deduct ALL of them regardless of the gratuity flag.
                destination = applied_sc.get("destination") or "RESTAURANT"
                dest_counts[destination] = dest_counts.get(destination, 0) + 1

                if destination == "RESTAURANT":
                    total_restaurant_sc += charge_amount

    _sc_daily_cache[cache_key] = total_restaurant_sc
    logger.info(
        "SC computed for %s|%s: restaurant_sc=%.2f (from %d orders, dest breakdown=%s)",
        restaurant_guid[:8] + "...",
        business_date_yyyymmdd,
        total_restaurant_sc,
        len(orders),
        dest_counts,
    )
    return total_restaurant_sc


async def _compute_sales_metrics_for_day(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> Dict:
    """
    Fetch all orders for one restaurant and one business day and compute aggregate metrics.

    Returns a dict with:
        order_count      – count of non-voided orders
        restaurant_sc    – total RESTAURANT-destination service charges
                           (also populates _sc_daily_cache as a side effect)
        discount_amount  – sum of appliedDiscounts[].discountAmount on non-voided checks
        void_count       – count of voided checks
        cash_amount      – sum of CASH-type payments on non-voided checks
        card_amount      – sum of non-CASH payments on non-voided checks
    """
    global _sc_daily_cache
    orders = await _fetch_orders_for_restaurant_day(client, restaurant_guid, business_date_yyyymmdd)

    order_count: int = 0
    restaurant_sc: float = 0.0
    discount_amount: float = 0.0
    void_count: int = 0
    cash_amount: float = 0.0
    card_amount: float = 0.0

    # Catering / non-catering splits (based on check-level selection names)
    cat_order_count: int = 0
    cat_sc: float = 0.0
    cat_discount: float = 0.0
    cat_void: int = 0
    cat_cash: float = 0.0
    cat_card: float = 0.0

    def _check_is_catering(check: Dict) -> bool:
        for sel in check.get("selections", []):
            if sel.get("voided"):
                continue
            name = (sel.get("displayName") or (sel.get("item") or {}).get("name") or "").lower()
            if "(catering)" in name:
                return True
        return False

    for order in orders:
        if order.get("voided"):
            void_count += len(order.get("checks", []))
            continue

        order_count += 1
        for check in order.get("checks", []):
            if check.get("voided"):
                void_count += 1
                continue

            is_catering = _check_is_catering(check)
            if is_catering:
                cat_order_count += 1

            # Restaurant-destination service charges
            for sc in check.get("appliedServiceCharges", []):
                charge = float(sc.get("chargeAmount") or 0)
                if charge > 0 and (sc.get("destination") or "RESTAURANT") == "RESTAURANT":
                    restaurant_sc += charge
                    if is_catering:
                        cat_sc += charge

            # Discounts
            for disc in check.get("appliedDiscounts", []):
                amt = float(disc.get("discountAmount") or 0)
                discount_amount += amt
                if is_catering:
                    cat_discount += amt

            # Payments: CASH vs everything else (credit, gift card, etc.)
            for pmt in check.get("payments", []):
                pmt_amount = float(pmt.get("amount") or 0)
                if pmt_amount <= 0:
                    continue
                if (pmt.get("type") or "").upper() == "CASH":
                    cash_amount += pmt_amount
                    if is_catering:
                        cat_cash += pmt_amount
                else:
                    card_amount += pmt_amount
                    if is_catering:
                        cat_card += pmt_amount

    # Populate SC cache as side effect so the standard net-sales report benefits too
    cache_key = f"{restaurant_guid}|{business_date_yyyymmdd}"
    _sc_daily_cache[cache_key] = restaurant_sc

    return {
        "order_count":      order_count,
        "restaurant_sc":    restaurant_sc,
        "discount_amount":  discount_amount,
        "void_count":       void_count,
        "cash_amount":      cash_amount,
        "card_amount":      card_amount,
        # catering split
        "cat_order_count":  cat_order_count,
        "cat_sc":           cat_sc,
        "cat_discount":     cat_discount,
        "cat_void":         cat_void,
        "cat_cash":         cat_cash,
        "cat_card":         cat_card,
    }


def _get_modifier_guid_to_name(restaurant_guid: str) -> Dict[str, str]:
    """Return modifier option GUID -> display name from menu cache. Call get_menu_tags_mapping first to populate."""
    return (_menu_tags_cache.get(restaurant_guid) or {}).get("guid_to_name") or {}


# ------------------
# Menus API V3 - Tags Mapping
# ------------------

async def get_menu_tags_mapping(client: httpx.AsyncClient, restaurant_guid: str) -> Dict[str, List[Dict]]:
    """
    Get cached mapping of menuItemGuid -> [{tagGuid, tagName}, ...]
    Uses V3 metadata to check if menu changed
    """
    global _menu_tags_cache
    
    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }
    
    # Check metadata first
    metadata_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/metadata"
    metadata_resp = await client.get(metadata_url, headers=headers_with_guid)
    
    if metadata_resp.status_code != 200:
        # 403 is expected if V3 metadata isn't available - silently fall back to V2
        if metadata_resp.status_code != 403:
            logger.warning(f"Failed to fetch menu metadata: {metadata_resp.status_code}")
        # Fall back to V2 without caching
        return await get_menu_tags_mapping_v2(client, restaurant_guid)
    
    metadata = metadata_resp.json()
    current_hash = metadata.get("hash") or metadata.get("version") or str(metadata)
    logger.info(f"MENU TAGS PATH=V3  restaurant={restaurant_guid}  hash={str(current_hash)[:16]}")

    # Check cache
    if restaurant_guid in _menu_tags_cache:
        cached = _menu_tags_cache[restaurant_guid]
        if cached.get("hash") == current_hash:
            logger.info(f"Menu tags cache HIT for restaurant {restaurant_guid}")
            return cached["mapping"]

    logger.info(f"Menu tags cache MISS for restaurant {restaurant_guid}, fetching from API")
    
    # Fetch menus V3
    menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus"
    menus_resp = await client.get(menus_url, headers=headers_with_guid)
    
    if menus_resp.status_code != 200:
        logger.warning(f"Failed to fetch menus V3: {menus_resp.status_code}, falling back to V2")
        return await get_menu_tags_mapping_v2(client, restaurant_guid)
    
    menus_data = menus_resp.json()

    # Build mapping: menuItemGuid/modifierGuid -> [{tagGuid, tagName}, ...]
    mapping = {}
    # Parallel mapping: menuItemGuid/modifierGuid -> int PLU. Same source-of-
    # truth concerns as tags — we want PLU live from Toast, not stale Excel.
    plu_mapping: Dict[str, int] = {}

    def _coerce_plu(v) -> int:
        if v is None or v == "":
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

    # Process menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                if not item_guid:
                    continue

                tags = []
                for tag in item.get("itemTags", []):
                    tag_guid = tag.get("guid", "")
                    tag_name = tag.get("name", "")
                    if tag_name:  # Only include if name exists
                        tags.append({
                            "tagGuid": tag_guid,
                            "tagName": tag_name
                        })

                if tags:
                    mapping[item_guid] = tags

                _plu = _coerce_plu(item.get("plu"))
                if _plu:
                    plu_mapping[item_guid] = _plu
    
    # Process modifiers from modifierGroupReferences and modifierOptionReferences
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    
    logger.debug(f"DEBUG (V3): Found {len(modifier_group_refs)} modifier groups and {len(modifier_option_refs)} modifier option references")
    
    all_modifier_guids_from_menus = set()
    # Process modifier groups
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if not mod_option_guid:
                continue
            
            all_modifier_guids_from_menus.add(mod_option_guid)
            
            # Debug: Check for target modifier
            if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
                logger.debug(f"DEBUG (V3): Found target modifier in modifierGroupReferences - guid={mod_option_guid}, name='{mod_option_name}', group_guid={mod_group_guid}")
            
            tags = []
            for tag in mod_option.get("itemTags", []):
                tag_guid = tag.get("guid", "")
                tag_name = tag.get("name", "")
                if tag_name:
                    tags.append({
                        "tagGuid": tag_guid,
                        "tagName": tag_name
                    })
            
            # Include in mapping even if no tags (for GUID lookup)
            if tags:
                mapping[mod_option_guid] = tags
            else:
                mapping[mod_option_guid] = []

            _plu = _coerce_plu(mod_option.get("plu"))
            if _plu:
                plu_mapping[mod_option_guid] = _plu

    # Process modifier option references directly
    # NOTE: modifierOptionReferences keys are numeric IDs, but each option has a "guid" field with the actual UUID
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log structure for target modifier
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.debug(f"DEBUG (V3): Modifier option structure - id={mod_option_id}, keys={list(mod_option.keys())}, guid={mod_option_guid}")
            # Check for alternative GUID fields
            for key in ["guid", "modifierOptionGuid", "modifierGuid", "id"]:
                if key in mod_option:
                    logger.debug(f"DEBUG (V3):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            # If no guid field, use the key as fallback (might be numeric ID)
            mod_option_guid = str(mod_option_id)
        
        all_modifier_guids_from_menus.add(mod_option_guid)
        
        # Debug: Check for target modifier
        if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
            logger.debug(f"DEBUG (V3): Found target modifier in modifierOptionReferences - id={mod_option_id}, guid={mod_option_guid}, name='{mod_option_name}'")

        tags = []
        for tag in mod_option.get("itemTags", []):
            tag_guid = tag.get("guid", "")
            tag_name = tag.get("name", "")
            if tag_name:
                tags.append({
                    "tagGuid": tag_guid,
                    "tagName": tag_name
                })

        # Include in mapping even if no tags (for GUID lookup)
        # Use the actual GUID, not the numeric ID key
        if tags:
            mapping[mod_option_guid] = tags
        else:
            mapping[mod_option_guid] = []

        _plu = _coerce_plu(mod_option.get("plu"))
        if _plu:
            plu_mapping[mod_option_guid] = _plu

    # Debug: Check if target GUID is in our mapping
    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_guid in mapping:
        logger.debug(f"DEBUG (V3): Target GUID {target_guid} IS in mapping with {len(mapping[target_guid])} tags")
    else:
        logger.debug(f"DEBUG (V3): Target GUID {target_guid} NOT in mapping. Total modifier GUIDs from Menus API: {len(all_modifier_guids_from_menus)}")
        # Show some sample GUIDs
        sample_guids = list(all_modifier_guids_from_menus)[:10]
        logger.debug(f"DEBUG (V3): Sample modifier GUIDs from Menus API: {sample_guids}")
    
    # Cache it
    _menu_tags_cache[restaurant_guid] = {
        "hash": current_hash,
        "mapping": mapping,
        "plu_mapping": plu_mapping,
    }

    # Opportunistic write to toast_config.db (non-blocking, best-effort)
    try:
        toast_config_builder.merge_menu_config_from_api(menus_data, restaurant_guid)
    except Exception as exc:
        logger.debug(f"Opportunistic toast_config write failed for {restaurant_guid}: {exc}")

    # Also build a name-to-GUID mapping for modifiers (fallback matching)
    modifier_name_to_guid = {}
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                name_key = mod_option_name.lower().strip()
                modifier_name_to_guid[name_key] = mod_option_guid
                # Debug: log if this is our target modifier
                if "half black" in name_key and "bleu" in name_key:
                    logger.info(f"DEBUG: Found target modifier in Menus API (V3) - name='{mod_option_name}', guid={mod_option_guid}, normalized='{name_key}'")
    
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        if not mod_option_guid:
            mod_option_guid = str(mod_option_id)  # Fallback to ID if no guid
        
        if mod_option_guid and mod_option_name:
            name_key = mod_option_name.lower().strip()
            modifier_name_to_guid[name_key] = mod_option_guid
            # Debug: log if this is our target modifier
            if "half black" in name_key and "bleu" in name_key:
                logger.info(f"DEBUG: Found target modifier in Menus API (V3 option refs) - name='{mod_option_name}', id={mod_option_id}, guid={mod_option_guid}, normalized='{name_key}'")
    
    logger.info(f"Built menu tags mapping: {len(mapping)} items/modifiers with tags")
    logger.info(f"Built modifier name mapping: {len(modifier_name_to_guid)} modifier names")
    
    # Debug: Check if target modifier is in name mapping
    target_name_variations = [
        "half black & bleu (olo pairing choice)",
        "half black & bleu",
        "half black and bleu"
    ]
    for var in target_name_variations:
        if var in modifier_name_to_guid:
            logger.info(f"DEBUG: Target modifier name variation '{var}' found in name mapping -> {modifier_name_to_guid[var]}")
    
    # Build GUID -> name mapping for both menu items and modifiers
    guid_to_name = {}
    # Add menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                # Prefer displayName over name (name might be kitchenName)
                item_name = item.get("displayName") or item.get("name", "")
                # If name exists but displayName doesn't, check if name is actually kitchenName
                if not item.get("displayName") and item.get("name") and item.get("kitchenName"):
                    # If name matches kitchenName, prefer name (it's the display name)
                    # Otherwise, use name (it's the display name)
                    item_name = item.get("name", "")
                if item_guid and item_name:
                    guid_to_name[item_guid] = item_name
    # Add modifiers
    for mod_group_guid, mod_group in modifier_group_refs.items():
        for mod_option in mod_group.get("modifierOptions", []):
            mod_option_guid = mod_option.get("guid")
            # Prefer displayName over name for modifiers too
            mod_option_name = mod_option.get("displayName") or mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                guid_to_name[mod_option_guid] = mod_option_name
    # Also check modifierOptionReferences
    for mod_option_guid, mod_option in modifier_option_refs.items():
        # Prefer displayName over name for modifiers too
        mod_option_name = mod_option.get("displayName") or mod_option.get("name", "")
        if mod_option_guid and mod_option_name:
            guid_to_name[mod_option_guid] = mod_option_name
    
    # Store name mappings in cache for later use
    if restaurant_guid in _menu_tags_cache:
        _menu_tags_cache[restaurant_guid]["name_mapping"] = modifier_name_to_guid  # Keep reverse mapping for fuzzy matching
        _menu_tags_cache[restaurant_guid]["guid_to_name"] = guid_to_name  # Add GUID -> name mapping
    
    return mapping

async def get_menu_tags_mapping_v2(client: httpx.AsyncClient, restaurant_guid: str) -> Dict[str, List[Dict]]:
    """Fallback to V2 menus API if V3 not available.

    Cached per-restaurant in _menu_tags_cache with a TTL. V2 has no hash/version
    to invalidate on, so we use a wall-clock TTL of 1 hour — long enough that
    typical report sessions hit cache, short enough that Toastweb tag edits
    propagate without a restart. Force invalidation by restarting uvicorn (the
    cache is in-process only).
    """
    global _menu_tags_cache

    # Cache check — V2 entries use a separate key from the V3 hash so a future
    # V3-entitled response wouldn't conflict with stale V2 data.
    _V2_TTL_SECONDS = 60 * 60  # 1 hour
    cached = _menu_tags_cache.get(restaurant_guid)
    if cached and "v2_mapping" in cached:
        age = time.time() - cached.get("v2_fetched_at", 0)
        if age < _V2_TTL_SECONDS:
            return cached["v2_mapping"]

    logger.info(f"MENU TAGS PATH=V2  restaurant={restaurant_guid}  (V3 not entitled or errored)")
    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }

    menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
    menus_resp = await client.get(menus_url, headers=headers_with_guid)

    if menus_resp.status_code != 200:
        raise HTTPException(status_code=menus_resp.status_code, detail="Failed to fetch menu data")

    menus_data = menus_resp.json()
    
    mapping = {}
    
    # Process menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                if not item_guid:
                    continue
                
                tags = []
                for tag in item.get("itemTags", []):
                    tag_guid = tag.get("guid", "")
                    tag_name = tag.get("name", "")
                    if tag_name:
                        tags.append({
                            "tagGuid": tag_guid,
                            "tagName": tag_name
                        })
                
                if tags:
                    mapping[item_guid] = tags
    
    # Process modifiers from modifierGroupReferences and modifierOptionReferences
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    
    logger.debug(f"DEBUG (V2): Found {len(modifier_group_refs)} modifier groups and {len(modifier_option_refs)} modifier option references")
    
    all_modifier_guids_from_menus = set()
    # Process modifier groups
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            if not mod_option_guid:
                continue
            
            tags = []
            for tag in mod_option.get("itemTags", []):
                tag_guid = tag.get("guid", "")
                tag_name = tag.get("name", "")
                if tag_name:
                    tags.append({
                        "tagGuid": tag_guid,
                        "tagName": tag_name
                    })
            
            if tags:
                mapping[mod_option_guid] = tags
    
    # Process modifier option references directly
    # NOTE: modifierOptionReferences keys are numeric IDs, but each option has a "guid" field with the actual UUID
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log structure for target modifier
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.debug(f"DEBUG (V2): Modifier option structure - id={mod_option_id}, keys={list(mod_option.keys())}, guid={mod_option_guid}")
            # Check for alternative GUID fields
            for key in ["guid", "modifierOptionGuid", "modifierGuid", "id", "externalId"]:
                if key in mod_option:
                    logger.debug(f"DEBUG (V2):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            # If no guid field, use the key as fallback (might be numeric ID)
            mod_option_guid = str(mod_option_id)
        
        all_modifier_guids_from_menus.add(mod_option_guid)
        
        # Debug: Check for target modifier
        if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
            logger.debug(f"DEBUG (V2): Found target modifier in modifierOptionReferences - id={mod_option_id}, guid={mod_option_guid}, name='{mod_option_name}'")
        
        tags = []
        for tag in mod_option.get("itemTags", []):
            tag_guid = tag.get("guid", "")
            tag_name = tag.get("name", "")
            if tag_name:
                tags.append({
                    "tagGuid": tag_guid,
                    "tagName": tag_name
                })
        
        # Include in mapping even if no tags (for GUID lookup)
        # Use the actual GUID, not the numeric ID key
        if tags:
            mapping[mod_option_guid] = tags
        else:
            mapping[mod_option_guid] = []
    
    # Debug: Check if target GUID is in our mapping
    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_guid in mapping:
        logger.debug(f"DEBUG (V2): Target GUID {target_guid} IS in mapping with {len(mapping[target_guid])} tags")
    else:
        logger.debug(f"DEBUG (V2): Target GUID {target_guid} NOT in mapping. Total modifier GUIDs from Menus API: {len(all_modifier_guids_from_menus)}")
        # Show some sample GUIDs
        sample_guids = list(all_modifier_guids_from_menus)[:10]
        logger.debug(f"DEBUG (V2): Sample modifier GUIDs from Menus API: {sample_guids}")
    
    # Also build a name-to-GUID mapping for modifiers (fallback matching)
    modifier_name_to_guid = {}
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                modifier_name_to_guid[mod_option_name.lower().strip()] = mod_option_guid
    
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log full structure for target modifier to find UUID
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.debug(f"DEBUG (V2 name mapping): Modifier option full structure - id={mod_option_id}")
            logger.debug(f"DEBUG (V2 name mapping):   All keys: {list(mod_option.keys())}")
            for key in mod_option.keys():
                logger.debug(f"DEBUG (V2 name mapping):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            mod_option_guid = str(mod_option_id)  # Fallback to ID if no guid
        
        if mod_option_guid and mod_option_name:
            name_key = mod_option_name.lower().strip()
            modifier_name_to_guid[name_key] = mod_option_guid
            # Debug: log if this is our target modifier
            if "half black" in name_key and "bleu" in name_key:
                logger.debug(f"DEBUG (V2 name mapping): Added to name mapping - name='{mod_option_name}', normalized='{name_key}', guid={mod_option_guid}")
    
    logger.info(f"Built modifier name mapping (V2): {len(modifier_name_to_guid)} modifier names")
    
    # Build GUID -> name mapping for both menu items and modifiers (V2)
    guid_to_name = {}
    # Add menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                # Prefer displayName over name (name might be kitchenName)
                item_name = item.get("displayName") or item.get("name", "")
                if item_guid and item_name:
                    guid_to_name[item_guid] = item_name
    # Add modifiers from modifierGroupReferences
    for mod_group_guid, mod_group in modifier_group_refs.items():
        for mod_option in mod_group.get("modifierOptions", []):
            mod_option_guid = mod_option.get("guid")
            # Prefer displayName over name for modifiers too
            mod_option_name = mod_option.get("displayName") or mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                guid_to_name[mod_option_guid] = mod_option_name
    # Add modifiers from modifierOptionReferences
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")
        # Prefer displayName over name for modifiers too
        mod_option_name = mod_option.get("displayName") or mod_option.get("name", "")
        if mod_option_guid and mod_option_name:
            guid_to_name[mod_option_guid] = mod_option_name
    
    # Store name mappings in cache for later use (V2)
    if restaurant_guid not in _menu_tags_cache:
        _menu_tags_cache[restaurant_guid] = {}
    _menu_tags_cache[restaurant_guid]["name_mapping"] = modifier_name_to_guid  # Keep reverse mapping for fuzzy matching
    _menu_tags_cache[restaurant_guid]["guid_to_name"] = guid_to_name  # Add GUID -> name mapping
    # Cache the tag mapping itself so subsequent reports skip the V2 API call.
    # See cache check at the top of this function for TTL details.
    _menu_tags_cache[restaurant_guid]["v2_mapping"] = mapping
    _menu_tags_cache[restaurant_guid]["v2_fetched_at"] = time.time()

    return mapping


async def fetch_toast_menus(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    force_refresh: bool = False
) -> Dict:
    """Fetch menus from Toast V2 (no V3 dependency)."""
    return await fetch_toast_menus_v2(
        client,
        restaurant_guid,
        force_refresh=force_refresh
    )


async def fetch_price_groups(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    force_refresh: bool = False
) -> Dict[str, str]:
    """Fetch price groups (location pricing tiers) and return guid->name map."""
    now = time.time()
    cache_entry = _price_group_cache.get(restaurant_guid)
    if cache_entry and not force_refresh and cache_entry.get("expires_at", 0) > now:
        return cache_entry.get("data", {})

    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }

    url = f"{TOAST_STANDARD_API_BASE_URL}/config/v2/priceGroups"
    try:
        resp = await client.get(url, headers=headers_with_guid)
    except Exception:
        return {}

    if resp.status_code == 429:
        logger.warning("Toast rate limit hit when fetching price groups.")
        if cache_entry and cache_entry.get("data"):
            return cache_entry.get("data", {})
        return {}

    if resp.status_code != 200:
        return {}

    data = resp.json()
    if isinstance(data, dict) and "priceGroups" in data:
        price_groups = data.get("priceGroups", [])
    elif isinstance(data, list):
        price_groups = data
    else:
        price_groups = []

    mapping = {}
    for group in price_groups:
        if not isinstance(group, dict):
            continue
        guid = group.get("guid") or group.get("priceGroupGuid")
        name = group.get("name") or group.get("priceGroupName")
        if guid and name:
            mapping[str(guid)] = str(name)

    _price_group_cache[restaurant_guid] = {
        "data": mapping,
        "expires_at": now + PRICE_GROUP_CACHE_TTL_SECONDS
    }
    return mapping


async def fetch_toast_menus_v2(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    force_refresh: bool = False,
    max_attempts: int = 5
) -> Dict:
    """Fetch menus from Toast V2 endpoint with rate-limit handling."""
    now = time.time()
    cache_entry = _menu_data_cache_v2.get(restaurant_guid)
    if cache_entry and not force_refresh and cache_entry.get("expires_at", 0) > now:
        return cache_entry.get("data", {})

    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }

    url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
    delay_seconds = 1.5

    for _ in range(max_attempts):
        resp = await client.get(url, headers=headers_with_guid)
        if resp.status_code == 200:
            menus_data = resp.json()
            _menu_data_cache_v2[restaurant_guid] = {
                "data": menus_data,
                "expires_at": now + MENU_V2_CACHE_TTL_SECONDS
            }
            return menus_data

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_seconds = delay_seconds
            if retry_after:
                try:
                    wait_seconds = max(float(retry_after), wait_seconds)
                except ValueError:
                    pass
            await asyncio.sleep(wait_seconds)
            delay_seconds = min(delay_seconds * 1.8, 10.0)
            continue

        raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch menu data: {resp.text[:200]}")

    raise HTTPException(status_code=429, detail="Toast API rate limit exceeded while fetching menu data.")


def _collect_menu_items(menu_data: Dict) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    def walk_groups(groups: List[Dict], menu_name: str) -> None:
        for group in groups or []:
            group_name = group.get("name") or "Unnamed Group"
            for item in group.get("menuItems", []):
                rows.append({
                    "menu": menu_name,
                    "menu_group": group_name,
                    "item": item.get("displayName") or item.get("name") or "Unnamed Item",
                    "guid": item.get("guid") or ""
                })
            nested_groups = group.get("menuGroups") or []
            if nested_groups:
                walk_groups(nested_groups, menu_name)

    for menu in menu_data.get("menus", []):
        menu_name = menu.get("name") or "Unnamed Menu"
        walk_groups(menu.get("menuGroups", []), menu_name)

    return rows


def load_price_tier_mapping() -> List[Tuple[str, List[str]]]:
    """Load tier mapping file with TierName + location identifiers."""
    if not os.path.exists(PRICE_TIER_MAPPING_FILE):
        raise HTTPException(status_code=400, detail=f"Missing price tier mapping file: {PRICE_TIER_MAPPING_FILE}")

    ext = os.path.splitext(PRICE_TIER_MAPPING_FILE)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df = pd.read_excel(PRICE_TIER_MAPPING_FILE)
    else:
        df = pd.read_csv(PRICE_TIER_MAPPING_FILE)

    if df.shape[1] < 2:
        raise HTTPException(status_code=400, detail="Price tier mapping file must have at least two columns")

    columns = list(df.columns)
    lower_cols = [str(c).strip().lower() for c in columns]
    tier_col = next((columns[i] for i, c in enumerate(lower_cols) if "tier" in c or "group" in c), columns[0])
    stores_col = next(
        (columns[i] for i, c in enumerate(lower_cols) if "location" in c or "store" in c or "guid" in c),
        columns[1]
    )

    mapping: Dict[str, List[str]] = {}
    current_tier: Optional[str] = None

    for _, row in df.iterrows():
        tier_raw = row.get(tier_col)
        tier_name = str(tier_raw).strip() if tier_raw is not None and str(tier_raw).strip() else ""
        if tier_name:
            current_tier = tier_name
        if not current_tier:
            continue

        raw_value = row.get(stores_col)
        if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
            continue

        identifiers: List[str] = []
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            if float(raw_value).is_integer():
                identifiers = [str(int(raw_value))]
            else:
                identifiers = [str(raw_value)]
        else:
            raw_text = str(raw_value).strip()
            if raw_text == "-" or raw_text == "":
                identifiers = []
            else:
                identifiers = [s.strip() for s in re.split(r"[,\n;]+", raw_text) if s.strip()]

        if identifiers:
            mapping.setdefault(current_tier, []).extend(identifiers)
        else:
            mapping.setdefault(current_tier, [])

    normalized: List[Tuple[str, List[str]]] = []
    for tier_name, values in mapping.items():
        deduped = []
        seen = set()
        for value in values:
            key = str(value).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        normalized.append((tier_name, deduped))

    return normalized


def extract_store_numbers_from_location(location: Location) -> List[str]:
    candidates: List[str] = []
    if location.externalId:
        candidates.append(str(location.externalId).strip())
    if location.restaurantName:
        candidates.extend(re.findall(r"\b\d{3,5}\b", location.restaurantName))
    return list({c for c in candidates if c})


def format_location_display_name(location: Location) -> str:
    """Return display name including 4-digit store number when available, e.g. 'Store Name (1234)'."""
    name = (location.restaurantName or "").strip() or "Unknown"
    store_num: Optional[str] = None
    if location.externalId:
        eid = str(location.externalId).strip()
        if len(eid) == 4 and eid.isdigit():
            store_num = eid
    if not store_num and location.restaurantName:
        four_digit = re.findall(r"\b\d{4}\b", location.restaurantName)
        if four_digit:
            store_num = four_digit[0]
    if not store_num:
        candidates = extract_store_numbers_from_location(location)
        if candidates:
            store_num = candidates[0]
    if store_num and not (name.endswith(f" ({store_num})")):
        return f"{name} ({store_num})"
    return name


def build_tier_location_map(
    tiers: List[Tuple[str, List[str]]],
    locations: List[Location]
) -> Tuple[Dict[str, List[Location]], Dict[str, List[str]]]:
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
    store_to_locations: Dict[str, List[Location]] = {}
    for loc in locations:
        for store_num in extract_store_numbers_from_location(loc):
            store_to_locations.setdefault(store_num, []).append(loc)

    guid_to_location: Dict[str, Location] = {loc.restaurantGuid: loc for loc in locations}
    for loc in locations:
        if loc.externalId:
            guid_to_location.setdefault(str(loc.externalId), loc)

    tier_locations: Dict[str, List[Location]] = {}
    tier_missing: Dict[str, List[str]] = {}
    for tier_name, store_nums in tiers:
        matched: List[Location] = []
        missing: List[str] = []
        for store_num in store_nums:
            if uuid_re.match(store_num):
                loc = guid_to_location.get(store_num)
                if loc:
                    matched.append(loc)
                else:
                    missing.append(store_num)
            else:
                locs = store_to_locations.get(store_num)
                if locs:
                    matched.extend(locs)
                else:
                    missing.append(store_num)
        deduped: Dict[str, Location] = {loc.restaurantGuid: loc for loc in matched}
        tier_locations[tier_name] = list(deduped.values())
        if missing:
            tier_missing[tier_name] = missing

    return tier_locations, tier_missing


def format_tier_price(prices: List[Optional[float]]) -> str:
    clean = [p for p in prices if p is not None]
    if not clean:
        return ""
    unique = sorted({round(float(p), 2) for p in clean})
    if len(unique) == 1:
        return _format_price_value(unique[0])
    return f"MIXED ({_format_price_value(unique[0])}-{_format_price_value(unique[-1])}, n={len(unique)})"


def find_item_match(base_rows: List[Dict[str, str]], needle: str) -> Tuple[Optional[Dict[str, str]], List[Dict[str, str]]]:
    cleaned = (needle or "").strip().lower()
    if not cleaned:
        return None, []
    exact = next((row for row in base_rows if str(row.get("guid") or "").lower() == cleaned), None)
    if exact:
        return exact, [exact]
    matches = [row for row in base_rows if cleaned in str(row.get("item") or "").lower()]
    return (matches[0] if matches else None), matches


def compute_store_tier_duplicates(tiers: List[Tuple[str, List[str]]]) -> Dict[str, List[str]]:
    store_to_tiers: Dict[str, List[str]] = {}
    for tier_name, store_nums in tiers:
        for store_num in store_nums:
            store_to_tiers.setdefault(store_num, []).append(tier_name)
    return {store: tier_list for store, tier_list in store_to_tiers.items() if len(tier_list) > 1}


def _format_price_value(value: Optional[float]) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _extract_price_tiers(item: Dict) -> List[Dict[str, Optional[float]]]:
    tiers: List[Dict[str, Optional[float]]] = []

    def add_tier(name: str, price: Optional[float]) -> None:
        if not name and price is None:
            return
        for existing in tiers:
            if existing.get("name") == name and existing.get("price") == price:
                return
        tiers.append({"name": name, "price": price})

    base_price = item.get("price")
    if base_price is not None:
        add_tier("Base Price", base_price)

    for key in [
        "priceGroups",
        "priceGroupPrices",
        "priceLevels",
        "pricesByPriceGroup",
        "priceByPriceGroup",
        "priceGroupsByGuid"
    ]:
        if key not in item:
            continue
        container = item.get(key)
        if isinstance(container, list):
            for entry in container:
                if not isinstance(entry, dict):
                    continue
                name = (
                    entry.get("name")
                    or entry.get("priceGroupName")
                    or entry.get("priceLevelName")
                    or entry.get("priceGroup")
                    or entry.get("priceLevel")
                    or entry.get("priceGroupGuid")
                    or entry.get("guid")
                    or ""
                )
                price = entry.get("price")
                if price is None:
                    price = entry.get("amount") or entry.get("value") or entry.get("basePrice")
                add_tier(str(name), price)
        elif isinstance(container, dict):
            for entry_key, entry_value in container.items():
                if isinstance(entry_value, dict):
                    name = (
                        entry_value.get("name")
                        or entry_value.get("priceGroupName")
                        or entry_value.get("priceLevelName")
                        or entry_key
                    )
                    price = entry_value.get("price")
                    if price is None:
                        price = entry_value.get("amount") or entry_value.get("value") or entry_value.get("basePrice")
                    add_tier(str(name), price)
                else:
                    add_tier(str(entry_key), entry_value)

    if not tiers and item.get("pricingStrategy"):
        add_tier(item.get("pricingStrategy", "").replace("_", " ").title(), None)

    return tiers


def _extract_location_prices(
    item: Dict,
    location_name_by_guid: Optional[Dict[str, str]] = None,
    price_group_name_by_guid: Optional[Dict[str, str]] = None
) -> Dict[str, Optional[float]]:
    location_name_by_guid = location_name_by_guid or {}
    price_group_name_by_guid = price_group_name_by_guid or {}
    prices: Dict[str, Optional[float]] = {}

    def add_price(name: str, price: Optional[float]) -> None:
        if not name:
            return
        if name not in prices:
            prices[name] = price

    containers = []
    for key in [
        "locationPrices",
        "locationSpecificPrices",
        "pricesByLocation",
        "locationPriceOverrides",
        "locationPriceMap",
        "priceGroupPrices",
        "priceGroups",
        "pricesByPriceGroup",
        "priceByPriceGroup",
        "priceGroupsByGuid"
    ]:
        if key in item:
            containers.append(item.get(key))

    for container in containers:
        if isinstance(container, list):
            for entry in container:
                if not isinstance(entry, dict):
                    continue
                price = entry.get("price")
                if price is None:
                    price = entry.get("amount") or entry.get("value") or entry.get("basePrice")
                name = (
                    entry.get("locationName")
                    or entry.get("locationGroupName")
                    or entry.get("priceGroupName")
                    or entry.get("restaurantName")
                    or entry.get("groupName")
                    or entry.get("name")
                )
                guid = (
                    entry.get("locationGuid")
                    or entry.get("locationId")
                    or entry.get("restaurantGuid")
                    or entry.get("restaurantId")
                    or entry.get("groupGuid")
                    or entry.get("priceGroupGuid")
                    or entry.get("guid")
                )
                if not name and guid:
                    name = location_name_by_guid.get(str(guid)) or price_group_name_by_guid.get(str(guid), str(guid))
                if name:
                    add_price(str(name), price)
        elif isinstance(container, dict):
            for entry_key, entry_value in container.items():
                name = None
                price = None
                if isinstance(entry_value, dict):
                    name = (
                        entry_value.get("locationName")
                        or entry_value.get("locationGroupName")
                        or entry_value.get("priceGroupName")
                        or entry_value.get("restaurantName")
                        or entry_value.get("groupName")
                        or entry_value.get("name")
                        or entry_key
                    )
                    price = entry_value.get("price")
                    if price is None:
                        price = entry_value.get("amount") or entry_value.get("value") or entry_value.get("basePrice")
                    guid = (
                        entry_value.get("locationGuid")
                        or entry_value.get("locationId")
                        or entry_value.get("restaurantGuid")
                        or entry_value.get("restaurantId")
                        or entry_value.get("groupGuid")
                        or entry_value.get("priceGroupGuid")
                        or entry_value.get("guid")
                    )
                    if not name and guid:
                        name = location_name_by_guid.get(str(guid)) or price_group_name_by_guid.get(str(guid), str(guid))
                else:
                    name = entry_key
                    price = entry_value
                if name:
                    add_price(str(name), price)

    return prices

def aggregate_quantities_by_tag(
    analytics_rows: List[dict],
    tags_mapping: Dict[str, List[Dict]],
    attribution_mode: str = "MULTI_COUNT"
) -> List[dict]:
    """
    Aggregate analytics rows by tag
    
    attribution_mode:
    - MULTI_COUNT: each tag gets full quantity
    - SPLIT: divide quantity evenly across tags
    - PRIMARY_ONLY: use only first tag
    """
    aggregation = {}
    untagged_count = 0
    
    for row in analytics_rows:
        restaurant_guid = row.get("restaurantGuid", "")
        restaurant_name = row.get("restaurantName", "Unknown")
        business_date = row.get("businessDate", "")
        menu_item_guid = row.get("menuItemGuid", "")
        quantity_sold = row.get("quantitySold", 0)
        
        if not menu_item_guid or menu_item_guid not in tags_mapping:
            # Untagged item
            untagged_count += 1
            key = f"{restaurant_guid}|{business_date}|UNTAGGED"
            
            if key not in aggregation:
                aggregation[key] = {
                    "restaurantGuid": restaurant_guid,
                    "restaurantName": restaurant_name,
                    "businessDate": business_date,
                    "tagGuid": "UNTAGGED",
                    "tagName": "Untagged",
                    "quantitySold": 0
                }
            
            aggregation[key]["quantitySold"] += quantity_sold
            continue
        
        # Get tags for this item
        tags = tags_mapping[menu_item_guid]
        
        if attribution_mode == "PRIMARY_ONLY":
            tags = tags[:1]  # Only first tag
        
        # Calculate quantity per tag
        if attribution_mode == "SPLIT" and len(tags) > 1:
            qty_per_tag = quantity_sold / len(tags)
        else:
            qty_per_tag = quantity_sold
        
        for tag in tags:
            tag_guid = tag["tagGuid"]
            tag_name = tag["tagName"]
            
            key = f"{restaurant_guid}|{business_date}|{tag_guid}"
            
            if key not in aggregation:
                aggregation[key] = {
                    "restaurantGuid": restaurant_guid,
                    "restaurantName": restaurant_name,
                    "businessDate": business_date,
                    "tagGuid": tag_guid,
                    "tagName": tag_name,
                    "quantitySold": 0
                }
            
            aggregation[key]["quantitySold"] += qty_per_tag
    
    logger.info(f"Aggregation complete: {len(aggregation)} tag groups, {untagged_count} untagged items")
    
    return list(aggregation.values())

# ------------------
# User Storage & Authentication
# ------------------

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

def load_users() -> Dict[str, dict]:
    """Load users from JSON file"""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading users: {e}")
        return {}

def save_users(users: Dict[str, dict]) -> None:
    """Save users to JSON file"""
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving users: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save users: {str(e)}")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt directly"""
    # Password should already be truncated, but double-check to be safe
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password = truncate_password_to_72_bytes(password)
        password_bytes = password.encode('utf-8')
    
    # Use bcrypt directly
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password using bcrypt directly"""
    # Truncate if needed (same as hash_password)
    plain_password = truncate_password_to_72_bytes(plain_password)
    password_bytes = plain_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hashed_bytes)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    """Get current authenticated user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    users = load_users()
    user_data = users.get(user_id)
    if user_data is None:
        raise credentials_exception
    
    return User(**{k: v for k, v in user_data.items() if k != "password"})

async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Get current user and verify they are an admin"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# Optional auth — does not raise 401 when no token is provided
security_optional = HTTPBearer(auto_error=False)

async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_optional)
) -> Optional[User]:
    """Return the authenticated User if a valid Bearer token is present; otherwise None."""
    if not credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            return None
        users = load_users()
        user_data = users.get(user_id)
        if not user_data:
            return None
        return User(**{k: v for k, v in user_data.items() if k != "password"})
    except Exception:
        return None

# ------------------
# Initialize Super Admin (if configured)
# ------------------

def initialize_super_admin():
    """Create super admin user if configured and doesn't exist"""
    if not SUPER_ADMIN_EMAIL or not SUPER_ADMIN_PASSWORD:
        return
    
    users = load_users()
    
    # Check if admin already exists
    for user_id, user in users.items():
        if user.get("email", "").lower() == SUPER_ADMIN_EMAIL:
            # Update existing admin if password changed
            if SUPER_ADMIN_PASSWORD:
                user["password"] = hash_password(truncate_password_to_72_bytes(SUPER_ADMIN_PASSWORD))
                user["role"] = "admin"
                users[user_id] = user
                save_users(users)
                logger.info(f"Super admin updated: {SUPER_ADMIN_EMAIL}")
            return
    
    # Create new super admin
    user_id = str(uuid.uuid4())
    hashed_password = hash_password(truncate_password_to_72_bytes(SUPER_ADMIN_PASSWORD))
    
    admin_user = {
        "id": user_id,
        "email": SUPER_ADMIN_EMAIL,
        "name": SUPER_ADMIN_NAME,
        "password": hashed_password,
        "createdAt": datetime.now().isoformat(),
        "lastLogin": None,
        "preferences": {},
        "role": "admin"
    }
    
    users[user_id] = admin_user
    save_users(users)
    logger.info(f"Super admin created: {SUPER_ADMIN_EMAIL}")

# Note: initialize_super_admin() is called at the end of the file
# after truncate_password_to_72_bytes is defined

# ------------------
# Saved Reports Storage
# ------------------

SAVED_REPORTS_FILE = os.path.join(os.path.dirname(__file__), "saved_reports.json")
STORE_GROUPS_FILE = os.path.join(os.path.dirname(__file__), "store_groups.json")

def load_saved_reports() -> List[SavedReport]:
    if not os.path.exists(SAVED_REPORTS_FILE):
        default_report = SavedReport(
            id=str(uuid.uuid4()),
            name="Net Sales by Location - Weekly",
            description="Weekly net sales breakdown by location",
            createdBy="System",
            createdAt=datetime.now().isoformat(),
            lastModified=datetime.now().isoformat(),
            config=ReportConfig(
                reportType="metrics",
                dateRangeType="last_7_days",
                restaurantIds=[],
                groupBy=[]
            )
        )
        save_saved_reports([default_report])
        return [default_report]
    
    try:
        with open(SAVED_REPORTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return [SavedReport(**item) for item in data]
    except Exception as e:
        logger.error(f"Error loading saved reports: {e}")
        return []

def save_saved_reports(reports: List[SavedReport]) -> None:
    try:
        with open(SAVED_REPORTS_FILE, 'w', encoding='utf-8') as f:
            json.dump([r.dict() for r in reports], f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving reports: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save reports: {str(e)}")

def load_store_groups() -> List[StoreGroup]:
    if not os.path.exists(STORE_GROUPS_FILE):
        return []
    
    try:
        with open(STORE_GROUPS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return [StoreGroup(**item) for item in data]
    except Exception as e:
        logger.error(f"Error loading store groups: {e}")
        return []

def save_store_groups(groups: List[StoreGroup]) -> None:
    try:
        with open(STORE_GROUPS_FILE, 'w', encoding='utf-8') as f:
            json.dump([g.dict() for g in groups], f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving store groups: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save store groups: {str(e)}")

# ------------------
# Report Data Transformation
# ------------------

def pivot_metrics_by_location_and_date(
    raw_rows: list,
    service_charges: Optional[Dict[str, float]] = None,
) -> dict:
    """
    Transform metrics data into pivot table format:
    - Rows: Restaurant locations
    - Columns: Business dates (Toast net sales) + same dates with _newks suffix (Newk's net sales)

    The Toast ERA netSalesAmount INCLUDES restaurant-destination service charges (delivery/catering
    fees collected from the customer on behalf of Newk's).  However, Newk's must pay those fees
    out to the delivery providers (DeliverThat, etc.), so they are NOT Newk's revenue:
        Newk's Net Sales = Toast netSalesAmount - restaurant_sc (destination==RESTAURANT, non-gratuity)

    service_charges: optional dict keyed by f"{restaurantGuid}|{businessDate}" (YYYYMMDD),
        value = total restaurant-destination SC amount for that location+day (from Orders API).
        When None or a key is missing, falls back to 0 (Newk's == Toast).

    Returns:
    {
        "pivotTable": [
            {"restaurantName": "Location A", "20251210": 1000, "20251210_newks": 937, ...},
            ...
        ],
        "totals": [
            {"date": "20251210", "totalNetSales": 5000, "totalNetSalesNewks": 4685, "totalServiceChargeAddon": 315},
            ...
        ]
    }
    """
    if not raw_rows:
        return {"pivotTable": [], "totals": []}

    location_data = {}  # location -> date -> toast_net_sales
    location_sc_addon = {}  # location -> date -> restaurant_sc amount subtracted from Newk's
    all_dates = set()
    date_totals = {}       # date -> total Toast net sales
    date_totals_newks = {} # date -> total Newk's net sales
    date_totals_sc = {}    # date -> total SC add-on (for display)

    for row in raw_rows:
        restaurant_name = row.get("restaurantName", "Unknown")
        restaurant_guid = row.get("restaurantGuid", "")
        business_date = row.get("businessDate", "")
        # Toast ERA netSalesAmount: menu item sales only, service charges excluded
        toast_net_sales = row.get("netSalesAmount", 0) or 0
        # Restaurant-destination SCs fetched via Orders API; subtracted to get Newk's true net
        sc_key = f"{restaurant_guid}|{business_date}"
        sc_addon = (service_charges or {}).get(sc_key, 0.0)
        newks_net_sales = toast_net_sales - sc_addon

        if restaurant_name not in location_data:
            location_data[restaurant_name] = {}
            location_sc_addon[restaurant_name] = {}

        location_data[restaurant_name][business_date] = toast_net_sales
        location_sc_addon[restaurant_name][business_date] = sc_addon
        all_dates.add(business_date)

        if business_date not in date_totals:
            date_totals[business_date] = 0
            date_totals_newks[business_date] = 0
            date_totals_sc[business_date] = 0
        date_totals[business_date] += toast_net_sales
        date_totals_newks[business_date] += newks_net_sales
        date_totals_sc[business_date] += sc_addon

    sorted_dates = sorted(list(all_dates))
    sorted_locations = sorted(location_data.keys())

    # Build pivot rows: each date column has Toast value; date_newks has Newk's (Toast - SC addon)
    pivot_rows = []
    for location in sorted_locations:
        row_data = {"restaurantName": location}
        for d in sorted_dates:
            toast_val = location_data[location].get(d, 0)
            sc = location_sc_addon[location].get(d, 0)
            row_data[d] = toast_val
            row_data[f"{d}_newks"] = toast_val - sc
        pivot_rows.append(row_data)

    totals_rows = [
        {
            "date": d,
            "totalNetSales": date_totals[d],
            "totalNetSalesNewks": date_totals_newks[d],
            "totalServiceChargeAddon": date_totals_sc[d],
        }
        for d in sorted_dates
    ]
    
    return {
        "pivotTable": pivot_rows,
        "totals": totals_rows
    }

async def _fetch_catering_item_data(
    client: httpx.AsyncClient,
    start_date: date,
    end_date: date,
    restaurant_ids: List[str],
) -> Dict[str, Dict]:
    """
    Fetch item-level analytics and return per '{restaurantGuid}|{businessDate}' key:
      catering_ns    – net sales from items whose name contains '(catering)'
      catering_gc    – tag-based guest count for catering items (qty × GC tag value)
      non_catering_gc – tag-based guest count for non-catering items
    """
    # Build guest-count tag lookup (same approach as build_guest_count_report)
    gc_by_guid: Dict[str, float] = {}
    try:
        tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
        for guid, tags in tags_mapping.items():
            for tag in tags:
                val = parse_guest_count_value(tag.get("tagName", ""))
                if val is not None:
                    gc_by_guid[guid] = val
                    break
    except Exception as exc:
        logger.warning("_fetch_catering_item_data: failed to load GC tag mapping: %s", exc)

    days_diff = (end_date - start_date).days + 1
    all_rows: list = []

    if days_diff > 7:
        current_start = start_date
        while current_start <= end_date:
            chunk_end = min(current_start + timedelta(days=6), end_date)
            try:
                rg = await request_toast_report(
                    client, current_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
                )
                rows = await fetch_toast_report(client, rg, "menu")
                if rows:
                    all_rows.extend(rows)
            except Exception as exc:
                logger.warning("Catering analytics fetch failed for %s–%s: %s", current_start, chunk_end, exc)
            current_start = chunk_end + timedelta(days=1)
    else:
        try:
            rg = await request_toast_report(
                client, start_date, end_date, restaurant_ids, "menu", ["MENU_ITEM"]
            )
            all_rows = await fetch_toast_report(client, rg, "menu")
        except Exception as exc:
            logger.warning("Catering analytics fetch failed: %s", exc)

    result: Dict[str, Dict] = {}
    for row in (all_rows or []):
        r_guid  = row.get("restaurantGuid", "")
        b_date  = row.get("businessDate", "")
        key     = f"{r_guid}|{b_date}"
        name    = (row.get("menuItemName") or "").lower()
        ns      = float(row.get("netSalesAmount") or 0)
        qty     = float(row.get("quantitySold") or 0)
        item_gc = gc_by_guid.get(row.get("menuItemGuid", ""), 0.0)
        gc_contrib = qty * item_gc

        if key not in result:
            result[key] = {"catering_ns": 0.0, "catering_gc": 0.0, "non_catering_gc": 0.0}

        if "(catering)" in name:
            result[key]["catering_ns"] += ns
            result[key]["catering_gc"] += gc_contrib
        else:
            result[key]["non_catering_gc"] += gc_contrib

    logger.info(
        "Catering item data fetch: %d item rows → %d location/date keys",
        len(all_rows or []), len(result),
    )
    return result


def compute_sales_summary_by_location(
    raw_rows: list,
    metrics_by_key: Dict[str, Dict],
    catering_item_data: Optional[Dict[str, Dict]] = None,
) -> Dict:
    """
    Aggregate ERA netSalesAmount + Orders API metrics into a per-location summary table.

    raw_rows:        ERA analytics rows: restaurantName, restaurantGuid, businessDate, netSalesAmount
    metrics_by_key:  {f"{restaurantGuid}|{businessDate}": metrics_dict}
                     Each metrics_dict is from _compute_sales_metrics_for_day().

    Returns:
    {
        "summaryTable": [
            { "restaurantName", "orders", "toastNetSales", "serviceFees",
              "netSales", "avgTicket", "discounts", "voids", "cash", "card" },
            ...
        ],
        "summaryTotals": { same keys, totals/averages across all locations }
    }
    """
    loc: Dict[str, Dict] = {}

    for row in raw_rows:
        r_name   = row.get("restaurantName", "Unknown")
        r_guid   = row.get("restaurantGuid", "")
        b_date   = row.get("businessDate", "")
        toast_ns = float(row.get("netSalesAmount") or 0)
        key      = f"{r_guid}|{b_date}"
        m        = metrics_by_key.get(key, {})

        guest_count   = int(row.get("guestCount") or 0)
        _item_key     = (catering_item_data or {}).get(key, {})
        catering_ns   = float(_item_key.get("catering_ns", 0.0))
        catering_gc   = float(_item_key.get("catering_gc", 0.0))
        non_cat_gc    = float(_item_key.get("non_catering_gc", 0.0))

        if r_name not in loc:
            loc[r_name] = {
                "orders": 0, "toast_net": 0.0, "svc_fees": 0.0,
                "discounts": 0.0, "voids": 0, "cash": 0.0, "card": 0.0,
                "guest_count": 0, "catering_toast": 0.0,
                "cat_orders": 0, "cat_sc": 0.0, "cat_discount": 0.0,
                "cat_void": 0, "cat_cash": 0.0, "cat_card": 0.0,
                "cat_gc": 0.0, "non_cat_gc": 0.0,
            }

        loc[r_name]["orders"]       += m.get("order_count", 0)
        loc[r_name]["toast_net"]    += toast_ns
        loc[r_name]["svc_fees"]     += m.get("restaurant_sc", 0.0)
        loc[r_name]["discounts"]    += m.get("discount_amount", 0.0)
        loc[r_name]["voids"]        += m.get("void_count", 0)
        loc[r_name]["cash"]         += m.get("cash_amount", 0.0)
        loc[r_name]["card"]         += m.get("card_amount", 0.0)
        loc[r_name]["guest_count"]  += guest_count
        loc[r_name]["catering_toast"] += catering_ns
        loc[r_name]["cat_gc"]       += catering_gc
        loc[r_name]["non_cat_gc"]   += non_cat_gc
        loc[r_name]["cat_orders"]   += m.get("cat_order_count", 0)
        loc[r_name]["cat_sc"]       += m.get("cat_sc", 0.0)
        loc[r_name]["cat_discount"] += m.get("cat_discount", 0.0)
        loc[r_name]["cat_void"]     += m.get("cat_void", 0)
        loc[r_name]["cat_cash"]     += m.get("cat_cash", 0.0)
        loc[r_name]["cat_card"]     += m.get("cat_card", 0.0)

    summary_rows  = []
    catering_rows = []
    non_cat_rows  = []
    tot: Dict[str, Any] = {
        "orders": 0, "toastNetSales": 0.0, "serviceFees": 0.0,
        "netSales": 0.0, "discounts": 0.0, "voids": 0, "cash": 0.0, "card": 0.0,
        "guestCount": 0,
    }
    cat_tot: Dict[str, Any] = {
        "orders": 0, "guestCount": 0.0, "netSales": 0.0, "discounts": 0.0, "voids": 0, "cash": 0.0, "card": 0.0,
    }
    non_cat_tot: Dict[str, Any] = {
        "orders": 0, "guestCount": 0.0, "netSales": 0.0, "discounts": 0.0, "voids": 0, "cash": 0.0, "card": 0.0,
    }

    for r_name in sorted(loc.keys()):
        d              = loc[r_name]
        toast_ns       = round(d["toast_net"], 2)
        svc_fees       = round(d["svc_fees"], 2)
        net_sales      = round(toast_ns - svc_fees, 2)
        orders         = d["orders"]
        avg_tick       = round(net_sales / orders, 2) if orders > 0 else 0.0
        discounts      = round(d["discounts"], 2)
        voids          = d["voids"]
        cash           = round(d["cash"], 2)
        card           = round(d["card"], 2)
        guest_count    = d["guest_count"]
        ppa            = round(net_sales / guest_count, 2) if guest_count > 0 else 0.0
        catering_toast = round(d["catering_toast"], 2)

        # Catering split — net sales from item analytics, other metrics from orders
        cat_svc          = round(d["cat_sc"], 2)
        catering_net     = round(catering_toast - cat_svc, 2)
        cat_orders       = d["cat_orders"]
        cat_discounts    = round(d["cat_discount"], 2)
        cat_voids        = d["cat_void"]
        cat_cash         = round(d["cat_cash"], 2)
        cat_card         = round(d["cat_card"], 2)
        cat_guest_count  = round(d["cat_gc"], 2)
        cat_avg          = round(catering_net / cat_orders, 2) if cat_orders > 0 else 0.0
        cat_ppa          = round(catering_net / cat_guest_count, 2) if cat_guest_count > 0 else 0.0

        non_cat_net      = round(net_sales - catering_net, 2)
        non_cat_orders   = orders - cat_orders
        non_cat_disc     = round(discounts - cat_discounts, 2)
        non_cat_voids    = voids - cat_voids
        non_cat_cash     = round(cash - cat_cash, 2)
        non_cat_card     = round(card - cat_card, 2)
        non_cat_gc       = round(d["non_cat_gc"], 2)
        non_cat_avg      = round(non_cat_net / non_cat_orders, 2) if non_cat_orders > 0 else 0.0
        non_cat_ppa      = round(non_cat_net / non_cat_gc, 2) if non_cat_gc > 0 else 0.0

        summary_rows.append({
            "restaurantName": r_name,
            "orders":         orders,
            "guestCount":     guest_count,
            "toastNetSales":  toast_ns,
            "serviceFees":    svc_fees,
            "netSales":       net_sales,
            "avgTicket":      avg_tick,
            "ppa":            ppa,
            "discounts":      discounts,
            "voids":          voids,
            "cash":           cash,
            "card":           card,
        })
        catering_rows.append({
            "restaurantName": r_name,
            "orders":         cat_orders,
            "guestCount":     cat_guest_count,
            "netSales":       catering_net,
            "avgTicket":      cat_avg,
            "ppa":            cat_ppa,
            "discounts":      cat_discounts,
            "voids":          cat_voids,
            "cash":           cat_cash,
            "card":           cat_card,
        })
        non_cat_rows.append({
            "restaurantName": r_name,
            "orders":         non_cat_orders,
            "guestCount":     non_cat_gc,
            "netSales":       non_cat_net,
            "avgTicket":      non_cat_avg,
            "ppa":            non_cat_ppa,
            "discounts":      non_cat_disc,
            "voids":          non_cat_voids,
            "cash":           non_cat_cash,
            "card":           non_cat_card,
        })

        tot["orders"]        += orders
        tot["toastNetSales"]  = round(tot["toastNetSales"] + toast_ns, 2)
        tot["serviceFees"]    = round(tot["serviceFees"] + svc_fees, 2)
        tot["netSales"]       = round(tot["netSales"] + net_sales, 2)
        tot["discounts"]      = round(tot["discounts"] + discounts, 2)
        tot["voids"]         += voids
        tot["cash"]           = round(tot["cash"] + cash, 2)
        tot["card"]           = round(tot["card"] + card, 2)
        tot["guestCount"]    += guest_count

        cat_tot["orders"]      += cat_orders
        cat_tot["guestCount"]   = round(cat_tot["guestCount"] + cat_guest_count, 2)
        cat_tot["netSales"]     = round(cat_tot["netSales"] + catering_net, 2)
        cat_tot["discounts"]    = round(cat_tot["discounts"] + cat_discounts, 2)
        cat_tot["voids"]       += cat_voids
        cat_tot["cash"]         = round(cat_tot["cash"] + cat_cash, 2)
        cat_tot["card"]         = round(cat_tot["card"] + cat_card, 2)

        non_cat_tot["orders"]     += non_cat_orders
        non_cat_tot["guestCount"]  = round(non_cat_tot["guestCount"] + non_cat_gc, 2)
        non_cat_tot["netSales"]    = round(non_cat_tot["netSales"] + non_cat_net, 2)
        non_cat_tot["discounts"]   = round(non_cat_tot["discounts"] + non_cat_disc, 2)
        non_cat_tot["voids"]      += non_cat_voids
        non_cat_tot["cash"]        = round(non_cat_tot["cash"] + non_cat_cash, 2)
        non_cat_tot["card"]        = round(non_cat_tot["card"] + non_cat_card, 2)

    tot["avgTicket"]          = round(tot["netSales"] / tot["orders"], 2) if tot["orders"] > 0 else 0.0
    tot["ppa"]                = round(tot["netSales"] / tot["guestCount"], 2) if tot["guestCount"] > 0 else 0.0
    cat_tot["avgTicket"]      = round(cat_tot["netSales"] / cat_tot["orders"], 2) if cat_tot["orders"] > 0 else 0.0
    cat_tot["ppa"]            = round(cat_tot["netSales"] / cat_tot["guestCount"], 2) if cat_tot["guestCount"] > 0 else 0.0
    non_cat_tot["avgTicket"]  = round(non_cat_tot["netSales"] / non_cat_tot["orders"], 2) if non_cat_tot["orders"] > 0 else 0.0
    non_cat_tot["ppa"]        = round(non_cat_tot["netSales"] / non_cat_tot["guestCount"], 2) if non_cat_tot["guestCount"] > 0 else 0.0

    return {
        "summaryTable":      summary_rows,
        "summaryTotals":     tot,
        "cateringTable":     catering_rows,
        "cateringTotals":    cat_tot,
        "nonCateringTable":  non_cat_rows,
        "nonCateringTotals": non_cat_tot,
    }


# Tag-name patterns used to classify live Toast tags into the EE / Guest Count /
# LTO buckets (mirror of toast_config_db._EE_TAG_RE etc.; kept inline here so
# we avoid the SQLite round-trip on every report run).
_EE_TAG_RE_LIVE  = re.compile(r"^EE\s+(\.?\d+(?:\.\d+)?)$",         re.IGNORECASE)
_GC_TAG_RE_LIVE  = re.compile(r"^Guest\s+Count\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)
_LTO_TAG_RE_LIVE = re.compile(r"^\d{4}LTO\d*$",                     re.IGNORECASE)


def pivot_menu_items_by_tag(
    analytics_rows: List[dict],
    tags_mapping: Dict[str, List[Dict]],
    selected_tags: List[str],
    item_names_mapping: Dict[str, str] = None,
    menu_item_guids: set = None,
    include_zero_sales: bool = False,
    plu_mapping: Dict[str, int] = None,
) -> dict:
    logger.info(f"pivot_menu_items_by_tag called with include_zero_sales={include_zero_sales}, selected_tags={selected_tags}, tags_mapping size={len(tags_mapping)}")
    """
    Transform menu items into pivot table format by restaurant and tag.
    
    Returns:
    {
        "pivotTable": [
            {"restaurantName": "Location A", "Tag1": 100, "Tag2": 50, ...},
            ...
        ],
        "totals": [
            {"itemTagName": "Tag1", "totalQty": 175},
            ...
        ],
        "itemBreakdown": [
            {"itemTagName": "Tag1", "menuItemGuid": "guid1", "menuItemName": "Item Name", "totalQty": 50},
            ...
        ]
    }
    """
    # Don't return early if include_zero_sales is True - we need to process zero-sales items
    if not analytics_rows and not include_zero_sales:
        return {"pivotTable": [], "totals": [], "itemBreakdown": []}
    
    if item_names_mapping is None:
        item_names_mapping = {}
    
    if menu_item_guids is None:
        menu_item_guids = set()
    
    # Aggregate by restaurant + tag
    restaurant_tag_data = {}  # {restaurantGuid: {restaurantName: str, tags: {tagName: qty}}}
    tag_totals = {}  # {tagName: total_qty}
    tag_item_breakdown = {}  # {tagName: {menuItemGuid: total_qty}}
    tag_item_breakdown_daily = {}  # {tagName: {menuItemGuid: {menuItemName, totalQty, daily}}}
    item_totals = {}  # {menuItemGuid: {menuItemName: str, totalQty: int}}
    date_columns = set()

    # Per-item attribute lookup (consolidated category + EE tag) — sourced from
    # mli_plu_attribute_map via Excel mapping table. Keyed by both item_guid and
    # normalized name so we hit on either. Used to stamp each breakdown row so the
    # menu performance Excel export can show ConsolidatedCat + EE alongside qty.
    _attr_lookup = toast_config_db.get_item_attribute_lookup() if toast_config_db.db_available() else {}
    # PLU range classifier — Aloha ranges → "Item Classification" (broader),
    # Toast ranges → "Item Subclass" (more granular). Built once per report.
    _plu_classify = toast_config_db.get_plu_range_classifier()
    # Live tag-derived attrs — derived directly from `tags_mapping`, which is
    # fetched fresh from the Toast V3 Menus API on each report run (with
    # metadata-hash caching). This means changes published in Toastweb appear
    # in the report as soon as Toast's API has propagated them — no separate
    # toast_config sync needed. The Excel mli_plu_attribute_map values stay
    # as a fallback for items Toast hasn't tagged.
    _live_tag_attrs: Dict[str, Dict[str, object]] = {}
    for _guid, _tags in (tags_mapping or {}).items():
        if not _guid or not _tags:
            continue
        recipes: set = set()
        consol:  set = set()
        ee_val: float = 0.0
        gc_val: float = 0.0
        for _t in _tags:
            _name = (_t.get("tagName") if isinstance(_t, dict) else "") or ""
            _s = _name.strip()
            if not _s or _s == "Catering Menu":
                continue
            if _s.startswith("*"):
                consol.add(_name)
                continue
            m = _EE_TAG_RE_LIVE.match(_s)
            if m:
                try:
                    v = float(m.group(1))
                    if v > ee_val:
                        ee_val = v
                except ValueError:
                    pass
                continue
            m = _GC_TAG_RE_LIVE.match(_s)
            if m:
                try:
                    v = float(m.group(1))
                    if v > gc_val:
                        gc_val = v
                except ValueError:
                    pass
                continue
            if _LTO_TAG_RE_LIVE.match(_s):
                continue
            recipes.add(_name)
        if recipes or consol or ee_val or gc_val:
            _live_tag_attrs[_guid] = {
                "recipe":           ", ".join(sorted(recipes)),
                "consolidated_cat": ", ".join(sorted(consol)),
                "entree_equiv_tag": ee_val,
                "guest_count_tag":  gc_val,
            }
    # SQLite snapshot fallback — covers items the live API call didn't return
    # (e.g. items tagged in stores other than restaurant_ids[0], or items the
    # V2 menus payload omits). Populated by sync_toast_config; might be stale,
    # but stale-but-present beats blank when the live API is missing data.
    _sqlite_tag_attrs = (
        toast_config_db.get_live_item_tag_attrs()
        if toast_config_db.db_available() else {}
    )
    # Live Toast PLU — caller passes plu_mapping built from the same V3 menus
    # payload as tags_mapping when available. SQLite snapshot is the second
    # fallback; Excel mapping is the third.
    _live_plu_map = plu_mapping or {}
    _sqlite_plu_map = (
        toast_config_db.get_live_toast_plu_map()
        if toast_config_db.db_available() else {}
    )

    def _attr_for(guid: str, name: str) -> Tuple[str, float, str, float, int, str, str]:
        """
        Returns: (consolidated_cat, entree_equiv_tag, recipe, guest_count_tag,
                  toast_plu, item_classification, item_subclass)

        Three-tier fallback for each value:
          1. Live V3/V2 Menus API data (tags_mapping / plu_mapping)
          2. SQLite snapshot from cfg_map_item_tags / cfg_menu_items
             (last sync_toast_config run; covers all stores)
          3. Excel mli_plu_attribute_map (manual mapping)

        Stale-but-present beats blank when the live API call doesn't return
        data for an item (e.g. items tagged only in stores other than the
        first one we fetch from, or V2 omissions).
        """
        entry = _attr_lookup.get(guid) if guid else None
        if entry is None and name:
            entry = _attr_lookup.get(_normalize_item_name(name))

        live_attrs   = _live_tag_attrs.get(guid, {})    if guid else {}
        sqlite_attrs = _sqlite_tag_attrs.get(guid, {})  if guid else {}
        live_plu     = _live_plu_map.get(guid, 0)       if guid else 0
        sqlite_plu   = _sqlite_plu_map.get(guid, 0)     if guid else 0

        # Excel-side fallbacks (default to empty/zero when no Excel row).
        ex_cat    = str(entry.get("consolidated_cat", "") or "")        if entry else ""
        ex_ee     = float(entry.get("entree_equiv_tag", 0.0) or 0.0)    if entry else 0.0
        ex_recipe = str(entry.get("recipe", "") or "")                  if entry else ""
        ex_gc     = float(entry.get("guest_count_tag", 0.0) or 0.0)     if entry else 0.0
        ex_plu    = int(entry.get("toast_plu", 0) or 0)                 if entry else 0
        ex_aplu   = int(entry.get("aloha_plu", 0) or 0)                 if entry else 0

        # Walk the chain: live → sqlite → excel; first non-empty wins.
        consolidated_cat = (
            str(live_attrs.get("consolidated_cat", "") or "")
            or str(sqlite_attrs.get("consolidated_cat", "") or "")
            or ex_cat
        )
        recipe = (
            str(live_attrs.get("recipe", "") or "")
            or str(sqlite_attrs.get("recipe", "") or "")
            or ex_recipe
        )
        entree_equiv_tag = (
            float(live_attrs.get("entree_equiv_tag") or 0.0)
            or float(sqlite_attrs.get("entree_equiv_tag") or 0.0)
            or ex_ee
        )
        guest_count_tag = (
            float(live_attrs.get("guest_count_tag") or 0.0)
            or float(sqlite_attrs.get("guest_count_tag") or 0.0)
            or ex_gc
        )
        toast_plu = int(live_plu) or int(sqlite_plu) or ex_plu

        # Item Classification (Aloha range): three tries, in order
        #   1. Explicit aloha_plu from the curated Excel mapping
        #   2. Raw Toast PLU against Aloha ranges (some Aloha ranges use
        #      Toast-style PLUs directly — e.g. "Toast Parent Items - POS"
        #      at 300200-300210 catches Meal Deal parent items like 300204)
        #   3. Prefix-strip the Toast PLU and look up the underlying Aloha
        #      PLU (catches 3001003 → 1003 → "Soups" pattern)
        item_classification = _plu_classify("Aloha", ex_aplu) if ex_aplu else ""
        if not item_classification and toast_plu:
            item_classification = _plu_classify("Aloha", toast_plu)
        if not item_classification and toast_plu:
            item_classification = _plu_classify(
                "Aloha", toast_config_db.derive_aloha_plu_from_toast(toast_plu)
            )
        # Suppress prefix-strip false positives: when the derived Aloha range
        # name contains "Catering", "Inactive", or "Do Not Use" but the item's
        # consolidated category clearly isn't catering, the strip landed in a
        # coincidental range (e.g. Toast OLO PLU 40070303 strips to 70303,
        # which is in Aloha "Catering - Sandwich Platter Items" — wrong).
        # Only suppress when there's NO explicit aloha_plu in the mapping —
        # that's curated and trusted.
        if item_classification and not ex_aplu:
            cls_low = item_classification.lower()
            cat_low = consolidated_cat.lower()
            looks_suspect = (
                "catering" in cls_low or "inactive" in cls_low or "do not use" in cls_low
            )
            if looks_suspect and "catering" not in cat_low:
                item_classification = ""
        # Item Subclass: direct Toast range lookup against the live PLU.
        item_subclass = _plu_classify("Toast", toast_plu) if toast_plu else ""

        return (
            consolidated_cat,
            entree_equiv_tag,
            recipe,
            guest_count_tag,
            toast_plu,
            item_classification,
            item_subclass,
        )

    modifier_count_in_pivot = 0
    modifier_count_in_breakdown = 0
    skipped_no_tags = 0

    # One canonical display name per normalized tag so "* Consolidated Pizza" and "Consolidated Pizza" merge
    _canonical_for_norm: Dict[str, str] = {}

    def _canonical_tag(tag_name: str) -> str:
        norm = _normalize_tag_name(tag_name)
        if norm not in _canonical_for_norm:
            _canonical_for_norm[norm] = tag_name
        return _canonical_for_norm[norm]

    if selected_tags:
        for t in selected_tags:
            _canonical_tag(t)

    # Pre-compute once: normalized selected-tag set (was previously rebuilt
    # per row, which was ~15 normalizations × 1M rows = ~15M wasted calls).
    selected_tags_norm_set: set = (
        {_normalize_tag_name(t) for t in selected_tags} if selected_tags else set()
    )

    # Per-item attribute cache. `_attr_for` is a pure function of (guid, name)
    # but used to be called once per (row × matched-tag); with multi-tag items
    # that's 3-5× per row, plus PLU range classification inside. Memoizing by
    # guid collapses it to once per unique item.
    _attr_cache: Dict[str, Tuple[str, float, str, float, int, str, str]] = {}

    def _attr_for_cached(guid: str, name: str):
        if guid in _attr_cache:
            return _attr_cache[guid]
        result_attrs = _attr_for(guid, name)
        _attr_cache[guid] = result_attrs
        return result_attrs

    for row in analytics_rows:
        restaurant_guid = row.get("restaurantGuid", "")
        restaurant_name = row.get("restaurantName", "Unknown")
        menu_item_guid = row.get("menuItemGuid", "")
        menu_item_name = row.get("menuItemName", "") or item_names_mapping.get(menu_item_guid, f"Item {menu_item_guid[:8]}")
        quantity_sold = row.get("quantitySold", 0) or 0
        business_date = row.get("businessDate")
        
        if not menu_item_guid:
            continue
        
        # If tags are selected, require mapping to validate tag membership
        if selected_tags and menu_item_guid not in tags_mapping:
            skipped_no_tags += 1
            # Log first few skipped items to debug
            if skipped_no_tags <= 3:
                logger.debug(f"Skipping row: guid={menu_item_guid}, name='{menu_item_name}' - not in tags_mapping")
            continue
        
        # Determine if this is a modifier (not in the original menu_item_guids set)
        is_modifier = menu_item_guid not in menu_item_guids
        
        # Get tags for this item (works for both menu items and modifiers)
        if selected_tags:
            item_tags = tags_mapping.get(menu_item_guid, [])
        else:
            item_tags = [{"tagName": "All Items"}]
        
        # Filter to only selected tags (if any selected) - use normalized so "* Consolidated Pizza" matches "Consolidated Pizza"
        if selected_tags:
            item_tags = [t for t in item_tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm_set]
            if not item_tags:
                continue
        else:
            # No tags selected: collapse all items into a single section
            item_tags = [{"tagName": "All Items"}]
        
        # Track overall item totals once per row (avoid double-counting across tags)
        if menu_item_guid not in item_totals:
            item_totals[menu_item_guid] = {
                "menuItemName": menu_item_name,
                "totalQty": 0
            }
        item_totals[menu_item_guid]["totalQty"] += quantity_sold
        
        # Initialize restaurant data if needed (only for menu items)
        if not is_modifier:
            if restaurant_guid not in restaurant_tag_data:
                restaurant_tag_data[restaurant_guid] = {
                    "restaurantName": restaurant_name,
                    "tags": {}
                }
        
        # Add quantity to each tag (MULTI_COUNT mode - each tag gets full quantity)
        # Use canonical tag so "* Consolidated Pizza" and "Consolidated Pizza" merge into one column
        for tag in item_tags:
            tag_name = tag["tagName"]
            canonical = _canonical_tag(tag_name)
            
            # Add to restaurant-tag aggregation (pivot table) - only for menu items
            if not is_modifier:
                if canonical not in restaurant_tag_data[restaurant_guid]["tags"]:
                    restaurant_tag_data[restaurant_guid]["tags"][canonical] = 0
                restaurant_tag_data[restaurant_guid]["tags"][canonical] += quantity_sold
                
                # Add to totals (pivot table totals) - only for menu items
                if canonical not in tag_totals:
                    tag_totals[canonical] = 0
                tag_totals[canonical] += quantity_sold
            
            # Look up the per-item attributes (consolidated category, EE tag,
            # Guest Count tag, Toast PLU, Item Classification, Item Subclass).
            # Memoized by guid — same guid hits the cache across all its tags
            # and across all its sales rows.
            _cat, _ee, _recipe, _gct, _plu, _cls, _sub = _attr_for_cached(menu_item_guid, menu_item_name)

            # Track item-level breakdown by tag (SAME for both menu items and modifiers)
            if canonical not in tag_item_breakdown:
                tag_item_breakdown[canonical] = {}
            if menu_item_guid not in tag_item_breakdown[canonical]:
                tag_item_breakdown[canonical][menu_item_guid] = {
                    "menuItemName": menu_item_name,
                    "totalQty": 0,
                    "consolidatedCat": _cat,
                    "entreeEquivTag": _ee,
                    "recipe": _recipe,
                    "guestCountTag": _gct,
                    "plu": _plu,
                    "itemClassification": _cls,
                    "itemSubclass": _sub,
                }
            tag_item_breakdown[canonical][menu_item_guid]["totalQty"] += quantity_sold

            # Track item-level breakdown with daily columns
            if canonical not in tag_item_breakdown_daily:
                tag_item_breakdown_daily[canonical] = {}
            if menu_item_guid not in tag_item_breakdown_daily[canonical]:
                tag_item_breakdown_daily[canonical][menu_item_guid] = {
                    "menuItemName": menu_item_name,
                    "totalQty": 0,
                    "daily": {},
                    "consolidatedCat": _cat,
                    "entreeEquivTag": _ee,
                    "recipe": _recipe,
                    "guestCountTag": _gct,
                    "plu": _plu,
                    "itemClassification": _cls,
                    "itemSubclass": _sub,
                }
            tag_item_breakdown_daily[canonical][menu_item_guid]["totalQty"] += quantity_sold
            if business_date:
                date_columns.add(business_date)
                if business_date not in tag_item_breakdown_daily[canonical][menu_item_guid]["daily"]:
                    tag_item_breakdown_daily[canonical][menu_item_guid]["daily"][business_date] = 0
                tag_item_breakdown_daily[canonical][menu_item_guid]["daily"][business_date] += quantity_sold
            
            if is_modifier:
                modifier_count_in_breakdown += 1
    
    # Build pivot table
    # Get all unique tag names (sorted); use canonical so one column per normalized tag
    if include_zero_sales and not tag_totals and selected_tags:
        for t in selected_tags:
            _canonical_tag(t)
        all_tag_names = sorted(set(_canonical_for_norm.values()))
    else:
        all_tag_names = sorted(set(tag_totals.keys()))
    
    # Build pivot rows
    pivot_rows = []
    for restaurant_guid, data in sorted(restaurant_tag_data.items(), key=lambda x: x[1]["restaurantName"]):
        row = {"restaurantName": data["restaurantName"]}
        for tag_name in all_tag_names:
            row[tag_name] = data["tags"].get(tag_name, 0)
        pivot_rows.append(row)
    
    # Build totals section
    totals_rows = [
        {"itemTagName": tag_name, "totalQty": tag_totals.get(tag_name, 0)}
        for tag_name in all_tag_names
    ]
    
    # Build item breakdown section
    item_breakdown_rows = []
    item_breakdown_daily_rows = []
    modifier_breakdown_totals = {}  # Track modifier totals across all tags for debugging
    
    # If include_zero_sales is True, get all items with selected tags from tags_mapping
    items_with_zero_sales = {}
    if include_zero_sales and selected_tags:
        # Use case-insensitive matching
        selected_tags_lower = [t.lower() for t in selected_tags]
        selected_tags_set = set(selected_tags_lower)
        logger.info(f"Looking for zero-sales items in tags_mapping (size: {len(tags_mapping)}) with selected tags: {selected_tags}")
        for item_guid, item_tags in tags_mapping.items():
            # Check if this item has any of the selected tags (case-insensitive)
            matching_tags = [t for t in item_tags if t["tagName"].lower() in selected_tags_set]
            if matching_tags:
                item_name = item_names_mapping.get(item_guid, f"Item {item_guid[:8]}")
                # Track which tags this item has (use canonical so we merge with pivot)
                for tag in matching_tags:
                    canon = _canonical_tag(tag["tagName"])
                    if canon not in items_with_zero_sales:
                        items_with_zero_sales[canon] = {}
                    if item_guid not in items_with_zero_sales[canon]:
                        items_with_zero_sales[canon][item_guid] = item_name
        logger.info(f"Found {sum(len(items) for items in items_with_zero_sales.values())} zero-sales items across {len(items_with_zero_sales)} tags")
    elif include_zero_sales and not selected_tags:
        logger.info("No tags selected; adding zero-sales items under 'All Items'")
        items_with_zero_sales["All Items"] = {}
        for item_guid, item_tags in tags_mapping.items():
            item_name = item_names_mapping.get(item_guid, f"Item {item_guid[:8]}")
            items_with_zero_sales["All Items"][item_guid] = item_name

    # Always build "all items per tag" from tags_mapping (normalized tag match) so the breakdown
    # and Toast Items popup list every item that belongs to the tag, including 0-count.
    all_items_per_tag = {}
    if selected_tags and tags_mapping:
        selected_tags_norm = [_normalize_tag_name(t) for t in selected_tags]
        for tag_name in all_tag_names:
            all_items_per_tag[tag_name] = {}
            tag_norm = _normalize_tag_name(tag_name)
            for item_guid, item_tags in tags_mapping.items():
                for t in item_tags:
                    if _normalize_tag_name(t["tagName"]) == tag_norm:
                        all_items_per_tag[tag_name][item_guid] = item_names_mapping.get(item_guid, f"Item {item_guid[:8]}")
                        break
    
    for tag_name in all_tag_names:
        # Track which items we've already added (those with sales)
        items_in_tag = set()
        
        # Start with items that have sales
        if tag_name in tag_item_breakdown:
            for menu_item_guid, item_data in sorted(tag_item_breakdown[tag_name].items(),
                                                     key=lambda x: x[1]["totalQty"],
                                                     reverse=True):
                items_in_tag.add(menu_item_guid)
                item_breakdown_rows.append({
                    "itemTagName": tag_name,
                    "menuItemGuid": menu_item_guid,
                    "menuItemName": item_data["menuItemName"],
                    "totalQty": item_data["totalQty"],
                    "consolidatedCat":  item_data.get("consolidatedCat", ""),
                    "entreeEquivTag":   item_data.get("entreeEquivTag", 0.0),
                    "recipe":           item_data.get("recipe", ""),
                    "guestCountTag":    item_data.get("guestCountTag", 0.0),
                    "plu":              item_data.get("plu", 0),
                    "itemClassification": item_data.get("itemClassification", ""),
                    "itemSubclass":       item_data.get("itemSubclass", ""),
                })
                if tag_name in tag_item_breakdown_daily and menu_item_guid in tag_item_breakdown_daily[tag_name]:
                    daily_data = tag_item_breakdown_daily[tag_name][menu_item_guid]
                    item_breakdown_daily_rows.append({
                        "itemTagName": tag_name,
                        "menuItemGuid": menu_item_guid,
                        "menuItemName": daily_data["menuItemName"],
                        "totalQty": daily_data["totalQty"],
                        "daily": daily_data["daily"],
                        "consolidatedCat":  daily_data.get("consolidatedCat", ""),
                        "entreeEquivTag":   daily_data.get("entreeEquivTag", 0.0),
                        "recipe":           daily_data.get("recipe", ""),
                        "guestCountTag":    daily_data.get("guestCountTag", 0.0),
                        "plu":              daily_data.get("plu", 0),
                        "itemClassification": daily_data.get("itemClassification", ""),
                        "itemSubclass":       daily_data.get("itemSubclass", ""),
                    })
                
                # Track modifier totals for debugging
                if menu_item_guid not in menu_item_guids:
                    if menu_item_guid not in modifier_breakdown_totals:
                        modifier_breakdown_totals[menu_item_guid] = {
                            "name": item_data["menuItemName"],
                            "totalQty": 0,
                            "tags": []
                        }
                    modifier_breakdown_totals[menu_item_guid]["totalQty"] += item_data["totalQty"]
                    modifier_breakdown_totals[menu_item_guid]["tags"].append(tag_name)
        
        # Add items with 0 sales if include_zero_sales is True
        if include_zero_sales and tag_name in items_with_zero_sales:
            logger.info(f"Adding {len(items_with_zero_sales[tag_name])} zero-sales items for tag '{tag_name}'")
            for item_guid, item_name in sorted(items_with_zero_sales[tag_name].items()):
                if item_guid not in items_in_tag:
                    logger.info(f"  Adding zero-sales item: {item_name} (guid: {item_guid[:8]}...)")
                    items_in_tag.add(item_guid)
                    _zcat, _zee, _zrec, _zgct, _zplu, _zcls, _zsub = _attr_for_cached(item_guid, item_name)
                    item_breakdown_rows.append({
                        "itemTagName": tag_name,
                        "menuItemGuid": item_guid,
                        "menuItemName": item_name,
                        "totalQty": 0,
                        "consolidatedCat": _zcat,
                        "entreeEquivTag": _zee,
                        "recipe": _zrec,
                        "guestCountTag": _zgct,
                        "plu": _zplu,
                        "itemClassification": _zcls,
                        "itemSubclass": _zsub,
                    })
                    item_breakdown_daily_rows.append({
                        "itemTagName": tag_name,
                        "menuItemGuid": item_guid,
                        "menuItemName": item_name,
                        "totalQty": 0,
                        "daily": {},
                        "consolidatedCat": _zcat,
                        "entreeEquivTag": _zee,
                        "recipe": _zrec,
                        "guestCountTag": _zgct,
                        "plu": _zplu,
                        "itemClassification": _zcls,
                        "itemSubclass": _zsub,
                    })
                else:
                    logger.info(f"  Skipping zero-sales item {item_name} (guid: {item_guid[:8]}...) - already has sales")

        # When include_zero_sales is enabled, add any tag-member item that wasn't
        # caught by items_with_zero_sales (normalized tag matching catches more
        # than the lowercase match used above).
        if include_zero_sales and tag_name in all_items_per_tag:
            for item_guid, item_name in sorted(all_items_per_tag[tag_name].items()):
                if item_guid not in items_in_tag:
                    items_in_tag.add(item_guid)
                    _zcat, _zee, _zrec, _zgct, _zplu, _zcls, _zsub = _attr_for_cached(item_guid, item_name)
                    item_breakdown_rows.append({
                        "itemTagName": tag_name,
                        "menuItemGuid": item_guid,
                        "menuItemName": item_name,
                        "totalQty": 0,
                        "consolidatedCat": _zcat,
                        "entreeEquivTag": _zee,
                        "recipe": _zrec,
                        "guestCountTag": _zgct,
                        "plu": _zplu,
                        "itemClassification": _zcls,
                        "itemSubclass": _zsub,
                    })
                    item_breakdown_daily_rows.append({
                        "itemTagName": tag_name,
                        "menuItemGuid": item_guid,
                        "menuItemName": item_name,
                        "totalQty": 0,
                        "daily": {},
                        "consolidatedCat": _zcat,
                        "entreeEquivTag": _zee,
                        "recipe": _zrec,
                        "guestCountTag": _zgct,
                        "plu": _zplu,
                        "itemClassification": _zcls,
                        "itemSubclass": _zsub,
                    })
    
    # Log modifier totals for debugging
    target_modifier_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_modifier_guid in modifier_breakdown_totals:
        mod_info = modifier_breakdown_totals[target_modifier_guid]
        logger.info(f"TARGET MODIFIER in breakdown: guid={target_modifier_guid}, name='{mod_info['name']}'")
        logger.info(f"  Total quantity across all tags: {mod_info['totalQty']}")
        logger.info(f"  Appears in {len(mod_info['tags'])} tags: {mod_info['tags']}")
        # Log per-tag breakdown
        for tag_name in mod_info['tags']:
            if tag_name in tag_item_breakdown and target_modifier_guid in tag_item_breakdown[tag_name]:
                tag_qty = tag_item_breakdown[tag_name][target_modifier_guid]["totalQty"]
                logger.info(f"  -> Tag '{tag_name}': {tag_qty}")
    
    logger.info(f"Pivot processing: {modifier_count_in_pivot} modifiers excluded from pivot totals, {modifier_count_in_breakdown} modifier entries in breakdown, {skipped_no_tags} rows skipped (not in tags_mapping)")

    item_totals_rows = [
        {
            "menuItemGuid": item_guid,
            "menuItemName": data["menuItemName"],
            "totalQty": data["totalQty"]
        }
        for item_guid, data in item_totals.items()
    ]
    item_totals_rows.sort(key=lambda x: (-x["totalQty"], (x["menuItemName"] or "").lower()))

    # ── Order mode split ────────────────────────────────────────────────────────
    # Items whose name contains "OLO" (case-insensitive) are online-ordering items;
    # everything else is in-store.

    def _mode_totals_from_breakdown(rows: list) -> list:
        totals_by_tag: Dict[str, float] = {}
        for r in rows:
            tag = r.get("itemTagName", "")
            totals_by_tag[tag] = totals_by_tag.get(tag, 0.0) + (r.get("totalQty") or 0)
        return [{"itemTagName": k, "totalQty": v} for k, v in sorted(totals_by_tag.items())]

    olo_breakdown_daily = [r for r in item_breakdown_daily_rows if _is_olo_item(r.get("menuItemName", ""))]
    instore_breakdown_daily = [r for r in item_breakdown_daily_rows if not _is_olo_item(r.get("menuItemName", ""))]
    olo_breakdown = [r for r in item_breakdown_rows if _is_olo_item(r.get("menuItemName", ""))]
    instore_breakdown = [r for r in item_breakdown_rows if not _is_olo_item(r.get("menuItemName", ""))]

    return {
        "pivotTable": pivot_rows,
        "totals": totals_rows,
        "itemTotals": item_totals_rows,
        "itemBreakdown": item_breakdown_rows,
        "itemBreakdownDaily": item_breakdown_daily_rows,
        "dateColumns": sorted(date_columns),
        # ── New fields ──────────────────────────────────────────────────────────
        "orderModeSplit": {
            "inStore": {
                "itemBreakdownDaily": instore_breakdown_daily,
                "itemBreakdown": instore_breakdown,
                "totals": _mode_totals_from_breakdown(instore_breakdown),
            },
            "olo": {
                "itemBreakdownDaily": olo_breakdown_daily,
                "itemBreakdown": olo_breakdown,
                "totals": _mode_totals_from_breakdown(olo_breakdown),
            },
        },
    }

# ------------------
# MSI / Monkey Discrepancy Report Parser
# ------------------

_MSI_UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)
_MSI_DATETIME_RE = re.compile(
    r'(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s+(?:AM|PM))',
    re.IGNORECASE,
)
# Location header: 4-digit code (City State) — region may have nested parens like "(Brookhaven)"
_MSI_LOC_HDR_RE = re.compile(
    r'\b(\d{3,4})\s+\(([^)]*(?:\([^)]*\)[^)]*)*)\)([\w][^\d]+?)\s+Order\s+ID\s+POS\s+ID',
    re.IGNORECASE | re.DOTALL,
)

_MSI_CLASSIFICATION_LABELS: Dict[str, str] = {
    "minor_rounding":      "Minor Rounding",
    "tax_rounding":        "Tax Rounding",
    "total_only":          "Total-Only (No Tax Diff)",
    "item_price_mismatch": "Item Price Mismatch",
    "large_discrepancy":   "Large Discrepancy",
    "tax_rate_issue":      "Tax Rate Issue",
    "mixed":               "Mixed Difference",
}
_MSI_CLASSIFICATION_SEVERITY: Dict[str, str] = {
    "minor_rounding":      "low",
    "tax_rounding":        "low",
    "total_only":          "medium",
    "item_price_mismatch": "high",
    "large_discrepancy":   "high",
    "tax_rate_issue":      "high",
    "mixed":               "medium",
}


def _msi_classify(tax_diff: Optional[float], total_diff: Optional[float]) -> str:
    """Return a classification code for one MSI discrepancy record."""
    td  = total_diff or 0.0
    txd = tax_diff   or 0.0
    abs_td  = abs(td)
    abs_txd = abs(txd)

    if abs_td < 0.02:
        return "minor_rounding"        # < 2¢ — essentially harmless

    if abs_txd < 0.01:
        return "total_only"            # total differs, no tax diff → fee or non-taxable item

    # Both differ — decide on nature
    if abs(abs_td - abs_txd) <= 0.05 and abs_td < 1.00:
        return "tax_rounding"          # total diff ≈ tax diff → standard rounding

    if abs_td >= 5.00:
        if abs_txd >= 1.00:
            return "tax_rate_issue"    # large tax discrepancy drives large total
        return "large_discrepancy"     # $5+ total diff with minimal tax cause

    if abs_td >= 1.00 and abs_td > abs_txd * 2.5:
        return "item_price_mismatch"   # total far exceeds tax diff → price issue

    if abs_txd >= 1.00:
        return "tax_rate_issue"        # large tax-specific diff

    return "mixed"                     # both differ, doesn't fit neat categories


def parse_msi_discrepancy_report(text: str) -> Dict:
    """
    Parse a Monkey / MSI discrepancy report (pasted raw text) into structured records.

    The report is typically copy-pasted as one flat space-delimited string from the Monkey
    portal.  Each location block has a header like:
        1182 (Athens GA)Atlanta - Barkin Eateries Order ID POS ID Log Date TAX TOTAL ...
    followed by order rows, each anchored by a Toast order GUID (UUID format).

    Returns:
    {
        "records":        [...],
        "count":          int,
        "parse_warnings": [...],
        "summary": {
            "by_classification":      {code: count},
            "by_severity":            {low/medium/high: count},
            "total_tax_exposure":     float,   # sum of |tax_diff|
            "total_dollar_exposure":  float,   # sum of |total_diff|
            "location_count":         int,
        }
    }
    """
    if not text or not text.strip():
        return {"records": [], "count": 0, "parse_warnings": ["Empty input."], "summary": {}}

    uuid_matches = list(_MSI_UUID_RE.finditer(text))
    if not uuid_matches:
        return {
            "records": [], "count": 0,
            "parse_warnings": ["No Toast order GUIDs found — is this a Monkey discrepancy report?"],
            "summary": {},
        }

    records: List[Dict] = []
    warnings: List[str] = []
    current_location: Dict = {}

    for i, uuid_m in enumerate(uuid_matches):
        uuid_str  = uuid_m.group(0)
        uuid_start = uuid_m.start()
        uuid_end   = uuid_m.end()

        # ── Text between end of previous UUID block and start of this UUID ──
        prev_end   = uuid_matches[i - 1].end() if i > 0 else 0
        inter_text = text[prev_end:uuid_start]

        # ── Location header detection ──
        loc_m = _MSI_LOC_HDR_RE.search(inter_text)
        if loc_m:
            loc_code   = loc_m.group(1).strip()
            loc_city   = loc_m.group(2).strip()
            loc_region = loc_m.group(3).strip()
            # Strip any trailing whitespace/column headers that sneak in
            loc_region = re.sub(
                r'\s*(Order\s+ID|Monkey|POS\s+Monkey|TAX|TOTAL|Message).*',
                '', loc_region, flags=re.IGNORECASE,
            ).strip()
            current_location = {
                "code":   loc_code,
                "name":   f"{loc_code} ({loc_city})",
                "region": loc_region,
            }

        # ── Order ID: last 5-7 digit integer in inter_text (before the UUID) ──
        # Use the tail of inter_text (order ID is the token right before UUID)
        tail = inter_text[-80:].strip() if len(inter_text) > 80 else inter_text.strip()
        oid_m = re.search(r'(\d{5,7})\s*$', tail)
        order_id = oid_m.group(1) if oid_m else None

        # ── Post-UUID block: everything until next UUID ──
        next_start = uuid_matches[i + 1].start() if i + 1 < len(uuid_matches) else len(text)
        post_block = text[uuid_end:next_start]

        # Strip leading "Toast" keyword
        post_block = re.sub(r'^\s*Toast\b\s*', '', post_block, flags=re.IGNORECASE).strip()

        # Extract date + time
        dt_m      = _MSI_DATETIME_RE.search(post_block)
        log_date  = dt_m.group(1)        if dt_m else None
        log_time  = dt_m.group(2).strip() if dt_m else None
        after_dt  = post_block[dt_m.end():].strip() if dt_m else post_block

        # Split into: values section | message section
        # Message starts at first "POS Tax is off", "POS Total is off", or "Monkey and POS"
        msg_anchor = re.search(
            r'\b(POS\s+Tax\s+is\s+off|POS\s+Total\s+is\s+off|Monkey\s+and\s+POS)\b',
            after_dt, re.IGNORECASE,
        )
        values_raw  = after_dt[:msg_anchor.start()].strip() if msg_anchor else ''
        message_raw = after_dt[msg_anchor.start():].strip()  if msg_anchor else after_dt.strip()

        # ── Parse numeric values section ──
        # Pattern: "{num} Up/Down {num}" (total only) or "{num} Up/Down {num} {num} Up/Down {num}"
        monkey_tax = toast_tax = monkey_total = toast_total = None
        val_tokens = values_raw.split()
        nums = [float(t) for t in val_tokens if re.match(r'^\d+\.\d+$', t)]
        dirs = [t for t in val_tokens if t in ('Up', 'Down')]

        if len(nums) == 4 and len(dirs) >= 2:
            monkey_tax, toast_tax, monkey_total, toast_total = nums[:4]
        elif len(nums) == 2 and len(dirs) >= 1:
            # 2-number block is always the totals (tax blank when no tax diff)
            monkey_total, toast_total = nums[:2]
        elif len(nums) > 0:
            warnings.append(
                f"Unexpected value count for order {order_id} (GUID …{uuid_str[-8:]}): "
                f"got {len(nums)} nums, {len(dirs)} dir tokens — raw: '{values_raw[:80]}'"
            )

        # ── Compute differences ──
        tax_diff   = round(toast_tax   - monkey_tax,   4) if (monkey_tax   is not None and toast_tax   is not None) else None
        total_diff = round(toast_total - monkey_total, 4) if (monkey_total is not None and toast_total is not None) else None

        # Override with explicit "Tax Difference:" / "Total Difference:" if present in message
        m = re.search(r'Tax\s+Difference:\s*\((\d+\.?\d*)\)', message_raw, re.I)
        if m:
            tax_diff = -round(float(m.group(1)), 4)
        else:
            m = re.search(r'Tax\s+Difference:\s*(-?\d+\.?\d*)', message_raw, re.I)
            if m:
                tax_diff = round(float(m.group(1)), 4)

        m = re.search(r'Total\s+Difference:\s*\((\d+\.?\d*)\)', message_raw, re.I)
        if m:
            total_diff = -round(float(m.group(1)), 4)
        else:
            m = re.search(r'Total\s+Difference:\s*(-?\d+\.?\d*)', message_raw, re.I)
            if m:
                total_diff = round(float(m.group(1)), 4)

        classification = _msi_classify(tax_diff, total_diff)
        records.append({
            "location_code":   current_location.get("code"),
            "location_name":   current_location.get("name"),
            "region":          current_location.get("region"),
            "monkey_order_id": order_id,
            "toast_order_guid": uuid_str,
            "log_date":  log_date,
            "log_time":  log_time,
            "monkey_tax":   monkey_tax,
            "toast_tax":    toast_tax,
            "monkey_total": monkey_total,
            "toast_total":  toast_total,
            "tax_difference":   tax_diff,
            "total_difference": total_diff,
            "message": message_raw,
            "classification":       classification,
            "classification_label": _MSI_CLASSIFICATION_LABELS.get(classification, classification),
            "severity":             _MSI_CLASSIFICATION_SEVERITY.get(classification, "medium"),
        })

    # ── Build summary ──
    by_class:    Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    total_tax_exposure   = 0.0
    total_dollar_exposure = 0.0
    location_codes: set = set()

    for r in records:
        cls = r["classification"]
        sev = r["severity"]
        by_class[cls]    = by_class.get(cls, 0) + 1
        by_severity[sev] = by_severity.get(sev, 0) + 1
        if r["tax_difference"]   is not None: total_tax_exposure    += abs(r["tax_difference"])
        if r["total_difference"] is not None: total_dollar_exposure += abs(r["total_difference"])
        if r["location_code"]: location_codes.add(r["location_code"])

    return {
        "records": records,
        "count":   len(records),
        "parse_warnings": warnings,
        "summary": {
            "by_classification":     by_class,
            "by_severity":           by_severity,
            "total_tax_exposure":    round(total_tax_exposure, 2),
            "total_dollar_exposure": round(total_dollar_exposure, 2),
            "location_count":        len(location_codes),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# MSI Order-Detail Comparison Utilities
# Parse an MSI order paste, fetch the matching Toast order, compare item prices.
# ──────────────────────────────────────────────────────────────────────────────

def _msi_extract_amount(pattern: str, text: str) -> Optional[float]:
    """Extract a dollar amount from text using a regex pattern (returns None if not found)."""
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse_msi_order_detail(text: str) -> Dict:
    """
    Parse an MSI / Monkey order-detail paste into structured items + totals.

    The paste is the copy-pasted order detail view from the Monkey portal, typically
    formatted as a flat text blob like:
        PriceAmount
        Box Lunches - Boxed Sandwiches
        5Chicken Bacon Club on Wheatberry12.5062.50
        Side Choice*5 Baked Lays
        ...
        *Total for all items ordered
        Subtotal545.00Delivery Fee:65.00Service Fee:0.00Tax (9%):54.90Total:664.90

    Returns:
        items: list of {name, qty, unit_price, extended_price}
        subtotal, delivery_fee, service_fee, tax_rate, tax_amount, gratuity, total
        parse_warnings: list of any unexpected patterns noticed
    """
    warnings_out: List[str] = []

    # ── Split at totals section ───────────────────────────────────────────────
    totals_split_re = re.compile(r'\*?\s*Total for all items ordered', re.IGNORECASE)
    parts = totals_split_re.split(text, maxsplit=1)
    items_section  = parts[0]
    totals_section = parts[1] if len(parts) > 1 else text

    # ── Parse item lines ──────────────────────────────────────────────────────
    # Pattern: {qty: 1-3 digits}{name: starts with capital, no digits/asterisks/plus}
    #          {unit_price: N.NN}{extended_price: N,NNN.NN}
    # We validate: qty * unit_price ≈ extended_price as a false-positive guard.
    #
    # Excluding '+' from the name charset is critical: MSI modifier upcharge lines
    # look like  "50 Newk's Club on Croissant+0.50+25.00" where +0.50 is the per-unit
    # upcharge and +25.00 is the total upcharge.  By stopping the name group at '+',
    # the price group (\d...) immediately sees '+' next and the whole match fails —
    # so modifier upcharge lines are never mistaken for real menu items.
    #
    # Extended prices may use comma-separated thousands (e.g. 1,950.00) so we allow
    # [\d,]+ in that group and strip commas before float().
    item_re = re.compile(
        r'\b(\d{1,3})\s*([A-Z][^0-9\r\n*+]+?)(\d{1,4}\.\d{2})\s*([\d,]{1,11}\.\d{2})',
        re.DOTALL,
    )

    items: List[Dict] = []
    used_end_positions: List[int] = []
    match_spans: List[Tuple[int, int]] = []  # (start, end) of each accepted regex match

    for m in item_re.finditer(items_section):
        qty        = int(m.group(1))
        name       = m.group(2).strip()
        unit_price = float(m.group(3))
        extended   = float(m.group(4).replace(',', ''))

        # Validate: extended ≈ qty × unit_price (within 2¢ per unit)
        expected = round(qty * unit_price, 2)
        if abs(expected - extended) > max(0.02 * qty, 0.05):
            continue

        # Skip overlapping matches
        if any(abs(m.end() - p) < 4 for p in used_end_positions):
            continue
        used_end_positions.append(m.end())

        # Clean up name: strip leading/trailing whitespace and any trailing punctuation
        name = re.sub(r'\s+', ' ', name).strip().rstrip('- ')
        if not name:
            continue

        items.append({
            "name":               name,
            "qty":                qty,
            "unit_price":         unit_price,
            "extended_price":     extended,
            "modifiers":          [],
            "effective_extended": extended,   # updated below if modifiers carry upcharges
        })
        match_spans.append((m.start(), m.end()))

    # ── Second pass: extract modifier lines from interstitial text ────────────
    # The text between item A's last price character and item B's first digit
    # may contain modifier choice blocks such as:
    #   "Side Choice*\t5\tBaked Lays"
    #   "Dessert Choice*\t5\tBig Crispy\t+0.59\t+14.75"
    #   "Sandwich Pick*\n50 Newk's Club on Croissant+0.50+25.00\n..."
    # We parse those and attach them to the preceding item as structured modifiers.
    for i, item in enumerate(items):
        inter_start  = match_spans[i][1]
        inter_end    = match_spans[i + 1][0] if i + 1 < len(match_spans) else len(items_section)
        interstitial = items_section[inter_start:inter_end]
        if interstitial.strip():
            item["modifiers"] = _parse_modifiers_from_interstitial(interstitial)
            total_mod_up = round(
                sum(cat.get("total_upcharge", 0.0) for cat in item["modifiers"]), 2
            )
            item["effective_extended"] = round(item["extended_price"] + total_mod_up, 2)

    if not items:
        warnings_out.append("No item lines found — check that the paste includes item rows before '*Total for all items ordered'")

    # ── Parse totals ──────────────────────────────────────────────────────────
    subtotal     = _msi_extract_amount(r'Subtotal[:\s]*(\d+\.\d{2})', totals_section)
    delivery_fee = _msi_extract_amount(r'Delivery\s+Fee[:\s]*(\d+\.\d{2})', totals_section) or 0.0
    service_fee  = _msi_extract_amount(r'Service\s+Fee[:\s]*(\d+\.\d{2})', totals_section) or 0.0
    gratuity     = _msi_extract_amount(r'Gratuity[:\s]*(\d+\.\d{2})', totals_section) or 0.0

    # Tax: "Tax (9%):54.90" or "Tax:54.90"
    tax_match  = re.search(r'Tax\s*\((\d+(?:\.\d+)?)%\)[:\s]*(\d+\.\d{2})', totals_section, re.IGNORECASE)
    tax_rate   = float(tax_match.group(1)) if tax_match else None
    tax_amount = float(tax_match.group(2)) if tax_match else _msi_extract_amount(r'\bTax[:\s]*(\d+\.\d{2})', totals_section)

    # Grand total: match "Total:664.90" but NOT "Subtotal"
    grand_total = _msi_extract_amount(r'(?<![a-zA-Z])Total[:\s]*(\d+\.\d{2})', totals_section)

    return {
        "items":        items,
        "item_count":   len(items),
        "subtotal":     subtotal,
        "delivery_fee": delivery_fee,
        "service_fee":  service_fee,
        "tax_rate":     tax_rate,
        "tax_amount":   tax_amount,
        "gratuity":     gratuity,
        "total":        grand_total,
        "parse_warnings": warnings_out,
    }


def _parse_modifiers_from_interstitial(text: str) -> List[Dict]:
    """
    Parse modifier choice lines from the interstitial text that appears between
    two consecutive top-level MSI item lines in an order-detail paste.

    Handles three MSI formats:
      Format A — inline tab-separated (category + choice on the same line):
        "Side Choice*\\t5\\tBaked Lays"
        "Dessert Choice*\\t5\\tBig Crispy\\t+0.59\\t+14.75"

      Format B — category header line followed by individual choice lines:
        "Sandwich Pick*"
        "50 Newk's Club on Croissant+0.50+25.00"
        "50 Chicken Salad on Croissant"

      Format C — concatenated (no newlines), common when copying from browser:
        "Dressing Choice*25 CaesarWould you like to add a protein?*25 All-Natural Chicken (4 oz.)+4.00+100.00Dessert Choice*..."

    Returns:
        List of {category, choices: [{name, qty, upcharge_per_unit, total_upcharge}],
                 total_upcharge}
    """
    # ── Normalize line structure ─────────────────────────────────────────────
    # MSI copy-paste may arrive as a single long concatenated string (Format C)
    # or as properly newline-separated text (Formats A/B).  Insert newlines at
    # natural boundaries so the existing line-by-line patterns work for all formats.
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # (a) Insert \n before category headers not already at line start.
    #     A header is alpha text (no digits in body) ending with *.
    #     Use a lookbehind so we only insert when the header is not already
    #     at the start of a line (i.e. preceded by a non-whitespace/non-* char).
    text = re.sub(
        r'(?<=[^*\n\s])([A-Za-z][A-Za-z\s?,:()+.-]*\*)',
        r'\n\1', text,
    )
    # (b) Insert \n after * not already followed by \n (put header on its own line).
    text = re.sub(r'\*(?!\n)', r'*\n', text)

    # (c) Insert \n before choice lines (qty + space + Capital) not at line start:
    #     Case 1 — preceded by a letter or symbol (e.g. "CookieIncluded" seam).
    text = re.sub(r'(?<!\n)(?<![0-9])(\d{1,3}[\t ]+[A-Z])', r'\n\1', text)
    #     Case 2 — immediately follows the end of a price value (X.XX + qty),
    #     e.g. "+5.905 Brownie" → "+5.90\n5 Brownie"
    text = re.sub(r'(?<=\.\d\d)(\d{1,3}[\t ]+[A-Z])', r'\n\1', text)

    categories: Dict[str, Dict] = {}
    current_category: Optional[str] = None

    # Format A: "Category Name*\tqty\tChoice[\t+upcharge\t+total]"
    inline_re  = re.compile(
        r'^([A-Za-z][^\t\n\r*]+?)\*\s*\t(\d+)\t([^\t\n\r+]+?)(?:\s*\t\+([0-9.]+)\s*\t\+([0-9.]+))?\s*$'
    )
    # Category-only header: "Side Choice*"  (no tabs / prices on the same line)
    header_re  = re.compile(r'^([A-Za-z][^\n\r*]+?)\*\s*$')
    # Choice with upcharge  (concat or tab-sep format):
    #   "50 Newk's Club on Croissant+0.50+25.00"
    #   "25 All-Natural Chicken+4.00+100.00"
    #   "25\tAll-Natural Chicken\t+4.00\t+100.00"
    choice_up_re = re.compile(r'^(\d+)\s+([A-Z][^+\n\r\t]+?)\s*\+([0-9.]+)[\t ]*\+([0-9.]+)\s*$')
    # Choice without upcharge: "50\tChicken Salad on Croissant"  or  "50 Baked Lays"
    choice_no_re = re.compile(r'^(\d+)[\t ]+([A-Z][^\n\r\t+0-9]+?)\s*$')

    def _add(cat_name: str, name: str, qty: int, upcharge: float, total: float) -> None:
        if cat_name not in categories:
            categories[cat_name] = {"category": cat_name, "choices": [], "total_upcharge": 0.0}
        categories[cat_name]["choices"].append({
            "name": name, "qty": qty,
            "upcharge_per_unit": upcharge, "total_upcharge": total,
        })
        categories[cat_name]["total_upcharge"] = round(
            categories[cat_name]["total_upcharge"] + total, 2
        )

    for line in text.split('\n'):
        line = line.rstrip('\r')
        if not line.strip():
            continue

        # Try Format A (inline tab-separated)
        m = inline_re.match(line)
        if m:
            _add(m.group(1).strip(), m.group(3).strip(), int(m.group(2)),
                 float(m.group(4)) if m.group(4) else 0.0,
                 float(m.group(5)) if m.group(5) else 0.0)
            current_category = None   # inline line resets state-machine mode
            continue

        # Try category-only header: "Sandwich Pick*"
        m = header_re.match(line)
        if m and not line.lstrip()[:1].isdigit():
            current_category = m.group(1).strip()
            if current_category not in categories:
                categories[current_category] = {
                    "category": current_category, "choices": [], "total_upcharge": 0.0
                }
            continue

        if current_category:
            # Choice with upcharge: "50 Newk's Club on Croissant+0.50+25.00"
            m = choice_up_re.match(line)
            if m:
                _add(current_category, m.group(2).strip(), int(m.group(1)),
                     float(m.group(3)), float(m.group(4)))
                continue
            # Choice without upcharge: "50 Chicken Salad on Croissant"
            m = choice_no_re.match(line)
            if m:
                _add(current_category, m.group(2).strip(), int(m.group(1)), 0.0, 0.0)
                continue
            # Non-matching non-empty line → exit the modifier block
            if line.strip() and not line.strip().startswith('*'):
                current_category = None

    return list(categories.values())


def _normalize_msi_items(
    items: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """
    Expand MSI grouped items into unit-level derived items using parsed modifier data.

    For each MSI item:
      • No modifiers            → passed through unchanged.
      • Has a "primary" modifier (choices whose quantities sum to the parent qty)
                                → exploded: one derived item per choice with
                                  final_unit_price = base_price + choice_upcharge
      • Secondary modifiers with upcharges (e.g. dessert pools) → returned as
        modifier_pools for separate dollar accounting.
      • Has modifiers but none is primary → single blended item with average upcharge.

    Returns:
        derived_items  — [{name, parent_name, choice_name, qty, base_price,
                           modifier_upcharge, final_unit_price, extended_price, source}]
        modifier_pools — [{description, total_amount, parent_item}]
    """
    derived: List[Dict] = []
    pools:   List[Dict] = []

    for item in items:
        mods       = item.get("modifiers", [])
        parent_qty = item["qty"]

        if not mods:
            derived.append({
                "name":               item["name"],
                "parent_name":        item["name"],
                "choice_name":        None,
                "qty":                parent_qty,
                "base_price":         item["unit_price"],
                "modifier_upcharge":  0.0,
                "final_unit_price":   item["unit_price"],
                "extended_price":     item["extended_price"],
                "source":             "direct",
            })
            continue

        # Find primary modifier: the category whose choice-qty sum equals parent qty
        primary_cat: Optional[Dict] = None
        for cat in mods:
            if cat["choices"] and abs(sum(c["qty"] for c in cat["choices"]) - parent_qty) <= 1:
                primary_cat = cat
                break

        if primary_cat is None:
            # Blend all upcharges into a single average per-unit amount
            total_up = round(sum(cat.get("total_upcharge", 0.0) for cat in mods), 2)
            avg_up   = round(total_up / parent_qty, 4) if parent_qty else 0.0
            derived.append({
                "name":               item["name"],
                "parent_name":        item["name"],
                "choice_name":        None,
                "qty":                parent_qty,
                "base_price":         item["unit_price"],
                "modifier_upcharge":  avg_up,
                "final_unit_price":   round(item["unit_price"] + avg_up, 2),
                "extended_price":     round(item.get("effective_extended", item["extended_price"]), 2),
                "source":             "blended",
            })
            continue

        secondary_cats = [c for c in mods if c is not primary_cat]

        # One derived item per primary modifier choice
        for choice in primary_cat["choices"]:
            derived.append({
                "name":               f"{item['name']} / {choice['name']}",
                "parent_name":        item["name"],
                "choice_name":        choice["name"],
                "qty":                choice["qty"],
                "base_price":         item["unit_price"],
                "modifier_upcharge":  choice["upcharge_per_unit"],
                "final_unit_price":   round(item["unit_price"] + choice["upcharge_per_unit"], 2),
                "extended_price":     round(
                    choice["qty"] * (item["unit_price"] + choice["upcharge_per_unit"]), 2
                ),
                "source":             "exploded",
            })

        # Secondary modifier upcharges become modifier pools (e.g. dessert upcharges)
        for cat in secondary_cats:
            if cat.get("total_upcharge", 0.0) > 0.001:
                up_choices = [
                    f"{c['name']} ×{c['qty']}"
                    for c in cat["choices"]
                    if c["upcharge_per_unit"] > 0
                ]
                pools.append({
                    "description": (
                        f"{item['name']} — {cat['category']} upcharges"
                        + (f" ({', '.join(up_choices)})" if up_choices else "")
                    ),
                    "total_amount": cat["total_upcharge"],
                    "parent_item":  item["name"],
                })

    return derived, pools


async def _fetch_single_toast_order(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    order_guid: str,
) -> Dict:
    """Fetch a single Toast order by its GUID using the Orders API."""
    standard_headers = await get_standard_api_auth_headers()
    headers = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid,
    }
    url  = f"{TOAST_ORDERS_API_BASE_URL}/orders/v2/orders/{order_guid}"
    resp = await client.get(url, headers=headers, timeout=30.0)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Toast Orders API error fetching order {order_guid}: {resp.text[:400]}",
        )
    return resp.json()


def _extract_toast_selections(order: Dict) -> List[Dict]:
    """
    Extract top-level (non-voided) item selections from a Toast order object,
    grouped by (name, unit_price).

    Toast catering orders often return one selection per item (qty=1 each) rather
    than a single selection with qty=N, so we group them here before comparison.

    Each returned dict: {name, qty, unit_price, extended_price, guid, modifiers, applied_discounts}
    """
    raw: List[Dict] = []
    for check in order.get("checks", []):
        if check.get("voided"):
            continue
        for sel in check.get("selections", []):
            if sel.get("voided"):
                continue
            name = (
                sel.get("displayName")
                or (sel.get("item") or {}).get("name")
                or ""
            ).strip()
            if not name:
                continue
            qty        = float(sel.get("quantity") or 1)
            unit_price = float(sel.get("price") or 0)
            raw.append({
                "name":           name,
                "qty":            qty,
                "unit_price":     unit_price,
                "extended_price": round(qty * unit_price, 2),
                "guid":           sel.get("guid", ""),
                "modifiers": [
                    (m.get("displayName") or (m.get("item") or {}).get("name") or "")
                    for m in sel.get("modifiers", [])
                    if (m.get("displayName") or (m.get("item") or {}).get("name"))
                ],
                "applied_discounts": [
                    (d.get("discount") or {}).get("name", "")
                    for d in sel.get("appliedDiscounts", [])
                ],
            })

    # Group by (name, unit_price) so catering orders with qty=1 per line are consolidated
    grouped: Dict[tuple, Dict] = {}
    for item in raw:
        key = (item["name"].lower(), item["unit_price"])
        if key in grouped:
            grouped[key]["qty"]            += item["qty"]
            grouped[key]["extended_price"]  = round(grouped[key]["qty"] * grouped[key]["unit_price"], 2)
            grouped[key]["modifiers"]       = list(set(grouped[key]["modifiers"] + item["modifiers"]))
        else:
            grouped[key] = dict(item)

    return list(grouped.values())


def _normalize_item_name(name: str) -> str:
    """
    Lowercase, strip punctuation, collapse whitespace — used for fuzzy name matching.
    Also strips Toast venue/service-mode suffixes like '(Catering)', '(Dine In)', etc.
    that appear in Toast item names but not in MSI names.
    Also strips MSI-specific markers like '(Bulk)' and leading 'Custom ' prefix so
    that e.g. 'Custom Classic Box (Bulk)' aligns with 'Box Classic Sandwich'.
    """
    n = name.lower()
    # Remove Toast service-mode suffixes in parentheses
    n = re.sub(
        r'\s*\((catering|dine[- ]?in|to[- ]?go|drive[- ]?thru|take[- ]?out|delivery|carry[- ]?out)\)',
        '', n, flags=re.IGNORECASE,
    )
    # Remove MSI bulk/modifier indicator suffixes
    n = re.sub(r'\s*\(bulk\)', '', n, flags=re.IGNORECASE)
    # Strip MSI "Custom" prefix (e.g. "Custom Classic Box" → "Classic Box")
    n = re.sub(r'^custom\s+', '', n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _name_similarity(a: str, b: str) -> float:
    """
    Similarity score [0.0–1.0] between two normalised item names.

    Scoring tiers:
      1.0  — exact match
      0.85 — one is a substring of the other
      0.80 — all words of the shorter name appear in the longer name
              (handles "Works Sweet" ↔ "Gallon of Sweet Tea with The Works",
               "Club Wrap" ↔ "Box Club Classic Wrap", etc.)
      else — Jaccard word-overlap ratio
    """
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    # All words of the shorter name contained in the longer → strong partial match
    shorter, longer = (words_a, words_b) if len(words_a) <= len(words_b) else (words_b, words_a)
    if shorter.issubset(longer):
        return 0.80
    return len(words_a & words_b) / len(words_a | words_b)


def _compare_order_items(
    derived_items: List[Dict],
    toast_items: List[Dict],
    modifier_pools: List[Dict],
    msi_total_diff: float,
    msi_tax_diff: float,
    msi_raw_items: Optional[List[Dict]] = None,
) -> Dict:
    """
    Match MSI item GROUPS to Toast item name groups and compare effective totals.

    Phase 1 — Group MSI derived items by parent_name; group Toast items by normalized name.
    Phase 2 — Match each MSI parent group to the best-scoring Toast name group (one-to-one
               at the group level, so all Toast price-tiers for 'Box Caesar' are captured
               together rather than split across multiple one-to-one item pairings).
    Phase 3 — Compare group-level totals:
               MSI effective total = sum(final_unit_price × qty for each derived item)
                                   + sum of secondary modifier pool amounts for this parent
               Toast total         = sum(unit_price × qty) for all Toast items in the group
               group_diff          = toast_total − msi_effective_total
    Phase 4 — Within each matched group, do best-effort per-unit detail matching for display.

    This approach correctly handles multi-level modifier catering orders where a single MSI
    parent item (e.g. 'Custom Classic Box (Bulk)' or 'Caesar') explodes into multiple
    price tiers via protein × dessert combinations that Toast expresses as individual
    line items — whereas the old one-to-one approach would leave most of them unmatched.

    explained_diff = sum of group_diff values for flagged groups (no separate pool addition,
    since pool amounts are already included in each group's msi_effective_total).
    """
    # Build indexed Toast pool with normalized names
    toast_pool = [
        {
            "idx":        i,
            "name":       t["name"],
            "norm":       _normalize_item_name(t["name"]),
            "qty":        t["qty"],
            "unit_price": t["unit_price"],
        }
        for i, t in enumerate(toast_items)
    ]

    # Group Toast items by normalized name (captures all price tiers under one key)
    toast_by_norm: Dict[str, List[Dict]] = {}
    for t in toast_pool:
        toast_by_norm.setdefault(t["norm"], []).append(t)

    # Group MSI derived items by parent_name
    msi_by_parent: Dict[str, List[Dict]] = {}
    for msi in derived_items:
        msi_by_parent.setdefault(msi["parent_name"], []).append(msi)

    # Sum modifier pool amounts per parent item
    pool_by_parent: Dict[str, float] = {}
    for p in modifier_pools:
        k = p["parent_item"]
        pool_by_parent[k] = round(pool_by_parent.get(k, 0.0) + p["total_amount"], 2)

    # Build a lookup of authoritative effective totals from the original parsed MSI items.
    # These are pre-computed by parse_msi_order_detail as extended_price + all modifier
    # total_upcharges — so they are correct even when the modifier regex parsing fails.
    # IMPORTANT: SUM across all raw items with the same name (MSI may list the same item
    # twice if it appears in multiple sections, e.g. 25×Caesar and 10×Caesar separately).
    msi_eff_by_name: Dict[str, float] = {}
    if msi_raw_items:
        for raw in msi_raw_items:
            name = raw.get("name", "")
            eff  = raw.get("effective_extended")
            if name and eff is not None:
                msi_eff_by_name[name] = round(
                    msi_eff_by_name.get(name, 0.0) + float(eff), 2
                )

    matches:             List[Dict] = []
    unmatched_msi:       List[Dict] = []
    used_toast_norms:    set        = set()
    all_used_toast_idxs: set        = set()

    for parent_name, msi_group in msi_by_parent.items():
        parent_norm = _normalize_item_name(parent_name)

        # Find best-matching Toast name group
        best_norm, best_score = None, 0.0
        for tnorm in toast_by_norm:
            if tnorm in used_toast_norms:
                continue
            score = _name_similarity(parent_norm, tnorm)
            if score > best_score:
                best_score, best_norm = score, tnorm

        # MSI effective total for this parent group.
        # Prefer the authoritative value from the original parsed item (effective_extended),
        # which is pre-computed as extended_price + all modifier upcharges.  Fall back to
        # recomputing from derived item unit prices + secondary modifier pools.
        msi_total_qty = sum(m["qty"] for m in msi_group)
        if parent_name in msi_eff_by_name:
            msi_eff_total = msi_eff_by_name[parent_name]
        else:
            msi_eff_total = round(
                sum(m["final_unit_price"] * m["qty"] for m in msi_group)
                + pool_by_parent.get(parent_name, 0.0),
                2,
            )

        if best_norm and best_score >= 0.45:
            used_toast_norms.add(best_norm)
            tgroup      = toast_by_norm[best_norm]
            toast_total = round(sum(t["unit_price"] * t["qty"] for t in tgroup), 2)
            toast_qty   = int(sum(t["qty"] for t in tgroup))
            group_diff  = round(toast_total - msi_eff_total, 2)

            for t in tgroup:
                all_used_toast_idxs.add(t["idx"])

            # Phase 4: best-effort per-unit detail matching within the group (for display)
            used_in_group: set        = set()
            detail_rows:   List[Dict] = []
            for msi in msi_group:
                best_tidx, best_pdist = None, float("inf")
                for t in tgroup:
                    if t["idx"] in used_in_group:
                        continue
                    pdist = abs(t["unit_price"] - msi["final_unit_price"])
                    if pdist < best_pdist:
                        best_pdist, best_tidx = pdist, t["idx"]

                if best_tidx is not None:
                    used_in_group.add(best_tidx)
                    tt         = next(t for t in tgroup if t["idx"] == best_tidx)
                    price_diff = round(tt["unit_price"] - msi["final_unit_price"], 4)
                    detail_rows.append({
                        "msi_name":              msi["name"],
                        "msi_parent_name":       msi["parent_name"],
                        "msi_choice_name":       msi["choice_name"],
                        "toast_name":            tt["name"],
                        "msi_qty":               msi["qty"],
                        "toast_qty":             int(tt["qty"]),
                        "msi_base_price":        msi["base_price"],
                        "msi_modifier_upcharge": msi["modifier_upcharge"],
                        "msi_unit_price":        msi["final_unit_price"],
                        "toast_unit_price":      tt["unit_price"],
                        "price_diff":            price_diff,
                        "qty_diff":              int(tt["qty"]) - msi["qty"],
                        "source":                msi.get("source", "direct"),
                    })

            group_flagged = abs(group_diff) > 0.005
            matches.append({
                "msi_name":              parent_name,
                "msi_parent_name":       parent_name,
                "msi_choice_name":       None,
                "toast_name":            tgroup[0]["name"],
                "match_score":           round(min(best_score, 1.0), 2),
                "msi_qty":               msi_total_qty,
                "toast_qty":             toast_qty,
                "msi_base_price":        None,
                "msi_modifier_upcharge": pool_by_parent.get(parent_name, 0.0),
                "msi_unit_price":        None,
                "toast_unit_price":      None,
                "msi_eff_total":         msi_eff_total,
                "toast_total":           toast_total,
                "price_diff":            round(group_diff / msi_total_qty, 4) if msi_total_qty else 0.0,
                "qty_diff":              toast_qty - msi_total_qty,
                "impact":                group_diff,
                "flagged":               group_flagged,
                "cause":                 "price_mismatch" if group_flagged else "match",
                "source":                "group",
                "detail_rows":           detail_rows,
                "pool_amount":           pool_by_parent.get(parent_name, 0.0),
            })
        else:
            for msi in msi_group:
                unmatched_msi.append({
                    "name":               msi["name"],
                    "parent_name":        msi["parent_name"],
                    "choice_name":        msi["choice_name"],
                    "qty":                msi["qty"],
                    "unit_price":         msi["final_unit_price"],
                    "base_price":         msi["base_price"],
                    "modifier_upcharge":  msi["modifier_upcharge"],
                    "source":             msi.get("source", "direct"),
                })

    unmatched_toast = [
        {
            "name":       toast_items[t["idx"]]["name"],
            "qty":        t["qty"],
            "unit_price": t["unit_price"],
        }
        for t in toast_pool
        if t["idx"] not in all_used_toast_idxs and t["unit_price"] > 0
    ]

    flagged = sorted(
        [m for m in matches if m["flagged"]],
        key=lambda x: abs(x["impact"]),
        reverse=True,
    )

    pool_total      = round(sum(p["total_amount"] for p in modifier_pools), 2)
    # explained_diff is the sum of group-level diffs for flagged groups.
    # Pool amounts are already included in each group's msi_eff_total, so they are NOT
    # added separately here (doing so would double-count them).
    explained_diff  = round(sum(f["impact"] for f in flagged), 2)
    unexplained     = round(msi_total_diff - explained_diff, 2)
    fully_explained = abs(unexplained) < 0.03

    return {
        "matches":          matches,
        "flagged":          flagged,
        "unmatched_msi":    unmatched_msi,
        "unmatched_toast":  unmatched_toast,
        "modifier_pools":   modifier_pools,
        "pool_total":       pool_total,
        "explained_diff":   explained_diff,
        "unexplained_diff": unexplained,
        "fully_explained":  fully_explained,
        "primary_cause":    flagged[0] if flagged else None,
    }


# ------------------
# FastAPI App
# ------------------

app = FastAPI(title="Newk's Custom Reporting Platform")
app.include_router(pick3_router)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------
# APScheduler — nightly orders cache job
# ------------------

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = AsyncIOScheduler()
_nightly_job_last_result: Dict[str, Any] = {}  # Track last run result for admin visibility


# ──────────────────────────────────────────────────────────────────────────────
# Reconciliation tunables (env-overridable, sane defaults)
#
# Toast lets restaurants amend/void orders for past business dates (a refund
# issued 3 days late, an item added to a check after closeout, etc.). The
# nightly pull used to skip already-cached dates, so those amendments never
# made it into our local DB — historical reports drifted out of sync.
#
# The new behavior re-pulls the last N days on every nightly run. Each pull
# computes an aggregate snapshot (net sales, check count, voided count) and
# compares against the previous snapshot. If the delta crosses one of the
# thresholds below, the change is collected and an email summary fires at
# the end of the job.
# ──────────────────────────────────────────────────────────────────────────────

RECONCILE_WINDOW_DAYS = int(os.getenv("RECONCILE_WINDOW_DAYS", "7"))
RECONCILE_NET_SALES_THRESHOLD_USD = float(os.getenv("RECONCILE_NET_SALES_THRESHOLD_USD", "50"))
RECONCILE_CHECK_COUNT_THRESHOLD = int(os.getenv("RECONCILE_CHECK_COUNT_THRESHOLD", "2"))
RECONCILE_ALERT_RECIPIENTS = [
    addr.strip()
    for addr in (os.getenv("RECONCILE_ALERT_RECIPIENTS", "") or "").split(",")
    if addr.strip()
]


def _is_large_reconciliation_change(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """
    Return True if the delta between two aggregate snapshots crosses any
    configured reconciliation threshold (net-sales $ change OR check-count
    change OR voided-check-count change).
    """
    if not before or not after:
        return False
    net_delta = abs((after.get("net_sales", 0) or 0) - (before.get("net_sales", 0) or 0))
    if net_delta >= RECONCILE_NET_SALES_THRESHOLD_USD:
        return True
    check_delta = abs((after.get("check_count", 0) or 0) - (before.get("check_count", 0) or 0))
    if check_delta >= RECONCILE_CHECK_COUNT_THRESHOLD:
        return True
    voided_delta = abs((after.get("voided_count", 0) or 0) - (before.get("voided_count", 0) or 0))
    if voided_delta >= RECONCILE_CHECK_COUNT_THRESHOLD:
        return True
    return False


async def nightly_orders_cache_job():
    """
    Pull yesterday's orders + reconcile the prior RECONCILE_WINDOW_DAYS-1 days.

    Reconciliation strategy:
      - For yesterday: pull if not already cached (the day's first ingest).
      - For day-2 through day-N: ALWAYS re-pull, even if the date is already
        cached. The store_orders() function is delete-then-insert, so the
        local DB ends up mirroring Toast's current state. Aggregate snapshots
        before/after the re-pull surface "large change" amendments — those
        get emailed as a batched summary at the end of the run.

    Skipped dates (already cached + within window but pull failed earlier)
    are NOT skipped by the reconcile loop — that's the whole point.
    """
    today = date.today()
    yesterday_date = today - timedelta(days=1)
    yesterday = yesterday_date.strftime("%Y%m%d")
    window_dates: List[date] = [yesterday_date - timedelta(days=i) for i in range(RECONCILE_WINDOW_DAYS)]
    window_str = (
        f"{window_dates[-1].isoformat()}..{window_dates[0].isoformat()}"
        if window_dates else "(empty)"
    )
    logger.info(
        "Nightly orders cache + reconcile started · window=%s · thresholds: $%.2f net, %d checks/voids",
        window_str, RECONCILE_NET_SALES_THRESHOLD_USD, RECONCILE_CHECK_COUNT_THRESHOLD,
    )

    success_count = 0
    fail_count = 0
    skipped_count = 0
    reconciled_count = 0
    changes: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            locations = await get_all_active_locations(client)
            total = len(locations)

            for i, loc in enumerate(locations, 1):
                rguid = loc.restaurantGuid
                rname = loc.restaurantName

                for window_idx, bdate_dt in enumerate(window_dates):
                    bdate = bdate_dt.strftime("%Y%m%d")
                    is_yesterday = (window_idx == 0)

                    # Yesterday: skip if already cached (idempotent across reruns).
                    # Older dates: ALWAYS re-pull to capture amendments.
                    if is_yesterday and toast_orders_db.is_date_cached(rguid, bdate):
                        skipped_count += 1
                        continue

                    # Snapshot the existing aggregate before the re-pull so we
                    # can diff against the post-pull aggregate. Yesterday's
                    # first pull has no "before" — that's fine, no change to
                    # detect.
                    before_agg = (
                        toast_orders_db.get_aggregate(rguid, bdate)
                        if not is_yesterday else None
                    )

                    try:
                        toast_orders_db.mark_import_started(rguid, bdate)
                        orders = await _fetch_orders_from_toast_api(client, rguid, bdate)
                        toast_orders_db.store_orders(rguid, bdate, orders)
                        after_agg = toast_orders_db.compute_aggregates(orders)
                        toast_orders_db.save_aggregate(rguid, bdate, after_agg)

                        if is_yesterday:
                            success_count += 1
                            logger.info(
                                "Nightly cache: %s %s (%d/%d) — %d orders",
                                rguid[:8], bdate, i, total, len(orders),
                            )
                        else:
                            reconciled_count += 1
                            if before_agg and _is_large_reconciliation_change(before_agg, after_agg):
                                changes.append({
                                    "restaurant_name":     rname,
                                    "restaurant_guid":     rguid,
                                    "business_date":       bdate,
                                    "net_sales_before":    before_agg.get("net_sales", 0),
                                    "net_sales_after":     after_agg.get("net_sales", 0),
                                    "net_sales_delta":     round(
                                        after_agg.get("net_sales", 0) - before_agg.get("net_sales", 0), 2
                                    ),
                                    "check_count_before":  before_agg.get("check_count", 0),
                                    "check_count_after":   after_agg.get("check_count", 0),
                                    "voided_before":       before_agg.get("voided_count", 0),
                                    "voided_after":        after_agg.get("voided_count", 0),
                                    "voided_delta":        (
                                        after_agg.get("voided_count", 0) - before_agg.get("voided_count", 0)
                                    ),
                                })
                                logger.info(
                                    "Reconcile change: %s %s · net $%.2f → $%.2f (Δ $%+.2f) · checks %d → %d",
                                    rname, bdate,
                                    before_agg.get("net_sales", 0), after_agg.get("net_sales", 0),
                                    after_agg.get("net_sales", 0) - before_agg.get("net_sales", 0),
                                    before_agg.get("check_count", 0), after_agg.get("check_count", 0),
                                )
                        await asyncio.sleep(0.3)  # Rate limit courtesy
                    except Exception as exc:
                        fail_count += 1
                        toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                        logger.error("Nightly pull failed for %s %s: %s", rguid[:8], bdate, exc)
    except Exception as exc:
        logger.error("Nightly orders cache job failed: %s", exc)

    # Sort changes by absolute net-sales delta descending so the email
    # surfaces the biggest amendments first.
    changes.sort(key=lambda c: abs(c.get("net_sales_delta", 0)), reverse=True)

    _nightly_job_last_result.update({
        "business_date":         yesterday,
        "window_days":           RECONCILE_WINDOW_DAYS,
        "window":                window_str,
        "success":               success_count,    # yesterday's first-time imports
        "failed":                fail_count,
        "skipped":               skipped_count,    # yesterday already cached
        "reconciled":            reconciled_count, # re-pulls of older days
        "changes_detected":      len(changes),
        "alert_sent":            False,
        "completed_at":          datetime.now().isoformat(),
    })
    logger.info(
        "Nightly orders cache + reconcile completed: %d new, %d reconciled, %d failed, %d skipped, %d changes",
        success_count, reconciled_count, fail_count, skipped_count, len(changes),
    )

    # ── Fire the large-change alert email (best-effort, never raise) ──
    if changes and RECONCILE_ALERT_RECIPIENTS:
        try:
            from email_service import send_reconciliation_alert_email
            sent = await asyncio.to_thread(
                send_reconciliation_alert_email,
                changes,
                RECONCILE_ALERT_RECIPIENTS,
                RECONCILE_WINDOW_DAYS,
                window_str,
            )
            _nightly_job_last_result["alert_sent"] = bool(sent)
        except Exception as exc:
            logger.error("Failed to send reconciliation alert email: %s", exc, exc_info=True)
    elif changes:
        logger.warning(
            "Reconcile detected %d large changes but RECONCILE_ALERT_RECIPIENTS is empty — no email sent",
            len(changes),
        )

    # ── Auto-retry previously failed imports ──
    try:
        failed_imports = toast_orders_db.get_failed_imports()
        if failed_imports:
            retry_count = 0
            retry_success = 0
            max_retries = 50  # Cap per nightly run
            logger.info("Nightly orders cache: retrying %d previously failed imports (max %d)",
                        len(failed_imports), max_retries)
            async with httpx.AsyncClient(timeout=120.0) as retry_client:
                for fi in failed_imports[:max_retries]:
                    rguid = fi.get("restaurant_guid", "")
                    bdate = fi.get("business_date", "")
                    if not rguid or not bdate:
                        continue
                    retry_count += 1
                    try:
                        orders = await _fetch_orders_from_toast_api(retry_client, rguid, bdate)
                        toast_orders_db.store_orders(rguid, bdate, orders)
                        # Snapshot the aggregate so future reconcile runs can
                        # detect amendments on this date too.
                        toast_orders_db.save_aggregate(
                            rguid, bdate, toast_orders_db.compute_aggregates(orders),
                        )
                        retry_success += 1
                        await asyncio.sleep(0.5)
                    except Exception as retry_exc:
                        logger.debug("Retry still failing for %s|%s: %s", rguid[:8], bdate, retry_exc)
            if retry_count > 0:
                logger.info("Nightly auto-retry: %d/%d succeeded", retry_success, retry_count)
                _nightly_job_last_result["retried"] = retry_count
                _nightly_job_last_result["retry_success"] = retry_success
    except Exception as retry_exc:
        logger.warning("Nightly auto-retry failed: %s", retry_exc)


# Schedule: run at 3:00 AM daily (after Toast closeout typically settles)
_scheduler.add_job(nightly_orders_cache_job, CronTrigger(hour=3, minute=0),
                   id="nightly_orders_cache", replace_existing=True)


# ------------------
# Nightly Historical Orders Backfill — fills in older dates from Toast Orders API
# Runs 3:15 AM–8:00 AM daily, picks up where it left off, processes ~100-150 days per night
# ------------------
_BACKFILL_TARGET_START = "2024-01-01"  # How far back to go
_BACKFILL_CUTOFF_HOUR = 8  # Stop at 8:00 AM to avoid daytime API load
_backfill_status: Dict[str, Any] = {}

async def nightly_historical_backfill():
    """
    Backfill orders.db with historical data, working backwards from the earliest
    cached date. Runs until 8 AM, then stops and resumes the next night.
    """
    from datetime import datetime as dt
    global _backfill_status

    target_start = date.fromisoformat(_BACKFILL_TARGET_START)

    # Find earliest date currently in orders.db
    stats = toast_orders_db.get_stats()
    min_cached = stats.get("min_date", "")
    if min_cached:
        # min_date is YYYYMMDD format
        earliest_cached = date(int(min_cached[:4]), int(min_cached[4:6]), int(min_cached[6:8]))
    else:
        earliest_cached = date.today() - timedelta(days=1)

    # Work backwards from day before earliest cached
    backfill_end = earliest_cached - timedelta(days=1)
    if backfill_end < target_start:
        logger.info("Historical backfill complete — already have data back to %s", target_start)
        _backfill_status = {"status": "complete", "earliest": target_start.isoformat()}
        return

    days_remaining = (backfill_end - target_start).days + 1
    logger.info("Historical backfill starting: %s to %s (%d days remaining)",
                target_start, backfill_end, days_remaining)

    _backfill_status = {
        "status": "running",
        "target_start": target_start.isoformat(),
        "current_end": backfill_end.isoformat(),
        "days_remaining": days_remaining,
        "days_done_tonight": 0,
        "started_at": dt.now().isoformat(),
    }

    days_done = 0
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            locations = await get_all_active_locations(client)
            guids = [loc.restaurantGuid for loc in locations]

            # Process one day at a time, working backwards
            current_date = backfill_end
            while current_date >= target_start:
                # Check if we should stop (8 AM cutoff)
                if dt.now().hour >= _BACKFILL_CUTOFF_HOUR:
                    logger.info("Historical backfill pausing at 8 AM — done %d days tonight, will resume tomorrow. Next date: %s",
                                days_done, current_date)
                    _backfill_status.update({
                        "status": "paused",
                        "days_done_tonight": days_done,
                        "next_date": current_date.isoformat(),
                    })
                    return

                bdate = current_date.strftime("%Y%m%d")
                day_success = 0
                day_fail = 0

                for rguid in guids:
                    if toast_orders_db.is_date_cached(rguid, bdate):
                        continue
                    try:
                        toast_orders_db.mark_import_started(rguid, bdate)
                        orders = await _fetch_orders_from_toast_api(client, rguid, bdate)
                        toast_orders_db.store_orders(rguid, bdate, orders)
                        # Snapshot the aggregate so reconciliation has a baseline
                        # if the window is ever widened to include this date.
                        toast_orders_db.save_aggregate(
                            rguid, bdate, toast_orders_db.compute_aggregates(orders),
                        )
                        day_success += 1
                        await asyncio.sleep(0.3)
                    except Exception as exc:
                        toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                        day_fail += 1

                days_done += 1
                if days_done % 5 == 0:
                    logger.info("Historical backfill progress: %d days done, current date: %s", days_done, current_date)
                _backfill_status["days_done_tonight"] = days_done
                _backfill_status["current_date"] = current_date.isoformat()

                current_date -= timedelta(days=1)

    except Exception as exc:
        logger.error("Historical backfill error: %s", exc)
        _backfill_status.update({"status": "error", "error": str(exc)})
        return

    logger.info("Historical backfill COMPLETE — all %d days processed back to %s", days_done, target_start)
    _backfill_status.update({"status": "complete", "days_done_tonight": days_done})


_scheduler.add_job(nightly_historical_backfill, CronTrigger(hour=3, minute=15),
                   id="nightly_historical_backfill", replace_existing=True)


# ------------------
# Nightly Flash Sales Import — Toast ERA API → aloha_sales_db
# Replaces Don Wilson's SQL Server ETL pipeline entirely.
# ------------------

# Granular mapping: ERA dining option → aloha_sales_db column
# Some columns need sub-splits (e.g. callin vs olo_callin, curbside vs curbside_olo)
_DINING_OPTION_TO_DB_COL = {
    # callin_net_sales (non-OLO call-ins)
    "Call-In":              "callin_net_sales",
    # olo_callin_net_sales (OLO-originated call-ins)
    "X-Call In-OLO":        "olo_callin_net_sales",
    "X-KEA Call In":        "olo_callin_net_sales",
    # togo_net_sales
    "ToGo":                 "togo_net_sales",
    "X-To Go (K)":          "togo_net_sales",
    "Drive Thru":           "togo_net_sales",
    "X-Drive Thru-OLO":     "togo_net_sales",
    # olo_net_sales
    "X-ToGo-OLO":          "olo_net_sales",
    # dinein_net_sales
    "Dine In":              "dinein_net_sales",
    "X-Dine-In (K)":        "dinein_net_sales",
    "X-Dine-In-OLO":        "dinein_net_sales",
    # delivery_net_sales
    "Delivery":             "delivery_net_sales",
    "X-Delivery-OLO":       "delivery_net_sales",
    "X-Doordash (R)":       "delivery_net_sales",
    "X-UberEats (R)":       "delivery_net_sales",
    "X-GrubHub (R)":        "delivery_net_sales",
    "X-Dispatch DD":        "delivery_net_sales",
    "X-Dispatch DL":        "delivery_net_sales",
    "X-Dispatch PM":        "delivery_net_sales",
    "X-Dispatch SC":        "delivery_net_sales",
    "X-Dispatch Uber":      "delivery_net_sales",
    "X-Dispatch (Generic)": "delivery_net_sales",
    "X-Rails (Generic)":    "delivery_net_sales",
    # curbside_net_sales (non-OLO)
    "Curbside":             "curbside_net_sales",
    # curbside_olo_net_sales (OLO-originated curbside)
    "X-Curbside-OLO":       "curbside_olo_net_sales",
    # catering_net_sales
    "Catering":             "catering_net_sales",
    "X-EzCater":            "catering_net_sales",
    "X-Online Catering":    "catering_net_sales",
    # POS2 — not a column in aloha_sales_db, skip
    # "Union Aramark (POS 2)": n/a
}

# Dining options that should NEVER be treated as catering (even if name contains
# "catering" as a substring — none today, but keeps the fallback safe).
_NON_CATERING_OVERRIDES: set[str] = set()


def _classify_dining_option(dining_opt: str) -> Tuple[Optional[str], bool]:
    """Return (db_column, is_catering) for a dining option.

    Uses the exact _DINING_OPTION_TO_DB_COL map first, then falls back to a
    case-insensitive keyword match so that new/variant catering labels
    (e.g. "X-Catering", "Catering-OLO", "EzCater Online", "Online Catering OLO")
    automatically route to catering_net_sales instead of silently leaking into
    NonCatering via the Net - Catering subtraction.
    """
    if not dining_opt:
        return (None, False)
    name = dining_opt.strip()
    # 1) exact hardcoded mapping
    db_col = _DINING_OPTION_TO_DB_COL.get(name)
    if db_col is not None:
        return (db_col, db_col == "catering_net_sales")
    # 2) explicit overrides — never catering
    if name in _NON_CATERING_OVERRIDES:
        return (None, False)
    # 3) case-insensitive catering keyword fallback
    lower = name.lower()
    if ("catering" in lower) or ("ezcater" in lower) or ("ez cater" in lower):
        return ("catering_net_sales", True)
    # 4) unknown — leave unmapped (will only hit net_sales total)
    return (None, False)


# GNG (Grab N Go) sales category GUID — items with this salesCategory are GNG revenue
_GNG_SALES_CATEGORY_GUID = "497c8ee0-a5f3-44bf-a699-11c3e8ebf9a0"

# Catering sales category GUID — items in this salesCategory are catering revenue
# regardless of which dining option they're rung up under. The SSRS reference
# report attributes catering by item salesCategory, not by order diningOption.
_CATERING_SALES_CATEGORY_GUID = "05cb784b-881f-4da9-a7bd-956efd2ce099"

# Catering dining option GUID — orders under this dopt are already counted as
# catering via ERA's groupBy=DINING_OPTION output. We only need to RE-CLASSIFY
# catering-salesCategory items that were rung up under a different dopt (e.g.
# a boxed-lunch catering order rung in as Call-In).
_CATERING_DINING_OPTION_GUID = "b823dbd8-e248-4d39-8fb6-b47d06e764dc"

# Chain-wide Newk's dining-option GUID → aloha_sales_db column. Used when
# reclassifying catering-salesCategory items that were rung up under a
# non-catering dopt: we ADD to catering_net_sales and SUBTRACT from the
# column the item was originally booked into. GUIDs verified against ERA
# diningOption names across 7 stores on 2026-04-16 (see probe_dopt_guid_map.py).
_DINING_OPTION_GUID_TO_DB_COL: Dict[str, str] = {
    "fe9d83c4-5e7a-485a-9825-12b9a7a2d917": "dinein_net_sales",      # Dine In
    "9aeda505-5443-4a31-aba5-7b90b4632c01": "togo_net_sales",         # ToGo
    "18e3fe91-b5b0-41e2-b69f-ee149817d27c": "callin_net_sales",       # Call-In
    "53cedf50-f68d-480b-ab19-939eb601bd24": "olo_net_sales",          # X-ToGo-OLO
    "68d696e6-98b6-4f3a-8c45-f87d71090be7": "delivery_net_sales",     # X-Doordash (R)
    "3fd8a153-d1bd-4a3e-91cd-9cf7074c7d10": "delivery_net_sales",     # X-UberEats (R)
    "d0b4a05b-20ff-468b-9409-237068bf22ea": "delivery_net_sales",     # X-GrubHub (R)
    "80e81bb9-c5a6-4699-ac54-6d095d89c9c7": "dinein_net_sales",       # X-Dine-In (K)
    "e6f9a691-0962-40bb-8bbc-dd00f1e4684a": "dinein_net_sales",       # X-Dine-In-OLO
    "abaf2c91-bf39-4103-b222-2d9d30756574": "curbside_olo_net_sales", # X-Curbside-OLO
    "f03c63b2-31a9-41a0-a2da-6cfa8fba37e1": "togo_net_sales",         # X-To Go (K)
    # Catering dopt itself is excluded — revenue there is already in catering.
}

_nightly_flash_last_result: Dict[str, Any] = {}
_flash_import_progress: Dict[str, Any] = {}  # live progress for in-flight imports


async def _compute_sc_and_gng(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    business_date_yyyymmdd: str,
) -> Tuple[float, float, Dict[str, float], Dict[str, float], int]:
    """
    Walk orders for one restaurant+day and compute:
      1. Total restaurant-destination service charges (to subtract from net sales)
      2. Total GNG (Grab N Go) item sales (salesCategory GUID match)
      3. SC broken down by dining option (to subtract from the correct channel)
      4. Catering-salesCategory revenue booked under NON-catering dining options,
         keyed by SOURCE dining-option GUID.  Caller reclassifies by subtracting
         from the source column and adding to catering_net_sales — matches the
         SSRS REF report's item-level attribution logic.
      5. Count of non-catering-dopt checks that contained catering-salesCategory
         items (so they're counted toward catering_checks / excluded from
         NonCateringChecks, matching the REF report).

    Returns (service_charge_total, gng_sales_total, sc_by_dining_option,
             catering_leakage_by_source_dopt_guid, catering_leakage_checks).
    sc_by_dining_option: {"Catering": 1390.00, "Delivery": 30.00, ...}
    catering_leakage_by_source_dopt_guid: {"<dopt_guid>": 479.00, ...}
    """
    orders = await _fetch_orders_for_restaurant_day(client, restaurant_guid, business_date_yyyymmdd)

    total_sc = 0.0
    total_gng = 0.0
    sc_by_dining_opt: Dict[str, float] = {}
    catering_leakage_by_dopt: Dict[str, float] = {}
    catering_leakage_checks = 0

    for order in orders:
        if order.get("voided"):
            continue
        # Is this order on the Catering dining option? If so, ERA already
        # attributes its revenue to catering_net_sales — no reclassification.
        dopt_guid = (order.get("diningOption") or {}).get("guid") or ""
        order_is_catering_dopt = (dopt_guid == _CATERING_DINING_OPTION_GUID)

        for check in order.get("checks", []):
            if check.get("voided"):
                continue
            # Service charges
            for applied_sc in check.get("appliedServiceCharges", []):
                charge_amount = float(applied_sc.get("chargeAmount") or 0)
                if charge_amount <= 0:
                    continue
                destination = applied_sc.get("destination") or "RESTAURANT"
                if destination == "RESTAURANT":
                    total_sc += charge_amount
                    # Map SC to dining option channel based on SC name.
                    # IMPORTANT: only route to a channel if the name clearly
                    # matches — generic "Service Fee" etc. must NOT default to
                    # Catering (that caused ~$600 of catering leakage on
                    # 2026-04-16 across Tuscaloosa/Hattiesburg/Starkville).
                    sc_name = str(applied_sc.get("name", "") or "").strip().upper()
                    if sc_name.startswith("CAT") or "CATERING" in sc_name or "EZCATER" in sc_name:
                        sc_channel = "Catering"
                    elif "DELIVERY" in sc_name or "DISPATCH" in sc_name:
                        sc_channel = "Delivery"
                    elif "TOGO" in sc_name or "TO GO" in sc_name or "CURBSIDE" in sc_name:
                        sc_channel = "ToGo"
                    else:
                        sc_channel = None  # unknown — only subtract from net_sales total
                    if sc_channel is not None:
                        sc_by_dining_opt[sc_channel] = sc_by_dining_opt.get(sc_channel, 0) + charge_amount

            # Walk selections for: GNG revenue AND catering-item leakage
            check_has_catering_leak = False
            for sel in check.get("selections", []):
                if sel.get("voided"):
                    continue
                sc_obj = sel.get("salesCategory") or {}
                sc_guid = sc_obj.get("guid")
                if sc_guid == _GNG_SALES_CATEGORY_GUID:
                    total_gng += float(sel.get("price") or 0)
                elif sc_guid == _CATERING_SALES_CATEGORY_GUID and not order_is_catering_dopt:
                    # Catering-category item rung up under a non-catering dopt.
                    # REF reclassifies this revenue into catering.
                    price = float(sel.get("price") or 0)
                    catering_leakage_by_dopt[dopt_guid] = catering_leakage_by_dopt.get(dopt_guid, 0) + price
                    check_has_catering_leak = True
            if check_has_catering_leak:
                catering_leakage_checks += 1

    # Also populate the SC cache so other code paths benefit
    global _sc_daily_cache
    cache_key = f"{restaurant_guid}|{business_date_yyyymmdd}"
    _sc_daily_cache[cache_key] = total_sc

    return total_sc, total_gng, sc_by_dining_opt, catering_leakage_by_dopt, catering_leakage_checks


async def nightly_flash_import_job():
    """
    Nightly job: fetch yesterday's sales from Toast ERA API (by dining option),
    compute service charges from Orders API, and write Newk's net sales into
    aloha_sales_db — fully replacing Don Wilson's SQL Server ETL pipeline.
    """
    yesterday = date.today() - timedelta(days=1)
    yesterday_str = yesterday.isoformat()  # YYYY-MM-DD
    yesterday_yyyymmdd = yesterday.strftime("%Y%m%d")
    logger.info("Nightly flash import started for %s", yesterday_str)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 1. Get all active restaurants (+ any mapped GUIDs not yet in API)
            locations = await get_all_active_locations(client)
            restaurant_ids = [loc.restaurantGuid for loc in locations]
            restaurant_ids = _ensure_all_mapped_guids(restaurant_ids)
            guid_to_name = {loc.restaurantGuid: loc.restaurantName for loc in locations}

            # 2. Load GUID → Aloha location mapping
            guid_to_loc = _get_flash_loc_mapping()

            # 3. Fetch ERA metrics with DINING_OPTION groupBy for yesterday
            report_guid = await request_toast_report(
                client, yesterday, yesterday, restaurant_ids,
                report_type="metrics", group_by=["DINING_OPTION"],
            )
            era_rows = await fetch_toast_report(client, report_guid, report_type="metrics")
            if not isinstance(era_rows, list):
                era_rows = []

            logger.info("Nightly flash: got %d ERA rows for %s", len(era_rows), yesterday_str)

            # 4. Aggregate ERA data into per-location channel buckets
            loc_data: Dict[str, Dict[str, float]] = {}
            for row in era_rows:
                guid = row.get("restaurantGuid", "")
                loc_id = guid_to_loc.get(guid)
                if not loc_id:
                    continue

                dining_opt = str(row.get("diningOption", "") or "").strip()
                net = float(row.get("netSalesAmount", 0) or 0)
                orders = float(row.get("ordersCount", 0) or 0)

                if loc_id not in loc_data:
                    loc_data[loc_id] = {
                        "net_sales": 0, "olo_net_sales": 0, "delivery_net_sales": 0,
                        "catering_net_sales": 0, "gng_net_sales": 0, "togo_net_sales": 0,
                        "curbside_net_sales": 0, "curbside_olo_net_sales": 0,
                        "dinein_net_sales": 0, "callin_net_sales": 0, "olo_callin_net_sales": 0,
                        "check_count": 0, "checks_minus_catering": 0,
                        "_guid": guid, "_catering_checks": 0,
                    }

                loc_data[loc_id]["net_sales"] += net
                loc_data[loc_id]["check_count"] += orders

                # Map to specific DB column (exact → case-insensitive catering fallback)
                db_col, is_catering = _classify_dining_option(dining_opt)
                if db_col and db_col in loc_data[loc_id]:
                    loc_data[loc_id][db_col] += net

                # Track catering checks for NonCateringChecks
                if is_catering:
                    loc_data[loc_id]["_catering_checks"] += orders

            # 5. Walk orders for each location: compute service charges + GNG sales
            sc_total = 0.0
            gng_total = 0.0
            sc_count = 0
            for loc_id, data in loc_data.items():
                guid = data.pop("_guid", "")
                cat_checks = data.pop("_catering_checks", 0)
                data["checks_minus_catering"] = data["check_count"] - cat_checks

                if guid:
                    try:
                        sc_amount, gng_amount, sc_by_dopt, cat_leak_by_dopt, cat_leak_checks = await _compute_sc_and_gng(
                            client, guid, yesterday_yyyymmdd
                        )
                        _SC_CHANNEL_TO_DB_COL = {
                            "Catering": "catering_net_sales",
                            "Delivery": "delivery_net_sales",
                            "ToGo": "togo_net_sales",
                            "OLO": "olo_net_sales",
                            "DineIn": "dinein_net_sales",
                            "CallIn": "callin_net_sales",
                        }
                        if sc_amount > 0:
                            data["net_sales"] -= sc_amount
                            sc_total += sc_amount
                            sc_count += 1
                            # Subtract SC from the specific channel it belongs to
                            for sc_channel, sc_val in sc_by_dopt.items():
                                db_col = _SC_CHANNEL_TO_DB_COL.get(sc_channel)
                                if db_col and db_col in data:
                                    data[db_col] -= sc_val
                        if gng_amount > 0:
                            data["gng_net_sales"] = gng_amount
                            gng_total += gng_amount
                        # Reclassify item-level catering leakage: subtract from
                        # the source non-catering dopt column, add to catering.
                        # Matches SSRS REF item-level attribution logic.
                        for src_dopt_guid, leak_amt in cat_leak_by_dopt.items():
                            if leak_amt <= 0:
                                continue
                            src_col = _DINING_OPTION_GUID_TO_DB_COL.get(src_dopt_guid)
                            if src_col and src_col in data:
                                data[src_col] -= leak_amt
                            data["catering_net_sales"] += leak_amt
                        if cat_leak_checks > 0:
                            # Leakage checks belong to catering per REF; remove
                            # them from NonCateringChecks.
                            data["checks_minus_catering"] = max(
                                0, data["checks_minus_catering"] - cat_leak_checks
                            )
                        await asyncio.sleep(0.3)  # Rate limit courtesy
                    except Exception as exc:
                        logger.warning("Flash import: SC/GNG fetch failed for %s: %s", loc_id, exc)

            logger.info("Nightly flash: SC subtracted=$%.2f (%d locs), GNG=$%.2f",
                        sc_total, sc_count, gng_total)

            # 6. Get store info for restaurant names
            store_info = aloha_sales_db.get_store_info()
            ownership = _load_store_ownership()

            # 7. Build DataFrame and upsert into aloha_sales_db
            rows_for_df = []
            for loc_id, data in loc_data.items():
                si = store_info.get(loc_id, {})
                own = ownership.get(str(loc_id), {}) if ownership else {}
                rname = (
                    (si.get("restaurant_name") or "").strip()
                    or (own.get("restaurantName") or "").strip()
                    or guid_to_name.get(data.get("_guid", ""), "")
                )
                rows_for_df.append({
                    "RestaurantID": loc_id,
                    "RestaurantName": rname,
                    "NetSales": data["net_sales"],
                    "OLONetSales": data["olo_net_sales"],
                    "DeliveryNetSales": data["delivery_net_sales"],
                    "CateringNetSales": data["catering_net_sales"],
                    "GNGNetSales": data["gng_net_sales"],
                    "ToGoNetSales": data["togo_net_sales"],
                    "CurbSideNetSales": data["curbside_net_sales"],
                    "CurbSideOLONetSales": data["curbside_olo_net_sales"],
                    "DineInNetSales": data["dinein_net_sales"],
                    "CallInNetSales": data["callin_net_sales"],
                    "OLOCallInNetSales": data["olo_callin_net_sales"],
                    "CheckCount": data["check_count"],
                    "ChecksMinusCatering": data["checks_minus_catering"],
                })

            if rows_for_df:
                df = pd.DataFrame(rows_for_df)
                inserted = aloha_sales_db.upsert_date(df, yesterday_str)
                logger.info("Nightly flash import: inserted %d rows for %s", inserted, yesterday_str)
            else:
                inserted = 0
                logger.warning("Nightly flash import: no data to insert for %s", yesterday_str)

        _nightly_flash_last_result.update({
            "business_date": yesterday_str,
            "locations": len(loc_data),
            "rows_inserted": inserted,
            "service_charges_total": round(sc_total, 2),
            "service_charges_locations": sc_count,
            "gng_total": round(gng_total, 2),
            "completed_at": datetime.now().isoformat(),
            "status": "success",
        })

        # ── Auto-backfill: check last 14 days for missing/partial data ──
        try:
            backfilled = 0
            max_backfill = 7  # Cap per nightly run to avoid overloading
            check_start = date.today() - timedelta(days=14)
            check_end = date.today() - timedelta(days=2)  # Skip yesterday (just imported)
            conn_check = aloha_sales_db.get_connection()
            try:
                gap_rows = conn_check.execute(
                    "SELECT business_date, COUNT(*) as cnt "
                    "FROM aloha_sales_data "
                    "WHERE business_date BETWEEN ? AND ? "
                    "GROUP BY business_date",
                    (check_start.isoformat(), check_end.isoformat())
                ).fetchall()
            finally:
                conn_check.close()

            existing = {row[0]: row[1] for row in gap_rows}
            d = check_start
            while d <= check_end and backfilled < max_backfill:
                d_str = d.isoformat()
                row_count = existing.get(d_str, 0)
                if row_count < 80:
                    logger.info("Flash auto-backfill: %s has %d rows (< 80), re-importing", d_str, row_count)
                    try:
                        await _run_flash_import_for_date(d)
                        backfilled += 1
                    except Exception as bf_exc:
                        logger.warning("Flash auto-backfill failed for %s: %s", d_str, bf_exc)
                d += timedelta(days=1)

            if backfilled > 0:
                logger.info("Flash auto-backfill: filled %d gap dates", backfilled)
                _nightly_flash_last_result["auto_backfilled"] = backfilled
        except Exception as gap_exc:
            logger.warning("Flash auto-backfill gap check failed: %s", gap_exc)

    except Exception as exc:
        logger.error("Nightly flash import failed: %s", exc, exc_info=True)
        _nightly_flash_last_result.update({
            "business_date": yesterday_str,
            "status": "failed",
            "error": str(exc),
            "completed_at": datetime.now().isoformat(),
        })


# Schedule: run at 4:00 AM daily (after the 3 AM orders cache job finishes)
_scheduler.add_job(nightly_flash_import_job, CronTrigger(hour=4, minute=0),
                   id="nightly_flash_import", replace_existing=True)


# ------------------
# Nightly Monkey Media sync
# ------------------

_nightly_monkey_last_result: Dict[str, Any] = {}


async def nightly_monkey_sync_job():
    """Incremental sync of Monkey Media orders from Azure SQL (last 3 days)."""
    logger.info("Nightly Monkey sync started")
    try:
        result = await asyncio.to_thread(_run_monkey_sync, days=3)
        _nightly_monkey_last_result.update({
            "status": "success",
            "rows": result.get("rows", 0),
            "elapsed": result.get("elapsed", 0),
            "ran_at": datetime.now().isoformat(),
        })
        logger.info("Nightly Monkey sync complete: %d rows in %.1fs",
                     result.get("rows", 0), result.get("elapsed", 0))
    except Exception as exc:
        logger.error("Nightly Monkey sync failed: %s", exc, exc_info=True)
        _nightly_monkey_last_result.update({
            "status": "failed",
            "error": str(exc),
            "ran_at": datetime.now().isoformat(),
        })


def _run_monkey_sync(days: int | None = None, tables: list | None = None):
    """Run sync_monkey in the current process (blocking). Called via asyncio.to_thread."""
    import sync_monkey as sm
    from datetime import timedelta

    monkey_db.initialize_db()
    t0 = time.time()
    azure_conn = sm.get_azure_connection()
    sqlite_conn = monkey_db.get_connection()

    since = None
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    tables_to_sync = tables or list(sm.TABLES.keys())
    total_rows = 0
    try:
        for table in tables_to_sync:
            config = sm.TABLES[table]
            table_since = since if "date_col" in config else None
            if since and table_since is None:
                continue  # Skip non-date tables for incremental sync
            rows = sm.sync_table(azure_conn, sqlite_conn, table, config, since=table_since)
            total_rows += rows

            # Second pass: also sync orders by date_reqd so catering orders
            # placed long ago but with upcoming required dates are captured
            if table == "orders" and since and "extra_date_cols" in config:
                reqd_since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                reqd_until = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
                reqd_config = {
                    "query": f"SELECT * FROM msr.orders WHERE date_reqd >= ? AND date_reqd <= ?",
                    "pk": "order_id",
                }
                # Use a custom sync call — sync_table with since=None does INSERT OR REPLACE
                is_pymssql = sm.HAS_PYMSSQL and type(azure_conn).__module__.startswith("pymssql")
                ph = "%s" if is_pymssql else "?"
                reqd_config["query"] = f"SELECT * FROM msr.orders WHERE date_reqd >= {ph} AND date_reqd <= {ph}"
                azure_cursor = azure_conn.cursor()
                azure_cursor.execute(reqd_config["query"], (reqd_since, reqd_until))
                azure_cols = [desc[0] for desc in azure_cursor.description]
                sqlite_cols = sm.get_sqlite_columns(sqlite_conn, "orders")
                col_map = []
                sqlite_col_lower = {s.lower(): s for s in sqlite_cols}
                for ac in azure_cols:
                    sc = ac.lower().replace(" ", "_")
                    if sc in sqlite_col_lower:
                        col_map.append((ac, sqlite_col_lower[sc]))
                if col_map:
                    azure_indices = [azure_cols.index(ac) for ac, _ in col_map]
                    sqlite_names = [sc for _, sc in col_map]
                    ph_vals = ",".join("?" * len(sqlite_names))
                    col_str = ",".join(sqlite_names)
                    extra_rows = 0
                    for row in azure_cursor:
                        vals = []
                        for idx in azure_indices:
                            v = row[idx]
                            if isinstance(v, datetime):
                                v = v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                            elif v is not None and not isinstance(v, (int, float, str)):
                                v = str(v)
                            vals.append(v)
                        sqlite_conn.execute(
                            f"INSERT OR REPLACE INTO orders ({col_str}) VALUES ({ph_vals})", vals
                        )
                        extra_rows += 1
                    sqlite_conn.commit()
                    if extra_rows:
                        logger.info("  orders (by date_reqd %s..%s): %d extra rows upserted",
                                    reqd_since, reqd_until, extra_rows)
                    total_rows += extra_rows
    finally:
        azure_conn.close()
        sqlite_conn.close()

    return {"rows": total_rows, "elapsed": round(time.time() - t0, 1)}


# Schedule: run at 4:30 AM daily (after flash import)
_scheduler.add_job(nightly_monkey_sync_job, CronTrigger(hour=4, minute=30),
                   id="nightly_monkey_sync", replace_existing=True)


# ------------------
# Nightly Menu Performance precompute
#
# Warms the disk JSON cache (backend/data/precomputed/menu_*.json) for the
# 12 most-common Menu Performance shapes — 3 date bases × 4 store groups.
# When Kim opens the page in the morning, every "Run Report" click that
# matches one of these shapes returns in <100ms instead of doing the full
# ~1M-row pivot + YoY enrichment.
#
# Runs at 5:00 AM after the upstream nightly pulls have settled. Total job
# typically finishes by ~6:30 AM (12 shapes × 5-10min each, sequential).
# Foundation for emailed report subscriptions — a subscription declares
# one of these shapes; the email job reads the same cache and ships the
# Excel without a second pivot.
# ------------------

_nightly_menu_precompute_last_result: Dict[str, Any] = {}


async def nightly_menu_precompute_job():
    """Build + execute the nightly Menu Performance shapes via loopback HTTP."""
    import precompute_menu_report
    logger.info("Nightly menu precompute started")
    started = time.time()
    try:
        base_url = "http://127.0.0.1:8000"
        shapes = await precompute_menu_report.build_nightly_shapes(base_url)
        if not shapes:
            logger.warning(
                "Nightly menu precompute: no shapes to warm "
                "(fiscal calendar missing? no active stores?)"
            )
            _nightly_menu_precompute_last_result.update({
                "status":  "skipped",
                "reason":  "no shapes built",
                "ran_at":  datetime.now().isoformat(),
            })
            return
        logger.info("Nightly menu precompute: running %d shapes", len(shapes))
        summary = await precompute_menu_report.run_default_shapes(
            base_url=base_url,
            auth_header=None,
            shapes=shapes,
            on_progress=None,
            request_timeout_s=1800.0,
        )
        ok = summary.get("ok_count", 0)
        err = summary.get("error_count", 0)
        elapsed = round(time.time() - started, 1)
        _nightly_menu_precompute_last_result.update({
            "status":       "success" if err == 0 else "partial",
            "ok_count":     ok,
            "error_count":  err,
            "total_shapes": summary.get("total_shapes", 0),
            "elapsed_s":    elapsed,
            "ran_at":       datetime.now().isoformat(),
            "shapes":       summary.get("shapes", []),
        })
        logger.info(
            "Nightly menu precompute complete: %d/%d ok in %ss",
            ok, summary.get("total_shapes", 0), elapsed,
        )
    except Exception as exc:
        logger.error("Nightly menu precompute failed: %s", exc, exc_info=True)
        _nightly_menu_precompute_last_result.update({
            "status":    "failed",
            "error":     str(exc),
            "ran_at":    datetime.now().isoformat(),
            "elapsed_s": round(time.time() - started, 1),
        })


# Schedule: 5:00 AM daily, after monkey sync (4:30) finishes. Gives the
# 12-shape pass ~4 hours of headroom before a 9 AM analyst arrives.
_scheduler.add_job(nightly_menu_precompute_job, CronTrigger(hour=5, minute=0),
                   id="nightly_menu_precompute", replace_existing=True)


@app.on_event("startup")
async def _on_startup():
    toast_orders_db.initialize_db()
    aloha_sales_db.initialize_db()
    toast_config_db.initialize_db()
    monkey_db.initialize_db()
    # Contacts SQLite: tables created idempotently so the UI works even
    # before the first SQL Server → SQLite export has happened.
    try:
        contacts_db.initialize_schema()
    except Exception as exc:
        logger.warning("contacts_db.initialize_schema failed (non-fatal): %s", exc)
    _scheduler.start()
    logger.info(
        "APScheduler started — nightly jobs scheduled "
        "(orders@3AM, flash@4AM, monkey@4:30AM, menu_precompute@5AM)"
    )


@app.on_event("startup")
async def _on_startup_warm_menu_tags():
    """
    Pre-fetch V2 menu tag mappings for every active store on uvicorn boot so
    the first user request (Menu Performance / pick reports) doesn't pay the
    per-store Toast Menus API cost. Adds ~30-60s to startup; eliminates the
    "first run after restart is slow" surprise.

    Wrapped in try/except so a warmup failure (e.g. Toast 403, network blip)
    can't prevent uvicorn from coming up. Stores that fail warmup stay cold
    and are populated lazily on first real request, same as before.
    """
    logger.info("Menu tags warmup: starting")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                locs = await get_all_active_locations(client)
            except Exception as e:
                logger.warning("Menu tags warmup: get_all_active_locations failed (%s) — skipping", e)
                return
            if not locs:
                logger.warning("Menu tags warmup: no active locations returned — skipping")
                return

            sem = asyncio.Semaphore(10)
            async def warm_one(rguid: str):
                async with sem:
                    try:
                        await get_menu_tags_mapping(client, rguid)
                    except Exception as e:
                        logger.warning("Menu tags warmup miss rguid=%s: %s", rguid[:8], e)

            t0 = time.perf_counter()
            await asyncio.gather(
                *[warm_one(l.restaurantGuid) for l in locs],
                return_exceptions=True,
            )
            logger.info(
                "Menu tags warmup: done in %.1fs (%d stores attempted, %d cached)",
                time.perf_counter() - t0, len(locs), len(_menu_tags_cache),
            )
    except Exception as e:
        # Defense in depth — never crash startup on a warmup failure.
        logger.error("Menu tags warmup: unexpected failure: %s", e, exc_info=True)


@app.on_event("shutdown")
async def _on_shutdown():
    _scheduler.shutdown(wait=False)


# ------------------
# Media uploads for kiosk/menu board
# ------------------

UPLOAD_ROOT = os.path.join(os.path.dirname(__file__), "uploads", "menu-board")
os.makedirs(UPLOAD_ROOT, exist_ok=True)


@app.post("/api/uploads/menu-board")
async def upload_menu_board_file(file: UploadFile = File(...)) -> Dict[str, str]:
  """Upload an image or video for the kiosk menu board and return a URL."""
  original_name = file.filename or "upload"
  name, ext = os.path.splitext(original_name)
  safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)[:80] or "upload"
  unique = uuid.uuid4().hex[:8]
  filename = f"{safe_name}_{unique}{ext}"
  dest_path = os.path.join(UPLOAD_ROOT, filename)

  try:
      with open(dest_path, "wb") as out:
          while True:
              chunk = await file.read(1024 * 1024)
              if not chunk:
                  break
              out.write(chunk)
  except Exception as exc:
      logger.error(f"Failed to save uploaded file {original_name}: {exc}")
      raise HTTPException(status_code=500, detail="Failed to save uploaded file")

  url = f"/media/menu-board/{filename}"
  return {"url": url}


class Base64Upload(BaseModel):
    """JSON-based upload: base64-encoded file data + original filename.
    Works around Cloudways backend nginx blocking multipart/form-data."""
    filename: str
    data: str  # base64-encoded file content


@app.post("/api/uploads/menu-board-b64")
async def upload_menu_board_file_b64(body: Base64Upload) -> Dict[str, str]:
    """Upload via JSON with base64-encoded data (bypasses multipart proxy issues)."""
    import base64 as _b64

    original_name = body.filename or "upload"
    name, ext = os.path.splitext(original_name)
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)[:80] or "upload"
    unique = uuid.uuid4().hex[:8]
    filename = f"{safe_name}_{unique}{ext}"
    dest_path = os.path.join(UPLOAD_ROOT, filename)

    try:
        raw = _b64.b64decode(body.data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")

    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    try:
        with open(dest_path, "wb") as out:
            out.write(raw)
    except Exception as exc:
        logger.error(f"Failed to save uploaded file {original_name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    url = f"/media/menu-board/{filename}"
    return {"url": url}


# ------------------
# V2 API Router
# ------------------

v2_router = APIRouter(prefix="/api/v2", tags=["v2"])

@v2_router.get("/health")
async def v2_health():
    """V2 health check endpoint"""
    return {"status": "ok", "version": "v2", "platform": "operational-intelligence"}

@v2_router.get("/health/detailed")
async def v2_health_detailed():
    """Detailed health check with Toast API connectivity"""
    health_info = {
        "status": "ok",
        "version": "v2",
        "platform": "operational-intelligence",
        "toast_api": {
            "configured": bool(TOAST_API_CLIENT_ID and TOAST_API_CLIENT_SECRET) or bool(TOAST_API_TOKEN),
            "base_url": TOAST_API_BASE_URL,
            "auth_cached": bool(_token_cache.get("token")),
            "locations_test": "not_tested"
        }
    }
    
    # Test locations endpoint
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            locations = await get_all_active_locations(client)
            health_info["toast_api"]["locations_test"] = "success"
            health_info["toast_api"]["location_count"] = len(locations)
    except Exception as e:
        health_info["toast_api"]["locations_test"] = f"failed: {str(e)[:100]}"
        health_info["status"] = "degraded"
    
    return health_info

@v2_router.get("/locations", response_model=List[Location])
async def v2_list_locations():
    """V2 locations endpoint - reuses v1 logic exactly"""
    logger.info("V2 locations endpoint called")
    # Use exact same implementation as v1 endpoint for consistency
    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("Calling get_all_active_locations from v2 endpoint")
        result = await get_all_active_locations(client)
        logger.info(f"V2 locations endpoint returning {len(result)} locations")
        return result

def calculate_date_range_from_request(request: V2InsightRequest) -> Tuple[date, date]:
    """Helper function to calculate start and end dates from V2InsightRequest"""
    if request.dateRangeType == "custom":
        if not request.customStartDate or not request.customEndDate:
            raise HTTPException(status_code=400, detail="Custom date range requires start and end dates")
        start_date = date.fromisoformat(request.customStartDate)
        end_date = date.fromisoformat(request.customEndDate)
    elif request.dateRangeType == "last_7_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=6)
    elif request.dateRangeType == "last_30_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=29)
    elif request.dateRangeType == "last_90_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=89)
    else:
        raise HTTPException(status_code=400, detail="Invalid date range type")
    
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    
    return start_date, end_date

@v2_router.post("/insights/item-performance", response_model=List[ItemPerformanceResponse])
async def v2_item_performance(request: V2InsightRequest = Body(...)):
    """Get item performance data - uses MENU_ITEM groupBy and fetches tags"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            locations = None
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            elif TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID or TOAST_MENU_TAGS_PRIORITY_NAME:
                locations = await get_all_active_locations(client)
            else:
                locations = None
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Get menu tags mapping for tag names
            all_tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
            # Try a few more restaurants to get complete coverage
            for i in range(1, min(4, len(restaurant_ids))):
                try:
                    additional_mapping = await get_menu_tags_mapping(client, restaurant_ids[i])
                    for item_guid, tags in additional_mapping.items():
                        if item_guid not in all_tags_mapping:
                            all_tags_mapping[item_guid] = tags
                except Exception as e:
                    logger.warning(f"Failed to get menu mapping from restaurant {restaurant_ids[i]}: {e}")
                    continue
            
            # Helper function to fetch menu data with MENU_ITEM groupBy
            async def fetch_menu_data_with_groupby(chunk_start: date, chunk_end: date) -> list:
                days_diff = (chunk_end - chunk_start).days + 1
                
                if days_diff <= 7:
                    report_guid = await request_toast_report(
                        client, chunk_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
                    )
                    chunk_data = await fetch_toast_report(client, report_guid, "menu")
                    return chunk_data if isinstance(chunk_data, list) else []
                else:
                    # Split into week-sized chunks and fetch in parallel
                    chunks = []
                    current_start = chunk_start
                    while current_start <= chunk_end:
                        current_end = min(current_start + timedelta(days=6), chunk_end)
                        chunks.append((current_start, current_end))
                        current_start = current_end + timedelta(days=1)
                    
                    async def fetch_chunk(ch_start: date, ch_end: date) -> list:
                        report_guid = await request_toast_report(
                            client, ch_start, ch_end, restaurant_ids, "menu", ["MENU_ITEM"]
                        )
                        chunk_data = await fetch_toast_report(client, report_guid, "menu")
                        return chunk_data if isinstance(chunk_data, list) else []
                    
                    chunk_results = await asyncio.gather(*[fetch_chunk(cs, ce) for cs, ce in chunks])
                    all_data = []
                    for chunk_data in chunk_results:
                        all_data.extend(chunk_data)
                    return all_data
            
            # Fetch menu data with MENU_ITEM groupBy
            raw_data = await fetch_menu_data_with_groupby(start_date, end_date)
            
            # Ensure data is a list
            if not isinstance(raw_data, list):
                raw_data = []
            
            # Transform to normalized format with tags
            results = []
            for row in raw_data:
                if not isinstance(row, dict):
                    continue
                
                # Only process menu items (not modifiers)
                item_guid = row.get("menuItemGuid", "")
                if not item_guid:
                    continue
                
                item_name = row.get("menuItemName", "")
                if not item_name:
                    item_name = f"Item {item_guid[:8]}"
                
                # Get tags for this item
                tag_name = None
                if item_guid in all_tags_mapping:
                    tags = all_tags_mapping[item_guid]
                    if tags and len(tags) > 0:
                        # Use first tag name (or join multiple if needed)
                        tag_name = tags[0]["tagName"]
                
                quantity = int(row.get("quantitySold", 0) or 0)
                sales = float(row.get("netSalesAmount", 0.0) or 0.0)
                store_name = row.get("restaurantName", "Unknown")
                business_date = row.get("businessDate", "")
                
                # Format date from YYYYMMDD to YYYY-MM-DD if needed
                if business_date and len(str(business_date)) == 8:
                    formatted_date = f"{str(business_date)[:4]}-{str(business_date)[4:6]}-{str(business_date)[6:8]}"
                else:
                    formatted_date = str(business_date) if business_date else ""
                
                results.append(ItemPerformanceResponse(
                    itemName=item_name,
                    tagName=tag_name,
                    quantity=quantity,
                    sales=sales,
                    storeName=store_name,
                    date=formatted_date
                ))
            
            return results
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_item_performance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching item performance: {str(e)}")

@v2_router.post("/insights/top-movers", response_model=List[TopMoversResponse])
async def v2_top_movers(request: V2InsightRequest = Body(...)):
    """Get top movers insight - compares two periods to find items with largest changes"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    # Calculate period length
    period_length = (end_date - start_date).days
    
    # Period 1: earlier period of same length
    period1_end = start_date - timedelta(days=1)
    period1_start = period1_end - timedelta(days=period_length)
    
    # Validate period1_start is not too far in the past (Toast API may not have data)
    if period1_start < date.today() - timedelta(days=365):
        raise HTTPException(status_code=400, detail="Comparison period is too far in the past. Please use a more recent date range.")
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Helper function to fetch menu data with MENU_ITEM groupBy
            # If date range is > 7 days, split into week chunks (groupBy only works for day/week)
            async def fetch_menu_data_with_groupby(chunk_start: date, chunk_end: date) -> list:
                days_diff = (chunk_end - chunk_start).days + 1
                
                if days_diff <= 7:
                    # Can use groupBy directly
                    report_guid = await request_toast_report(
                        client, chunk_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
                    )
                    chunk_data = await fetch_toast_report(client, report_guid, "menu")
                    return chunk_data if isinstance(chunk_data, list) else []
                else:
                    # Split into week-sized chunks and fetch in parallel
                    chunks = []
                    current_start = chunk_start
                    while current_start <= chunk_end:
                        current_end = min(current_start + timedelta(days=6), chunk_end)
                        chunks.append((current_start, current_end))
                        current_start = current_end + timedelta(days=1)
                    
                    # Fetch all chunks in parallel
                    async def fetch_chunk(ch_start: date, ch_end: date) -> list:
                        report_guid = await request_toast_report(
                            client, ch_start, ch_end, restaurant_ids, "menu", ["MENU_ITEM"]
                        )
                        chunk_data = await fetch_toast_report(client, report_guid, "menu")
                        return chunk_data if isinstance(chunk_data, list) else []
                    
                    # Run all chunk fetches in parallel
                    chunk_results = await asyncio.gather(*[fetch_chunk(cs, ce) for cs, ce in chunks])
                    
                    # Flatten results
                    all_data = []
                    for chunk_data in chunk_results:
                        all_data.extend(chunk_data)
                    
                    return all_data
            
            # Fetch data for both periods
            data_p1 = await fetch_menu_data_with_groupby(period1_start, period1_end)
            data_p2 = await fetch_menu_data_with_groupby(start_date, end_date)
            
            # Aggregate by item GUID (not name) to avoid combining different items with same name
            # Only include rows with menuItemGuid (exclude modifiers which have modifierGuid instead)
            p1_totals: Dict[str, Dict[str, float]] = {}  # guid -> {name, sales}
            p2_totals: Dict[str, Dict[str, float]] = {}  # guid -> {name, sales}
            
            for row in data_p1:
                if not isinstance(row, dict):
                    continue
                # Only process menu items (not modifiers)
                item_guid = row.get("menuItemGuid", "")
                if not item_guid:
                    # Skip if no menuItemGuid (might be a modifier or invalid row)
                    continue
                item_name = row.get("menuItemName", "")
                if not item_name:
                    item_name = f"Item {item_guid[:8]}"
                sales = float(row.get("netSalesAmount", 0.0) or 0.0)
                if item_guid not in p1_totals:
                    p1_totals[item_guid] = {"name": item_name, "sales": 0.0}
                p1_totals[item_guid]["sales"] += sales
            
            for row in data_p2:
                if not isinstance(row, dict):
                    continue
                # Only process menu items (not modifiers)
                item_guid = row.get("menuItemGuid", "")
                if not item_guid:
                    # Skip if no menuItemGuid (might be a modifier or invalid row)
                    continue
                item_name = row.get("menuItemName", "")
                if not item_name:
                    item_name = f"Item {item_guid[:8]}"
                sales = float(row.get("netSalesAmount", 0.0) or 0.0)
                if item_guid not in p2_totals:
                    p2_totals[item_guid] = {"name": item_name, "sales": 0.0}
                p2_totals[item_guid]["sales"] += sales
            
            # Calculate changes for all items (by GUID to avoid name collisions)
            movers = []
            all_guids = set(p1_totals.keys()) | set(p2_totals.keys())
            
            for item_guid in all_guids:
                p1_data = p1_totals.get(item_guid, {"name": "Unknown", "sales": 0.0})
                p2_data = p2_totals.get(item_guid, {"name": "Unknown", "sales": 0.0})
                
                # Use the name from whichever period has it, or prefer p2
                item_name = p2_data["name"] or p1_data["name"] or "Unknown"
                p1_val = p1_data["sales"]
                p2_val = p2_data["sales"]
                
                if p1_val == 0:
                    change_percent = 100.0 if p2_val > 0 else 0.0
                else:
                    change_percent = ((p2_val - p1_val) / p1_val) * 100.0
                
                direction = "up" if change_percent > 0 else ("down" if change_percent < 0 else "neutral")
                
                movers.append(TopMoversResponse(
                    itemName=item_name,
                    changePercent=round(change_percent, 2),
                    period1Value=round(p1_val, 2),
                    period2Value=round(p2_val, 2),
                    direction=direction
                ))
            
            # Sort by absolute change percent, return top 20
            movers.sort(key=lambda x: abs(x.changePercent), reverse=True)
            return movers[:20]
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_top_movers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching top movers: {str(e)}")

@v2_router.post("/insights/store-variance", response_model=List[StoreVarianceResponse])
async def v2_store_variance(request: V2InsightRequest = Body(...)):
    """Get store variance insight - compares stores against average benchmark"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Fetch metrics data
            report_guid = await request_toast_report(
                client, start_date, end_date, restaurant_ids, "metrics", []
            )
            raw_data = await fetch_toast_report(client, report_guid, "metrics")
            
            # Ensure data is a list
            if not isinstance(raw_data, list):
                raw_data = []
            
            # Aggregate by store
            store_totals: Dict[str, Dict[str, float]] = {}
            
            for row in raw_data:
                if not isinstance(row, dict):
                    continue
                store_name = row.get("restaurantName", "Unknown")
                if store_name not in store_totals:
                    store_totals[store_name] = {"netSales": 0.0, "guestCount": 0.0, "itemCount": 0.0}
                
                store_totals[store_name]["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                store_totals[store_name]["guestCount"] += float(row.get("guestCount", 0.0) or 0.0)
                store_totals[store_name]["itemCount"] += float(row.get("itemCount", 0.0) or 0.0)
            
            # Calculate benchmarks (average across all stores)
            num_stores = len(store_totals)
            if num_stores == 0:
                return []
            
            benchmarks = {
                "netSales": sum(s["netSales"] for s in store_totals.values()) / num_stores,
                "guestCount": sum(s["guestCount"] for s in store_totals.values()) / num_stores,
                "itemCount": sum(s["itemCount"] for s in store_totals.values()) / num_stores
            }
            
            # Calculate variance for each store and metric
            results = []
            for store_name, totals in store_totals.items():
                for metric, value in totals.items():
                    benchmark = benchmarks[metric]
                    if benchmark > 0:
                        variance = ((value - benchmark) / benchmark) * 100.0
                    else:
                        variance = 0.0
                    
                    results.append(StoreVarianceResponse(
                        storeName=store_name,
                        metric=metric,
                        value=round(value, 2),
                        variance=round(variance, 2),
                        benchmark=round(benchmark, 2)
                    ))
            
            # Sort by absolute variance
            results.sort(key=lambda x: abs(x.variance), reverse=True)
            return results
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_store_variance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching store variance: {str(e)}")

@v2_router.post("/insights/store-rollups", response_model=List[StoreRollupResponse])
async def v2_store_rollups(request: V2InsightRequest = Body(...)):
    """Get aggregated metrics rolled up by store"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Fetch metrics data
            report_guid = await request_toast_report(
                client, start_date, end_date, restaurant_ids, "metrics", []
            )
            raw_data = await fetch_toast_report(client, report_guid, "metrics")
            
            # Ensure data is a list
            if not isinstance(raw_data, list):
                raw_data = []
            
            # Aggregate by store
            store_rollups: Dict[str, Dict[str, Any]] = {}
            
            for row in raw_data:
                if not isinstance(row, dict):
                    continue
                store_name = row.get("restaurantName", "Unknown")
                store_guid = row.get("restaurantGuid", "")
                
                if store_name not in store_rollups:
                    store_rollups[store_name] = {
                        "storeGuid": store_guid,
                        "netSales": 0.0,
                        "guestCount": 0,
                        "itemCount": 0
                    }
                
                store_rollups[store_name]["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                store_rollups[store_name]["guestCount"] += int(row.get("guestCount", 0) or 0)
                store_rollups[store_name]["itemCount"] += int(row.get("itemCount", 0) or 0)
            
            # Build response
            results = []
            date_range_str = f"{start_date.isoformat()} to {end_date.isoformat()}"
            
            for store_name, totals in store_rollups.items():
                guest_count = totals["guestCount"]
                avg_ticket = totals["netSales"] / guest_count if guest_count > 0 else 0.0
                
                results.append(StoreRollupResponse(
                    storeName=store_name,
                    storeGuid=totals["storeGuid"],
                    netSales=round(totals["netSales"], 2),
                    guestCount=totals["guestCount"],
                    itemCount=totals["itemCount"],
                    avgTicket=round(avg_ticket, 2),
                    dateRange=date_range_str
                ))
            
            # Sort by net sales descending
            results.sort(key=lambda x: x.netSales, reverse=True)
            return results
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_store_rollups: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching store rollups: {str(e)}")

@v2_router.post("/insights/trend-deltas", response_model=List[TrendDeltaResponse])
async def v2_trend_deltas(request: V2InsightRequest = Body(...)):
    """Get period-over-period trend deltas for key metrics"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    # Calculate period length
    period_length = (end_date - start_date).days
    
    # Period 1: earlier period of same length
    period1_end = start_date - timedelta(days=1)
    period1_start = period1_end - timedelta(days=period_length)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Validate period1_start is not too far in the past
            if period1_start < date.today() - timedelta(days=365):
                raise HTTPException(status_code=400, detail="Comparison period is too far in the past. Please use a more recent date range.")
            
            # Fetch data for both periods
            report_guid_p1 = await request_toast_report(
                client, period1_start, period1_end, restaurant_ids, "metrics", []
            )
            data_p1 = await fetch_toast_report(client, report_guid_p1, "metrics")
            
            report_guid_p2 = await request_toast_report(
                client, start_date, end_date, restaurant_ids, "metrics", []
            )
            data_p2 = await fetch_toast_report(client, report_guid_p2, "metrics")
            
            # Ensure data is a list
            if not isinstance(data_p1, list):
                data_p1 = []
            if not isinstance(data_p2, list):
                data_p2 = []
            
            # Aggregate metrics across all stores
            p1_totals = {"netSales": 0.0, "guestCount": 0.0, "itemCount": 0.0}
            p2_totals = {"netSales": 0.0, "guestCount": 0.0, "itemCount": 0.0}
            
            for row in data_p1:
                if not isinstance(row, dict):
                    continue
                p1_totals["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                p1_totals["guestCount"] += float(row.get("guestCount", 0.0) or 0.0)
                p1_totals["itemCount"] += float(row.get("itemCount", 0.0) or 0.0)
            
            for row in data_p2:
                if not isinstance(row, dict):
                    continue
                p2_totals["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                p2_totals["guestCount"] += float(row.get("guestCount", 0.0) or 0.0)
                p2_totals["itemCount"] += float(row.get("itemCount", 0.0) or 0.0)
            
            # Calculate deltas
            results = []
            period1_label = f"{period1_start.isoformat()} to {period1_end.isoformat()}"
            period2_label = f"{start_date.isoformat()} to {end_date.isoformat()}"
            
            for metric in ["netSales", "guestCount", "itemCount"]:
                p1_val = p1_totals[metric]
                p2_val = p2_totals[metric]
                delta = p2_val - p1_val
                
                if p1_val > 0:
                    delta_percent = (delta / p1_val) * 100.0
                else:
                    delta_percent = 100.0 if p2_val > 0 else 0.0
                
                direction = "up" if delta > 0 else ("down" if delta < 0 else "neutral")
                
                results.append(TrendDeltaResponse(
                    metric=metric,
                    period1Value=round(p1_val, 2),
                    period2Value=round(p2_val, 2),
                    delta=round(delta, 2),
                    deltaPercent=round(delta_percent, 2),
                    period1Label=period1_label,
                    period2Label=period2_label,
                    direction=direction
                ))
            
            return results
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_trend_deltas: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching trend deltas: {str(e)}")

@v2_router.post("/insights/anomaly-flags", response_model=List[AnomalyFlagResponse])
async def v2_anomaly_flags(request: V2InsightRequest = Body(...)):
    """Detect anomalies in store metrics using statistical methods"""
    start_date, end_date = calculate_date_range_from_request(request)
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = request.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Fetch metrics data
            report_guid = await request_toast_report(
                client, start_date, end_date, restaurant_ids, "metrics", []
            )
            raw_data = await fetch_toast_report(client, report_guid, "metrics")
            
            # Ensure data is a list
            if not isinstance(raw_data, list):
                raw_data = []
            
            # Aggregate by store
            store_metrics: Dict[str, Dict[str, float]] = {}
            
            for row in raw_data:
                if not isinstance(row, dict):
                    continue
                store_name = row.get("restaurantName", "Unknown")
                if store_name not in store_metrics:
                    store_metrics[store_name] = {"netSales": 0.0, "guestCount": 0.0}
                
                store_metrics[store_name]["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                store_metrics[store_name]["guestCount"] += float(row.get("guestCount", 0.0) or 0.0)
            
            if len(store_metrics) < 3:
                # Need at least 3 stores for meaningful anomaly detection
                return []
            
            # Calculate statistics for each metric
            results = []
            
            for metric in ["netSales", "guestCount"]:
                values = [store_metrics[store][metric] for store in store_metrics.keys()]
                mean = sum(values) / len(values)
                
                # Calculate standard deviation
                variance = sum((x - mean) ** 2 for x in values) / len(values)
                std_dev = variance ** 0.5
                
                if std_dev == 0:
                    continue
                
                # Flag stores that are more than 2 standard deviations from mean
                for store_name, metrics in store_metrics.items():
                    value = metrics[metric]
                    deviation = (value - mean) / std_dev
                    
                    if abs(deviation) >= 2.0:
                        severity = "high" if abs(deviation) >= 3.0 else "medium"
                        flag_type = "outlier"
                        
                        if deviation > 0:
                            flag_type = "spike"
                        else:
                            flag_type = "drop"
                        
                        results.append(AnomalyFlagResponse(
                            storeName=store_name,
                            metric=metric,
                            value=round(value, 2),
                            expectedValue=round(mean, 2),
                            deviation=round(deviation, 2),
                            severity=severity,
                            flagType=flag_type
                        ))
            
            # Sort by deviation (absolute value)
            results.sort(key=lambda x: abs(x.deviation), reverse=True)
            return results
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in v2_anomaly_flags: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching anomaly flags: {str(e)}")

@v2_router.get("/dashboards/{dashboard_type}", response_model=DashboardResponse)
async def v2_get_dashboard(dashboard_type: str):
    """Get dashboard data for executive, operations, or financial dashboard"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get all locations for default
            locations = await get_all_active_locations(client)
            restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            # Default to last 30 days
            end_date = date.today()
            start_date = end_date - timedelta(days=30)
            
            widgets = []
            
            if dashboard_type == "executive":
                # Executive dashboard: High-level KPIs
                # Get store rollups
                report_guid = await request_toast_report(
                    client, start_date, end_date, restaurant_ids, "metrics", []
                )
                raw_data = await fetch_toast_report(client, report_guid, "metrics")
                
                if isinstance(raw_data, list):
                    total_sales = sum(float(row.get("netSalesAmount", 0.0) or 0.0) for row in raw_data if isinstance(row, dict))
                    total_guests = sum(float(row.get("guestCount", 0.0) or 0.0) for row in raw_data if isinstance(row, dict))
                    avg_ticket = total_sales / total_guests if total_guests > 0 else 0.0
                    
                    widgets.append(DashboardWidget(
                        type="kpi",
                        title="Total Net Sales",
                        data={"value": round(total_sales, 2), "format": "currency"},
                        config={}
                    ))
                    widgets.append(DashboardWidget(
                        type="kpi",
                        title="Total Guests",
                        data={"value": int(total_guests), "format": "number"},
                        config={}
                    ))
                    widgets.append(DashboardWidget(
                        type="kpi",
                        title="Average Ticket",
                        data={"value": round(avg_ticket, 2), "format": "currency"},
                        config={}
                    ))
                
                # Get trend deltas
                period1_end = start_date - timedelta(days=1)
                period1_start = period1_end - timedelta(days=30)
                if period1_start >= date.today() - timedelta(days=365):
                    report_guid_p1 = await request_toast_report(
                        client, period1_start, period1_end, restaurant_ids, "metrics", []
                    )
                    data_p1 = await fetch_toast_report(client, report_guid_p1, "metrics")
                    
                    if isinstance(data_p1, list) and isinstance(raw_data, list):
                        p1_sales = sum(float(row.get("netSalesAmount", 0.0) or 0.0) for row in data_p1 if isinstance(row, dict))
                        p2_sales = total_sales
                        if p1_sales > 0:
                            change_pct = ((p2_sales - p1_sales) / p1_sales) * 100.0
                            widgets.append(DashboardWidget(
                                type="kpi",
                                title="Sales Change",
                                data={"value": round(change_pct, 2), "format": "percent", "direction": "up" if change_pct > 0 else "down"},
                                config={}
                            ))
                
                # Add a sales trend chart for visual cues
                if isinstance(raw_data, list):
                    daily_sales = {}
                    for row in raw_data:
                        if not isinstance(row, dict):
                            continue
                        business_date = row.get("businessDate", "")
                        if business_date:
                            daily_sales[business_date] = daily_sales.get(business_date, 0.0) + float(row.get("netSalesAmount", 0.0) or 0.0)
                    
                    widgets.append(DashboardWidget(
                        type="chart",
                        title="Daily Sales Trend",
                        data={"series": [{"date": k, "sales": round(v, 2)} for k, v in sorted(daily_sales.items())]},
                        config={"chartType": "line"}
                    ))
            
            elif dashboard_type == "operations":
                # Operations dashboard: Store performance, anomalies
                # Get store rollups
                report_guid = await request_toast_report(
                    client, start_date, end_date, restaurant_ids, "metrics", []
                )
                raw_data = await fetch_toast_report(client, report_guid, "metrics")
                
                if isinstance(raw_data, list):
                    # Aggregate by store
                    store_totals = {}
                    for row in raw_data:
                        if not isinstance(row, dict):
                            continue
                        store_name = row.get("restaurantName", "Unknown")
                        if store_name not in store_totals:
                            store_totals[store_name] = {"netSales": 0.0, "guestCount": 0.0}
                        store_totals[store_name]["netSales"] += float(row.get("netSalesAmount", 0.0) or 0.0)
                        store_totals[store_name]["guestCount"] += float(row.get("guestCount", 0.0) or 0.0)
                    
                    # Top stores with average ticket
                    top_stores = sorted(store_totals.items(), key=lambda x: x[1]["netSales"], reverse=True)[:5]
                    store_rows = []
                    for k, v in top_stores:
                        avg_ticket = v["netSales"] / v["guestCount"] if v["guestCount"] > 0 else 0.0
                        store_rows.append({
                            "store": k,
                            "sales": round(v["netSales"], 2),
                            "guests": int(v["guestCount"]),
                            "avgTicket": round(avg_ticket, 2)
                        })
                    widgets.append(DashboardWidget(
                        type="table",
                        title="Top 5 Stores by Sales",
                        data={"rows": store_rows},
                        config={}
                    ))
                    
                    # Anomaly flags
                    if len(store_totals) >= 3:
                        values = [v["netSales"] for v in store_totals.values()]
                        mean = sum(values) / len(values)
                        variance = sum((x - mean) ** 2 for x in values) / len(values)
                        std_dev = variance ** 0.5 if variance > 0 else 0
                        
                        anomalies = []
                        for store_name, metrics in store_totals.items():
                            value = metrics["netSales"]
                            if std_dev > 0:
                                deviation = (value - mean) / std_dev
                                if abs(deviation) >= 2.0:
                                    anomalies.append({
                                        "store": store_name,
                                        "value": round(value, 2),
                                        "deviation": round(deviation, 2),
                                        "severity": "high" if abs(deviation) >= 3.0 else "medium"
                                    })
                        
                        if anomalies:
                            widgets.append(DashboardWidget(
                                type="alert",
                                title="Performance Alerts",
                                data={"alerts": anomalies[:5]},
                                config={}
                            ))
            
            elif dashboard_type == "financial":
                # Financial dashboard: Revenue trends, forecasting
                report_guid = await request_toast_report(
                    client, start_date, end_date, restaurant_ids, "metrics", []
                )
                raw_data = await fetch_toast_report(client, report_guid, "metrics")
                
                if isinstance(raw_data, list):
                    # Daily sales trend
                    daily_sales = {}
                    for row in raw_data:
                        if not isinstance(row, dict):
                            continue
                        business_date = row.get("businessDate", "")
                        if business_date:
                            daily_sales[business_date] = daily_sales.get(business_date, 0.0) + float(row.get("netSalesAmount", 0.0) or 0.0)
                    
                    widgets.append(DashboardWidget(
                        type="chart",
                        title="Daily Sales Trend",
                        data={"series": [{"date": k, "sales": round(v, 2)} for k, v in sorted(daily_sales.items())]},
                        config={"chartType": "line"}
                    ))
            
            return DashboardResponse(
                dashboardType=dashboard_type,
                widgets=widgets,
                lastUpdated=datetime.now().isoformat(),
                dateRange={"start": start_date.isoformat(), "end": end_date.isoformat()}
            )
            
    except HTTPException as e:
        # Re-raise HTTP exceptions (like 429 rate limits) as-is
        raise e
    except Exception as e:
        logger.error(f"Error in v2_get_dashboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching dashboard: {str(e)}")

# Store Groups Endpoints
@v2_router.get("/store-groups", response_model=List[StoreGroup])
async def v2_get_store_groups():
    """Get all store groups"""
    return load_store_groups()

@v2_router.post("/store-groups", response_model=StoreGroup)
async def v2_create_store_group(group: StoreGroupCreate = Body(...)):
    """Create a new store group"""
    groups = load_store_groups()
    
    if any(g.name.lower() == group.name.lower() for g in groups):
        raise HTTPException(status_code=400, detail="A store group with this name already exists")
    
    new_group = StoreGroup(
        id=str(uuid.uuid4()),
        name=group.name,
        description=group.description or "",
        storeGuids=group.storeGuids,
        createdAt=datetime.now().isoformat(),
        updatedAt=datetime.now().isoformat()
    )
    groups.append(new_group)
    save_store_groups(groups)
    
    return new_group

@v2_router.put("/store-groups/{group_id}", response_model=StoreGroup)
async def v2_update_store_group(group_id: str, group: StoreGroupCreate = Body(...)):
    """Update a store group"""
    groups = load_store_groups()
    
    index = None
    for i, g in enumerate(groups):
        if g.id == group_id:
            index = i
            break
    
    if index is None:
        raise HTTPException(status_code=404, detail="Store group not found")
    
    if any(g.name.lower() == group.name.lower() and g.id != group_id for g in groups):
        raise HTTPException(status_code=400, detail="A store group with this name already exists")
    
    updated_group = StoreGroup(
        id=group_id,
        name=group.name,
        description=group.description or "",
        storeGuids=group.storeGuids,
        createdAt=groups[index].createdAt,
        updatedAt=datetime.now().isoformat()
    )
    groups[index] = updated_group
    save_store_groups(groups)
    
    return updated_group

@v2_router.delete("/store-groups/{group_id}")
async def v2_delete_store_group(group_id: str):
    """Delete a store group"""
    groups = load_store_groups()
    original_count = len(groups)
    groups = [g for g in groups if g.id != group_id]
    
    if len(groups) == original_count:
        raise HTTPException(status_code=404, detail="Store group not found")
    
    save_store_groups(groups)
    return {"message": "Store group deleted successfully"}

# Mount v2 router
app.include_router(v2_router)

# ------------------
# OLO Order Export / Digital Orders by Platform
# ------------------

olo_router = APIRouter(prefix="/api/olo", tags=["olo"])


class OloDigitalOrdersRequest(BaseModel):
    """Request to run Digital Orders by Platform report for selected batches."""
    batch_ids: List[int]


class OloDigitalOrdersByDateRequest(BaseModel):
    """Request to run report for a date range; batches are auto-selected by generated date."""
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD


@olo_router.get("/batches")
async def olo_list_batches():
    """List available OLO order export batches (Signature auth required)."""
    try:
        batches = await list_order_batches()
        return {"batches": batches}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text or "OLO API error")
    except Exception as e:
        logger.exception("OLO list batches failed")
        raise HTTPException(status_code=500, detail=str(e))


@olo_router.post("/digital-orders-by-platform")
async def olo_digital_orders_by_platform(body: OloDigitalOrdersRequest = Body(...)):
    """
    Download selected order batches from OLO, parse orderbatch XML, and return
    aggregated Digital Orders by Platform report rows (platform, store, order count, totals).
    """
    if not body.batch_ids:
        raise HTTPException(status_code=400, detail="At least one batch_id is required")
    all_orders = []
    for batch_id in body.batch_ids:
        try:
            zip_bytes = await download_batch_zip(batch_id)
            orders = extract_orders_from_zip(zip_bytes)
            all_orders.extend(orders)
        except Exception as e:
            logger.warning(f"OLO batch {batch_id} failed: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to process batch {batch_id}: {e}")
    rows = aggregate_digital_orders_by_platform(all_orders)
    return {"rows": rows, "totalOrders": len(all_orders)}


@olo_router.post("/digital-orders-by-platform-by-date")
async def olo_digital_orders_by_platform_by_date(body: OloDigitalOrdersByDateRequest = Body(...)):
    """
    Run Digital Orders by Platform for a date range. Fetches batches, filters by generated
    date within [start_date, end_date], then downloads and aggregates. No batch selection needed.
    """
    try:
        all_batches = await list_order_batches()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("OLO list batches failed")
        raise HTTPException(status_code=500, detail=str(e))
    filtered = filter_batches_by_date_range(
        all_batches, body.start_date.strip(), body.end_date.strip()
    )
    if not filtered:
        raise HTTPException(
            status_code=404,
            detail=f"No OLO batches found for date range {body.start_date} to {body.end_date}. Try a different range or check OLO batch availability.",
        )
    batch_ids = [b["batchId"] for b in filtered]
    all_orders = []
    for batch_id in batch_ids:
        try:
            zip_bytes = await download_batch_zip(batch_id)
            orders = extract_orders_from_zip(zip_bytes)
            all_orders.extend(orders)
        except Exception as e:
            logger.warning(f"OLO batch {batch_id} failed: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to process batch {batch_id}: {e}")
    rows = aggregate_digital_orders_by_platform(all_orders)
    return {
        "rows": rows,
        "totalOrders": len(all_orders),
        "batchIds": batch_ids,
        "startDate": body.start_date,
        "endDate": body.end_date,
    }


@olo_router.post("/sales-by-handoff")
async def olo_sales_by_handoff(body: OloDigitalOrdersByDateRequest = Body(...)):
    """OLO sales broken out by handoff mode (pickup, delivery, etc.)."""
    try:
        all_batches = await list_order_batches()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    filtered = filter_batches_by_date_range(
        all_batches, body.start_date.strip(), body.end_date.strip()
    )
    if not filtered:
        raise HTTPException(status_code=404, detail="No OLO batches found for that date range.")
    all_orders = []
    for batch_id in [b["batchId"] for b in filtered]:
        try:
            zip_bytes = await download_batch_zip(batch_id)
            orders = extract_orders_from_zip(zip_bytes)
            all_orders.extend(orders)
        except Exception as e:
            logger.warning(f"OLO batch {batch_id} failed: {e}")
    rows = aggregate_by_handoff_mode(all_orders)
    return {
        "rows": rows,
        "totalOrders": len(all_orders),
        "startDate": body.start_date,
        "endDate": body.end_date,
    }


@olo_router.post("/orders-placed-ahead")
async def olo_orders_placed_ahead(body: OloDigitalOrdersByDateRequest = Body(...)):
    """Orders placed ahead of time (timePlaced differs from timeWanted)."""
    try:
        all_batches = await list_order_batches()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    filtered = filter_batches_by_date_range(
        all_batches, body.start_date.strip(), body.end_date.strip()
    )
    if not filtered:
        raise HTTPException(status_code=404, detail="No OLO batches found for that date range.")
    all_orders = []
    for batch_id in [b["batchId"] for b in filtered]:
        try:
            zip_bytes = await download_batch_zip(batch_id)
            orders = extract_orders_from_zip(zip_bytes)
            all_orders.extend(orders)
        except Exception as e:
            logger.warning(f"OLO batch {batch_id} failed: {e}")
    rows = aggregate_orders_placed_ahead(all_orders)
    total_ahead = sum(r["placedAhead"] for r in rows)
    return {
        "rows": rows,
        "totalOrders": len(all_orders),
        "totalPlacedAhead": total_ahead,
        "placedAheadPct": round((total_ahead / len(all_orders)) * 100, 1) if all_orders else 0,
        "startDate": body.start_date,
        "endDate": body.end_date,
    }


app.include_router(olo_router)

# ------------------
# Core Endpoints
# ------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2025-12-17-analytics-tags"}

# ------------------
# Authentication Endpoints
# ------------------

def truncate_password_to_72_bytes(password: str) -> str:
    """Truncate password to 72 bytes maximum for bcrypt compatibility"""
    password_bytes = password.encode('utf-8')
    if len(password_bytes) <= 72:
        return password
    
    # Truncate to exactly 72 bytes
    password_bytes = password_bytes[:72]
    # Decode back, handling potential incomplete UTF-8 sequences
    try:
        return password_bytes.decode('utf-8')
    except UnicodeDecodeError:
        # If we truncated in the middle of a multi-byte character, remove incomplete bytes
        while len(password_bytes) > 0:
            try:
                return password_bytes.decode('utf-8')
            except UnicodeDecodeError:
                password_bytes = password_bytes[:-1]
        return ""

@app.post("/api/auth/register", response_model=Token)
async def register(user_data: UserCreate = Body(...)):
    """Register a new user"""
    try:
        # Truncate password to 72 bytes BEFORE validation (bcrypt requirement)
        original_password = user_data.password
        user_data.password = truncate_password_to_72_bytes(user_data.password)
        
        if len(user_data.password) < 6:
            logger.warning(f"Registration attempt with password too short: {len(user_data.password)} chars")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 6 characters long."
            )
        
        if not user_data.name or len(user_data.name.strip()) == 0:
            logger.warning("Registration attempt without name")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Name is required."
            )
        
        users = load_users()
        
        # Check if user already exists
        for user_id, user in users.items():
            if user.get("email", "").lower() == user_data.email.lower():
                logger.warning(f"Registration attempt with existing email: {user_data.email}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered"
                )
        
        # Create new user
        user_id = str(uuid.uuid4())
        hashed_password = hash_password(user_data.password)
        
        new_user = {
            "id": user_id,
            "email": user_data.email.lower(),
            "name": user_data.name.strip(),
            "password": hashed_password,
            "createdAt": datetime.now().isoformat(),
            "lastLogin": None,
            "preferences": {},
            "role": "user"  # Default role
        }
        
        users[user_id] = new_user
        save_users(users)
        
        # Create access token
        access_token = create_access_token(data={"sub": user_id})
        
        user_obj = User(**{k: v for k, v in new_user.items() if k != "password"})
        
        logger.info(f"New user registered: {user_data.email}")
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user_obj
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during registration: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )

@app.post("/api/auth/login", response_model=Token)
async def login(login_data: UserLogin = Body(...)):
    """Login and get access token"""
    # Truncate password to 72 bytes (bcrypt requirement)
    login_data.password = truncate_password_to_72_bytes(login_data.password)
    
    users = load_users()
    
    # Find user by email
    user_id = None
    user_data = None
    for uid, user in users.items():
        if user.get("email", "").lower() == login_data.email.lower():
            user_id = uid
            user_data = user
            break
    
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    # Block login for invited (not yet activated) users
    if user_data.get("status") == "invited":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Please check your email and set up your password first"
        )

    # Verify password
    if not verify_password(login_data.password, user_data.get("password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    # Update last login
    user_data["lastLogin"] = datetime.now().isoformat()
    users[user_id] = user_data
    save_users(users)
    
    # Create access token
    access_token = create_access_token(data={"sub": user_id})
    
    user_obj = User(**{k: v for k, v in user_data.items() if k != "password"})
    
    logger.info(f"User logged in: {login_data.email}")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_obj
    }

@app.get("/api/auth/me", response_model=User)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    return current_user

@app.put("/api/auth/me", response_model=User)
async def update_current_user(
    user_update: UserUpdate = Body(...),
    current_user: User = Depends(get_current_user)
):
    """Update current user information"""
    users = load_users()
    user_data = users.get(current_user.id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update fields
    if user_update.name is not None:
        user_data["name"] = user_update.name
    if user_update.preferences is not None:
        user_data["preferences"] = user_update.preferences
    
    users[current_user.id] = user_data
    save_users(users)
    
    return User(**{k: v for k, v in user_data.items() if k != "password"})

# ------------------
# Admin Endpoints
# ------------------

@app.get("/api/admin/users", response_model=List[User])
async def list_all_users(admin_user: User = Depends(get_current_admin_user)):
    """List all users (admin only)"""
    users = load_users()
    return [User(**{k: v for k, v in user.items() if k != "password"}) for user in users.values()]

@app.post("/api/admin/users", response_model=User)
async def create_user(
    user_data: UserCreate = Body(...),
    admin_user: User = Depends(get_current_admin_user)
):
    """Create a new user via invite flow (admin only).

    No password required — an invitation email is sent so the user
    can set their own password.
    """
    users = load_users()

    # Check if user already exists
    for uid, user in users.items():
        if user.get("email", "").lower() == user_data.email.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # Validate allowedPages keys
    allowed = user_data.allowedPages or []
    invalid = [p for p in allowed if p not in AVAILABLE_PAGES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid page keys: {', '.join(invalid)}"
        )

    user_id = str(uuid.uuid4())
    invite_token = str(uuid.uuid4())
    invite_expiry = (datetime.now() + timedelta(hours=INVITE_EXPIRY_HOURS)).isoformat()

    new_user = {
        "id": user_id,
        "email": user_data.email.lower(),
        "name": user_data.name.strip(),
        "password": "",  # No password until invite accepted
        "createdAt": datetime.now().isoformat(),
        "lastLogin": None,
        "preferences": {},
        "role": user_data.role if user_data.role in ["user", "admin"] else "user",
        "allowedPages": allowed,
        "status": "invited",
        "inviteToken": invite_token,
        "inviteTokenExpiry": invite_expiry,
    }

    users[user_id] = new_user
    save_users(users)

    # Send invitation email
    invite_url = f"{APP_BASE_URL}/set-password.html?token={invite_token}"
    email_sent = send_invite_email(user_data.email.lower(), user_data.name.strip(), invite_url)
    if not email_sent:
        logger.warning(f"User {user_data.email} created but invite email failed to send")

    logger.info(f"Admin {admin_user.email} invited user: {user_data.email}")
    return User(**{k: v for k, v in new_user.items() if k not in ("password", "inviteToken", "inviteTokenExpiry")})

@app.put("/api/admin/users/{user_id}/role", response_model=User)
async def update_user_role(
    user_id: str,
    role: str = Body(..., embed=True),
    admin_user: User = Depends(get_current_admin_user)
):
    """Update a user's role (admin only)"""
    if role not in ["user", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be 'user' or 'admin'"
        )
    
    users = load_users()
    user_data = users.get(user_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_data["role"] = role
    users[user_id] = user_data
    save_users(users)
    
    logger.info(f"Admin {admin_user.email} updated user {user_data.get('email')} role to {role}")
    return User(**{k: v for k, v in user_data.items() if k != "password"})

@app.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    admin_user: User = Depends(get_current_admin_user)
):
    """Delete a user (admin only)"""
    users = load_users()
    user_data = users.get(user_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prevent deleting yourself
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account"
        )
    
    # Delete the user
    del users[user_id]
    save_users(users)

    logger.info(f"Admin {admin_user.email} deleted user {user_data.get('email')}")
    return {"message": "User deleted successfully"}

@app.put("/api/users/me/restaurants")
async def update_my_restaurants(
    data: dict = Body(...),
    current_user: User = Depends(get_current_user)
):
    """Save the calling user's restaurant filter. Pass restaurantIds=[] to clear (show all)."""
    restaurant_ids = data.get("restaurantIds", [])
    users = load_users()
    if current_user.id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    users[current_user.id]["assignedRestaurants"] = restaurant_ids
    save_users(users)
    logger.info(f"User {current_user.email} updated assignedRestaurants: {len(restaurant_ids)} location(s)")
    return User(**{k: v for k, v in users[current_user.id].items() if k != "password"})

@app.put("/api/admin/users/{user_id}/restaurants")
async def admin_set_user_restaurants(
    user_id: str,
    data: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user)
):
    """Admin assigns restaurant access for any user. Pass restaurantIds=[] to restore 'show all'."""
    restaurant_ids = data.get("restaurantIds", [])
    users = load_users()
    if user_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    users[user_id]["assignedRestaurants"] = restaurant_ids
    save_users(users)
    logger.info(f"Admin {admin_user.email} set assignedRestaurants for user {user_id}: {len(restaurant_ids)} location(s)")
    return User(**{k: v for k, v in users[user_id].items() if k != "password"})

@app.put("/api/admin/users/{user_id}/pages", response_model=User)
async def admin_set_user_pages(
    user_id: str,
    data: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user)
):
    """Admin sets which report pages a user can access. Pass pages=[] to allow all."""
    pages = data.get("pages", [])
    invalid = [p for p in pages if p not in AVAILABLE_PAGES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid page keys: {', '.join(invalid)}")
    users = load_users()
    if user_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    users[user_id]["allowedPages"] = pages
    save_users(users)
    logger.info(f"Admin {admin_user.email} set allowedPages for user {user_id}: {pages}")
    return User(**{k: v for k, v in users[user_id].items() if k not in ("password", "inviteToken", "inviteTokenExpiry")})

@app.post("/api/admin/users/{user_id}/resend-invite")
async def resend_invite(
    user_id: str,
    admin_user: User = Depends(get_current_admin_user)
):
    """Regenerate invite token and resend invitation email."""
    users = load_users()
    user_data = users.get(user_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    if user_data.get("status") != "invited":
        raise HTTPException(status_code=400, detail="User has already accepted their invite")

    invite_token = str(uuid.uuid4())
    invite_expiry = (datetime.now() + timedelta(hours=INVITE_EXPIRY_HOURS)).isoformat()
    user_data["inviteToken"] = invite_token
    user_data["inviteTokenExpiry"] = invite_expiry
    users[user_id] = user_data
    save_users(users)

    invite_url = f"{APP_BASE_URL}/set-password.html?token={invite_token}"
    email_sent = send_invite_email(user_data["email"], user_data["name"], invite_url)
    if not email_sent:
        raise HTTPException(status_code=500, detail="Failed to send invitation email")

    logger.info(f"Admin {admin_user.email} resent invite for {user_data['email']}")
    return {"message": f"Invitation resent to {user_data['email']}"}

@app.get("/api/auth/invite-info")
async def get_invite_info(token: str = Query(...)):
    """Public endpoint — returns name and email for a valid invite token."""
    users = load_users()
    for uid, user_data in users.items():
        if user_data.get("inviteToken") == token:
            if user_data.get("status") != "invited":
                raise HTTPException(status_code=400, detail="This invitation has already been used")
            expiry = user_data.get("inviteTokenExpiry", "")
            if expiry and datetime.fromisoformat(expiry) < datetime.now():
                raise HTTPException(status_code=400, detail="This invitation has expired. Please ask your administrator to resend it.")
            return {"name": user_data["name"], "email": user_data["email"]}
    raise HTTPException(status_code=404, detail="Invalid invitation link")

@app.post("/api/auth/accept-invite")
async def accept_invite(data: dict = Body(...)):
    """Accept an invitation by setting a password. Public endpoint."""
    token = data.get("token", "")
    password = data.get("password", "")
    if not token or not password:
        raise HTTPException(status_code=400, detail="Token and password are required")

    password = truncate_password_to_72_bytes(password)
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")

    users = load_users()
    target_uid = None
    for uid, user_data in users.items():
        if user_data.get("inviteToken") == token:
            target_uid = uid
            break

    if not target_uid:
        raise HTTPException(status_code=404, detail="Invalid invitation link")

    user_data = users[target_uid]
    if user_data.get("status") != "invited":
        raise HTTPException(status_code=400, detail="This invitation has already been used")

    expiry = user_data.get("inviteTokenExpiry", "")
    if expiry and datetime.fromisoformat(expiry) < datetime.now():
        raise HTTPException(status_code=400, detail="This invitation has expired. Please ask your administrator to resend it.")

    # Activate the account
    user_data["password"] = hash_password(password)
    user_data["status"] = "active"
    user_data["inviteToken"] = None
    user_data["inviteTokenExpiry"] = None
    users[target_uid] = user_data
    save_users(users)

    logger.info(f"User {user_data['email']} accepted invite and set password")
    return {"message": "Account activated! You can now log in."}

@app.post("/api/auth/forgot-password")
async def forgot_password(data: dict = Body(...)):
    """Request a password reset link. Public endpoint.

    Always returns 200 regardless of whether the email exists, to avoid
    leaking which addresses are registered.
    """
    email = (data.get("email") or "").strip().lower()
    generic_response = {
        "message": "If an account exists for that email, a password reset link has been sent."
    }
    if not email:
        return generic_response

    users = load_users()
    target_uid = None
    for uid, user_data in users.items():
        if user_data.get("email", "").lower() == email:
            target_uid = uid
            break

    if not target_uid:
        logger.info(f"Password reset requested for unknown email: {email}")
        return generic_response

    user_data = users[target_uid]
    if user_data.get("status") == "invited":
        logger.info(f"Password reset requested for invited (not activated) user: {email}")
        return generic_response

    reset_token = str(uuid.uuid4())
    reset_expiry = (datetime.now() + timedelta(hours=PASSWORD_RESET_EXPIRY_HOURS)).isoformat()
    user_data["resetToken"] = reset_token
    user_data["resetTokenExpiry"] = reset_expiry
    users[target_uid] = user_data
    save_users(users)

    reset_url = f"{APP_BASE_URL}/reset-password.html?token={reset_token}"
    email_sent = send_password_reset_email(user_data["email"], user_data["name"], reset_url)
    if not email_sent:
        logger.warning(f"Password reset token generated for {email} but email failed to send")
    else:
        logger.info(f"Password reset email sent to {email}")

    return generic_response

@app.get("/api/auth/reset-info")
async def get_reset_info(token: str = Query(...)):
    """Public endpoint — verifies a reset token and returns the user's name."""
    users = load_users()
    for uid, user_data in users.items():
        if user_data.get("resetToken") == token:
            expiry = user_data.get("resetTokenExpiry", "")
            if expiry and datetime.fromisoformat(expiry) < datetime.now():
                raise HTTPException(status_code=400, detail="This password reset link has expired. Please request a new one.")
            return {"name": user_data["name"], "email": user_data["email"]}
    raise HTTPException(status_code=404, detail="Invalid or expired password reset link")

@app.post("/api/auth/reset-password")
async def reset_password(data: dict = Body(...)):
    """Reset a password using a token. Public endpoint. One-time use."""
    token = data.get("token", "")
    password = data.get("password", "")
    if not token or not password:
        raise HTTPException(status_code=400, detail="Token and password are required")

    password = truncate_password_to_72_bytes(password)
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")

    users = load_users()
    target_uid = None
    for uid, user_data in users.items():
        if user_data.get("resetToken") == token:
            target_uid = uid
            break

    if not target_uid:
        raise HTTPException(status_code=404, detail="Invalid or expired password reset link")

    user_data = users[target_uid]
    expiry = user_data.get("resetTokenExpiry", "")
    if expiry and datetime.fromisoformat(expiry) < datetime.now():
        raise HTTPException(status_code=400, detail="This password reset link has expired. Please request a new one.")

    user_data["password"] = hash_password(password)
    user_data["resetToken"] = None
    user_data["resetTokenExpiry"] = None
    users[target_uid] = user_data
    save_users(users)

    logger.info(f"User {user_data['email']} reset their password")
    return {"message": "Password reset successfully. You can now log in with your new password."}

@app.get("/api/available-pages")
async def get_available_pages():
    """Return the list of available report pages (for admin UI)."""
    return AVAILABLE_PAGES

@app.get("/locations", response_model=List[Location])
async def list_locations(optional_user: Optional[User] = Depends(get_optional_user), includeTest: bool = False, includeInactive: bool = False, refresh: bool = False):
    if refresh:
        # Clear the location cache to force a fresh pull from Toast API
        _locations_cache.clear()
        logger.info("Location cache cleared by refresh=true request")
    async with httpx.AsyncClient(timeout=30.0) as client:
        all_locs = await get_all_active_locations(client, include_test=includeTest, include_inactive=includeInactive)
    # Filter to assigned restaurants for authenticated non-admin users who have an assignment.
    # Admins and unauthenticated callers always receive the full list.
    if optional_user and optional_user.role != "admin" and optional_user.assignedRestaurants:
        assigned = set(optional_user.assignedRestaurants)
        return [loc for loc in all_locs if loc.restaurantGuid in assigned]
    return all_locs


@app.post("/api/menu/export-missing-consolidated", response_model=MissingConsolidatedExportResponse)
async def export_missing_consolidated_categories(preview_limit: int = 0):
    """Export menu items/modifiers with sales missing ConsolidatedCat mapping."""
    end_date = date.today()
    start_date = end_date - timedelta(days=89)
    output_file = os.path.join(ROOT_DIR, "Missing_ConsolidatedCat_Items.xlsx")

    async with httpx.AsyncClient(timeout=120.0) as client:
        locations = await get_all_active_locations(client)
        restaurant_ids = [loc.restaurantGuid for loc in locations]
        if not restaurant_ids:
            raise HTTPException(status_code=400, detail="No restaurants available")

        consolidated_map = get_consolidated_category_map()
        consolidated_override = get_consolidated_override_map()

        menu_rows = await _fetch_menu_rows_for_range(
            client,
            restaurant_ids,
            start_date,
            end_date,
            ["MENU_ITEM"]
        )
        modifier_rows = await _fetch_menu_rows_for_range(
            client,
            restaurant_ids,
            start_date,
            end_date,
            ["MODIFIER"]
        )

    if modifier_rows:
        for row in modifier_rows:
            if "modifierGuid" in row:
                row["menuItemGuid"] = row.pop("modifierGuid")
            elif "modifierOptionGuid" in row:
                row["menuItemGuid"] = row.pop("modifierOptionGuid")
            if "modifierName" in row:
                row["menuItemName"] = row.pop("modifierName")
            elif "modifierOptionName" in row:
                row["menuItemName"] = row.pop("modifierOptionName")

    aggregated_menu = _aggregate_missing_consolidated_rows(menu_rows, "menu", consolidated_map)
    aggregated_mods = _aggregate_missing_consolidated_rows(modifier_rows, "modifier", consolidated_map)
    aggregated_menu.update(aggregated_mods)

    missing_rows = list(aggregated_menu.values())
    missing_rows.sort(key=lambda r: (r["menuItemName"] or "").lower())

    df = pd.DataFrame(missing_rows)
    output_file = _safe_write_excel(df, output_file)

    if preview_limit and preview_limit > 0:
        preview_file = os.path.join(ROOT_DIR, "Missing_ConsolidatedCat_Preview.xlsx")
        aggregated_all = _aggregate_all_rows(menu_rows, "menu", consolidated_map)
        aggregated_all.update(_aggregate_all_rows(modifier_rows, "modifier", consolidated_map))
        preview_rows = list(aggregated_all.values())
        preview_rows.sort(key=lambda r: (r["menuItemName"] or "").lower())
        preview_df = pd.DataFrame(preview_rows[:preview_limit])
        _safe_write_excel(preview_df, preview_file)

    return {
        "success": True,
        "output_file": output_file,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_rows": len(menu_rows) + len(modifier_rows),
        "missing_rows": len(missing_rows)
    }


# ------------------
# Mapping Table API (Toast–Aloha consolidated mapping)
# ------------------

def _load_mapping_table_excel() -> Tuple[str, List[str], List[Dict]]:
    """Load primary mapping Excel; returns (source_path, columns, rows)."""
    path = MAPPING_FILE
    if not os.path.exists(path):
        path = FALLBACK_MAPPING_FILE
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Mapping file not found (Updated Mapping Table.xlsx or Fallback)")
    df = pd.read_excel(path)
    columns = [str(c) for c in df.columns]
    rows = []
    for _, row in df.iterrows():
        r = {}
        for col in columns:
            v = row.get(col)
            if pd.isna(v):
                r[col] = ""
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                r[col] = int(v) if float(v).is_integer() else v
            else:
                r[col] = str(v).strip() if v is not None else ""
        rows.append(r)
    return path, columns, rows


@app.get("/api/consolidated-categories")
async def get_consolidated_categories():
    """
    Return the canonical list of "* Consolidated …" tag names from
    consolidated_category_ids. The Excel export uses this to fire a
    re-fetch when the on-screen report was filtered to a subset, so the
    download always covers every category instead of just what the user
    was looking at.
    """
    if not toast_config_db.db_available():
        return {"categories": []}
    try:
        with toast_config_db.get_connection() as conn:
            rows = conn.execute(
                "SELECT tag_name FROM consolidated_category_ids ORDER BY tag_name"
            ).fetchall()
        return {"categories": [r[0] for r in rows if r[0]]}
    except Exception as exc:
        logger.warning(f"Failed to load consolidated categories: {exc}")
        return {"categories": []}


@app.get("/api/mapping-table")
async def get_mapping_table():
    """Return the Toast–Aloha consolidated mapping table for viewing/editing."""
    # Prefer SQLite; fall back to Excel if DB not available
    if toast_config_db.db_available():
        rows = toast_config_db.get_mapping_table_rows()
        if rows:
            columns = list(rows[0].keys()) if rows else toast_config_db._MAPPING_COLUMNS
            # Remove internal 'id' column from response
            columns = [c for c in columns if c != "id"]
            clean_rows = [{k: v for k, v in r.items() if k != "id"} for r in rows]
            return {
                "source": "toast_config.db",
                "columns": columns,
                "rows": clean_rows,
            }
    # Fallback to Excel
    path, columns, rows = _load_mapping_table_excel()
    return {
        "source": path,
        "columns": columns,
        "rows": rows,
    }


class MappingTableUpdate(BaseModel):
    columns: List[str]
    rows: List[Dict[str, str]]  # values as strings from UI


@app.put("/api/mapping-table")
async def update_mapping_table(body: MappingTableUpdate):
    """Save the mapping table to SQLite (falls back to Excel)."""
    if not body.columns or not body.rows:
        raise HTTPException(status_code=400, detail="columns and rows are required")
    # Clean up values
    ordered = []
    for r in body.rows:
        row_dict = {}
        for c in body.columns:
            v = r.get(c)
            if v is None or v == "":
                row_dict[c] = ""
            elif isinstance(v, str):
                row_dict[c] = v.strip()
            else:
                row_dict[c] = v
        ordered.append(row_dict)

    # Prefer SQLite; fall back to Excel
    if toast_config_db.db_available():
        count = toast_config_db.update_mapping_table_rows(ordered)
        saved_to = "toast_config.db"
    else:
        path = MAPPING_FILE
        if not os.path.exists(path):
            path = FALLBACK_MAPPING_FILE
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Mapping file not found; cannot save")
        df = pd.DataFrame(ordered, columns=body.columns)
        saved_to = _safe_write_excel(df, path)
        count = len(body.rows)

    # Invalidate consolidated category cache so next read uses new data
    global _consolidated_cat_cache
    _consolidated_cat_cache = {}
    # Also clear the precomputed menu-report cache. The cache key doesn't
    # include mapping-table state, so a stale entry would mask this save's
    # effect on the next identical report run. Best-effort: never block the
    # save on a cache-clear failure.
    menu_cache_cleared = 0
    try:
        menu_cache_cleared = precompute_cache.clear_cache()
        logger.info("Mapping table saved — cleared %d precomputed menu cache entries", menu_cache_cleared)
    except Exception as exc:
        logger.warning("precompute_cache clear failed after mapping save: %s", exc)
    return {"success": True, "saved_to": saved_to, "rows": count, "menu_cache_cleared": menu_cache_cleared}


@app.get("/api/aloha-items")
async def get_aloha_items_endpoint(consol_cat: Optional[str] = None):
    """
    Return the canonical Aloha item list for the Mapping Table UI dropdown.

    Optional query param `consol_cat` (e.g. "* Consolidated NA Bev") scopes
    the result to one category. Without it, returns the full list — used
    when the UI's "Show all categories" toggle is on.

    Source: aloha_items_master in toast_config.db, populated from
    "Items by Category.xlsx" by backend/import_aloha_items.py.
    """
    items = toast_config_db.get_aloha_items(consol_cat=consol_cat)
    return {"count": len(items), "items": items}


# ------------------
# Mapping Audit API
# ------------------


def _collect_items_from_groups(groups, menu_name, loc_name, out: Dict[str, dict]):
    """Recursively walk menuGroups (Toast can nest sub-groups)."""
    for group in groups:
        group_name = group.get("name", "")
        for item in group.get("menuItems", []):
            name = (item.get("name") or "").strip()
            if not name:
                continue
            norm = _normalize_item_name(name)
            if norm not in out:
                out[norm] = {
                    "name":          name,
                    "guid":          item.get("guid", ""),
                    "plu":           str(item.get("plu") or ""),
                    "mli":           str(item.get("multiLocationId") or ""),
                    "menu":          menu_name,
                    "group":         group_name,
                    "tags":          [t.get("name") for t in item.get("itemTags", []) if t.get("name")],
                    "locationCount": 0,
                }
            out[norm]["locationCount"] += 1
        # Recurse into nested sub-groups
        _collect_items_from_groups(group.get("menuGroups", []), menu_name, loc_name, out)


async def _poll_all_toast_items() -> Tuple[Dict[str, dict], int, List[str]]:
    """
    Poll all active Toast locations and collect every menu item.
    Returns (toast_items_by_norm, location_count, errors).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        locations = await get_all_active_locations(client)
        if not locations:
            raise HTTPException(status_code=404, detail="No active locations found")
        standard_headers = await get_standard_api_auth_headers()

        toast_items: Dict[str, dict] = {}
        errors: List[str] = []

        for loc in locations:
            loc_name = (getattr(loc, "restaurantName", None) or "").strip() or loc.restaurantGuid
            try:
                resp = await client.get(
                    f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus",
                    headers={**standard_headers, "Toast-Restaurant-External-ID": loc.restaurantGuid},
                )
                if resp.status_code != 200:
                    errors.append(f"{loc_name}: HTTP {resp.status_code}")
                    continue
                menu_data = resp.json()
                for menu in menu_data.get("menus", []):
                    _collect_items_from_groups(
                        menu.get("menuGroups", []),
                        menu.get("name", ""),
                        loc_name,
                        toast_items,
                    )
            except Exception as exc:
                errors.append(f"{loc_name}: {exc}")

    return toast_items, len(locations), errors


@app.get("/api/audit/toast-vs-mapping")
async def audit_toast_vs_mapping():
    """
    Compare every item on the live Toast menu (V2) across ALL locations against
    Toast_Item_Name in the mapping table.  Returns three buckets:
      unmapped  – Toast items with no matching mapping row
      stale     – Mapping rows whose Toast_Item_Name is not on any current menu
      ok        – Items matched in both directions
    Each item tracks which location names it was found at.
    """
    _, _cols, mapping_rows = _load_mapping_table_excel()

    # Build normalised name → row from mapping
    mapping_by_norm: Dict[str, dict] = {}
    for row in mapping_rows:
        name = (row.get("Toast_Item_Name") or "").strip()
        if name:
            mapping_by_norm[_normalize_item_name(name)] = row

    try:
        toast_items, location_count, errors = await _poll_all_toast_items()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error fetching Toast menu: {exc}")

    # Build secondary index: MLI → toast item (for items with a non-empty MLI)
    toast_by_mli: Dict[str, dict] = {}
    for item in toast_items.values():
        mli = (item.get("mli") or "").strip()
        if mli:
            toast_by_mli[mli] = item

    # Also build secondary index: ToastPLU → toast item
    toast_by_plu: Dict[str, dict] = {}
    for item in toast_items.values():
        plu = (item.get("plu") or "").strip()
        if plu:
            toast_by_plu[plu] = item

    unmapped = []
    ok = []
    for norm, item in toast_items.items():
        if norm in mapping_by_norm:
            ok.append({**item, "mappedAs": mapping_by_norm[norm].get("Toast_Item_Name", "")})
        else:
            unmapped.append(item)

    stale = []
    name_drifted = []
    for norm, row in mapping_by_norm.items():
        if norm in toast_items:
            continue  # already counted in ok

        map_mli = str(row.get("MultiLocationId") or "").strip()
        map_plu = str(row.get("ToastPLU") or "").strip()

        # Secondary match: try MLI first, then ToastPLU
        matched_item = None
        match_method = ""
        if map_mli and map_mli in toast_by_mli:
            matched_item = toast_by_mli[map_mli]
            match_method = "MLI"
        elif map_plu and map_plu in toast_by_plu:
            matched_item = toast_by_plu[map_plu]
            match_method = "PLU"

        if matched_item:
            name_drifted.append({
                "mappingName":     row.get("Toast_Item_Name", ""),
                "currentName":     matched_item["name"],
                "matchMethod":     match_method,
                "mli":             map_mli,
                "toastPLU":        map_plu,
                "guid":            matched_item.get("guid", ""),
                "menu":            matched_item.get("menu", ""),
                "group":           matched_item.get("group", ""),
                "AlohaItemName":   row.get("AlohaItemName", ""),
                "ConsolidatedCat": row.get("ConsolidatedCat", ""),
            })
        else:
            stale.append({
                "Toast_Item_Name":  row.get("Toast_Item_Name", ""),
                "AlohaItemName":    row.get("AlohaItemName", ""),
                "ConsolidatedCat":  row.get("ConsolidatedCat", ""),
                "ToastPLU":         map_plu,
                "MultiLocationId":  map_mli,
            })

    return {
        "summary": {
            "toastItemCount":       len(toast_items),
            "locationCount":        location_count,
            "mappingToastRowCount": len(mapping_by_norm),
            "unmappedCount":        len(unmapped),
            "nameDriftedCount":     len(name_drifted),
            "staleCount":           len(stale),
            "okCount":              len(ok),
            "errors":               errors,
        },
        "unmapped":    sorted(unmapped,     key=lambda x: x["name"].lower()),
        "nameDrifted": sorted(name_drifted, key=lambda x: x["mappingName"].lower()),
        "stale":       sorted(stale,        key=lambda x: x["Toast_Item_Name"].lower()),
        "ok":          sorted(ok,           key=lambda x: x["name"].lower()),
    }


@app.post("/api/audit/sync-from-toast")
async def sync_from_toast():
    """
    Sync Toast-side fields into the mapping table using toast_config.db.
    No live API calls — uses menu items + tags already synced to SQLite.
    - Matches existing rows by item_guid > multi_location_id > toast_plu > normalized name
    - Updates toast_item_name, item_guid, consolidated_cat, guest_count_tag, entree_equiv_tag
    - Returns updated rows, unmapped Toast items, and stale rows
    - Saves updated mapping back to SQLite
    Falls back to legacy Excel + live API flow if toast_config.db is unavailable.
    """
    # ── Get Toast items from DB (fast) or fall back to live API (slow) ──
    if toast_config_db.db_available():
        toast_items = await asyncio.to_thread(toast_config_db.get_toast_items_with_tags)
        location_count = toast_config_db.get_stats().get("stores", 0)
        errors = []
        using_db = True
    else:
        try:
            toast_items, location_count, errors = await _poll_all_toast_items()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error fetching Toast menu: {exc}")
        using_db = False

    # ── Get current mapping rows from DB or Excel ──
    if using_db:
        mapping_rows = toast_config_db.get_mapping_table_rows()
        # Remove internal 'id' key
        for row in mapping_rows:
            row.pop("id", None)
    else:
        _path, _columns, mapping_rows = _load_mapping_table_excel()

    # ── Build Toast lookup indexes ──
    toast_by_guid: Dict[str, Tuple[str, dict]] = {}
    toast_by_mli: Dict[str, Tuple[str, dict]] = {}
    toast_by_plu: Dict[str, Tuple[str, dict]] = {}
    toast_by_norm: Dict[str, dict] = {}

    for norm, item in toast_items.items():
        toast_by_norm[norm] = item
        guid = (item.get("guid") or "").strip()
        if guid:
            toast_by_guid[guid] = (norm, item)
        mli = (item.get("mli") or "").strip()
        if mli:
            toast_by_mli[mli] = (norm, item)
        plu = (item.get("plu") or "").strip()
        if plu:
            toast_by_plu[plu] = (norm, item)

    matched_toast_norms: set = set()
    updated = []
    stale = []

    for row in mapping_rows:
        toast_name = (row.get("toast_item_name") or row.get("Toast_Item_Name") or "").strip()
        if not toast_name:
            continue

        matched_item = None
        match_method = ""

        row_guid = str(row.get("item_guid") or "").strip()
        row_mli = str(row.get("multi_location_id") or row.get("MultiLocationId") or "").strip()
        row_plu = str(row.get("toast_plu") or row.get("ToastPLU") or "").strip()
        row_norm = _normalize_item_name(toast_name)

        if row_guid and row_guid in toast_by_guid:
            norm, matched_item = toast_by_guid[row_guid]
            match_method = "GUID"
            matched_toast_norms.add(norm)
        elif row_mli and row_mli in toast_by_mli:
            norm, matched_item = toast_by_mli[row_mli]
            match_method = "MLI"
            matched_toast_norms.add(norm)
        elif row_plu and row_plu in toast_by_plu:
            norm, matched_item = toast_by_plu[row_plu]
            match_method = "PLU"
            matched_toast_norms.add(norm)
        elif row_norm in toast_by_norm:
            matched_item = toast_by_norm[row_norm]
            match_method = "Name"
            matched_toast_norms.add(row_norm)
        else:
            for key in _mapping_lookup_keys(row_norm):
                if key in toast_by_norm:
                    matched_item = toast_by_norm[key]
                    match_method = "Name"
                    matched_toast_norms.add(key)
                    break

        if matched_item:
            changes = {}
            new_name = matched_item["name"]
            new_guid = matched_item.get("guid", "")
            new_mli = matched_item.get("mli", "")
            new_plu = matched_item.get("plu", "")

            # Derive tag-based columns
            new_consolidated = ""
            new_guest_count = ""
            new_ee = ""
            for t in matched_item.get("tags", []):
                if not new_consolidated and t.startswith("* Consolidated"):
                    new_consolidated = t
                elif not new_guest_count and t.startswith("Guest Count"):
                    new_guest_count = t
                elif not new_ee and t.startswith("EE "):
                    new_ee = t

            # Detect changes (use DB column names)
            old_name = row.get("toast_item_name") or row.get("Toast_Item_Name", "")
            if old_name != new_name:
                changes["toast_item_name"] = {"old": old_name, "new": new_name}
            old_guid = str(row.get("item_guid", ""))
            if old_guid != new_guid:
                changes["item_guid"] = {"old": old_guid, "new": new_guid}
            old_mli = str(row.get("multi_location_id") or row.get("MultiLocationId", ""))
            if old_mli != new_mli:
                changes["multi_location_id"] = {"old": old_mli, "new": new_mli}
            old_plu = str(row.get("toast_plu") or row.get("ToastPLU", ""))
            if old_plu != new_plu:
                changes["toast_plu"] = {"old": old_plu, "new": new_plu}
            old_consol = str(row.get("consolidated_cat") or row.get("ConsolidatedCat", ""))
            if new_consolidated and old_consol != new_consolidated:
                changes["consolidated_cat"] = {"old": old_consol, "new": new_consolidated}
            old_gc = str(row.get("guest_count_tag") or row.get("GuestCountTag", ""))
            if new_guest_count and old_gc != new_guest_count:
                changes["guest_count_tag"] = {"old": old_gc, "new": new_guest_count}
            old_ee = str(row.get("entree_equiv_tag") or row.get("EntreEquivTag", ""))
            if new_ee and old_ee != new_ee:
                changes["entree_equiv_tag"] = {"old": old_ee, "new": new_ee}

            # Apply updates to row (DB column names)
            row["toast_item_name"] = new_name
            row["item_guid"] = new_guid
            row["multi_location_id"] = new_mli
            row["toast_plu"] = new_plu
            if new_consolidated:
                row["consolidated_cat"] = new_consolidated
            if new_guest_count:
                row["guest_count_tag"] = new_guest_count
            if new_ee:
                row["entree_equiv_tag"] = new_ee

            if changes:
                updated.append({
                    "Toast_Item_Name": new_name,
                    "matchMethod":     match_method,
                    "changes":         changes,
                    "AlohaItemName":   row.get("aloha_item_name") or row.get("AlohaItemName", ""),
                    "ConsolidatedCat": row.get("consolidated_cat") or row.get("ConsolidatedCat", ""),
                })
        else:
            stale.append({
                "Toast_Item_Name":  toast_name,
                "AlohaItemName":    row.get("aloha_item_name") or row.get("AlohaItemName", ""),
                "ConsolidatedCat":  row.get("consolidated_cat") or row.get("ConsolidatedCat", ""),
                "ToastPLU":         str(row.get("toast_plu") or row.get("ToastPLU") or ""),
                "MultiLocationId":  str(row.get("multi_location_id") or row.get("MultiLocationId") or ""),
            })

    # Unmapped: Toast items that didn't match any mapping row
    unmapped = []
    for norm, item in toast_items.items():
        if norm not in matched_toast_norms:
            unmapped.append(item)

    # ── Save back ──
    if using_db:
        toast_config_db.update_mapping_table_rows(mapping_rows)
        saved_to = "toast_config.db"
    else:
        new_columns = list(_columns)
        for col in ("item_guid", "Tags", "ConsolidatedCat", "GuestCountTag", "EntreEquivTag"):
            if col not in new_columns:
                new_columns.append(col)
        df = pd.DataFrame(mapping_rows, columns=new_columns)
        saved_to = _safe_write_excel(df, _path)

    # Invalidate consolidated category cache
    global _consolidated_cat_cache
    _consolidated_cat_cache = {}

    return {
        "summary": {
            "toastItemCount":   len(toast_items),
            "locationCount":    location_count,
            "mappingRowCount":  len([r for r in mapping_rows if (r.get("toast_item_name") or r.get("Toast_Item_Name") or "").strip()]),
            "updatedCount":     len(updated),
            "unmappedCount":    len(unmapped),
            "staleCount":       len(stale),
            "errors":           errors,
            "savedTo":          saved_to,
        },
        "updated":   sorted(updated,   key=lambda x: x["Toast_Item_Name"].lower()),
        "unmapped":  sorted(unmapped,   key=lambda x: x["name"].lower()),
        "stale":     sorted(stale,      key=lambda x: x["Toast_Item_Name"].lower()),
    }


@app.post("/api/audit/aloha-vs-mapping")
async def audit_aloha_vs_mapping(file: UploadFile = File(...)):
    """
    Parse an uploaded ITM.DBF (Aloha item master) and compare LONGNAME / ID
    against AlohaItemName / AlohaPLU in the mapping table.  Returns:
      unmappedAloha  – DBF items with no matching mapping row
      stale          – Mapping rows whose AlohaItemName is not in the DBF
      pluMismatches  – Name matches but AlohaPLU ≠ DBF ID
      ok             – Fully matched
    """
    try:
        from dbfread import DBF as DbfReader
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="dbfread not installed — run: pip install dbfread"
        )

    import tempfile
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".dbf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        dbf = DbfReader(tmp_path, encoding="latin-1", load=True)
        dbf_items: Dict[str, dict] = {}
        for rec in dbf:
            longname = (rec.get("LONGNAME") or "").strip()
            if not longname:
                continue
            norm = _normalize_item_name(longname)
            if norm not in dbf_items:
                dbf_items[norm] = {
                    "longname":  longname,
                    "shortname": (rec.get("SHORTNAME") or "").strip(),
                    "id":        rec.get("ID"),
                }
    finally:
        os.unlink(tmp_path)

    _, _cols, mapping_rows = _load_mapping_table_excel()
    mapping_aloha: Dict[str, dict] = {}
    for row in mapping_rows:
        name = (row.get("AlohaItemName") or "").strip()
        if name:
            mapping_aloha[_normalize_item_name(name)] = row

    unmapped_aloha, plu_mismatches, ok = [], [], []
    for norm, dbf_item in dbf_items.items():
        if norm in mapping_aloha:
            row      = mapping_aloha[norm]
            map_plu  = str(row.get("AlohaPLU") or "").strip()
            dbf_plu  = str(dbf_item["id"] or "").strip() if dbf_item["id"] is not None else ""
            if map_plu and dbf_plu and map_plu != dbf_plu:
                plu_mismatches.append({
                    **dbf_item,
                    "mappedAlohaName":  row.get("AlohaItemName", ""),
                    "mappingAlohaPLU": map_plu,
                    "dbfPLU":          dbf_plu,
                })
            else:
                ok.append({**dbf_item, "mappingRow": {
                    "AlohaItemName":  row.get("AlohaItemName", ""),
                    "Toast_Item_Name": row.get("Toast_Item_Name", ""),
                    "ConsolidatedCat": row.get("ConsolidatedCat", ""),
                    "AlohaPLU":       row.get("AlohaPLU", ""),
                }})
        else:
            unmapped_aloha.append(dbf_item)

    stale = []
    for norm, row in mapping_aloha.items():
        if norm not in dbf_items:
            stale.append({
                "AlohaItemName":   row.get("AlohaItemName", ""),
                "Toast_Item_Name": row.get("Toast_Item_Name", ""),
                "ConsolidatedCat": row.get("ConsolidatedCat", ""),
                "AlohaPLU":        row.get("AlohaPLU", ""),
            })

    return {
        "summary": {
            "dbfItemCount":         len(dbf_items),
            "mappingAlohaRowCount": len(mapping_aloha),
            "unmappedCount":        len(unmapped_aloha),
            "staleCount":           len(stale),
            "pluMismatchCount":     len(plu_mismatches),
            "okCount":              len(ok),
        },
        "unmappedAloha":  sorted(unmapped_aloha,  key=lambda x: x["longname"].lower()),
        "stale":          sorted(stale,            key=lambda x: x["AlohaItemName"].lower()),
        "pluMismatches":  sorted(plu_mismatches,   key=lambda x: x["longname"].lower()),
        "ok":             sorted(ok,               key=lambda x: x["longname"].lower()),
    }


# ------------------
# Saved Reports API
# ------------------

@app.get("/api/saved-reports")
async def get_saved_reports():
    return load_saved_reports()

@app.get("/api/saved-reports/{report_id}")
async def get_saved_report(report_id: str):
    reports = load_saved_reports()
    report = next((r for r in reports if r.id == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report

@app.post("/api/saved-reports")
async def create_saved_report(report: SavedReportCreate = Body(...)):
    reports = load_saved_reports()
    
    if any(r.name.lower() == report.name.lower() for r in reports):
        raise HTTPException(status_code=400, detail="A report with this name already exists")
    
    new_report = SavedReport(
        id=str(uuid.uuid4()),
        name=report.name,
        description=report.description or "",
        createdBy=report.createdBy,
        createdAt=datetime.now().isoformat(),
        lastModified=datetime.now().isoformat(),
        config=report.config
    )
    reports.append(new_report)
    save_saved_reports(reports)
    
    return new_report

@app.put("/api/saved-reports/{report_id}")
async def update_saved_report(report_id: str, report: SavedReportCreate = Body(...)):
    reports = load_saved_reports()
    
    index = None
    for i, r in enumerate(reports):
        if r.id == report_id:
            index = i
            break
    
    if index is None:
        raise HTTPException(status_code=404, detail="Report not found")
    
    if any(r.name.lower() == report.name.lower() and r.id != report_id for r in reports):
        raise HTTPException(status_code=400, detail="A report with this name already exists")
    
    updated_report = SavedReport(
        id=report_id,
        name=report.name,
        description=report.description or "",
        createdBy=reports[index].createdBy,
        createdAt=reports[index].createdAt,
        lastModified=datetime.now().isoformat(),
        config=report.config
    )
    reports[index] = updated_report
    save_saved_reports(reports)
    
    return updated_report

@app.delete("/api/saved-reports/{report_id}")
async def delete_saved_report(report_id: str):
    reports = load_saved_reports()
    original_count = len(reports)
    reports = [r for r in reports if r.id != report_id]
    
    if len(reports) == original_count:
        raise HTTPException(status_code=404, detail="Report not found")
    
    save_saved_reports(reports)
    return {"message": "Report deleted successfully"}

# ------------------
# Report Execution API
# ------------------

def compute_royalty_by_location(raw_rows: list) -> Dict:
    """
    Aggregate ERA netSalesAmount per location and compute royalty (5%) and MKT fund (1.75%).

    Returns:
    {
        "royaltyTable": [
            { "restaurantName", "netSales", "royalty5", "mktFund175" },
            ...
        ],
        "royaltyTotals": { "netSales", "royalty5", "mktFund175", "locationCount" }
    }
    """
    ROYALTY_RATE = 0.05
    MKT_FUND_RATE = 0.0175

    loc: Dict[str, float] = {}
    for row in raw_rows:
        r_name = row.get("restaurantName", "Unknown")
        ns = float(row.get("netSalesAmount") or 0)
        loc[r_name] = loc.get(r_name, 0.0) + ns

    table = []
    tot_ns = 0.0
    for r_name in sorted(loc.keys()):
        ns = round(loc[r_name], 2)
        r5 = round(ns * ROYALTY_RATE, 2)
        mkt = round(ns * MKT_FUND_RATE, 2)
        table.append({
            "restaurantName": r_name,
            "netSales": ns,
            "royalty5": r5,
            "mktFund175": mkt,
        })
        tot_ns += ns

    tot_ns = round(tot_ns, 2)
    return {
        "royaltyTable": table,
        "royaltyTotals": {
            "netSales": tot_ns,
            "royalty5": round(tot_ns * ROYALTY_RATE, 2),
            "mktFund175": round(tot_ns * MKT_FUND_RATE, 2),
            "locationCount": len(table),
        },
    }


_GUEST_COUNT_RE = re.compile(r"^Guest\s*Count\s+([\d.]+)$", re.IGNORECASE)

def parse_guest_count_value(tag_name: str) -> Optional[float]:
    """Parse a Guest Count tag name like 'Guest Count 0.5' → 0.5. Returns None if not a GC tag."""
    m = _GUEST_COUNT_RE.match(tag_name.strip())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


async def build_guest_count_report(
    client: httpx.AsyncClient,
    report_config: ReportConfig,
    restaurant_ids: List[str],
    start_date: date,
    end_date: date,
    locations: Optional[List] = None,
) -> dict:
    """Build guest count report: qty sold × Guest Count tag value per item."""

    # ── 1. Build GUID → guest count value lookup ──
    # Prefer toast_config.db (fast, no API call); fall back to live Toast API
    gc_by_guid: Dict[str, Tuple[float, str]] = {}  # guid → (value, tagName)

    if toast_config_db.db_available():
        attr_rows = toast_config_db.get_mapping_table_rows()
        for row in attr_rows:
            gc_val = row.get("guest_count_tag", 0)
            guid = row.get("item_guid", "")
            if gc_val and guid:
                gc_by_guid[guid] = (float(gc_val), f"Guest Count {gc_val}")
        logger.info(f"Guest count report: {len(gc_by_guid)} items from toast_config.db")
    else:
        # Fallback: fetch tags from live Toast API
        if locations:
            restaurant_ids = prioritize_menu_tag_restaurants(restaurant_ids, locations)
        tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
        logger.info(f"Guest count report: tag mapping has {len(tags_mapping)} items")
        for guid, tags in tags_mapping.items():
            for tag in tags:
                val = parse_guest_count_value(tag.get("tagName", ""))
                if val is not None:
                    gc_by_guid[guid] = (val, tag["tagName"])
                    break
    logger.info(f"Guest count report: {len(gc_by_guid)} items have Guest Count tags")

    if not gc_by_guid:
        return {
            "guestCountTable": [],
            "guestCountTotals": {"totalGuestCount": 0, "totalQtySold": 0, "locationCount": 0, "itemCount": 0},
            "guestCountItemBreakdown": [],
        }

    # ── 3. Fetch menu mix data (MENU_ITEM analytics, weekly chunks) ──
    days_diff = (end_date - start_date).days + 1
    all_rows: List[Dict] = []
    if days_diff > 7:
        current_start = start_date
        while current_start <= end_date:
            chunk_end = min(current_start + timedelta(days=6), end_date)
            report_guid = await request_toast_report(
                client, current_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
            )
            chunk_rows = await fetch_toast_report(client, report_guid, "menu")
            if chunk_rows:
                all_rows.extend(chunk_rows)
            current_start = chunk_end + timedelta(days=1)
    else:
        report_guid = await request_toast_report(
            client, start_date, end_date, restaurant_ids, "menu", ["MENU_ITEM"]
        )
        all_rows = await fetch_toast_report(client, report_guid, "menu") or []

    logger.info(f"Guest count report: {len(all_rows)} analytics rows fetched")

    # ── 4. Also fetch MODIFIER analytics ──
    modifier_rows: List[Dict] = []
    if days_diff > 7:
        current_start = start_date
        while current_start <= end_date:
            chunk_end = min(current_start + timedelta(days=6), end_date)
            mod_guid = await request_toast_report(
                client, current_start, chunk_end, restaurant_ids, "menu", ["MODIFIER"]
            )
            chunk = await fetch_toast_report(client, mod_guid, "menu")
            if chunk:
                modifier_rows.extend(chunk)
            current_start = chunk_end + timedelta(days=1)
    else:
        mod_guid = await request_toast_report(
            client, start_date, end_date, restaurant_ids, "menu", ["MODIFIER"]
        )
        modifier_rows = await fetch_toast_report(client, mod_guid, "menu") or []

    all_rows.extend(modifier_rows)

    # Get display name mapping
    cache_entry = _menu_tags_cache.get(restaurant_ids[0], {})
    guid_to_name = cache_entry.get("guid_to_name", {})

    # ── 5. Process rows: multiply qty × guest count value ──
    # Accumulate by location and by item
    loc_totals: Dict[str, Dict] = {}  # restaurantGuid → {name, totalQty, totalGC, items set}
    item_rows: List[Dict] = []

    for row in all_rows:
        item_guid = row.get("menuItemGuid") or row.get("modifierGuid") or ""
        if item_guid not in gc_by_guid:
            continue

        gc_value, gc_tag = gc_by_guid[item_guid]
        qty = float(row.get("quantitySold", 0) or 0)
        if qty == 0:
            continue

        contribution = round(qty * gc_value, 2)
        rest_guid = row.get("restaurantGuid", "")
        rest_name = row.get("restaurantName", "Unknown")
        item_name = guid_to_name.get(item_guid) or row.get("menuItemName") or row.get("modifierName") or "Unknown Item"

        item_rows.append({
            "restaurantGuid": rest_guid,
            "restaurantName": rest_name,
            "menuItemName": item_name,
            "quantitySold": qty,
            "guestCountValue": gc_value,
            "guestContribution": contribution,
        })

        if rest_guid not in loc_totals:
            loc_totals[rest_guid] = {"restaurantName": rest_name, "totalQtySold": 0, "totalGuestCount": 0, "items": set()}
        loc_totals[rest_guid]["totalQtySold"] += qty
        loc_totals[rest_guid]["totalGuestCount"] += contribution
        loc_totals[rest_guid]["items"].add(item_guid)

    # ── 6. Build response ──
    guest_count_table = sorted([
        {
            "restaurantGuid": guid,
            "restaurantName": info["restaurantName"],
            "totalQtySold": round(info["totalQtySold"], 2),
            "totalGuestCount": round(info["totalGuestCount"], 2),
            "itemCount": len(info["items"]),
        }
        for guid, info in loc_totals.items()
    ], key=lambda x: x["restaurantName"])

    grand_qty = sum(r["totalQtySold"] for r in guest_count_table)
    grand_gc = sum(r["totalGuestCount"] for r in guest_count_table)
    # Count unique items that actually had sales (union across all stores)
    all_sold_items = set()
    for info in loc_totals.values():
        all_sold_items |= info["items"]

    # Sort item breakdown by location then item name
    item_rows.sort(key=lambda x: (x["restaurantName"], x["menuItemName"]))

    return {
        "guestCountTable": guest_count_table,
        "guestCountTotals": {
            "totalGuestCount": round(grand_gc, 2),
            "totalQtySold": round(grand_qty, 2),
            "locationCount": len(guest_count_table),
            "itemCount": len(all_sold_items),
        },
        "guestCountItemBreakdown": item_rows,
    }


async def build_menu_mix_report_fallback(
    client: httpx.AsyncClient,
    report_config: ReportConfig,
    restaurant_ids: List[str],
    start_date: date,
    end_date: date,
    response: Optional[Response] = None,
) -> dict:
    if response is not None:
        response.headers["X-Menu-Path"] = "forced_menu"

    if TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID or TOAST_MENU_TAGS_PRIORITY_NAME:
        locations = await get_all_active_locations(client)
        restaurant_ids = prioritize_menu_tag_restaurants(restaurant_ids, locations)
    
    tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
    
    if report_config.itemTags and len(report_config.itemTags) > 0:
        selected_tags_lower = [t.lower() for t in report_config.itemTags]
        selected_tags_norm = [_normalize_tag_name(t) for t in report_config.itemTags]
        # Use normalized tag comparison so "* Consolidated Pizza" and "Consolidated Pizza" match
        filtered_mapping = {
            guid: [t for t in tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
            for guid, tags in tags_mapping.items()
            if any(_normalize_tag_name(t["tagName"]) in selected_tags_norm for t in tags)
        }
    else:
        filtered_mapping = dict(tags_mapping)
    
    if len(filtered_mapping) == 0 and report_config.itemTags:
        return {"pivotTable": [], "totals": [], "itemBreakdown": [], "itemBreakdownDaily": [], "dateColumns": []}
    consolidated_cat_map = get_consolidated_category_map()
    report_tag_map = get_report_tag_map()
    consolidated_override = get_consolidated_override_map()
    consolidated_fallback_hits = 0

    name_to_tags: Dict[str, List[Dict]] = {}
    if report_config.itemTags and restaurant_ids:
        cache_entry = _menu_tags_cache.get(restaurant_ids[0], {})
        guid_to_name = cache_entry.get("guid_to_name", {})
        for guid, tags in tags_mapping.items():
            item_name = guid_to_name.get(guid)
            if not item_name:
                continue
            key = _normalize_item_name(item_name)
            if not key:
                continue
            existing = {t["tagName"] for t in name_to_tags.get(key, [])}
            merged = name_to_tags.get(key, [])
            for tag in tags:
                if tag["tagName"] not in existing:
                    merged.append(tag)
            name_to_tags[key] = merged
    
    # Fetch MENU_ITEM analytics (week chunks if needed)
    days_diff = (end_date - start_date).days + 1
    all_rows: List[Dict] = []
    modifier_rows: List[Dict] = []

    # ── Local DB short-circuit (same as main menu path) ──────────────────────
    _fb_use_local = False
    try:
        _fb_has_coverage = await asyncio.to_thread(
            toast_menu_raw_db.has_full_coverage,
            start_date.isoformat(), end_date.isoformat(),
        )
        if _fb_has_coverage:
            all_rows = await asyncio.to_thread(
                toast_menu_raw_db.query_rows_for_report,
                start_date.isoformat(), end_date.isoformat(),
                restaurant_ids, "MENU_ITEM",
            )
            modifier_rows = await asyncio.to_thread(
                toast_menu_raw_db.query_rows_for_report,
                start_date.isoformat(), end_date.isoformat(),
                restaurant_ids, "MODIFIER",
            )
            _fb_use_local = True
            logger.info(
                "menu fallback: using local DB (%d menu items + %d modifiers) — skipping Toast Analytics",
                len(all_rows), len(modifier_rows),
            )
            if response is not None:
                response.headers["X-Menu-Source"] = "local-db"
    except Exception as _exc:
        logger.warning("local DB short-circuit failed in fallback, falling back to Toast: %s", _exc)
        _fb_use_local = False
        all_rows = []
        modifier_rows = []

    if _fb_use_local:
        pass  # all_rows + modifier_rows populated above
    elif days_diff > 7:
        current_start = start_date
        while current_start <= end_date:
            chunk_end = min(current_start + timedelta(days=6), end_date)
            report_guid = await request_toast_report(
                client,
                current_start,
                chunk_end,
                restaurant_ids,
                "menu",
                ["MENU_ITEM"]
            )
            chunk_rows = await fetch_toast_report(client, report_guid, "menu")
            if chunk_rows:
                all_rows.extend(chunk_rows)
            current_start = chunk_end + timedelta(days=1)
    else:
        report_guid = await request_toast_report(
            client,
            start_date,
            end_date,
            restaurant_ids,
            "menu",
            ["MENU_ITEM"]
        )
        all_rows = await fetch_toast_report(client, report_guid, "menu")

    menu_item_count = len(all_rows)

    # Fetch MODIFIER analytics and merge (or use local-DB rows from above)
    if _fb_use_local:
        pass  # modifier_rows already loaded — skip Toast fetch
    elif days_diff > 7:
        current_start = start_date
        while current_start <= end_date:
            chunk_end = min(current_start + timedelta(days=6), end_date)
            modifier_guid = await request_toast_report(
                client,
                current_start,
                chunk_end,
                restaurant_ids,
                "menu",
                ["MODIFIER"]
            )
            chunk_rows = await fetch_toast_report(client, modifier_guid, "menu")
            if chunk_rows:
                modifier_rows.extend(chunk_rows)
            current_start = chunk_end + timedelta(days=1)
    else:
        modifier_guid = await request_toast_report(
            client,
            start_date,
            end_date,
            restaurant_ids,
            "menu",
            ["MODIFIER"]
        )
        modifier_rows = await fetch_toast_report(client, modifier_guid, "menu")
    
    if modifier_rows:
        for row in modifier_rows:
            if "modifierGuid" in row:
                row["menuItemGuid"] = row.pop("modifierGuid")
            elif "modifierOptionGuid" in row:
                row["menuItemGuid"] = row.pop("modifierOptionGuid")
            if "modifierName" in row:
                row["menuItemName"] = row.pop("modifierName")
            elif "modifierOptionName" in row:
                row["menuItemName"] = row.pop("modifierOptionName")
        menu_item_guids_set = {r.get("menuItemGuid") for r in all_rows[:menu_item_count] if r.get("menuItemGuid")}
        modifier_rows = [r for r in modifier_rows if r.get("menuItemGuid") not in menu_item_guids_set]
        all_rows.extend(modifier_rows)
    
    if not all_rows:
        return {"pivotTable": [], "totals": [], "itemBreakdown": [], "itemBreakdownDaily": [], "dateColumns": []}
    
    # Prefer display names from menu cache over names from Toast analytics (which may be kitchen names, e.g. "cup pepjac&craw")
    guid_to_display_name: Dict[str, str] = {}
    for rid in restaurant_ids:
        if rid in _menu_tags_cache and "guid_to_name" in _menu_tags_cache[rid]:
            guid_to_display_name.update(_menu_tags_cache[rid]["guid_to_name"])
    for row in all_rows:
        guid = row.get("menuItemGuid")
        if guid and guid in guid_to_display_name:
            row["menuItemName"] = guid_to_display_name[guid]
    
    # Build item names mapping
    item_names_mapping: Dict[str, str] = {}
    for row in all_rows:
        guid = row.get("menuItemGuid")
        name = row.get("menuItemName")
        if guid and name:
            item_names_mapping[guid] = name

    # Fill missing names for zero-sales items
    if report_config.includeZeroSales:
        missing_guids = set(filtered_mapping.keys()) - set(item_names_mapping.keys())
        if missing_guids:
            # Try cache-based names first (if available)
            for restaurant_id in restaurant_ids:
                if restaurant_id in _menu_tags_cache and "guid_to_name" in _menu_tags_cache[restaurant_id]:
                    guid_to_name = _menu_tags_cache[restaurant_id]["guid_to_name"]
                    for guid in list(missing_guids):
                        if guid in guid_to_name:
                            item_names_mapping[guid] = guid_to_name[guid]
                            missing_guids.discard(guid)
                if not missing_guids:
                    break

            if missing_guids:
                try:
                    standard_headers = await get_standard_api_auth_headers()
                    headers_with_guid = {
                        **standard_headers,
                        "Toast-Restaurant-External-ID": restaurant_ids[0]
                    }
                    menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus"
                    menus_resp = await client.get(menus_url, headers=headers_with_guid)
                    if menus_resp.status_code == 200:
                        menus_data = menus_resp.json()
                        # Extract item names
                        for menu in menus_data.get("menus", []):
                            for group in menu.get("menuGroups", []):
                                for item in group.get("menuItems", []):
                                    guid = item.get("guid")
                                    if guid in missing_guids:
                                        name = item.get("displayName") or item.get("name", "")
                                        if name:
                                            item_names_mapping[guid] = name
                                            missing_guids.discard(guid)
                        # Extract modifier names
                        modifier_group_refs = menus_data.get("modifierGroupReferences", {})
                        modifier_option_refs = menus_data.get("modifierOptionReferences", {})
                        for mod_group in modifier_group_refs.values():
                            for mod_option in mod_group.get("modifierOptions", []):
                                guid = mod_option.get("guid")
                                if guid in missing_guids:
                                    name = mod_option.get("displayName") or mod_option.get("name", "")
                                    if name:
                                        item_names_mapping[guid] = name
                                        missing_guids.discard(guid)
                        for mod_option in modifier_option_refs.values():
                            guid = mod_option.get("guid", "")
                            if guid in missing_guids:
                                name = mod_option.get("displayName") or mod_option.get("name", "")
                                if name:
                                    item_names_mapping[guid] = name
                                    missing_guids.discard(guid)
                except Exception as e:
                    logger.warning(f"Failed to fetch menu names for zero-sales items (fallback): {e}")
    
    # Filter analytics rows by selected tags if any
    filtered_rows = []
    debug_mapping_fallback_names = [] if getattr(report_config, "debug", False) else None
    if report_config.itemTags:
        for row in all_rows:
            guid = row.get("menuItemGuid")
            if guid in filtered_mapping:
                filtered_rows.append(row)
                continue
            name_key = _normalize_item_name(row.get("menuItemName", ""))
            report_tags = _get_tag_from_mapping(name_key, report_tag_map, consolidated_cat_map, consolidated_override, guid)
            matching_report_tags = [t for t in report_tags if _normalize_tag_name(t) in selected_tags_norm]
            if matching_report_tags:
                filtered_mapping[guid] = [{"tagName": t} for t in matching_report_tags]
                filtered_rows.append(row)
                consolidated_fallback_hits += 1
                if debug_mapping_fallback_names is not None:
                    debug_mapping_fallback_names.append(row.get("menuItemName", ""))
                continue
            # Fallback: match by item name to tag mapping (risk of mis-tagging)
            if name_key and name_key in name_to_tags:
                matching_tags = [t for t in name_to_tags[name_key] if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                if matching_tags:
                    filtered_mapping[guid] = matching_tags
                    filtered_rows.append(row)
        if consolidated_fallback_hits:
            logger.info(f"ConsolidatedCat fallback hits (menu_fallback): {consolidated_fallback_hits}")

        debug_names = [
            "Pepperoni & Portabella Pizza (Entree)",
            "Pepperoni & Mushroom Pizza (Full Pairing Entree)"
        ]
        for name in debug_names:
            consolidated_cat = consolidated_cat_map.get(_normalize_item_name(name))
            logger.info(
                f"Menu fallback tag compare debug for {name}: "
                f"selected_raw={report_config.itemTags}, "
                f"selected_norm={[_normalize_tag_name(t) for t in report_config.itemTags]}, "
                f"consolidated_cat={consolidated_cat}, "
                f"consolidated_norm={_normalize_tag_name(consolidated_cat) if consolidated_cat else ''}"
            )
    else:
        filtered_rows = all_rows

    # Add mapping-only items (in mapping table with selected tag but no analytics row) so they
    # appear in the breakdown and Toast Items popup with count 0.
    if report_config.itemTags and selected_tags_norm:
        names_in_filtered = {_normalize_item_name(r.get("menuItemName", "")) for r in filtered_rows if r.get("menuItemName")}
        # report_tag_map values are List[str]; consolidated_cat_map values are str
        for norm_name, tags in report_tag_map.items():
            if not norm_name or not tags:
                continue
            matching = [t for t in tags if _normalize_tag_name(t) in selected_tags_norm]
            if not matching:
                continue
            if norm_name in names_in_filtered:
                continue
            synthetic_guid = "mapping:" + norm_name
            if synthetic_guid in filtered_mapping:
                continue
            filtered_mapping[synthetic_guid] = [{"tagName": t} for t in matching]
            item_names_mapping[synthetic_guid] = _humanize_item_name(norm_name)
            names_in_filtered.add(norm_name)
        for norm_name, cat in consolidated_cat_map.items():
            if not norm_name or not cat:
                continue
            if _normalize_tag_name(cat) not in selected_tags_norm:
                continue
            if norm_name in names_in_filtered:
                continue
            synthetic_guid = "mapping:" + norm_name
            if synthetic_guid in filtered_mapping:
                continue
            filtered_mapping[synthetic_guid] = [{"tagName": cat}]
            item_names_mapping[synthetic_guid] = _humanize_item_name(norm_name)
            names_in_filtered.add(norm_name)

    # Tag/mapping debug for fallback path (when report_config.debug is True)
    debug_info_extra = None
    if getattr(report_config, "debug", False) and report_config.itemTags:
        _nk = _normalize_item_name
        _tk = _normalize_tag_name
        mapping_keys_pepperoni = sorted(
            k for k in list(report_tag_map.keys()) + list(consolidated_cat_map.keys())
            if "pepperoni" in k or "mushroom" in k or "portabella" in k
        )[:50]
        analytics_names_pepperoni = sorted(
            set(r.get("menuItemName", "") for r in all_rows if r.get("menuItemName"))
        )
        analytics_names_pepperoni = [n for n in analytics_names_pepperoni if "pepperoni" in n.lower() or "mushroom" in n.lower() or "portabella" in n.lower()][:30]
        target_names = [
            "Pepperoni & Mushroom Pizza (Full Pairing Entree)",
            "Pepperoni & Portabella Pizza (Entree)",
        ]
        target_details = []
        for tn in target_names:
            nkey = _nk(tn)
            tags_from_map = _get_tag_from_mapping(nkey, report_tag_map, consolidated_cat_map, consolidated_override, "")
            in_analytics = any(_nk(r.get("menuItemName", "")) == nkey or tn in (r.get("menuItemName") or "") for r in all_rows)
            in_filtered = any(_nk(r.get("menuItemName", "")) == nkey or tn in (r.get("menuItemName") or "") for r in filtered_rows)
            target_details.append({
                "name": tn,
                "normalizedKey": nkey,
                "tagsFromMappingTable": tags_from_map,
                "tagsNormalized": [_tk(t) for t in tags_from_map],
                "selectedTagsNormalized": selected_tags_norm[:5],
                "inAnalyticsRows": in_analytics,
                "inFilteredRows": in_filtered,
                "inReportTagMap": nkey in report_tag_map,
                "inConsolidatedCatMap": nkey in consolidated_cat_map,
            })
        debug_info_extra = {
            "consolidatedFallbackHits": consolidated_fallback_hits,
            "mappingFallbackItemNames": debug_mapping_fallback_names or [],
            "mappingTableKeysContainingPepperoniMushroomPortabella": mapping_keys_pepperoni,
            "analyticsNamesContainingPepperoniMushroomPortabella": analytics_names_pepperoni,
            "targetItems": target_details,
            "totalAnalyticsRows": len(all_rows),
            "totalFilteredRows": len(filtered_rows),
            "menuItemCount": menu_item_count,
        }
    
    menu_item_guids = {r.get("menuItemGuid") for r in all_rows[:menu_item_count] if r.get("menuItemGuid")}
    # Pull the live PLU mapping captured during the same V3 fetch that built
    # tags_mapping. Pivot uses this as its authoritative PLU source.
    _live_plu_for_pivot = (_menu_tags_cache.get(restaurant_ids[0], {})
                           .get("plu_mapping") or {})
    result = pivot_menu_items_by_tag(
        filtered_rows,
        filtered_mapping,
        report_config.itemTags,
        item_names_mapping,
        menu_item_guids,
        report_config.includeZeroSales,
        _live_plu_for_pivot,
    )
    result["debugInfo"] = {
        "path": "menu_fallback",
        "itemTags": len(report_config.itemTags or []),
        "includeZeroSales": bool(report_config.includeZeroSales),
        "yoyEnabled": bool(report_config.yoyEnabled),
        "alohaDate": report_config.alohaDate
    }
    if debug_info_extra is not None:
        result["debugInfo"]["tagMappingDebug"] = debug_info_extra
    result = await apply_yoy_aloha_quantities(result, report_config, start_date, end_date)
    result = await apply_yoy_toast_quantities(result, report_config, start_date, end_date)
    return result

@app.post("/api/reports/run")
async def run_report(report_config: ReportConfig = Body(...), response: Response = None, request: Request = None):
    """Run a report with the given configuration"""

    if response is not None:
        response.headers["X-Backend-Version"] = "menu-mix-v3"
        response.headers["X-Report-Type-Received"] = str(report_config.reportType)
        response.headers["X-ItemTags-Count"] = str(len(report_config.itemTags or []))

    report_type = (report_config.reportType or "").strip().lower()
    if "menu" in report_type:
        report_type = "menu"
    report_config.reportType = report_type

    # Calculate date range
    if report_config.dateRangeType == "custom":
        if not report_config.customStartDate or not report_config.customEndDate:
            raise HTTPException(status_code=400, detail="Custom date range requires start and end dates")
        start_date = date.fromisoformat(report_config.customStartDate)
        end_date = date.fromisoformat(report_config.customEndDate)
    elif report_config.dateRangeType == "last_7_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=6)
    elif report_config.dateRangeType == "last_30_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=29)
    elif report_config.dateRangeType == "last_90_days":
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=89)
    else:
        raise HTTPException(status_code=400, detail="Invalid date range type")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")

    # ── Precomputed menu-report cache (read-through) ──────────────────────────
    # Heavy menu reports (full month × all stores × modifier pivot) regularly
    # blow past nginx's ~60s timeout and crash the worker. A nightly precompute
    # job warms a disk cache for common shapes; we check it here before doing
    # the heavy work. Bypass via X-Force-Refresh header (the precompute job
    # itself uses this to actually do the work).
    _menu_cache_key: Optional[str] = None
    if report_config.reportType == "menu" and not report_config.debug:
        force_refresh = False
        if request is not None:
            force_refresh = str(request.headers.get("X-Force-Refresh", "")).strip() in ("1", "true", "yes")
        try:
            _menu_cache_key = precompute_cache.cache_key(
                report_config.dict(),
                start_date.isoformat(),
                end_date.isoformat(),
            )
        except Exception as _exc:
            logger.warning("precompute_cache: key build failed: %s", _exc)
            _menu_cache_key = None
        if _menu_cache_key and not force_refresh:
            # Try exact-match first, then fall back to the Consolidated-tags
            # superset (and filter the result) if the user picked any subset.
            hit = precompute_cache.lookup_with_superset_fallback(
                report_config.dict(),
                start_date.isoformat(),
                end_date.isoformat(),
            )
            if hit is not None:
                match_kind = hit.get("match", "exact")
                if response is not None:
                    response.headers["X-Menu-Cache"] = (
                        "hit" if match_kind == "exact" else "hit-superset"
                    )
                    response.headers["X-Menu-Cache-Key"] = hit.get("key") or _menu_cache_key
                logger.info(
                    "menu cache HIT (%s) key=%s range=%s..%s",
                    match_kind, hit.get("key"),
                    start_date.isoformat(), end_date.isoformat(),
                )
                return hit["result"]
        if response is not None and _menu_cache_key:
            response.headers["X-Menu-Cache"] = "miss"
            response.headers["X-Menu-Cache-Key"] = _menu_cache_key

    def _cache_write_menu(result_to_cache):
        """Write a freshly-computed menu result back to the cache. Best-effort."""
        if not _menu_cache_key or not isinstance(result_to_cache, dict):
            return result_to_cache
        try:
            precompute_cache.cache_set(
                _menu_cache_key,
                result_to_cache,
                label=f"{report_config.dateRangeType or 'custom'} {start_date.isoformat()}..{end_date.isoformat()}",
            )
            logger.info(
                "menu cache WRITE key=%s range=%s..%s",
                _menu_cache_key, start_date.isoformat(), end_date.isoformat(),
            )
        except Exception as _exc:
            logger.warning("precompute_cache: write failed: %s", _exc)
        return result_to_cache
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = report_config.restaurantIds
            locations = None
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            elif TOAST_MENU_TAGS_PRIORITY_EXTERNAL_ID or TOAST_MENU_TAGS_PRIORITY_NAME:
                locations = await get_all_active_locations(client)
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")

            # Tip Pool report
            if report_config.reportType == "tip_pool":
                return await build_tip_pool_report(
                    client, report_config, restaurant_ids, start_date, end_date, locations
                )

            # Guest Count report
            if report_config.reportType == "guest_count":
                return await build_guest_count_report(
                    client, report_config, restaurant_ids, start_date, end_date, locations
                )

            # Special handling for menu reports
            # Use Analytics API with groupBy: MENU_ITEM + join to tags
            if report_config.reportType == "menu":
                if locations:
                    restaurant_ids = prioritize_menu_tag_restaurants(restaurant_ids, locations)
                if response is not None:
                    response.headers["X-Menu-Path"] = "menu"
                if report_config.itemTags and len(report_config.itemTags) > 0:
                    logger.info(f"Using Analytics API with MENU_ITEM groupBy for tag filtering: {report_config.itemTags}")
                else:
                    logger.info("Using Analytics API with MENU_ITEM groupBy (no tag filters)")
                
                # Get menu tags mapping - try multiple restaurants to get complete coverage
                # Start with first restaurant, then try a few more if needed
                all_tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
                logger.info(f"Initial menu mapping from first restaurant: {len(all_tags_mapping)} items")
                
                # Get modifier name mapping from cache (for fallback matching)
                modifier_name_to_guid = {}
                if restaurant_ids[0] in _menu_tags_cache and "name_mapping" in _menu_tags_cache[restaurant_ids[0]]:
                    modifier_name_to_guid = _menu_tags_cache[restaurant_ids[0]]["name_mapping"]
                    logger.info(f"Loaded modifier name mapping: {len(modifier_name_to_guid)} modifier names")
                    # Debug: Check if target name is in mapping
                    target_name_key = "half black & bleu (olo pairing choice)"
                    if target_name_key in modifier_name_to_guid:
                        logger.info(f"DEBUG: Target name '{target_name_key}' found in name mapping -> {modifier_name_to_guid[target_name_key]}")
                    else:
                        # Check for similar names
                        similar = [n for n in modifier_name_to_guid.keys() if "half black" in n and "bleu" in n]
                        logger.info(f"DEBUG: Target name not found. Similar names: {similar[:5]}")
                
                # Check all selected restaurants to get complete menu coverage
                # This is especially important for includeZeroSales to find all items with tags
                restaurants_to_check = len(restaurant_ids)
                logger.info(f"Checking {restaurants_to_check} restaurants for complete menu mapping")
                for i in range(1, restaurants_to_check):
                    try:
                        additional_mapping = await get_menu_tags_mapping(client, restaurant_ids[i])
                        # Merge mappings (items from later restaurants will override if same GUID)
                        for item_guid, tags in additional_mapping.items():
                            if item_guid not in all_tags_mapping:
                                all_tags_mapping[item_guid] = tags
                            else:
                                # Merge tags if item already exists (items can have different tags in different restaurants)
                                existing_tags = {t["tagName"] for t in all_tags_mapping[item_guid]}
                                new_tags = [t for t in tags if t["tagName"] not in existing_tags]
                                if new_tags:
                                    all_tags_mapping[item_guid].extend(new_tags)
                        logger.info(f"After checking restaurant {i+1}/{restaurants_to_check}: {len(all_tags_mapping)} total items in mapping")
                        
                        # Also merge name mappings and guid_to_name mappings
                        if restaurant_ids[i] in _menu_tags_cache:
                            if "name_mapping" in _menu_tags_cache[restaurant_ids[i]]:
                                additional_name_mapping = _menu_tags_cache[restaurant_ids[i]]["name_mapping"]
                                modifier_name_to_guid.update(additional_name_mapping)
                            if "guid_to_name" in _menu_tags_cache[restaurant_ids[i]]:
                                additional_guid_to_name = _menu_tags_cache[restaurant_ids[i]]["guid_to_name"]
                                # We'll merge this into item_names_mapping later when it's initialized
                                pass
                    except Exception as e:
                        logger.warning(f"Failed to get menu mapping from restaurant {restaurant_ids[i]}: {e}")
                        continue
                
                # Filter mapping to only selected tags (or include all if no tags selected)
                filtered_mapping = {}
                if report_config.itemTags and len(report_config.itemTags) > 0:
                    # Use normalized tag comparison so "* Consolidated Pizza" and "Consolidated Pizza" match
                    selected_tags_lower = [t.lower() for t in report_config.itemTags]
                    selected_tags_norm = [_normalize_tag_name(t) for t in report_config.itemTags]
                    for item_guid, tags in all_tags_mapping.items():
                        matching_tags = [t for t in tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                        if matching_tags:
                            filtered_mapping[item_guid] = matching_tags
                else:
                    filtered_mapping = dict(all_tags_mapping)
                
                logger.info(f"Filtered to {len(filtered_mapping)} items with selected tags: {report_config.itemTags}")
                logger.info(f"Total items in all_tags_mapping: {len(all_tags_mapping)}")
                if report_config.includeZeroSales:
                    logger.info(f"IncludeZeroSales is TRUE - will show all {len(filtered_mapping)} items with selected tags, even if they have 0 sales")

                name_to_tags: Dict[str, List[Dict]] = {}
                if report_config.itemTags:
                    for restaurant_id in restaurant_ids:
                        cache_entry = _menu_tags_cache.get(restaurant_id, {})
                        guid_to_name = cache_entry.get("guid_to_name", {})
                        for guid, tags in all_tags_mapping.items():
                            item_name = guid_to_name.get(guid)
                            if not item_name:
                                continue
                            key = _normalize_item_name(item_name)
                            if not key:
                                continue
                            existing = {t["tagName"] for t in name_to_tags.get(key, [])}
                            merged = name_to_tags.get(key, [])
                            for tag in tags:
                                if tag["tagName"] not in existing:
                                    merged.append(tag)
                            name_to_tags[key] = merged

                consolidated_cat_map = get_consolidated_category_map()
                report_tag_map = get_report_tag_map()
                consolidated_override = get_consolidated_override_map()
                consolidated_fallback_hits = 0

                # Debug specific GUIDs to confirm menu mapping/tag coverage
                debug_guids = {
                    "81c4867f-b73c-41e3-abe8-a89f9e180919": "Pepperoni & Portabella Pizza (Entree)",
                    "ac99345a-f79d-4083-bb01-f56f0e44cf64": "Pepperoni & Mushroom Pizza (Full Pairing Entree)"
                }
                for guid, label in debug_guids.items():
                    in_mapping = guid in all_tags_mapping
                    in_filtered = guid in filtered_mapping
                    tags = [t.get("tagName") for t in all_tags_mapping.get(guid, [])] if in_mapping else []
                    consolidated_cat = consolidated_cat_map.get(_normalize_item_name(label))
                    logger.info(
                        f"Tag compare debug for {label}: "
                        f"selected_raw={report_config.itemTags}, "
                        f"selected_norm={[_normalize_tag_name(t) for t in report_config.itemTags]}, "
                        f"consolidated_cat={consolidated_cat}, "
                        f"consolidated_norm={_normalize_tag_name(consolidated_cat) if consolidated_cat else ''}"
                    )
                    logger.info(
                        f"Tag debug for {label}: guid={guid} in_all_tags_mapping={in_mapping}, "
                        f"in_filtered_mapping={in_filtered}, tags={tags}"
                    )
                
                # Log sample of items that were filtered out (for debugging)
                if len(filtered_mapping) < len(all_tags_mapping):
                    sample_excluded = []
                    for item_guid, tags in all_tags_mapping.items():
                        if item_guid not in filtered_mapping:
                            tag_names = [t["tagName"] for t in tags]
                            if tag_names:  # Only log items that have tags
                                sample_excluded.append((item_guid, tag_names))
                                if len(sample_excluded) >= 5:
                                    break
                    if sample_excluded:
                        logger.info(f"Sample excluded items (have tags but not selected ones): {sample_excluded[:3]}")
                
                # For includeZeroSales, we need to ensure filtered_mapping contains ALL items with selected tags
                # even if they don't appear in analytics. filtered_mapping already has this, so we're good.
                
                if len(filtered_mapping) == 0 and report_config.itemTags:
                    return {"pivotTable": [], "totals": []}
                
                # Split date range into week-sized chunks if needed (groupBy only works for day/week)
                days_diff = (end_date - start_date).days + 1
                all_analytics_rows = []

                # ── Local DB short-circuit ──────────────────────────────────────
                # If toast_menu_raw has full coverage for [start_date, end_date],
                # load MENU_ITEM + MODIFIER rows from local SQLite instead of
                # round-tripping to Toast Analytics (the slow part). The data
                # is shaped to match Toast's response, so the rest of the
                # pipeline (dedup, name fixups, pivot, YoY) is unchanged.
                _use_local_menu = False
                try:
                    # Run the SQLite queries off the event loop — a multi-week
                    # query can return ~1M rows and the row-building takes real
                    # CPU. Without to_thread, this blocks every other request
                    # (page loads, Item Tags, etc.) for the duration.
                    _has_coverage = await asyncio.to_thread(
                        toast_menu_raw_db.has_full_coverage,
                        start_date.isoformat(), end_date.isoformat(),
                    )
                    if _has_coverage:
                        _local_mi_rows = await asyncio.to_thread(
                            toast_menu_raw_db.query_rows_for_report,
                            start_date.isoformat(), end_date.isoformat(),
                            restaurant_ids, "MENU_ITEM",
                        )
                        _local_mod_rows = await asyncio.to_thread(
                            toast_menu_raw_db.query_rows_for_report,
                            start_date.isoformat(), end_date.isoformat(),
                            restaurant_ids, "MODIFIER",
                        )
                        all_analytics_rows = list(_local_mi_rows)
                        menu_item_count = len(all_analytics_rows)
                        # Stash modifier rows for the next block to pick up.
                        _local_modifier_rows_to_merge = _local_mod_rows
                        _use_local_menu = True
                        logger.info(
                            "menu report: using local DB (%d menu items + %d modifiers) — skipping Toast Analytics",
                            len(_local_mi_rows), len(_local_mod_rows),
                        )
                        if response is not None:
                            response.headers["X-Menu-Source"] = "local-db"
                except Exception as _exc:
                    logger.warning("local DB short-circuit failed, falling back to Toast: %s", _exc)
                    _use_local_menu = False
                    all_analytics_rows = []

                if _use_local_menu:
                    pass  # all_analytics_rows + menu_item_count already populated
                elif days_diff > 7:
                    # Split into week-sized chunks
                    logger.info(f"Splitting {days_diff} day range into week-sized chunks for groupBy support")
                    current_start = start_date
                    while current_start <= end_date:
                        # Calculate chunk end (max 7 days)
                        chunk_end = min(current_start + timedelta(days=6), end_date)

                        logger.info(f"Requesting chunk: {current_start} to {chunk_end}")
                        report_guid = await request_toast_report(
                            client,
                            current_start,
                            chunk_end,
                            restaurant_ids,
                            "menu",
                            ["MENU_ITEM"]
                        )

                        logger.info(f"Fetching report for chunk {current_start} to {chunk_end} (guid: {report_guid})")
                        chunk_rows = await fetch_toast_report(client, report_guid, "menu")
                        if chunk_rows:
                            all_analytics_rows.extend(chunk_rows)

                        # Move to next chunk
                        current_start = chunk_end + timedelta(days=1)
                else:
                    # Single request for ranges <= 7 days
                    report_guid = await request_toast_report(
                        client,
                        start_date,
                        end_date,
                        restaurant_ids,
                        "menu",
                        ["MENU_ITEM"]
                    )

                    all_analytics_rows = await fetch_toast_report(client, report_guid, "menu")

                # Track count before adding modifiers (skip if we already set it from local DB)
                if not _use_local_menu:
                    menu_item_count = len(all_analytics_rows)
                
                # Also request modifier-level data
                logger.info("Requesting Analytics menu report with MODIFIER groupBy...")
            restaurant_batch_size = 25

            async def fetch_modifier_rows_for_chunk(chunk_start: date, chunk_end: date) -> List[Dict]:
                try:
                    modifier_report_guid = await request_toast_report(
                        client,
                        chunk_start,
                        chunk_end,
                        restaurant_ids,
                        "menu",
                        ["MODIFIER"]  # Group by modifier instead
                    )
                    logger.info(f"Fetching MODIFIER report for chunk {chunk_start} to {chunk_end} (guid: {modifier_report_guid})")
                    return await fetch_toast_report(client, modifier_report_guid, "menu")
                except HTTPException as e:
                    if e.status_code in [502, 503, 504]:
                        batches = chunk_list(restaurant_ids, restaurant_batch_size)
                        logger.warning(
                            f"MODIFIER report failed with {e.status_code} for {chunk_start} to {chunk_end}; "
                            f"retrying in {len(batches)} restaurant batches of {restaurant_batch_size}."
                        )
                        all_rows: List[Dict] = []
                        for i, batch in enumerate(batches):
                            logger.info(
                                f"Requesting MODIFIER chunk batch {i + 1}/{len(batches)}: "
                                f"{chunk_start} to {chunk_end} ({len(batch)} restaurants)"
                            )
                            batch_guid = await request_toast_report(
                                client,
                                chunk_start,
                                chunk_end,
                                batch,
                                "menu",
                                ["MODIFIER"]
                            )
                            batch_rows = await fetch_toast_report(client, batch_guid, "menu")
                            if batch_rows:
                                all_rows.extend(batch_rows)
                        return all_rows
                    raise
                
                if _use_local_menu:
                    # Local-DB short-circuit: modifier rows already loaded above
                    # in _local_modifier_rows_to_merge. They've already got
                    # menuItemGuid set (no rename needed) but we still dedup
                    # against the menu-item section to match the live path's
                    # behavior.
                    menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                    deduplicated_local_mods = [
                        r for r in _local_modifier_rows_to_merge
                        if r.get("menuItemGuid") not in menu_item_guids_set
                    ]
                    if len(deduplicated_local_mods) < len(_local_modifier_rows_to_merge):
                        logger.info(
                            "Removed %d duplicate modifiers from local DB merge",
                            len(_local_modifier_rows_to_merge) - len(deduplicated_local_mods),
                        )
                    all_analytics_rows.extend(deduplicated_local_mods)
                elif days_diff > 7:
                    # Split into week-sized chunks for modifiers too
                    logger.info(f"Splitting modifier data into week-sized chunks")
                    current_start = start_date
                    while current_start <= end_date:
                        chunk_end = min(current_start + timedelta(days=6), end_date)

                        logger.info(f"Requesting MODIFIER chunk: {current_start} to {chunk_end}")
                        modifier_chunk_rows = await fetch_modifier_rows_for_chunk(current_start, chunk_end)
                        if modifier_chunk_rows:
                            # Log sample row to see what fields are returned
                            if len(modifier_chunk_rows) > 0:
                                logger.info(f"Sample MODIFIER row keys: {list(modifier_chunk_rows[0].keys())}")

                            # Rename the field from modifierGuid to menuItemGuid for consistency
                            for row in modifier_chunk_rows:
                                # Handle different possible field names
                                if "modifierGuid" in row:
                                    row["menuItemGuid"] = row.pop("modifierGuid")
                                elif "modifierOptionGuid" in row:
                                    row["menuItemGuid"] = row.pop("modifierOptionGuid")

                                if "modifierName" in row:
                                    row["menuItemName"] = row.pop("modifierName")
                                elif "modifierOptionName" in row:
                                    row["menuItemName"] = row.pop("modifierOptionName")

                            # Before adding, check for duplicates with menu items
                            menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                            deduplicated_chunk = [
                                r for r in modifier_chunk_rows
                                if r.get("menuItemGuid") not in menu_item_guids_set
                            ]
                            if len(deduplicated_chunk) < len(modifier_chunk_rows):
                                logger.info(f"Removed {len(modifier_chunk_rows) - len(deduplicated_chunk)} duplicate modifiers from chunk {current_start} to {chunk_end}")
                            all_analytics_rows.extend(deduplicated_chunk)

                        current_start = chunk_end + timedelta(days=1)
                else:
                    # Single request for ranges <= 7 days
                    modifier_rows = await fetch_modifier_rows_for_chunk(start_date, end_date)
                    if modifier_rows:
                        # Log sample row to see what fields are returned
                        if len(modifier_rows) > 0:
                            logger.info(f"Sample MODIFIER row keys: {list(modifier_rows[0].keys())}")
                        
                        # Rename fields for consistency
                        for row in modifier_rows:
                            # Handle different possible field names
                            if "modifierGuid" in row:
                                row["menuItemGuid"] = row.pop("modifierGuid")
                            elif "modifierOptionGuid" in row:
                                row["menuItemGuid"] = row.pop("modifierOptionGuid")
                            
                            if "modifierName" in row:
                                row["menuItemName"] = row.pop("modifierName")
                            elif "modifierOptionName" in row:
                                row["menuItemName"] = row.pop("modifierOptionName")
                        
                        # Before adding, check for duplicates with menu items
                        menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                        deduplicated_modifier_rows = [
                            r for r in modifier_rows 
                            if r.get("menuItemGuid") not in menu_item_guids_set
                        ]
                        if len(deduplicated_modifier_rows) < len(modifier_rows):
                            logger.info(f"Removed {len(modifier_rows) - len(deduplicated_modifier_rows)} duplicate modifiers from single request")
                        all_analytics_rows.extend(deduplicated_modifier_rows)
                
                logger.info(f"Total rows after adding modifiers: {len(all_analytics_rows)} (was {menu_item_count} menu items, added {len(all_analytics_rows) - menu_item_count} modifiers)")
                
                # CRITICAL: Deduplicate across ALL chunks to prevent cross-chunk duplicates
                # When splitting date ranges, the same sale might appear in multiple chunks
                logger.info(f"Total rows before cross-chunk deduplication: {len(all_analytics_rows)}")
                
                # Store the original menu item GUIDs before deduplication
                original_menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                
                # Deduplicate across ALL rows using (restaurantGuid, businessDate, menuItemGuid, quantitySold) as key
                seen = set()
                deduplicated_rows = []
                for row in all_analytics_rows:
                    key = (
                        row.get("restaurantGuid"),
                        row.get("businessDate"),
                        row.get("menuItemGuid"),
                        row.get("quantitySold", 0)
                    )
                    if key not in seen:
                        seen.add(key)
                        deduplicated_rows.append(row)
                
                duplicates_removed = len(all_analytics_rows) - len(deduplicated_rows)
                if duplicates_removed > 0:
                    logger.warning(f"Removed {duplicates_removed} cross-chunk duplicates")
                all_analytics_rows = deduplicated_rows
                logger.info(f"Total rows after cross-chunk deduplication: {len(all_analytics_rows)}")
                
                # After deduplication, recalculate menu_item_count
                # Count how many deduplicated rows are menu items (those with GUIDs from original menu items)
                menu_item_count = len([r for r in all_analytics_rows if r.get("menuItemGuid") in original_menu_item_guids])
                
                # DEBUG: Check for duplicate GUIDs across menu items and modifiers
                menu_item_guids_section = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                modifier_guids_section = {r.get("menuItemGuid") for r in all_analytics_rows[menu_item_count:] if r.get("menuItemGuid")}
                duplicate_guids = menu_item_guids_section.intersection(modifier_guids_section)
                
                if duplicate_guids:
                    logger.warning(f"FOUND DUPLICATE GUIDs in both menu items and modifiers: {len(duplicate_guids)} duplicates")
                    logger.warning(f"Sample duplicates: {list(duplicate_guids)[:10]}")
                    
                    # Check if target modifier is duplicated
                    if "6d02e1aa-41c4-489e-bd72-90d12d79edf2" in duplicate_guids:
                        logger.warning(f"TARGET MODIFIER IS DUPLICATED!")
                        # Count total qty from both sections
                        menu_section_qty = sum(r.get("quantitySold", 0) for r in all_analytics_rows[:menu_item_count] 
                                              if r.get("menuItemGuid") == "6d02e1aa-41c4-489e-bd72-90d12d79edf2")
                        mod_section_qty = sum(r.get("quantitySold", 0) for r in all_analytics_rows[menu_item_count:] 
                                             if r.get("menuItemGuid") == "6d02e1aa-41c4-489e-bd72-90d12d79edf2")
                        logger.warning(f"  In menu items section: {menu_section_qty}")
                        logger.warning(f"  In modifiers section: {mod_section_qty}")
                        logger.warning(f"  Combined (DOUBLE COUNT): {menu_section_qty + mod_section_qty}")
                    
                    # Deduplicate: remove modifier rows whose GUID already exists in menu items
                    menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                    deduplicated_modifier_rows = [
                        r for r in all_analytics_rows[menu_item_count:] 
                        if r.get("menuItemGuid") not in menu_item_guids_set
                    ]
                    
                    # Replace the modifier section with deduplicated version
                    all_analytics_rows = all_analytics_rows[:menu_item_count] + deduplicated_modifier_rows
                    logger.warning(f"After deduplication: {len(all_analytics_rows)} total rows (removed {len(all_analytics_rows[menu_item_count:]) - len(deduplicated_modifier_rows)} duplicate modifier rows)")
                else:
                    logger.info("No duplicate GUIDs found between menu items and modifiers ✓")
                
                # Log sample of modifier rows to verify they're there
                # Modifiers should be in the rows after menu_item_count
                if len(all_analytics_rows) > menu_item_count:
                    modifier_rows_sample = all_analytics_rows[menu_item_count:menu_item_count+5]  # First 5 modifier rows
                    logger.info(f"Sample modifier rows found: {len(all_analytics_rows) - menu_item_count} total modifiers")
                    for i, mod_row in enumerate(modifier_rows_sample):
                        logger.info(f"  Modifier {i+1}: guid={mod_row.get('menuItemGuid')}, name='{mod_row.get('menuItemName')}', qty={mod_row.get('quantitySold', 0)}, restaurant={mod_row.get('restaurantName', 'Unknown')}, date={mod_row.get('businessDate', 'N/A')}")
                        # Check if it's in tags mapping
                        mod_guid = mod_row.get('menuItemGuid')
                        if mod_guid:
                            in_mapping = mod_guid in all_tags_mapping
                            logger.info(f"    -> In tags_mapping: {in_mapping}")
                            if in_mapping:
                                tags = all_tags_mapping[mod_guid]
                                logger.info(f"    -> Tags: {[t['tagName'] for t in tags]}")
                    
                    # Check for specific modifier and sum its quantities across all rows
                    target_modifier_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
                    target_modifier_name = "Half Black & Bleu (OLO Pairing Choice)"
                    total_qty_for_target = 0
                    rows_for_target = []
                    for mod_row in all_analytics_rows[menu_item_count:]:
                        mod_guid = mod_row.get('menuItemGuid')
                        mod_name = mod_row.get('menuItemName', '')
                        if mod_guid == target_modifier_guid or target_modifier_name.lower() in mod_name.lower():
                            qty = mod_row.get('quantitySold', 0)
                            total_qty_for_target += qty
                            rows_for_target.append({
                                'restaurant': mod_row.get('restaurantName', 'Unknown'),
                                'date': mod_row.get('businessDate', 'N/A'),
                                'qty': qty
                            })
                    if rows_for_target:
                        logger.info(f"TARGET MODIFIER '{target_modifier_name}' (guid: {target_modifier_guid}):")
                        logger.info(f"  Total quantity across all rows (raw API data): {total_qty_for_target}")
                        logger.info(f"  Number of rows: {len(rows_for_target)}")
                        logger.info(f"  Sample rows (first 10): {rows_for_target[:10]}")
                        # Check tags for this modifier
                        if target_modifier_guid in all_tags_mapping:
                            mod_tags = all_tags_mapping[target_modifier_guid]
                            logger.info(f"  Modifier tags: {[t['tagName'] for t in mod_tags]}")
                            if report_config.itemTags:
                                matching_tags = [t for t in mod_tags if _normalize_tag_name(t.get('tagName', '')) in selected_tags_norm]
                                logger.info(f"  Selected tags: {report_config.itemTags}")
                                logger.info(f"  Matching tags: {[t['tagName'] for t in matching_tags]}")
                                logger.info(f"  Will be included in breakdown: {len(matching_tags) > 0}")
                    
                    # Also search for the specific modifier we're looking for
                    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
                    target_name = "Half Black & Bleu (OLO Pairing Choice)"
                    for mod_row in all_analytics_rows[menu_item_count:]:
                        mod_guid = mod_row.get('menuItemGuid')
                        mod_name = mod_row.get('menuItemName', '')
                        if mod_guid == target_guid or target_name.lower() in mod_name.lower():
                            logger.info(f"FOUND TARGET MODIFIER: guid={mod_guid}, name='{mod_name}', qty={mod_row.get('quantitySold', 0)}")
                            # Check name mapping
                            name_key = mod_name.lower().strip()
                            in_name_mapping = name_key in modifier_name_to_guid if modifier_name_to_guid else False
                            logger.info(f"  -> In name_mapping: {in_name_mapping}")
                            logger.info(f"  -> Looking for normalized name: '{name_key}'")
                            
                            # Show similar names in mapping
                            similar_names = []
                            for map_name in modifier_name_to_guid.keys():
                                if "black" in map_name and "bleu" in map_name:
                                    similar_names.append(map_name)
                                elif "half" in map_name and ("black" in map_name or "bleu" in map_name):
                                    similar_names.append(map_name)
                            
                            if similar_names:
                                logger.info(f"  -> Similar names in mapping (first 10): {similar_names[:10]}")
                            
                            if in_name_mapping:
                                mapped_guid = modifier_name_to_guid[name_key]
                                logger.info(f"  -> Mapped GUID: {mapped_guid}")
                                if mapped_guid in all_tags_mapping:
                                    tags = all_tags_mapping[mapped_guid]
                                    logger.info(f"  -> Tags for mapped GUID: {[t['tagName'] for t in tags]}")
                            break
                else:
                    logger.warning(f"No modifier rows found - expected {menu_item_count} menu items, got {len(all_analytics_rows)} total rows")
                
                if not all_analytics_rows:
                    return {"pivotTable": [], "totals": [], "itemBreakdown": []}
                
                # Filter analytics rows - include items that are in filtered_mapping OR have tags in all_tags_mapping
                # This handles cases where items might have different GUIDs in menu vs analytics
                # For modifiers, also try name-based matching as fallback
                filtered_rows = []
                modifier_count_in_filtered = 0
                menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                debug_mapping_fallback_names = [] if getattr(report_config, "debug", False) else None

                for row in all_analytics_rows:
                    menu_item_guid = row.get("menuItemGuid", "")
                    menu_item_name = row.get("menuItemName", "")
                    if not menu_item_guid:
                        continue
                    
                    is_modifier = menu_item_guid not in menu_item_guids
                    matched_guid = menu_item_guid
                    name_key = _normalize_item_name(menu_item_name)
                    
                    # 1) Already in filtered set from Toast tags
                    if menu_item_guid in filtered_mapping:
                        filtered_rows.append(row)
                        if is_modifier:
                            modifier_count_in_filtered += 1
                    # 2) Mapping table first: assign tag from mapping so items without the tag in Toast still count
                    elif report_config.itemTags:
                        report_tags = _get_tag_from_mapping(name_key, report_tag_map, consolidated_cat_map, consolidated_override, menu_item_guid)
                        matching_report_tags = [t for t in report_tags if _normalize_tag_name(t) in selected_tags_norm]
                        if matching_report_tags:
                            filtered_mapping[menu_item_guid] = [{"tagName": t} for t in matching_report_tags]
                            filtered_rows.append(row)
                            if is_modifier:
                                modifier_count_in_filtered += 1
                            consolidated_fallback_hits += 1
                            if debug_mapping_fallback_names is not None:
                                debug_mapping_fallback_names.append(menu_item_name)
                    # 3) Toast tags: item in Menus API with matching tag (normalized so "* Consolidated Pizza" matches "Consolidated Pizza")
                    elif menu_item_guid in all_tags_mapping:
                        item_tags = all_tags_mapping[menu_item_guid]
                        matching_tags = [t for t in item_tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                        if matching_tags:
                            filtered_mapping[menu_item_guid] = matching_tags
                            filtered_rows.append(row)
                            if is_modifier:
                                modifier_count_in_filtered += 1
                    # 4) Name-to-tags from Toast (same name, different GUID); use normalized tag match
                    elif not is_modifier and report_config.itemTags and name_key and name_key in name_to_tags:
                        matching_tags = [t for t in name_to_tags[name_key] if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                        if matching_tags:
                            filtered_mapping[menu_item_guid] = matching_tags
                            filtered_rows.append(row)
                    elif is_modifier:
                        # No mapping-table match; skip
                        pass
                        # Try to find modifier by name
                        name_key = menu_item_name.lower().strip()
                        
                        # Normalize variations: "&" -> "and", remove extra spaces
                        name_key_normalized = name_key.replace("&", "and").replace("  ", " ").strip()
                        
                        # Debug logging for the specific modifier we're looking for
                        if "half black" in name_key.lower() or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                            logger.info(f"DEBUG: Looking for modifier - guid={menu_item_guid}, name='{menu_item_name}', normalized='{name_key}', normalized_v2='{name_key_normalized}'")
                            logger.info(f"DEBUG: Name in mapping? {name_key in modifier_name_to_guid}")
                            logger.info(f"DEBUG: Normalized name in mapping? {name_key_normalized in modifier_name_to_guid}")
                            if name_key in modifier_name_to_guid:
                                logger.info(f"DEBUG: Found GUID: {modifier_name_to_guid[name_key]}")
                            elif name_key_normalized in modifier_name_to_guid:
                                logger.info(f"DEBUG: Found GUID with normalized name: {modifier_name_to_guid[name_key_normalized]}")
                            else:
                                # Try to find similar names
                                similar = [n for n in modifier_name_to_guid.keys() if "half black" in n or ("bleu" in n and "black" in n)]
                                logger.info(f"DEBUG: Similar names in mapping: {similar[:10]}")

                        # Try exact match first
                        matched_guid = None
                        if name_key in modifier_name_to_guid:
                            matched_guid = modifier_name_to_guid[name_key]
                        # Try normalized version (with & -> and)
                        elif name_key_normalized in modifier_name_to_guid:
                            matched_guid = modifier_name_to_guid[name_key_normalized]

                        if matched_guid:
                            matched_guid = modifier_name_to_guid[name_key]
                            if matched_guid in all_tags_mapping:
                                item_tags = all_tags_mapping[matched_guid]
                                matching_tags = [t for t in item_tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                                if matching_tags:
                                    # Update the row's GUID to the matched one and add to filtered_mapping
                                    row["menuItemGuid"] = matched_guid
                                    filtered_mapping[matched_guid] = matching_tags
                                    filtered_rows.append(row)
                                    modifier_count_in_filtered += 1
                                    logger.info(f"Matched modifier by name: '{menu_item_name}' (guid: {menu_item_guid}) -> {matched_guid}")
                                else:
                                    if "half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                                        logger.warning(f"DEBUG: Modifier matched by name but tags don't match selected tags. Tags: {[t['tagName'] for t in item_tags]}, Selected: {report_config.itemTags}")
                            else:
                                if "half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                                    logger.warning(f"DEBUG: Modifier matched by name but GUID {matched_guid} not in all_tags_mapping")
                        else:
                            # Try fuzzy matching - check if any mapping name contains key parts
                            # Extract key words from the name
                            key_words = set()
                            for word in name_key.split():
                                # Remove common words and punctuation
                                clean_word = word.strip("()&,").lower()
                                if len(clean_word) > 2 and clean_word not in ["the", "and", "or", "for", "with"]:
                                    key_words.add(clean_word)

                            # Try to find a match with at least 3 key words in common
                            best_match = None
                            best_score = 0
                            for map_name, map_guid in modifier_name_to_guid.items():
                                map_words = set(map_name.split())
                                common_words = key_words.intersection(map_words)
                                if len(common_words) >= 3:  # At least 3 words in common
                                    score = len(common_words)
                                    if score > best_score:
                                        best_score = score
                                        best_match = (map_name, map_guid)

                            if best_match and ("half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid):
                                map_name, map_guid = best_match
                                logger.info(f"DEBUG: Found potential fuzzy match: '{map_name}' -> {map_guid} (score: {best_score})")

                            if best_match:
                                map_name, map_guid = best_match
                                if map_guid in all_tags_mapping:
                                    item_tags = all_tags_mapping[map_guid]
                                    matching_tags = [t for t in item_tags if _normalize_tag_name(t["tagName"]) in selected_tags_norm]
                                    if matching_tags:
                                        row["menuItemGuid"] = map_guid
                                        filtered_mapping[map_guid] = matching_tags
                                        filtered_rows.append(row)
                                        modifier_count_in_filtered += 1
                                        logger.info(f"Matched modifier by fuzzy name: '{menu_item_name}' -> '{map_name}' (guid: {map_guid})")
                
                if consolidated_fallback_hits:
                    logger.info(f"ConsolidatedCat fallback hits (menu): {consolidated_fallback_hits}")

                logger.info(f"Filtered analytics rows: {len(filtered_rows)} from {len(all_analytics_rows)} total (includes {modifier_count_in_filtered} modifiers)")

                # Build debug info for tag/mapping troubleshooting (when report_config.debug is True)
                debug_info_extra = None
                if getattr(report_config, "debug", False) and report_config.itemTags:
                    _nk = _normalize_item_name
                    _tk = _normalize_tag_name
                    # Keys in mapping table that might relate to pepperoni/mushroom/portabella
                    mapping_keys_pepperoni = sorted(
                        k for k in list(report_tag_map.keys()) + list(consolidated_cat_map.keys())
                        if "pepperoni" in k or "mushroom" in k or "portabella" in k
                    )[:50]
                    # Unique names from analytics that contain those words (what Toast sent)
                    analytics_names_pepperoni = sorted(
                        set(r.get("menuItemName", "") for r in all_analytics_rows if r.get("menuItemName"))
                    )
                    analytics_names_pepperoni = [n for n in analytics_names_pepperoni if "pepperoni" in n.lower() or "mushroom" in n.lower() or "portabella" in n.lower()][:30]
                    # Target items: did we see them and did they get in?
                    target_names = [
                        "Pepperoni & Mushroom Pizza (Full Pairing Entree)",
                        "Pepperoni & Portabella Pizza (Entree)",
                    ]
                    target_details = []
                    for tn in target_names:
                        nkey = _nk(tn)
                        tags_from_map = _get_tag_from_mapping(nkey, report_tag_map, consolidated_cat_map, consolidated_override, "")
                        in_analytics = any(_nk(r.get("menuItemName", "")) == nkey or tn in (r.get("menuItemName") or "") for r in all_analytics_rows)
                        in_filtered = any(_nk(r.get("menuItemName", "")) == nkey or tn in (r.get("menuItemName") or "") for r in filtered_rows)
                        target_details.append({
                            "name": tn,
                            "normalizedKey": nkey,
                            "tagsFromMappingTable": tags_from_map,
                            "tagsNormalized": [_tk(t) for t in tags_from_map],
                            "selectedTagsNormalized": selected_tags_norm[:5],
                            "inAnalyticsRows": in_analytics,
                            "inFilteredRows": in_filtered,
                            "inReportTagMap": nkey in report_tag_map,
                            "inConsolidatedCatMap": nkey in consolidated_cat_map,
                        })
                    debug_info_extra = {
                        "consolidatedFallbackHits": consolidated_fallback_hits,
                        "mappingFallbackItemNames": debug_mapping_fallback_names or [],
                        "mappingTableKeysContainingPepperoniMushroomPortabella": mapping_keys_pepperoni,
                        "analyticsNamesContainingPepperoniMushroomPortabella": analytics_names_pepperoni,
                        "targetItems": target_details,
                        "totalAnalyticsRows": len(all_analytics_rows),
                        "totalFilteredRows": len(filtered_rows),
                        "menuItemCount": menu_item_count,
                    }
                
                # Log sample of filtered modifier rows
                # Get menu item GUIDs from first part (menu items)
                menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                filtered_modifier_rows = [r for r in filtered_rows if r.get("menuItemGuid") and r.get("menuItemGuid") not in menu_item_guids]
                if filtered_modifier_rows:
                    logger.info(f"Filtered modifier rows: {len(filtered_modifier_rows)} modifiers will be included in pivot")
                    for i, mod_row in enumerate(filtered_modifier_rows[:5]):  # Log first 5
                        logger.info(f"  Filtered Modifier {i+1}: guid={mod_row.get('menuItemGuid')}, name='{mod_row.get('menuItemName')}', qty={mod_row.get('quantitySold', 0)}")
                else:
                    logger.warning(f"No modifiers in filtered_rows (out of {len(all_analytics_rows) - menu_item_count} total modifiers) - modifiers may not have matching tags")
                
                # Debug: Check if the specific GUIDs are in the data
                missing_guids = ["6d02e1aa-41c4-489e-bd72-90d12d79edf2", "99746810-e792-4e7a-b99f-0e30b470a2bd"]
                for guid in missing_guids:
                    in_mapping = guid in all_tags_mapping
                    in_analytics = any(row.get("menuItemGuid") == guid for row in all_analytics_rows)
                    in_filtered = guid in filtered_mapping
                    in_filtered_rows = any(row.get("menuItemGuid") == guid for row in filtered_rows)
                    logger.info(f"Item GUID {guid}: in_menu_mapping={in_mapping}, in_analytics={in_analytics}, in_filtered={in_filtered}, in_filtered_rows={in_filtered_rows}")
                    if in_analytics:
                        # Find the analytics row to see item name
                        analytics_row = next((row for row in all_analytics_rows if row.get("menuItemGuid") == guid), None)
                        if analytics_row:
                            item_name = analytics_row.get("menuItemName", "Unknown")
                            qty = analytics_row.get("quantitySold", 0)
                            logger.info(f"  Analytics data: name='{item_name}', qty={qty}")
                        # Check what tags it has
                        if in_mapping:
                            tags = all_tags_mapping[guid]
                            logger.info(f"  Tags for {guid}: {[t['tagName'] for t in tags]}")
                        else:
                            logger.warning(f"  GUID {guid} found in analytics but not in menu mapping from first restaurant!")
                            logger.warning(f"  This item will be excluded because we can't determine its tags.")
                
                # Build item names mapping from analytics data and menu mapping cache
                item_names_mapping = {}
                # First, get names from analytics data
                for row in all_analytics_rows:
                    item_guid = row.get("menuItemGuid")
                    item_name = row.get("menuItemName")
                    if item_guid and item_name:
                        item_names_mapping[item_guid] = item_name
                
                # Then, get names from menu mapping cache for items not in analytics (for zero sales items)
                # Check ALL selected restaurants to get complete name coverage
                for restaurant_id in restaurant_ids:
                    if restaurant_id in _menu_tags_cache:
                        # Use guid_to_name mapping if available (GUID -> name)
                        if "guid_to_name" in _menu_tags_cache[restaurant_id]:
                            guid_to_name = _menu_tags_cache[restaurant_id]["guid_to_name"]
                            for item_guid, item_name in guid_to_name.items():
                                if item_guid not in item_names_mapping:
                                    item_names_mapping[item_guid] = item_name
                
                # If we still don't have names for some items (for zero sales), fetch from menu API
                if report_config.includeZeroSales:
                    missing_guids = set()
                    for item_guid in all_tags_mapping.keys():
                        if item_guid not in item_names_mapping:
                            missing_guids.add(item_guid)
                    
                    if missing_guids:
                        logger.info(f"Fetching names for {len(missing_guids)} items with 0 sales")
                        # Fetch menu data from first restaurant to get item names
                        try:
                            standard_headers = await get_standard_api_auth_headers()
                            headers_with_guid = {
                                **standard_headers,
                                "Toast-Restaurant-External-ID": restaurant_ids[0]
                            }
                            menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus"
                            menus_resp = await client.get(menus_url, headers=headers_with_guid)
                            if menus_resp.status_code == 200:
                                menus_data = menus_resp.json()
                                # Extract item names from menu data
                                for menu in menus_data.get("menus", []):
                                    for group in menu.get("menuGroups", []):
                                        for item in group.get("menuItems", []):
                                            item_guid = item.get("guid")
                                            # Prefer displayName over name (name might be kitchenName)
                                            item_name = item.get("displayName") or item.get("name", "")
                                            if item_guid in missing_guids and item_name:
                                                item_names_mapping[item_guid] = item_name
                                
                                # Also check modifiers
                                modifier_group_refs = menus_data.get("modifierGroupReferences", {})
                                modifier_option_refs = menus_data.get("modifierOptionReferences", {})
                                for mod_group in modifier_group_refs.values():
                                    for mod_option in mod_group.get("modifierOptions", []):
                                        mod_guid = mod_option.get("guid")
                                        # Prefer displayName over name for modifiers too
                                        mod_name = mod_option.get("displayName") or mod_option.get("name", "")
                                        if mod_guid in missing_guids and mod_name:
                                            item_names_mapping[mod_guid] = mod_name
                                for mod_option in modifier_option_refs.values():
                                    mod_guid = mod_option.get("guid", "")
                                    # Prefer displayName over name for modifiers too
                                    mod_name = mod_option.get("displayName") or mod_option.get("name", "")
                                    if mod_guid in missing_guids and mod_name:
                                        item_names_mapping[mod_guid] = mod_name
                        except Exception as e:
                            logger.warning(f"Failed to fetch menu names for zero-sales items: {e}")

                # Add mapping-only items (in mapping table with selected tag but no analytics row)
                # so they appear in the breakdown and Toast Items popup with count 0.
                if report_config.itemTags and selected_tags_norm:
                    names_in_filtered = {_normalize_item_name(r.get("menuItemName", "")) for r in filtered_rows if r.get("menuItemName")}
                    # report_tag_map values are List[str]; consolidated_cat_map values are str
                    for norm_name, tags in report_tag_map.items():
                        if not norm_name or not tags:
                            continue
                        matching = [t for t in tags if _normalize_tag_name(t) in selected_tags_norm]
                        if not matching:
                            continue
                        if norm_name in names_in_filtered:
                            continue
                        synthetic_guid = "mapping:" + norm_name
                        if synthetic_guid in filtered_mapping:
                            continue
                        filtered_mapping[synthetic_guid] = [{"tagName": t} for t in matching]
                        item_names_mapping[synthetic_guid] = _humanize_item_name(norm_name)
                        names_in_filtered.add(norm_name)
                    for norm_name, cat in consolidated_cat_map.items():
                        if not norm_name or not cat:
                            continue
                        if _normalize_tag_name(cat) not in selected_tags_norm:
                            continue
                        if norm_name in names_in_filtered:
                            continue
                        synthetic_guid = "mapping:" + norm_name
                        if synthetic_guid in filtered_mapping:
                            continue
                        filtered_mapping[synthetic_guid] = [{"tagName": cat}]
                        item_names_mapping[synthetic_guid] = _humanize_item_name(norm_name)
                        names_in_filtered.add(norm_name)
                
                # Transform into pivot table format
                # For includeZeroSales, we need to use filtered_mapping (items with selected tags)
                # but we also need all items from all_tags_mapping that have the selected tags
                # So we'll pass filtered_mapping which already contains all items with selected tags
                # The pivot function will then include items with 0 sales from this mapping
                # Live PLU mapping captured during the same V3 fetch — gives
                # the All Items sheet a "live from Toast" PLU column.
                _live_plu_for_pivot = (_menu_tags_cache.get(restaurant_ids[0], {})
                                       .get("plu_mapping") or {})
                result = pivot_menu_items_by_tag(
                    filtered_rows,
                    filtered_mapping,  # Use filtered_mapping which has all items with selected tags
                    report_config.itemTags,
                    item_names_mapping,
                    menu_item_guids,  # Pass menu item GUIDs to identify modifiers
                    report_config.includeZeroSales,  # Include items with 0 sales
                    _live_plu_for_pivot,
                )
                result["debugInfo"] = {
                    "path": "menu",
                    "itemTags": len(report_config.itemTags or []),
                    "includeZeroSales": bool(report_config.includeZeroSales),
                    "yoyEnabled": bool(report_config.yoyEnabled),
                    "alohaDate": report_config.alohaDate
                }
                if debug_info_extra is not None:
                    result["debugInfo"]["tagMappingDebug"] = debug_info_extra
                result = await apply_yoy_aloha_quantities(result, report_config, start_date, end_date)
                result = await apply_yoy_toast_quantities(result, report_config, start_date, end_date)
                return _cache_write_menu(result)

            # Default: Use ERA reporting API
            report_guid = await request_toast_report(
                client,
                start_date,
                end_date,
                restaurant_ids,
                report_config.reportType,
                report_config.groupBy
            )
            
            raw_rows = await fetch_toast_report(client, report_guid, report_config.reportType)
            
            if not raw_rows:
                return []
            
            # Transform metrics data to pivot table format if needed
            if report_config.reportType == "metrics" and not report_config.groupBy:
                # Fetch non-restaurant service charges for each (restaurant, date) in parallel.
                # These are third-party delivery fees (DoorDash, UberEats, etc.) that flow away
                # from the restaurant — used to compute "Newk's Net Sales" vs Toast gross.
                unique_combos: set = {
                    (row.get("restaurantGuid", ""), row.get("businessDate", ""))
                    for row in raw_rows
                    if row.get("restaurantGuid") and row.get("businessDate")
                }
                sem = asyncio.Semaphore(10)

                sample_guid = next(iter(g for g, _ in unique_combos), "")
                logger.info(
                    "Net sales SC fetch: %d location-date combos, sample GUID=%s",
                    len(unique_combos),
                    sample_guid,
                )

                async def _fetch_sc_for_combo(guid: str, bd: str) -> Tuple[str, float]:
                    async with sem:
                        try:
                            amount = await _compute_restaurant_service_charges(client, guid, bd)
                            return f"{guid}|{bd}", amount
                        except Exception as exc:
                            logger.warning(
                                "SC fetch failed for %s|%s: %s — defaulting to 0",
                                guid, bd, exc,
                                exc_info=True,
                            )
                            return f"{guid}|{bd}", 0.0

                sc_results = await asyncio.gather(
                    *[_fetch_sc_for_combo(g, b) for g, b in unique_combos]
                )
                service_charges: Dict[str, float] = dict(sc_results)
                total_sc = sum(service_charges.values())
                logger.info(
                    "Net sales SC fetch complete: %d combos, total_restaurant_sc=%.2f, "
                    "non_zero_combos=%d",
                    len(service_charges),
                    total_sc,
                    sum(1 for v in service_charges.values() if v > 0),
                )
                pivot_data = pivot_metrics_by_location_and_date(raw_rows, service_charges)
                pivot_data = apply_yoy_aloha_sales(pivot_data, "metrics", raw_rows, report_config, start_date, end_date)
                return pivot_data

            if report_config.reportType == "sales_summary" and not report_config.groupBy:
                # Fetch full order metrics (SC, discounts, voids, payments) per location/date in parallel
                unique_combos_ss: set = {
                    (row.get("restaurantGuid", ""), row.get("businessDate", ""))
                    for row in raw_rows
                    if row.get("restaurantGuid") and row.get("businessDate")
                }
                sem_ss = asyncio.Semaphore(10)

                async def _fetch_metrics_combo(guid: str, bd: str) -> Tuple[str, Dict]:
                    async with sem_ss:
                        try:
                            m = await _compute_sales_metrics_for_day(client, guid, bd)
                            return f"{guid}|{bd}", m
                        except Exception as exc:
                            logger.warning(
                                "Sales metrics fetch failed for %s|%s: %s — defaulting to zeros",
                                guid, bd, exc, exc_info=True,
                            )
                            return f"{guid}|{bd}", {}

                met_results, catering_item_data = await asyncio.gather(
                    asyncio.gather(*[_fetch_metrics_combo(g, b) for g, b in unique_combos_ss]),
                    _fetch_catering_item_data(client, start_date, end_date, restaurant_ids),
                )
                metrics_by_key: Dict[str, Dict] = dict(met_results)
                logger.info(
                    "Sales summary metrics fetch complete: %d combos, total_orders=%d",
                    len(metrics_by_key),
                    sum(m.get("order_count", 0) for m in metrics_by_key.values()),
                )
                summary_result = compute_sales_summary_by_location(raw_rows, metrics_by_key, catering_item_data)
                summary_result = apply_yoy_aloha_sales(summary_result, "sales_summary", raw_rows, report_config, start_date, end_date)
                return summary_result

            # Royalty report — just needs ERA net sales, no Orders API calls
            if report_config.reportType == "royalty":
                return compute_royalty_by_location(raw_rows)

            if report_config.reportType == "menu":
                _menu_fb_result = await build_menu_mix_report_fallback(
                    client,
                    report_config,
                    restaurant_ids,
                    start_date,
                    end_date,
                    response,
                )
                return _cache_write_menu(_menu_fb_result)
            
            if response is not None:
                if report_config.reportType == "menu":
                    response.headers["X-Menu-Path"] = "default_menu"
                else:
                    response.headers["X-Menu-Path"] = "default"
            return raw_rows
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running report: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error running report: {str(e)}")


# ---------------------------------------------------------------------------
# Async job wrapper for /api/reports/run
#
# Large Menu Performance runs (90 stores × 30 days × modifier pivot) routinely
# exceed an edge-layer 300s timeout we have not been able to fully eliminate
# (Cloudways nginx is 900s but something upstream still cuts at 300s; see
# 2026-05-15 incident log). The original endpoint above is left untouched —
# these endpoints offer the start/poll pattern as an alternative for clients
# that need to outrun the edge timeout.
#
#   POST /api/reports/run/start         → returns { job_id, status: "running" }
#   GET  /api/reports/run/status/{id}   → returns { status, progress, result?, error? }
#   POST /api/reports/run/cancel/{id}   → marks the job cancelled
#
# Implementation: the background task makes a single loopback HTTPS-free call
# to 127.0.0.1:8000/api/reports/run with the same body. No nginx in the path,
# no edge timeout possible. Job state lives in the shared pick_export_jobs
# SQLite registry (renamed conceptually to "async_jobs" — it is generic).
# ---------------------------------------------------------------------------

async def _run_report_job(
    job_id: str,
    body: Dict[str, Any],
    forward_headers: Optional[Dict[str, str]] = None,
) -> None:
    """Background task body — runs /api/reports/run via loopback and stores result in SQLite.

    `forward_headers` is an optional dict of headers (e.g. X-Force-Refresh) to
    propagate to the inner /api/reports/run call. The Excel export's all-cats
    fetch uses this so the user-explicit "rebuild for export" path skips the
    menu precompute cache even if a stale entry exists for this exact shape.
    """
    try:
        await asyncio.to_thread(pick_export_jobs.update_progress, job_id, 0, 1)
        # No timeout on the loopback client — the inner endpoint will take as long as it takes.
        async with httpx.AsyncClient(timeout=None) as client:
            # Spawn the loopback call as a task so we can poll for client-initiated cancellation
            # while waiting. The inner endpoint doesn't know about cancellation, but at least the
            # browser stops polling and the job ends in 'cancelled' state.
            inner_task = asyncio.create_task(
                client.post(
                    "http://127.0.0.1:8000/api/reports/run",
                    json=body,
                    headers=forward_headers or None,
                )
            )
            while not inner_task.done():
                if await asyncio.to_thread(pick_export_jobs.is_cancelled, job_id):
                    inner_task.cancel()
                    try:
                        await inner_task
                    except Exception:
                        pass
                    logger.info("Run-report job %s cancelled by client", job_id)
                    return  # status already 'cancelled' in DB
                await asyncio.sleep(2.0)
            r = inner_task.result()
            if r.status_code != 200:
                await asyncio.to_thread(
                    pick_export_jobs.finish_error,
                    job_id,
                    f"upstream {r.status_code}: {r.text[:500]}",
                )
                return
            try:
                result = r.json()
            except Exception as e:
                await asyncio.to_thread(
                    pick_export_jobs.finish_error,
                    job_id,
                    f"invalid JSON from upstream: {e}",
                )
                return
        # Don't overwrite cancelled status with done if user cancelled mid-flight.
        if await asyncio.to_thread(pick_export_jobs.is_cancelled, job_id):
            logger.info("Run-report job %s finished but already cancelled, result discarded", job_id)
            return
        await asyncio.to_thread(pick_export_jobs.finish_done, job_id, result)
    except Exception as e:
        logger.error("Run-report job %s failed: %s", job_id, e, exc_info=True)
        try:
            await asyncio.to_thread(pick_export_jobs.finish_error, job_id, str(e))
        except Exception:
            pass


@app.post("/api/reports/run/start")
async def run_report_start(request: Request, body: Dict[str, Any] = Body(...)):
    """
    Kick off a /api/reports/run call as a background job. Returns immediately
    with {job_id} so the client can poll for completion. The request body is
    forwarded verbatim to /api/reports/run — same shape as the sync endpoint.

    X-Force-Refresh on the incoming request is forwarded to the loopback call
    so the menu precompute cache can be bypassed by clients that need a guaranteed
    fresh recompute (e.g. the Excel export's "Include All Categories" reload).
    """
    job_id = uuid.uuid4().hex
    await asyncio.to_thread(pick_export_jobs.create, job_id)

    forward_headers: Optional[Dict[str, str]] = None
    force_refresh = (request.headers.get("X-Force-Refresh") or "").strip()
    if force_refresh:
        forward_headers = {"X-Force-Refresh": force_refresh}

    asyncio.create_task(_run_report_job(job_id, body, forward_headers))
    logger.info(
        "Run-report job %s started reportType=%s dateRangeType=%s force_refresh=%s",
        job_id, body.get("reportType"), body.get("dateRangeType"),
        bool(forward_headers),
    )
    return {"job_id": job_id, "status": "running"}


@app.get("/api/reports/run/status/{job_id}")
async def run_report_status(job_id: str):
    """Poll a run-report job. Returns full result when status=='done'."""
    await asyncio.to_thread(pick_export_jobs.purge_old, 3600.0)
    job = await asyncio.to_thread(pick_export_jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found (may have expired)")
    return {
        "job_id":   job_id,
        "status":   job["status"],
        "progress": job.get("progress"),
        "result":   job.get("result") if job["status"] == "done" else None,
        "error":    job.get("error"),
    }


@app.post("/api/reports/run/cancel/{job_id}")
async def run_report_cancel(job_id: str):
    """
    Mark a run-report job as cancelled. The background task polls is_cancelled
    every 2s and bails out of the inner loopback fetch when it sees True.
    NOTE: the inner /api/reports/run task continues server-side (the inner
    endpoint doesn't know about cancellation yet) but its response is discarded
    and never written to the job result.
    """
    was_running = await asyncio.to_thread(pick_export_jobs.cancel, job_id)
    job = await asyncio.to_thread(pick_export_jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found (may have expired)")
    logger.info("Run-report job %s cancel requested (was_running=%s, status now=%s)",
                job_id, was_running, job["status"])
    return {"job_id": job_id, "status": job["status"], "was_running": was_running}


# ---------------------------------------------------------------------------
# IPA Sales Export — async job pattern.
#
#   POST /api/exports/ipa-sales/start         → { job_id }
#   GET  /api/exports/ipa-sales/status/{id}   → { status, progress, result?, error? }
#   GET  /api/exports/ipa-sales/download/{id} → streams the CSV
#   POST /api/exports/ipa-sales/cancel/{id}   → marks cancelled
#
# The CSV is written to backend/data/ipa_exports/{job_id}.csv and the path is
# stored in the job result. The download endpoint streams that file.
# ---------------------------------------------------------------------------

IPA_EXPORT_DIR = os.path.join(os.path.dirname(__file__), "data", "ipa_exports")
IPA_EXPORT_RETENTION_SECONDS = 3600.0  # 1 hour — matches pick_export_jobs.purge_old default


def _ipa_purge_old_csvs(retention_seconds: float = IPA_EXPORT_RETENTION_SECONDS) -> int:
    """Best-effort: delete CSV files in IPA_EXPORT_DIR older than retention_seconds."""
    if not os.path.isdir(IPA_EXPORT_DIR):
        return 0
    cutoff = time.time() - retention_seconds
    removed = 0
    try:
        for name in os.listdir(IPA_EXPORT_DIR):
            if not name.endswith(".csv"):
                continue
            path = os.path.join(IPA_EXPORT_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


class IpaExportRequest(BaseModel):
    start_date:  str             # YYYY-MM-DD inclusive
    end_date:    str             # YYYY-MM-DD inclusive
    store_guids: List[str]       # Toast restaurant GUIDs to include


async def _run_ipa_export_job(job_id: str, req: IpaExportRequest) -> None:
    """Background task: build rows, write CSV to disk, mark job done."""
    try:
        start_date = date.fromisoformat(req.start_date)
        end_date   = date.fromisoformat(req.end_date)
    except ValueError as exc:
        await asyncio.to_thread(pick_export_jobs.finish_error, job_id, f"invalid date: {exc}")
        return

    def _progress(done: int, total: int) -> None:
        # Best-effort progress write from inside the row builder.
        try:
            pick_export_jobs.update_progress(job_id, done, total)
        except Exception:
            pass

    def _cancelled() -> bool:
        try:
            return pick_export_jobs.is_cancelled(job_id)
        except Exception:
            return False

    try:
        # toast_guid -> aloha_id  (used as the IPA "Location" column)
        guid_to_location = get_aloha_location_mapping()

        rows = await ipa_export.build_ipa_rows(
            start_date, end_date, req.store_guids,
            request_toast_report = request_toast_report,
            fetch_toast_report   = fetch_toast_report,
            guid_to_location     = guid_to_location,
            progress_cb          = _progress,
            is_cancelled_cb      = _cancelled,
        )

        if _cancelled():
            logger.info("IPA export job %s cancelled before write", job_id)
            return

        os.makedirs(IPA_EXPORT_DIR, exist_ok=True)
        csv_path = os.path.join(IPA_EXPORT_DIR, f"{job_id}.csv")
        with open(csv_path, "wb") as f:
            f.write(ipa_export.rows_to_csv(rows))

        filename = f"ipa_sales_{req.start_date}_{req.end_date}.csv"
        await asyncio.to_thread(pick_export_jobs.finish_done, job_id, {
            "csv_path":  csv_path,
            "filename":  filename,
            "row_count": len(rows),
        })
        logger.info("IPA export job %s done: %d rows -> %s", job_id, len(rows), csv_path)
    except Exception as exc:
        logger.error("IPA export job %s failed: %s", job_id, exc, exc_info=True)
        try:
            await asyncio.to_thread(pick_export_jobs.finish_error, job_id, str(exc))
        except Exception:
            pass


@app.post("/api/exports/ipa-sales/start")
async def ipa_export_start(req: IpaExportRequest):
    """Kick off an IPA sales-export job. Returns {job_id} immediately."""
    if not req.store_guids:
        raise HTTPException(status_code=400, detail="store_guids must not be empty")
    # Reap stale CSVs (>1h old) so disk usage stays bounded.
    await asyncio.to_thread(_ipa_purge_old_csvs)
    job_id = uuid.uuid4().hex
    await asyncio.to_thread(pick_export_jobs.create, job_id)
    asyncio.create_task(_run_ipa_export_job(job_id, req))
    logger.info("IPA export job %s started: %s..%s, %d stores",
                job_id, req.start_date, req.end_date, len(req.store_guids))
    return {"job_id": job_id, "status": "running"}


@app.get("/api/exports/ipa-sales/status/{job_id}")
async def ipa_export_status(job_id: str):
    """Poll an IPA export job. Returns row_count + filename when done."""
    await asyncio.to_thread(pick_export_jobs.purge_old, 3600.0)
    job = await asyncio.to_thread(pick_export_jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found (may have expired)")
    return {
        "job_id":   job_id,
        "status":   job["status"],
        "progress": job.get("progress"),
        "result":   job.get("result") if job["status"] == "done" else None,
        "error":    job.get("error"),
    }


@app.post("/api/exports/ipa-sales/cancel/{job_id}")
async def ipa_export_cancel(job_id: str):
    was_running = await asyncio.to_thread(pick_export_jobs.cancel, job_id)
    job = await asyncio.to_thread(pick_export_jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "status": job["status"], "was_running": was_running}


@app.get("/api/exports/ipa-sales/download/{job_id}")
async def ipa_export_download(job_id: str):
    """Stream the CSV produced by a completed IPA export job."""
    from starlette.responses import FileResponse as _FileResponse
    job = await asyncio.to_thread(pick_export_jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found (may have expired)")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"job not done (status={job['status']})")
    result = job.get("result") or {}
    csv_path = result.get("csv_path")
    filename = result.get("filename") or f"ipa_sales_{job_id}.csv"
    if not csv_path or not os.path.exists(csv_path):
        raise HTTPException(status_code=410, detail="csv file gone (job result expired)")
    return _FileResponse(csv_path, media_type="text/csv", filename=filename)


@app.get("/api/debug/service-charges")
async def debug_service_charges(
    business_date: str = Query(..., description="Business date YYYYMMDD, e.g. 20250301"),
    restaurant_name: str = Query(None, description="Partial location name filter (case-insensitive). Omit to use first location."),
    restaurant_guid: str = Query(None, description="Exact restaurant GUID (alternative to restaurant_name)."),
):
    """
    Debug endpoint: fetches raw Orders API data for one restaurant/date and returns
    the full appliedServiceCharges structure so you can see exactly what fields are present.

    Usage examples:
      /api/debug/service-charges?business_date=20250301&restaurant_name=Jackson
      /api/debug/service-charges?business_date=20250301&restaurant_guid=xxxx-xxxx-...

    Returns sc_samples (full raw objects per SC name) so you can see every field Toast returns.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Resolve the GUID — look up by name if needed
        if not restaurant_guid:
            locations = await get_all_active_locations(client)
            if not locations:
                raise HTTPException(status_code=404, detail="No active locations found")
            if restaurant_name:
                needle = restaurant_name.lower()
                matches = [loc for loc in locations if needle in (loc.restaurantName or "").lower()]
                if not matches:
                    names = [loc.restaurantName for loc in locations[:20]]
                    raise HTTPException(
                        status_code=404,
                        detail=f"No location matching '{restaurant_name}'. Available (first 20): {names}",
                    )
                chosen = matches[0]
            else:
                chosen = locations[0]
            restaurant_guid = chosen.restaurantGuid
            restaurant_name_resolved = chosen.restaurantName
        else:
            restaurant_guid = restaurant_guid.strip()
            restaurant_name_resolved = restaurant_guid  # fallback label

        # Fetch orders via the same path the live SC computation uses
        try:
            orders = await _fetch_orders_for_restaurant_day(client, restaurant_guid, business_date)
        except HTTPException as e:
            return {
                "error": f"Orders API returned {e.status_code}: {e.detail}",
                "restaurant_guid": restaurant_guid,
                "restaurant_name": restaurant_name_resolved,
                "business_date": business_date,
                "hint": "Check that restaurant_guid is a valid Toast UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)",
            }

    total_orders = len(orders)
    total_checks = sum(len(o.get("checks", [])) for o in orders)
    total_applied_scs = 0
    sc_samples: dict = {}   # sc name -> first full appliedServiceCharge object seen
    sc_totals: dict = {}    # sc name -> {count, total_amount, destinations}

    for order in orders:
        if order.get("voided"):
            continue
        for check in order.get("checks", []):
            if check.get("voided"):
                continue
            for applied_sc in check.get("appliedServiceCharges", []):
                total_applied_scs += 1
                name = applied_sc.get("name") or "(unnamed)"
                if name not in sc_samples:
                    sc_samples[name] = applied_sc  # full raw object — shows every field present
                if name not in sc_totals:
                    sc_totals[name] = {"count": 0, "total_amount": 0.0, "destinations": set()}
                sc_totals[name]["count"] += 1
                sc_totals[name]["total_amount"] += float(applied_sc.get("chargeAmount") or 0)
                dest = applied_sc.get("destination") or "MISSING"
                sc_totals[name]["destinations"].add(dest)

    # Convert sets to lists for JSON serialisation
    sc_totals_serial = {
        name: {**v, "destinations": sorted(v["destinations"])}
        for name, v in sc_totals.items()
    }

    return {
        "restaurant_guid": restaurant_guid,
        "restaurant_name": restaurant_name_resolved,
        "business_date": business_date,
        "total_orders": total_orders,
        "total_checks": total_checks,
        "total_applied_service_charges": total_applied_scs,
        "sc_summary": sc_totals_serial,
        "sc_samples": sc_samples,
    }


@app.post("/api/admin/clear-sc-cache")
async def clear_sc_cache():
    """
    Clear the in-memory service-charge daily cache (_sc_daily_cache).

    The SC cache persists for the life of the server process.  If a stale 0.0 entry was
    cached from an earlier run with incorrect filter logic, it will suppress all SC
    deductions for that restaurant/date even after the code is fixed.  Call this endpoint
    (or restart the server) to force a fresh Orders API fetch on the next report run.
    """
    global _sc_daily_cache
    count = len(_sc_daily_cache)
    _sc_daily_cache = {}
    logger.info("SC daily cache cleared manually: %d entries removed", count)
    return {"cleared": True, "entries_removed": count}


# ------------------
# Orders Cache Admin Endpoints
# ------------------

@app.get("/api/admin/orders-cache/stats")
async def admin_orders_cache_stats(admin_user: User = Depends(get_current_admin_user)):
    """Overall cache statistics."""
    stats = toast_orders_db.get_stats()
    stats["scheduler"] = {
        "last_run": _nightly_job_last_result or None,
        "jobs": [
            {"id": j.id, "next_run": str(j.next_run_time), "trigger": str(j.trigger)}
            for j in _scheduler.get_jobs()
        ],
    }
    return stats


@app.get("/api/admin/orders-cache/historical-backfill-status")
async def admin_historical_backfill_status(admin_user: User = Depends(get_current_admin_user)):
    """Get the current status of the nightly historical backfill job."""
    return _backfill_status or {"status": "not_started"}


@app.post("/api/admin/orders-cache/start-historical-backfill")
async def admin_start_historical_backfill(admin_user: User = Depends(get_current_admin_user)):
    """Manually trigger the historical backfill job (runs until 8 AM cutoff)."""
    asyncio.create_task(nightly_historical_backfill())
    return {"status": "started", "message": "Historical backfill started. Will run until 8 AM."}


@app.get("/api/admin/orders-cache/failed")
async def admin_orders_cache_failed(admin_user: User = Depends(get_current_admin_user)):
    """List failed imports."""
    return toast_orders_db.get_failed_imports()


@app.post("/api/admin/orders-cache/backfill")
async def admin_orders_cache_backfill(
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    restaurant_guid: Optional[str] = Query(None, description="Single restaurant, or all if omitted"),
    overwrite: bool = Query(False),
    admin_user: User = Depends(get_current_admin_user),
):
    """Trigger backfill for a date range. Runs in background."""
    async def _run_backfill():
        from datetime import datetime as dt
        start = dt.strptime(start_date, "%Y-%m-%d").date()
        end = dt.strptime(end_date, "%Y-%m-%d").date()
        logger.info("Admin backfill started: %s to %s (restaurant=%s, overwrite=%s)",
                     start_date, end_date, restaurant_guid or "ALL", overwrite)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                if restaurant_guid:
                    guids = [restaurant_guid]
                else:
                    locations = await get_all_active_locations(client)
                    guids = [loc.restaurantGuid for loc in locations]

                d = start
                while d <= end:
                    bdate = d.strftime("%Y%m%d")
                    for rguid in guids:
                        if not overwrite and toast_orders_db.is_date_cached(rguid, bdate):
                            continue
                        try:
                            toast_orders_db.mark_import_started(rguid, bdate)
                            orders = await _fetch_orders_from_toast_api(client, rguid, bdate)
                            toast_orders_db.store_orders(rguid, bdate, orders)
                            await asyncio.sleep(0.5)
                        except Exception as exc:
                            toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                            logger.error("Backfill failed %s|%s: %s", rguid[:8], bdate, exc)
                    d += timedelta(days=1)
        except Exception as exc:
            logger.error("Admin backfill job failed: %s", exc)
        logger.info("Admin backfill completed: %s to %s", start_date, end_date)

    asyncio.create_task(_run_backfill())
    return {"status": "started", "start_date": start_date, "end_date": end_date,
            "restaurant_guid": restaurant_guid, "overwrite": overwrite}


@app.post("/api/admin/orders-cache/retry-failed")
async def admin_orders_cache_retry_failed(admin_user: User = Depends(get_current_admin_user)):
    """Retry all failed imports in background."""
    failed = toast_orders_db.get_failed_imports()
    if not failed:
        return {"status": "nothing_to_retry", "count": 0}

    async def _run_retry():
        logger.info("Retrying %d failed imports", len(failed))
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for item in failed:
                    rguid = item["restaurant_guid"]
                    bdate = item["business_date"]
                    try:
                        toast_orders_db.mark_import_started(rguid, bdate)
                        orders = await _fetch_orders_from_toast_api(client, rguid, bdate)
                        toast_orders_db.store_orders(rguid, bdate, orders)
                        await asyncio.sleep(0.5)
                    except Exception as exc:
                        toast_orders_db.mark_import_failed(rguid, bdate, str(exc))
                        logger.error("Retry failed %s|%s: %s", rguid[:8], bdate, exc)
        except Exception as exc:
            logger.error("Retry job failed: %s", exc)
        logger.info("Retry completed")

    asyncio.create_task(_run_retry())
    return {"status": "started", "count": len(failed)}


@app.post("/api/admin/orders-cache/run-nightly")
async def admin_run_nightly_job(admin_user: User = Depends(get_current_admin_user)):
    """Manually trigger the nightly orders cache job."""
    asyncio.create_task(nightly_orders_cache_job())
    return {"status": "started"}


@app.post("/api/admin/flash-import/run")
async def admin_run_flash_import(
    payload: dict = Body(default={}),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Manually trigger the flash sales import.
    By default imports yesterday. Pass {"date": "YYYY-MM-DD"} for a specific date,
    or {"start_date": "...", "end_date": "..."} for a backfill range.
    """
    start_str = payload.get("start_date") or payload.get("date")
    end_str = payload.get("end_date") or start_str

    # Block if already running
    if _flash_import_progress.get("running"):
        raise HTTPException(status_code=409, detail="A flash import is already running. Wait for it to finish.")

    if start_str and end_str:
        try:
            start_d = date.fromisoformat(start_str)
            end_d = date.fromisoformat(end_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD")

        total_days = (end_d - start_d).days + 1

        async def _run_backfill():
            _flash_import_progress.update({
                "running": True,
                "type": "backfill",
                "range": f"{start_str} to {end_str}",
                "total_days": total_days,
                "completed_days": 0,
                "current_date": start_str,
                "started_at": datetime.now().isoformat(),
                "failed_dates": [],
            })
            d = start_d
            completed = 0
            while d <= end_d:
                _flash_import_progress["current_date"] = d.isoformat()
                logger.info("Flash import backfill: %s (%d/%d)", d.isoformat(), completed + 1, total_days)
                try:
                    await _run_flash_import_for_date(d)
                except Exception as exc:
                    logger.error("Flash backfill failed for %s: %s", d.isoformat(), exc)
                    _flash_import_progress["failed_dates"].append(d.isoformat())
                completed += 1
                _flash_import_progress["completed_days"] = completed
                d += timedelta(days=1)
            _flash_import_progress.update({
                "running": False,
                "completed_at": datetime.now().isoformat(),
            })

        asyncio.create_task(_run_backfill())
        return {"status": "started", "range": f"{start_str} to {end_str}", "total_days": total_days}
    else:
        _flash_import_progress.update({
            "running": True,
            "type": "single",
            "range": "yesterday",
            "total_days": 1,
            "completed_days": 0,
            "current_date": (date.today() - timedelta(days=1)).isoformat(),
            "started_at": datetime.now().isoformat(),
            "failed_dates": [],
        })

        async def _run_single():
            try:
                await nightly_flash_import_job()
            finally:
                _flash_import_progress.update({
                    "running": False,
                    "completed_days": 1,
                    "completed_at": datetime.now().isoformat(),
                })

        asyncio.create_task(_run_single())
        return {"status": "started", "date": "yesterday"}


@app.get("/api/admin/flash-import/status")
async def admin_flash_import_status(admin_user: User = Depends(get_current_admin_user)):
    """Return the flash import status including live progress."""
    result = dict(_nightly_flash_last_result) if _nightly_flash_last_result else {"status": "never_run"}
    if _flash_import_progress:
        result["progress"] = dict(_flash_import_progress)
    return result


# Module-level state for the async reconcile job — same pattern as
# _flash_import_progress.  POST /api/admin/flash-reconcile kicks off a
# background task and returns immediately; the UI / CLI polls
# GET /api/admin/flash-reconcile/status until `running` flips to False.
_flash_reconcile_progress: Dict[str, Any] = {}


@app.post("/api/admin/flash-reconcile")
async def admin_flash_reconcile(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Kick off a flash reconcile job in the background.  Returns immediately
    with {"status": "started", ...}; the actual work (Toast ERA calls + diff
    against aloha_sales_data) runs as an asyncio task so HTTP timeouts don't
    bite long ranges.  Poll GET /api/admin/flash-reconcile/status for
    progress and the final result.

    Payload:
        {
            "start_date": "YYYY-MM-DD",
            "end_date":   "YYYY-MM-DD",
            "min_abs_diff":  50.0,      # optional, dollars; default 50
            "min_pct_diff":  0.5,       # optional, percent; default 0.5
            "locations":     [...]      # optional Aloha loc-id allowlist
        }
    """
    start_str = payload.get("start_date")
    end_str = payload.get("end_date")
    if not start_str or not end_str:
        raise HTTPException(status_code=400, detail="start_date and end_date required (YYYY-MM-DD)")
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")
    if (end - start).days > 31:
        raise HTTPException(status_code=400, detail="Range max 31 days")

    if _flash_reconcile_progress.get("running"):
        raise HTTPException(status_code=409, detail="A flash reconcile is already running. Wait for it to finish.")

    min_abs = float(payload.get("min_abs_diff", 50.0))
    min_pct_pct = float(payload.get("min_pct_diff", 0.5))   # keep raw percent for display
    min_pct = min_pct_pct / 100.0
    loc_filter = set(str(x).strip() for x in payload.get("locations") or [])
    total_days = (end - start).days + 1

    async def _run_reconcile():
        discrepancies: List[dict] = []
        chain_stored = 0.0
        chain_fresh = 0.0
        chain_diff_abs = 0.0
        per_date_summary: List[dict] = []

        _flash_reconcile_progress.update({
            "running": True,
            "range": f"{start.isoformat()} to {end.isoformat()}",
            "total_days": total_days,
            "completed_days": 0,
            "current_date": start.isoformat(),
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "result": None,
            "thresholds": {
                "min_abs_diff_usd": round(min_abs, 2),
                "min_pct_diff": round(min_pct_pct, 4),
            },
        })

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                d = start
                completed = 0
                while d <= end:
                    d_str = d.isoformat()
                    _flash_reconcile_progress["current_date"] = d_str
                    try:
                        fresh = await _compute_flash_loc_data(client, d)
                    except Exception as exc:
                        logger.error("flash-reconcile: compute failed for %s: %s", d_str, exc, exc_info=True)
                        per_date_summary.append({
                            "business_date": d_str,
                            "status": "compute_failed",
                            "error": str(exc),
                        })
                        completed += 1
                        _flash_reconcile_progress["completed_days"] = completed
                        d += timedelta(days=1)
                        continue

                    df = aloha_sales_db.query_date_range(
                        d_str, d_str,
                        locations=list(loc_filter) if loc_filter else None,
                    )
                    stored_by_loc: Dict[str, float] = {}
                    if not df.empty:
                        for _, row in df.iterrows():
                            lid = str(row["location"]).strip()
                            if not lid:
                                continue
                            stored_by_loc[lid] = float(row.get("net_sales", 0) or 0)

                    fresh_total = 0.0
                    stored_total = 0.0
                    all_locs = set(fresh.keys()) | set(stored_by_loc.keys())
                    if loc_filter:
                        all_locs &= loc_filter
                    for lid in all_locs:
                        fresh_val = float(fresh.get(lid, {}).get("net_sales", 0.0))
                        stored_val = float(stored_by_loc.get(lid, 0.0))
                        fresh_total += fresh_val
                        stored_total += stored_val
                        diff = stored_val - fresh_val
                        denom = max(abs(fresh_val), abs(stored_val), 1e-9)
                        pct = diff / denom
                        if abs(diff) >= min_abs or abs(pct) >= min_pct:
                            discrepancies.append({
                                "location": lid,
                                "business_date": d_str,
                                "stored_net_sales": round(stored_val, 2),
                                "fresh_net_sales": round(fresh_val, 2),
                                "diff": round(diff, 2),
                                "pct_diff": round(pct * 100, 2),
                            })

                    chain_stored += stored_total
                    chain_fresh += fresh_total
                    chain_diff_abs += abs(stored_total - fresh_total)
                    per_date_summary.append({
                        "business_date": d_str,
                        "status": "ok",
                        "locations_checked": len(all_locs),
                        "stored_total": round(stored_total, 2),
                        "fresh_total": round(fresh_total, 2),
                        "diff": round(stored_total - fresh_total, 2),
                    })
                    completed += 1
                    _flash_reconcile_progress["completed_days"] = completed
                    d += timedelta(days=1)

            discrepancies.sort(key=lambda r: abs(r["diff"]), reverse=True)

            _flash_reconcile_progress.update({
                "running": False,
                "completed_at": datetime.now().isoformat(),
                "result": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "thresholds": {
                        "min_abs_diff_usd": round(min_abs, 2),
                        "min_pct_diff": round(min_pct_pct, 4),
                    },
                    "chain_stored_total": round(chain_stored, 2),
                    "chain_fresh_total": round(chain_fresh, 2),
                    "chain_net_diff": round(chain_stored - chain_fresh, 2),
                    "chain_abs_diff_sum_per_date": round(chain_diff_abs, 2),
                    "discrepancy_count": len(discrepancies),
                    "per_date": per_date_summary,
                    "discrepancies": discrepancies,
                },
            })
        except Exception as exc:
            logger.error("flash-reconcile job crashed: %s", exc, exc_info=True)
            _flash_reconcile_progress.update({
                "running": False,
                "completed_at": datetime.now().isoformat(),
                "error": str(exc),
            })

    asyncio.create_task(_run_reconcile())
    return {
        "status": "started",
        "range": f"{start.isoformat()} to {end.isoformat()}",
        "total_days": total_days,
        "thresholds": {
            "min_abs_diff_usd": round(min_abs, 2),
            "min_pct_diff": round(min_pct_pct, 4),
        },
    }


@app.get("/api/admin/flash-reconcile/status")
async def admin_flash_reconcile_status(
    admin_user: User = Depends(get_current_admin_user),
):
    """Return live progress + final result for the most recent reconcile job."""
    if not _flash_reconcile_progress:
        return {"status": "never_run"}
    return dict(_flash_reconcile_progress)


# ──────────────────────────────────────────────────────────────────────────────
# Precompute menu-report cache (admin-triggered backfill)
# ──────────────────────────────────────────────────────────────────────────────
# Mirrors the flash-reconcile pattern: POST kicks off a background asyncio
# task that warms the menu-report cache for the canonical shapes (see
# precompute_menu_report.DEFAULT_SHAPES). UI polls GET .../status until
# `running` flips False.

_precompute_menu_progress: Dict[str, Any] = {}
_precompute_menu_task: Optional[asyncio.Task] = None


@app.post("/api/admin/precompute-menu")
async def admin_precompute_menu(
    payload: dict = Body(default={}),
    request: Request = None,
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Kick off the menu-report precompute job. Returns immediately; poll
    GET /api/admin/precompute-menu/status for progress.

    Payload (all optional):
        {
            "base_url":   "http://127.0.0.1:8000",   # default: same-origin via request
            "timeout_s":  600                          # per-shape HTTP timeout
        }
    """
    import precompute_menu_report

    if _precompute_menu_progress.get("running"):
        raise HTTPException(status_code=409, detail="A precompute job is already running. Wait for it to finish.")

    # Default to plain-HTTP loopback — uvicorn listens on 127.0.0.1:8000 on
    # this server, no need to round-trip through the public HTTPS endpoint.
    # Deriving from `request.url` was unreliable on Cloudways: the outer nginx
    # set X-Forwarded-Proto=https, so the scheme came back as https but httpx
    # then tried TLS against a non-TLS local target → SSL WRONG_VERSION_NUMBER.
    base_url = payload.get("base_url") or "http://127.0.0.1:8000"

    timeout_s = float(payload.get("timeout_s", 1800.0))

    auth_header: Optional[str] = None
    if request is not None:
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")

    # If the caller supplied a custom date range, build a single ad-hoc shape
    # using the canonical Consolidated tag superset (so any tag subset of that
    # range still hits the cache instantly via the superset fallback).
    custom_start_raw = (payload.get("start_date") or "").strip()
    custom_end_raw = (payload.get("end_date") or "").strip()
    if custom_start_raw and custom_end_raw:
        try:
            _sd = date.fromisoformat(custom_start_raw)
            _ed = date.fromisoformat(custom_end_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date / end_date must be YYYY-MM-DD")
        if _ed < _sd:
            raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
        shapes = [{
            "label": f"custom {_sd.isoformat()} → {_ed.isoformat()} · YoY fiscal · all stores · consolidated tags",
            "config": {
                "reportType":       "menu",
                "dateRangeType":    "custom",
                "customStartDate":  _sd.isoformat(),
                "customEndDate":    _ed.isoformat(),
                "restaurantIds":    [],
                "groupBy":          [],
                "itemTags":         sorted(precompute_cache.CONSOLIDATED_TAGS_SUPERSET),
                "includeZeroSales": False,
                "yoyEnabled":       True,
                "yoyMode":          "fiscal",
                "alohaDate":        None,
                "orderModeFilter":  None,
                "debug":            False,
            },
        }]
    else:
        shapes = precompute_menu_report.DEFAULT_SHAPES
    total = len(shapes)

    _precompute_menu_progress.clear()
    _precompute_menu_progress.update({
        "running":         True,
        "started_at":      datetime.now().isoformat(),
        "completed_at":    None,
        "base_url":        base_url,
        "total_shapes":    total,
        "completed_shapes": 0,
        "current_label":   None,
        "shapes":          [
            {"index": i, "label": s.get("label", f"shape #{i+1}"), "status": "pending"}
            for i, s in enumerate(shapes)
        ],
        "error":           None,
        "summary":         None,
    })

    def _on_progress(entry: Dict[str, Any]) -> None:
        idx = entry.get("index")
        if idx is None or idx >= total:
            return
        slot = _precompute_menu_progress["shapes"][idx]
        slot.update({
            "label":       entry.get("label", slot.get("label")),
            "status":      entry.get("status", slot.get("status")),
            "duration_s":  entry.get("duration_s"),
            "cache":       entry.get("cache"),
            "cache_key":   entry.get("cache_key"),
            "http_status": entry.get("http_status"),
            "error":       entry.get("error"),
        })
        if entry.get("status") == "running":
            _precompute_menu_progress["current_label"] = entry.get("label")
        else:
            _precompute_menu_progress["completed_shapes"] = sum(
                1 for s in _precompute_menu_progress["shapes"]
                if s.get("status") in ("ok", "error")
            )

    async def _run_job():
        try:
            summary = await precompute_menu_report.run_default_shapes(
                base_url=base_url,
                auth_header=auth_header,
                shapes=shapes,
                on_progress=_on_progress,
                request_timeout_s=timeout_s,
            )
            _precompute_menu_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_label": None,
                "summary": {
                    "total_shapes": summary.get("total_shapes"),
                    "ok_count":     summary.get("ok_count"),
                    "error_count":  summary.get("error_count"),
                },
            })
        except asyncio.CancelledError:
            logger.info("precompute-menu job cancelled by admin")
            # Mark any not-yet-finished shapes as cancelled so the UI clears.
            for _s in _precompute_menu_progress.get("shapes", []):
                if _s.get("status") in ("pending", "running"):
                    _s["status"] = "cancelled"
            _precompute_menu_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_label": None,
                "cancelled":    True,
            })
            # Re-raise so the asyncio runtime can finalize the task properly.
            raise
        except Exception as exc:
            logger.error("precompute-menu job crashed: %s", exc, exc_info=True)
            _precompute_menu_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_label": None,
                "error":        str(exc),
            })

    global _precompute_menu_task
    _precompute_menu_task = asyncio.create_task(_run_job())
    return {
        "status":       "started",
        "total_shapes": total,
        "base_url":     base_url,
    }


@app.delete("/api/admin/precompute-menu")
async def admin_precompute_menu_cancel(
    admin_user: User = Depends(get_current_admin_user),
):
    """Cancel the currently-running precompute job. Idempotent."""
    global _precompute_menu_task
    if _precompute_menu_task is None or _precompute_menu_task.done():
        return {"status": "not_running"}
    _precompute_menu_task.cancel()
    return {"status": "cancel_requested"}


@app.get("/api/admin/precompute-menu/status")
async def admin_precompute_menu_status(
    admin_user: User = Depends(get_current_admin_user),
):
    """Return live progress + final summary for the most recent precompute run."""
    if not _precompute_menu_progress:
        return {"status": "never_run"}
    return dict(_precompute_menu_progress)


@app.get("/api/admin/precompute-menu/cache")
async def admin_precompute_menu_cache_list(
    admin_user: User = Depends(get_current_admin_user),
):
    """List the on-disk menu-report cache entries."""
    return precompute_cache.list_index()


@app.delete("/api/admin/precompute-menu/cache")
async def admin_precompute_menu_cache_clear(
    admin_user: User = Depends(get_current_admin_user),
):
    """Delete every cached menu_*.json file. Next requests will recompute fresh."""
    n = precompute_cache.clear_cache()
    return {"deleted": n}


# ──────────────────────────────────────────────────────────────────────────────
# Toast menu raw-data backfill (admin-triggered)
# ──────────────────────────────────────────────────────────────────────────────
# Backfills `toast_menu_raw.db` by week-chunked Toast Analytics calls (both
# MENU_ITEM and MODIFIER groupBy). Once a date range is in, ANY menu report
# for any sub-range / store filter / tag selection can be served from local
# SQLite instead of round-tripping to Toast — that's the slow part of a
# menu report (~30s+ per Toast call). Immutable semantics: days already in
# the backfill_log are skipped unless force=True.

_menu_backfill_progress: Dict[str, Any] = {}
_menu_backfill_task: Optional[asyncio.Task] = None


def _menu_backfill_week_chunks(start: date, end: date, days_per_chunk: int = 7) -> List[Tuple[date, date]]:
    """Inclusive [start, end] split into chunks of at most days_per_chunk days."""
    out: List[Tuple[date, date]] = []
    cur = start
    while cur <= end:
        last = min(cur + timedelta(days=days_per_chunk - 1), end)
        out.append((cur, last))
        cur = last + timedelta(days=1)
    return out


@app.post("/api/admin/menu-backfill")
async def admin_menu_backfill(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Kick off a Toast menu-raw-data backfill for [start_date, end_date].
    Returns immediately; poll GET /api/admin/menu-backfill/status for progress.

    Payload:
      {
        "start_date":  "YYYY-MM-DD",
        "end_date":    "YYYY-MM-DD",
        "force":       false,                # re-fetch days already in backfill_log
        "stores":      []                    # optional restaurantGuid filter; empty = all
      }
    """
    global _menu_backfill_task

    start_str = (payload.get("start_date") or "").strip()
    end_str = (payload.get("end_date") or "").strip()
    if not start_str or not end_str:
        raise HTTPException(status_code=400, detail="start_date + end_date required (YYYY-MM-DD)")
    try:
        bf_start = date.fromisoformat(start_str)
        bf_end = date.fromisoformat(end_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if bf_end < bf_start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    if (bf_end - bf_start).days > 400:
        raise HTTPException(status_code=400, detail="Backfill range capped at 400 days per run")

    if _menu_backfill_progress.get("running"):
        raise HTTPException(status_code=409, detail="A menu backfill is already running. Wait or cancel it first.")

    force = bool(payload.get("force", False))
    requested_stores = list(payload.get("stores") or [])

    chunks = _menu_backfill_week_chunks(bf_start, bf_end, days_per_chunk=7)
    total_days = (bf_end - bf_start).days + 1

    _menu_backfill_progress.clear()
    _menu_backfill_progress.update({
        "running":          True,
        "started_at":       datetime.now().isoformat(),
        "completed_at":     None,
        "range":            f"{bf_start.isoformat()} to {bf_end.isoformat()}",
        "force":            force,
        "total_days":       total_days,
        "total_chunks":     len(chunks),
        "completed_chunks": 0,
        "current_chunk":    None,
        "rows_inserted":    0,
        "days_logged":      0,
        "skipped_days":     0,
        "error":            None,
        "cancelled":        False,
    })

    async def _run_backfill():
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                if requested_stores:
                    restaurant_ids = requested_stores
                else:
                    locations = await get_all_active_locations(client)
                    restaurant_ids = [loc.restaurantGuid for loc in locations]

                for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
                    chunk_label = f"{chunk_start.isoformat()} → {chunk_end.isoformat()}"
                    _menu_backfill_progress["current_chunk"] = chunk_label

                    # Build the list of dates in this chunk once.
                    chunk_dates: List[str] = []
                    d = chunk_start
                    while d <= chunk_end:
                        chunk_dates.append(d.isoformat())
                        d += timedelta(days=1)

                    # Skip the whole chunk if every day is already backfilled and not force.
                    if not force:
                        already = set(toast_menu_raw_db.dates_covered(
                            chunk_start.isoformat(), chunk_end.isoformat()
                        ))
                        if set(chunk_dates).issubset(already):
                            _menu_backfill_progress["skipped_days"] = (
                                _menu_backfill_progress.get("skipped_days", 0) + len(chunk_dates)
                            )
                            _menu_backfill_progress["completed_chunks"] = chunk_idx + 1
                            continue

                    chunk_total_rows = 0
                    menu_item_guid_set: set = set()

                    # Pass 1: MENU_ITEM (also gives us the guid set for modifier flagging).
                    guid_mi = await request_toast_report(
                        client, chunk_start, chunk_end, restaurant_ids,
                        report_type="menu", group_by=["MENU_ITEM"],
                    )
                    mi_rows = await fetch_toast_report(client, guid_mi, "menu")
                    for r in mi_rows:
                        g = r.get("menuItemGuid") or r.get("modifierGuid")
                        if g:
                            menu_item_guid_set.add(g)
                    # SQLite insert of 60k+ rows is synchronous and blocks the
                    # event loop — page loads / API polls stall while it runs.
                    # asyncio.to_thread keeps uvicorn responsive during backfill.
                    chunk_total_rows += await asyncio.to_thread(
                        toast_menu_raw_db.insert_rows,
                        mi_rows, "MENU_ITEM", menu_item_guid_set,
                    )

                    # Pass 2: MODIFIER
                    guid_mod = await request_toast_report(
                        client, chunk_start, chunk_end, restaurant_ids,
                        report_type="menu", group_by=["MODIFIER"],
                    )
                    mod_rows = await fetch_toast_report(client, guid_mod, "menu")
                    chunk_total_rows += await asyncio.to_thread(
                        toast_menu_raw_db.insert_rows,
                        mod_rows, "MODIFIER", menu_item_guid_set,
                    )

                    days_logged = await asyncio.to_thread(
                        toast_menu_raw_db.mark_dates_backfilled, chunk_dates,
                    )
                    _menu_backfill_progress["days_logged"] = (
                        _menu_backfill_progress.get("days_logged", 0) + days_logged
                    )
                    _menu_backfill_progress["rows_inserted"] = (
                        _menu_backfill_progress.get("rows_inserted", 0) + chunk_total_rows
                    )
                    _menu_backfill_progress["completed_chunks"] = chunk_idx + 1

            # ── Phase 2: warm the precompute cache for the backfilled range
            # The pivot + YoY work over a month of data exceeds nginx's 60s
            # timeout, which breaks the "Include All Consolidated Categories"
            # Excel export path. Trigger /api/reports/run via loopback HTTP
            # (no nginx, no 60s limit) with all 15 consolidated tags so the
            # cache is warm when the user goes to export.
            try:
                import precompute_menu_report
                custom_shape = {
                    "label": f"auto-warm {bf_start.isoformat()} → {bf_end.isoformat()} · YoY fiscal · all stores · consolidated tags",
                    "config": {
                        "reportType":       "menu",
                        "dateRangeType":    "custom",
                        "customStartDate":  bf_start.isoformat(),
                        "customEndDate":    bf_end.isoformat(),
                        "restaurantIds":    [],
                        "groupBy":          [],
                        "itemTags":         sorted(precompute_cache.CONSOLIDATED_TAGS_SUPERSET),
                        "includeZeroSales": False,
                        "yoyEnabled":       True,
                        "yoyMode":          "fiscal",
                        "alohaDate":        None,
                        "orderModeFilter":  None,
                        "debug":            False,
                    },
                }
                _menu_backfill_progress["phase"] = "warming_cache"
                _menu_backfill_progress["current_chunk"] = f"Warming menu-report cache for {bf_start.isoformat()} → {bf_end.isoformat()}"
                logger.info(
                    "menu-backfill: warming precompute cache for %s → %s",
                    bf_start.isoformat(), bf_end.isoformat(),
                )
                warm_summary = await precompute_menu_report.run_default_shapes(
                    base_url="http://127.0.0.1:8000",
                    auth_header=None,
                    shapes=[custom_shape],
                    on_progress=None,
                    request_timeout_s=1800.0,
                )
                _menu_backfill_progress["cache_warm"] = {
                    "ok_count":    warm_summary.get("ok_count", 0),
                    "error_count": warm_summary.get("error_count", 0),
                }
            except Exception as _warm_exc:
                # The backfill itself succeeded — don't fail the whole job
                # just because warming the cache hit a problem. Log it and
                # let the user retry via the precompute UI if they need to.
                logger.warning("menu-backfill: cache warm step failed: %s", _warm_exc)
                _menu_backfill_progress["cache_warm_error"] = str(_warm_exc)

            _menu_backfill_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_chunk": None,
                "phase":         "done",
            })
        except asyncio.CancelledError:
            logger.info("menu-backfill cancelled by admin")
            _menu_backfill_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_chunk": None,
                "cancelled":    True,
            })
            raise
        except Exception as exc:
            logger.error("menu-backfill crashed: %s", exc, exc_info=True)
            _menu_backfill_progress.update({
                "running":      False,
                "completed_at": datetime.now().isoformat(),
                "current_chunk": None,
                "error":        str(exc),
            })

    _menu_backfill_task = asyncio.create_task(_run_backfill())
    return {
        "status":       "started",
        "range":        f"{bf_start.isoformat()} to {bf_end.isoformat()}",
        "total_chunks": len(chunks),
        "total_days":   total_days,
    }


@app.get("/api/admin/menu-backfill/status")
async def admin_menu_backfill_status(admin_user: User = Depends(get_current_admin_user)):
    """Live progress + final state for the most recent backfill run."""
    if not _menu_backfill_progress:
        return {"status": "never_run"}
    return dict(_menu_backfill_progress)


@app.delete("/api/admin/menu-backfill")
async def admin_menu_backfill_cancel(admin_user: User = Depends(get_current_admin_user)):
    """Cancel the in-flight backfill. Idempotent."""
    global _menu_backfill_task
    if _menu_backfill_task is None or _menu_backfill_task.done():
        return {"status": "not_running"}
    _menu_backfill_task.cancel()
    return {"status": "cancel_requested"}


@app.get("/api/admin/menu-backfill/stats")
async def admin_menu_backfill_stats(admin_user: User = Depends(get_current_admin_user)):
    """Top-line stats: row count, date range covered, db size."""
    return toast_menu_raw_db.get_stats()


@app.get("/api/admin/menu-backfill/coverage")
async def admin_menu_backfill_coverage(
    start_date: str,
    end_date: str,
    admin_user: User = Depends(get_current_admin_user),
):
    """Per-day backfill log entries within [start, end]."""
    return toast_menu_raw_db.coverage_summary(start_date, end_date)


@app.delete("/api/admin/menu-backfill/range")
async def admin_menu_backfill_clear(
    start_date: str,
    end_date: str,
    admin_user: User = Depends(get_current_admin_user),
):
    """Wipe raw rows + log entries within [start, end]. Use before a force-refresh."""
    deleted = toast_menu_raw_db.clear_range(start_date, end_date)
    return {"deleted_rows": deleted}


# ──────────────────────────────────────────────────────────────────────────────
# Toast Config DB admin endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/admin/toast-config/stats")
async def admin_toast_config_stats(admin_user: User = Depends(get_current_admin_user)):
    """Return toast_config.db statistics."""
    return toast_config_db.get_stats()


@app.post("/api/admin/toast-config/sync")
async def admin_toast_config_sync(
    payload: dict = Body(default={}),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Trigger toast config sync from Toast API.
    Pass {"step": "menu-config"|"attribute-map"|"all"} (default: all).
    Pass {"restaurant_guid": "..."} to sync a single location.
    """
    from sync_toast_config import sync_menu_config, sync_attribute_map, sync_all

    step = payload.get("step", "all")
    guid = payload.get("restaurant_guid")
    guids = [guid] if guid else None

    if step == "menu-config":
        result = await sync_menu_config(guids)
    elif step == "attribute-map":
        count = await sync_attribute_map()
        result = {"attribute_map_rows": count}
    else:
        result = await sync_all(guids)

    return result


@app.get("/api/admin/toast-config/menu-export")
async def admin_toast_config_menu_export(
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Wide-format CSV export of the Toast menu with one column per distinct tag.

    Columns: toast_item_name, item_guid, multi_location_id, sales_cat, item_plu,
    then one column per distinct tag name (alphabetical). Cell holds the tag
    name if the item has that tag, else blank.
    Deduplicated by (multi_location_id, item_guid); tag set is the union across
    all store_guids that carry the item.
    """
    import io
    import sqlite3
    db_path = toast_config_db.TOAST_CONFIG_DB_PATH

    with toast_config_db.get_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        all_tags = [r[0] for r in conn.execute(
            "SELECT DISTINCT tag_name FROM cfg_tags WHERE tag_name != '' ORDER BY tag_name"
        ).fetchall()]

        rows = conn.execute(
            """SELECT multi_location_id, item_guid, item_name, sales_cat,
                      item_plu, item_tags
                 FROM cfg_menu_items
                WHERE multi_location_id != '' AND item_guid != ''"""
        ).fetchall()

    # Aggregate by (mli, item_guid): union of tag names, first-seen item fields.
    agg: dict = {}
    for r in rows:
        key = (r["multi_location_id"], r["item_guid"])
        entry = agg.get(key)
        if entry is None:
            entry = {
                "toast_item_name": r["item_name"] or "",
                "item_guid": r["item_guid"],
                "multi_location_id": r["multi_location_id"],
                "sales_cat": r["sales_cat"] or "",
                "item_plu": r["item_plu"] or 0,
                "tags": set(),
            }
            agg[key] = entry
        try:
            tag_list = json.loads(r["item_tags"] or "[]")
        except (TypeError, ValueError):
            tag_list = []
        for t in tag_list:
            name = t.get("name") if isinstance(t, dict) else None
            if name:
                entry["tags"].add(name)

    # Build CSV
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    header = ["toast_item_name", "item_guid", "multi_location_id", "sales_cat", "item_plu"] + all_tags
    writer.writerow(header)

    for entry in sorted(agg.values(), key=lambda e: (e["sales_cat"], e["toast_item_name"])):
        item_tags = entry["tags"]
        row = [
            entry["toast_item_name"],
            entry["item_guid"],
            entry["multi_location_id"],
            entry["sales_cat"],
            entry["item_plu"],
        ]
        row.extend(t if t in item_tags else "" for t in all_tags)
        writer.writerow(row)

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"toast_menu_tags_{today}.csv"
    logger.info("Admin exported Toast menu tags CSV (%d items, %d tag cols) by %s",
                len(agg), len(all_tags), admin_user.email)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/admin/upload-db")
async def admin_upload_db(
    db_name: str = Query(..., description="Target DB: aloha_sales, crunchtime, punchh, monkey"),
    file: UploadFile = File(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Upload a SQLite DB file to replace an existing database on the server."""
    ALLOWED_DBS = {
        "aloha_sales": os.path.join(os.path.dirname(__file__), "data", "aloha", "aloha_sales.db"),
        "crunchtime": os.path.join(os.path.dirname(__file__), "data", "crunchtime.db"),
        "punchh": os.path.join(os.path.dirname(__file__), "data", "punchh.db"),
        "monkey": os.path.join(os.path.dirname(__file__), "data", "monkey.db"),
    }
    if db_name not in ALLOWED_DBS:
        raise HTTPException(400, f"Invalid db_name. Allowed: {list(ALLOWED_DBS.keys())}")

    target_path = ALLOWED_DBS[db_name]
    content = await file.read()

    # Basic validation: SQLite files start with "SQLite format 3"
    if not content[:16].startswith(b"SQLite format 3"):
        raise HTTPException(400, "File does not appear to be a valid SQLite database")

    # Write to disk
    with open(target_path, "wb") as f:
        f.write(content)

    size_mb = len(content) / (1024 * 1024)
    logger.info("Admin uploaded %s DB (%.1f MB) by %s", db_name, size_mb, admin_user.email)
    return {"status": "ok", "db_name": db_name, "size_mb": round(size_mb, 1), "path": target_path}


@app.post("/api/admin/upload-store-information")
async def admin_upload_store_information(
    file: UploadFile = File(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Upload the Store Information Excel to refresh store_ownership.json."""
    content = await file.read()
    try:
        import io
        import pandas as pd
        df = pd.read_excel(io.BytesIO(content), header=0)
        required = {"Restaurant ID", "Restaurant Name", "Company Name", "Company or Franchisee", "Toast GUID"}
        missing = required - set(df.columns)
        if missing:
            raise HTTPException(400, f"Missing columns: {missing}")

        ownership = {}
        for _, row in df.iterrows():
            rid_raw = row.get("Restaurant ID")
            if pd.isna(rid_raw):
                continue
            rid = str(int(rid_raw))
            ot = str(row.get("Company or Franchisee", "Franchise")).strip()
            guid_raw = str(row.get("Toast GUID", "")).strip()
            guid = guid_raw if guid_raw not in ("0", "nan", "") else None
            ownership[rid] = {
                "ownershipType": ot,
                "restaurantName": str(row.get("Restaurant Name", "")).strip(),
                "companyName": str(row.get("Company Name", "")).strip() if pd.notna(row.get("Company Name")) else "",
                "toastGuid": guid,
            }

        out_path = os.path.join(os.path.dirname(__file__), "data", "store_ownership.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(ownership, f, indent=2)

        company_count = sum(1 for v in ownership.values() if v["ownershipType"] == "Company")
        franchise_count = sum(1 for v in ownership.values() if v["ownershipType"] == "Franchise")
        logger.info("Admin uploaded Store Information: %d stores (%d Company, %d Franchise) by %s",
                    len(ownership), company_count, franchise_count, admin_user.email)
        return {"status": "ok", "total": len(ownership), "company": company_count, "franchise": franchise_count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Failed to parse Store Information Excel: {e}")


@app.post("/api/admin/aloha-sales/push-rows")
async def admin_push_aloha_sales_rows(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Push individual aloha_sales_db rows (for non-Toast stores like Lubbock).
    Payload: {"rows": [{"business_date": "2026-03-23", "location": "1188", "restaurant_name": "Lubbock TX (Broadway)", "net_sales": 1109.85, ...}]}
    """
    rows = payload.get("rows", [])
    if not rows:
        raise HTTPException(400, "No rows provided")

    import pandas as pd
    from collections import defaultdict

    # Group rows by business_date so upsert_date gets ALL rows for each date at once
    # (upsert_date DELETEs all rows for a date before inserting)
    by_date = defaultdict(list)
    for row in rows:
        bdate = row.get("business_date")
        if not bdate:
            continue
        by_date[bdate].append({
            "RestaurantID": row.get("location", ""),
            "RestaurantName": row.get("restaurant_name", ""),
            "NetSales": float(row.get("net_sales", 0) or 0),
            "OLONetSales": float(row.get("olo_net_sales", 0) or 0),
            "DeliveryNetSales": float(row.get("delivery_net_sales", 0) or 0),
            "CateringNetSales": float(row.get("catering_net_sales", 0) or 0),
            "GNGNetSales": float(row.get("gng_net_sales", 0) or 0),
            "ToGoNetSales": float(row.get("togo_net_sales", 0) or 0),
            "CurbsideNetSales": float(row.get("curbside_net_sales", 0) or 0),
            "CurbsideOLONetSales": float(row.get("curbside_olo_net_sales", 0) or 0),
            "DineInNetSales": float(row.get("dinein_net_sales", 0) or 0),
            "CallInNetSales": float(row.get("callin_net_sales", 0) or 0),
            "OLOCallInNetSales": float(row.get("olo_callin_net_sales", 0) or 0),
            "CheckCount": float(row.get("check_count", 0) or 0),
            "ChecksMinusCatering": float(row.get("checks_minus_catering", 0) or 0),
        })

    inserted = 0
    for bdate, date_rows in sorted(by_date.items()):
        df = pd.DataFrame(date_rows)
        # Use upsert_rows (not upsert_date) to avoid deleting other locations
        n = aloha_sales_db.upsert_rows(df, bdate)
        inserted += n

    logger.info("Admin pushed %d aloha_sales rows (%d dates) by %s", inserted, len(by_date), admin_user.email)
    return {"status": "ok", "rows_upserted": inserted, "dates_updated": len(by_date)}


@app.post("/api/admin/toast-config/backfill-aloha")
async def admin_backfill_aloha(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Backfill aloha_plu and aloha_item_name into mli_plu_attribute_map.
    Expects {"mappings": [{"multi_location_id": "...", "aloha_plu": 123, "aloha_item_name": "..."}, ...]}
    """
    mappings = payload.get("mappings", [])
    if not mappings:
        raise HTTPException(status_code=400, detail="No mappings provided")

    conn = toast_config_db.get_connection()
    updated = 0
    try:
        for m in mappings:
            mli = m.get("multi_location_id", "")
            aplu = m.get("aloha_plu", 0)
            aname = m.get("aloha_item_name", "")
            if mli and (aplu or aname):
                result = conn.execute(
                    "UPDATE mli_plu_attribute_map SET aloha_plu = ?, aloha_item_name = ? WHERE multi_location_id = ?",
                    (aplu, aname, mli),
                )
                updated += result.rowcount
        conn.commit()
    finally:
        conn.close()

    return {"updated": updated, "submitted": len(mappings)}


# ──────────────────────────────────────────────────────────────────────────────
def _parse_ee_value(val) -> float:
    """Parse entree equiv value — handles both numeric (1.0) and tag strings ('EE 1')."""
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        # Parse "EE 1" or "EE 0.5" style strings
        s = str(val).strip()
        if s.upper().startswith("EE"):
            s = s[2:].strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0


# Validation Reports (migrated from sp_rpt_Toast_Net_Sales_Validation
# and sp_rpt_IT_Menu_Item_Count_Validation)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/validation/net-sales")
async def admin_validation_net_sales(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Compare ERA net sales vs order-level calculated net per store per day.
    Replaces sp_rpt_Toast_Net_Sales_Validation.
    """
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="start_date and end_date required")

    # Load GUID → store number mapping
    loc_mapping = get_aloha_location_mapping()  # {guid: store_num}
    reverse_mapping = {str(v): k for k, v in loc_mapping.items()}  # {store_num: guid}

    # 1. ERA net sales by date/store
    era_df = aloha_sales_db.query_date_range_daily(start_date, end_date)
    era_lookup: Dict[str, Dict] = {}  # "date|store" → {net_sales, name}
    if not era_df.empty:
        for _, row in era_df.iterrows():
            key = f"{row['business_date']}|{row['location']}"
            era_lookup[key] = {
                "net_sales": float(row.get("net_sales", 0) or 0),
                "name": row.get("restaurant_name", ""),
            }

    # 2. Orders net sales by date/store (JSON aggregation)
    # Convert date format: aloha_sales uses YYYY-MM-DD, orders uses YYYYMMDD
    start_yyyymmdd = start_date.replace("-", "")
    end_yyyymmdd = end_date.replace("-", "")

    orders_lookup: Dict[str, float] = {}  # "date|store" → calc_net
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(toast_orders_db.get_daily_net_sales, start_yyyymmdd, end_yyyymmdd),
            timeout=30.0,
        )

        for bdate, guid, calc_net in rows:
            store_num = str(loc_mapping.get(guid, ""))
            if store_num:
                # Convert YYYYMMDD → YYYY-MM-DD
                formatted_date = f"{bdate[:4]}-{bdate[4:6]}-{bdate[6:]}"
                key = f"{formatted_date}|{store_num}"
                orders_lookup[key] = round(float(calc_net or 0), 2)
    except asyncio.TimeoutError:
        logger.warning("Validation: orders query timed out after 30s")
    except Exception as exc:
        logger.warning(f"Validation: orders query failed: {exc}")

    orders_available = len(orders_lookup) > 0

    # 3. Join and compare
    all_keys = set(era_lookup.keys()) | set(orders_lookup.keys())
    result_rows = []
    total_era = 0.0
    total_orders = 0.0
    discrepancies = 0
    THRESHOLD = 1.00  # $1 tolerance

    for key in sorted(all_keys):
        parts = key.split("|")
        bdate = parts[0]
        store_num = parts[1]
        era_info = era_lookup.get(key, {})
        era_net = era_info.get("net_sales", 0)
        store_name = era_info.get("name", f"Store {store_num}")
        orders_net = orders_lookup.get(key, 0)
        diff = round(era_net - orders_net, 2)
        pct = round((diff / era_net * 100), 2) if era_net else 0

        total_era += era_net
        total_orders += orders_net
        if abs(diff) > THRESHOLD:
            discrepancies += 1

        result_rows.append({
            "business_date": bdate,
            "store_num": store_num,
            "store_name": store_name,
            "era_net_sales": round(era_net, 2),
            "orders_net_sales": round(orders_net, 2),
            "difference": diff,
            "pct_diff": pct,
        })

    # Sort by largest absolute difference
    result_rows.sort(key=lambda x: abs(x["difference"]), reverse=True)

    dates_checked = len(set(k.split("|")[0] for k in all_keys))
    stores_checked = len(set(k.split("|")[1] for k in all_keys))

    return {
        "rows": result_rows,
        "summary": {
            "total_era": round(total_era, 2),
            "total_orders": round(total_orders, 2),
            "total_diff": round(total_era - total_orders, 2),
            "dates_checked": dates_checked,
            "stores_checked": stores_checked,
            "discrepancies": discrepancies,
            "threshold": THRESHOLD,
            "orders_available": orders_available,
        },
    }


@app.post("/api/admin/validation/item-counts")
async def admin_validation_item_counts(
    payload: dict = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Compare CY item counts (Toast Analytics) vs LY item counts (Aloha DB)
    for a given category. Replaces sp_rpt_IT_Menu_Item_Count_Validation.
    """
    start_date_str = payload.get("start_date")
    end_date_str = payload.get("end_date")
    category = payload.get("category", "")
    if not start_date_str or not end_date_str:
        raise HTTPException(status_code=400, detail="start_date and end_date required")

    start_dt = date.fromisoformat(start_date_str)
    end_dt = date.fromisoformat(end_date_str)

    # Get attribute map for item metadata + category filtering
    attr_df = toast_config_db.get_attribute_map()
    if attr_df.empty:
        raise HTTPException(status_code=500, detail="Attribute map not available")

    # Filter to category if specified
    if category:
        cat_df = attr_df[attr_df["consolidated_cat"] == category]
    else:
        cat_df = attr_df[attr_df["consolidated_cat"] != ""]

    if cat_df.empty:
        return {"rows": [], "summary": {"category": category, "total_cy": 0, "total_ly": 0,
                                         "items_cy_only": 0, "items_ly_only": 0, "items_matched": 0}}

    # Build item_guid → metadata lookup
    guid_meta: Dict[str, dict] = {}
    aloha_to_meta: Dict[str, dict] = {}
    for _, row in cat_df.iterrows():
        guid = row.get("item_guid", "")
        meta = {
            "toast_item_name": row.get("toast_item_name", ""),
            "aloha_item_name": row.get("aloha_item_name", ""),
            "consolidated_cat": row.get("consolidated_cat", ""),
            "entree_equiv": _parse_ee_value(row.get("entree_equiv_tag", 0)),
            "recipe": row.get("recipe", ""),
        }
        if guid:
            guid_meta[guid] = meta
        aname = row.get("aloha_item_name", "")
        if aname:
            aloha_to_meta[aname] = meta

    # CY: Fetch Toast Analytics for menu items
    cy_counts: Dict[str, float] = {}  # toast_item_name → qty
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            locs = await get_all_active_locations(client)
            restaurant_ids = [loc.restaurantGuid for loc in locs]

            report_guid = await request_toast_report(
                client, start_dt, end_dt, restaurant_ids, "menu", ["MENU_ITEM"]
            )
            analytics_rows = await fetch_toast_report(client, report_guid, "menu") or []

            for arow in analytics_rows:
                item_guid = arow.get("menuItemGuid", "")
                qty = float(arow.get("quantitySold", 0) or 0)
                if item_guid in guid_meta and qty:
                    name = guid_meta[item_guid]["toast_item_name"]
                    cy_counts[name] = cy_counts.get(name, 0) + qty
    except Exception as exc:
        logger.warning(f"Item count validation: CY fetch failed: {exc}")

    # LY: Get fiscal calendar dates, query Aloha DB
    ly_counts: Dict[str, float] = {}  # aloha_item_name → qty
    try:
        ly_dates = aloha_sales_db.get_fiscal_ly_dates(start_date_str, end_date_str)
        if ly_dates:
            ly_start = min(ly_dates.values())
            ly_end = max(ly_dates.values())
            aloha_df = aloha_db.query_date_range(ly_start, ly_end)
            if not aloha_df.empty:
                for _, arow in aloha_df.iterrows():
                    aname = arow.get("AlohaItem", "")
                    qty = float(arow.get("Quantity", 0) or 0)
                    if aname in aloha_to_meta:
                        ly_counts[aname] = ly_counts.get(aname, 0) + qty
    except Exception as exc:
        logger.warning(f"Item count validation: LY fetch failed: {exc}")

    # Join CY and LY
    result_rows = []
    all_toast_names = set(cy_counts.keys())
    all_aloha_names = set(ly_counts.keys())
    matched = 0
    cy_only = 0
    ly_only = 0

    # Match by toast_item_name → aloha_item_name
    for tname, cy_qty in cy_counts.items():
        meta = next((m for m in guid_meta.values() if m["toast_item_name"] == tname), {})
        aname = meta.get("aloha_item_name", "")
        ly_qty = ly_counts.pop(aname, 0) if aname else 0
        change = round(cy_qty - ly_qty, 1)
        pct = round((change / ly_qty * 100), 1) if ly_qty else 0

        if ly_qty:
            matched += 1
        else:
            cy_only += 1

        result_rows.append({
            "recipe": meta.get("recipe", ""),
            "toast_item_name": tname,
            "aloha_item_name": aname,
            "entree_equiv": meta.get("entree_equiv", 0),
            "cy_count": round(cy_qty, 1),
            "ly_count": round(ly_qty, 1),
            "change": change,
            "change_pct": pct,
        })

    # Remaining LY items not matched
    for aname, ly_qty in ly_counts.items():
        meta = aloha_to_meta.get(aname, {})
        ly_only += 1
        result_rows.append({
            "recipe": meta.get("recipe", ""),
            "toast_item_name": meta.get("toast_item_name", ""),
            "aloha_item_name": aname,
            "entree_equiv": meta.get("entree_equiv", 0),
            "cy_count": 0,
            "ly_count": round(ly_qty, 1),
            "change": round(-ly_qty, 1),
            "change_pct": -100.0,
        })

    result_rows.sort(key=lambda x: abs(x["change"]), reverse=True)
    total_cy = sum(r["cy_count"] for r in result_rows)
    total_ly = sum(r["ly_count"] for r in result_rows)

    return {
        "rows": result_rows,
        "summary": {
            "category": category or "(all categories)",
            "total_cy": round(total_cy, 1),
            "total_ly": round(total_ly, 1),
            "items_cy_only": cy_only,
            "items_ly_only": ly_only,
            "items_matched": matched,
        },
    }


async def _compute_flash_loc_data(
    client: httpx.AsyncClient,
    target_date: date,
) -> Dict[str, Dict[str, float]]:
    """
    Compute per-location flash sales for one business date — the SAME math as
    the nightly importer, just without writing to the DB.  Used by both the
    importer ([_run_flash_import_for_date]) and the reconcile endpoint, so the
    two cannot drift apart.

    Steps:
      1. Fetch ERA metrics with groupBy=DINING_OPTION for the date.
      2. Bucket netSalesAmount by location and by channel db_col.
      3. For each location: subtract restaurant-destination service charges
         (per-channel where the SC name maps cleanly) and add GNG revenue;
         reclassify catering-salesCategory items rung up under non-catering
         dopts.

    Returns: {loc_id: {net_sales, olo_net_sales, ..., check_count, ...}}.
    """
    target_yyyymmdd = target_date.strftime("%Y%m%d")

    locations = await get_all_active_locations(client)
    restaurant_ids = [loc.restaurantGuid for loc in locations]
    restaurant_ids = _ensure_all_mapped_guids(restaurant_ids)
    guid_to_loc = _get_flash_loc_mapping()

    report_guid = await request_toast_report(
        client, target_date, target_date, restaurant_ids,
        report_type="metrics", group_by=["DINING_OPTION"],
    )
    era_rows = await fetch_toast_report(client, report_guid, report_type="metrics")
    if not isinstance(era_rows, list):
        era_rows = []

    loc_data: Dict[str, Dict[str, float]] = {}
    for row in era_rows:
        guid = row.get("restaurantGuid", "")
        loc_id = guid_to_loc.get(guid)
        if not loc_id:
            continue

        dining_opt = str(row.get("diningOption", "") or "").strip()
        net = float(row.get("netSalesAmount", 0) or 0)
        orders = float(row.get("ordersCount", 0) or 0)

        if loc_id not in loc_data:
            loc_data[loc_id] = {
                "net_sales": 0, "olo_net_sales": 0, "delivery_net_sales": 0,
                "catering_net_sales": 0, "gng_net_sales": 0, "togo_net_sales": 0,
                "curbside_net_sales": 0, "curbside_olo_net_sales": 0,
                "dinein_net_sales": 0, "callin_net_sales": 0, "olo_callin_net_sales": 0,
                "check_count": 0, "checks_minus_catering": 0,
                "_guid": guid, "_catering_checks": 0,
            }

        loc_data[loc_id]["net_sales"] += net
        loc_data[loc_id]["check_count"] += orders

        db_col, is_catering = _classify_dining_option(dining_opt)
        if db_col and db_col in loc_data[loc_id]:
            loc_data[loc_id][db_col] += net

        if is_catering:
            loc_data[loc_id]["_catering_checks"] += orders

    _SC_CHANNEL_TO_DB_COL = {
        "Catering": "catering_net_sales",
        "Delivery": "delivery_net_sales",
        "ToGo": "togo_net_sales",
        "OLO": "olo_net_sales",
        "DineIn": "dinein_net_sales",
        "CallIn": "callin_net_sales",
    }
    for loc_id, data in loc_data.items():
        guid = data.pop("_guid", "")
        cat_checks = data.pop("_catering_checks", 0)
        data["checks_minus_catering"] = data["check_count"] - cat_checks

        if guid:
            try:
                sc_amount, gng_amount, sc_by_dopt, cat_leak_by_dopt, cat_leak_checks = await _compute_sc_and_gng(
                    client, guid, target_yyyymmdd
                )
                if sc_amount > 0:
                    data["net_sales"] -= sc_amount
                    for sc_channel, sc_val in sc_by_dopt.items():
                        db_col = _SC_CHANNEL_TO_DB_COL.get(sc_channel)
                        if db_col and db_col in data:
                            data[db_col] -= sc_val
                if gng_amount > 0:
                    data["gng_net_sales"] = gng_amount
                for src_dopt_guid, leak_amt in cat_leak_by_dopt.items():
                    if leak_amt <= 0:
                        continue
                    src_col = _DINING_OPTION_GUID_TO_DB_COL.get(src_dopt_guid)
                    if src_col and src_col in data:
                        data[src_col] -= leak_amt
                    data["catering_net_sales"] += leak_amt
                if cat_leak_checks > 0:
                    data["checks_minus_catering"] = max(
                        0, data["checks_minus_catering"] - cat_leak_checks
                    )
                await asyncio.sleep(0.3)
            except Exception as exc:
                logger.warning("Flash SC/GNG for %s: %s", loc_id, exc)

    return loc_data


async def _run_flash_import_for_date(target_date: date):
    """Run the flash import for a specific date (used for backfill)."""
    target_str = target_date.isoformat()
    logger.info("Flash import for %s", target_str)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            loc_data = await _compute_flash_loc_data(client, target_date)

            # guid_to_name for restaurant_name fallback (not in loc_data anymore)
            locations = await get_all_active_locations(client)
            guid_to_name = {loc.restaurantGuid: loc.restaurantName for loc in locations}
            guid_to_loc = _get_flash_loc_mapping()
            loc_to_guid = {v: k for k, v in guid_to_loc.items()}

            store_info = aloha_sales_db.get_store_info()
            ownership = _load_store_ownership()
            rows_for_df = []
            for loc_id, data in loc_data.items():
                si = store_info.get(loc_id, {})
                own = ownership.get(str(loc_id), {}) if ownership else {}
                rname = (
                    (si.get("restaurant_name") or "").strip()
                    or (own.get("restaurantName") or "").strip()
                    or guid_to_name.get(loc_to_guid.get(loc_id, ""), "")
                )
                rows_for_df.append({
                    "RestaurantID": loc_id,
                    "RestaurantName": rname,
                    "NetSales": data["net_sales"],
                    "OLONetSales": data["olo_net_sales"],
                    "DeliveryNetSales": data["delivery_net_sales"],
                    "CateringNetSales": data["catering_net_sales"],
                    "GNGNetSales": data["gng_net_sales"],
                    "ToGoNetSales": data["togo_net_sales"],
                    "CurbSideNetSales": data["curbside_net_sales"],
                    "CurbSideOLONetSales": data["curbside_olo_net_sales"],
                    "DineInNetSales": data["dinein_net_sales"],
                    "CallInNetSales": data["callin_net_sales"],
                    "OLOCallInNetSales": data["olo_callin_net_sales"],
                    "CheckCount": data["check_count"],
                    "ChecksMinusCatering": data["checks_minus_catering"],
                })

            if rows_for_df:
                df = pd.DataFrame(rows_for_df)
                inserted = aloha_sales_db.upsert_date(df, target_str)
                logger.info("Flash import: %d rows for %s", inserted, target_str)

                _nightly_flash_last_result.update({
                    "business_date": target_str,
                    "locations": len(loc_data),
                    "rows_inserted": inserted,
                    "completed_at": datetime.now().isoformat(),
                    "status": "success",
                })

    except Exception as exc:
        logger.error("Flash import failed for %s: %s", target_str, exc, exc_info=True)
        _nightly_flash_last_result.update({
            "business_date": target_str,
            "status": "failed",
            "error": str(exc),
            "completed_at": datetime.now().isoformat(),
        })


class MsiDiscrepancyParseRequest(BaseModel):
    text: str


@app.post("/api/admin/msi-discrepancy/parse")
async def parse_msi_discrepancy(
    body: MsiDiscrepancyParseRequest,
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Parse a raw Monkey / MSI discrepancy report paste into structured records.

    Accepts the flat space-delimited text you copy from the Monkey portal (the report that
    shows MSI→Toast discrepancies by location with order GUIDs, tax/total diffs, and messages).

    Returns structured records with parsed fields, computed differences, and a classification
    for each discrepancy (tax rounding, item price mismatch, large discrepancy, etc.) plus
    a summary by severity and classification.
    """
    logger.info("MSI discrepancy parse requested by %s (%d chars)", admin_user.email, len(body.text))
    result = parse_msi_discrepancy_report(body.text)
    logger.info(
        "MSI discrepancy parse complete: %d records, %d warnings, %d locations, "
        "tax_exposure=$%.2f, total_exposure=$%.2f",
        result["count"],
        len(result["parse_warnings"]),
        result["summary"].get("location_count", 0),
        result["summary"].get("total_tax_exposure", 0),
        result["summary"].get("total_dollar_exposure", 0),
    )
    return result


class MsiOrderCompareRequest(BaseModel):
    toast_order_guid: str
    location_code: str
    msi_order_text: str
    msi_total_diff: Optional[float] = None
    msi_tax_diff: Optional[float] = None


@app.post("/api/admin/msi-discrepancy/compare-order")
async def msi_compare_order(
    body: MsiOrderCompareRequest,
    admin_user: User = Depends(get_current_admin_user),
):
    """
    Given a Toast order GUID + MSI location code + pasted MSI order-detail text:
      1. Resolve the location code → Toast restaurantGuid
      2. Fetch the full Toast order from the Orders API
      3. Parse the MSI order detail text into items + totals
      4. Match items between MSI and Toast, compare unit prices
      5. Return a structured comparison identifying which items (if any) explain the discrepancy
    """
    logger.info(
        "MSI order compare requested by %s — guid=%s loc=%s msi_chars=%d",
        admin_user.email, body.toast_order_guid, body.location_code, len(body.msi_order_text),
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        # ── Step 1: Resolve location code → restaurant GUID ──────────────────
        locations = await get_all_active_locations(client)
        restaurant_guid: Optional[str] = None
        restaurant_name: Optional[str] = None

        loc_code_str = str(body.location_code).strip()
        for loc in locations:
            if str(loc.externalId or "").strip() == loc_code_str:
                restaurant_guid = loc.restaurantGuid
                restaurant_name = loc.restaurantName
                break

        if not restaurant_guid:
            # Fallback: look for location code embedded in the restaurant name
            for loc in locations:
                if loc_code_str in (loc.restaurantName or ""):
                    restaurant_guid = loc.restaurantGuid
                    restaurant_name = loc.restaurantName
                    break

        if not restaurant_guid:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Location code '{body.location_code}' not found in Toast. "
                    f"Available external IDs: {[str(l.externalId) for l in locations if l.externalId][:10]}"
                ),
            )

        # ── Step 2: Fetch Toast order ─────────────────────────────────────────
        toast_order = await _fetch_single_toast_order(client, restaurant_guid, body.toast_order_guid)

        # ── Step 3: Extract Toast items ───────────────────────────────────────
        toast_items = _extract_toast_selections(toast_order)

        # ── Step 4: Parse MSI order detail ───────────────────────────────────
        msi_data = parse_msi_order_detail(body.msi_order_text)

        # ── Step 4b: Normalize — explode grouped items by primary modifier ────
        # e.g. "Custom Classic Box (Bulk) ×150" with Sandwich Pick modifier choices
        # becomes three derived items: ×50 Newk's Club @ $13.50, ×50 Chicken Salad
        # @ $13.00, ×50 BLT @ $13.00.  Secondary modifier upcharge pools (desserts,
        # etc.) are returned separately for dollar accounting.
        derived_items, modifier_pools = _normalize_msi_items(msi_data["items"])

        # ── Step 5: Compare ───────────────────────────────────────────────────
        comparison = _compare_order_items(
            derived_items,
            toast_items,
            modifier_pools,
            body.msi_total_diff or 0.0,
            body.msi_tax_diff   or 0.0,
            msi_raw_items=msi_data["items"],
        )

    logger.info(
        "MSI order compare complete — loc=%s guid=%s msi_items=%d derived=%d "
        "toast_items=%d pools=%d flagged=%d explained=$%.2f unexplained=$%.2f",
        body.location_code, body.toast_order_guid[:8],
        len(msi_data["items"]), len(derived_items), len(toast_items),
        len(modifier_pools), len(comparison["flagged"]),
        comparison["explained_diff"], comparison["unexplained_diff"],
    )

    return {
        "toast_order": {
            "guid":            body.toast_order_guid,
            "restaurant_guid": restaurant_guid,
            "restaurant_name": restaurant_name,
            "items_count":     len(toast_items),
            "items":           toast_items,
        },
        "msi_order":   msi_data,
        "msi_derived": {
            "items":          derived_items,
            "modifier_pools": modifier_pools,
        },
        "comparison":  comparison,
    }


@app.post("/api/reports/item-tags-quantities")
async def run_item_tags_quantities_report(request: ItemTagsQuantitiesRequest = Body(...)):
    """
    Generate quantities report grouped by item tags
    Uses Analytics API + Menus API tag mapping
    """
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            restaurant_ids = request.restaurantIds
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            # Parse dates
            start_date = datetime.strptime(request.startBusinessDate, "%Y%m%d").date()
            end_date = datetime.strptime(request.endBusinessDate, "%Y%m%d").date()
            
            # Get menu tags mapping
            logger.info("Fetching menu tags mapping...")
            tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
            
            # Request Analytics menu report with MENU_ITEM grouping
            logger.info("Requesting Analytics menu report with MENU_ITEM grouping...")
            report_guid = await request_toast_report(
                client,
                start_date,
                end_date,
                restaurant_ids,
                "menu",
                ["MENU_ITEM"]
            )
            
            # Fetch analytics data
            logger.info(f"Fetching report {report_guid}...")
            analytics_rows = await fetch_toast_report(client, report_guid, "menu")
            
            if not analytics_rows:
                return []
            
            # Aggregate by tags
            logger.info(f"Aggregating {len(analytics_rows)} rows by tags...")
            result = aggregate_quantities_by_tag(
                analytics_rows,
                tags_mapping,
                request.tagAttributionMode
            )
            
            # If not daily, sum across dates
            if not request.daily:
                aggregated = {}
                for row in result:
                    key = f"{row['restaurantGuid']}|{row['tagGuid']}"
                    if key not in aggregated:
                        aggregated[key] = {
                            "restaurantGuid": row["restaurantGuid"],
                            "restaurantName": row["restaurantName"],
                            "tagGuid": row["tagGuid"],
                            "tagName": row["tagName"],
                            "quantitySold": 0
                        }
                    aggregated[key]["quantitySold"] += row["quantitySold"]
                
                result = list(aggregated.values())
            
            # Sort by restaurant, tag name
            result.sort(key=lambda x: (x["restaurantName"], x["tagName"]))
            
            logger.info(f"Returning {len(result)} aggregated rows")
            return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in item tags quantities report: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating report: {str(e)}")

@app.post("/api/reports/pick3")
async def run_pick3_report(request: Pick3ReportRequest = Body(...)):
    """
    Pick 3 Report: per-store-per-day counts of (Pick 3 Entree) items and
    drill-down of (POS Pairing Choice) / (OLO Pairing Choice) modifier selections.
    Prompts only for time period; uses all locations when restaurantIds is empty.
    """
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            restaurant_ids = list(request.restaurantIds) if request.restaurantIds else []
            locations: List[Location] = []
            if not restaurant_ids:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            else:
                locations = await get_all_active_locations(client)
            if not restaurant_ids:
                return {"summary": [], "drillDown": []}
            guid_to_location = {loc.restaurantGuid: loc for loc in locations}

            try:
                start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
                end_date = datetime.strptime(request.end_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
            if start_date > end_date:
                raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

            days_diff = (end_date - start_date).days + 1
            pick3_entree_substring = "(Pick 3 Entree)"
            pairing_substrings = ("(POS Pairing Choice)", "(OLO Pairing Choice)")

            # --- Fetch MENU_ITEM report (chunked by week if needed) ---
            all_menu_rows: List[Dict] = []
            if days_diff > 7:
                current_start = start_date
                while current_start <= end_date:
                    chunk_end = min(current_start + timedelta(days=6), end_date)
                    report_guid = await request_toast_report(
                        client, current_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
                    )
                    chunk_rows = await fetch_toast_report(client, report_guid, "menu")
                    if chunk_rows:
                        all_menu_rows.extend(chunk_rows)
                    current_start = chunk_end + timedelta(days=1)
            else:
                report_guid = await request_toast_report(
                    client, start_date, end_date, restaurant_ids, "menu", ["MENU_ITEM"]
                )
                all_menu_rows = await fetch_toast_report(client, report_guid, "menu")

            # Filter by variant: POS = (Pick 3 Entree) only, OLO = Pick 3 (OLO) only
            variant = (request.variant or "pos").strip().lower()
            if variant not in ("pos", "olo"):
                variant = "pos"
            summary_agg: Dict[Tuple[str, str], Dict] = {}
            parent_items_agg: Dict[Tuple[str, str], Dict[str, int]] = {}  # (rguid, bdate) -> { menuItemName: qty }
            for row in all_menu_rows or []:
                name = (row.get("menuItemName") or "").strip()
                if variant == "olo":
                    menu_guid = (row.get("menuItemGuid") or "").strip().lower()
                    is_pick3 = "Pick 3 (OLO)" in name or menu_guid == PICK3_OLO_ITEM_GUID.lower()
                else:
                    is_pick3 = pick3_entree_substring in name
                if not is_pick3:
                    continue
                rguid = row.get("restaurantGuid", "")
                bdate = row.get("businessDate", "")
                qty = int(row.get("quantitySold", 0) or 0)
                if not rguid or not bdate:
                    continue
                key = (rguid, bdate)
                if key not in summary_agg:
                    summary_agg[key] = {
                        "restaurantGuid": rguid,
                        "restaurantName": row.get("restaurantName", "Unknown"),
                        "businessDate": bdate,
                        "pick3Total": 0,
                    }
                summary_agg[key]["pick3Total"] += qty
                if key not in parent_items_agg:
                    parent_items_agg[key] = {}
                parent_items_agg[key][name] = parent_items_agg[key].get(name, 0) + qty

            for r in summary_agg.values():
                if r["restaurantGuid"] in guid_to_location:
                    r["restaurantName"] = format_location_display_name(guid_to_location[r["restaurantGuid"]])
            summary = sorted(summary_agg.values(), key=lambda x: (x["businessDate"], x["restaurantName"]))
            summary_by_key = {(r["restaurantGuid"], r["businessDate"]): r for r in summary}
            # Debug: per-store Pick 3 totals from menu report (for comparison with drill-down)
            menu_totals_by_store: Dict[str, int] = {}
            for r in summary:
                g = r["restaurantGuid"]
                menu_totals_by_store[g] = menu_totals_by_store.get(g, 0) + r["pick3Total"]
            for g, total in sorted(menu_totals_by_store.items(), key=lambda x: -x[1]):
                name = next((r["restaurantName"] for r in summary if r["restaurantGuid"] == g), g[:8])
                logger.info("Pick3 menu report store total: guid=%s name=%s total=%s", g[:8] + "...", name, total)

            # --- Pairing selections: use menu report grouped by MENU_ITEM + MODIFIER so we only count
            #     pairings that were sold with a Pick 3 parent item (not all pairings across the menu). ---
            selections_agg: Dict[Tuple[str, str], Dict[str, int]] = {}
            all_item_mod_rows: List[Dict] = []
            group_by_item_modifier = ["MODIFIER"]
            if days_diff > 7:
                current_start = start_date
                while current_start <= end_date:
                    chunk_end = min(current_start + timedelta(days=6), end_date)
                    report_guid = await request_toast_report(
                        client, current_start, chunk_end, restaurant_ids, "menu", group_by_item_modifier
                    )
                    chunk_rows = await fetch_toast_report(client, report_guid, "menu")
                    if chunk_rows:
                        all_item_mod_rows.extend(chunk_rows)
                    current_start = chunk_end + timedelta(days=1)
            else:
                report_guid = await request_toast_report(
                    client, start_date, end_date, restaurant_ids, "menu", group_by_item_modifier
                )
                all_item_mod_rows = await fetch_toast_report(client, report_guid, "menu")
            for row in all_item_mod_rows or []:
                # Row has both menu item (parent) and modifier: menuItemGuid, menuItemName, modifierGuid/modifierOptionGuid, modifierName/modifierOptionName
                parent_name = (row.get("menuItemName") or "").strip()
                parent_guid = (row.get("menuItemGuid") or "").strip().lower()
                is_pick3_parent = (
                    (pick3_entree_substring in parent_name)
                    if variant != "olo"
                    else ("Pick 3 (OLO)" in parent_name or parent_guid == PICK3_OLO_ITEM_GUID.lower())
                )
                if not is_pick3_parent:
                    continue
                mod_name = (row.get("modifierName") or row.get("modifierOptionName") or "").strip()
                if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                    continue
                # POS report: only (POS Pairing Choice) modifiers; OLO report: only (OLO Pairing Choice) modifiers
                if variant == "olo":
                    if "(OLO Pairing Choice)" not in mod_name:
                        continue
                else:
                    if "(POS Pairing Choice)" not in mod_name:
                        continue
                rguid = row.get("restaurantGuid", "")
                bdate = row.get("businessDate", "")
                qty = int(row.get("quantitySold", 0) or 0)
                if not rguid or not bdate:
                    continue
                key = (rguid, bdate)
                if key not in selections_agg:
                    selections_agg[key] = {}
                selections_agg[key][mod_name] = selections_agg[key].get(mod_name, 0) + qty

            all_keys = set(summary_by_key.keys()) | set(selections_agg.keys())
            # Build drillDown: one entry per (store, date) with pick3Total, parentItems (Pick 3 Entree), and selections
            drill_down = []
            for (rguid, bdate) in sorted(all_keys):
                rec = summary_by_key.get((rguid, bdate))
                pick3_total = rec["pick3Total"] if rec else 0
                rname = rec["restaurantName"] if rec else "Unknown"
                parent_map = parent_items_agg.get((rguid, bdate), {})
                parent_items = [{"itemName": name, "quantity": qty} for name, qty in parent_map.items()]
                parent_items.sort(key=lambda x: (-x["quantity"], x["itemName"]))
                sel_map = selections_agg.get((rguid, bdate), {})
                selections = [{"itemName": name, "quantity": qty} for name, qty in sel_map.items()]
                selections.sort(key=lambda x: (-x["quantity"], x["itemName"]))
                drill_down.append({
                    "restaurantGuid": rguid,
                    "restaurantName": format_location_display_name(guid_to_location[rguid]) if rguid in guid_to_location else rname,
                    "businessDate": bdate,
                    "pick3Total": pick3_total,
                    "parentItems": parent_items,
                    "selections": selections,
                })

            return {
                "summary": summary,
                "drillDown": drill_down,
                "pairingSource": "menu_aggregate",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in Pick 3 report: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating Pick 3 report: {str(e)}")


PICK3_ENTREE_SUBSTRING = "(Pick 3 Entree)"
PICK3_OLO_ITEM_GUID = "e7adf8e6-5511-4e34-a45d-1a388996ad2f"  # Pick 3 (OLO) - separate report variant
PICK3_PAIRING_SUBSTRINGS = ("(POS Pairing Choice)", "(OLO Pairing Choice)")
# Premod prefixes: exclude modifiers like "ADD EXTRA NO XOTS" from Pick 3 pairing counts
PICK3_PREMOD_PREFIXES = ("add ", "no ", "extra ")


def _is_pick3_premod_name(mod_name: str) -> bool:
    """True if modifier name looks like a premod we exclude (e.g. ADD EXTRA NO XOTS)."""
    if not mod_name:
        return True
    upper = mod_name.strip().upper()
    return any(upper.startswith(p.upper()) for p in PICK3_PREMOD_PREFIXES)


def _is_pick3_selection(sel: Dict, variant: str) -> bool:
    """True if this order selection counts as Pick 3 for the given variant (pos or olo)."""
    if variant == "olo":
        display_name = (sel.get("displayName") or "").strip()
        if "Pick 3 (OLO)" in display_name:
            return True
        item = sel.get("item")
        guid = (item.get("guid") if isinstance(item, dict) else item) if item else ""
        return bool(guid and str(guid).strip().lower() == PICK3_OLO_ITEM_GUID.lower())
    return PICK3_ENTREE_SUBSTRING in (sel.get("displayName") or "")


async def _compute_pick3_drill_down_for_store(
    client: httpx.AsyncClient,
    rguid: str,
    start_date: date,
    end_date: date,
    variant: str = "pos",
) -> Dict:
    """
    Fetch orders for one store (date range), filter by businessDate in range,
    aggregate Pick 3 parent items and pairing selections. variant "pos" = (Pick 3 Entree) only, "olo" = Pick 3 (OLO) only.
    Returns dict with restaurantGuid, totalPick3Quantity, parentItems, selections (no restaurantName).
    """
    variant = (variant or "pos").strip().lower()
    if variant not in ("pos", "olo"):
        variant = "pos"
    start_bdate = int(to_business_date(start_date))
    end_bdate = int(to_business_date(end_date))
    await get_menu_tags_mapping(client, rguid)
    guid_to_name = _get_modifier_guid_to_name(rguid)
    orders = await _fetch_orders_for_restaurant_date_range(client, rguid, start_date, end_date)

    parent_totals: Dict[str, int] = {}
    pairing_totals: Dict[str, int] = {}

    for order in orders:
        obd = order.get("businessDate")
        try:
            order_bdate_int = int(obd) if obd is not None else None
        except (TypeError, ValueError):
            order_bdate_int = None
        if order_bdate_int is None or not (start_bdate <= order_bdate_int <= end_bdate):
            continue
        for check in order.get("checks") or []:
            for sel in check.get("selections") or []:
                if not _is_pick3_selection(sel, variant):
                    continue
                display_name = (sel.get("displayName") or "").strip()
                sel_qty = int(sel.get("quantity", 1) or 1)
                parent_totals[display_name] = parent_totals.get(display_name, 0) + sel_qty
                # Collect (mod_name, mod_qty) for this selection only; cap total at 3 pairings per Pick 3 entree
                sel_pairings: List[Tuple[str, int]] = []
                for mod in sel.get("modifiers") or []:
                    item_ref = mod.get("item")
                    if isinstance(item_ref, dict):
                        mod_guid = item_ref.get("guid")
                    elif isinstance(item_ref, str):
                        mod_guid = item_ref
                    else:
                        mod_guid = mod.get("guid")
                    if not mod_guid:
                        continue
                    mod_name = (guid_to_name.get(mod_guid) or "").strip()
                    if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                        continue
                    if variant == "olo":
                        if "(OLO Pairing Choice)" not in mod_name:
                            continue
                    else:
                        if "(POS Pairing Choice)" not in mod_name:
                            continue
                    mod_qty = int(mod.get("quantity", 1) or 1)
                    sel_pairings.append((mod_name, mod_qty))
                max_pairings_for_sel = sel_qty * 3
                total_sel_pairings = sum(q for _, q in sel_pairings)
                if total_sel_pairings > max_pairings_for_sel and max_pairings_for_sel > 0:
                    scale = max_pairings_for_sel / total_sel_pairings
                    for mod_name, mod_qty in sel_pairings:
                        add_qty = max(0, int(round(mod_qty * scale)))
                        if add_qty:
                            pairing_totals[mod_name] = pairing_totals.get(mod_name, 0) + add_qty
                else:
                    for mod_name, mod_qty in sel_pairings:
                        pairing_totals[mod_name] = pairing_totals.get(mod_name, 0) + mod_qty

    total_pick3 = sum(parent_totals.values()) or 0
    parent_items = [{"itemName": n, "quantity": q} for n, q in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))]
    selections = [{"itemName": n, "quantity": q} for n, q in sorted(pairing_totals.items(), key=lambda x: (-x[1], x[0]))]
    return {
        "restaurantGuid": rguid,
        "totalPick3Quantity": total_pick3,
        "parentItems": parent_items,
        "selections": selections,
    }


class Pick3DrillDownOrdersRequest(BaseModel):
    start_date: str
    end_date: str
    restaurant_guid: str
    variant: str = "pos"  # "pos" or "olo"
    debug: bool = False


@app.post("/api/reports/pick3/drill-down-orders")
async def pick3_drill_down_orders(request: Pick3DrillDownOrdersRequest = Body(...)):
    """
    On-demand: fetch order-level pairing counts for a single store only.
    Fetches orders by businessDate (one API call per day) so drill-down matches menu report.
    Set debug=true to return debug info (orders fetched, Pick 3 per order/businessDate).
    """
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    rguid = request.restaurant_guid.strip()
    if not rguid:
        raise HTTPException(status_code=400, detail="restaurant_guid required")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            start_iso = start_date.strftime("%Y-%m-%dT00:00:00.000-0600")
            end_iso = (end_date + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000-0600")
            variant = (request.variant or "pos").strip().lower()
            if variant not in ("pos", "olo"):
                variant = "pos"
            result = await _compute_pick3_drill_down_for_store(client, rguid, start_date, end_date, variant)
            locs = await get_all_active_locations(client)
            restaurant_name = "Unknown"
            for loc in locs:
                if loc.restaurantGuid == rguid:
                    restaurant_name = format_location_display_name(loc)
                    break
            out: Dict = {
                "pairingSource": "orders",
                "restaurantGuid": rguid,
                "restaurantName": restaurant_name,
                "totalPick3Quantity": result["totalPick3Quantity"],
                "parentItems": result["parentItems"],
                "selections": result["selections"],
            }
            if request.debug:
                orders = await _fetch_orders_for_restaurant_date_range(client, rguid, start_date, end_date)
                start_bdate = int(to_business_date(start_date))
                end_bdate = int(to_business_date(end_date))
                debug_per_order = []
                pick3_by_business_date: Dict[str, int] = {}
                for order in orders:
                    obd = order.get("businessDate")
                    try:
                        order_bdate_int = int(obd) if obd is not None else None
                    except (TypeError, ValueError):
                        order_bdate_int = None
                    if order_bdate_int is None or not (start_bdate <= order_bdate_int <= end_bdate):
                        continue
                    order_bdate = str(order_bdate_int)
                    order_pick3 = sum(
                        int(sel.get("quantity", 1) or 1)
                        for check in (order.get("checks") or [])
                        for sel in (check.get("selections") or [])
                        if _is_pick3_selection(sel, variant)
                    )
                    if order_pick3 > 0:
                        debug_per_order.append({
                            "orderGuid": (order.get("guid") or "")[:8],
                            "businessDate": order_bdate,
                            "pick3Count": order_pick3,
                        })
                        pick3_by_business_date[order_bdate] = pick3_by_business_date.get(order_bdate, 0) + order_pick3
                out["debug"] = {
                    "dateRangeRequested": {"start": start_iso, "end": end_iso},
                    "ordersFetched": len(orders),
                    "ordersWithPick3": len(debug_per_order),
                    "totalPick3FromOrders": result["totalPick3Quantity"],
                    "pick3ByBusinessDate": dict(sorted(pick3_by_business_date.items())),
                    "sampleOrdersWithPick3": debug_per_order[:20],
                }
            return out
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Pick 3 drill-down-orders: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class Pick3DrillDownAllStoresRequest(BaseModel):
    start_date: str
    end_date: str
    restaurant_ids: Optional[List[str]] = None  # if empty, all locations
    variant: str = "pos"  # "pos" or "olo"


@app.post("/api/reports/pick3/drill-down-all-stores")
async def pick3_drill_down_all_stores(request: Pick3DrillDownAllStoresRequest = Body(...)):
    """
    Fetch Pick 3 order-level breakdown for all stores in parallel (concurrency limited).
    Uses same logic as single-store drill-down; runs multiple stores at once so total
    time is much lower than sequential (e.g. ~2–4 min for 50 stores instead of 15+).
    """
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(request.end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            locs = await get_all_active_locations(client)
            if not locs:
                return {"stores": [], "pairingSource": "orders"}
            if request.restaurant_ids:
                guids = [g.strip() for g in request.restaurant_ids if g.strip()]
                locs = [loc for loc in locs if loc.restaurantGuid in guids]
            if not locs:
                return {"stores": [], "pairingSource": "orders"}

            store_guids = [loc.restaurantGuid for loc in locs]
            guid_to_name = {loc.restaurantGuid: format_location_display_name(loc) for loc in locs}

            variant = (request.variant or "pos").strip().lower()
            if variant not in ("pos", "olo"):
                variant = "pos"

            # Prefetch menu tags for all stores in parallel so each store task hits cache (faster)
            prefetch_sem = asyncio.Semaphore(10)
            async def prefetch_one(g: str):
                async with prefetch_sem:
                    await get_menu_tags_mapping(client, g)
            await asyncio.gather(*[prefetch_one(g) for g in store_guids], return_exceptions=True)

            sem = asyncio.Semaphore(8)

            async def one_store(rguid: str):
                async with sem:
                    try:
                        return await _compute_pick3_drill_down_for_store(client, rguid, start_date, end_date, variant)
                    except Exception as e:
                        logger.warning("Pick3 all-stores drill-down for %s: %s", rguid[:8], e)
                        return {"restaurantGuid": rguid, "totalPick3Quantity": 0, "parentItems": [], "selections": []}

            results = await asyncio.gather(*[one_store(g) for g in store_guids], return_exceptions=False)

            stores = []
            for r in results:
                stores.append({
                    "restaurantGuid": r["restaurantGuid"],
                    "restaurantName": guid_to_name.get(r["restaurantGuid"], "Unknown"),
                    "totalPick3Quantity": r["totalPick3Quantity"],
                    "parentItems": r["parentItems"],
                    "selections": r["selections"],
                })

            logger.info(
                "Pick3 drill-down all-stores: range=%s to %s stores=%s total_pick3=%s",
                request.start_date,
                request.end_date,
                len(stores),
                sum(s["totalPick3Quantity"] for s in stores),
            )
            return {"stores": stores, "pairingSource": "orders"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Pick 3 drill-down-all-stores: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reports/pick3/from-csv")
async def pick3_breakdown_from_csv(
    file: Optional[UploadFile] = File(None, description="Toast Product Mix Items.csv"),
    modifiers_file: Optional[UploadFile] = File(None, description="Toast Product Mix Modifiers.csv"),
    selected_levels_file: Optional[UploadFile] = File(None, description="Toast Product Mix Selected_levels.csv (recommended for correct pairing counts)"),
    variant: str = Query("pos", description="pos = (Pick 3 Entree), olo = Pick 3 (OLO), all = both"),
):
    """
    Parse Toast Product Mix CSVs. Upload one or more files:
    - Selected_levels.csv (recommended): uses Type='modifier' for pairing rows; Item = Pick 3 parent, Modifier = pairing choice. Correct parent–child link.
    - Items.csv + optional Modifiers.csv: parent totals from Items; pairings from Modifiers where parent is Pick 3.
    If Selected_levels.csv is provided, it is used for both parent and pairing counts (Items/Modifiers ignored for pairings).
    """
    variant = (variant or "pos").strip().lower()
    if variant not in ("pos", "olo", "all"):
        variant = "pos"

    has_selected_levels = (
        selected_levels_file
        and selected_levels_file.filename
        and selected_levels_file.filename.lower().endswith(".csv")
    )
    has_items = file and file.filename and file.filename.lower().endswith(".csv")
    if not has_selected_levels and not has_items:
        raise HTTPException(
            status_code=400,
            detail="Upload at least one file: Selected_levels.csv (recommended) or Items.csv.",
        )

    def parse_qty(s: str) -> float:
        if not s:
            return 0.0
        s = str(s).strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return 0.0

    # Prefer Selected_levels.csv when provided (correct parent–child link: Type='modifier' for pairing rows)
    if has_selected_levels:
        try:
            raw_sl = await selected_levels_file.read()
            if len(raw_sl) > 20 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Selected_levels file too large (max 20MB).")
            text_sl = raw_sl.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Selected_levels must be UTF-8 encoded.")
        reader_sl = csv.reader(io.StringIO(text_sl))
        rows_sl = list(reader_sl)
        if not rows_sl:
            raise HTTPException(status_code=400, detail="Selected_levels CSV is empty.")
        header_sl = [c.strip() for c in rows_sl[0]]
        col_type = col_item = col_modifier = col_qty = None
        for i, h in enumerate(header_sl):
            if not h:
                continue
            h_lower = h.lower().strip()
            if col_type is None and h_lower in ("type", "level", "level type", "row type"):
                col_type = i
            if col_item is None and h_lower in ("item", "product", "menu item", "item name"):
                col_item = i
            if col_modifier is None and ("modifier" in h_lower or h_lower in ("option", "modifier name")):
                col_modifier = i
            if col_qty is None and "voided" not in h_lower:
                if h_lower in ("qty sold", "quantity sold", "quantity", "qty", "units", "sold", "unit sold") or ("qty" in h_lower and "sold" in h_lower) or "quantity" in h_lower:
                    col_qty = i
        if col_item is None:
            col_item = next((i for i, x in enumerate(header_sl) if x and x.lower().strip() == "item"), None)
        if col_modifier is None:
            col_modifier = next((i for i, x in enumerate(header_sl) if x and "modifier" in x.lower()), None)
        if col_qty is None:
            col_qty = next((i for i, x in enumerate(header_sl) if x and ("qty" in x.lower() or "quantity" in x.lower()) and "voided" not in x.lower()), None)
        missing = []
        if col_type is None:
            missing.append("Type (or Level)")
        if col_item is None:
            missing.append("Item")
        if col_modifier is None:
            missing.append("Modifier")
        if col_qty is None:
            missing.append("Qty sold / Quantity")
        if missing:
            raise HTTPException(
                status_code=400,
                detail="Selected_levels.csv missing: {}. Found columns: {}.".format(", ".join(missing), ", ".join(header_sl[:20])),
            )

        def _is_pick3_parent(item_name: str) -> bool:
            if not item_name:
                return False
            if variant == "olo":
                return "Pick 3 (OLO)" in item_name
            if variant == "pos":
                return "(Pick 3 Entree)" in item_name
            return "(Pick 3 Entree)" in item_name or "Pick 3 (OLO)" in item_name

        def _is_valid_pairing(mod_name: str) -> bool:
            if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                return False
            if variant == "olo":
                return "(OLO Pairing Choice)" in mod_name
            if variant == "pos":
                return "(POS Pairing Choice)" in mod_name
            return "(POS Pairing Choice)" in mod_name or "(OLO Pairing Choice)" in mod_name

        parent_totals_sl: Dict[str, float] = {}
        pairing_totals_sl: Dict[str, float] = {}
        for row in rows_sl[1:]:
            if len(row) <= max(col_type, col_item, col_modifier, col_qty):
                continue
            row_type = (row[col_type] or "").strip().lower()
            item_name = (row[col_item] or "").strip()
            mod_name = (row[col_modifier] or "").strip() if col_modifier < len(row) else ""
            qty = parse_qty(row[col_qty] if col_qty < len(row) else "")
            if row_type == "modifier":
                if not _is_pick3_parent(item_name) or not _is_valid_pairing(mod_name) or qty <= 0:
                    continue
                pairing_totals_sl[mod_name] = pairing_totals_sl.get(mod_name, 0.0) + qty
            else:
                if not _is_pick3_parent(item_name) or qty <= 0:
                    continue
                parent_totals_sl[item_name] = parent_totals_sl.get(item_name, 0.0) + qty
        total_pick3_sl = sum(parent_totals_sl.values())
        max_pairings_sl = 3.0 * total_pick3_sl if total_pick3_sl else 0
        total_pairings_raw_sl = sum(pairing_totals_sl.values())
        if max_pairings_sl > 0 and total_pairings_raw_sl > max_pairings_sl:
            scale = max_pairings_sl / total_pairings_raw_sl
            pairing_totals_sl = {n: qty * scale for n, qty in pairing_totals_sl.items()}
        parent_items_sl = [
            {"itemName": name, "quantity": int(round(qty))}
            for name, qty in sorted(parent_totals_sl.items(), key=lambda x: (-x[1], x[0]))
        ]
        selections_sl = [
            {"itemName": name, "quantity": int(round(qty))}
            for name, qty in sorted(pairing_totals_sl.items(), key=lambda x: (-x[1], x[0]))
        ]
        return {
            "pairingSource": "toast_product_mix_selected_levels",
            "totalPick3Quantity": int(round(total_pick3_sl)),
            "parentItems": parent_items_sl,
            "selections": selections_sl,
            "restaurantName": "All locations (from CSV)",
        }

    # Items.csv (+ optional Modifiers.csv) path
    if not has_items:
        raise HTTPException(status_code=400, detail="When not using Selected_levels.csv, Items.csv is required.")
    try:
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 20MB).")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded.")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="Items CSV is empty.")

    header = [c.strip() for c in rows[0]]
    col_master = col_parent = col_item = col_qty = None
    for i, h in enumerate(header):
        if h and h.lower() == "masterid":
            col_master = i
        if h and h.lower() == "parentid":
            col_parent = i
        if h and h.lower() == "item":
            col_item = i
        if h and h.lower().strip() == "qty sold":
            col_qty = i
        elif h and col_qty is None and "qty" in h.lower() and "sold" in h.lower() and "voided" not in h.lower():
            col_qty = i
    if col_item is None or col_qty is None:
        raise HTTPException(
            status_code=400,
            detail="Items.csv must have 'Item' and 'Qty sold' columns (Toast Product Mix Items.csv).",
        )

    def is_pick3_row(item_name: str) -> bool:
        if not item_name:
            return False
        if variant == "pos":
            return "(Pick 3 Entree)" in item_name
        if variant == "olo":
            return "Pick 3 (OLO)" in item_name
        return "(Pick 3 Entree)" in item_name or "Pick 3 (OLO)" in item_name

    parent_totals: Dict[str, float] = {}
    pick3_parent_ids: set = set()
    pick3_master_ids: set = set()
    item_parent_to_masters: Dict[str, set] = {}

    for row in rows[1:]:
        if len(row) <= max(col_item, col_qty):
            continue
        item_name = (row[col_item] or "").strip()
        master_id = (row[col_master] or "").strip() if col_master is not None and len(row) > col_master else ""
        parent_id = (row[col_parent] or "").strip() if col_parent is not None and len(row) > col_parent else ""
        if parent_id:
            item_parent_to_masters.setdefault(parent_id, set()).add(master_id)
        if not is_pick3_row(item_name):
            continue
        qty = parse_qty(row[col_qty])
        if qty <= 0:
            continue
        parent_totals[item_name] = parent_totals.get(item_name, 0.0) + qty
        if parent_id:
            pick3_parent_ids.add(parent_id)
        if master_id:
            pick3_master_ids.add(master_id)

    # Only count pairings attached to Pick 3 items (master id), not the whole product family
    pick3_master_ids_only: set = set(pick3_master_ids)

    selections: List[Dict] = []
    if modifiers_file and modifiers_file.filename and modifiers_file.filename.lower().endswith(".csv"):
        try:
            mod_raw = await modifiers_file.read()
            if len(mod_raw) > 20 * 1024 * 1024:
                pass
            else:
                mod_text = mod_raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
                mod_reader = csv.reader(io.StringIO(mod_text))
                mod_rows = list(mod_reader)
                if mod_rows:
                    mh = [c.strip() for c in mod_rows[0]]
                    m_col_parent = m_col_mod = m_col_cat = m_col_qty = None
                    for i, c in enumerate(mh):
                        if c and c.lower() == "parentid":
                            m_col_parent = i
                        if c and c.lower() == "modifier":
                            m_col_mod = i
                        if c and "sales" in c.lower() and "category" in c.lower():
                            m_col_cat = i
                        if c and c.lower().strip() == "qty sold":
                            m_col_qty = i
                        elif c and m_col_qty is None and "qty" in c.lower() and "sold" in c.lower() and "voided" not in c.lower():
                            m_col_qty = i
                    if m_col_parent is not None and m_col_mod is not None and m_col_qty is not None:
                        pairing_totals: Dict[str, float] = {}
                        for mrow in mod_rows[1:]:
                            if len(mrow) <= max(m_col_parent, m_col_mod, m_col_qty):
                                continue
                            pid = (mrow[m_col_parent] or "").strip()
                            if pid not in pick3_master_ids_only:
                                continue
                            mod_name = (mrow[m_col_mod] or "").strip()
                            if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                                continue
                            if variant == "olo":
                                if "(OLO Pairing Choice)" not in mod_name:
                                    continue
                            elif variant == "pos":
                                if "(POS Pairing Choice)" not in mod_name:
                                    continue
                            else:
                                if "(POS Pairing Choice)" not in mod_name and "(OLO Pairing Choice)" not in mod_name:
                                    continue
                            qty = parse_qty(mrow[m_col_qty])
                            if qty <= 0:
                                continue
                            pairing_totals[mod_name] = pairing_totals.get(mod_name, 0.0) + qty
                        total_pick3_csv = sum(parent_totals.values())
                        total_pairings_raw = sum(pairing_totals.values())
                        max_pairings = 3.0 * total_pick3_csv if total_pick3_csv else 0
                        if max_pairings > 0 and total_pairings_raw > max_pairings:
                            scale = max_pairings / total_pairings_raw
                            pairing_totals = {n: qty * scale for n, qty in pairing_totals.items()}
                        selections = [
                            {"itemName": name, "quantity": int(round(qty))}
                            for name, qty in sorted(pairing_totals.items(), key=lambda x: (-x[1], x[0]))
                        ]
        except Exception:
            pass

    total_pick3 = sum(parent_totals.values())
    parent_items = [
        {"itemName": name, "quantity": int(round(qty))}
        for name, qty in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))
    ]
    return {
        "pairingSource": "toast_product_mix_csv",
        "totalPick3Quantity": int(round(total_pick3)),
        "parentItems": parent_items,
        "selections": selections,
        "restaurantName": "All locations (from CSV)",
    }


@app.post("/api/reports/pick3/from-selected-levels")
async def pick3_breakdown_from_selected_levels(
    file: UploadFile = File(..., description="Toast Product Mix Selected_levels.csv"),
    variant: str = Query("pos", description="pos = (Pick 3 Entree), olo = Pick 3 (OLO), all = both"),
):
    """
    Parse Toast Product Mix Selected_levels.csv for Pick 3 entree + pairing counts.
    The parent-child link lives in this file: use Type == 'modifier' to isolate pairing rows.
    - POS: Item contains 'Pick 3 Entree' + Modifier contains 'POS Pairing Choice'
    - OLO: Item contains 'Pick 3 (OLO)' + Modifier contains 'OLO Pairing Choice'
    Excludes (Side) and premods (ADD/NO/EXTRA). Returns same shape as from-csv.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload Selected_levels.csv.")
    variant = (variant or "pos").strip().lower()
    if variant not in ("pos", "olo", "all"):
        variant = "pos"

    def parse_qty(s: str) -> float:
        if not s:
            return 0.0
        s = str(s).strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return 0.0

    try:
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 20MB).")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded.")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="Selected_levels CSV is empty.")

    header = [c.strip() for c in rows[0]]
    col_type = col_item = col_modifier = col_qty = None
    for i, h in enumerate(header):
        if not h:
            continue
        h_lower = h.lower().strip()
        if col_type is None and h_lower in ("type", "level", "level type", "row type"):
            col_type = i
        if col_item is None and h_lower in ("item", "product", "menu item", "item name"):
            col_item = i
        if col_modifier is None and ("modifier" in h_lower or h_lower in ("option", "modifier name")):
            col_modifier = i
        if col_qty is None and "voided" not in h_lower:
            if h_lower in ("qty sold", "quantity sold", "quantity", "qty", "units", "sold", "unit sold") or ("qty" in h_lower and "sold" in h_lower) or "quantity" in h_lower:
                col_qty = i
    if col_item is None:
        col_item = next((i for i, x in enumerate(header) if x and x.lower().strip() == "item"), None)
    if col_modifier is None:
        col_modifier = next((i for i, x in enumerate(header) if x and "modifier" in x.lower()), None)
    if col_qty is None:
        col_qty = next((i for i, x in enumerate(header) if x and ("qty" in x.lower() or "quantity" in x.lower()) and "voided" not in x.lower()), None)
    missing = []
    if col_type is None:
        missing.append("Type (or Level)")
    if col_item is None:
        missing.append("Item")
    if col_modifier is None:
        missing.append("Modifier")
    if col_qty is None:
        missing.append("Qty sold / Quantity")
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Selected_levels.csv missing: {}. Found columns: {}.".format(", ".join(missing), ", ".join(header[:20])),
        )

    def is_pick3_parent(item_name: str) -> bool:
        if not item_name:
            return False
        if variant == "olo":
            return "Pick 3 (OLO)" in item_name
        if variant == "pos":
            return "(Pick 3 Entree)" in item_name
        return "(Pick 3 Entree)" in item_name or "Pick 3 (OLO)" in item_name

    def is_valid_pairing_modifier(mod_name: str) -> bool:
        if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
            return False
        if variant == "olo":
            return "(OLO Pairing Choice)" in mod_name
        if variant == "pos":
            return "(POS Pairing Choice)" in mod_name
        return "(POS Pairing Choice)" in mod_name or "(OLO Pairing Choice)" in mod_name

    parent_totals: Dict[str, float] = {}
    pairing_totals: Dict[str, float] = {}

    for row in rows[1:]:
        if len(row) <= max(col_type, col_item, col_modifier, col_qty):
            continue
        row_type = (row[col_type] or "").strip().lower()
        item_name = (row[col_item] or "").strip()
        modifier_name = (row[col_modifier] or "").strip() if col_modifier < len(row) else ""
        qty = parse_qty(row[col_qty] if col_qty < len(row) else "")

        if row_type == "modifier":
            if not is_pick3_parent(item_name) or not is_valid_pairing_modifier(modifier_name) or qty <= 0:
                continue
            pairing_totals[modifier_name] = pairing_totals.get(modifier_name, 0.0) + qty
        else:
            if not is_pick3_parent(item_name) or qty <= 0:
                continue
            parent_totals[item_name] = parent_totals.get(item_name, 0.0) + qty

    total_pick3 = sum(parent_totals.values())
    max_pairings = 3.0 * total_pick3 if total_pick3 else 0
    total_pairings_raw = sum(pairing_totals.values())
    if max_pairings > 0 and total_pairings_raw > max_pairings:
        scale = max_pairings / total_pairings_raw
        pairing_totals = {n: qty * scale for n, qty in pairing_totals.items()}

    parent_items = [
        {"itemName": name, "quantity": int(round(qty))}
        for name, qty in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))
    ]
    selections = [
        {"itemName": name, "quantity": int(round(qty))}
        for name, qty in sorted(pairing_totals.items(), key=lambda x: (-x[1], x[0]))
    ]

    return {
        "pairingSource": "toast_product_mix_selected_levels",
        "totalPick3Quantity": int(round(total_pick3)),
        "parentItems": parent_items,
        "selections": selections,
        "restaurantName": "All locations (from Selected_levels)",
    }


# ------------------
# Menus Sync API
# ------------------

@app.get("/menus")
async def get_menus(refresh: bool = Query(False), restaurant_guid: Optional[str] = Query(None)):
    """Fetch full Toast menu data (cached for 5 minutes by default)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        if not restaurant_guid:
            locations = await get_all_active_locations(client)
            if not locations:
                raise HTTPException(status_code=404, detail="No active locations found")
            restaurant_guid = locations[0].restaurantGuid

        return await fetch_toast_menus(client, restaurant_guid, force_refresh=refresh)


@app.get("/api/toast-pricing-export")
async def toast_pricing_export(
    refresh: bool = Query(False),
    restaurant_guid: Optional[str] = Query(None),
    per_location: bool = Query(True),
    group_by_tier: bool = Query(False),
    diagnostics: bool = Query(False),
    diagnostics_item: Optional[str] = Query(None),
    location_limit: Optional[int] = Query(None, ge=1)
):
    """Export menu pricing with location-specific prices as a CSV download."""
    if group_by_tier:
        tiers = load_price_tier_mapping()
        if not tiers:
            raise HTTPException(status_code=400, detail="Price tier mapping file is empty.")
        async with httpx.AsyncClient(timeout=90.0) as client:
            locations = await get_all_active_locations(client)
            tier_locations, tier_missing = build_tier_location_map(tiers, locations)
            duplicate_store_numbers = compute_store_tier_duplicates(tiers)
            store_to_locations: Dict[str, List[Location]] = {}
            for loc in locations:
                for store_num in extract_store_numbers_from_location(loc):
                    store_to_locations.setdefault(store_num, []).append(loc)
            needed_locations: Dict[str, Location] = {}
            for locs in tier_locations.values():
                for loc in locs:
                    needed_locations[loc.restaurantGuid] = loc
            locations_to_fetch = list(needed_locations.values())
            if not locations_to_fetch:
                raise HTTPException(status_code=400, detail="No locations matched the tier mapping file.")

            base_menu_data = await fetch_toast_menus_v2(
                client,
                locations_to_fetch[0].restaurantGuid,
                force_refresh=refresh
            )
            base_rows = _collect_menu_items(base_menu_data)

            location_price_maps: Dict[str, Dict[str, Optional[float]]] = {}
            for idx, loc in enumerate(locations_to_fetch):
                menu_data_loc = await fetch_toast_menus_v2(
                    client,
                    loc.restaurantGuid,
                    force_refresh=refresh
                )
                price_map: Dict[str, Optional[float]] = {}
                for menu in menu_data_loc.get("menus", []):
                    for group in menu.get("menuGroups", []):
                        stack = [group]
                        while stack:
                            current = stack.pop()
                            for item in current.get("menuItems", []):
                                guid = item.get("guid")
                                if not guid:
                                    continue
                                price_map[guid] = item.get("price")
                            stack.extend(current.get("menuGroups") or [])
                location_price_maps[loc.restaurantGuid] = price_map

                if idx < len(locations_to_fetch) - 1:
                    await asyncio.sleep(1.1)

            if diagnostics:
                item_match, item_matches = find_item_match(base_rows, diagnostics_item or "")
                item_trace = None
                if item_match:
                    guid = item_match.get("guid")
                    tier_details = []
                    for tier_name, _ in tiers:
                        locs = tier_locations.get(tier_name, [])
                        entries = []
                        for loc in locs:
                            price = location_price_maps.get(loc.restaurantGuid, {}).get(guid)
                            store_nums = extract_store_numbers_from_location(loc)
                            entries.append({
                                "restaurantGuid": loc.restaurantGuid,
                                "restaurantName": loc.restaurantName,
                                "storeNumbers": store_nums,
                                "price": price
                            })
                        prices = [entry["price"] for entry in entries if entry["price"] is not None]
                        tier_details.append({
                            "tierName": tier_name,
                            "locationCount": len(locs),
                            "uniquePrices": sorted({round(float(p), 2) for p in prices}) if prices else [],
                            "locations": entries
                        })
                    item_trace = {
                        "itemName": item_match.get("item"),
                        "itemGuid": item_match.get("guid"),
                        "matchCount": len(item_matches),
                        "matches": [{"itemName": row.get("item"), "itemGuid": row.get("guid")} for row in item_matches],
                        "tiers": tier_details
                    }
                diagnostics_payload = {
                    "tierCount": len(tiers),
                    "locationCount": len(locations),
                    "matchedLocationCount": len(locations_to_fetch),
                    "duplicateStoreNumbers": duplicate_store_numbers,
                    "unmatchedStoreNumbers": tier_missing,
                    "itemTrace": item_trace
                }
                return diagnostics_payload

            output = io.StringIO()
            writer = csv.writer(output)
            tier_names = [tier_name for tier_name, _ in tiers]
            writer.writerow(["Menu", "Menu Group", "Item", *tier_names])
            for row in base_rows:
                guid = row.get("guid")
                if not guid:
                    continue
                tier_prices = []
                for tier_name in tier_names:
                    locs = tier_locations.get(tier_name, [])
                    prices = [location_price_maps.get(loc.restaurantGuid, {}).get(guid) for loc in locs]
                    tier_prices.append(format_tier_price(prices))
                writer.writerow([
                    row.get("menu", ""),
                    row.get("menu_group", ""),
                    row.get("item", ""),
                    *tier_prices
                ])

            filename = f"toast_pricing_export_{datetime.now().strftime('%Y-%m-%d')}.csv"
            bom = "\ufeff"
            return Response(
                content=bom + output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )

    if per_location:
        async with httpx.AsyncClient(timeout=90.0) as client:
            locations = await get_all_active_locations(client)
            if location_limit:
                locations = locations[:location_limit]
            if not locations:
                raise HTTPException(status_code=404, detail="No active locations found")

            base_menu_data = await fetch_toast_menus_v2(
                client,
                locations[0].restaurantGuid,
                force_refresh=refresh
            )
            base_rows = _collect_menu_items(base_menu_data)

            location_price_maps: Dict[str, Dict[str, Optional[float]]] = {}
            for idx, loc in enumerate(locations):
                menu_data_loc = await fetch_toast_menus_v2(
                    client,
                    loc.restaurantGuid,
                    force_refresh=refresh
                )
                price_map = {}
                # Build price map from menu data
                for menu in menu_data_loc.get("menus", []):
                    for group in menu.get("menuGroups", []):
                        stack = [group]
                        while stack:
                            current = stack.pop()
                            for item in current.get("menuItems", []):
                                guid = item.get("guid")
                                if not guid:
                                    continue
                                price_map[guid] = item.get("price")
                            stack.extend(current.get("menuGroups") or [])
                location_price_maps[loc.restaurantName] = price_map

                if idx < len(locations) - 1:
                    await asyncio.sleep(1.1)

            output = io.StringIO()
            writer = csv.writer(output)
            header = ["Menu", "Menu Group", "Item", *[loc.restaurantName for loc in locations]]
            writer.writerow(header)
            for row in base_rows:
                guid = row.get("guid")
                if not guid:
                    continue
                writer.writerow([
                    row.get("menu", ""),
                    row.get("menu_group", ""),
                    row.get("item", ""),
                    *[
                        _format_price_value(location_price_maps[loc.restaurantName].get(guid))
                        for loc in locations
                    ]
                ])

            filename = f"toast_pricing_export_{datetime.now().strftime('%Y-%m-%d')}.csv"
            bom = "\ufeff"
            return Response(
                content=bom + output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )

    async with httpx.AsyncClient(timeout=60.0) as client:
        if not restaurant_guid:
            locations = await get_all_active_locations(client)
            if not locations:
                raise HTTPException(status_code=404, detail="No active locations found")
            restaurant_guid = locations[0].restaurantGuid

        location_name_by_guid = {}
        price_group_name_by_guid = {}
        try:
            locations = await get_all_active_locations(client)
            location_name_by_guid = {
                loc.restaurantGuid: loc.restaurantName
                for loc in locations
            }
        except Exception:
            location_name_by_guid = {}
        try:
            price_group_name_by_guid = await fetch_price_groups(client, restaurant_guid, force_refresh=refresh)
        except Exception:
            price_group_name_by_guid = {}

        menu_data = await fetch_toast_menus(client, restaurant_guid, force_refresh=refresh)

    rows = []
    all_location_columns = set()
    has_base_price = False
    for menu in menu_data.get("menus", []):
        menu_name = menu.get("name", "Unnamed Menu")
        for group in menu.get("menuGroups", []):
            group_name = group.get("name", "Unnamed Group")
            for item in group.get("menuItems", []):
                item_name = item.get("displayName") or item.get("name") or "Unnamed Item"
                base_price = item.get("price")
                if base_price is not None:
                    has_base_price = True
                location_prices = _extract_location_prices(
                    item,
                    location_name_by_guid=location_name_by_guid,
                    price_group_name_by_guid=price_group_name_by_guid
                )
                if location_prices:
                    all_location_columns.update(location_prices.keys())
                rows.append({
                    "menu": menu_name,
                    "menu_group": group_name,
                    "item": item_name,
                    "base_price": base_price,
                    "location_prices": location_prices
                })

    location_columns = sorted(
        [name for name in all_location_columns if name],
        key=lambda value: (0 if str(value).startswith("*") else 1, str(value).lower())
    )
    include_base_price = has_base_price or not location_columns

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Menu", "Menu Group", "Item", *location_columns]
    if include_base_price:
        header.append("Base Price")
    writer.writerow(header)
    for row in rows:
        values = [
            row["menu"],
            row["menu_group"],
            row["item"]
        ]
        location_prices = row.get("location_prices", {}) or {}
        for location_name in location_columns:
            values.append(_format_price_value(location_prices.get(location_name)))
        if include_base_price:
            values.append(_format_price_value(row.get("base_price")))
        writer.writerow(values)

    filename = f"toast_pricing_export_{datetime.now().strftime('%Y-%m-%d')}.csv"
    bom = "\ufeff"
    return Response(
        content=bom + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def _collect_pricing_debug_fields(item: Dict) -> Dict:
    keys_of_interest = [
        "price",
        "pricingStrategy",
        "pricingRules",
        "locationPrices",
        "locationSpecificPrices",
        "pricesByLocation",
        "locationPriceOverrides",
        "locationPriceMap",
        "priceGroups",
        "priceGroupPrices",
        "pricesByPriceGroup",
        "priceByPriceGroup",
        "priceGroupsByGuid"
    ]
    debug = {key: item.get(key) for key in keys_of_interest if key in item}
    if not debug:
        # Fallback: include any price-related keys
        debug = {k: item.get(k) for k in item.keys() if "price" in k.lower()}
    return debug


@app.get("/api/toast-pricing-debug")
async def toast_pricing_debug(
    item_name: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=20),
    refresh: bool = Query(False),
    restaurant_guid: Optional[str] = Query(None)
):
    """Return sample menu items with pricing-related fields for debugging."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        if not restaurant_guid:
            locations = await get_all_active_locations(client)
            if not locations:
                raise HTTPException(status_code=404, detail="No active locations found")
            restaurant_guid = locations[0].restaurantGuid

        menu_data = await fetch_toast_menus(client, restaurant_guid, force_refresh=refresh)

    matches = []
    name_filter = (item_name or "").strip().lower()
    for menu in menu_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_display = item.get("displayName") or item.get("name") or ""
                if name_filter and name_filter not in item_display.lower():
                    continue
                debug_fields = _collect_pricing_debug_fields(item)
                if not debug_fields and not name_filter:
                    continue
                matches.append({
                    "menu": menu.get("name"),
                    "menuGroup": group.get("name"),
                    "itemName": item_display,
                    "guid": item.get("guid"),
                    "pricingFields": debug_fields
                })
                if len(matches) >= limit:
                    return {"count": len(matches), "items": matches}

    return {"count": len(matches), "items": matches}


# ------------------
# Menu Items & Tags API
# ------------------

# In-memory cache for the /api/menu-items endpoint. The Toast Menus v2
# endpoint is slow (returns the whole menu hierarchy) and the response
# rarely changes — caching for an hour cuts the "Filter By: Item Tags"
# loading time from many seconds to milliseconds for all users after the
# first hit. Keyed by restaurant_guid ("__default__" when caller didn't
# specify one).
_menu_items_cache: Dict[str, Dict[str, Any]] = {}
_MENU_ITEMS_CACHE_TTL_S = 3600.0  # 1 hour — bump if menu tag edits feel stale


@app.get("/api/menu-items")
async def get_menu_items(restaurant_guid: Optional[str] = Query(None)):
    """Get all menu items, modifiers, and nested modifiers with their tags"""
    cache_key = restaurant_guid or "__default__"
    cached = _menu_items_cache.get(cache_key)
    if cached:
        age = time.time() - cached.get("_cached_at", 0)
        if age < _MENU_ITEMS_CACHE_TTL_S:
            # Strip the internal _cached_at field before returning.
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if not restaurant_guid:
                locations = await get_all_active_locations(client)
                if not locations:
                    raise HTTPException(status_code=404, detail="No active locations found")
                restaurant_guid = locations[0].restaurantGuid

            standard_headers = await get_standard_api_auth_headers()
            menus_v2_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
            headers_with_guid = {
                **standard_headers,
                "Toast-Restaurant-External-ID": restaurant_guid
            }

            resp = await client.get(menus_v2_url, headers=headers_with_guid)

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Error fetching menu data")

            menu_data = resp.json()

            items_with_tags = []
            all_tags = set()

            mod_group_refs = menu_data.get("modifierGroupReferences", {})
            mod_option_refs = menu_data.get("modifierOptionReferences", {})

            for menu in menu_data.get("menus", []):
                for group in menu.get("menuGroups", []):
                    for item in group.get("menuItems", []):
                        item_tags = [tag.get("name") for tag in item.get("itemTags", []) if tag.get("name")]

                        if item_tags:
                            all_tags.update(item_tags)
                            items_with_tags.append({
                                "guid": item.get("guid"),
                                "name": item.get("name"),
                                "type": "item",
                                "tags": item_tags
                            })

            result = {
                "items": items_with_tags,
                "allTags": sorted(list(all_tags))
            }
            _menu_items_cache[cache_key] = {**result, "_cached_at": time.time()}
            return result

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in get_menu_items: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error fetching menu items: {str(e)}")

# ------------------
# Report Types Info
# ------------------

@app.get("/api/report-types")
async def get_report_types():
    """Get available report types and their options"""
    return {
        "reportTypes": [
            {
                "id": "sales_summary",
                "name": "Sales Summary",
                "description": "Orders, net sales, avg ticket, discounts, voids, cash & card by location",
                "groupByOptions": []
            },
            {
                "id": "metrics",
                "name": "Net Sales Report",
                "description": "Toast vs Newk's net sales by date and location",
                "groupByOptions": [
                    {"id": "", "name": "No grouping", "description": "Total by date and location only"},
                    {"id": "REVENUE_CENTER", "name": "Revenue Center", "description": "Group by revenue center"},
                    {"id": "DINING_OPTION", "name": "Dining Option", "description": "Group by dining option (Dine In, Takeout, etc.)"},
                    {"id": "EMPLOYEE", "name": "Employee", "description": "Group by employee"},
                    {"id": "JOB", "name": "Job", "description": "Group by job role"},
                ]
            },
            {
                "id": "menu",
                "name": "Menu Performance",
                "description": "Sales by menu item",
                "groupByOptions": [
                    {"id": "MENU_ITEM", "name": "Menu Item", "description": "Group by menu item"}
                ],
                "supportsItemTagFilters": True
            },
            {
                "id": "guest_count",
                "name": "Guest Count",
                "description": "Calculate guest count from item quantities x Guest Count tag values",
                "groupByOptions": []
            },
            {
                "id": "royalty",
                "name": "Royalty Report",
                "description": "Newk's net sales with Royalty 5% and MKT Fund 1.75% by location",
                "groupByOptions": []
            },
            {
                "id": "tip_pool",
                "name": "Payroll Report",
                "description": "Weekly payroll: total tips \u00f7 total hours = per-employee share",
                "groupByOptions": []
            }
        ],
        "dateRangeTypes": [
            {"id": "last_7_days", "name": "Last 7 Days"},
            {"id": "last_30_days", "name": "Last 30 Days"},
            {"id": "last_90_days", "name": "Last 90 Days"},
            {"id": "custom", "name": "Custom Range"}
        ]
    }

# ------------------
# ------------------
# Aloha Config APIs (Location Mapping + Order Mode Mapping)
# ------------------

@app.get("/api/aloha/location-mapping")
async def get_location_mapping_ui():
    """Return Toast restaurants with their mapped Aloha store IDs."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            locations = await get_all_active_locations(client)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not fetch Toast locations: {exc}")

    loc_map = get_aloha_location_mapping()
    rows = []
    for loc in locations:
        rows.append({
            "restaurantGuid": loc.restaurantGuid,
            "restaurantName": loc.restaurantName,
            "externalId": loc.externalId or "",
            "alohaStoreId": loc_map.get(loc.restaurantGuid, ""),
        })
    rows.sort(key=lambda r: r["restaurantName"])
    mapped_count = sum(1 for r in rows if r["alohaStoreId"])
    return {"rows": rows, "total": len(rows), "mapped": mapped_count}


@app.put("/api/aloha/location-mapping")
async def save_location_mapping_ui(body: dict = Body(...)):
    """Save the Toast GUID -> Aloha store ID mapping."""
    mapping = body.get("mapping", {})
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="'mapping' must be an object {guid: storeId}")
    cleaned: Dict[str, str] = {}
    for guid, store_id in mapping.items():
        sid = str(store_id).strip()
        if sid:
            cleaned[str(guid).strip()] = sid
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    with open(ALOHA_LOCATION_MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    return {"success": True, "saved": len(cleaned)}


@app.get("/api/aloha/order-modes")
async def get_aloha_order_modes_ui():
    """Return distinct Aloha OrderMode values + saved mapping."""
    order_modes: set = set()

    # Primary: query SQLite (instant, covers all data)
    if aloha_db.db_available():
        try:
            db_modes = await asyncio.to_thread(aloha_db.get_distinct_order_modes)
            order_modes.update(db_modes)
        except Exception:
            pass

    # Fallback: sample recent CSV files
    if not order_modes:
        index = load_yoy_index()
        file_entries = list(index.get("files", {}).values())
        sampled = file_entries[-60:] if len(file_entries) > 60 else file_entries
        for entry in sampled:
            path = entry.get("path")
            if path and os.path.exists(path):
                try:
                    df = pd.read_csv(path, usecols=lambda c: c == "OrderMode")
                    if "OrderMode" in df.columns:
                        for val in df["OrderMode"].dropna().unique():
                            order_modes.add(str(val).strip())
                except Exception:
                    pass

    mapping: Dict[str, str] = {}
    if os.path.exists(ALOHA_ORDER_MODE_MAPPING_FILE):
        try:
            with open(ALOHA_ORDER_MODE_MAPPING_FILE, "r", encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception:
            pass

    return {
        "aloha_order_modes": sorted(order_modes),
        "mapping": mapping,
    }


@app.put("/api/aloha/order-mode-mapping")
async def save_order_mode_mapping_ui(body: dict = Body(...)):
    """Save the Aloha OrderMode -> Toast channel mapping."""
    mapping = body.get("mapping", {})
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="'mapping' must be an object {alohaMode: toastChannel}")
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    with open(ALOHA_ORDER_MODE_MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): str(v) for k, v in mapping.items()}, f, indent=2)
    return {"success": True, "saved": len(mapping)}


@app.get("/api/aloha/toast-channels")
async def get_toast_channels_ui():
    """Return the list of Toast channel label options."""
    return {"channels": get_toast_channels()}


@app.put("/api/aloha/toast-channels")
async def save_toast_channels_ui(body: dict = Body(...)):
    """Save the Toast channel label list."""
    channels = body.get("channels", [])
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="'channels' must be an array of strings")
    cleaned = [str(c).strip() for c in channels if str(c).strip()]
    os.makedirs(YOY_DATA_DIR, exist_ok=True)
    with open(TOAST_CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    return {"success": True, "saved": len(cleaned)}


# YoY Comparison API
# ------------------

@app.get("/api/yoy/aloha-dates")
async def get_available_aloha_dates():
    """Get list of dates with Aloha data (merges SQLite + index.json sources)."""
    # Primary: SQLite (fast, authoritative after migration)
    db_dates = set(aloha_db.get_available_dates())
    # Secondary: index.json (catches manually-uploaded files not in SQLite)
    index = load_yoy_index()
    csv_dates = set(index.get("files", {}).keys())
    all_dates = sorted(db_dates | csv_dates)
    return {
        "available_dates": all_dates,
        "db_dates": len(db_dates),
        "csv_only_dates": len(csv_dates - db_dates),
    }


@app.get("/api/yoy/aloha-stats")
async def get_aloha_stats():
    """Return summary statistics about the Aloha SQLite database."""
    # aloha.db can be 7+ GB — use a fast check instead of full table scan
    available = await asyncio.to_thread(aloha_db.db_available)
    if not available:
        return {"db_available": False}
    try:
        stats = await asyncio.wait_for(asyncio.to_thread(aloha_db.get_stats), timeout=10.0)
    except asyncio.TimeoutError:
        # Full table scan timed out — return what we can
        import os as _os
        db_path = aloha_db.ALOHA_DB_PATH
        size_mb = _os.path.getsize(db_path) / (1024 * 1024) if _os.path.exists(db_path) else 0
        stats = {"total_rows": "large", "db_size_mb": round(size_mb, 2)}
    index = load_yoy_index()
    csv_count = len(index.get("files", {}))
    return {
        **stats,
        "csv_file_count": csv_count,
        "db_available": True,
    }


@app.post("/api/yoy/aloha-rebuild-db")
async def rebuild_aloha_db():
    """
    Rebuild the SQLite database from the existing CSV files.
    Use after a fresh install or if the DB gets out of sync.
    This runs synchronously and may take a minute for large datasets.
    """
    import glob as _glob

    index = load_yoy_index()
    file_entries = list(index.get("files", {}).items())
    # Also pick up any CSV files not in the index
    csv_glob = sorted(_glob.glob(os.path.join(YOY_DATA_DIR, "aloha_????-??-??.csv")))
    indexed_paths = {v.get("path") for _, v in file_entries}
    for p in csv_glob:
        if p not in indexed_paths:
            basename = os.path.basename(p)
            date_iso = basename[6:16]
            file_entries.append((date_iso, {"path": p}))

    aloha_db.initialize_db()
    written = 0
    errors = 0
    _ORDER_MODE_ID_ALIASES = ("fkordermodeid", "fk_order_mode_id", "ordermodeid", "order_mode_id")
    for date_iso, entry in file_entries:
        path = entry.get("path", "")
        if not path or not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            df.columns = [c.strip() for c in df.columns]
            if "Item" not in df.columns:
                continue
            if "OrderMode" not in df.columns:
                for c in df.columns:
                    if c.strip().lower() in _ORDER_MODE_ID_ALIASES:
                        df = df.rename(columns={c: "OrderMode"})
                        df["OrderMode"] = df["OrderMode"].astype(str).str.strip()
                        break
            for col, default in [("Quantity", 0), ("Sales", 0), ("Location", ""), ("OrderMode", "Unknown")]:
                if col not in df.columns:
                    df[col] = default
            aloha_db.upsert_date(df, date_iso)
            written += 1
        except Exception as exc:
            logger.warning(f"Rebuild: failed for {date_iso}: {exc}")
            errors += 1

    stats = aloha_db.get_stats()
    return {
        "success": True,
        "dates_written": written,
        "errors": errors,
        **stats,
    }


@app.post("/api/yoy/upload-aloha")
async def upload_aloha_data(
    file: UploadFile = File(...),
    business_date: str = Body(...)
):
    """Upload Aloha CSV/XLSX data and store for YoY comparisons."""
    ensure_yoy_storage()
    try:
        date_obj = date.fromisoformat(business_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="business_date must be YYYY-MM-DD")

    filename = file.filename or "aloha_data"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in [".csv", ".xlsx", ".xls"]:
        raise HTTPException(status_code=400, detail="Only .csv, .xlsx, or .xls files are supported")

    safe_name = f"aloha_{date_obj.isoformat()}{ext}"
    target_path = os.path.join(YOY_DATA_DIR, safe_name)
    raw = await file.read()

    with open(target_path, "wb") as f:
        f.write(raw)

    # Also insert into SQLite so it's immediately queryable
    try:
        if ext == ".csv":
            import io
            df_upload = pd.read_csv(io.BytesIO(raw))
        else:
            import io
            df_upload = pd.read_excel(io.BytesIO(raw))
        df_upload.columns = [c.strip() for c in df_upload.columns]
        if "Item" in df_upload.columns:
            if "OrderMode" not in df_upload.columns:
                for c in df_upload.columns:
                    if c.strip().lower() in ("fkordermodeid", "fk_order_mode_id", "ordermodeid", "order_mode_id"):
                        df_upload = df_upload.rename(columns={c: "OrderMode"})
                        df_upload["OrderMode"] = df_upload["OrderMode"].astype(str).str.strip()
                        break
            aloha_db.upsert_date(df_upload, date_obj.isoformat())
    except Exception as exc:
        logger.warning(f"Could not insert uploaded file into SQLite: {exc}")

    index = load_yoy_index()
    index.setdefault("files", {})[date_obj.isoformat()] = {
        "path": target_path,
        "original_name": filename,
        "uploaded_at": datetime.now().isoformat()
    }
    save_yoy_index(index)

    return {
        "success": True,
        "message": f"Uploaded Aloha data for {date_obj.isoformat()}",
        "stored_as": safe_name
    }

@app.post("/api/yoy/import-aloha-sql")
async def import_aloha_sql(request: AlohaSqlImportRequest = Body(...)):
    """Import historical Aloha data directly from SQL Server."""
    try:
        if request.end_date:
            end_date = date.fromisoformat(request.end_date)
        else:
            end_date = date.today()

        if request.start_date:
            start_date = date.fromisoformat(request.start_date)
        else:
            start_date = end_date - timedelta(days=729)
    except ValueError:
        raise HTTPException(status_code=400, detail="start_date/end_date must be YYYY-MM-DD")

    try:
        result = await asyncio.to_thread(
            import_aloha_sql_range,
            start_date,
            end_date,
            request.batch_days,
            request.overwrite
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 500 if "Missing ALOHA_SQL" in message else 400
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQL import failed: {exc}")

    return {
        "success": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        **result
    }

@app.post("/api/yoy/import-aloha-sql/recent")
async def import_aloha_sql_recent(request: AlohaSqlRecentImportRequest = Body(...)):
    """Import recent Aloha data for periodic updates."""
    try:
        result = await asyncio.to_thread(
            import_aloha_sql_last_days,
            request.days,
            request.overwrite,
            request.batch_days
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 500 if "Missing ALOHA_SQL" in message else 400
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQL import failed: {exc}")

    return {
        "success": True,
        "days": request.days,
        **result
    }

# ──────────────────────────────────────────────────────────────────────────────
# Aloha Sales YoY  —  aggregate net sales by location (not per-item)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/yoy/import-aloha-sales-sql")
async def import_aloha_sales_sql(request: AlohaSalesImportRequest = Body(...)):
    """Import historical Aloha aggregate sales data from SQL Server."""
    try:
        if request.end_date:
            end_dt = date.fromisoformat(request.end_date)
        else:
            end_dt = date.today()
        if request.start_date:
            start_dt = date.fromisoformat(request.start_date)
        else:
            start_dt = end_dt - timedelta(days=729)
    except ValueError:
        raise HTTPException(status_code=400, detail="start_date/end_date must be YYYY-MM-DD")

    try:
        result = await asyncio.to_thread(
            import_aloha_sales_range,
            start_dt, end_dt, request.batch_days, request.overwrite,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 500 if "Missing ALOHA_S" in message else 400
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sales SQL import failed: {exc}")

    return {"success": True, "start_date": start_dt.isoformat(), "end_date": end_dt.isoformat(), **result}


@app.post("/api/yoy/import-aloha-sales-sql/recent")
async def import_aloha_sales_sql_recent(request: AlohaSalesRecentImportRequest = Body(...)):
    """Import recent Aloha sales data for periodic updates."""
    try:
        result = await asyncio.to_thread(
            import_aloha_sales_last_days,
            request.days, request.overwrite, request.batch_days,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 500 if "Missing ALOHA_S" in message else 400
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sales SQL import failed: {exc}")

    return {"success": True, "days": request.days, **result}


@app.get("/api/yoy/aloha-sales-dates")
async def get_aloha_sales_dates():
    """Return list of dates with Aloha sales data."""
    return {"dates": aloha_sales_db.get_available_dates()}


@app.get("/api/yoy/aloha-sales-stats")
async def get_aloha_sales_stats():
    """Return summary statistics about the Aloha sales database, split by source."""
    stats = aloha_sales_db.get_stats()
    # Add split: Aloha historical vs Toast flash-imported
    # Toast went live ~Sept 2025; flash import started writing CY data from that point
    try:
        conn = aloha_sales_db.get_connection()
        try:
            # Dates with flash-imported data (96 locations = Toast)
            # vs Aloha historical (fewer locations or pre-2025-09)
            toast_range = conn.execute(
                "SELECT MIN(business_date), MAX(business_date), COUNT(DISTINCT business_date) "
                "FROM aloha_sales_data WHERE business_date >= '2025-09-01'"
            ).fetchone()
            aloha_range = conn.execute(
                "SELECT MIN(business_date), MAX(business_date), COUNT(DISTINCT business_date) "
                "FROM aloha_sales_data WHERE business_date < '2025-09-01'"
            ).fetchone()
        finally:
            conn.close()
        stats["toast_min_date"] = toast_range[0]
        stats["toast_max_date"] = toast_range[1]
        stats["toast_date_count"] = toast_range[2] or 0
        stats["aloha_min_date"] = aloha_range[0]
        stats["aloha_max_date"] = aloha_range[1]
        stats["aloha_date_count"] = aloha_range[2] or 0
    except Exception:
        pass
    return stats


@app.get("/api/yoy/fiscal-calendar-stats")
async def get_fiscal_calendar_stats():
    """Return summary statistics about the fiscal calendar table."""
    return aloha_sales_db.get_fiscal_calendar_stats()


@app.post("/api/yoy/import-fiscal-calendar")
async def import_fiscal_calendar_endpoint(payload: dict = Body(...)):
    """
    Import fiscal calendar from CalendarV2 into local SQLite.
    Expects: {"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}
    """
    start_str = payload.get("start_date")
    end_str = payload.get("end_date")
    if not start_str or not end_str:
        raise HTTPException(status_code=400, detail="start_date and end_date required")
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    try:
        count = import_fiscal_calendar(start, end)
        stats = aloha_sales_db.get_fiscal_calendar_stats()
        return {"success": True, "rows_imported": count, **stats}
    except Exception as exc:
        logger.error("Fiscal calendar import failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Weekly Sales Report — WPY Comparative
# ──────────────────────────────────────────────────────────────────────────────

# Channel definitions matching the old SSRS stored procedure
_WPY_CHANNELS = {
    "Net":              {"cols": ["net_sales"], "label": "Net Sales"},
    "GNG":              {"cols": ["gng_net_sales"], "label": "Grab & Go"},
    "Catering":         {"cols": ["catering_net_sales"], "label": "Catering"},
    "OLO":              {"cols": ["olo_net_sales"], "label": "OLO"},
    "NonCatering":      {"cols": ["net_sales", "-catering_net_sales"], "label": "Non-Catering"},
    "NonCateringChecks":{"cols": ["checks_minus_catering"], "label": "Non-Catering Checks"},
    "Checks":           {"cols": ["check_count"], "label": "Checks"},
    "DineInNet":        {"cols": ["dinein_net_sales"], "label": "Dine In"},
    "DeliveryNet":      {"cols": ["delivery_net_sales"], "label": "Delivery"},
    "ToGoNet":          {"cols": ["togo_net_sales"], "label": "To Go"},
    "CallInNet":        {"cols": ["callin_net_sales", "olo_callin_net_sales"], "label": "Call In"},
    "CurbSide":         {"cols": ["curbside_net_sales", "curbside_olo_net_sales"], "label": "CurbSide"},
}


def _compute_channel_value(row_dict: dict, col_spec: list) -> float:
    """Sum column values for a channel. Prefix '-' means subtract."""
    total = 0.0
    for c in col_spec:
        if c.startswith("-"):
            total -= float(row_dict.get(c[1:], 0) or 0)
        else:
            total += float(row_dict.get(c, 0) or 0)
    return total


def _make_totals(cur: float, prior: float) -> dict:
    """Build a totals dict with variance and comp %."""
    var = cur - prior
    comp = round((var / prior) * 100, 2) if prior else 0.0
    return {"currentYR": round(cur, 2), "priorYR": round(prior, 2), "variance": round(var, 2), "compPct": comp}


def _build_wpy_period(
    current_start: str, current_end: str,
    prior_start: str, prior_end: str,
    locations: Optional[List[str]] = None,
    store_info: Optional[dict] = None,
    fbc_mapping: dict = None,
) -> dict:
    """
    Build one period (WTD/PTD/QTD/YTD) of the WPY report.

    store_info: dict keyed by location from aloha_sales_db.get_store_info()
    fbc_mapping: {loc_id: "FBC - Name" or "Company - Name"}
    """
    if fbc_mapping is None:
        fbc_mapping = {}
    cur_df = aloha_sales_db.query_date_range(current_start, current_end, locations=locations)
    prior_df = aloha_sales_db.query_date_range(prior_start, prior_end, locations=locations)

    # Index by location
    cur_by_loc = {}
    if not cur_df.empty:
        for _, row in cur_df.iterrows():
            cur_by_loc[str(row["location"])] = row.to_dict()

    prior_by_loc = {}
    if not prior_df.empty:
        for _, row in prior_df.iterrows():
            prior_by_loc[str(row["location"])] = row.to_dict()

    all_locs = sorted(set(list(cur_by_loc.keys()) + list(prior_by_loc.keys())))

    # Get store metadata
    si = store_info or {}

    _COMPANY_ENTITY = "Newco Dining LLC"

    channels = {}
    for ch_key, ch_def in _WPY_CHANNELS.items():
        col_spec = ch_def["cols"]
        loc_rows = []
        total_cur = 0.0
        total_prior = 0.0
        comping_cur = 0.0
        comping_prior = 0.0
        co_cur = 0.0
        co_prior = 0.0
        co_comp_cur = 0.0
        co_comp_prior = 0.0
        fr_cur = 0.0
        fr_prior = 0.0
        fr_comp_cur = 0.0
        fr_comp_prior = 0.0

        for loc in all_locs:
            cur_val = _compute_channel_value(cur_by_loc.get(loc, {}), col_spec)
            prior_val = _compute_channel_value(prior_by_loc.get(loc, {}), col_spec)
            variance = cur_val - prior_val
            comp_pct = round((variance / prior_val) * 100, 2) if prior_val else 0.0

            loc_info = si.get(loc, {})
            loc_name = loc_info.get("restaurant_name", "")
            if not loc_name:
                loc_name = cur_by_loc.get(loc, {}).get("restaurant_name", "") or prior_by_loc.get(loc, {}).get("restaurant_name", "")

            is_comping = loc_info.get("comping") == "Y"
            is_company = loc_info.get("company") == _COMPANY_ENTITY

            loc_rows.append({
                "location": loc,
                "restaurantName": loc_name,
                "company": loc_info.get("company", ""),
                "fbcGroup": fbc_mapping.get(loc, loc_info.get("company", "") or "Unassigned"),
                "comping": loc_info.get("comping", ""),
                "openDate": loc_info.get("open_date", ""),
                "currentYR": round(cur_val, 2),
                "priorYR": round(prior_val, 2),
                "variance": round(variance, 2),
                "compPct": comp_pct,
            })
            total_cur += cur_val
            total_prior += prior_val

            if is_comping:
                comping_cur += cur_val
                comping_prior += prior_val

            if is_company:
                co_cur += cur_val
                co_prior += prior_val
                if is_comping:
                    co_comp_cur += cur_val
                    co_comp_prior += prior_val
            else:
                fr_cur += cur_val
                fr_prior += prior_val
                if is_comping:
                    fr_comp_cur += cur_val
                    fr_comp_prior += prior_val

        channels[ch_key] = {
            "label": ch_def["label"],
            "locations": loc_rows,
            "totals": {
                "allRestaurants": _make_totals(total_cur, total_prior),
                "comping": _make_totals(comping_cur, comping_prior),
                "companyAll": _make_totals(co_cur, co_prior),
                "companySss": _make_totals(co_comp_cur, co_comp_prior),
                "franchiseAll": _make_totals(fr_cur, fr_prior),
                "franchiseSss": _make_totals(fr_comp_cur, fr_comp_prior),
            },
        }

    return {
        "currentRange": {"start": current_start, "end": current_end},
        "priorRange": {"start": prior_start, "end": prior_end},
        "channels": channels,
    }


def _load_store_ownership() -> dict:
    """Load store_ownership.json → {aloha_id: {ownershipType, toastGuid, ...}}"""
    path = os.path.join(os.path.dirname(__file__), "store_ownership.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@app.get("/api/aloha-sales/locations")
async def list_aloha_sales_locations():
    """Return distinct Aloha locations that have sales data, including ownershipType."""
    if not aloha_sales_db.db_available():
        return []
    ownership = _load_store_ownership()
    conn = aloha_sales_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT location, restaurant_name FROM aloha_sales_data "
            "WHERE net_sales > 0 ORDER BY CAST(location AS INTEGER)"
        ).fetchall()
        result = []
        for r in rows:
            ot = ownership.get(r[0], {}).get("ownershipType", "Franchise")
            result.append({"id": r[0], "name": r[1], "ownershipType": ot})
        return result
    finally:
        conn.close()


@app.get("/api/locations/comping")
async def get_comping_locations():
    """
    Return the curated set of "comp" stores — locations included in the
    Daily Sales Flash Report's comp set. Used by the location-picker UI
    so a user can run a report scoped to just the comping group.

    Sourced from backend/comp_stores.json (manually generated by matching
    Flash Report names → Restaurant ID → Toast store_guid).
    """
    path = os.path.join(os.path.dirname(__file__), "comp_stores.json")
    if not os.path.exists(path):
        return {"stores": [], "guids": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to load comp_stores.json: {exc}")
        return {"stores": [], "guids": []}
    stores = data.get("stores", []) or []
    return {
        "stores": stores,
        "guids":  [s["store_guid"] for s in stores if s.get("store_guid")],
    }


@app.get("/api/location-ownership")
async def get_location_ownership():
    """Return ownership type (Company/Franchise) keyed by Aloha ID and Toast GUID."""
    ownership = _load_store_ownership()
    by_aloha = {rid: v["ownershipType"] for rid, v in ownership.items()}
    by_guid = {}
    for rid, v in ownership.items():
        if v.get("toastGuid"):
            by_guid[v["toastGuid"]] = v["ownershipType"]
    # Also derive from flash loc mapping for any GUIDs not in store_ownership.json
    guid_to_loc = _get_flash_loc_mapping()
    for guid, aloha_id in guid_to_loc.items():
        if guid not in by_guid and aloha_id in by_aloha:
            by_guid[guid] = by_aloha[aloha_id]
    return {"byAlohaId": by_aloha, "byGuid": by_guid}


@app.post("/api/wpy-report")
async def run_wpy_report(payload: dict = Body(...)):
    """
    Run the Weekly Sales Report — WPY Comparative.

    Expects: {"report_date": "YYYY-MM-DD"} — the "as of" date.
    Uses fiscal calendar to compute WTD, PTD, QTD, YTD date ranges and
    their prior-year equivalents.
    """
    report_date_str = payload.get("report_date")
    if not report_date_str:
        raise HTTPException(status_code=400, detail="report_date required (YYYY-MM-DD)")
    try:
        report_date = date.fromisoformat(report_date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="report_date must be YYYY-MM-DD")

    if not aloha_sales_db.db_available():
        raise HTTPException(status_code=400, detail="Aloha sales database not available")
    if not aloha_sales_db.fiscal_calendar_available():
        raise HTTPException(status_code=400, detail="Fiscal calendar not loaded — import it first")

    # Look up fiscal calendar for report_date to get fiscal period/week info
    fc = aloha_sales_db.get_fiscal_ly_dates(report_date_str, report_date_str)
    if not fc:
        raise HTTPException(status_code=400, detail=f"No fiscal calendar entry for {report_date_str}")

    # Get fiscal info for report_date
    conn = aloha_sales_db.get_connection()
    try:
        cal_row = conn.execute(
            "SELECT fiscal_year, fiscal_period, fiscal_week FROM fiscal_calendar WHERE date = ?",
            (report_date_str,)
        ).fetchone()
        if not cal_row:
            raise HTTPException(status_code=400, detail=f"No fiscal calendar entry for {report_date_str}")
        fy, fp, fw = cal_row

        # WTD: Find the start of the current fiscal week
        # The fiscal week starts on Monday (datefirst=1 in the SP)
        # Find the Monday on or before report_date
        weekday = report_date.weekday()  # 0=Monday
        wtd_start = report_date - timedelta(days=weekday)
        wtd_end = report_date

        # PTD: fiscal period start
        ptd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
            (fy, fp)
        ).fetchone()
        ptd_start = date.fromisoformat(ptd_row[0]) if ptd_row and ptd_row[0] else wtd_start
        ptd_end = report_date

        # QTD: fiscal quarter start (periods 1-3=Q1, 4-6=Q2, 7-9=Q3, 10-12=Q4)
        q_start_period = ((fp - 1) // 3) * 3 + 1
        qtd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
            (fy, q_start_period)
        ).fetchone()
        qtd_start = date.fromisoformat(qtd_row[0]) if qtd_row and qtd_row[0] else ptd_start
        qtd_end = report_date

        # YTD: fiscal year start
        ytd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ?",
            (fy,)
        ).fetchone()
        ytd_start = date.fromisoformat(ytd_row[0]) if ytd_row and ytd_row[0] else qtd_start
        ytd_end = report_date

    finally:
        conn.close()

    # ── Prior-year date ranges ──────────────────────────────────
    # WTD: use fiscal calendar per-day LY mapping (week-aligned)
    def get_ly_range_fiscal(start_d: date, end_d: date):
        """Get the fiscal calendar prior-year start/end for a date range (per-day mapping)."""
        ly_map = aloha_sales_db.get_fiscal_ly_dates(start_d.isoformat(), end_d.isoformat())
        if ly_map:
            ly_dates = sorted(ly_map.values())
            return ly_dates[0], ly_dates[-1]
        return (start_d - timedelta(days=365)).isoformat(), (end_d - timedelta(days=365)).isoformat()

    # PTD/QTD/YTD: find prior fiscal year's same period start, then offset same # of days
    def get_ly_range_period(cy_period_start: date, cy_end: date, py_fy: int, py_period: int):
        """Prior year = same period start in prior FY + same day-count offset."""
        py_start_row = conn2.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
            (py_fy, py_period)
        ).fetchone()
        if py_start_row and py_start_row[0]:
            py_start = date.fromisoformat(py_start_row[0])
            day_offset = (cy_end - cy_period_start).days
            py_end = py_start + timedelta(days=day_offset)
            return py_start.isoformat(), py_end.isoformat()
        return get_ly_range_fiscal(cy_period_start, cy_end)

    conn2 = aloha_sales_db.get_connection()
    try:
        wtd_ly = get_ly_range_fiscal(wtd_start, wtd_end)

        # PTD PY: FY-1 same period start + same offset
        ptd_ly = get_ly_range_period(ptd_start, ptd_end, fy - 1, fp)

        # QTD PY: FY-1 same quarter-start period + same offset
        qtd_ly = get_ly_range_period(qtd_start, qtd_end, fy - 1, q_start_period)

        # YTD PY: FY-1 period 1 start + same offset
        ytd_ly = get_ly_range_period(ytd_start, ytd_end, fy - 1, 1)
    finally:
        conn2.close()

    # Optional location filter
    locations = payload.get("locations")  # list of Aloha RestaurantID strings

    # ── Auto-import missing dates from ERA API ──────────────────
    # Check which CY dates in the WTD range are missing from aloha_sales_db
    # (WTD is the most time-sensitive; PTD/QTD/YTD benefit automatically)
    try:
        existing_dates = set()
        _conn_chk = aloha_sales_db.get_connection()
        _rows_chk = _conn_chk.execute(
            "SELECT DISTINCT business_date FROM aloha_sales_data WHERE business_date BETWEEN ? AND ?",
            (wtd_start.isoformat(), wtd_end.isoformat()),
        ).fetchall()
        _conn_chk.close()
        existing_dates = {r[0] for r in _rows_chk}

        missing_dates = []
        d = wtd_start
        while d <= wtd_end:
            if d.isoformat() not in existing_dates:
                missing_dates.append(d)
            d += timedelta(days=1)

        if missing_dates:
            logger.info("WPY report: auto-importing %d missing dates: %s",
                        len(missing_dates), [d.isoformat() for d in missing_dates])
            for md in missing_dates:
                try:
                    await _run_flash_import_for_date(md)
                except Exception as exc:
                    logger.warning("WPY auto-import failed for %s: %s", md.isoformat(), exc)
    except Exception as exc:
        logger.warning("WPY missing-date check failed: %s", exc)

    # Load store metadata for comping/company totals
    store_info = aloha_sales_db.get_store_info()

    # Load FBC grouping
    fbc_mapping = _load_fbc_mapping()

    # Build each period
    result = {
        "reportDate": report_date_str,
        "fiscalYear": fy,
        "fiscalPeriod": fp,
        "fiscalWeek": fw,
        "periods": {
            "WTD": _build_wpy_period(wtd_start.isoformat(), wtd_end.isoformat(), wtd_ly[0], wtd_ly[1], locations, store_info, fbc_mapping),
            "PTD": _build_wpy_period(ptd_start.isoformat(), ptd_end.isoformat(), ptd_ly[0], ptd_ly[1], locations, store_info, fbc_mapping),
            "QTD": _build_wpy_period(qtd_start.isoformat(), qtd_end.isoformat(), qtd_ly[0], qtd_ly[1], locations, store_info, fbc_mapping),
            "YTD": _build_wpy_period(ytd_start.isoformat(), ytd_end.isoformat(), ytd_ly[0], ytd_ly[1], locations, store_info, fbc_mapping),
        },
        "channelOrder": list(_WPY_CHANNELS.keys()),
        "channelLabels": {k: v["label"] for k, v in _WPY_CHANNELS.items()},
        "fbcGroups": sorted(set(fbc_mapping.values())) if fbc_mapping else [],
    }
    return result


# ── Daily Sales Flash Report ─────────────────────────────────────────

# Dining option → flash channel mapping
# Source: toast.dbo.cfg_DiningOptions JOIN toast.dbo.cfg_map_DiningOptionToOrderMode
_DINING_OPTION_TO_CHANNEL = {
    # CallInNet (FlashTab=CallInNet, FlashSort=2)
    "Call-In":              "CallInNet",
    "X-Call In-OLO":        "CallInNet",
    "X-KEA Call In":        "CallInNet",
    # ToGoNet (FlashTab=ToGoNet, FlashSort=3)
    "ToGo":                 "ToGoNet",
    "X-To Go (K)":          "ToGoNet",
    "Drive Thru":           "ToGoNet",
    "X-Drive Thru-OLO":     "ToGoNet",
    # OLO (FlashTab=OLO, FlashSort=4)
    "X-ToGo-OLO":          "OLO",
    # DineInNet (FlashTab=DineInNet, FlashSort=5)
    "Dine In":              "DineInNet",
    "X-Dine-In (K)":        "DineInNet",
    "X-Dine-In-OLO":        "DineInNet",
    # DeliveryNet (FlashTab=DeliveryNet, FlashSort=6)
    "Delivery":             "DeliveryNet",
    "X-Delivery-OLO":       "DeliveryNet",
    "X-Doordash (R)":       "DeliveryNet",
    "X-UberEats (R)":       "DeliveryNet",
    "X-GrubHub (R)":        "DeliveryNet",
    "X-Dispatch DD":        "DeliveryNet",
    "X-Dispatch DL":        "DeliveryNet",
    "X-Dispatch PM":        "DeliveryNet",
    "X-Dispatch SC":        "DeliveryNet",
    "X-Dispatch Uber":      "DeliveryNet",
    "X-Dispatch (Generic)": "DeliveryNet",
    "X-Rails (Generic)":    "DeliveryNet",
    # CurbSide (FlashTab=CurbSide, FlashSort=7)
    "Curbside":             "CurbSide",
    "X-Curbside-OLO":       "CurbSide",
    # POS2 (FlashTab=POS2, FlashSort=8)
    "Union Aramark (POS 2)":"POS2",
    # Catering — FlashTab says "NOT USED IN FLASH" but we include it as a channel
    "Catering":             "Catering",
    "X-EzCater":            "Catering",
    "X-Online Catering":    "Catering",
}

# Flash channels in display order
_FLASH_CHANNEL_KEYS = [
    "Net", "CallInNet", "ToGoNet", "GNG", "Catering", "OLO",
    "NonCatering", "NonCateringChecks", "CheckCount",
    "DineInNet", "DeliveryNet", "CurbSide",
]
_FLASH_CHANNEL_LABELS = {
    "Net": "R-Net", "CallInNet": "CallInNet", "ToGoNet": "ToGoNet",
    "GNG": "GNG", "Catering": "Catering (cat)", "OLO": "OLO",
    "NonCatering": "NonCatering", "NonCateringChecks": "NonCateringChecks",
    "CheckCount": "CheckCount", "DineInNet": "DineInNet",
    "DeliveryNet": "DeliveryNet", "CurbSide": "CurbSide",
    "CheckAverage": "CheckAverage", "POS2": "POS2",
}


async def _fetch_flash_era_data(
    start_date: date, end_date: date,
    restaurant_ids: List[str],
) -> List[dict]:
    """
    Call Toast ERA metrics API with groupBy diningOption.
    Returns list of rows: {restaurantGuid, restaurantName, diningOption, netSalesAmount, guestCount, ordersCount, ...}
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        report_guid = await request_toast_report(
            client, start_date, end_date, restaurant_ids,
            report_type="metrics",
            group_by=["DINING_OPTION"],
        )
        data = await fetch_toast_report(client, report_guid, report_type="metrics")
    return data if isinstance(data, list) else []


def _aggregate_flash_by_location(
    rows: List[dict],
    guid_to_loc: dict,
) -> dict:
    """
    Aggregate ERA metrics rows (with diningOption) into per-location channel totals.
    Returns: {loc_id: {channel_key: net_sales_amount, "Net": total, "CheckCount": count, ...}}
    """
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        guid = row.get("restaurantGuid", "")
        loc_id = guid_to_loc.get(guid)
        if not loc_id:
            continue

        net = float(row.get("netSalesAmount", 0) or 0)
        orders = float(row.get("ordersCount", 0) or 0)
        guests = float(row.get("guestCount", 0) or 0)
        dining_opt = str(row.get("diningOption", "") or "").strip()

        if loc_id not in result:
            result[loc_id] = {ch: 0.0 for ch in _FLASH_CHANNEL_KEYS}
            result[loc_id]["POS2"] = 0.0
            result[loc_id]["_orders"] = 0.0
            result[loc_id]["_guests"] = 0.0

        # Map dining option to channel
        channel = _DINING_OPTION_TO_CHANNEL.get(dining_opt)
        if channel and channel in result[loc_id]:
            result[loc_id][channel] += net

        # Net is always total regardless of dining option
        result[loc_id]["Net"] += net
        result[loc_id]["CheckCount"] += orders
        result[loc_id]["_orders"] += orders
        result[loc_id]["_guests"] += guests

    # Compute derived channels
    for loc_id, ch in result.items():
        ch["NonCatering"] = ch["Net"] - ch["Catering"]
        ch["NonCateringChecks"] = ch["CheckCount"] - 0  # TODO: subtract catering checks if available

    return result

def _load_fbc_mapping() -> dict:
    """Load the FBC (Franchise Business Consultant) location mapping.
    Returns TWO dicts: {loc_id: fbc_group} and {restaurant_name: fbc_group}.
    The name-based mapping is resolved to loc_id using store_info.
    """
    fbc_path = os.path.join(os.path.dirname(__file__), "data", "fbc_location_mapping.json")
    if not os.path.exists(fbc_path):
        return {}
    with open(fbc_path) as f:
        name_mapping = json.load(f)  # {restaurant_name: fbc_group}

    # Resolve restaurant names to Aloha location IDs
    si = aloha_sales_db.get_store_info()
    name_to_loc = {}
    for loc_id, info in si.items():
        name_to_loc[info.get("restaurant_name", "")] = loc_id

    loc_mapping = {}
    for store_name, fbc_group in name_mapping.items():
        loc_id = name_to_loc.get(store_name)
        if loc_id:
            loc_mapping[loc_id] = fbc_group
        else:
            # Keep name-based entry for stores not in store_info
            loc_mapping[store_name] = fbc_group

    return loc_mapping


def _build_flash_period_from_api(
    cur_by_loc: dict,
    prior_by_loc: dict,
    current_start: str, current_end: str,
    prior_start: str, prior_end: str,
    store_info: dict,
    fbc_mapping: dict = None,
) -> dict:
    """
    Build one period of the Daily Flash report from pre-aggregated API data.
    cur_by_loc / prior_by_loc: {loc_id: {channel_key: value, ...}}
    store_info: {loc_id: {restaurant_name, company, comping, open_date, ...}}
    fbc_mapping: {loc_id: "FBC - Name" or "Company - Name"}
    """
    all_locs = sorted(set(list(cur_by_loc.keys()) + list(prior_by_loc.keys())))
    _COMPANY_ENTITY = "Newco Dining LLC"
    if fbc_mapping is None:
        fbc_mapping = {}

    # Ensure ALL stores in fbc_mapping appear, even if they have no CY/PY data
    # (e.g. Lubbock TX which is still on Aloha POS and may not have data for every date)
    # fbc_mapping keys are loc_ids like "1188"
    for loc_id in fbc_mapping.keys():
        if loc_id in store_info and loc_id not in all_locs:
            all_locs.append(loc_id)
            logger.info("Added missing FBC store %s (%s) to flash report",
                         loc_id, store_info[loc_id].get("restaurant_name", ""))
    all_locs = sorted(set(all_locs))

    all_channel_keys = _FLASH_CHANNEL_KEYS + ["POS2"]
    channels = {}
    nc_cur_by_loc = {}; nc_prior_by_loc = {}
    ncch_cur_by_loc = {}; ncch_prior_by_loc = {}

    for ch_key in all_channel_keys:
        loc_rows = []
        total_cur = 0.0; total_prior = 0.0
        comping_cur = 0.0; comping_prior = 0.0
        co_cur = 0.0; co_prior = 0.0
        co_comp_cur = 0.0; co_comp_prior = 0.0
        fr_cur = 0.0; fr_prior = 0.0
        fr_comp_cur = 0.0; fr_comp_prior = 0.0

        for loc in all_locs:
            cur_val = cur_by_loc.get(loc, {}).get(ch_key, 0.0)
            prior_val = prior_by_loc.get(loc, {}).get(ch_key, 0.0)
            variance = cur_val - prior_val
            comp_pct = round((variance / prior_val) * 100, 2) if prior_val else 0.0

            si = store_info.get(loc, {})
            loc_name = si.get("restaurant_name", "")
            company = si.get("company", "")
            comping_flag = si.get("comping", "")
            open_date = si.get("open_date", "")
            is_comping = str(comping_flag).strip().upper() == "Y"
            is_company = company == _COMPANY_ENTITY

            # Flag stores not on Toast (no GUID in location mapping)
            guid_mapped = any(v == loc for v in _get_flash_loc_mapping().values())
            not_on_toast = not guid_mapped and loc in store_info

            loc_rows.append({
                "location": loc,
                "restaurantName": loc_name,
                "company": company,
                "fbcGroup": fbc_mapping.get(loc, company or "Unassigned"),
                "comping": "Y" if is_comping else "N",
                "openDate": str(open_date) if open_date else "",
                "currentYR": round(cur_val, 2),
                "priorYR": round(prior_val, 2),
                "variance": round(variance, 2),
                "compPct": comp_pct,
                "notOnToast": not_on_toast,
            })
            total_cur += cur_val; total_prior += prior_val
            if is_comping:
                comping_cur += cur_val; comping_prior += prior_val
            if is_company:
                co_cur += cur_val; co_prior += prior_val
                if is_comping:
                    co_comp_cur += cur_val; co_comp_prior += prior_val
            else:
                fr_cur += cur_val; fr_prior += prior_val
                if is_comping:
                    fr_comp_cur += cur_val; fr_comp_prior += prior_val

            if ch_key == "NonCatering":
                nc_cur_by_loc[loc] = cur_val; nc_prior_by_loc[loc] = prior_val
            elif ch_key == "NonCateringChecks":
                ncch_cur_by_loc[loc] = cur_val; ncch_prior_by_loc[loc] = prior_val

        channels[ch_key] = {
            "label": _FLASH_CHANNEL_LABELS.get(ch_key, ch_key),
            "locations": loc_rows,
            "totals": {
                "allRestaurants": _make_totals(total_cur, total_prior),
                "comping": _make_totals(comping_cur, comping_prior),
                "companyAll": _make_totals(co_cur, co_prior),
                "companySss": _make_totals(co_comp_cur, co_comp_prior),
                "franchiseAll": _make_totals(fr_cur, fr_prior),
                "franchiseSss": _make_totals(fr_comp_cur, fr_comp_prior),
            },
        }

    # ── CheckAverage = NonCatering / NonCateringChecks ──
    def _safe_div(a, b):
        return round(a / b, 6) if b else 0.0

    ca_loc_rows = []
    ca_total_nc_cur = 0.0; ca_total_nc_prior = 0.0
    ca_total_ncch_cur = 0.0; ca_total_ncch_prior = 0.0
    ca_comp_nc_cur = 0.0; ca_comp_nc_prior = 0.0; ca_comp_ncch_cur = 0.0; ca_comp_ncch_prior = 0.0
    ca_co_nc_cur = 0.0; ca_co_nc_prior = 0.0; ca_co_ncch_cur = 0.0; ca_co_ncch_prior = 0.0
    ca_cocomp_nc_cur = 0.0; ca_cocomp_nc_prior = 0.0; ca_cocomp_ncch_cur = 0.0; ca_cocomp_ncch_prior = 0.0
    ca_fr_nc_cur = 0.0; ca_fr_nc_prior = 0.0; ca_fr_ncch_cur = 0.0; ca_fr_ncch_prior = 0.0
    ca_frcomp_nc_cur = 0.0; ca_frcomp_nc_prior = 0.0; ca_frcomp_ncch_cur = 0.0; ca_frcomp_ncch_prior = 0.0

    for loc in all_locs:
        nc_c = nc_cur_by_loc.get(loc, 0); nc_p = nc_prior_by_loc.get(loc, 0)
        ncch_c = ncch_cur_by_loc.get(loc, 0); ncch_p = ncch_prior_by_loc.get(loc, 0)
        cur_val = _safe_div(nc_c, ncch_c)
        prior_val = _safe_div(nc_p, ncch_p)
        variance = round(cur_val - prior_val, 6)
        comp_pct = round((variance / prior_val) * 100, 2) if prior_val else 0.0

        si = store_info.get(loc, {})
        is_comping = str(si.get("comping", "")).strip().upper() == "Y"
        is_company = si.get("company", "") == _COMPANY_ENTITY

        ca_loc_rows.append({
            "location": loc, "restaurantName": si.get("restaurant_name", ""),
            "company": si.get("company", ""),
            "comping": "Y" if is_comping else "N",
            "openDate": str(si.get("open_date", "")) if si.get("open_date") else "",
            "currentYR": cur_val, "priorYR": prior_val,
            "variance": variance, "compPct": comp_pct,
        })
        ca_total_nc_cur += nc_c; ca_total_nc_prior += nc_p
        ca_total_ncch_cur += ncch_c; ca_total_ncch_prior += ncch_p
        if is_comping:
            ca_comp_nc_cur += nc_c; ca_comp_nc_prior += nc_p
            ca_comp_ncch_cur += ncch_c; ca_comp_ncch_prior += ncch_p
        if is_company:
            ca_co_nc_cur += nc_c; ca_co_nc_prior += nc_p
            ca_co_ncch_cur += ncch_c; ca_co_ncch_prior += ncch_p
            if is_comping:
                ca_cocomp_nc_cur += nc_c; ca_cocomp_nc_prior += nc_p
                ca_cocomp_ncch_cur += ncch_c; ca_cocomp_ncch_prior += ncch_p
        else:
            ca_fr_nc_cur += nc_c; ca_fr_nc_prior += nc_p
            ca_fr_ncch_cur += ncch_c; ca_fr_ncch_prior += ncch_p
            if is_comping:
                ca_frcomp_nc_cur += nc_c; ca_frcomp_nc_prior += nc_p
                ca_frcomp_ncch_cur += ncch_c; ca_frcomp_ncch_prior += ncch_p

    def _make_ca_totals(nc_c, nc_p, ncch_c, ncch_p):
        c = _safe_div(nc_c, ncch_c); p = _safe_div(nc_p, ncch_p)
        v = round(c - p, 6)
        comp = round((v / p) * 100, 2) if p else 0.0
        return {"currentYR": c, "priorYR": p, "variance": v, "compPct": comp}

    channels["CheckAverage"] = {
        "label": "CheckAverage",
        "locations": ca_loc_rows,
        "totals": {
            "allRestaurants": _make_ca_totals(ca_total_nc_cur, ca_total_nc_prior, ca_total_ncch_cur, ca_total_ncch_prior),
            "comping": _make_ca_totals(ca_comp_nc_cur, ca_comp_nc_prior, ca_comp_ncch_cur, ca_comp_ncch_prior),
            "companyAll": _make_ca_totals(ca_co_nc_cur, ca_co_nc_prior, ca_co_ncch_cur, ca_co_ncch_prior),
            "companySss": _make_ca_totals(ca_cocomp_nc_cur, ca_cocomp_nc_prior, ca_cocomp_ncch_cur, ca_cocomp_ncch_prior),
            "franchiseAll": _make_ca_totals(ca_fr_nc_cur, ca_fr_nc_prior, ca_fr_ncch_cur, ca_fr_ncch_prior),
            "franchiseSss": _make_ca_totals(ca_frcomp_nc_cur, ca_frcomp_nc_prior, ca_frcomp_ncch_cur, ca_frcomp_ncch_prior),
        },
    }

    return {
        "currentRange": {"start": current_start, "end": current_end},
        "priorRange": {"start": prior_start, "end": prior_end},
        "channels": channels,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Toast Repoll — chain-wide or single-store, with live progress polling
# ──────────────────────────────────────────────────────────────────────────────

_REPOLL_STORE_GROUPING = 9
_REPOLL_STORE_GROUP    = 243

# In-memory job store.  Keyed by job_id (10-char hex).
# Survives as long as the process is alive; old completed jobs are pruned on
# each new start request (keeps the last 20).
_repoll_jobs: dict = {}


class RepollRequest(BaseModel):
    store_num:  Optional[int] = None   # None = all stores
    start_date: str                    # YYYY-MM-DD
    end_date:   str                    # YYYY-MM-DD


def _rlog(job_id: str, msg: str) -> None:
    """Append a timestamped line to the job log and echo to app logger."""
    ts = datetime.utcnow().strftime("%H:%M:%S")
    _repoll_jobs[job_id]["logs"].append(f"[{ts}] {msg}")
    logger.info("[repoll %s] %s", job_id, msg)


def _repoll_date_range(start: str, end: str):
    """Yield each date string from start to end inclusive."""
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    while d <= e:
        yield d.isoformat()
        d += timedelta(days=1)


async def _run_repoll_job(job_id: str, payload: RepollRequest) -> None:
    """
    Background task that drives the full repoll sequence:
      1. For each date in range, for each store:
           delete stale rows → fetch Toast API → write staging → direct copy → hstItem
      2. Fix DayPartIDs (the post-repoll SSMS step, now automated)
      3. Rebuild tbl_SalesDataByDayPart rollup
      4. Rebuild royalty table
    Progress is written to _repoll_jobs[job_id]["logs"] and polled by the UI.
    """
    job        = _repoll_jobs[job_id]
    start_date = payload.start_date
    end_date   = payload.end_date
    dates      = list(_repoll_date_range(start_date, end_date))

    try:
        # ── Load active stores ────────────────────────────────────────────────
        conn = repoll_db.get_toast_connection()
        try:
            all_stores = repoll_db.get_live_toast_stores(conn)
        finally:
            conn.close()

        if payload.store_num:
            stores = [s for s in all_stores if int(s["store_num"]) == payload.store_num]
            if not stores:
                _rlog(job_id, f"ERROR: Store {payload.store_num} not found in active Toast stores")
                job["status"] = "failed"
                return
        else:
            stores = [s for s in all_stores if s.get("guid") and s.get("store_num")]

        total_units = len(stores) * len(dates)
        _rlog(job_id, f"Starting: {len(stores)} store(s) × {len(dates)} date(s) = {total_units} units")
        _rlog(job_id, f"Date range: {start_date} → {end_date}")
        job["summary"]["stores_total"]  = total_units
        job["summary"]["stores_done"]   = 0
        job["summary"]["stores_failed"] = 0

        # ── Main loop: date-first ─────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=120.0) as client:
            for date_sql in dates:
                date_yyyymmdd = date_sql.replace("-", "")
                _rlog(job_id, f"── {date_sql} ──────────────────────────────────────────────")

                for s in stores:
                    store_num  = int(s["store_num"])
                    store_guid = str(s["guid"])
                    store_name = s.get("name", f"Store {store_num}")
                    _rlog(job_id, f"  → {store_num}  {store_name}")

                    try:
                        # Delete stale rows
                        conn = repoll_db.get_toast_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute(
                                "DELETE FROM toast.dbo.hstItem  WHERE StoreNum=? AND DateOfBusiness=?",
                                store_num, date_sql)
                            deleted = cur.rowcount
                            cur.execute(
                                "DELETE FROM toast.dbo.hstCheck WHERE StoreNum=? AND DateOfBusiness=?",
                                store_num, date_sql)
                            cur.execute(
                                "DELETE FROM toast.dbo.hstOrder WHERE StoreNum=? AND DateOfBusiness=?",
                                store_num, date_sql)
                            conn.commit()
                        finally:
                            conn.close()

                        # Truncate staging table
                        conn = repoll_db.get_toast_connection()
                        try:
                            conn.execute("TRUNCATE TABLE dbo.hstItem_temp")
                            conn.commit()
                        finally:
                            conn.close()

                        # Fetch from Toast API
                        orders, errors = await fetch_orders_with_retry(
                            client, store_guid, store_num, date_yyyymmdd)

                        if errors and not orders:
                            raise Exception(f"API error: {errors[0]}")

                        if not orders:
                            _rlog(job_id, f"    {store_num} / {date_sql}: 0 orders (closed day)")
                            job["summary"]["stores_done"] += 1
                            continue

                        parsed = parse_orders(orders, store_guid, store_num, date_yyyymmdd)
                        _rlog(job_id,
                              f"    {store_num} / {date_sql}: "
                              f"{len(parsed['orders'])} orders / "
                              f"{len(parsed['checks'])} checks / "
                              f"{len(parsed['items'])} items")

                        # Write to staging
                        conn = repoll_db.get_toast_connection()
                        try:
                            if parsed["orders"]:
                                repoll_db.upsert_orders(conn, parsed["orders"])
                            if parsed["checks"]:
                                repoll_db.upsert_checks(conn, parsed["checks"])
                            if parsed["items"]:
                                repoll_db.upsert_items(
                                    conn, parsed["items"],
                                    skip_truncate=True, skip_proc=True)
                        finally:
                            conn.close()

                        # Direct copy staging → hstItem (bypasses encrypted merge proc)
                        conn = repoll_db.get_toast_connection()
                        try:
                            inserted = repoll_db.direct_copy_items_from_temp(
                                conn, store_num, date_sql)
                            _rlog(job_id,
                                  f"    {store_num} / {date_sql}: {inserted} rows → hstItem")
                        finally:
                            conn.close()

                        job["summary"]["stores_done"] += 1

                    except Exception as exc:
                        _rlog(job_id, f"    ERROR {store_num} / {date_sql}: {exc}")
                        job["summary"]["stores_failed"] += 1

                # ── Fix DayPartIDs for this date ──────────────────────────────
                _rlog(job_id, f"  Fixing DayPartIDs for {date_sql}…")
                conn = repoll_db.get_toast_connection()
                try:
                    fixed = repoll_db.fix_daypart_ids(conn, date_sql, date_sql)
                    _rlog(job_id, f"  DayPartID fix: {fixed} rows updated")
                finally:
                    conn.close()

                # ── Rebuild rollup for this date ──────────────────────────────
                _rlog(job_id, f"  Rebuilding rollup for {date_sql}…")
                conn = repoll_db.get_toast_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart "
                        "WHERE DateOfBusiness = ?", date_sql)
                    _rlog(job_id, f"  Deleted {cur.rowcount} stale rollup rows")
                    conn.commit()
                    repoll_db.run_rollup(
                        conn,
                        start_date=date_sql,
                        end_date=date_sql,
                        store_grouping=_REPOLL_STORE_GROUPING,
                        store_group=_REPOLL_STORE_GROUP,
                    )
                    _rlog(job_id, f"  Rollup complete for {date_sql}")
                finally:
                    conn.close()

        # ── Rebuild royalty for full range ────────────────────────────────────
        _rlog(job_id, f"Rebuilding royalty table {start_date} → {end_date}…")
        conn = repoll_db.get_toast_connection()
        try:
            conn.execute(
                "EXEC dev_aloha.dbo.sp_tbl_Royalty_Values @StartDate=?, @EndDate=?",
                start_date, end_date)
            conn.commit()
            _rlog(job_id, "Royalty rebuild complete")
        finally:
            conn.close()

        job["status"] = "done"
        done   = job["summary"]["stores_done"]
        failed = job["summary"]["stores_failed"]
        _rlog(job_id, f"✓ Complete — {done} store/dates done, {failed} failed")

    except Exception as exc:
        _rlog(job_id, f"FATAL: {exc}")
        logger.error("Repoll job %s failed: %s", job_id, exc, exc_info=True)
        job["status"] = "failed"
    finally:
        job["finished_at"] = datetime.utcnow().isoformat()


@app.get("/api/admin/repoll/stores")
async def repoll_get_stores(
    current_user: User = Depends(get_current_user),
):
    """Return active Toast stores for the repoll store picker."""
    try:
        conn = repoll_db.get_toast_connection()
        try:
            stores = repoll_db.get_live_toast_stores(conn)
        finally:
            conn.close()
        return [
            {"store_num": s["store_num"], "name": s.get("name", "")}
            for s in stores if s.get("store_num")
        ]
    except Exception as exc:
        logger.error("repoll_get_stores failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not load stores: {exc}")


@app.post("/api/admin/repoll/start")
async def repoll_start(
    payload: RepollRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_admin_user),
):
    """
    Start a chain-wide or single-store repoll as a background job.
    Returns immediately with a job_id.
    Poll GET /api/admin/repoll/status/{job_id} every 3 s for live progress.
    """
    # Prune old completed jobs — keep the last 20
    finished = [jid for jid, j in _repoll_jobs.items()
                if j["status"] in ("done", "failed")]
    for old_id in finished[:-20]:
        _repoll_jobs.pop(old_id, None)

    job_id = uuid.uuid4().hex[:10]
    _repoll_jobs[job_id] = {
        "job_id":      job_id,
        "status":      "running",
        "logs":        [],
        "started_at":  datetime.utcnow().isoformat(),
        "finished_at": None,
        "started_by":  current_user.email,
        "store_num":   payload.store_num,
        "start_date":  payload.start_date,
        "end_date":    payload.end_date,
        "summary":     {"stores_total": 0, "stores_done": 0, "stores_failed": 0},
    }
    background_tasks.add_task(_run_repoll_job, job_id, payload)
    return {"job_id": job_id}


@app.get("/api/admin/repoll/status/{job_id}")
async def repoll_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Return current status, logs, and summary for a repoll job."""
    job = _repoll_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _get_flash_loc_mapping() -> dict:
    """Load Toast GUID → Aloha location ID mapping.
    Prefers backend/aloha_location_mapping.json (tracked in git),
    falls back to backend/data/aloha/aloha_location_mapping.json (legacy path).
    """
    base = os.path.dirname(__file__)
    for mapping_path in [
        os.path.join(base, "aloha_location_mapping.json"),
        os.path.join(base, "data", "aloha", "aloha_location_mapping.json"),
    ]:
        if os.path.exists(mapping_path):
            with open(mapping_path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def _ensure_all_mapped_guids(restaurant_ids: List[str]) -> List[str]:
    """
    Ensure restaurant_ids includes ALL GUIDs from aloha_location_mapping.json,
    even if they aren't returned by get_all_active_locations() yet (e.g. new stores
    not yet propagated to the Toast Restaurant Management API but already in ERA).
    """
    mapping = _get_flash_loc_mapping()
    id_set = set(restaurant_ids)
    added = []
    for guid in mapping.keys():
        if guid not in id_set:
            restaurant_ids.append(guid)
            added.append(guid)
    if added:
        logger.info("Added %d mapped GUIDs not in active locations: %s",
                     len(added), [f"{g[:8]}..." for g in added])
    return restaurant_ids


def _aggregate_flash_from_aloha(start_date: str, end_date: str, locations: Optional[List[str]] = None) -> dict:
    """
    Query aloha_sales_db for the given date range and return data in the same
    {loc_id: {channel_key: value}} format as _aggregate_flash_by_location().
    """
    df = aloha_sales_db.query_date_range(start_date, end_date, locations=locations)
    if df.empty:
        return {}

    result = {}
    for _, row in df.iterrows():
        loc_id = str(row["location"]).strip()
        if not loc_id:
            continue
        ch = {k: 0.0 for k in _FLASH_CHANNEL_KEYS}
        ch["POS2"] = 0.0
        ch["Net"] = float(row.get("net_sales", 0) or 0)
        ch["CallInNet"] = float(row.get("callin_net_sales", 0) or 0) + float(row.get("olo_callin_net_sales", 0) or 0)
        ch["ToGoNet"] = float(row.get("togo_net_sales", 0) or 0)
        ch["GNG"] = float(row.get("gng_net_sales", 0) or 0)
        ch["Catering"] = float(row.get("catering_net_sales", 0) or 0)
        ch["OLO"] = float(row.get("olo_net_sales", 0) or 0)
        ch["DineInNet"] = float(row.get("dinein_net_sales", 0) or 0)
        ch["DeliveryNet"] = float(row.get("delivery_net_sales", 0) or 0)
        ch["CurbSide"] = float(row.get("curbside_net_sales", 0) or 0) + float(row.get("curbside_olo_net_sales", 0) or 0)
        ch["CheckCount"] = float(row.get("check_count", 0) or 0)
        ch["NonCateringChecks"] = float(row.get("checks_minus_catering", 0) or 0)
        ch["NonCatering"] = ch["Net"] - ch["Catering"]
        ch["_orders"] = ch["CheckCount"]
        ch["_guests"] = 0.0
        result[loc_id] = ch
    return result


@app.post("/api/daily-flash-report")
async def run_daily_flash_report(payload: dict = Body(...)):
    """
    Run the Daily Sales Flash Report using Toast ERA API.
    Expects: {"report_date": "YYYY-MM-DD", "locations": [...] (optional)}
    Returns 5 periods: Daily, WTD, PTD, QTD, YTD with 14 channels + company grouping.
    """
    report_date_str = payload.get("report_date")
    if not report_date_str:
        raise HTTPException(status_code=400, detail="report_date required (YYYY-MM-DD)")
    try:
        report_date = date.fromisoformat(report_date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="report_date must be YYYY-MM-DD")

    # Get fiscal calendar from local SQLite
    if not aloha_sales_db.fiscal_calendar_available():
        raise HTTPException(status_code=400, detail="Fiscal calendar not loaded — import it first")

    conn = aloha_sales_db.get_connection()
    try:
        cal_row = conn.execute(
            "SELECT fiscal_year, fiscal_period, fiscal_week FROM fiscal_calendar WHERE date = ?",
            (report_date_str,)
        ).fetchone()
        if not cal_row:
            raise HTTPException(status_code=400, detail=f"No fiscal calendar entry for {report_date_str}")
        fy, fp, fw = cal_row

        # CY date ranges
        weekday = report_date.weekday()
        wtd_start = report_date - timedelta(days=weekday)

        ptd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
            (fy, fp)
        ).fetchone()
        ptd_start = date.fromisoformat(ptd_row[0]) if ptd_row and ptd_row[0] else wtd_start

        q_start_period = ((fp - 1) // 3) * 3 + 1
        qtd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
            (fy, q_start_period)
        ).fetchone()
        qtd_start = date.fromisoformat(qtd_row[0]) if qtd_row and qtd_row[0] else ptd_start

        ytd_row = conn.execute(
            "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ?", (fy,)
        ).fetchone()
        ytd_start = date.fromisoformat(ytd_row[0]) if ytd_row and ytd_row[0] else qtd_start
    finally:
        conn.close()

    # PY date ranges — use fiscal calendar LY mapping
    def _get_ly_range(cy_start: date, cy_end: date, py_fy: int, py_period: int) -> Tuple[str, str]:
        ly_map = aloha_sales_db.get_fiscal_ly_dates(cy_start.isoformat(), cy_end.isoformat())
        if ly_map:
            ly_dates = sorted(ly_map.values())
            return ly_dates[0], ly_dates[-1]
        conn2 = aloha_sales_db.get_connection()
        try:
            py_row = conn2.execute(
                "SELECT MIN(date) FROM fiscal_calendar WHERE fiscal_year = ? AND fiscal_period = ?",
                (py_fy, py_period)
            ).fetchone()
            if py_row and py_row[0]:
                py_start = date.fromisoformat(py_row[0])
                py_end = py_start + timedelta(days=(cy_end - cy_start).days)
                return py_start.isoformat(), py_end.isoformat()
        finally:
            conn2.close()
        return (cy_start - timedelta(days=364)).isoformat(), (cy_end - timedelta(days=364)).isoformat()

    daily_ly = _get_ly_range(report_date, report_date, fy - 1, fp)
    wtd_ly = _get_ly_range(wtd_start, report_date, fy - 1, fp)
    ptd_ly = _get_ly_range(ptd_start, report_date, fy - 1, fp)
    qtd_ly = _get_ly_range(qtd_start, report_date, fy - 1, q_start_period)
    ytd_ly = _get_ly_range(ytd_start, report_date, fy - 1, 1)

    # Get all restaurant GUIDs and mappings
    async with httpx.AsyncClient(timeout=60.0) as client:
        all_locations = await get_all_active_locations(client)
    guid_to_loc = _get_flash_loc_mapping()
    restaurant_ids = [loc.restaurantGuid for loc in all_locations]
    restaurant_ids = _ensure_all_mapped_guids(restaurant_ids)

    # Filter locations if requested (by Aloha ID)
    requested_locs = payload.get("locations")
    if requested_locs:
        loc_set = set(requested_locs)
        restaurant_ids = [rid for rid in restaurant_ids if guid_to_loc.get(rid) in loc_set]

    store_info = aloha_sales_db.get_store_info()

    # Collect all unique date ranges we need to query
    periods_config = {
        "Daily": (report_date, report_date, daily_ly[0], daily_ly[1]),
        "WTD": (wtd_start.isoformat(), report_date_str, wtd_ly[0], wtd_ly[1]),
        "PTD": (ptd_start.isoformat(), report_date_str, ptd_ly[0], ptd_ly[1]),
        "QTD": (qtd_start.isoformat(), report_date_str, qtd_ly[0], qtd_ly[1]),
        "YTD": (ytd_start.isoformat(), report_date_str, ytd_ly[0], ytd_ly[1]),
    }

    # Normalize date strings
    all_ranges = set()
    for pname, (cs, ce, ps, pe) in periods_config.items():
        cs_str = cs.isoformat() if isinstance(cs, date) else cs
        ce_str = ce.isoformat() if isinstance(ce, date) else ce
        ps_str = ps.isoformat() if isinstance(ps, date) else ps
        pe_str = pe.isoformat() if isinstance(pe, date) else pe
        periods_config[pname] = (cs_str, ce_str, ps_str, pe_str)
        all_ranges.add((cs_str, ce_str))
        all_ranges.add((ps_str, pe_str))

    # Compute Aloha location IDs for the selected restaurants
    aloha_loc_ids = [guid_to_loc[rid] for rid in restaurant_ids if rid in guid_to_loc]

    # Also include non-Toast stores from FBC mapping (e.g. Lubbock still on Aloha POS)
    fbc_mapping = _load_fbc_mapping()
    for loc_id in fbc_mapping.keys():
        if loc_id in store_info and loc_id not in aloha_loc_ids:
            aloha_loc_ids.append(loc_id)
            logger.info("Added non-Toast store %s to aloha_loc_ids for flash query", loc_id)

    # For each unique date range: try Aloha sales DB first (has correct Newk's net sales
    # with service charges subtracted + GNG data), fall back to Toast ERA API
    unique_ranges = {}
    range_source = {}
    for (s, e) in all_ranges:
        aloha_data = _aggregate_flash_from_aloha(s, e, locations=aloha_loc_ids if aloha_loc_ids else None)
        if aloha_data:
            logger.info("Flash data from Aloha DB: %s to %s (%d locations)", s, e, len(aloha_data))
            unique_ranges[(s, e)] = aloha_data
            range_source[(s, e)] = "aloha_db"
        else:
            logger.info("No Aloha DB data for %s to %s — using Toast ERA API", s, e)
            raw = await _fetch_flash_era_data(
                date.fromisoformat(s), date.fromisoformat(e), restaurant_ids
            )
            unique_ranges[(s, e)] = _aggregate_flash_by_location(raw, guid_to_loc)
            range_source[(s, e)] = "toast_api"

    # Load FBC grouping
    fbc_mapping = _load_fbc_mapping()

    # Build periods from cached data
    periods = {}
    for pname, (cs, ce, ps, pe) in periods_config.items():
        periods[pname] = _build_flash_period_from_api(
            unique_ranges[(cs, ce)], unique_ranges[(ps, pe)],
            cs, ce, ps, pe, store_info, fbc_mapping,
        )
        periods[pname]["dataSource"] = {
            "cy": range_source.get((cs, ce), "toast_api"),
            "py": range_source.get((ps, pe), "toast_api"),
        }

    _flash_channel_order = _FLASH_CHANNEL_KEYS + ["CheckAverage", "POS2"]

    # Collect unique FBC groups from all locations
    fbc_groups = sorted(set(fbc_mapping.values())) if fbc_mapping else []

    return {
        "reportDate": report_date_str,
        "fiscalYear": fy,
        "fiscalPeriod": fp,
        "fiscalWeek": fw,
        "periods": periods,
        "channelOrder": _flash_channel_order,
        "channelLabels": _FLASH_CHANNEL_LABELS,
        "fbcGroups": fbc_groups,
    }


@app.post("/api/yoy/upload-aloha-sales-db")
async def upload_aloha_sales_db(payload: dict = Body(...)):
    """
    Upload the aloha_sales.db SQLite file as base64-encoded JSON.
    Expects: {"db_base64": "<base64 string>"}
    Overwrites the existing aloha_sales.db on the server.
    """
    import base64 as _b64
    db_b64 = payload.get("db_base64")
    if not db_b64:
        raise HTTPException(status_code=400, detail="Missing db_base64 field")
    try:
        db_bytes = _b64.b64decode(db_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")
    if len(db_bytes) < 100:
        raise HTTPException(status_code=400, detail="File too small to be a valid SQLite DB")

    db_path = aloha_sales_db.ALOHA_SALES_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Remove WAL/SHM files so SQLite doesn't use stale journal after overwrite
    for suffix in ("-wal", "-shm"):
        wal_path = db_path + suffix
        if os.path.exists(wal_path):
            os.remove(wal_path)

    with open(db_path, "wb") as f:
        f.write(db_bytes)

    # Verify with a fresh connection
    stats = aloha_sales_db.get_stats(db_path)
    logger.info("Aloha sales DB uploaded: %s", stats)
    return {"success": True, "size_mb": round(len(db_bytes) / (1024 * 1024), 2), **stats}


@app.post("/api/yoy/compare")
async def compare_yoy(request: YoYCompareRequest = Body(...)):
    """
    Compare Toast period vs Aloha historical data.
    """
    try:
        toast_start = date.fromisoformat(request.toast_start_date)
        toast_end = date.fromisoformat(request.toast_end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="toast_start_date/toast_end_date must be YYYY-MM-DD")
    
    if toast_end < toast_start:
        raise HTTPException(status_code=400, detail="toast_end_date must be on or after toast_start_date")
    
    try:
        aloha_date_obj = date.fromisoformat(request.aloha_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="aloha_date must be YYYY-MM-DD")
    
    aloha_file_path = get_aloha_file_path(aloha_date_obj.isoformat())
    
    consolidator = get_yoy_consolidator()
    hierarchy_gen = HierarchicalReportGenerator(consolidator)
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Get restaurant IDs
        restaurant_ids = request.restaurantIds
        if not restaurant_ids:
            locations = await get_all_active_locations(client)
            restaurant_ids = [loc.restaurantGuid for loc in locations]
        
        if not restaurant_ids:
            raise HTTPException(status_code=400, detail="No restaurants available")
        
        # Fetch Toast menu report
        report_guid = await request_toast_report(
            client,
            toast_start,
            toast_end,
            restaurant_ids,
            "menu",
            ["MENU_ITEM"]
        )
        toast_raw = await fetch_toast_report(client, report_guid, "menu")
        if not toast_raw:
            raise HTTPException(status_code=404, detail="No Toast data returned for the selected period")
        
        toast_df = pd.DataFrame(toast_raw)
        
        # Load Aloha data
        aloha_df = await asyncio.to_thread(consolidator.load_aloha_data, aloha_file_path)
        
        # Consolidate Toast to Aloha item level (no location/date breakdown)
        toast_consolidated = await asyncio.to_thread(
            consolidator.consolidate_toast_data,
            toast_df,
            "menuItemGuid",
            "quantitySold",
            "netSalesAmount",
            None,
            None
        )
        
        # Compare periods
        aloha_label = aloha_date_obj.strftime("%b %Y")
        toast_label = toast_start.strftime("%b %Y") if toast_start.month == toast_end.month else f"{toast_start} to {toast_end}"
        comparison = await asyncio.to_thread(
            consolidator.compare_yoy,
            aloha_df,
            toast_consolidated,
            aloha_label,
            toast_label
        )
        
        # Generate hierarchical report
        hierarchy = await asyncio.to_thread(
            hierarchy_gen.generate_hierarchical_comparison,
            comparison,
            aloha_label,
            toast_label
        )
        
        return {
            "success": True,
            "aloha_period": aloha_date_obj.isoformat(),
            "toast_period": f"{toast_start} to {toast_end}",
            "category_summary": hierarchy["category_summary"].to_dict("records"),
            "hierarchy": hierarchy["hierarchy"],
            "period_labels": {
                "aloha": aloha_label,
                "toast": toast_label
            }
        }

# ------------------
# Help Desk Integration
# ------------------

@app.post("/api/help/ticket")
async def create_help_ticket(ticket: HelpTicketRequest = Body(...)):
    """Create a help desk ticket via Slack webhook"""
    
    if not SLACK_WEBHOOK_URL:
        raise HTTPException(
            status_code=500,
            detail="Slack integration not configured. Please set SLACK_WEBHOOK_URL environment variable."
        )
    
    # Map priority values to emoji and text
    priority_map = {
        1: ("🟢", "Low"),
        2: ("🟡", "Medium"),
        3: ("🟠", "High"),
        4: ("🔴", "Urgent")
    }
    priority_emoji, priority_text = priority_map.get(ticket.priority, ("🟡", "Medium"))
    
    # Check if this is a workflow webhook (contains /workflows/) or regular webhook
    # For workflow webhooks, send raw data. For regular webhooks, send formatted blocks.
    is_workflow = "/workflows/" in SLACK_WEBHOOK_URL or "workflow" in SLACK_WEBHOOK_URL.lower()
    
    if is_workflow:
        # Send raw data for workflow to format
        # Slack workflows can access these as variables: {{name}}, {{email}}, etc.
        slack_message = {
            "name": ticket.name,
            "email": ticket.email,
            "subject": ticket.subject,
            "priority": priority_text,
            "priority_emoji": priority_emoji,
            "priority_value": str(ticket.priority),  # 1, 2, 3, or 4
            "description": ticket.description,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            "source": "Newk's Reporting Platform"
        }
    else:
        # Format Slack message with blocks for direct webhook
        slack_message = {
            "text": f"{priority_emoji} New Help Request from {ticket.name}",  # Fallback for notifications
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{priority_emoji} New Help Request - {priority_text} Priority",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*👤 Name:*\n{ticket.name}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*📧 Email:*\n<mailto:{ticket.email}|{ticket.email}>"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*⚡ Priority:*\n{priority_text} {priority_emoji}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*📋 Subject:*\n{ticket.subject}"
                        }
                    ]
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*📝 Description:*\n```{ticket.description}```"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"🕐 Submitted from Newk's Reporting Platform • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    ]
                }
            ]
        }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try sending with blocks first
            resp = await client.post(SLACK_WEBHOOK_URL, json=slack_message)
            
            # Log the response for debugging
            logger.info(f"Slack webhook response: {resp.status_code}")
            if resp.status_code != 200:
                logger.info(f"Response body: {resp.text[:500]}")
            
            if resp.status_code == 200:
                logger.info(f"Sent help ticket to Slack for {ticket.email}")
                return {
                    "success": True,
                    "ticketId": f"SLACK-{int(time.time())}",
                    "message": "Help request submitted successfully. Our team will respond soon!"
                }
            else:
                # If blocks fail, try simple text format as fallback
                logger.warning(f"Block format failed, trying simple text format")
                simple_message = {
                    "text": f"{priority_emoji} *New Help Request*\n\n*Name:* {ticket.name}\n*Email:* {ticket.email}\n*Priority:* {priority_text} ({priority_emoji})\n*Subject:* {ticket.subject}\n\n*Description:*\n{ticket.description}\n\n_Submitted from Newk's Reporting Platform at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
                }
                resp2 = await client.post(SLACK_WEBHOOK_URL, json=simple_message)
                
                if resp2.status_code == 200:
                    logger.info(f"Sent help ticket to Slack (simple format) for {ticket.email}")
                    return {
                        "success": True,
                        "ticketId": f"SLACK-{int(time.time())}",
                        "message": "Help request submitted successfully. Our team will respond soon!"
                    }
                
                error_msg = resp.text or resp2.text
                logger.error(f"Failed to send Slack message: {resp.status_code} / {resp2.status_code} - {error_msg}")
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Failed to submit request: {error_msg}"
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending Slack message: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error submitting request: {str(e)}")

# ------------------
# Tip Pool Report Helpers
# ------------------

# Only include these job titles in the tip pool
_TIP_POOL_JOBS = {
    "dish", "shift lead", "prep", "server", "salad", "pizza",
    "sandwich", "catering team leader", "training boh", "dra",
    "training foh", "expo", "certified trainer", "utility",
}


def _sum_tips_from_orders(orders: List[Dict]) -> float:
    """Sum valid credit-card tip amounts from a list of orders."""
    total = 0.0
    for order in orders:
        if order.get("voided") or order.get("deleted"):
            continue
        for check in order.get("checks") or []:
            if check.get("voided") or check.get("deleted"):
                continue
            for pmt in check.get("payments") or []:
                tip = float(pmt.get("tipAmount") or 0)
                if tip == 0:
                    continue
                pmt_voided = pmt.get("voided", False)
                pmt_status = (pmt.get("paymentStatus") or "").upper()
                refund_status = (pmt.get("refundStatus") or "").upper()
                original_pmt = pmt.get("originalMapPayment")
                if pmt_voided or pmt_status in ("VOIDED", "DENIED") or refund_status in ("SUCCESSFUL",) or original_pmt:
                    continue
                total += tip
    return total


async def _fetch_tips_for_restaurant(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    start_date: date,
    end_date: date,
) -> tuple:
    """
    Sum credit-card tipAmount across all orders for one restaurant over a date range.
    Returns (total_tips: float, tips_by_day: {date_iso: float}).
    """
    total_tips = 0.0
    tips_by_day: Dict[str, float] = {}
    d = start_date
    while d <= end_date:
        day_str = d.isoformat()
        bdate = to_business_date(d)
        orders = await _fetch_orders_for_restaurant_day(client, restaurant_guid, bdate)
        day_tips = _sum_tips_from_orders(orders)
        tips_by_day[day_str] = day_tips
        total_tips += day_tips
        d += timedelta(days=1)
        if d <= end_date:
            await asyncio.sleep(0.12)
    logger.info(
        f"Tip pool tips: restaurant={restaurant_guid[:8]}, "
        f"total=${total_tips:.2f}, days={len(tips_by_day)}"
    )
    return total_tips, tips_by_day


async def _fetch_employee_configs(
    client: httpx.AsyncClient,
    restaurant_guid: str,
) -> Dict[str, str]:
    """Fetch employee guid -> 'FirstName LastName' mapping from Toast config API.
    Tries multiple endpoint paths since different API scopes expose different paths."""
    standard_headers = await get_standard_api_auth_headers()
    headers = {**standard_headers, "Toast-Restaurant-External-ID": restaurant_guid}
    emp_map: Dict[str, str] = {}

    # Try multiple endpoint paths — different Toast API versions/scopes
    endpoints = [
        f"{TOAST_STANDARD_API_BASE_URL}/config/v2/employees",
        f"{TOAST_STANDARD_API_BASE_URL}/config/v1/employees",
        f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/employees",
    ]
    for url in endpoints:
        try:
            resp = await client.get(url, headers=headers, timeout=60.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    logger.info(f"Employees resolved via {url} ({len(data)} found), sample keys: {list(data[0].keys())[:10]}")
                    for emp in data:
                        guid = emp.get("guid", "")
                        first = (emp.get("firstName") or "").strip()
                        last = (emp.get("lastName") or "").strip()
                        name = f"{first} {last}".strip()
                        if not name:
                            name = (emp.get("name") or emp.get("displayName") or "").strip()
                        ext_id = (emp.get("externalEmployeeId") or emp.get("externalId") or "").strip()
                        emp_map[guid] = {"name": name or guid[:8], "externalId": ext_id}
                    return emp_map
            else:
                logger.debug(f"Employees endpoint {url} returned {resp.status_code}")
        except Exception as exc:
            logger.debug(f"Employees endpoint {url} error: {exc}")

    logger.warning(f"Could not resolve employee names for {restaurant_guid[:8]} (all endpoints failed)")
    return emp_map


async def _fetch_job_configs(
    client: httpx.AsyncClient,
    restaurant_guid: str,
) -> Dict[str, str]:
    """Fetch job guid -> job title mapping from Toast config API.
    Tries multiple endpoint paths since different API scopes expose different paths."""
    standard_headers = await get_standard_api_auth_headers()
    headers = {**standard_headers, "Toast-Restaurant-External-ID": restaurant_guid}
    job_map: Dict[str, str] = {}

    # Try multiple endpoint paths
    endpoints = [
        f"{TOAST_STANDARD_API_BASE_URL}/config/v2/jobs",
        f"{TOAST_STANDARD_API_BASE_URL}/config/v1/jobs",
        f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/jobs",
    ]
    for url in endpoints:
        try:
            resp = await client.get(url, headers=headers, timeout=60.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    logger.info(f"Jobs resolved via {url} ({len(data)} found), sample keys: {list(data[0].keys())[:10]}")
                    for job in data:
                        guid = job.get("guid", "")
                        title = (job.get("title") or job.get("name") or job.get("displayName") or "").strip()
                        job_map[guid] = title or guid[:8]
                    return job_map
            else:
                logger.debug(f"Jobs endpoint {url} returned {resp.status_code}")
        except Exception as exc:
            logger.debug(f"Jobs endpoint {url} error: {exc}")

    logger.warning(f"Could not resolve job titles for {restaurant_guid[:8]} (all endpoints failed)")
    return job_map


async def _fetch_employee_hours(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    start_date: date,
    end_date: date,
    emp_map: Dict[str, str],
    job_map: Dict[str, str],
) -> List[Dict]:
    """Fetch per-employee hours from labor API, filtered to tip pool jobs only. Returns list of dicts."""
    standard_headers = await get_standard_api_auth_headers()
    headers = {**standard_headers, "Toast-Restaurant-External-ID": restaurant_guid}
    labor_url = f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/timeEntries"

    # Keyed by (employee_guid, job_guid) so an employee who works multiple
    # tip-pool-eligible jobs in the same period gets a separate row for each.
    by_employee: Dict[tuple, Dict] = {}
    hours_by_day: Dict[str, float] = {}  # date_iso -> total hours

    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    end_dt = datetime(end_date.year, end_date.month, end_date.day)

    # Debug counters
    total_entries = 0
    skipped_clocked_in = 0
    skipped_non_server = 0
    jobs_seen: Dict[str, int] = {}

    logger.info(
        f"Tip pool labor: restaurant={restaurant_guid[:8]}, "
        f"emp_map_size={len(emp_map)}, job_map_size={len(job_map)}, "
        f"job_map_sample={dict(list(job_map.items())[:5])}"
    )

    chunk_start = start_dt
    while chunk_start <= end_dt:
        chunk_end = min(chunk_start + timedelta(days=29), end_dt)
        params = {
            "startDate": chunk_start.strftime("%Y-%m-%dT00:00:00.000+0000"),
            "endDate": chunk_end.strftime("%Y-%m-%dT23:59:59.999+0000"),
        }
        try:
            resp = await client.get(labor_url, headers=headers, params=params, timeout=60.0)
            if resp.status_code != 200:
                logger.warning(f"Tip pool labor API {resp.status_code} for {restaurant_guid[:8]}: {resp.text[:200]}")
            else:
                entries = resp.json()
                if not isinstance(entries, list):
                    entries = entries.get("timeEntries", []) if isinstance(entries, dict) else []
                total_entries += len(entries)
                # Log first entry for debugging field structure
                if entries and total_entries == len(entries):
                    sample = entries[0]
                    logger.info(
                        f"Tip pool sample time entry keys: {list(sample.keys())}, "
                        f"employeeReference={sample.get('employeeReference')}, "
                        f"jobReference={sample.get('jobReference')}"
                    )
                for entry in entries:
                    if entry.get("outDate") is None:
                        skipped_clocked_in += 1
                        continue
                    emp_ref = entry.get("employeeReference") or {}
                    job_ref = entry.get("jobReference") or {}
                    emp_guid = emp_ref.get("guid", "unknown")
                    job_guid = job_ref.get("guid", "")
                    job_title = job_map.get(job_guid) or job_ref.get("externalId") or "Unknown"
                    # Only include tip pool eligible jobs (Server)
                    jobs_seen[job_title] = jobs_seen.get(job_title, 0) + 1
                    if job_title.lower().strip() not in _TIP_POOL_JOBS:
                        skipped_non_server += 1
                        continue
                    emp_info = emp_map.get(emp_guid) or {}
                    if isinstance(emp_info, str):
                        emp_info = {"name": emp_info, "externalId": ""}
                    emp_name = emp_info.get("name") or emp_ref.get("externalId") or emp_guid[:8]
                    emp_ext_id = emp_info.get("externalId") or ""
                    reg_h = float(entry.get("regularHours") or 0)
                    ot_h = float(entry.get("overtimeHours") or 0)
                    wage = entry.get("hourlyWage")  # None for salaried employees
                    emp_job_key = (emp_guid, job_guid)
                    if emp_job_key not in by_employee:
                        by_employee[emp_job_key] = {
                            "employeeName": emp_name,
                            "employeeId": emp_ext_id,
                            "jobTitle": job_title,
                            "regularHours": 0.0,
                            "overtimeHours": 0.0,
                            "hourlyWage": None,
                        }
                    by_employee[emp_job_key]["regularHours"] += reg_h
                    by_employee[emp_job_key]["overtimeHours"] += ot_h
                    # Keep the most recent non-null wage seen for this employee+job
                    if wage is not None:
                        by_employee[emp_job_key]["hourlyWage"] = float(wage)
                    # Track hours by day using inDate
                    in_date_raw = entry.get("inDate") or ""
                    try:
                        day_key = in_date_raw[:10]  # "YYYY-MM-DD"
                        if day_key not in hours_by_day:
                            hours_by_day[day_key] = 0.0
                        hours_by_day[day_key] += reg_h + ot_h
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning(f"Tip pool labor exception for {restaurant_guid[:8]}: {exc}", exc_info=True)
        chunk_start = chunk_end + timedelta(days=1)

    logger.info(
        f"Tip pool labor results: restaurant={restaurant_guid[:8]}, "
        f"total_entries={total_entries}, skipped_clocked_in={skipped_clocked_in}, "
        f"skipped_non_server={skipped_non_server}, all_jobs={jobs_seen}, "
        f"unique_employees={len(by_employee)}"
    )

    results = []
    for emp_guid, data in by_employee.items():
        reg = round(data["regularHours"], 2)
        ot = round(data["overtimeHours"], 2)
        total_h = round(reg + ot, 2)
        if total_h > 0:
            results.append({
                "employeeId": data.get("employeeId", ""),
                "employeeName": data["employeeName"],
                "jobTitle": data["jobTitle"],
                "regularHours": reg,
                "overtimeHours": ot,
                "hoursWorked": total_h,
                "payRate": round(data["hourlyWage"], 2) if data.get("hourlyWage") is not None else None,
            })
    return results, hours_by_day


async def build_tip_pool_report(
    client: httpx.AsyncClient,
    report_config: "ReportConfig",
    restaurant_ids: List[str],
    start_date: date,
    end_date: date,
    locations=None,
) -> Dict:
    """Build the weekly tip pool report across selected restaurants."""
    # Resolve location names for display
    loc_name_map: Dict[str, str] = {}
    if locations:
        for loc in locations:
            loc_name_map[loc.restaurantGuid] = loc.restaurantName
    else:
        try:
            all_locs = await get_all_active_locations(client)
            for loc in all_locs:
                loc_name_map[loc.restaurantGuid] = loc.restaurantName
        except Exception:
            pass

    grand_total_tips = 0.0
    all_employees: List[Dict] = []
    per_location: List[Dict] = []
    errors: List[str] = []

    # Process all restaurants in parallel (max 5 at a time to avoid Toast API rate limits)
    sem = asyncio.Semaphore(5)

    async def _process_one(restaurant_guid: str):
        loc_name = loc_name_map.get(restaurant_guid, restaurant_guid[:8])
        async with sem:
            try:
                # Fetch employee configs, job configs, AND tips all in parallel
                # (tips don't need emp/job maps)
                emp_map, job_map, tips_result = await asyncio.gather(
                    _fetch_employee_configs(client, restaurant_guid),
                    _fetch_job_configs(client, restaurant_guid),
                    _fetch_tips_for_restaurant(client, restaurant_guid, start_date, end_date),
                )
                tips, tips_by_day = tips_result
                # Hours need emp/job maps — fetch after
                employees, hours_by_day = await _fetch_employee_hours(
                    client, restaurant_guid, start_date, end_date, emp_map, job_map
                )
                total_loc_hours = sum(e["hoursWorked"] for e in employees)
                loc_rate = round(tips / total_loc_hours, 4) if total_loc_hours > 0 else 0.0
                loc_tip_share_total = 0.0
                for emp in employees:
                    emp["restaurantName"] = loc_name
                    emp["restaurantGuid"] = restaurant_guid
                    emp["tipShare"] = round(emp["hoursWorked"] * loc_rate, 2)
                    loc_tip_share_total += emp["tipShare"]
                return {
                    "tips": tips,
                    "tips_by_day": tips_by_day,
                    "hours_by_day": hours_by_day,
                    "employees": employees,
                    "location": {
                        "restaurantName": loc_name,
                        "restaurantGuid": restaurant_guid,
                        "totalTips": round(tips, 2),
                        "totalHours": round(total_loc_hours, 2),
                        "hourlyRate": loc_rate,
                        "tipShareTotal": round(loc_tip_share_total, 2),
                    },
                    "error": None,
                }
            except Exception as exc:
                logger.warning(f"Tip pool error for {loc_name}: {exc}", exc_info=True)
                return {"tips": 0.0, "tips_by_day": {}, "hours_by_day": {}, "employees": [], "location": None, "error": f"{loc_name}: {str(exc)}"}

    results = await asyncio.gather(*[_process_one(guid) for guid in restaurant_ids])

    # Aggregate across restaurants — merge daily tips and hours
    combined_tips_by_day: Dict[str, float] = {}
    combined_hours_by_day: Dict[str, float] = {}

    for r in results:
        if r["error"]:
            errors.append(r["error"])
        else:
            grand_total_tips += r["tips"]
            all_employees.extend(r["employees"])
            if r["location"]:
                per_location.append(r["location"])
            for day, t in r.get("tips_by_day", {}).items():
                combined_tips_by_day[day] = combined_tips_by_day.get(day, 0.0) + t
            for day, h in r.get("hours_by_day", {}).items():
                combined_hours_by_day[day] = combined_hours_by_day.get(day, 0.0) + h

    # Build tipPoolByDay — all days in range, sorted ascending
    all_days = sorted(set(list(combined_tips_by_day.keys()) + list(combined_hours_by_day.keys())))
    tip_pool_by_day = []
    for day in all_days:
        day_tips = round(combined_tips_by_day.get(day, 0.0), 2)
        day_hours = round(combined_hours_by_day.get(day, 0.0), 2)
        day_rate = round(day_tips / day_hours, 4) if day_hours > 0 else 0.0
        tip_pool_by_day.append({
            "date": day,
            "totalTips": day_tips,
            "totalHours": day_hours,
            "hourlyRate": day_rate,
        })

    # Calculate totals (employees already carry per-location tipShare)
    grand_total_hours = sum(e["hoursWorked"] for e in all_employees)
    grand_total_regular = sum(e["regularHours"] for e in all_employees)
    grand_total_overtime = sum(e["overtimeHours"] for e in all_employees)
    grand_total_tip_share = sum(e["tipShare"] for e in all_employees)

    # Sort by location, then by hours descending within each location
    all_employees.sort(key=lambda e: (e.get("restaurantName", ""), -e["hoursWorked"]))

    # When a single location is selected, the blended rate equals that location's rate.
    # Across multiple locations there is no single rate — surface a weighted average so
    # the UI has something to display, but the truth lives in tipPoolByLocation.
    blended_rate = round(grand_total_tips / grand_total_hours, 4) if grand_total_hours > 0 else 0.0

    logger.info(
        f"Tip pool report (per-location pools): {len(restaurant_ids)} locations, "
        f"{start_date}–{end_date}, tips=${grand_total_tips:.2f}, hours={grand_total_hours:.2f}, "
        f"employees={len(all_employees)}, errors={errors}"
    )

    return {
        "tipPoolSummary": {
            "totalTips": round(grand_total_tips, 2),
            "totalHours": round(grand_total_hours, 2),
            "hourlyRate": blended_rate,
            "perLocationPool": True,
            "locationCount": len(per_location),
        },
        "tipPoolTable": all_employees,
        "tipPoolTotals": {
            "hoursWorked": round(grand_total_hours, 2),
            "regularHours": round(grand_total_regular, 2),
            "overtimeHours": round(grand_total_overtime, 2),
            "tipShare": round(grand_total_tip_share, 2),
        },
        "tipPoolByLocation": per_location,
        "tipPoolByDay": tip_pool_by_day,
        "errors": errors,
    }


# ------------------
# Labor Summary Endpoint
# ------------------

@app.post("/api/labor-summary")
async def get_labor_summary(
    req: LaborSummaryRequest,
    current_user: User = Depends(get_current_user),
):
    """Fetch aggregated labor cost + hours from Toast Labor API for a date range.

    Uses the Toast Standard API /labor/v1/timeEntries endpoint (one call per restaurant).
    Overtime is calculated at 1.5x. Salaried employees (hourlyWage=null) contribute to
    hours totals but are excluded from cost (wage not available via API).
    Toast Labor API caps requests at 30 days; wider ranges are automatically chunked.
    """
    start_dt = datetime.strptime(req.startDate, "%Y-%m-%d")
    end_dt   = datetime.strptime(req.endDate,   "%Y-%m-%d")

    total_cost      = 0.0
    total_reg_hours = 0.0
    total_ot_hours  = 0.0
    errors: List[str] = []

    standard_headers = await get_standard_api_auth_headers()
    labor_url = f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/timeEntries"

    async with httpx.AsyncClient(timeout=60.0) as client:
        for restaurant_guid in req.restaurantIds:
            headers = {
                **standard_headers,
                "Toast-Restaurant-External-ID": restaurant_guid,
            }
            # Toast Labor API caps at 30 days per request — chunk wider ranges
            chunk_start = start_dt
            while chunk_start <= end_dt:
                chunk_end = min(chunk_start + timedelta(days=29), end_dt)
                params = {
                    "startDate": chunk_start.strftime("%Y-%m-%dT00:00:00.000+0000"),
                    "endDate":   chunk_end.strftime("%Y-%m-%dT23:59:59.999+0000"),
                }
                try:
                    resp = await client.get(labor_url, headers=headers, params=params, timeout=60.0)
                    if resp.status_code != 200:
                        errors.append(f"{restaurant_guid}: HTTP {resp.status_code}")
                        logger.warning(f"Labor API {resp.status_code} for {restaurant_guid}: {resp.text[:200]}")
                    else:
                        entries = resp.json()
                        if not isinstance(entries, list):
                            entries = entries.get("timeEntries", []) if isinstance(entries, dict) else []
                        for entry in entries:
                            # Skip still-clocked-in shifts (outDate is null)
                            if entry.get("outDate") is None:
                                continue
                            reg_h = float(entry.get("regularHours")  or 0)
                            ot_h  = float(entry.get("overtimeHours") or 0)
                            wage  = entry.get("hourlyWage")   # None for salaried employees
                            total_reg_hours += reg_h
                            total_ot_hours  += ot_h
                            if wage is not None:   # hourly only; salaried excluded from cost
                                total_cost += reg_h * float(wage) + ot_h * float(wage) * 1.5
                except Exception as exc:
                    errors.append(f"{restaurant_guid}: {str(exc)}")
                    logger.warning(f"Labor API exception for {restaurant_guid}: {exc}")
                chunk_start = chunk_end + timedelta(days=1)

    logger.info(
        f"Labor summary: {len(req.restaurantIds)} locations, {req.startDate}–{req.endDate}, "
        f"cost=${total_cost:.2f}, hours={total_reg_hours + total_ot_hours:.2f}, errors={errors}"
    )
    return {
        "totalLaborCost":     round(total_cost, 2),
        "totalHours":         round(total_reg_hours + total_ot_hours, 2),
        "totalRegularHours":  round(total_reg_hours, 2),
        "totalOvertimeHours": round(total_ot_hours,  2),
        "errors": errors,
    }

# ------------------
# Static Frontend
# ------------------

# ------------------
# Initialize Super Admin (after all functions are defined)
# ------------------

# Initialize super admin on startup (after truncate_password_to_72_bytes is defined)
initialize_super_admin()

# ------------------
# Menuboard API Proxy → Express server on port 8787
# ------------------

MENUBOARD_API_URL = os.getenv("MENUBOARD_API_URL", "http://localhost:8787")

async def _proxy_to_menuboard(request: Request, path: str):
    """Forward a request to the menuboard Express server."""
    url = f"{MENUBOARD_API_URL}/{path.lstrip('/')}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding")}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=await request.body(),
                params=request.query_params,
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")},
            media_type=resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Menuboard API server unavailable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Menuboard API server timeout")

@app.api_route("/api/board-config/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_board_config(request: Request, rest: str):
    return await _proxy_to_menuboard(request, f"api/board-config/{rest}")

@app.api_route("/api/toast/{rest:path}", methods=["GET", "POST"])
async def proxy_toast_api(request: Request, rest: str):
    return await _proxy_to_menuboard(request, f"api/toast/{rest}")

@app.get("/api/menu")
async def proxy_menu_api(request: Request):
    return await _proxy_to_menuboard(request, "api/menu")

@app.get("/api/debug/menu")
async def proxy_debug_menu(request: Request):
    return await _proxy_to_menuboard(request, "api/debug/menu")

# ------------------------------------------------------------------
# Menu Mix Performance Report  (Excel download)
# ------------------------------------------------------------------

# Recipe tags – items marked Y in the Recipe Tags CSV.
# These are the specific recipe-level tags (Caesar, BlackBleu, etc.)
_RECIPE_TAGS: set = {
    "Caesar", "Favorite", "Garden", "Ultimate", "Greek", "Cobb",
    "StrawberryFields", "GreenGoddess", "ChickenPesto", "Steakhouse",
    "SalmonCaesar", "ShrimpAvo", "Southern", "BlackBleu", "Chef",
    "ChickenCaesar", "SummerBerry", "ChickenAsianSalad", "ChiliTacoSalad",
    "ShrimpCajunSalad", "PestoPastaCaesar",
    "CakeStrawberry", "CakeChocolate", "CakeCheesecakeNY", "CakeCaramel",
    "CakeRedVelvet", "CakeCoconut", "CakeBananaNut", "CakeLemon",
    "CakeMintChocolateChip", "CakePeanutButterChocolate", "CakePumpkin",
    "ChickenSalad", "Club", "NewksQ", "ChickenBaconSpicy", "ShrimpSpicy",
    "MacCheese", "MacCheesePimiento", "MacCheeseKids", "MacCheeseChili",
    "ChknSaladCrois", "TurkeyCrois", "HamCrois", "ClubWheat", "ClubCrois",
    "TurkeyAvo", "ChickenBaconClub", "Italian", "FrenchDip", "TurkeyBreast",
    "ChickenPestoGrilled", "NewksPhilly", "Ham", "SteakQ", "ShrimpPoBoy",
    "RoastBeef", "TurkeyKids", "PortabellaVeg", "GrilledChsKids",
    "DoubleClub", "GrilledChsAdult", "HamKids", "NewksCheesesteak",
    "TunaSalad", "TunaMelt",
    "SoupLoadedPotato", "SoupLobsterCrab", "SoupTomatoBasil",
    "SoupChickenTortilla", "SoupBroccoliCheese", "SoupBeefChili",
    "SoupCrawfish", "SoupGumbo", "SoupMarylandCrab",
    "TreatPBCrisby", "TreatBigCrispy", "TreatCookieCChip", "TreatBrownie",
    "Debra", "ChickenSpicy", "Mediterranean", "Supreme", "ChickenBBQ",
    "PepperoniKids", "CheeseKids", "Margherita", "PepperoniMushroom",
    "PepperoniSausage", "Pepperoni",
    "ChickenGreek", "GrilledSteak", "ChickenGarden", "HamWheat",
    "RoastBeefCrois", "RoastBeefWheat", "ShrimpCajunPoBoy", "TurkeyWheat",
    "BowlFruit", "BowlPastaSalad", "BowlPotatoSalad", "PimientoCheese",
    "Trio", "Parisian", "BBQChicken", "Chicken",
    "ChickenBaconClubCrios", "ChickenBaconClubFP", "ChickenBaconClubRoll",
    "ChickenBaconClubWheat", "ChickenPastaBowl", "ChickenPestoWheat",
    "ChickenSaladCrois", "ChickenSaladFP", "ChickenSaladRoll",
    "ChickenSaladWheat",
    "ChipsBakedLays", "ChipsBBQ", "ChipsCheetos", "ChipsDoritos",
    "ChipsFritos", "ChipsJalapeno", "ChipsOriginal", "ChipsSunChips",
    "ClubFP", "ClubRoll",
    "DrBalsamicVin", "DrBBQ", "DrBleuCheese", "DrCaesar", "DrComeback",
    "DrGreek", "DrGreenGoddess", "DrHoneyMust", "DrLemonVin", "DrNoDressing",
    "DrRanch", "DrSherryVin", "DrWhiteBBQ", "DrGinger", "DrChipotle",
    "Fruit", "HamCrios", "HamFP", "HamRoll",
    "ItalianCrios", "ItalianFP", "ItalianRoll", "ItalianWheat",
    "PastaSalad", "PestoChickenCrios", "PestoChickenFP", "PestoChickenRoll",
    "PimientoCheeseCrois", "PimientoCheeseRoll", "PortabellaVegWheat",
    "PotatoSalad", "RoastBeefRoll",
    "ShrimpPastaBowl", "SoupChickenTortila", "Southen", "SouthernDuo",
    "TreatAssorted", "TreatBigCrispy", "TreatBrownie", "TreatCookie",
    "TreatDessert", "TreatPBCrispy", "TripleTake",
    "TunaSaladCrois", "TunaSaladFP", "TunaSaladRoll", "TunaSaladWheat",
    "TurkeyAvoCrois", "TurkeyAvoFP", "TurkeyAvoRoll", "TurkeyAvoWheat",
    "TurkeyFP", "TurkeyRoll",
}


def _load_mapping_for_mix() -> pd.DataFrame:
    """Load mapping table with all columns needed for mix report."""
    for path in [MAPPING_FILE, FALLBACK_MAPPING_FILE]:
        if os.path.exists(path):
            try:
                return pd.read_excel(path)
            except Exception:
                continue
    return pd.DataFrame()


def _extract_recipe_tag(tags_str: str) -> str:
    """Extract the recipe tag from a comma-separated Tags string."""
    if not tags_str or pd.isna(tags_str):
        return ""
    for tag in str(tags_str).split(","):
        tag = tag.strip()
        if tag in _RECIPE_TAGS:
            return tag
    return ""


def _extract_ee_value(ee_tag: str) -> float:
    """Convert EntreEquivTag to numeric value."""
    if not ee_tag or pd.isna(ee_tag):
        return 0.0
    ee = str(ee_tag).strip()
    if ee == "EE 1":
        return 1.0
    if ee == "EE .5":
        return 0.5
    if ee == "EE 12":
        return 12.0
    return 0.0


def _build_mix_excel(
    report_details: dict,
    summary_combos: dict,
    recipe_summary: list,
    item_summary: list,
    item_counts: list,
) -> bytes:
    """Build the 5-sheet Mix Performance Report Excel file."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font = Font(bold=True, size=11)
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_font_white = Font(bold=True, size=11, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    num_fmt_dec2 = "0.00"
    num_fmt_int = "#,##0"
    num_fmt_pct = "0.0%"

    # ── Sheet 1: Report Details ──
    ws1 = wb.active
    ws1.title = "Report Details"
    loc_label = report_details.get("location_label", "All locations combined")
    ws1["A1"] = loc_label
    ws1["A1"].font = Font(bold=True, size=14)
    ws1["C2"] = "#stores being reported"
    ws1["D2"] = report_details["num_stores"]
    ws1["C3"] = "# days being reported"
    ws1["D3"] = report_details["num_days"]
    ws1["C2"].font = header_font
    ws1["C3"].font = header_font
    ws1.column_dimensions["A"].width = 30
    ws1.column_dimensions["C"].width = 25
    ws1.column_dimensions["D"].width = 12

    # ── Sheet 2: Summary Combos ──
    ws2 = wb.create_sheet("Summary Combos")
    ws2["D2"] = "Units"
    ws2["G2"] = "Per Store Per Day"
    ws2["D3"] = "This Y"
    ws2["E3"] = "Last Y"
    ws2["G3"] = "TY"
    ws2["H3"] = "LY"
    for cell in ["D2", "G2"]:
        ws2[cell].font = header_font_white
        ws2[cell].fill = header_fill
    for cell in ["D3", "E3", "G3", "H3"]:
        ws2[cell].font = header_font
    ws2["B4"] = "Combos ( Totals for all)"
    ws2["B4"].font = Font(bold=True, size=12)

    na_font = Font(italic=True, color="999999")
    combo_rows = summary_combos.get("combos", [])
    row = 5
    for item in combo_rows:
        ws2[f"C{row}"] = item["name"]
        ws2[f"D{row}"] = item.get("ty_units", 0)
        ws2[f"D{row}"].number_format = num_fmt_int
        ws2[f"G{row}"] = item.get("ty_pspd", 0)
        ws2[f"G{row}"].number_format = num_fmt_dec2
        if item.get("ly_units") is not None:
            ws2[f"E{row}"] = item["ly_units"]
            ws2[f"E{row}"].number_format = num_fmt_int
            ws2[f"H{row}"] = item["ly_pspd"]
            ws2[f"H{row}"].number_format = num_fmt_dec2
        else:
            ws2[f"E{row}"] = "N/A"
            ws2[f"E{row}"].font = na_font
            ws2[f"H{row}"] = "N/A"
            ws2[f"H{row}"].font = na_font
        row += 1

    row += 1
    ws2[f"B{row}"] = "Combo Upcharges"
    ws2[f"B{row}"].font = Font(bold=True, size=12)
    row += 1
    for item in summary_combos.get("upcharges", []):
        ws2[f"C{row}"] = item["name"]
        ws2[f"D{row}"] = item.get("ty_units", 0)
        ws2[f"D{row}"].number_format = num_fmt_int
        ws2[f"G{row}"] = item.get("ty_pspd", 0)
        ws2[f"G{row}"].number_format = num_fmt_dec2
        if item.get("ly_units") is not None:
            ws2[f"E{row}"] = item["ly_units"]
            ws2[f"E{row}"].number_format = num_fmt_int
            ws2[f"H{row}"] = item["ly_pspd"]
            ws2[f"H{row}"].number_format = num_fmt_dec2
        else:
            ws2[f"E{row}"] = "N/A"
            ws2[f"E{row}"].font = na_font
            ws2[f"H{row}"] = "N/A"
            ws2[f"H{row}"].font = na_font
        row += 1

    for col in ["B", "C", "D", "E", "F", "G", "H"]:
        ws2.column_dimensions[col].width = 18

    # ── Sheet 3: Recipe Summary ──
    ws3 = wb.create_sheet("Recipe summary")
    ws3["B3"] = "Recipe Summary All selected locations"
    ws3["B3"].font = Font(bold=True, size=13)
    ws3["C4"] = "All Entrée Equivalents"
    ws3["H4"] = "Full Entrée"
    ws3["M4"] = "Half Entrée"
    for cell in ["C4", "H4", "M4"]:
        ws3[cell].font = header_font_white
        ws3[cell].fill = header_fill
    ws3["C5"] = "PSPD"
    ws3["D5"] = "PSPD"
    ws3["F5"] = "Mix Change"
    ws3["H5"] = "PSPD"
    ws3["I5"] = "PSPD"
    ws3["K5"] = "Mix Change"
    ws3["M5"] = "PSPD"
    ws3["N5"] = "PSPD"
    ws3["P5"] = "Mix Change"
    ws3["A6"] = "Consolidated Tag"
    ws3["B6"] = "Recipe Tag"
    ws3["C6"] = "TY"
    ws3["D6"] = "LY"
    ws3["H6"] = "TY"
    ws3["I6"] = "LY"
    ws3["M6"] = "TY"
    ws3["N6"] = "LY"
    for cell in ["A6", "B6", "C5", "C6", "D5", "D6", "F5", "H5", "H6", "I5", "I6", "K5", "M5", "M6", "N5", "N6", "P5"]:
        ws3[cell].font = header_font

    row = 7
    for rec in recipe_summary:
        ws3[f"A{row}"] = rec.get("consolidated_tag", "")
        ws3[f"B{row}"] = rec.get("recipe_tag", "")
        ws3[f"C{row}"] = rec.get("all_ee_pspd_ty", 0)
        ws3[f"D{row}"] = rec.get("all_ee_pspd_ly", 0)
        ws3[f"F{row}"] = rec.get("all_mix_change", 0)
        ws3[f"H{row}"] = rec.get("full_ee_pspd_ty", 0)
        ws3[f"I{row}"] = rec.get("full_ee_pspd_ly", 0)
        ws3[f"K{row}"] = rec.get("full_mix_change", 0)
        ws3[f"M{row}"] = rec.get("half_ee_pspd_ty", 0)
        ws3[f"N{row}"] = rec.get("half_ee_pspd_ly", 0)
        ws3[f"P{row}"] = rec.get("half_mix_change", 0)
        for c in ["C", "D", "H", "I", "M", "N"]:
            ws3[f"{c}{row}"].number_format = num_fmt_dec2
        for c in ["F", "K", "P"]:
            ws3[f"{c}{row}"].number_format = num_fmt_pct
        row += 1

    for col_letter in "ABCDEFGHIJKLMNOP":
        ws3.column_dimensions[col_letter].width = 14

    # ── Sheet 4: Item Summary ──
    ws4 = wb.create_sheet("Item Summary")
    loc_label_item = report_details.get("location_label", "All locations combined")
    ws4["C3"] = loc_label_item
    ws4["C3"].font = Font(bold=True, size=13)
    # Header row 5
    ws4["D5"] = "Consolidated Tag"
    ws4["E5"] = "Recipe Tag Name"
    ws4["H5"] = "Units"
    ws4["J5"] = "Entrée Equivalent"
    ws4["L5"] = "PSPD"
    ws4["O5"] = "Entrée Equivalent PSPD"
    ws4["R5"] = "Premium Flag?"
    for cell in ["D5", "E5", "H5", "J5", "L5", "O5", "R5"]:
        ws4[cell].font = header_font_white
        ws4[cell].fill = header_fill
    # Header row 6 sub-headers
    ws4["B6"] = "Toast PLU"
    ws4["C6"] = "Item_Name_T"
    ws4["F6"] = "EE Tag"
    ws4["H6"] = "TY"
    ws4["I6"] = "LY"
    ws4["L6"] = "TY"
    ws4["M6"] = "LY"
    ws4["O6"] = "TY"
    ws4["P6"] = "LY"
    for cell in ["B6", "C6", "F6", "H6", "I6", "L6", "M6", "O6", "P6"]:
        ws4[cell].font = header_font

    row = 7
    for item in item_summary:
        ws4[f"B{row}"] = item.get("toast_plu", "")
        ws4[f"C{row}"] = item.get("item_name", "")
        ws4[f"D{row}"] = item.get("consolidated_tag", "")
        ws4[f"E{row}"] = item.get("recipe_tag", "")
        ws4[f"F{row}"] = item.get("ee_tag", "")
        ws4[f"H{row}"] = item.get("units_ty", 0)
        ws4[f"I{row}"] = item.get("units_ly", 0)
        ws4[f"J{row}"] = item.get("ee_value", 0)
        ws4[f"L{row}"] = item.get("pspd_ty", 0)
        ws4[f"M{row}"] = item.get("pspd_ly", 0)
        ws4[f"O{row}"] = item.get("ee_pspd_ty", 0)
        ws4[f"P{row}"] = item.get("ee_pspd_ly", 0)
        ws4[f"R{row}"] = item.get("premium_flag", "")
        ws4[f"H{row}"].number_format = num_fmt_int
        ws4[f"I{row}"].number_format = num_fmt_int
        ws4[f"J{row}"].number_format = num_fmt_dec2
        ws4[f"L{row}"].number_format = num_fmt_dec2
        ws4[f"M{row}"].number_format = num_fmt_dec2
        ws4[f"O{row}"].number_format = num_fmt_dec2
        ws4[f"P{row}"].number_format = num_fmt_dec2
        row += 1

    for col_letter in "BCDEFGHIJKLMNOPQR":
        ws4.column_dimensions[col_letter].width = 16
    ws4.column_dimensions["C"].width = 40

    # ── Sheet 5: Item Counts Compare ──
    ws5 = wb.create_sheet("Item Counts Compare")
    headers5 = ["StoreNum", "StoreName", "Recipe", "ItemMLI", "EntreeEquivalents",
                 "ItemID_T", "Item_Name_T", "ItemID_A", "Item_Name_A",
                 "CYItemCount", "LYItemCount", "BlankCol", "CY_Equiv_Count", "LY_Equiv_Count"]
    for col_idx, h in enumerate(headers5, 1):
        cell = ws5.cell(row=1, column=col_idx, value=h)
        cell.font = header_font_white
        cell.fill = header_fill

    row = 2
    for rec in item_counts:
        ws5[f"A{row}"] = rec.get("store_num", "")
        ws5[f"B{row}"] = rec.get("store_name", "")
        ws5[f"C{row}"] = rec.get("recipe", "")
        ws5[f"D{row}"] = rec.get("item_mli", "")
        ws5[f"E{row}"] = rec.get("ee", 0)
        ws5[f"F{row}"] = rec.get("item_id_t", "")
        ws5[f"G{row}"] = rec.get("item_name_t", "")
        ws5[f"H{row}"] = rec.get("item_id_a", "")
        ws5[f"I{row}"] = rec.get("item_name_a", "")
        ws5[f"J{row}"] = rec.get("cy_count", 0)
        ws5[f"K{row}"] = rec.get("ly_count", 0)
        ws5[f"L{row}"] = ""
        ws5[f"M{row}"] = rec.get("cy_equiv", 0)
        ws5[f"N{row}"] = rec.get("ly_equiv", 0)
        ws5[f"E{row}"].number_format = num_fmt_dec2
        ws5[f"J{row}"].number_format = num_fmt_int
        ws5[f"K{row}"].number_format = num_fmt_int
        ws5[f"M{row}"].number_format = num_fmt_dec2
        ws5[f"N{row}"].number_format = num_fmt_dec2
        row += 1

    for col_letter in "ABCDEFGHIJKLMN":
        ws5.column_dimensions[col_letter].width = 16
    ws5.column_dimensions["D"].width = 30
    ws5.column_dimensions["G"].width = 40
    ws5.column_dimensions["I"].width = 30

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class MenuMixRequest(BaseModel):
    startDate: str          # YYYY-MM-DD
    endDate: str            # YYYY-MM-DD
    restaurantIds: List[str] = []   # empty = all
    lyStartDate: Optional[str] = None  # override LY range
    lyEndDate: Optional[str] = None


@app.post("/api/reports/menu-mix/generate")
async def generate_menu_mix_report(req: MenuMixRequest):
    """Generate the Menu Mix Performance Report as an Excel download."""
    import base64

    cy_start = date.fromisoformat(req.startDate)
    cy_end = date.fromisoformat(req.endDate)
    num_days = (cy_end - cy_start).days + 1

    # LY dates: default to same dates minus 1 year
    if req.lyStartDate and req.lyEndDate:
        ly_start_str = req.lyStartDate
        ly_end_str = req.lyEndDate
    else:
        ly_start = cy_start.replace(year=cy_start.year - 1)
        ly_end = cy_end.replace(year=cy_end.year - 1)
        ly_start_str = ly_start.isoformat()
        ly_end_str = ly_end.isoformat()

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get locations
        all_locations = await get_all_active_locations(client)
        if req.restaurantIds:
            locations = [loc for loc in all_locations if loc.restaurantGuid in req.restaurantIds]
        else:
            locations = list(all_locations)
        restaurant_ids = [loc.restaurantGuid for loc in locations]
        num_stores = len(locations)

        if num_stores == 0:
            raise HTTPException(status_code=400, detail="No locations selected")

        location_label = "All locations combined" if not req.restaurantIds else ", ".join(
            loc.restaurantName for loc in locations[:5]
        )
        if len(locations) > 5:
            location_label += f" (+{len(locations) - 5} more)"

        # ── Load mapping data (SQLite first, Excel fallback) ──
        mapping_by_norm_name: Dict[str, dict] = {}
        mapping_by_mli: Dict[str, dict] = {}
        mapping_by_guid: Dict[str, dict] = {}
        aloha_to_recipe: Dict[str, dict] = {}

        attr_df = toast_config_db.get_attribute_map()
        if not attr_df.empty:
            logger.info(f"Menu Mix: Loading mapping from toast_config.db ({len(attr_df)} rows)")
            for _, mrow in attr_df.iterrows():
                toast_name = str(mrow.get("toast_item_name", "") or "").strip()
                norm = _normalize_item_name(toast_name)
                aloha_name = str(mrow.get("aloha_item_name", "") or "").strip()
                cons_cat = str(mrow.get("consolidated_cat", "") or "").strip()
                recipe_tag = str(mrow.get("recipe", "") or "").strip()
                ee_val = _parse_ee_value(mrow.get("entree_equiv_tag", 0))
                ee_tag = f"EE {ee_val}" if ee_val else ""
                mli = str(mrow.get("multi_location_id", "") or "").strip()
                guid = str(mrow.get("item_guid", "") or "").strip()
                toast_plu = str(int(mrow.get("toast_plu", 0) or 0)) if mrow.get("toast_plu") else ""

                row_data = {
                    "toast_name": toast_name,
                    "norm_name": norm,
                    "aloha_name": aloha_name,
                    "consolidated_tag": cons_cat,
                    "recipe_tag": recipe_tag,
                    "ee_tag": ee_tag,
                    "ee_value": ee_val,
                    "mli": mli,
                    "guid": guid,
                    "toast_plu": toast_plu,
                }

                if norm and norm not in mapping_by_norm_name:
                    mapping_by_norm_name[norm] = row_data
                if mli and mli not in mapping_by_mli:
                    mapping_by_mli[mli] = row_data
                if guid and guid not in mapping_by_guid:
                    mapping_by_guid[guid] = row_data
                if aloha_name:
                    aloha_norm = aloha_name.lower().strip()
                    if aloha_norm not in aloha_to_recipe:
                        aloha_to_recipe[aloha_norm] = row_data
        else:
            # Fall back to Excel mapping table
            logger.info("Menu Mix: toast_config.db empty, falling back to Excel mapping")
            mapping_df = _load_mapping_for_mix()
            if mapping_df.empty:
                raise HTTPException(status_code=500, detail="No mapping data available (toast_config.db empty and Excel mapping not found)")

            for _, mrow in mapping_df.iterrows():
                toast_name = str(mrow.get("Toast_Item_Name", "") or "").strip()
                norm = _normalize_item_name(toast_name)
                aloha_name = str(mrow.get("AlohaItemName", "") or "").strip() if pd.notna(mrow.get("AlohaItemName")) else ""
                cons_cat = str(mrow.get("ConsolidatedCat", "") or "").strip() if pd.notna(mrow.get("ConsolidatedCat")) else ""
                tags_str = str(mrow.get("Tags", "") or "").strip() if pd.notna(mrow.get("Tags")) else ""
                ee_tag = str(mrow.get("EntreEquivTag", "") or "").strip() if pd.notna(mrow.get("EntreEquivTag")) else ""
                recipe_tag = _extract_recipe_tag(tags_str)
                ee_val = _extract_ee_value(ee_tag)
                mli = str(mrow.get("MultiLocationId", "") or "").strip() if pd.notna(mrow.get("MultiLocationId")) else ""
                guid = str(mrow.get("item_guid", "") or "").strip() if pd.notna(mrow.get("item_guid")) else ""
                toast_plu = str(mrow.get("ToastPLU", "") or "").strip() if pd.notna(mrow.get("ToastPLU")) else ""

                row_data = {
                    "toast_name": toast_name,
                    "norm_name": norm,
                    "aloha_name": aloha_name,
                    "consolidated_tag": cons_cat,
                    "recipe_tag": recipe_tag,
                    "ee_tag": ee_tag,
                    "ee_value": ee_val,
                    "mli": mli,
                    "guid": guid,
                    "toast_plu": toast_plu,
                }

                if norm and norm not in mapping_by_norm_name:
                    mapping_by_norm_name[norm] = row_data
                if mli and mli not in mapping_by_mli:
                    mapping_by_mli[mli] = row_data
                if guid and guid not in mapping_by_guid:
                    mapping_by_guid[guid] = row_data
                if aloha_name:
                    aloha_norm = aloha_name.lower().strip()
                    if aloha_norm not in aloha_to_recipe:
                        aloha_to_recipe[aloha_norm] = row_data

        def _lookup_mapping(item_guid: str, item_name: str) -> dict:
            """Look up mapping by GUID first, then by normalized name."""
            if item_guid:
                hit = mapping_by_guid.get(item_guid)
                if hit:
                    return hit
            norm = _normalize_item_name(item_name)
            for key in _mapping_lookup_keys(norm):
                hit = mapping_by_norm_name.get(key)
                if hit:
                    return hit
            return {}

        # ── CY: Fetch Toast Analytics MENU_ITEM data ──
        logger.info(f"Menu Mix: Fetching CY data {cy_start} to {cy_end} for {num_stores} stores")
        all_menu_item_rows: List[Dict] = []
        current_start = cy_start
        while current_start <= cy_end:
            chunk_end = min(current_start + timedelta(days=6), cy_end)
            report_guid = await request_toast_report(
                client, current_start, chunk_end, restaurant_ids, "menu", ["MENU_ITEM"]
            )
            chunk_rows = await fetch_toast_report(client, report_guid, "menu")
            if chunk_rows:
                all_menu_item_rows.extend(chunk_rows)
            current_start = chunk_end + timedelta(days=1)

        # Also fetch MODIFIER data for modifiers with recipe tags
        all_modifier_rows: List[Dict] = []
        logger.info(f"Menu Mix: Fetching CY modifier data")
        current_start = cy_start
        while current_start <= cy_end:
            chunk_end = min(current_start + timedelta(days=6), cy_end)
            try:
                mod_report_guid = await request_toast_report(
                    client, current_start, chunk_end, restaurant_ids, "menu", ["MODIFIER"]
                )
                mod_rows = await fetch_toast_report(client, mod_report_guid, "menu")
                if mod_rows:
                    all_modifier_rows.extend(mod_rows)
            except Exception as e:
                logger.warning(f"Menu Mix: Failed to fetch modifier data for {current_start}-{chunk_end}: {e}")
            current_start = chunk_end + timedelta(days=1)

        logger.info(f"Menu Mix: Got {len(all_menu_item_rows)} MENU_ITEM rows + {len(all_modifier_rows)} MODIFIER rows")

        # Aggregate CY by item (GUID + name) per store
        # cy_items: {item_key: {guid, name, qty_total, qty_by_store, is_modifier}}
        cy_items: Dict[str, Dict] = {}

        # MENU_ITEM rows
        for row in all_menu_item_rows:
            guid = row.get("menuItemGuid") or ""
            name = (row.get("menuItemName") or "").strip()
            qty = float(row.get("quantitySold", 0) or 0)
            store = row.get("restaurantGuid", "")
            if not name or qty == 0:
                continue
            key = f"{guid}|{_normalize_item_name(name)}"
            if key not in cy_items:
                cy_items[key] = {"guid": guid, "name": name, "qty_total": 0, "qty_by_store": {}, "is_modifier": False}
            cy_items[key]["qty_total"] += qty
            cy_items[key]["qty_by_store"][store] = cy_items[key]["qty_by_store"].get(store, 0) + qty

        # MODIFIER rows (tagged so combo counting can skip them)
        for row in all_modifier_rows:
            guid = row.get("modifierOptionGuid") or ""
            name = (row.get("modifierOptionName") or "").strip()
            qty = float(row.get("quantitySold", 0) or 0)
            store = row.get("restaurantGuid", "")
            if not name or qty == 0:
                continue
            key = f"mod|{guid}|{_normalize_item_name(name)}"
            if key not in cy_items:
                cy_items[key] = {"guid": guid, "name": name, "qty_total": 0, "qty_by_store": {}, "is_modifier": True}
            cy_items[key]["qty_total"] += qty
            cy_items[key]["qty_by_store"][store] = cy_items[key]["qty_by_store"].get(store, 0) + qty

        # ── LY: Query aloha_db ──
        aloha_loc_map = get_aloha_location_mapping()
        aloha_loc_ids = [aloha_loc_map[rid] for rid in restaurant_ids if rid in aloha_loc_map]

        logger.info(f"Menu Mix: Querying aloha_db for LY {ly_start_str} to {ly_end_str}, {len(aloha_loc_ids)} locations")
        ly_df = aloha_db.query_date_range(
            ly_start_str, ly_end_str,
            locations=aloha_loc_ids if aloha_loc_ids else None,
        )
        logger.info(f"Menu Mix: Got {len(ly_df)} LY rows from aloha_db")

        # Aggregate LY by Aloha item per store
        # ly_items: {aloha_item_lower: {qty_total, qty_by_store: {aloha_loc: qty}}}
        ly_items: Dict[str, Dict] = {}
        for _, arow in ly_df.iterrows():
            aloha_item = str(arow.get("AlohaItem", "")).strip()
            qty = float(arow.get("Quantity", 0) or 0)
            loc = str(arow.get("Location", "")).strip()
            if not aloha_item or qty == 0:
                continue
            key = aloha_item.lower().strip()
            if key not in ly_items:
                ly_items[key] = {"name": aloha_item, "qty_total": 0, "qty_by_store": {}}
            ly_items[key]["qty_total"] += qty
            ly_items[key]["qty_by_store"][loc] = ly_items[key]["qty_by_store"].get(loc, 0) + qty

        # ── Use SalesConsolidator for proper Toast→Aloha mapping (same as menu report YoY) ──
        consolidator = get_yoy_consolidator()
        toast_to_aloha = consolidator.toast_to_aloha_map
        toast_name_to_aloha = consolidator.toast_name_to_aloha_map

        # Build aloha qty lookup from LY data
        aloha_qty_by_name: Dict[str, float] = {}
        for _, arow in ly_df.iterrows():
            aloha_item = str(arow.get("AlohaItem", "")).strip()
            qty = float(arow.get("Quantity", 0) or 0)
            if aloha_item:
                aloha_qty_by_name[aloha_item] = aloha_qty_by_name.get(aloha_item, 0) + qty

        def _lookup_ly_qty(item_guid: str, item_name: str) -> float:
            """Look up LY quantity using SalesConsolidator mapping (same logic as menu report YoY)."""
            # Try by GUID first
            if item_guid:
                aloha_name = toast_to_aloha.get(item_guid)
                if aloha_name:
                    return float(aloha_qty_by_name.get(aloha_name, 0))
            # Then by normalized Toast name
            if item_name:
                normalized = consolidator.normalize_name(item_name)
                aloha_name = toast_name_to_aloha.get(normalized)
                if aloha_name:
                    return float(aloha_qty_by_name.get(aloha_name, 0))
                # Direct match on Aloha item name
                norm_qty = {consolidator.normalize_name(k): v for k, v in aloha_qty_by_name.items() if consolidator.normalize_name(k)}
                if normalized in norm_qty:
                    return float(norm_qty[normalized])
            # Try mapping table AlohaItemName
            mapping_row = _lookup_mapping(item_guid, item_name)
            aloha_name = mapping_row.get("aloha_name", "")
            if aloha_name:
                return float(aloha_qty_by_name.get(aloha_name, 0))
            return 0.0

        # ── Build Item Summary (Sheet 4) — ONLY items with a recipe tag ──
        item_summary_list = []
        recipe_agg: Dict[str, Dict] = {}  # key = "cons_tag|recipe_tag"
        ly_num_days = (date.fromisoformat(ly_end_str) - date.fromisoformat(ly_start_str)).days + 1
        skipped_no_recipe = 0

        for item_key, cy_data in cy_items.items():
            mapping_row = _lookup_mapping(cy_data["guid"], cy_data["name"])
            recipe_tag = mapping_row.get("recipe_tag", "")

            # *** FILTER: Only include items that have a recipe tag (IsRecipeTag=Y) ***
            if not recipe_tag:
                skipped_no_recipe += 1
                continue

            cons_tag = mapping_row.get("consolidated_tag", "")
            ee_tag = mapping_row.get("ee_tag", "")
            ee_val = mapping_row.get("ee_value", 0)
            toast_plu = mapping_row.get("toast_plu", "")

            # Look up LY quantity using consolidator mapping
            ly_qty = _lookup_ly_qty(cy_data["guid"], cy_data["name"])

            cy_qty = cy_data["qty_total"]
            pspd_ty = cy_qty / num_stores / num_days if num_stores and num_days else 0
            pspd_ly = ly_qty / num_stores / ly_num_days if num_stores and ly_num_days else 0
            ee_pspd_ty = pspd_ty * ee_val
            ee_pspd_ly = pspd_ly * ee_val

            item_summary_list.append({
                "toast_plu": toast_plu,
                "item_name": cy_data["name"],
                "consolidated_tag": cons_tag,
                "recipe_tag": recipe_tag,
                "ee_tag": ee_tag,
                "ee_value": ee_val,
                "units_ty": cy_qty,
                "units_ly": ly_qty,
                "pspd_ty": round(pspd_ty, 4),
                "pspd_ly": round(pspd_ly, 4),
                "ee_pspd_ty": round(ee_pspd_ty, 4),
                "ee_pspd_ly": round(ee_pspd_ly, 4),
                "premium_flag": "",
            })

            # Aggregate for recipe summary
            rkey = f"{cons_tag}|{recipe_tag}"
            if rkey not in recipe_agg:
                recipe_agg[rkey] = {
                    "consolidated_tag": cons_tag,
                    "recipe_tag": recipe_tag,
                    "all_ee_ty": 0, "all_ee_ly": 0,
                    "full_ee_ty": 0, "full_ee_ly": 0,
                    "half_ee_ty": 0, "half_ee_ly": 0,
                }
            recipe_agg[rkey]["all_ee_ty"] += cy_qty * ee_val
            recipe_agg[rkey]["all_ee_ly"] += ly_qty * ee_val
            if ee_val >= 1.0:
                recipe_agg[rkey]["full_ee_ty"] += cy_qty * ee_val
                recipe_agg[rkey]["full_ee_ly"] += ly_qty * ee_val
            elif ee_val == 0.5:
                recipe_agg[rkey]["half_ee_ty"] += cy_qty * ee_val
                recipe_agg[rkey]["half_ee_ly"] += ly_qty * ee_val

        logger.info(f"Menu Mix: {len(item_summary_list)} items with recipe tags, {skipped_no_recipe} skipped (no recipe tag)")

        # Sort item summary by consolidated tag then recipe tag then item name
        item_summary_list.sort(key=lambda x: (x["consolidated_tag"], x["recipe_tag"], x["item_name"]))

        # ── Build Recipe Summary (Sheet 3) ──
        # Compute totals for mix change calculation
        total_all_ee_ty = sum(r["all_ee_ty"] for r in recipe_agg.values())
        total_all_ee_ly = sum(r["all_ee_ly"] for r in recipe_agg.values())
        total_full_ee_ty = sum(r["full_ee_ty"] for r in recipe_agg.values())
        total_full_ee_ly = sum(r["full_ee_ly"] for r in recipe_agg.values())
        total_half_ee_ty = sum(r["half_ee_ty"] for r in recipe_agg.values())
        total_half_ee_ly = sum(r["half_ee_ly"] for r in recipe_agg.values())

        recipe_summary_list = []
        for rkey in sorted(recipe_agg.keys()):
            r = recipe_agg[rkey]
            all_pspd_ty = r["all_ee_ty"] / num_stores / num_days if num_stores and num_days else 0
            ly_nd = (date.fromisoformat(ly_end_str) - date.fromisoformat(ly_start_str)).days + 1
            all_pspd_ly = r["all_ee_ly"] / num_stores / ly_nd if num_stores and ly_nd else 0
            full_pspd_ty = r["full_ee_ty"] / num_stores / num_days if num_stores and num_days else 0
            full_pspd_ly = r["full_ee_ly"] / num_stores / ly_nd if num_stores and ly_nd else 0
            half_pspd_ty = r["half_ee_ty"] / num_stores / num_days if num_stores and num_days else 0
            half_pspd_ly = r["half_ee_ly"] / num_stores / ly_nd if num_stores and ly_nd else 0

            # Mix Change = TY% - LY%
            all_mix_ty = r["all_ee_ty"] / total_all_ee_ty if total_all_ee_ty else 0
            all_mix_ly = r["all_ee_ly"] / total_all_ee_ly if total_all_ee_ly else 0
            full_mix_ty = r["full_ee_ty"] / total_full_ee_ty if total_full_ee_ty else 0
            full_mix_ly = r["full_ee_ly"] / total_full_ee_ly if total_full_ee_ly else 0
            half_mix_ty = r["half_ee_ty"] / total_half_ee_ty if total_half_ee_ty else 0
            half_mix_ly = r["half_ee_ly"] / total_half_ee_ly if total_half_ee_ly else 0

            recipe_summary_list.append({
                "consolidated_tag": r["consolidated_tag"],
                "recipe_tag": r["recipe_tag"],
                "all_ee_pspd_ty": round(all_pspd_ty, 4),
                "all_ee_pspd_ly": round(all_pspd_ly, 4),
                "all_mix_change": round(all_mix_ty - all_mix_ly, 4),
                "full_ee_pspd_ty": round(full_pspd_ty, 4),
                "full_ee_pspd_ly": round(full_pspd_ly, 4),
                "full_mix_change": round(full_mix_ty - full_mix_ly, 4),
                "half_ee_pspd_ty": round(half_pspd_ty, 4),
                "half_ee_pspd_ly": round(half_pspd_ly, 4),
                "half_mix_change": round(half_mix_ty - half_mix_ly, 4),
            })

        # ── Build Summary Combos (Sheet 2) ──
        # Combo identification by item name patterns (matching how Pick 3 / Full Pairings reports work)
        combo_defs = [
            # (label, CY name matchers — checks if ANY pattern is in the item name)
            ("Pick 3",                    ["(pick 3 entree)", "pick 3 (olo)"]),
            ("Large Pairings",            ["(full pairing entree)", "full value pairings (olo)"]),
            ("Half Pairings",             ["(half pairing", "pick 2 (olo)"]),
            ("Pizza Family Meal Deal",    ["pizza family meal deal"]),
            ("3 Pizza Meal Deal",         ["3 pizza meal deal"]),
            ("Half Sandwich Combo Deal",  ["half sandwich meal deal"]),
        ]
        upcharge_defs = [
            ("Half Sandwich Premium Upcharge $",  ["half sandwich premium upcharge"]),
            ("Half Salad Premium Upcharge $",     ["half salad premium upcharge"]),
            ("Premium Soup Upcharge Lg&sm",       ["premium soup upcharge"]),
            ("Pick 3 Upcharge Bowl Fruit $",      ["pick 3 upcharge bowl fruit"]),
            ("Pairing Upcharge Bowl Fruit $",     ["pairing upcharge bowl fruit"]),
        ]

        def _count_combo(patterns: List[str]) -> float:
            """Count CY units for MENU_ITEM rows matching name patterns (skip modifiers)."""
            cy_total = 0.0
            for item_key, cy_data in cy_items.items():
                if cy_data.get("is_modifier"):
                    continue
                name_lower = cy_data["name"].lower()
                if any(p in name_lower for p in patterns):
                    cy_total += cy_data["qty_total"]
            return cy_total

        combos_list = []
        for label, patterns in combo_defs:
            cy_u = _count_combo(patterns)
            combos_list.append({
                "name": label,
                "ty_units": cy_u,
                "ly_units": None,  # LY combo data not available from Aloha
                "ty_pspd": round(cy_u / num_stores / num_days, 2) if num_stores and num_days else 0,
                "ly_pspd": None,
            })

        upcharges_list = []
        for label, patterns in upcharge_defs:
            cy_u = _count_combo(patterns)
            upcharges_list.append({
                "name": label,
                "ty_units": cy_u,
                "ly_units": None,
                "ty_pspd": round(cy_u / num_stores / num_days, 2) if num_stores and num_days else 0,
                "ly_pspd": None,
            })

        # ── Build Item Counts Compare (Sheet 5) ──
        # Per-store × recipe × item detail — only items with recipe tags
        item_counts_list = []

        # Build per-store LY lookup: {aloha_item_lower: {aloha_loc: qty}}
        ly_by_store: Dict[str, Dict[str, float]] = {}
        for _, arow in ly_df.iterrows():
            aloha_item = str(arow.get("AlohaItem", "")).strip()
            qty = float(arow.get("Quantity", 0) or 0)
            loc = str(arow.get("Location", "")).strip()
            if aloha_item and qty:
                key = aloha_item.lower().strip()
                if key not in ly_by_store:
                    ly_by_store[key] = {}
                ly_by_store[key][loc] = ly_by_store[key].get(loc, 0) + qty

        for item_key, cy_data in cy_items.items():
            mapping_row = _lookup_mapping(cy_data["guid"], cy_data["name"])
            recipe_tag = mapping_row.get("recipe_tag", "")

            # Only include items with a recipe tag
            if not recipe_tag:
                continue

            ee_val = mapping_row.get("ee_value", 0)
            mli = mapping_row.get("mli", "")
            toast_plu = mapping_row.get("toast_plu", "")

            # Resolve aloha item name via consolidator
            aloha_name = ""
            if cy_data["guid"]:
                aloha_name = toast_to_aloha.get(cy_data["guid"], "")
            if not aloha_name:
                normalized = consolidator.normalize_name(cy_data["name"])
                aloha_name = toast_name_to_aloha.get(normalized, "")
            if not aloha_name:
                aloha_name = mapping_row.get("aloha_name", "")

            for store_guid, store_qty in cy_data.get("qty_by_store", {}).items():
                loc_obj = next((l for l in locations if l.restaurantGuid == store_guid), None)
                store_num = aloha_loc_map.get(store_guid, "")
                store_name = loc_obj.restaurantName if loc_obj else ""

                # LY for this specific store
                ly_store_qty = 0
                if aloha_name and store_num:
                    ly_store_qty = ly_by_store.get(aloha_name.lower().strip(), {}).get(store_num, 0)

                item_counts_list.append({
                    "store_num": store_num,
                    "store_name": store_name,
                    "recipe": recipe_tag,
                    "item_mli": mli,
                    "ee": ee_val,
                    "item_id_t": toast_plu,
                    "item_name_t": cy_data["name"],
                    "item_id_a": "",
                    "item_name_a": aloha_name,
                    "cy_count": store_qty,
                    "ly_count": ly_store_qty,
                    "cy_equiv": store_qty * ee_val,
                    "ly_equiv": ly_store_qty * ee_val,
                })

        item_counts_list.sort(key=lambda x: (x["store_num"], x["recipe"], x["item_name_t"]))

        # ── Generate Excel ──
        logger.info(f"Menu Mix: Building Excel with {len(item_summary_list)} items, {len(recipe_summary_list)} recipes, {len(item_counts_list)} store-item rows")
        xlsx_bytes = _build_mix_excel(
            report_details={
                "location_label": location_label,
                "num_stores": num_stores,
                "num_days": num_days,
            },
            summary_combos={
                "combos": combos_list,
                "upcharges": upcharges_list,
            },
            recipe_summary=recipe_summary_list,
            item_summary=item_summary_list,
            item_counts=item_counts_list,
        )

        date_label = f"{cy_start.strftime('%Y%m%d')}_{cy_end.strftime('%Y%m%d')}"
        filename = f"Menu_Mix_Performance_{date_label}.xlsx"

        # Return as base64 JSON — avoids binary streaming issues through nginx proxies
        return {
            "filename": filename,
            "data": base64.b64encode(xlsx_bytes).decode("ascii"),
        }


# ------------------------------------------------------------------
# Cashier vs Kiosk Report
# ------------------------------------------------------------------

_KIOSK_DINING_OPTIONS = {"X-Dine-In (K)", "X-To Go (K)"}


def _is_kiosk_dining_option(dining_opt: str) -> bool:
    return dining_opt in _KIOSK_DINING_OPTIONS or "(K)" in dining_opt


# Cache: restaurant_guid → {dining_option_guid: name}
_dining_option_config_cache: Dict[str, Dict[str, str]] = {}


async def _fetch_dining_option_configs(
    client: httpx.AsyncClient,
    restaurant_guid: str,
) -> Dict[str, str]:
    """Fetch dining option GUID→name mapping from Toast Config API. Cached per restaurant."""
    if restaurant_guid in _dining_option_config_cache:
        return _dining_option_config_cache[restaurant_guid]

    standard_headers = await get_standard_api_auth_headers()
    headers = {**standard_headers, "Toast-Restaurant-External-ID": restaurant_guid}
    url = f"{TOAST_STANDARD_API_BASE_URL}/config/v2/diningOptions"
    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            logger.warning("Dining option config fetch failed for %s: HTTP %s",
                           restaurant_guid[:8] + "...", resp.status_code)
            _dining_option_config_cache[restaurant_guid] = {}
            return {}
        do_list = resp.json()
        mapping: Dict[str, str] = {}
        if isinstance(do_list, list):
            for do in do_list:
                guid = do.get("guid", "")
                name = do.get("name", "")
                if guid and name:
                    mapping[guid] = name
        _dining_option_config_cache[restaurant_guid] = mapping
        logger.info("Dining option configs cached for %s: %d options",
                    restaurant_guid[:8] + "...", len(mapping))
        return mapping
    except Exception as exc:
        logger.warning("Dining option config error for %s: %s", restaurant_guid[:8] + "...", exc)
        _dining_option_config_cache[restaurant_guid] = {}
        return {}


async def _fetch_tips_by_channel(
    client: httpx.AsyncClient,
    restaurant_guids: List[str],
    start_date: date,
    end_date: date,
    dining_option_maps: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    Fetch tips from cached orders (same approach as tip pool report).
    Processes one restaurant+day at a time using get_cached_orders.
    Returns: {restaurant_guid: {business_date_yyyymmdd: {"cashier": tips, "kiosk": tips}}}
    """
    result: Dict[str, Dict[str, Dict[str, float]]] = {}
    order_count = 0

    for idx, guid in enumerate(restaurant_guids):
        rest_map = dining_option_maps.get(guid, {})
        d = start_date
        while d <= end_date:
            biz_date = to_business_date(d)
            orders = await _fetch_orders_for_restaurant_day(client, guid, biz_date)
            for order in orders:
                order_count += 1
                if order.get("voided") or order.get("deleted"):
                    continue
                do_obj = order.get("diningOption")
                do_guid = do_obj.get("guid", "") if isinstance(do_obj, dict) else ""
                do_name = rest_map.get(do_guid, "") if do_guid else ""
                channel = "kiosk" if _is_kiosk_dining_option(do_name) else "cashier"
                for check in order.get("checks") or []:
                    if check.get("voided") or check.get("deleted"):
                        continue
                    for pmt in check.get("payments") or []:
                        tip = float(pmt.get("tipAmount") or 0)
                        if tip <= 0:
                            continue
                        if pmt.get("voided"):
                            continue
                        status = (pmt.get("paymentStatus") or "").upper()
                        if status in ("VOIDED", "DENIED"):
                            continue
                        refund = (pmt.get("refundStatus") or "").upper()
                        if refund == "SUCCESSFUL":
                            continue
                        if pmt.get("originalMapPayment"):
                            continue
                        result.setdefault(guid, {}).setdefault(biz_date, {"cashier": 0.0, "kiosk": 0.0})
                        result[guid][biz_date][channel] += tip
            d += timedelta(days=1)
        if (idx + 1) % 10 == 0:
            logger.info("Cashier vs Kiosk tips: %d/%d restaurants processed", idx + 1, len(restaurant_guids))

    logger.info("Cashier vs Kiosk tips: done — %d restaurants, %d orders", len(restaurant_guids), order_count)
    return result


async def _build_cashier_vs_kiosk_data(
    week_end_date: date,
    location_filter: Optional[List[str]] = None,
) -> dict:
    """Core logic shared by the JSON endpoint and the Excel export."""
    week_start = week_end_date - timedelta(days=6)

    async with httpx.AsyncClient(timeout=120.0) as client:
        locations = await get_all_active_locations(client)
        restaurant_ids = [loc.restaurantGuid for loc in locations]
        restaurant_ids = _ensure_all_mapped_guids(restaurant_ids)
        guid_to_name = {loc.restaurantGuid: loc.restaurantName for loc in locations}
        guid_to_ext = {loc.restaurantGuid: (loc.externalId or "") for loc in locations}

        if location_filter:
            filter_set = set(location_filter)
            restaurant_ids = [g for g in restaurant_ids if g in filter_set
                              or guid_to_ext.get(g, "") in filter_set
                              or guid_to_name.get(g, "") in filter_set]

        report_guid = await request_toast_report(
            client, week_start, week_end_date, restaurant_ids,
            report_type="metrics", group_by=["DINING_OPTION"],
        )
        era_rows = await fetch_toast_report(client, report_guid, report_type="metrics")
        if not isinstance(era_rows, list):
            era_rows = []

    # Aggregate ERA data: {guid: {date_str: {cashier: {checks, net}, kiosk: {checks, net}}}}
    store_data: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}
    for row in era_rows:
        if not isinstance(row, dict):
            continue
        guid = row.get("restaurantGuid", "")
        if guid not in guid_to_name:
            continue

        biz_date_raw = str(row.get("businessDate", "")).replace("-", "")
        if len(biz_date_raw) == 8:
            date_key = biz_date_raw
        else:
            continue

        dining_opt = str(row.get("diningOption", "") or "").strip()
        channel = "kiosk" if _is_kiosk_dining_option(dining_opt) else "cashier"
        net = float(row.get("netSalesAmount", 0) or 0)
        checks = float(row.get("ordersCount", 0) or 0)

        store_data.setdefault(guid, {}).setdefault(date_key, {
            "cashier": {"checkCount": 0.0, "netSales": 0.0},
            "kiosk": {"checkCount": 0.0, "netSales": 0.0},
        })
        store_data[guid][date_key][channel]["checkCount"] += checks
        store_data[guid][date_key][channel]["netSales"] += net

    # Fetch dining option configs (parallel, cached after first call)
    dining_option_maps: Dict[str, Dict[str, str]] = {}
    guids_needing_config = [g for g in store_data.keys() if g not in _dining_option_config_cache]
    if guids_needing_config:
        async with httpx.AsyncClient(timeout=30.0) as cfg_client:
            for i in range(0, len(guids_needing_config), 10):
                batch = guids_needing_config[i:i+10]
                results = await asyncio.gather(
                    *[_fetch_dining_option_configs(cfg_client, g) for g in batch],
                    return_exceptions=True,
                )
                for guid, result in zip(batch, results):
                    if isinstance(result, dict):
                        dining_option_maps[guid] = result
                    else:
                        logger.warning("Dining option config failed for %s: %s", guid[:8], result)
                        dining_option_maps[guid] = {}
    for guid in store_data.keys():
        if guid not in dining_option_maps:
            dining_option_maps[guid] = _dining_option_config_cache.get(guid, {})

    # Fetch tips from cached orders (one restaurant+day at a time, same as tip pool)
    async with httpx.AsyncClient(timeout=120.0) as tips_client:
        tips_data = await _fetch_tips_by_channel(
            tips_client, list(store_data.keys()), week_start, week_end_date, dining_option_maps,
        )

    # Build response
    stores_list = []
    for guid in sorted(store_data.keys(), key=lambda g: guid_to_ext.get(g, g)):
        ext_id = guid_to_ext.get(guid, "")
        name = guid_to_name.get(guid, guid[:8])
        daily_entries = store_data[guid]

        weekly_cashier = {"checkCount": 0, "netSales": 0.0, "tips": 0.0}
        weekly_kiosk = {"checkCount": 0, "netSales": 0.0, "tips": 0.0}
        daily_list = []

        for d_key in sorted(daily_entries.keys()):
            day = daily_entries[d_key]
            c_tips = tips_data.get(guid, {}).get(d_key, {}).get("cashier", 0.0)
            k_tips = tips_data.get(guid, {}).get(d_key, {}).get("kiosk", 0.0)

            c_checks = int(day["cashier"]["checkCount"])
            k_checks = int(day["kiosk"]["checkCount"])
            c_net = round(day["cashier"]["netSales"], 2)
            k_net = round(day["kiosk"]["netSales"], 2)
            c_tips = round(c_tips, 2)
            k_tips = round(k_tips, 2)
            c_tpc = round(c_tips / c_checks, 2) if c_checks else 0.0
            k_tpc = round(k_tips / k_checks, 2) if k_checks else 0.0

            formatted_date = f"{d_key[:4]}-{d_key[4:6]}-{d_key[6:8]}"

            daily_list.append({
                "date": formatted_date,
                "cashier": {"checkCount": c_checks, "netSales": c_net, "tips": c_tips, "tipsPerCheck": c_tpc},
                "kiosk": {"checkCount": k_checks, "netSales": k_net, "tips": k_tips, "tipsPerCheck": k_tpc},
            })

            weekly_cashier["checkCount"] += c_checks
            weekly_cashier["netSales"] += c_net
            weekly_cashier["tips"] += c_tips
            weekly_kiosk["checkCount"] += k_checks
            weekly_kiosk["netSales"] += k_net
            weekly_kiosk["tips"] += k_tips

        weekly_cashier["netSales"] = round(weekly_cashier["netSales"], 2)
        weekly_cashier["tips"] = round(weekly_cashier["tips"], 2)
        weekly_cashier["tipsPerCheck"] = round(weekly_cashier["tips"] / weekly_cashier["checkCount"], 2) if weekly_cashier["checkCount"] else 0.0
        weekly_kiosk["netSales"] = round(weekly_kiosk["netSales"], 2)
        weekly_kiosk["tips"] = round(weekly_kiosk["tips"], 2)
        weekly_kiosk["tipsPerCheck"] = round(weekly_kiosk["tips"] / weekly_kiosk["checkCount"], 2) if weekly_kiosk["checkCount"] else 0.0

        stores_list.append({
            "storeId": ext_id,
            "storeName": name,
            "restaurantGuid": guid,
            "weekly": {"cashier": weekly_cashier, "kiosk": weekly_kiosk},
            "daily": daily_list,
        })

    return {
        "weekName": week_end_date.isoformat(),
        "weekStart": week_start.isoformat(),
        "weekEnd": week_end_date.isoformat(),
        "stores": stores_list,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


@app.post("/api/cashier-vs-kiosk")
async def cashier_vs_kiosk_report(payload: dict = Body(...)):
    """
    Cashier vs Kiosk Counts and Dollars report.
    Expects: {"week_end_date": "YYYY-MM-DD", "locations": [...] (optional)}
    """
    wed_str = payload.get("week_end_date")
    if not wed_str:
        raise HTTPException(status_code=400, detail="week_end_date required (YYYY-MM-DD)")
    try:
        week_end = date.fromisoformat(wed_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="week_end_date must be YYYY-MM-DD")

    location_filter = payload.get("locations") or None
    try:
        return await _build_cashier_vs_kiosk_data(week_end, location_filter)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Cashier vs Kiosk report failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")


@app.get("/api/cashier-vs-kiosk/excel")
async def cashier_vs_kiosk_excel(week_end_date: str):
    """
    Excel export of the Cashier vs Kiosk report.
    Returns base64-encoded xlsx with filename.
    """
    try:
        week_end = date.fromisoformat(week_end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="week_end_date must be YYYY-MM-DD")

    data = await _build_cashier_vs_kiosk_data(week_end)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = data["weekEnd"]

    header_font_white = Font(bold=True, size=10, color="FFFFFF")
    header_fill_blue = PatternFill("solid", fgColor="1E3A5F")
    header_fill_green = PatternFill("solid", fgColor="2E7D32")
    bold_font = Font(bold=True, size=10)
    money_fmt = '#,##0.00'
    int_fmt = '#,##0'
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    # Title
    ws.merge_cells("A1:P1")
    ws["A1"] = "Cashier vs Kiosk Counts and Dollars"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A3"] = f"Report generated at:  {data['generatedAt']}"

    # Column group headers
    ws["I4"] = "Cashier"
    ws["I4"].font = header_font_white
    ws["I4"].fill = header_fill_blue
    ws.merge_cells("I4:L4")
    ws["M4"] = "Kiosk"
    ws["M4"].font = header_font_white
    ws["M4"].fill = header_fill_green
    ws.merge_cells("M4:P4")

    # Column headers row
    col_headers = [
        ("A", "Week Name"), ("B", "Week Start"), ("C", "Week End"),
        ("D", "Store ID"), ("E", "Store Name"), ("F", "Date Of Business"),
        ("I", "Check Count"), ("J", "Net Sales"), ("K", "Tips"), ("L", "Tips Per Check"),
        ("M", "Check Count"), ("N", "Net Sales"), ("O", "Tips"), ("P", "Tips Per Check"),
    ]
    for col_letter, label in col_headers:
        cell = ws[f"{col_letter}5"]
        cell.value = label
        cell.font = bold_font
        cell.border = thin_border
        if col_letter in ("I", "J", "K", "L"):
            cell.fill = PatternFill("solid", fgColor="D6E4F0")
        elif col_letter in ("M", "N", "O", "P"):
            cell.fill = PatternFill("solid", fgColor="C8E6C9")

    # Widths
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 35
    ws.column_dimensions["F"].width = 18
    for c in "IJKLMNOP":
        ws.column_dimensions[c].width = 14

    row_num = 6
    for store in data["stores"]:
        # Weekly total row
        ws[f"A{row_num}"] = data["weekEnd"]
        ws[f"B{row_num}"] = data["weekStart"]
        ws[f"C{row_num}"] = data["weekEnd"]
        ws[f"D{row_num}"] = store["storeId"]
        ws[f"E{row_num}"] = store["storeName"]
        ws[f"F{row_num}"] = "Weekly Total"
        ws[f"F{row_num}"].font = bold_font

        wc = store["weekly"]["cashier"]
        wk = store["weekly"]["kiosk"]
        ws[f"I{row_num}"] = wc["checkCount"]
        ws[f"J{row_num}"] = wc["netSales"]
        ws[f"K{row_num}"] = wc["tips"]
        ws[f"L{row_num}"] = wc["tipsPerCheck"]
        ws[f"M{row_num}"] = wk["checkCount"]
        ws[f"N{row_num}"] = wk["netSales"]
        ws[f"O{row_num}"] = wk["tips"]
        ws[f"P{row_num}"] = wk["tipsPerCheck"]

        for c in "IJKLMNOP":
            cell = ws[f"{c}{row_num}"]
            cell.border = thin_border
            if c in ("J", "K", "L", "N", "O", "P"):
                cell.number_format = money_fmt
            else:
                cell.number_format = int_fmt
        for c in "ABCDEF":
            ws[f"{c}{row_num}"].font = bold_font

        row_num += 1

        # Daily rows
        for day in store["daily"]:
            ws[f"F{row_num}"] = day["date"]
            dc = day["cashier"]
            dk = day["kiosk"]
            ws[f"I{row_num}"] = dc["checkCount"]
            ws[f"J{row_num}"] = dc["netSales"]
            ws[f"K{row_num}"] = dc["tips"]
            ws[f"L{row_num}"] = dc["tipsPerCheck"]
            ws[f"M{row_num}"] = dk["checkCount"]
            ws[f"N{row_num}"] = dk["netSales"]
            ws[f"O{row_num}"] = dk["tips"]
            ws[f"P{row_num}"] = dk["tipsPerCheck"]

            for c in "IJKLMNOP":
                cell = ws[f"{c}{row_num}"]
                cell.border = thin_border
                if c in ("J", "K", "L", "N", "O", "P"):
                    cell.number_format = money_fmt
                else:
                    cell.number_format = int_fmt
            row_num += 1

    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    filename = f"Cashier_vs_Kiosk_{data['weekEnd']}.xlsx"
    return {
        "filename": filename,
        "data": base64.b64encode(xlsx_bytes).decode("ascii"),
    }


# ------------------
# Static Frontend
# ------------------

frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
menuboard_path = os.path.join(os.path.dirname(__file__), "menuboard")
kiosk_path = os.path.join(os.path.dirname(__file__), "kiosk")
media_path = UPLOAD_ROOT

if os.path.isdir(menuboard_path):
    from starlette.responses import FileResponse as _FileResponse

    @app.get("/menuboard")
    async def menuboard_redirect():
        from starlette.responses import RedirectResponse
        return RedirectResponse("/menuboard/")

    @app.get("/menuboard/{rest:path}")
    async def menuboard_spa_fallback(rest: str):
        if rest:
            file_path = os.path.join(menuboard_path, rest)
            if os.path.isfile(file_path):
                return _FileResponse(file_path)
        return _FileResponse(os.path.join(menuboard_path, "index.html"))

# ── Monkey Media (Catering) API ─────────────────────────────────────


@app.get("/api/monkey/stats")
async def monkey_stats():
    """Return summary statistics about the Monkey SQLite database."""
    if not monkey_db.db_available():
        return {"available": False}
    stats = monkey_db.get_stats()
    stats["available"] = True
    return stats


@app.post("/api/monkey/reports/sales-by-channel")
async def monkey_sales_by_channel(payload: dict = Body(...)):
    """Catering sales by channel and weekday."""
    return monkey_db.rpt_sales_by_channel_by_weekday(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/monkey/reports/delivery-by-partner")
async def monkey_delivery_by_partner(payload: dict = Body(...)):
    """Delivery order counts by delivery partner per store."""
    return monkey_db.rpt_delivery_counts_by_partner(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/monkey/reports/catering-by-handoff")
async def monkey_catering_by_handoff(payload: dict = Body(...)):
    """Catering orders by pickup vs delivery per store."""
    return monkey_db.rpt_catering_by_handoff_mode(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.get("/api/monkey/reports/client-stats")
async def monkey_client_stats():
    """All clients with at least one order — totals and last order."""
    return monkey_db.rpt_client_stats()


@app.get("/api/monkey/reports/top-clients")
async def monkey_top_clients(min_total: float = 500.0):
    """Clients with lifetime sales over threshold."""
    return monkey_db.rpt_top_clients_by_sales(min_total=min_total)


@app.post("/api/monkey/reports/discounts-by-user")
async def monkey_discounts_by_user(payload: dict = Body(...)):
    """Discounts applied to orders by entering/editing agent."""
    return monkey_db.rpt_discounts_by_user(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/monkey/reports/refunds-by-user")
async def monkey_refunds_by_user(payload: dict = Body(...)):
    """Refunds by who issued them."""
    return monkey_db.rpt_refunds_by_user(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/monkey/reports/catering-over-500")
async def monkey_catering_over_500(payload: dict = Body(...)):
    """Clients who spent over $500 in a given month."""
    return monkey_db.rpt_catering_over_500(
        payload["year"], payload["month"],
    )


@app.post("/api/monkey/reports/order-notes")
async def monkey_order_notes(payload: dict = Body(...)):
    """Orders with notes populated."""
    return monkey_db.rpt_order_notes(
        payload["startDate"], payload["endDate"],
        store_ids=payload.get("storeIds"),
    )


@app.get("/api/monkey/lock-the-day")
async def monkey_lock_the_day(date: str, storeId: int | None = None):
    """Single-date view of orders blocking accounting lock."""
    return monkey_db.lock_the_day_summary(biz_date=date, store_id=storeId)


@app.post("/api/monkey/lock-the-day/notify")
async def monkey_lock_the_day_notify(payload: dict = Body(...)):
    """
    Send reminder emails for unposted orders.
    Expects: { "date": "YYYY-MM-DD", "blockers": [{ orderId, storeName, storeEmail, storeManagerEmail, issue }] }
    Only sends for blockers with "Not Posted" in their issue.
    Groups orders by store so each store gets one email.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from collections import defaultdict

    biz_date = payload.get("date", "")
    blockers = payload.get("blockers", [])

    # Filter to only "Not Posted" orders
    unposted = [b for b in blockers if "Not Posted" in b.get("issue", "")]
    if not unposted:
        return {"sent": 0, "message": "No unposted orders to notify about."}

    gmail_user = os.getenv("GMAIL_ADDRESS")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    cc_addresses = ["lpearson@newks.com", "itdepartment@newks.com"]

    if not gmail_user or not gmail_pass:
        raise HTTPException(status_code=500, detail="GMAIL_ADDRESS / GMAIL_APP_PASSWORD not configured in .env")

    # Group orders by store
    by_store: dict = defaultdict(list)
    store_emails: dict = {}
    for b in unposted:
        key = b.get("storeName", "Unknown")
        by_store[key].append(b)
        # Collect all non-empty emails for this store
        emails = set()
        if b.get("storeEmail"):
            emails.add(b["storeEmail"])
        if b.get("storeManagerEmail"):
            emails.add(b["storeManagerEmail"])
        if key not in store_emails:
            store_emails[key] = emails
        else:
            store_emails[key].update(emails)

    sent = 0
    errors = []

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_pass)

        for store_name, orders in by_store.items():
            to_addrs = list(store_emails.get(store_name, set()))
            if not to_addrs:
                errors.append(f"No email on file for {store_name}")
                continue

            # Build order list for the email body
            order_lines = "\n".join(
                f"  - Order {o['orderId']}"
                + (f" ({o['clientName']})" if o.get("clientName") else "")
                + (f" — ${o['total']:,.2f}" if o.get("total") else "")
                for o in orders
            )

            body = (
                f"Good Morning Team,\n\n"
                f"As part of our daily review, we noticed the following unposted "
                f"{'order' if len(orders) == 1 else 'orders'} in Monkey Media "
                f"from {biz_date} that still {'needs' if len(orders) == 1 else 'need'} attention.\n\n"
                f"{order_lines}\n\n"
                f"When you have a moment, please post "
                f"{'this order' if len(orders) == 1 else 'these orders'}. "
                f"Posting orders promptly helps ensure sales are accurately reflected "
                f"and prevents delays with reporting and credit card batching "
                f"in Monkey Media.\n\n"
                f"If you have any questions or run into any issues, please let us "
                f"know — we're happy to help."
            )

            msg = MIMEMultipart()
            send_as = os.getenv("GMAIL_SEND_AS", gmail_user)
            msg["From"] = send_as
            msg["To"] = ", ".join(to_addrs)
            msg["Cc"] = ", ".join(cc_addresses)
            msg["Subject"] = f"Monkey Media — Unposted Order{'s' if len(orders) > 1 else ''} for {store_name} ({biz_date})"
            msg.attach(MIMEText(body, "plain"))

            all_recipients = to_addrs + cc_addresses
            server.sendmail(gmail_user, all_recipients, msg.as_string())
            sent += 1
            logger.info("Sent unposted-order reminder to %s for %s (%d orders)",
                        to_addrs, store_name, len(orders))

        server.quit()
    except Exception as exc:
        logger.error("SMTP error sending lock-the-day reminders: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Email send failed: {exc}")

    return {"sent": sent, "errors": errors}


@app.post("/api/monkey/sync")
async def monkey_sync_now(payload: dict = Body(default={})):
    """Manually trigger Monkey Media sync (incremental, last 3 days by default)."""
    days = payload.get("days", 3)

    async def _run():
        try:
            result = await asyncio.to_thread(_run_monkey_sync, days=days)
            _nightly_monkey_last_result.update({
                "status": "success",
                "rows": result.get("rows", 0),
                "elapsed": result.get("elapsed", 0),
                "ran_at": datetime.now().isoformat(),
            })
        except Exception as exc:
            logger.error("Manual Monkey sync failed: %s", exc, exc_info=True)
            _nightly_monkey_last_result.update({
                "status": "failed",
                "error": str(exc),
                "ran_at": datetime.now().isoformat(),
            })

    asyncio.create_task(_run())
    return {"status": "started", "days": days}


@app.get("/api/monkey/sync/status")
async def monkey_sync_status():
    """Return the last Monkey sync result."""
    return _nightly_monkey_last_result if _nightly_monkey_last_result else {"status": "never_run"}


@app.get("/api/admin/nightly-menu-precompute/status")
async def nightly_menu_precompute_status():
    """Last-run status of the nightly Menu Performance precompute job.

    Shape: {status, ok_count, error_count, total_shapes, elapsed_s, ran_at, shapes[]}
    Each shape entry has {label, status, duration_s, cache, cache_key, ...}.
    """
    return _nightly_menu_precompute_last_result if _nightly_menu_precompute_last_result else {"status": "never_run"}


@app.post("/api/admin/nightly-menu-precompute/run-now")
async def nightly_menu_precompute_run_now(admin_user: User = Depends(get_current_admin_user)):
    """
    Trigger the nightly menu-precompute job immediately (instead of waiting
    for the 5:00 AM cron). Useful for testing / one-off warming after a
    mapping-table edit. Returns immediately; poll the /status endpoint
    above for completion.
    """
    asyncio.create_task(nightly_menu_precompute_job())
    return {"status": "started", "started_at": datetime.now().isoformat()}


@app.get("/api/admin/nightly-reconcile/status")
async def nightly_reconcile_status():
    """
    Last-run status of the nightly orders cache + reconciliation job.

    Shape:
      {
        business_date:   "20260527",
        window_days:     7,
        window:          "20260521..20260527",
        success:         88,    # new yesterday pulls
        reconciled:      560,   # re-pulls of older window days (6 × 88-ish)
        failed:          2,
        skipped:         0,
        changes_detected: 3,    # large changes that crossed thresholds
        alert_sent:      true,
        completed_at:    "2026-05-28T03:48:12"
      }
    """
    return _nightly_job_last_result if _nightly_job_last_result else {"status": "never_run"}


@app.post("/api/admin/nightly-reconcile/run-now")
async def nightly_reconcile_run_now(admin_user: User = Depends(get_current_admin_user)):
    """
    Trigger the nightly orders cache + reconciliation job immediately.
    Useful for testing the reconciliation logic + alert email without
    waiting for the 3:00 AM cron. Returns right away — poll the /status
    endpoint above for completion. Takes ~5-15 minutes for a 7-day window
    across ~90 stores.
    """
    asyncio.create_task(nightly_orders_cache_job())
    return {"status": "started", "started_at": datetime.now().isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Contacts section — replaces the legacy forms.nfc.local PHP CRUD pages.
#
# Data lives in SQL-PROD's `contacts` DB (untouched by this app — we just
# read/write through it). Audit triggers in SQL Server record every change
# because we always stamp LastUpdatedBy with the authenticated user's name.
#
# Auth: any logged-in user can read; only admin role can create/update/
# delete. Mirrors the spirit of Don's old AD-group restrictions but uses
# our existing app user model instead of LDAP.
#
# v1: AreaDir (proof-of-concept). v2+ rolls the same pattern out to
# BillingContacts, Company, CCProcessors, etc.
# ─────────────────────────────────────────────────────────────────────────────

import contacts_db


class AreaDirectorIn(BaseModel):
    """Input shape for INSERT/UPDATE. All fields optional except names
    aren't validated here — backend composes AreaDirName from First+Last
    if not explicit (matches Don's legacy behavior)."""
    AreaDirName:      Optional[str] = None
    AreaDirFirstName: Optional[str] = None
    AreaDirLastName:  Optional[str] = None
    AreaDirEmail:     Optional[str] = None
    AreaDirSMGEmail:  Optional[str] = None
    AreaDirCFPW:      Optional[str] = None
    AreaDirCFID:      Optional[str] = None
    CompanyID:        Optional[int] = None


@app.get("/api/contacts/health")
async def contacts_health(_user: User = Depends(get_current_user)):
    """Status of the local SQLite contacts mirror — row counts, file mtime, path."""
    return contacts_db.get_metadata()


@app.get("/api/contacts/companies")
async def contacts_list_companies(_user: User = Depends(get_current_user)):
    """Companies list — used to populate the CompanyID dropdown."""
    return contacts_db.list_companies()


@app.get("/api/contacts/areadir")
async def contacts_list_areadir(_user: User = Depends(get_current_user)):
    """List all Area Directors. Any logged-in user can view."""
    try:
        return contacts_db.list_area_directors()
    except Exception as exc:
        logger.error("contacts.list_area_directors failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.get("/api/contacts/areadir/{areadir_id}")
async def contacts_get_areadir(areadir_id: int, _user: User = Depends(get_current_user)):
    row = contacts_db.get_area_director(areadir_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"AreaDir {areadir_id} not found")
    return row


@app.post("/api/contacts/areadir")
async def contacts_create_areadir(
    body: AreaDirectorIn,
    admin_user: User = Depends(get_current_admin_user),
):
    """Create a new Area Director. Admin only."""
    try:
        new_id = contacts_db.insert_area_director(body.dict(), updated_by=admin_user.name)
        logger.info("AreaDir created id=%s by %s", new_id, admin_user.email)
        return {"AreaDirID": new_id, "success": True}
    except Exception as exc:
        logger.error("contacts.insert_area_director failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.put("/api/contacts/areadir/{areadir_id}")
async def contacts_update_areadir(
    areadir_id: int,
    body: AreaDirectorIn,
    admin_user: User = Depends(get_current_admin_user),
):
    """Update an existing Area Director. Admin only."""
    if not contacts_db.get_area_director(areadir_id):
        raise HTTPException(status_code=404, detail=f"AreaDir {areadir_id} not found")
    try:
        ok = contacts_db.update_area_director(areadir_id, body.dict(), updated_by=admin_user.name)
        logger.info("AreaDir updated id=%s by %s", areadir_id, admin_user.email)
        return {"AreaDirID": areadir_id, "success": ok}
    except Exception as exc:
        logger.error("contacts.update_area_director failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.delete("/api/contacts/areadir/{areadir_id}")
async def contacts_delete_areadir(
    areadir_id: int,
    admin_user: User = Depends(get_current_admin_user),
):
    """Delete an Area Director. Admin only. Stamps LastUpdatedBy first
    so the audit trigger records who initiated the delete."""
    if not contacts_db.get_area_director(areadir_id):
        raise HTTPException(status_code=404, detail=f"AreaDir {areadir_id} not found")
    try:
        ok = contacts_db.delete_area_director(areadir_id, updated_by=admin_user.name)
        logger.info("AreaDir deleted id=%s by %s", areadir_id, admin_user.email)
        return {"AreaDirID": areadir_id, "success": ok}
    except Exception as exc:
        logger.error("contacts.delete_area_director failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


# ─── Store master ─────────────────────────────────────────────────────────────

@app.get("/api/contacts/stores")
async def contacts_list_stores(_user: User = Depends(get_current_user)):
    """List all stores. Returns every column from the SQLite Store table —
    list view picks which to display, edit modal renders all."""
    try:
        return contacts_db.list_stores()
    except Exception as exc:
        logger.error("contacts.list_stores failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.get("/api/contacts/stores/{store_id}")
async def contacts_get_store(store_id: int, _user: User = Depends(get_current_user)):
    row = contacts_db.get_store(store_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Store {store_id} not found")
    return row


@app.put("/api/contacts/stores/{store_id}")
async def contacts_update_store(
    store_id: int,
    body: Dict[str, Any] = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Update a store. Admin only. Body is a flat dict of column → value;
    unknown/protected columns are silently dropped."""
    if not contacts_db.get_store(store_id):
        raise HTTPException(status_code=404, detail=f"Store {store_id} not found")
    try:
        ok = contacts_db.update_store(store_id, body, updated_by=admin_user.name)
        logger.info("Store updated id=%s by %s (%d fields touched)",
                    store_id, admin_user.email, len(body))
        return {"StoreID": store_id, "success": ok}
    except Exception as exc:
        logger.error("contacts.update_store failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.post("/api/contacts/stores")
async def contacts_create_store(
    body: Dict[str, Any] = Body(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Create a new store. Admin only. Caller must supply StoreID
    (Newk's restaurant IDs are externally assigned, not auto-incremented)."""
    if not body.get("StoreID"):
        raise HTTPException(status_code=400, detail="StoreID is required for new stores")
    if contacts_db.get_store(int(body["StoreID"])):
        raise HTTPException(status_code=409, detail=f"Store {body['StoreID']} already exists")
    try:
        new_id = contacts_db.insert_store(body, updated_by=admin_user.name)
        logger.info("Store created id=%s by %s", new_id, admin_user.email)
        return {"StoreID": new_id, "success": True}
    except Exception as exc:
        logger.error("contacts.insert_store failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")


@app.get("/api/monkey/alerts/not-accepted")
async def monkey_alert_not_accepted():
    """Orders due within 24 hours not yet accepted."""
    return monkey_db.alert_orders_not_accepted()


@app.get("/api/monkey/alerts/not-posted")
async def monkey_alert_not_posted():
    """Confirmed orders with no POS order ID."""
    return monkey_db.alert_orders_not_posted()


@app.get("/api/monkey/alerts/unconfirmed")
async def monkey_alert_unconfirmed():
    """Online orders within last 7 days that remain unconfirmed."""
    return monkey_db.alert_unconfirmed_orders()


@app.get("/api/monkey/stores")
async def monkey_stores():
    """List all Monkey stores."""
    monkey_db.initialize_db()
    conn = monkey_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT store_id, store_name, store_code, city, state, status "
            "FROM stores WHERE status = 'Active' ORDER BY store_name"
        ).fetchall()
        return [
            {"storeId": r[0], "storeName": r[1], "storeCode": r[2],
             "city": r[3], "state": r[4], "status": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


# ── Monkey Media Azure SQL Explorer (read-only) ────────────────────

def _run_monkey_azure_query(sql: str, params: tuple = ()) -> dict:
    """Execute a read-only query against the Monkey Azure SQL DB.

    Restricted to a single SELECT statement. Returns {columns, rows, rowCount}.
    """
    import re
    import sync_monkey as sm

    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise HTTPException(status_code=400, detail="Empty query")
    if ";" in s:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed")
    if not re.match(r"^(?:select|with)\b", s, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only SELECT/WITH queries are allowed")
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|merge|exec|execute|sp_|xp_)\b",
        re.IGNORECASE,
    )
    if forbidden.search(s):
        raise HTTPException(status_code=400, detail="Query contains a forbidden keyword")

    conn = sm.get_azure_connection()
    try:
        cur = conn.cursor()
        cur.execute(s, params) if params else cur.execute(s)
        cols = [d[0] for d in cur.description] if cur.description else []
        raw = cur.fetchmany(2000)
        rows = []
        for r in raw:
            row = []
            for v in r:
                if isinstance(v, datetime):
                    v = v.strftime("%Y-%m-%d %H:%M:%S")
                elif v is not None and not isinstance(v, (int, float, str, bool)):
                    v = str(v)
                row.append(v)
            rows.append(row)
        return {"columns": cols, "rows": rows, "rowCount": len(rows)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.get("/api/monkey/explore/tables")
async def monkey_explore_tables():
    """List all tables in the msr schema on Azure SQL."""
    return _run_monkey_azure_query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA='msr' ORDER BY TABLE_NAME"
    )


@app.get("/api/monkey/explore/columns")
async def monkey_explore_columns(table: str):
    """List columns for a given msr.<table>."""
    import re
    if not re.match(r"^[A-Za-z0-9_]+$", table or ""):
        raise HTTPException(status_code=400, detail="Invalid table name")
    return _run_monkey_azure_query(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA='msr' AND TABLE_NAME='{table}' "
        "ORDER BY ORDINAL_POSITION"
    )


@app.post("/api/monkey/explore/query")
async def monkey_explore_query(payload: dict = Body(...)):
    """Run an arbitrary read-only SELECT against Monkey Azure SQL."""
    return _run_monkey_azure_query(payload.get("sql", ""))


@app.post("/api/monkey/explore/search-guid")
async def monkey_explore_search_guid(payload: dict = Body(...)):
    """Search every string column in msr.* for a GUID-like value.

    Returns a list of {table, column, matchCount, sample} hits.
    """
    guid = (payload.get("guid") or "").strip().lower()
    tables_filter = payload.get("tables") or []  # optional list of table names (LIKE patterns)
    import re
    if not re.match(r"^[0-9a-f-]{8,64}$", guid):
        raise HTTPException(status_code=400, detail="Invalid guid")

    import sync_monkey as sm
    conn = sm.get_azure_connection()
    hits = []
    try:
        cur = conn.cursor()
        base_q = (
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='msr' AND DATA_TYPE IN "
            "('char','varchar','nchar','nvarchar','text','ntext','uniqueidentifier')"
        )
        if tables_filter:
            safe = [t for t in tables_filter if re.match(r"^[A-Za-z0-9_%]+$", t)]
            if safe:
                like_clauses = " OR ".join([f"TABLE_NAME LIKE '{t}'" for t in safe])
                base_q += f" AND ({like_clauses})"
        cur.execute(base_q)
        cols = cur.fetchall()
        total = len(cols)
        logger.info("[guid-search] scanning %d columns for '%s'", total, guid)
        for i, (tbl, col, dtype) in enumerate(cols, 1):
            try:
                logger.info("[guid-search] (%d/%d) msr.%s.%s (%s)", i, total, tbl, col, dtype)
                q = (
                    f"SELECT TOP 5 [{col}] FROM msr.[{tbl}] "
                    f"WHERE LOWER(CAST([{col}] AS NVARCHAR(200))) LIKE '%{guid}%'"
                )
                c2 = conn.cursor()
                c2.execute(q)
                samples = c2.fetchall()
                if samples:
                    logger.info("[guid-search]   ✓ HIT in msr.%s.%s (%d sample rows)", tbl, col, len(samples))
                    hits.append({
                        "table": tbl,
                        "column": col,
                        "dataType": dtype,
                        "matchCount": len(samples),
                        "sample": str(samples[0][0]) if samples[0][0] is not None else None,
                    })
            except Exception as e:
                # Skip columns that error (permissions, etc.)
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"guid": guid, "hits": hits, "hitCount": len(hits)}


# ── CrunchTime (Food Cost / COGS) API ──────────────────────────────


@app.get("/api/crunchtime/stats")
async def crunchtime_stats():
    """Return summary statistics about the CrunchTime SQLite database."""
    if not crunchtime_db.db_available():
        return {"available": False}
    stats = crunchtime_db.get_stats()
    stats["available"] = True
    return stats


@app.get("/api/crunchtime/periods")
async def crunchtime_periods():
    """List available reporting periods."""
    return crunchtime_db.get_available_periods()


@app.get("/api/crunchtime/categories")
async def crunchtime_categories():
    """List available COGS categories."""
    return crunchtime_db.get_available_categories()


@app.get("/api/crunchtime/gl-codes")
async def crunchtime_gl_codes():
    """List available GL codes."""
    return crunchtime_db.get_available_gl_codes()


@app.post("/api/crunchtime/reports/actual-costs")
async def crunchtime_actual_costs(payload: dict = Body(...)):
    """Actual costs by store and fiscal period."""
    return crunchtime_db.rpt_actual_costs_by_period(
        fiscal_period=payload.get("fiscalPeriod"),
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/crunchtime/reports/costs-by-gl")
async def crunchtime_costs_by_gl(payload: dict = Body(...)):
    """Actual costs by GL code per store."""
    return crunchtime_db.rpt_costs_by_gl_code(
        reporting_period=payload.get("reportingPeriod"),
        gl_code=payload.get("glCode"),
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/crunchtime/reports/cogs-by-product")
async def crunchtime_cogs_by_product(payload: dict = Body(...)):
    """COGS by product — actual vs theoretical."""
    return crunchtime_db.rpt_cogs_by_product(
        reporting_period=payload.get("reportingPeriod"),
        category=payload.get("category"),
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/crunchtime/reports/cogs-summary")
async def crunchtime_cogs_summary(payload: dict = Body(...)):
    """COGS summary by category per store."""
    return crunchtime_db.rpt_cogs_summary_by_category(
        reporting_period=payload.get("reportingPeriod"),
        store_ids=payload.get("storeIds"),
    )


@app.post("/api/crunchtime/reports/gross-profit")
async def crunchtime_gross_profit(payload: dict = Body(...)):
    """Gross profit by menu item."""
    return crunchtime_db.rpt_gross_profit_by_item(
        fiscal_week_start=payload.get("fiscalWeekStart"),
        store_ids=payload.get("storeIds"),
    )


# ── Punchh Loyalty Reports ────────────────────────────────────────

@app.get("/api/punchh/stats")
async def punchh_stats():
    return punchh_db.get_stats()

@app.get("/api/punchh/stores")
async def punchh_stores():
    conn = punchh_db.get_connection()
    rows = conn.execute(
        "SELECT DISTINCT store_number, location_name FROM coupon WHERE store_number IS NOT NULL AND store_number != '' ORDER BY store_number"
    ).fetchall()
    conn.close()
    return [{"storeNumber": r[0], "locationName": r[1] or ""} for r in rows]

@app.post("/api/punchh/reports/daily-point-balance")
async def punchh_daily_points(payload: dict = Body(...)):
    return punchh_db.rpt_daily_point_balance(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/coupon-dollars")
async def punchh_coupon_dollars(payload: dict = Body(...)):
    return punchh_db.rpt_coupon_dollars_counts(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/coupon-summary")
async def punchh_coupon_summary(payload: dict = Body(...)):
    return punchh_db.rpt_coupon_summary(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/redeemable-dollars")
async def punchh_redeemable_dollars(payload: dict = Body(...)):
    return punchh_db.rpt_redeemable_dollars_counts(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/redeemable-summary")
async def punchh_redeemable_summary(payload: dict = Body(...)):
    return punchh_db.rpt_redeemable_summary(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/summary-rollup")
async def punchh_summary_rollup(payload: dict = Body(...)):
    return punchh_db.rpt_summary_rollup(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        store_numbers=payload.get("storeNumbers"),
    )

@app.post("/api/punchh/reports/google-ads-export")
async def punchh_google_ads(payload: dict = Body(...)):
    return punchh_db.rpt_google_ads_export(
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
    )


# ── CrunchTime Sigma Excel Upload ─────────────────────────────────

@app.post("/api/admin/crunchtime/upload-sigma")
async def admin_upload_sigma_excel(
    file: UploadFile = File(...),
    admin_user: User = Depends(get_current_admin_user),
):
    """Upload a Sigma/BizIQ Excel export to update CrunchTime data."""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="File must be .xlsx or .xls")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        results = crunchtime_db.import_sigma_excel(tmp.name)
        total = sum(results.values())
        logger.info("Sigma upload by %s: %d total rows (%s)", admin_user.email, total, results)
        return {
            "status": "success",
            "tables": results,
            "totalRows": total,
            "stats": crunchtime_db.get_stats(),
        }
    except Exception as e:
        logger.error("Sigma upload failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp.name)


if os.path.isdir(kiosk_path):
    app.mount("/kiosk", StaticFiles(directory=kiosk_path, html=True), name="kiosk")

if os.path.isdir(media_path):
    app.mount("/media/menu-board", StaticFiles(directory=media_path, html=False), name="menu_board_media")

app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")