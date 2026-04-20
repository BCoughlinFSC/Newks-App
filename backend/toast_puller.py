"""
Toast API Puller — replaces Don Wilson's PHP pipeline on dev-jan02.

Pulls orders/checks/items from Toast's /orders/v2/ordersBulk endpoint
and writes directly to SQL-PROD's toast.dbo.* tables. Idempotent —
re-runs are safe and result in MERGE-style upserts.

Usage (CLI):
  python toast_puller.py --store 1040 --date 2026-04-06
  python toast_puller.py --store 1040 --start-date 2026-04-01 --end-date 2026-04-06
  python toast_puller.py --store 1040 --date 2026-04-06 --skip-rollup

Usage (Web UI):
  python toast_puller.py --serve
  → opens http://localhost:8001 with a simple repoll form

Requires .env with:
  TOAST_STANDARD_API_CLIENT_ID, TOAST_STANDARD_API_CLIENT_SECRET
  ALOHA_SQL_SERVER, ALOHA_SQL_USERNAME, ALOHA_SQL_PASSWORD
  TOAST_SQL_DATABASE=toast
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv

# Load .env from the backend/ directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# Import token helper from existing code
from backfill_orders import get_standard_auth_headers, TOAST_ORDERS_API_BASE_URL

# Import our DB helpers
import toast_puller_db as db

# ── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BASE_DELAY = 2  # seconds — exponential: 2, 4, 8, 16, 32
PAGE_SIZE = 100  # Toast supports up to 100 (Don's PHP uses 10)


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class PullResult:
    store_num: int
    store_guid: str
    business_date: date
    pages_fetched: int
    orders_count: int
    checks_count: int
    items_count: int
    duration_sec: float
    errors: list = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


# ── API fetch with retry ─────────────────────────────────────────────────────

async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict,
    store_num: int,
    page: int,
) -> Optional[list]:
    """
    Fetch one page of ordersBulk with exponential-backoff retry on 5xx.
    Returns list of orders, empty list (end of pagination), or None (all retries failed).
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=60.0)

            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else []

            if resp.status_code == 401:
                log.warning("[%s] 401 on page %d — token may have expired, refreshing...", store_num, page)
                new_headers = await get_standard_auth_headers(client)
                headers.update(new_headers)
                continue

            if 500 <= resp.status_code < 600:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning(
                    "[%s] Page %d returned %d — retrying in %ds (attempt %d/%d)",
                    store_num, page, resp.status_code, delay, attempt, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            # 4xx (other than 401) — don't retry
            log.error("[%s] Page %d returned %d: %s", store_num, page, resp.status_code, resp.text[:200])
            return None

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(
                "[%s] Page %d network error (%s) — retrying in %ds (attempt %d/%d)",
                store_num, page, type(e).__name__, delay, attempt, MAX_RETRIES,
            )
            await asyncio.sleep(delay)

    log.error("[%s] Page %d failed after %d retries — giving up", store_num, page, MAX_RETRIES)
    return None


async def fetch_orders_with_retry(
    client: httpx.AsyncClient,
    restaurant_guid: str,
    store_num: int,
    business_date: str,
) -> tuple[list, list[str]]:
    """
    Paginated fetch of all orders for one store + one date with retry logic.
    Returns (orders_list, errors_list).
    """
    headers = await get_standard_auth_headers(client)
    headers["Toast-Restaurant-External-ID"] = restaurant_guid

    url = f"{TOAST_ORDERS_API_BASE_URL}/orders/v2/ordersBulk"
    all_orders = []
    errors = []
    page = 1

    while True:
        params = {"businessDate": business_date, "pageSize": PAGE_SIZE, "page": page}
        orders = await _fetch_page(client, url, headers, params, store_num, page)

        if orders is None:
            errors.append(f"Page {page} failed permanently after {MAX_RETRIES} retries")
            break

        if not orders:
            break  # empty page = end of pagination

        all_orders.extend(orders)
        page += 1
        await asyncio.sleep(0.15)  # mild rate limiting

    return all_orders, errors


# ── JSON → row dict parsers ──────────────────────────────────────────────────

def _safe_str(val, max_len=None) -> Optional[str]:
    """Convert a value to string, handling None and nested dicts."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("guid") or val.get("entityType") or str(val)
    s = str(val)
    return s[:max_len] if max_len else s


def _parse_bool(val) -> int:
    """Convert Toast boolean (could be bool, string, None) to 0/1."""
    if val is None:
        return 0
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, str):
        return 1 if val.lower() in ("true", "1") else 0
    return int(bool(val))


def _parse_timestamp(val) -> Optional[datetime]:
    """
    Convert Toast timestamp strings to Python datetime for pyodbc.
    Toast formats: "2026-04-06T12:34:56.789+0000", "2026-04-06T12:34:56.000-0500",
    or integer timestamps like 1712345678000 (milliseconds since epoch).
    """
    if not val:
        return None
    if isinstance(val, (int, float)):
        # Millisecond epoch timestamp
        try:
            return datetime.utcfromtimestamp(val / 1000.0)
        except (ValueError, OSError):
            return None
    s = str(val)
    if not s or s == "null":
        return None
    # Normalize timezone offset: "+0000" → "+00:00", "-0500" → "-05:00"
    import re
    s = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)
    # Try parsing common formats
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    log.warning("Could not parse timestamp: %s", s[:50])
    return None


def _parse_business_date(val):
    """Convert Toast businessDate (integer YYYYMMDD or string) to a date object."""
    if val is None:
        return None
    if isinstance(val, date):
        return val
    s = str(val).replace("-", "")
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    # Try as ISO string
    try:
        return datetime.strptime(str(val), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_orders(raw_orders: list, store_guid: str, store_num: int, business_date: str) -> dict:
    """
    Parse Toast ordersBulk response into row dicts for each table.
    Returns {"orders": [...], "checks": [...], "items": [...]}.
    """
    order_rows = []
    check_rows = []
    item_rows = []

    biz_date = datetime.strptime(business_date, "%Y%m%d").date() if len(business_date) == 8 else business_date

    for order in raw_orders:
        order_guid = order.get("guid", "")
        dining_option = order.get("diningOption") or {}
        dining_option_guid = dining_option.get("guid") if isinstance(dining_option, dict) else _safe_str(dining_option)
        revenue_center = order.get("revenueCenter") or {}
        rc_guid = revenue_center.get("guid") if isinstance(revenue_center, dict) else _safe_str(revenue_center)
        server_info = order.get("server") or {}
        server_guid = server_info.get("guid") if isinstance(server_info, dict) else _safe_str(server_info)

        # ── Order row ────────────────────────────────────────────────────
        order_rows.append({
            "OrderID": order_guid,
            "DateOfBusiness": biz_date,
            "StoreID": store_guid,
            "StoreNum": store_num,
            "RevenueCenter": rc_guid,
            "OrderMode": dining_option_guid,
            "Server": server_guid,
            "DisplayNumber": _safe_str(order.get("displayNumber")),
            "LastModifiedDevice": _safe_str(order.get("lastModifiedDevice")),
            "Source": _safe_str(order.get("source")),
            "Duration": order.get("duration"),
            "DayPart": _safe_str(order.get("dayPart")),
            "ExcessFood": _safe_str(order.get("excessFood")),
            "Tab": _safe_str(order.get("tab")),
            "RequiredPrepTime": _safe_str(order.get("requiredPrepTime")),
            "ApprovalStatus": _safe_str(order.get("approvalStatus")),
            "CreatedDevice": _safe_str(order.get("createdDevice")),
            "NumberOfGuests": order.get("numberOfGuests", 0),
            "ChannelID": _safe_str(order.get("channelGuid")),
            "PricingFeatures": _safe_str(order.get("pricingFeatures")),
            "CreatedDate": _parse_timestamp(order.get("createdDate")),
            "OpenedDate": _parse_timestamp(order.get("openedDate")),
            "EstimatedFulfillmentDate": _parse_timestamp(order.get("estimatedFulfillmentDate")),
            "ClosedDate": _parse_timestamp(order.get("closedDate")),
            "Deleted": _parse_bool(order.get("deleted")),
            "DeletedDate": _parse_timestamp(order.get("deletedDate")),
            "ModifiedDate": _parse_timestamp(order.get("modifiedDate")),
            "PromisedDate": _parse_timestamp(order.get("promisedDate")),
            "Voided": _parse_bool(order.get("voided")),
            "VoidDate": _parse_timestamp(order.get("voidDate")),
            "VoidBusinessDate": _parse_business_date(order.get("voidBusinessDate")),
        })

        # ── Check rows ───────────────────────────────────────────────────
        for check in order.get("checks", []):
            check_guid = check.get("guid", "")

            check_rows.append({
                "CheckID": check_guid,
                "DateOfBusiness": biz_date,
                "DayPart": _safe_str(order.get("dayPart")),
                "StoreID": store_guid,
                "StoreNum": store_num,
                "OrderID": order_guid,
                "DisplayNumber": _safe_str(check.get("displayNumber")),
                "PaymentStatus": _safe_str(check.get("paymentStatus")),
                "TabName": _safe_str(check.get("tabName")),
                "TaxExempt": _parse_bool(check.get("taxExempt")),
                "TaxExemptState": _safe_str(check.get("taxExemptState")),
                "TaxExemptNumber": _safe_str(check.get("taxExemptNumber")),
                "Amount": check.get("amount", 0),
                "DiscountAmount": check.get("discountAmount"),
                "TotalAmount": check.get("totalAmount", 0),
                "TaxAmount": check.get("taxAmount", 0),
                "CreatedDevice": _safe_str(check.get("createdDevice")),
                "CustomerID": _safe_str(check.get("customer", {}).get("guid") if isinstance(check.get("customer"), dict) else check.get("customer")),
                "LastModifiedDevice": _safe_str(check.get("lastModifiedDevice")),
                "OrderMode": dining_option_guid,
                "CreatedTime": _parse_timestamp(check.get("createdDate")),
                "OpenedTime": _parse_timestamp(check.get("openedDate")),
                "ClosedTime": _parse_timestamp(check.get("closedDate")),
                "ModifiedTime": _parse_timestamp(check.get("modifiedDate")),
                "Deleted": _parse_bool(check.get("deleted")),
                "DeletedTime": _parse_timestamp(check.get("deletedDate")),
                "Voided": _parse_bool(check.get("voided")),
                "VoidTime": _parse_timestamp(check.get("voidDate")),
            })

            # ── Item rows (flatten the selection tree) ───────────────────
            for sel in check.get("selections", []):
                _flatten_selection(
                    sel, item_rows,
                    order_guid=order_guid,
                    check_guid=check_guid,
                    store_guid=store_guid,
                    store_num=store_num,
                    biz_date=biz_date,
                    daypart=_safe_str(order.get("dayPart")),
                    order_mode=dining_option_guid,
                    parent_guid=None,
                    depth=0,
                )

    return {"orders": order_rows, "checks": check_rows, "items": item_rows}


def _flatten_selection(
    sel: dict,
    item_rows: list,
    *,
    order_guid: str,
    check_guid: str,
    store_guid: str,
    store_num: int,
    biz_date,
    daypart: str,
    order_mode: str,
    parent_guid: Optional[str],
    depth: int,
):
    """Recursively flatten a Toast selection (item + modifiers) into hstItem rows."""
    sel_guid = sel.get("guid", "")
    item_info = sel.get("item") or {}
    item_guid = item_info.get("guid") if isinstance(item_info, dict) else _safe_str(item_info)
    sales_cat = sel.get("salesCategory") or {}
    sales_cat_guid = sales_cat.get("guid") if isinstance(sales_cat, dict) else _safe_str(sales_cat)

    # Determine modification fields based on depth
    if depth == 0:
        mod_id = "0"
        mod_item_id = ""
        sub_mod_id = "0"
        sub_mod_item_id = ""
        effective_parent = sel_guid  # top-level: parentguid = self
    elif depth == 1:
        mod_id = sel_guid
        mod_item_id = sel_guid
        sub_mod_id = "0"
        sub_mod_item_id = ""
        effective_parent = parent_guid
    else:
        mod_id = "0"
        mod_item_id = ""
        sub_mod_id = sel_guid
        sub_mod_item_id = sel_guid
        effective_parent = parent_guid

    # Get appliedDiscounts total
    discounts = sel.get("appliedDiscounts") or []
    discount_amount = sum(
        (d.get("discountAmount") or 0) for d in discounts if isinstance(d, dict)
    )

    # Price fields
    price = sel.get("price", 0) or 0
    pre_discount = sel.get("preDiscountPrice")
    disc_price = pre_discount if pre_discount is not None else price

    # MultiLocationId (MLI) — Toast sends this on the item object
    mli = item_info.get("multiLocationId") if isinstance(item_info, dict) else None

    item_rows.append({
        "DateOfBusiness": biz_date,
        "StoreID": store_guid,
        "StoreNum": store_num,
        "OrderID": order_guid,
        "CheckID": check_guid,
        "DayPart": daypart,
        "DayPartID": None,  # populated later by sp_Update_Sold_Items_From_Check
        "SelectionID": sel_guid,
        "ParentGUID": effective_parent,
        "ItemGUID": item_guid,
        "ParentID": None,  # populated later from cfg_menu_items
        "ItemID": None,  # populated later from cfg_menu_items
        "ModificationID": mod_id,
        "ModifierItemID": mod_item_id,
        "SubModificationID": sub_mod_id,
        "SubModifierItemID": sub_mod_item_id,
        "Deferred": _parse_bool(sel.get("deferred")),
        "PreModifier": _safe_str(sel.get("preModifier")),
        "Quantity": sel.get("quantity", 0) or 0,
        "SalesCategoryGUID": sales_cat_guid,
        "OrderMode": order_mode,
        "Price": price,
        "DiscPric": disc_price,
        "Tax": sel.get("tax", 0) or 0,
        "TaxInclusion": _safe_str(sel.get("taxInclusion")),
        "SystemTime": _parse_timestamp(sel.get("createdDate")),
        "Voided": _parse_bool(sel.get("voided")),
        "VoidDate": _parse_timestamp(sel.get("voidDate")),
        "VoidReason": _safe_str(sel.get("voidReason")),
        "AppliedDiscountAmount": discount_amount,
        "MenuItemMLI": mli,
        "ParentMLI": None,  # populated later
    })

    # Recurse into modifiers
    for mod in sel.get("modifiers", []):
        _flatten_selection(
            mod, item_rows,
            order_guid=order_guid,
            check_guid=check_guid,
            store_guid=store_guid,
            store_num=store_num,
            biz_date=biz_date,
            daypart=daypart,
            order_mode=order_mode,
            parent_guid=sel_guid,
            depth=depth + 1,
        )


# ── Main pull function ───────────────────────────────────────────────────────

async def pull_store_for_date(
    store_num: int,
    business_date: str,
    restaurant_guid: str = None,
    skip_rollup: bool = False,
) -> PullResult:
    """
    Pull one store's data for one business date from Toast API and write
    to SQL Server. This is the main entry point.

    Args:
        store_num: The Newk's store number (e.g. 1040)
        business_date: YYYYMMDD format (e.g. "20260406") or YYYY-MM-DD
        restaurant_guid: Toast restaurant GUID (looked up if not provided)
        skip_rollup: If True, don't run sp_toast_SalesDataByDayPart at the end
    """
    start = time.time()

    # Normalize date format
    if "-" in str(business_date):
        biz_date_str = str(business_date).replace("-", "")
        biz_date_sql = str(business_date)
    else:
        biz_date_str = str(business_date)
        biz_date_sql = f"{biz_date_str[:4]}-{biz_date_str[4:6]}-{biz_date_str[6:8]}"

    log.info("=" * 60)
    log.info("Pulling store %s for %s", store_num, biz_date_sql)
    log.info("=" * 60)

    # Look up restaurant GUID if not provided
    conn = db.get_toast_connection()
    try:
        if not restaurant_guid:
            row = conn.execute(
                "SELECT GUID FROM dbo.restaurants WHERE StoreID = ?", store_num
            ).fetchone()
            if not row:
                raise ValueError(f"Store {store_num} not found in toast.dbo.restaurants")
            restaurant_guid = row[0]
            log.info("Resolved store %s → GUID %s", store_num, restaurant_guid)
    finally:
        conn.close()

    # Fetch from Toast API
    log.info("Fetching orders from Toast API (page size %d)...", PAGE_SIZE)
    async with httpx.AsyncClient(timeout=120.0) as client:
        orders, errors = await fetch_orders_with_retry(
            client, restaurant_guid, store_num, biz_date_str
        )

    if not orders and errors:
        duration = time.time() - start
        log.error("No orders fetched — all pages failed")
        return PullResult(
            store_num=store_num, store_guid=restaurant_guid,
            business_date=biz_date_sql, pages_fetched=0,
            orders_count=0, checks_count=0, items_count=0,
            duration_sec=duration, errors=errors,
        )

    log.info("Fetched %d orders from Toast API", len(orders))

    # Parse JSON into row dicts
    parsed = parse_orders(orders, restaurant_guid, store_num, biz_date_str)
    log.info(
        "Parsed: %d orders, %d checks, %d items",
        len(parsed["orders"]), len(parsed["checks"]), len(parsed["items"]),
    )

    # Write to SQL Server
    conn = db.get_toast_connection()
    try:
        log.info("Upserting orders to hstOrder...")
        db.upsert_orders(conn, parsed["orders"])

        log.info("Upserting checks to hstCheck...")
        db.upsert_checks(conn, parsed["checks"])

        log.info("Inserting items to hstItem_temp and running sp_Update_Sold_Items_From_Check...")
        db.upsert_items(conn, parsed["items"])

        if not skip_rollup:
            log.info("Running rollup (sp_toast_SalesDataByDayPart)...")
            db.run_rollup(conn, biz_date_sql, biz_date_sql, store_grouping=8, store_group=store_num)
        else:
            log.info("Skipping rollup (--skip-rollup flag)")

    finally:
        conn.close()

    duration = time.time() - start
    result = PullResult(
        store_num=store_num,
        store_guid=restaurant_guid,
        business_date=biz_date_sql,
        pages_fetched=len(orders) // PAGE_SIZE + 1,
        orders_count=len(parsed["orders"]),
        checks_count=len(parsed["checks"]),
        items_count=len(parsed["items"]),
        duration_sec=duration,
        errors=errors,
    )

    log.info("=" * 60)
    if result.success:
        log.info(
            "SUCCESS — store %s, date %s: %d orders, %d checks, %d items in %.1fs",
            store_num, biz_date_sql, result.orders_count,
            result.checks_count, result.items_count, duration,
        )
    else:
        log.warning(
            "PARTIAL — store %s, date %s: %d orders fetched but %d errors: %s",
            store_num, biz_date_sql, result.orders_count,
            len(errors), "; ".join(errors),
        )
    log.info("=" * 60)

    return result


# ── Web UI (HTML served inline) ──────────────────────────────────────────────

PULLER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Toast Puller</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; padding: 20px; }
  .container { max-width: 700px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 4px; color: #1a1a1a; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 24px; margin-bottom: 16px; }
  .form-row { display: flex; gap: 12px; margin-bottom: 16px; align-items: end; }
  .form-group { flex: 1; }
  .form-group label { display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 4px; }
  .form-group input, .form-group select { width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
  .form-group input:focus, .form-group select:focus { outline: none; border-color: #e85d26; box-shadow: 0 0 0 2px rgba(232,93,38,0.15); }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
  .checkbox-row input { width: auto; }
  .checkbox-row label { font-size: 13px; color: #555; }
  button { background: #e85d26; color: #fff; border: none; padding: 10px 24px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  button:hover { background: #d14d1a; }
  button:disabled { background: #ccc; cursor: not-allowed; }
  .status { margin-top: 16px; }
  .log-box { background: #1a1a2e; color: #e0e0e0; border-radius: 6px; padding: 16px; font-family: 'Consolas', 'Monaco', monospace; font-size: 13px; line-height: 1.6; max-height: 400px; overflow-y: auto; white-space: pre-wrap; }
  .log-box .info { color: #90caf9; }
  .log-box .success { color: #81c784; }
  .log-box .error { color: #ef9a9a; }
  .log-box .warn { color: #ffe082; }
  .result-cards { display: flex; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
  .result-card { background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 6px; padding: 12px 16px; flex: 1; min-width: 120px; text-align: center; }
  .result-card .num { font-size: 24px; font-weight: 700; color: #1a1a1a; }
  .result-card .label { font-size: 12px; color: #888; margin-top: 2px; }
  .result-card.ok { border-color: #81c784; }
  .result-card.err { border-color: #ef9a9a; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <h1>Toast Puller</h1>
  <p class="subtitle">Repoll sales data from Toast API &rarr; SQL Server (replaces SSH + PHP)</p>

  <div class="card">
    <div class="form-row">
      <div class="form-group">
        <label>Store</label>
        <select id="storeNum">
          <option value="">Loading stores...</option>
        </select>
      </div>
      <div class="form-group">
        <label>Start Date</label>
        <input type="date" id="startDate">
      </div>
      <div class="form-group">
        <label>End Date</label>
        <input type="date" id="endDate">
      </div>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="skipRollup">
      <label for="skipRollup">Skip rollup (sp_toast_SalesDataByDayPart) — faster, use when batch-repolling multiple stores</label>
    </div>
    <button id="pullBtn" onclick="startPull()">Pull Sales Data</button>
  </div>

  <div class="status" id="statusArea" style="display:none;">
    <div class="card">
      <div class="log-box" id="logBox"></div>
      <div class="result-cards" id="resultCards" style="display:none;"></div>
    </div>
  </div>
</div>

<script>
// Set default dates to yesterday
const yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
document.getElementById('startDate').value = yesterday.toISOString().split('T')[0];
document.getElementById('endDate').value = yesterday.toISOString().split('T')[0];

// Load store list from API
(async function loadStores() {
  try {
    const resp = await fetch('/api/stores');
    const data = await resp.json();
    const sel = document.getElementById('storeNum');
    sel.innerHTML = '<option value="ALL">All Live Toast Stores</option>';
    for (const s of data.stores) {
      sel.innerHTML += `<option value="${s.store_num}">${s.store_num} - ${s.name}</option>`;
    }
  } catch(e) {
    document.getElementById('storeNum').innerHTML = '<option value="">Error loading stores</option>';
  }
})();

function appendLog(msg, cls) {
  const box = document.getElementById('logBox');
  const span = document.createElement('span');
  span.className = cls || '';
  span.textContent = msg + '\\n';
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

async function startPull() {
  const store = document.getElementById('storeNum').value;
  const startDate = document.getElementById('startDate').value;
  const endDate = document.getElementById('endDate').value;
  const skipRollup = document.getElementById('skipRollup').checked;
  const btn = document.getElementById('pullBtn');

  if (!store || !startDate || !endDate) { alert('Please fill in all fields'); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Pulling...';
  document.getElementById('statusArea').style.display = 'block';
  document.getElementById('resultCards').style.display = 'none';
  document.getElementById('logBox').innerHTML = '';

  appendLog(`Starting pull for store ${store} (${startDate} to ${endDate})...`, 'info');

  try {
    const resp = await fetch('/api/pull', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        store_num: store,
        start_date: startDate,
        end_date: endDate,
        skip_rollup: skipRollup,
      }),
    });
    const data = await resp.json();

    if (!resp.ok || data.error || data.detail) {
      appendLog(`ERROR: ${data.error || data.detail || JSON.stringify(data)}`, 'error');
    } else if (!data.results || !Array.isArray(data.results)) {
      appendLog(`ERROR: Unexpected response: ${JSON.stringify(data).substring(0, 500)}`, 'error');
    } else {
      for (const r of data.results) {
        const prefix = r.store_num ? `[${r.store_num}] ${r.business_date}` : r.business_date;
        if (r.errors && r.errors.length > 0) {
          appendLog(`${prefix}: PARTIAL — ${r.checks_count} checks, ${r.items_count} items (${r.errors.join('; ')})`, 'warn');
        } else {
          appendLog(`${prefix}: ${r.checks_count} checks, ${r.items_count} items in ${r.duration_sec.toFixed(1)}s`, 'success');
        }
      }

      // Show summary cards
      const totalChecks = data.results.reduce((s,r) => s + r.checks_count, 0);
      const totalItems = data.results.reduce((s,r) => s + r.items_count, 0);
      const totalTime = data.results.reduce((s,r) => s + r.duration_sec, 0);
      const totalErrors = data.results.reduce((s,r) => s + (r.errors ? r.errors.length : 0), 0);

      const cards = document.getElementById('resultCards');
      cards.innerHTML = `
        <div class="result-card ${totalErrors === 0 ? 'ok' : 'err'}">
          <div class="num">${totalChecks}</div><div class="label">Checks</div>
        </div>
        <div class="result-card ${totalErrors === 0 ? 'ok' : 'err'}">
          <div class="num">${totalItems}</div><div class="label">Items</div>
        </div>
        <div class="result-card">
          <div class="num">${totalTime.toFixed(1)}s</div><div class="label">Duration</div>
        </div>
        <div class="result-card ${totalErrors === 0 ? 'ok' : 'err'}">
          <div class="num">${totalErrors}</div><div class="label">Errors</div>
        </div>
      `;
      cards.style.display = 'flex';
      appendLog(`\\nDone! Total: ${totalChecks} checks, ${totalItems} items, ${totalTime.toFixed(1)}s`, totalErrors === 0 ? 'success' : 'warn');
    }
  } catch (e) {
    appendLog(`Network error: ${e.message}`, 'error');
  }

  btn.disabled = false;
  btn.textContent = 'Pull Sales Data';
}
</script>
</body>
</html>
"""


HEALTH_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pipeline Health</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; padding: 20px; }
  .container { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 20px; }
  .nav { margin-bottom: 20px; display: flex; gap: 12px; }
  .nav a { text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; color: #555; background: #e9ecef; }
  .nav a.active { background: #e85d26; color: #fff; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }
  .badge.ok { background: #d4edda; color: #155724; }
  .badge.fail { background: #f8d7da; color: #721c24; }
  .badge.warn { background: #fff3cd; color: #856404; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px; border-bottom: 2px solid #dee2e6; color: #555; font-weight: 600; }
  td { padding: 6px 8px; border-bottom: 1px solid #f0f0f0; }
  tr:hover { background: #f8f9fa; }
  .status-ok { color: #28a745; font-weight: 600; }
  .status-fail { color: #dc3545; font-weight: 600; }
  .status-warn { color: #ffc107; font-weight: 600; }
  .summary-cards { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .summary-card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 16px 20px; flex: 1; min-width: 150px; text-align: center; }
  .summary-card .num { font-size: 32px; font-weight: 700; }
  .summary-card .label { font-size: 12px; color: #888; margin-top: 4px; }
  .summary-card.green .num { color: #28a745; }
  .summary-card.red .num { color: #dc3545; }
  .summary-card.orange .num { color: #fd7e14; }
  .summary-card.blue .num { color: #007bff; }
  button.rerun { background: #6c757d; color: #fff; border: none; padding: 4px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; }
  button.rerun:hover { background: #5a6268; }
  .refresh-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .refresh-bar select { padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
  button.refresh { background: #e85d26; color: #fff; border: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .loading { text-align: center; padding: 40px; color: #888; }
  .error-msg { background: #f8d7da; color: #721c24; padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
  .empty { color: #888; padding: 16px; text-align: center; font-style: italic; }
</style>
</head>
<body>
<div class="container">
  <h1>Pipeline Health Dashboard</h1>
  <p class="subtitle">Overnight job status, proc failures, and Toast poll completeness</p>

  <div class="nav">
    <a href="/">Toast Puller</a>
    <a href="/health" class="active">Pipeline Health</a>
  </div>

  <div class="refresh-bar">
    <div>
      <label>Look back: </label>
      <select id="hoursBack" onchange="loadHealth()">
        <option value="12">12 hours</option>
        <option value="24" selected>24 hours</option>
        <option value="48">48 hours</option>
        <option value="72">3 days</option>
        <option value="168">7 days</option>
      </select>
    </div>
    <button class="refresh" onclick="loadHealth()">Refresh</button>
  </div>

  <div class="summary-cards" id="summaryCards">
    <div class="summary-card"><div class="num">-</div><div class="label">Loading...</div></div>
  </div>

  <div id="content"><div class="loading">Loading pipeline health data...</div></div>
</div>

<script>
async function loadHealth() {
  const hours = document.getElementById('hoursBack').value;
  const content = document.getElementById('content');
  content.innerHTML = '<div class="loading">Loading...</div>';

  try {
    const resp = await fetch(`/api/health?hours_back=${hours}`);
    const data = await resp.json();
    if (data.error) { content.innerHTML = `<div class="error-msg">${data.error}</div>`; return; }
    renderHealth(data);
  } catch(e) {
    content.innerHTML = `<div class="error-msg">Failed to load: ${e.message}</div>`;
  }
}

function renderHealth(data) {
  // Summary cards
  const jobsOk = data.jobs.filter(j => j.status === 'Succeeded').length;
  const jobsFail = data.jobs.filter(j => j.status === 'Failed').length;
  const buildsOk = data.table_builds.filter(b => b.success === true).length;
  const buildsFail = data.table_builds.filter(b => b.success === false).length;
  const pollErrors = data.poll_errors.length;
  const storeGaps = data.store_completeness.length;

  document.getElementById('summaryCards').innerHTML = `
    <div class="summary-card green"><div class="num">${jobsOk}</div><div class="label">Jobs OK</div></div>
    <div class="summary-card ${jobsFail > 0 ? 'red' : 'green'}"><div class="num">${jobsFail}</div><div class="label">Jobs Failed</div></div>
    <div class="summary-card ${buildsFail > 0 ? 'red' : 'green'}"><div class="num">${buildsFail}</div><div class="label">Builds Failed</div></div>
    <div class="summary-card ${pollErrors > 0 ? 'orange' : 'green'}"><div class="num">${pollErrors}</div><div class="label">Poll Errors</div></div>
    <div class="summary-card ${storeGaps > 0 ? 'red' : 'green'}"><div class="num">${storeGaps}</div><div class="label">Store Data Gaps</div></div>
  `;

  let html = '';

  // SQL Agent Jobs
  html += '<div class="card"><h2>SQL Agent Jobs</h2>';
  if (data.jobs.length === 0) { html += '<div class="empty">No jobs found in this window</div>'; }
  else {
    html += '<table><tr><th>Job</th><th>Status</th><th>Run Date</th><th>Duration</th></tr>';
    for (const j of data.jobs) {
      const cls = j.status === 'Succeeded' ? 'status-ok' : j.status === 'Failed' ? 'status-fail' : 'status-warn';
      const dur = j.run_duration ? String(j.run_duration).padStart(6,'0').replace(/(\\d{2})(\\d{2})(\\d{2})/, '$1:$2:$3') : '-';
      const dt = String(j.run_date);
      const tm = String(j.run_time).padStart(6, '0');
      const dateStr = `${dt.slice(0,4)}-${dt.slice(4,6)}-${dt.slice(6,8)} ${tm.slice(0,2)}:${tm.slice(2,4)}`;
      html += `<tr><td>${j.job_name}</td><td class="${cls}">${j.status}</td><td>${dateStr}</td><td>${dur}</td></tr>`;
    }
    html += '</table>';
  }
  html += '</div>';

  // Table Builds (failures only, or all if few)
  const failedBuilds = data.table_builds.filter(b => b.success === false);
  if (failedBuilds.length > 0) {
    html += '<div class="card"><h2>Failed Table Builds <span class="badge fail">' + failedBuilds.length + ' failed</span></h2>';
    html += '<table><tr><th>Database</th><th>Proc</th><th>Start</th><th>Error</th><th>Action</th></tr>';
    for (const b of failedBuilds) {
      const start = b.start_time ? b.start_time.slice(0,19) : '-';
      html += `<tr>
        <td>${b.db_name || '-'}</td>
        <td>${b.proc_name}</td>
        <td>${start}</td>
        <td class="status-fail">${b.error_message || 'Unknown error'}</td>
        <td><button class="rerun" onclick="rerunProc('${b.db_name}.dbo.${b.proc_name}')">Re-run</button></td>
      </tr>`;
    }
    html += '</table></div>';
  }

  // Toast Poll Errors
  if (data.poll_errors.length > 0) {
    html += '<div class="card"><h2>Toast Poll Errors <span class="badge warn">' + data.poll_errors.length + '</span></h2>';
    html += '<table><tr><th>Time</th><th>Level</th><th>Message</th></tr>';
    for (const e of data.poll_errors) {
      const t = e.log_time ? e.log_time.slice(0,19) : '-';
      const cls = e.level.includes('Error') ? 'status-fail' : 'status-warn';
      html += `<tr><td>${t}</td><td class="${cls}">${e.level}</td><td>${e.message.substring(0, 200)}</td></tr>`;
    }
    html += '</table></div>';
  }

  // Store Completeness Gaps
  if (data.store_completeness.length > 0) {
    html += `<div class="card"><h2>Store Data Gaps (${data.completeness_date}) <span class="badge fail">${data.store_completeness.length} stores</span></h2>`;
    html += '<table><tr><th>Store</th><th>hstCheck</th><th>hstItem Checks</th><th>Gap</th><th>Check $</th><th>Rollup $</th><th>Action</th></tr>';
    for (const s of data.store_completeness) {
      html += `<tr>
        <td><strong>${s.store_num}</strong></td>
        <td>${s.check_count}</td>
        <td>${s.item_check_count}</td>
        <td class="status-fail">${s.gap}</td>
        <td>$${s.check_amount.toLocaleString('en-US', {minimumFractionDigits: 2})}</td>
        <td>$${s.rollup_net.toLocaleString('en-US', {minimumFractionDigits: 2})}</td>
        <td><a href="/?store=${s.store_num}&date=${data.completeness_date}">Repoll</a></td>
      </tr>`;
    }
    html += '</table></div>';
  }

  // Truncated Poll Days (504 timeout fingerprint)
  if (data.truncated_polls && data.truncated_polls.length > 0) {
    html += `<div class="card"><h2>Truncated Polls — Incomplete Data <span class="badge fail">${data.truncated_polls.length} stores</span></h2>`;
    html += '<p style="color:#666;font-size:13px;margin-bottom:12px;">These stores closed significantly earlier than normal yesterday — likely caused by Toast API 504 timeouts during the nightly poll. Click Repoll to fix.</p>';
    html += '<table><tr><th>Store</th><th>Checks</th><th>Last Close (CDT)</th><th>Normal Close</th><th>Minutes Early</th><th>Check $</th><th>Action</th></tr>';
    for (const s of data.truncated_polls) {
      const closeCdt = s.latest_close_cdt ? s.latest_close_cdt.slice(11,16) : '-';
      const normalClose = s.normal_close_time ? s.normal_close_time.slice(11,16) : '-';
      html += `<tr>
        <td><strong>${s.store_num}</strong></td>
        <td>${s.check_count}</td>
        <td class="status-fail">${closeCdt}</td>
        <td>${normalClose}</td>
        <td class="status-fail">${s.minutes_early} min</td>
        <td>$${s.check_amount.toLocaleString('en-US', {minimumFractionDigits: 2})}</td>
        <td><a href="/?store=${s.store_num}&date=${data.completeness_date}">Repoll</a></td>
      </tr>`;
    }
    html += '</table></div>';
  }

  // Aloha→Toast Transition Gaps
  if (data.transition_gaps && data.transition_gaps.length > 0) {
    html += `<div class="card"><h2>Aloha→Toast Transition Gaps <span class="badge fail">${data.transition_gaps.length} stores</span></h2>`;
    html += '<p style="color:#666;font-size:13px;margin-bottom:12px;">These stores transitioned from Aloha to Toast but their pre-Toast Aloha data is missing from the rollup. Fix: temporarily remove from group 243, run sp_SalesDataByDayPart for the missing dates, re-add to group 243.</p>';
    html += '<table><tr><th>Store</th><th>Name</th><th>Toast Live</th><th>Rollup Starts</th><th>Missing Days</th><th>Aloha Source Rows</th></tr>';
    for (const g of data.transition_gaps) {
      html += `<tr>
        <td><strong>${g.store_id}</strong></td>
        <td>${g.store_name}</td>
        <td>${g.toast_live ? g.toast_live.slice(0,10) : '-'}</td>
        <td class="status-fail">${g.rollup_earliest ? g.rollup_earliest.slice(0,10) : '-'}</td>
        <td>${g.missing_days}</td>
        <td>${g.aloha_source_rows.toLocaleString()}</td>
      </tr>`;
    }
    html += '</table></div>';
  }

  // All clear message
  const transGaps = (data.transition_gaps || []).length;
  if (jobsFail === 0 && buildsFail === 0 && pollErrors === 0 && storeGaps === 0 && transGaps === 0) {
    html += '<div class="card" style="text-align:center;padding:40px;"><h2 style="color:#28a745;">All Systems Healthy</h2><p style="color:#666;margin-top:8px;">No failures, no errors, no data gaps detected.</p></div>';
  }

  document.getElementById('content').innerHTML = html;
}

async function rerunProc(procName) {
  if (!confirm('Re-run ' + procName + '?')) return;
  try {
    const resp = await fetch(`/api/rerun?proc_name=${encodeURIComponent(procName)}`, {method: 'POST'});
    const data = await resp.json();
    if (data.success) {
      alert('Successfully re-ran ' + procName);
      loadHealth();
    } else {
      alert('Error: ' + (data.error || 'Unknown'));
    }
  } catch(e) { alert('Failed: ' + e.message); }
}

// Load on page load
loadHealth();
</script>
</body>
</html>
"""

# Also add nav link to the puller page
PULLER_HTML = PULLER_HTML.replace(
    '<p class="subtitle">Repoll sales data from Toast API &rarr; SQL Server (replaces SSH + PHP)</p>',
    '<p class="subtitle">Repoll sales data from Toast API &rarr; SQL Server (replaces SSH + PHP)</p>'
    + '\\n  <div class="nav" style="margin-bottom:20px;display:flex;gap:12px;">'
    + '<a href="/" style="text-decoration:none;padding:8px 16px;border-radius:6px;font-size:13px;font-weight:600;color:#fff;background:#e85d26;">Toast Puller</a>'
    + '<a href="/health" style="text-decoration:none;padding:8px 16px;border-radius:6px;font-size:13px;font-weight:600;color:#555;background:#e9ecef;">Pipeline Health</a>'
    + '</div>'
)


# ── FastAPI web server (for --serve mode) ────────────────────────────────────

def serve():
    """Run a tiny FastAPI server with the puller UI."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn

    app = FastAPI(title="Toast Puller")

    class PullRequest(BaseModel):
        store_num: str  # can be int or "ALL"
        start_date: str
        end_date: str
        skip_rollup: bool = False

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PULLER_HTML

    @app.post("/api/pull")
    async def api_pull(req: PullRequest):
        try:
            start = datetime.strptime(req.start_date, "%Y-%m-%d").date()
            end = datetime.strptime(req.end_date, "%Y-%m-%d").date()
            dates = []
            d = start
            while d <= end:
                dates.append(d.isoformat())
                d += timedelta(days=1)

            # Resolve store list
            if req.store_num == "ALL":
                conn = db.get_toast_connection()
                stores = db.get_live_toast_stores(conn)
                conn.close()
                store_nums = [s["store_num"] for s in stores]
                log.info("Pulling ALL %d live Toast stores", len(store_nums))
            else:
                store_nums = [int(req.store_num)]

            results = []
            for store_num in store_nums:
                for d in dates:
                    r = await pull_store_for_date(
                        store_num=store_num,
                        business_date=d,
                        skip_rollup=req.skip_rollup,
                    )
                    results.append({
                        "store_num": r.store_num,
                        "business_date": r.business_date,
                        "orders_count": r.orders_count,
                        "checks_count": r.checks_count,
                        "items_count": r.items_count,
                        "duration_sec": r.duration_sec,
                        "errors": r.errors,
                        "success": r.success,
                    })
            return {"results": results}
        except Exception as e:
            log.exception("Pull failed")
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/api/stores")
    async def api_stores():
        """Get list of live Toast stores for the dropdown."""
        try:
            conn = db.get_toast_connection()
            stores = db.get_live_toast_stores(conn)
            conn.close()
            return {"stores": stores}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    # ── Health Dashboard ────────────────────────────────────────────────
    @app.get("/health", response_class=HTMLResponse)
    async def health_page():
        return HEALTH_HTML

    @app.get("/api/health")
    async def api_health(hours_back: int = 24):
        try:
            conn = db.get_toast_connection()
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            result = {
                "jobs": db.get_overnight_job_status(conn, hours_back),
                "table_builds": db.get_table_build_status(conn, hours_back),
                "proc_runs": db.get_proc_run_status(conn, hours_back),
                "poll_errors": db.get_toast_poll_errors(conn, hours_back),
                "store_completeness": db.get_store_poll_completeness(conn, yesterday),
                "truncated_polls": db.get_truncated_poll_days(conn, yesterday),
                "transition_gaps": db.get_aloha_toast_transition_gaps(conn),
                "as_of": datetime.now().isoformat(),
                "hours_back": hours_back,
                "completeness_date": yesterday,
            }
            conn.close()
            return result
        except Exception as e:
            log.exception("Health check failed")
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.post("/api/rerun")
    async def api_rerun(proc_name: str, params: str = ""):
        try:
            conn = db.get_toast_connection()
            result = db.rerun_proc(conn, proc_name, params)
            conn.close()
            return result
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    print("=" * 50)
    print("  Toast Puller & Pipeline Health")
    print("  http://localhost:8001        — Repoll UI")
    print("  http://localhost:8001/health — Pipeline Health")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Toast API Puller — pull sales data for a store")
    parser.add_argument("--serve", action="store_true", help="Start web UI on http://localhost:8001")
    parser.add_argument("--store", type=int, help="Store number (e.g. 1040)")
    parser.add_argument("--date", type=str, help="Business date YYYY-MM-DD (single day)")
    parser.add_argument("--start-date", type=str, help="Start date YYYY-MM-DD (for range)")
    parser.add_argument("--end-date", type=str, help="End date YYYY-MM-DD (for range)")
    parser.add_argument("--skip-rollup", action="store_true", help="Skip sp_toast_SalesDataByDayPart")
    parser.add_argument("--guid", type=str, help="Restaurant GUID (auto-looked up if omitted)")
    args = parser.parse_args()

    if args.serve:
        serve()
        return

    if not args.store:
        parser.error("--store is required (or use --serve for web UI)")

    # Determine date range
    if args.date:
        dates = [args.date]
    elif args.start_date and args.end_date:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        dates = []
        d = start
        while d <= end:
            dates.append(d.isoformat())
            d += timedelta(days=1)
    else:
        parser.error("Either --date or both --start-date and --end-date are required")

    log.info("Toast Puller starting — store %d, dates: %s", args.store, dates)

    results = []
    for d in dates:
        result = asyncio.run(
            pull_store_for_date(
                store_num=args.store,
                business_date=d,
                restaurant_guid=args.guid,
                skip_rollup=args.skip_rollup,
            )
        )
        results.append(result)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        status = "OK" if r.success else f"ERRORS: {len(r.errors)}"
        print(f"  {r.business_date}: {r.checks_count} checks, {r.items_count} items, {r.duration_sec:.1f}s — {status}")
    print("=" * 60)

    total_time = sum(r.duration_sec for r in results)
    total_errors = sum(len(r.errors) for r in results)
    print(f"Total: {len(results)} dates, {total_time:.1f}s, {total_errors} errors")

    if total_errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
