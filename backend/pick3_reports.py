"""
pick3_reports.py
----------------
All Pick 3 report logic as a self-contained FastAPI APIRouter.

Mount in main.py with:
    from pick3_reports import router as pick3_router
    app.include_router(pick3_router)

Endpoints:
    POST /api/reports/pick3                        - per-store per-day counts (Toast API)
    POST /api/reports/pick3/drill-down-orders      - single-store order-level pairing drill-down
    POST /api/reports/pick3/drill-down-all-stores  - all-stores parallel drill-down
    POST /api/reports/pick3/from-csv               - parse Toast Product Mix CSVs
    POST /api/reports/pick3/excel                  - generate Excel report from Selected_levels.csv
"""

import csv
import io
import logging
import re
import time
import uuid
import asyncio
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import pick_export_jobs

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Lazy imports from main.py — loaded on first call to avoid circular import
# at module load time (main imports this router; this router needs main's helpers).
# ---------------------------------------------------------------------------
def _main():
    """Return the main module, imported lazily to break circular dependency."""
    import main as _m
    return _m

async def _fetch_orders_for_restaurant_date_range(client, rguid, start, end):
    return await _main()._fetch_orders_for_restaurant_date_range(client, rguid, start, end)

def _get_modifier_guid_to_name(rguid):
    return _main()._get_modifier_guid_to_name(rguid)

def format_location_display_name(loc):
    return _main().format_location_display_name(loc)

async def get_all_active_locations(client):
    return await _main().get_all_active_locations(client)

async def get_menu_tags_mapping(client, rguid):
    return await _main().get_menu_tags_mapping(client, rguid)

async def request_toast_report(client, start, end, restaurant_ids, report_type, group_by):
    return await _main().request_toast_report(client, start, end, restaurant_ids, report_type, group_by)

async def fetch_toast_report(client, report_guid, report_type):
    return await _main().fetch_toast_report(client, report_guid, report_type)

def to_business_date(d):
    return _main().to_business_date(d)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PICK3_ENTREE_SUBSTRING = "(Pick 3 Entree)"
PICK3_OLO_ITEM_GUID = "e7adf8e6-5511-4e34-a45d-1a388996ad2f"
PICK3_PREMOD_PREFIXES = ("add ", "no ", "extra ")

# Matches both correct spelling "(OLO Pairing Choice)" AND known Toast typo "(OLO Pairing Chocie)"
_OLO_PAIRING_RE = re.compile(r"OLO Pairing Cho(?:ice|cie)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Report configs — parameterises this router for Pick 2 / Full Pairings too
# ---------------------------------------------------------------------------

REPORT_CONFIGS: Dict[str, Dict] = {
    "pick3": {
        "pos_parent":              "(Pick 3 Entree)",
        "olo_parent":              "Pick 3 (OLO)",
        "olo_item_guid":           "e7adf8e6-5511-4e34-a45d-1a388996ad2f",
        "max_pairings_per_entree": 3,
        "display_name":            "Pick 3",
    },
    "pick2": {
        "pos_parent":              "(Half Pairing Entree)",
        "olo_parent":              "Pick 2 (OLO)",
        "olo_item_guid":           None,
        "max_pairings_per_entree": 2,
        "display_name":            "Pick 2",
    },
    "full_pairings": {
        "pos_parent":              "(Full Pairing Entree)",
        "olo_parent":              "Full Value Pairings (OLO)",
        "olo_item_guid":           None,
        "max_pairings_per_entree": 2,
        "display_name":            "Full Pairings",
    },
}


def _get_config(report_type: str) -> Dict:
    """Return config dict for the given report_type, defaulting to pick3."""
    return REPORT_CONFIGS.get(report_type) or REPORT_CONFIGS["pick3"]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Pick3ReportRequest(BaseModel):
    start_date: str
    end_date: str
    restaurantIds: List[str] = []
    variant: str = "pos"
    report_type: str = "pick3"


class Pick3DrillDownOrdersRequest(BaseModel):
    start_date: str
    end_date: str
    restaurant_guid: str
    variant: str = "pos"
    debug: bool = False
    report_type: str = "pick3"


class Pick3DrillDownAllStoresRequest(BaseModel):
    start_date: str
    end_date: str
    restaurant_ids: Optional[List[str]] = None
    variant: str = "pos"
    report_type: str = "pick3"


class PickMultiDrillDownAllStoresRequest(BaseModel):
    start_date: str
    end_date: str
    restaurant_ids: Optional[List[str]] = None
    variant: str = "pos"
    # One or more of: "pick3", "pick2", "full_pairings". Fetches orders once
    # per store and aggregates each requested type from the same orders pass.
    report_types: List[str] = ["pick3", "pick2", "full_pairings"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_pick3_premod_name(mod_name: str) -> bool:
    """True if modifier looks like a premod we should exclude (ADD / NO / EXTRA)."""
    if not mod_name:
        return True
    upper = mod_name.strip().upper()
    return any(upper.startswith(p.upper()) for p in PICK3_PREMOD_PREFIXES)


def _is_olo_pairing_choice(mod_name: str) -> bool:
    """True if modifier is an OLO Pairing Choice — handles Toast typo 'Chocie'."""
    return bool(mod_name and _OLO_PAIRING_RE.search(mod_name))


def _normalize_modifier_name(mod_name: str) -> str:
    """Strip Toast-added prefixes from pairing choice modifier names so they
    merge into their canonical counterparts.  Prefixes are ONLY stripped when
    the modifier already carries a pairing suffix — regular premods like
    "ADD Ranch" are left unchanged and filtered out by is_valid_pairing.

    Examples:
        "XOTS Half Green Goddess (POS Pairing Choice)"        → "Half Green Goddess (POS Pairing Choice)"
        "ADD Cup Chicken Tortilla (POS Pairing Choice)"       → "Cup Chicken Tortilla (POS Pairing Choice)"
        "NO Cup Tomato Basil (POS Pairing Choice)"            → "Cup Tomato Basil (POS Pairing Choice)"
        "EXTRA Cup Crawfish Pepper Jack (POS Pairing Choice)" → "Cup Crawfish Pepper Jack (POS Pairing Choice)"
        "ADD Ranch"                                           → "ADD Ranch"  (no pairing suffix → unchanged)
    """
    if not mod_name:
        return ""
    is_pairing = "(POS Pairing Choice)" in mod_name or bool(_OLO_PAIRING_RE.search(mod_name))
    if is_pairing:
        for prefix in ("XOTS ", "ADD ", "NO ", "EXTRA "):
            if mod_name.startswith(prefix):
                return mod_name[len(prefix):]
    return mod_name


def _is_pick3_selection(sel: Dict, variant: str, cfg: Dict) -> bool:
    """True if this order selection counts as a pairing entree for the given variant."""
    display_name = (sel.get("displayName") or "").strip()
    if variant in ("olo", "all"):
        if cfg["olo_parent"] in display_name:
            return True
        item = sel.get("item")
        guid = (item.get("guid") if isinstance(item, dict) else item) if item else ""
        olo_guid = cfg.get("olo_item_guid")
        if olo_guid and guid and str(guid).strip().lower() == olo_guid.lower():
            return True
    if variant in ("pos", "all"):
        if cfg["pos_parent"] in display_name:
            return True
    return False


def _normalize_variant(v: Optional[str], choices=("pos", "olo")) -> str:
    v = (v or "pos").strip().lower()
    return v if v in choices else choices[0]


def _parse_qty(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(str(s).strip().replace(",", ""))
    except ValueError:
        return 0.0


def _detect_selected_levels_columns(header: List[str]) -> Tuple[Optional[int], ...]:
    """
    Return (col_type, col_item, col_modifier, col_qty) from a Selected_levels.csv header.
    Handles both Toast's standard export column names AND the older/shorter aliases.

    Toast Product Mix "Selected levels" actual headers (as of 2024-25):
        Type | masterId | parentId | itemGuid | Menu | Menu group | Subgroup |
        Item, open item | Size modifier | Modifiers, special requests |
        Sales Category | Item tags | Deferred | Qty sold | ...
    """
    col_type = col_item = col_modifier = col_qty = None
    for i, h in enumerate(header):
        if not h:
            continue
        hl = h.lower().strip()
        # Type column
        if col_type is None and hl in ("type", "level", "level type", "row type"):
            col_type = i
        # Item column — Toast uses "Item, open item"
        if col_item is None and hl in ("item", "item, open item", "product", "menu item", "item name"):
            col_item = i
        # Modifier column — Toast uses "Modifiers, special requests"
        if col_modifier is None and hl in (
            "modifier", "modifiers, special requests", "modifier name",
            "modifiers", "option", "special requests",
        ):
            col_modifier = i
        # Qty sold column — skip any "voided" variants
        if col_qty is None and "voided" not in hl:
            if hl in ("qty sold", "quantity sold", "quantity", "qty", "units", "sold", "unit sold") \
                    or ("qty" in hl and "sold" in hl) \
                    or "quantity" in hl:
                col_qty = i
    # Fallback fuzzy passes
    if col_item is None:
        col_item = next((i for i, h in enumerate(header) if h and "item" in h.lower() and "void" not in h.lower()), None)
    if col_modifier is None:
        col_modifier = next((i for i, h in enumerate(header) if h and ("modifier" in h.lower() or "special request" in h.lower())), None)
    if col_qty is None:
        col_qty = next((
            i for i, h in enumerate(header)
            if h and ("qty" in h.lower() or "quantity" in h.lower()) and "voided" not in h.lower()
        ), None)
    return col_type, col_item, col_modifier, col_qty


def _parse_selected_levels_csv(raw: bytes, variant: str, cfg: Optional[Dict] = None) -> Dict:
    """
    Parse a Toast Product Mix Selected_levels.csv and return
    {totalPick3Quantity, parentItems, selections, pairingSource}.
    variant: "pos" | "olo" | "all"
    """
    if cfg is None:
        cfg = REPORT_CONFIGS["pick3"]
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Selected_levels.csv must be UTF-8 encoded.")

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise HTTPException(status_code=400, detail="Selected_levels CSV is empty.")

    header = [c.strip() for c in rows[0]]
    col_type, col_item, col_modifier, col_qty = _detect_selected_levels_columns(header)

    missing = []
    if col_type is None:
        missing.append("Type")
    if col_item is None:
        missing.append("Item / Item, open item")
    if col_modifier is None:
        missing.append("Modifier / Modifiers, special requests")
    if col_qty is None:
        missing.append("Qty sold")
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Selected_levels.csv missing columns: {}. Found: {}.".format(
                ", ".join(missing), ", ".join(header[:20])
            ),
        )

    def is_pick3_parent(name: str) -> bool:
        if not name:
            return False
        if variant == "olo":
            return cfg["olo_parent"] in name
        if variant == "pos":
            return cfg["pos_parent"] in name
        return cfg["pos_parent"] in name or cfg["olo_parent"] in name

    def is_valid_pairing(mod: str) -> bool:
        if not mod or "(Side)" in mod or _is_pick3_premod_name(mod):
            return False
        if variant == "olo":
            return _is_olo_pairing_choice(mod)
        if variant == "pos":
            return "(POS Pairing Choice)" in mod
        return "(POS Pairing Choice)" in mod or _is_olo_pairing_choice(mod)

    parent_totals: Dict[str, float] = {}
    pairing_totals: Dict[str, float] = {}
    max_col = max(col_type, col_item, col_modifier, col_qty)

    for row in rows[1:]:
        if len(row) <= max_col:
            continue
        row_type = (row[col_type] or "").strip().lower()
        item_name = (row[col_item] or "").strip()
        mod_name = _normalize_modifier_name((row[col_modifier] or "").strip() if col_modifier < len(row) else "")
        qty = _parse_qty(row[col_qty] if col_qty < len(row) else "")
        if qty <= 0:
            continue
        if row_type == "modifier":
            if is_pick3_parent(item_name) and is_valid_pairing(mod_name):
                pairing_totals[mod_name] = pairing_totals.get(mod_name, 0.0) + qty
        elif row_type == "menuitem":
            # Only count actual menu item rows — excludes specialrequest/openitem/giftcard
            # rows that may also reference a Pick 3 parent name but aren't the sale itself.
            if is_pick3_parent(item_name):
                parent_totals[item_name] = parent_totals.get(item_name, 0.0) + qty

    total_pick3 = sum(parent_totals.values())
    # Safety cap: pairings can't exceed max_pairings_per_entree × total orders
    total_pairings = sum(pairing_totals.values())
    max_pairings = cfg["max_pairings_per_entree"] * total_pick3 if total_pick3 else 0
    if max_pairings > 0 and total_pairings > max_pairings:
        scale = max_pairings / total_pairings
        pairing_totals = {n: q * scale for n, q in pairing_totals.items()}

    return {
        "pairingSource": "toast_product_mix_selected_levels",
        "totalPick3Quantity": int(round(total_pick3)),
        "parentItems": [
            {"itemName": n, "quantity": int(round(q))}
            for n, q in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))
        ],
        "selections": [
            {"itemName": n, "quantity": int(round(q))}
            for n, q in sorted(pairing_totals.items(), key=lambda x: (-x[1], x[0]))
        ],
        "restaurantName": "All locations (from CSV)",
    }


def _aggregate_pick_from_orders(
    rguid: str,
    orders: List[Dict],
    start_bdate: int,
    end_bdate: int,
    variant: str,
    cfg: Dict,
    guid_to_name: Dict[str, str],
) -> Dict:
    """
    Aggregate pairing entree + pairing selection counts from a pre-fetched orders list.
    Pure compute — does NOT fetch orders, menu tags, or modifiers. Designed so callers
    can fetch orders once per store and call this multiple times with different cfgs
    (e.g. one orders pass → pick3 + pick2 + full_pairings outputs).
    """
    parent_totals: Dict[str, int] = {}
    pairing_totals: Dict[str, int] = {}
    combo_counts: Dict[Tuple[str, ...], int] = {}

    for order in orders:
        if order.get("voided") or order.get("deleted"):
            continue
        obd = order.get("businessDate")
        try:
            order_bdate_int = int(obd) if obd is not None else None
        except (TypeError, ValueError):
            order_bdate_int = None
        if order_bdate_int is None or not (start_bdate <= order_bdate_int <= end_bdate):
            continue
        for check in order.get("checks") or []:
            if check.get("voided") or check.get("deleted"):
                continue
            for sel in check.get("selections") or []:
                if sel.get("voided"):
                    continue
                if not _is_pick3_selection(sel, variant, cfg):
                    continue
                display_name = (sel.get("displayName") or "").strip()
                sel_qty = int(sel.get("quantity", 1) or 1)
                parent_totals[display_name] = parent_totals.get(display_name, 0) + sel_qty
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
                    mod_name = _normalize_modifier_name((guid_to_name.get(mod_guid) or "").strip())
                    if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                        continue
                    if variant == "all":
                        if "(POS Pairing Choice)" not in mod_name and not _is_olo_pairing_choice(mod_name):
                            continue
                    elif variant == "olo":
                        if not _is_olo_pairing_choice(mod_name):
                            continue
                    else:
                        if "(POS Pairing Choice)" not in mod_name:
                            continue
                    mod_qty = int(mod.get("quantity", 1) or 1)
                    sel_pairings.append((mod_name, mod_qty))
                max_p = sel_qty * cfg["max_pairings_per_entree"]
                total_p = sum(q for _, q in sel_pairings)
                if total_p > max_p > 0:
                    scale = max_p / total_p
                    for mn, mq in sel_pairings:
                        add = max(0, int(round(mq * scale)))
                        if add:
                            pairing_totals[mn] = pairing_totals.get(mn, 0) + add
                else:
                    for mn, mq in sel_pairings:
                        pairing_totals[mn] = pairing_totals.get(mn, 0) + mq

                if sel_pairings:
                    combo_names = sorted(mn for mn, _ in sel_pairings)
                    if len(combo_names) == cfg["max_pairings_per_entree"]:
                        combo_key = tuple(combo_names)
                        combo_counts[combo_key] = combo_counts.get(combo_key, 0) + sel_qty

    total_pick3 = sum(parent_totals.values()) or 0
    top_combos = [
        {"items": list(combo), "count": cnt}
        for combo, cnt in sorted(combo_counts.items(), key=lambda x: -x[1])[:3]
    ]
    return {
        "restaurantGuid": rguid,
        "totalPick3Quantity": total_pick3,
        "parentItems": [
            {"itemName": n, "quantity": q}
            for n, q in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))
        ],
        "selections": [
            {"itemName": n, "quantity": q}
            for n, q in sorted(pairing_totals.items(), key=lambda x: (-x[1], x[0]))
        ],
        "topCombos": top_combos,
    }


async def _compute_pick3_drill_down_for_store(
    client: httpx.AsyncClient,
    rguid: str,
    start_date: date,
    end_date: date,
    variant: str = "pos",
    cfg: Optional[Dict] = None,
) -> Dict:
    """Fetch orders for one store, aggregate pairing entree counts and pairing selections."""
    if cfg is None:
        cfg = REPORT_CONFIGS["pick3"]
    variant = _normalize_variant(variant, ("pos", "olo", "all"))
    start_bdate = int(to_business_date(start_date))
    end_bdate = int(to_business_date(end_date))
    await get_menu_tags_mapping(client, rguid)
    guid_to_name = _get_modifier_guid_to_name(rguid)
    _t_fetch_start = time.perf_counter()
    orders = await _fetch_orders_for_restaurant_date_range(client, rguid, start_date, end_date)
    _t_fetch = time.perf_counter() - _t_fetch_start

    _t_compute_start = time.perf_counter()
    result = _aggregate_pick_from_orders(rguid, orders, start_bdate, end_bdate, variant, cfg, guid_to_name)
    _t_compute = time.perf_counter() - _t_compute_start
    logger.info(
        "PROFILE store=%s orders=%d fetch=%.2fs compute=%.2fs total_qty=%d",
        rguid[:8], len(orders), _t_fetch, _t_compute, result["totalPick3Quantity"],
    )
    return result


def _build_pick3_excel(parent_items: List[Dict], selections: List[Dict], variant: str, cfg: Optional[Dict] = None) -> bytes:
    """
    Build a styled Excel workbook from parsed pairing report data.
    Returns the .xlsx file as bytes.
    variant: "pos" | "olo" | "all"
    """
    if cfg is None:
        cfg = REPORT_CONFIGS["pick3"]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl is required for Excel export. Run: pip install openpyxl")

    HEADER_FILL  = PatternFill("solid", start_color="1F3864", end_color="1F3864")
    SECTION_FILL = PatternFill("solid", start_color="2E75B6", end_color="2E75B6")
    ALT_FILL     = PatternFill("solid", start_color="D6E4F0", end_color="D6E4F0")
    WHITE_FILL   = PatternFill("solid", start_color="FFFFFF", end_color="FFFFFF")
    TOTAL_FILL   = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")
    COL_FILL     = PatternFill("solid", start_color="BDD7EE", end_color="BDD7EE")

    def _border():
        s = Side(style="thin", color="AAAAAA")
        return Border(left=s, right=s, top=s, bottom=s)

    def _cell(ws, r, c, val, bold=False, color="000000", fill=None, align="left", size=10):
        cell = ws.cell(row=r, column=c, value=val)
        cell.font = Font(name="Arial", bold=bold, color=color, size=size)
        cell.fill = fill or WHITE_FILL
        cell.alignment = Alignment(horizontal=align, vertical="center")
        cell.border = _border()

    def _build_sheet(ws, title, items_data, pairings_data, item_col_label, parent_col_label):
        ws.merge_cells("A1:C1")
        c = ws.cell(row=1, column=1, value=title)
        c.font = Font(name="Arial", bold=True, color="FFFFFF", size=14)
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")

        row = 3
        ws.merge_cells(f"A{row}:C{row}")
        c = ws.cell(row=row, column=1, value="ITEM QUANTITY COUNTS")
        c.font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
        c.fill = SECTION_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")

        row += 1
        _cell(ws, row, 1, item_col_label, bold=True, fill=COL_FILL)
        _cell(ws, row, 2, "Qty Sold",     bold=True, fill=COL_FILL)
        data_start = row + 1
        for i, item in enumerate(items_data):
            row += 1
            f = ALT_FILL if i % 2 == 0 else WHITE_FILL
            _cell(ws, row, 1, item["itemName"],  fill=f)
            _cell(ws, row, 2, item["quantity"],  fill=f, align="right")
        row += 1
        _cell(ws, row, 1, "TOTAL", bold=True, fill=TOTAL_FILL)
        tc = ws.cell(row=row, column=2, value=f"=SUM(B{data_start}:B{row-1})")
        tc.font = Font(name="Arial", bold=True)
        tc.fill = TOTAL_FILL
        tc.alignment = Alignment(horizontal="right", vertical="center")
        tc.border = _border()

        row += 2
        ws.merge_cells(f"A{row}:C{row}")
        c = ws.cell(row=row, column=1, value="PAIRING CHOICES BREAKDOWN")
        c.font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
        c.fill = SECTION_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")

        row += 1
        _cell(ws, row, 1, parent_col_label,   bold=True, fill=COL_FILL)
        _cell(ws, row, 2, "Pairing Choice",   bold=True, fill=COL_FILL)
        _cell(ws, row, 3, "Qty Sold",         bold=True, fill=COL_FILL)
        for i, sel in enumerate(pairings_data):
            row += 1
            f = ALT_FILL if i % 2 == 0 else WHITE_FILL
            _cell(ws, row, 1, sel.get("parentItem", ""), fill=f)
            _cell(ws, row, 2, sel["itemName"],           fill=f)
            _cell(ws, row, 3, sel["quantity"],           fill=f, align="right")

        ws.column_dimensions["A"].width = 52
        ws.column_dimensions["B"].width = 52
        ws.column_dimensions["C"].width = 14
        ws.row_dimensions[1].height = 30

    # ----------------------------------------------------------------
    # Split selections into POS vs OLO if variant == "all"
    # ----------------------------------------------------------------
    pos_selections = [s for s in selections if "(POS Pairing Choice)" in s["itemName"]]
    olo_selections = [s for s in selections if _is_olo_pairing_choice(s["itemName"])]
    pos_parents    = [p for p in parent_items if cfg["pos_parent"] in p["itemName"]]
    olo_parents    = [p for p in parent_items if cfg["olo_parent"] in p["itemName"]]

    # Add a parentItem label to each pairing row (blank — we show all combined)
    for s in selections:
        s.setdefault("parentItem", "")

    wb = Workbook()

    if variant in ("pos", "all"):
        ws = wb.active if variant == "pos" else wb.active
        ws.title = "POS Breakdown"
        items = pos_parents if pos_parents else parent_items
        pairs = pos_selections if pos_selections else [s for s in selections if "(POS Pairing Choice)" in s["itemName"]]
        _build_sheet(ws, f"In-Store POS Breakdown — {cfg['display_name']}", items, pairs,
                     "Item", f"{cfg['display_name']} Entree Item")

    if variant in ("olo", "all"):
        ws2 = wb.create_sheet("OLO Breakdown") if variant == "all" else wb.active
        if variant == "olo":
            ws2.title = "OLO Breakdown"
        items = olo_parents if olo_parents else parent_items
        pairs = olo_selections if olo_selections else [s for s in selections if _is_olo_pairing_choice(s["itemName"])]
        _build_sheet(ws2, f"OLO Breakdown — {cfg['display_name']}", items, pairs,
                     "Item", f"{cfg['display_name']} OLO Item")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/reports/pick3")
async def run_pick3_report(request: Pick3ReportRequest = Body(...)):
    """Per-store per-day pairing entree counts from the Toast menu report API."""
    cfg = _get_config(request.report_type)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            restaurant_ids = list(request.restaurantIds) if request.restaurantIds else []
            locations = await get_all_active_locations(client)
            if not restaurant_ids:
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            if not restaurant_ids:
                return {"summary": [], "drillDown": []}
            guid_to_location = {loc.restaurantGuid: loc for loc in locations}

            try:
                start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
                end_date   = datetime.strptime(request.end_date,   "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
            if start_date > end_date:
                raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

            # Accept "all" so callers can pull POS + OLO totals in a single
            # request — same Toast API cost as either variant alone, just a
            # looser filter. Defaults to "pos" for backward compatibility.
            variant = _normalize_variant(request.variant, ("pos", "olo", "all"))
            days_diff = (end_date - start_date).days + 1

            async def _fetch_menu_chunked(group_by: List[str]) -> List[Dict]:
                rows: List[Dict] = []
                if days_diff > 7:
                    cur = start_date
                    while cur <= end_date:
                        chunk_end = min(cur + timedelta(days=6), end_date)
                        rg = await request_toast_report(client, cur, chunk_end, restaurant_ids, "menu", group_by)
                        chunk = await fetch_toast_report(client, rg, "menu")
                        if chunk:
                            rows.extend(chunk)
                        cur = chunk_end + timedelta(days=1)
                else:
                    rg = await request_toast_report(client, start_date, end_date, restaurant_ids, "menu", group_by)
                    rows = await fetch_toast_report(client, rg, "menu") or []
                return rows

            # --- Summary (MENU_ITEM) ---
            all_menu_rows = await _fetch_menu_chunked(["MENU_ITEM"])
            summary_agg: Dict[Tuple, Dict] = {}
            parent_items_agg: Dict[Tuple, Dict[str, int]] = {}
            # When variant=all we ALSO track POS-only and OLO-only chain-wide
            # totals so the caller can render the two sides separately without
            # making a second round of Toast API calls.
            parent_pos_total: Dict[str, int] = {}
            parent_olo_total: Dict[str, int] = {}
            olo_guid_lower = (cfg["olo_item_guid"] or "").lower()
            for row in all_menu_rows:
                name  = (row.get("menuItemName") or "").strip()
                mguid = (row.get("menuItemGuid") or "").strip().lower()
                is_pos = cfg["pos_parent"] in name
                is_olo = cfg["olo_parent"] in name or (olo_guid_lower and mguid == olo_guid_lower)
                if variant == "all":
                    is_p3 = is_pos or is_olo
                elif variant == "olo":
                    is_p3 = is_olo
                else:
                    is_p3 = is_pos
                if not is_p3:
                    continue
                rguid = row.get("restaurantGuid", "")
                bdate = row.get("businessDate", "")
                qty   = int(row.get("quantitySold", 0) or 0)
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
                pi = parent_items_agg.setdefault(key, {})
                pi[name] = pi.get(name, 0) + qty
                if variant == "all":
                    if is_pos:
                        parent_pos_total[name] = parent_pos_total.get(name, 0) + qty
                    if is_olo:
                        parent_olo_total[name] = parent_olo_total.get(name, 0) + qty

            for r in summary_agg.values():
                if r["restaurantGuid"] in guid_to_location:
                    r["restaurantName"] = format_location_display_name(guid_to_location[r["restaurantGuid"]])
            summary = sorted(summary_agg.values(), key=lambda x: (x["businessDate"], x["restaurantName"]))
            summary_by_key = {(r["restaurantGuid"], r["businessDate"]): r for r in summary}

            # --- Pairing selections (MODIFIER — includes parent menu item info) ---
            all_item_mod_rows = await _fetch_menu_chunked(["MODIFIER"])
            selections_agg: Dict[Tuple, Dict[str, int]] = {}
            # Per-variant chain-wide selection totals (variant=all only).
            sel_pos_total: Dict[str, int] = {}
            sel_olo_total: Dict[str, int] = {}
            for row in all_item_mod_rows:
                parent_name = (row.get("menuItemName") or "").strip()
                parent_guid = (row.get("menuItemGuid") or "").strip().lower()
                p_is_pos = cfg["pos_parent"] in parent_name
                p_is_olo = cfg["olo_parent"] in parent_name or (olo_guid_lower and parent_guid == olo_guid_lower)
                if variant == "all":
                    is_p3 = p_is_pos or p_is_olo
                elif variant == "olo":
                    is_p3 = p_is_olo
                else:
                    is_p3 = p_is_pos
                if not is_p3:
                    continue
                mod_name = _normalize_modifier_name((row.get("modifierName") or row.get("modifierOptionName") or "").strip())
                if not mod_name or "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                    continue
                m_is_olo = _is_olo_pairing_choice(mod_name)
                m_is_pos = "(POS Pairing Choice)" in mod_name
                if variant == "all":
                    if not (m_is_pos or m_is_olo):
                        continue
                elif variant == "olo":
                    if not m_is_olo:
                        continue
                else:
                    if not m_is_pos:
                        continue
                rguid = row.get("restaurantGuid", "")
                bdate = row.get("businessDate", "")
                qty   = int(row.get("quantitySold", 0) or 0)
                if not rguid or not bdate:
                    continue
                key = (rguid, bdate)
                sa = selections_agg.setdefault(key, {})
                sa[mod_name] = sa.get(mod_name, 0) + qty
                if variant == "all":
                    if m_is_pos:
                        sel_pos_total[mod_name] = sel_pos_total.get(mod_name, 0) + qty
                    if m_is_olo:
                        sel_olo_total[mod_name] = sel_olo_total.get(mod_name, 0) + qty

            # --- Build drillDown ---
            all_keys = set(summary_by_key.keys()) | set(selections_agg.keys())
            drill_down = []
            for (rguid, bdate) in sorted(all_keys):
                rec    = summary_by_key.get((rguid, bdate))
                p3_tot = rec["pick3Total"] if rec else 0
                rname  = rec["restaurantName"] if rec else "Unknown"
                p_map  = parent_items_agg.get((rguid, bdate), {})
                s_map  = selections_agg.get((rguid, bdate), {})
                drill_down.append({
                    "restaurantGuid": rguid,
                    "restaurantName": format_location_display_name(guid_to_location[rguid]) if rguid in guid_to_location else rname,
                    "businessDate":   bdate,
                    "pick3Total":     p3_tot,
                    "parentItems":    sorted([{"itemName": n, "quantity": q} for n, q in p_map.items()], key=lambda x: (-x["quantity"], x["itemName"])),
                    "selections":     sorted([{"itemName": n, "quantity": q} for n, q in s_map.items()], key=lambda x: (-x["quantity"], x["itemName"])),
                })
            response = {
                "summary":       summary,
                "drillDown":     drill_down,
                "pairingSource": "menu_aggregate",
            }
            # variant=all: also expose chain-wide totals split by side so the
            # caller can render POS and OLO sub-sections without re-fetching.
            if variant == "all":
                def _to_sorted(d: Dict[str, int]) -> List[Dict]:
                    return sorted(
                        [{"itemName": n, "quantity": q} for n, q in d.items()],
                        key=lambda x: (-x["quantity"], x["itemName"]),
                    )
                response["variantBreakdown"] = {
                    "pos": {
                        "parentItems": _to_sorted(parent_pos_total),
                        "selections":  _to_sorted(sel_pos_total),
                        "total":       sum(parent_pos_total.values()),
                    },
                    "olo": {
                        "parentItems": _to_sorted(parent_olo_total),
                        "selections":  _to_sorted(sel_olo_total),
                        "total":       sum(parent_olo_total.values()),
                    },
                }
            return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Pick 3 report: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating Pick 3 report: {e}")


@router.post("/api/reports/pick3/drill-down-orders")
async def pick3_drill_down_orders(request: Pick3DrillDownOrdersRequest = Body(...)):
    """Order-level pairing drill-down for a single store."""
    cfg = _get_config(request.report_type)
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date   = datetime.strptime(request.end_date,   "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    rguid = request.restaurant_guid.strip()
    if not rguid:
        raise HTTPException(status_code=400, detail="restaurant_guid required")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            variant = _normalize_variant(request.variant, ("pos", "olo", "all"))
            result  = await _compute_pick3_drill_down_for_store(client, rguid, start_date, end_date, variant, cfg)
            locs    = await get_all_active_locations(client)
            rname   = next((format_location_display_name(l) for l in locs if l.restaurantGuid == rguid), "Unknown")
            out: Dict = {
                "pairingSource": "orders",
                "restaurantGuid": rguid,
                "restaurantName": rname,
                "totalPick3Quantity": result["totalPick3Quantity"],
                "parentItems": result["parentItems"],
                "selections":  result["selections"],
            }
            if request.debug:
                orders = await _fetch_orders_for_restaurant_date_range(client, rguid, start_date, end_date)
                start_bdate = int(to_business_date(start_date))
                end_bdate   = int(to_business_date(end_date))
                debug_orders = []
                p3_by_date: Dict[str, int] = {}
                for order in orders:
                    obd = order.get("businessDate")
                    try:
                        obd_int = int(obd) if obd is not None else None
                    except (TypeError, ValueError):
                        obd_int = None
                    if obd_int is None or not (start_bdate <= obd_int <= end_bdate):
                        continue
                    obd_str = str(obd_int)
                    p3 = sum(
                        int(sel.get("quantity", 1) or 1)
                        for check in (order.get("checks") or [])
                        for sel in (check.get("selections") or [])
                        if _is_pick3_selection(sel, variant, cfg)
                    )
                    if p3 > 0:
                        debug_orders.append({"orderGuid": (order.get("guid") or "")[:8], "businessDate": obd_str, "pick3Count": p3})
                        p3_by_date[obd_str] = p3_by_date.get(obd_str, 0) + p3
                out["debug"] = {
                    "ordersFetched": len(orders),
                    "ordersWithPick3": len(debug_orders),
                    "totalPick3FromOrders": result["totalPick3Quantity"],
                    "pick3ByBusinessDate": dict(sorted(p3_by_date.items())),
                    "sampleOrdersWithPick3": debug_orders[:20],
                }
            return out
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Pick 3 drill-down-orders: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/reports/pick3/drill-down-all-stores")
async def pick3_drill_down_all_stores(request: Pick3DrillDownAllStoresRequest = Body(...)):
    """Parallel order-level pairing drill-down for all stores."""
    cfg = _get_config(request.report_type)
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date   = datetime.strptime(request.end_date,   "%Y-%m-%d").date()
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
                guids = {g.strip() for g in request.restaurant_ids if g.strip()}
                locs  = [l for l in locs if l.restaurantGuid in guids]
            if not locs:
                return {"stores": [], "pairingSource": "orders"}

            variant = _normalize_variant(request.variant, ("pos", "olo", "all"))
            guid_to_name = {l.restaurantGuid: format_location_display_name(l) for l in locs}

            # Prefetch menu tags in parallel
            pre_sem = asyncio.Semaphore(10)
            async def prefetch(g: str):
                async with pre_sem:
                    await get_menu_tags_mapping(client, g)
            await asyncio.gather(*[prefetch(g) for g in guid_to_name], return_exceptions=True)

            sem = asyncio.Semaphore(8)
            async def one_store(rguid: str):
                async with sem:
                    _t_store_start = time.perf_counter()
                    try:
                        r = await _compute_pick3_drill_down_for_store(client, rguid, start_date, end_date, variant, cfg)
                        r["_profileSeconds"] = time.perf_counter() - _t_store_start
                        return r
                    except Exception as e:
                        logger.warning("Pick3 all-stores: %s: %s", rguid[:8], e)
                        return {"restaurantGuid": rguid, "totalPick3Quantity": 0, "parentItems": [], "selections": [], "topCombos": [], "_profileSeconds": time.perf_counter() - _t_store_start}

            _t_gather_start = time.perf_counter()
            results = await asyncio.gather(*[one_store(g) for g in guid_to_name])
            _t_gather = time.perf_counter() - _t_gather_start
            _t_store_sum = sum(r.get("_profileSeconds", 0.0) for r in results)
            logger.info(
                "PROFILE all-stores wall=%.2fs sum_per_store=%.2fs stores=%d range=%s..%s variant=%s report=%s parallelism=%.1fx",
                _t_gather, _t_store_sum, len(guid_to_name), request.start_date, request.end_date,
                variant, request.report_type, (_t_store_sum / _t_gather) if _t_gather > 0 else 0.0,
            )
            stores = [
                {
                    "restaurantGuid":    r["restaurantGuid"],
                    "restaurantName":    guid_to_name.get(r["restaurantGuid"], "Unknown"),
                    "totalPick3Quantity": r["totalPick3Quantity"],
                    "parentItems":       r["parentItems"],
                    "selections":        r["selections"],
                    "topCombos":         r.get("topCombos", []),
                }
                for r in results
            ]
            logger.info("Pick3 all-stores: %s–%s stores=%s total=%s",
                        request.start_date, request.end_date, len(stores),
                        sum(s["totalPick3Quantity"] for s in stores))
            return {"stores": stores, "pairingSource": "orders"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Pick3 drill-down-all-stores: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _validate_multi_request(request: "PickMultiDrillDownAllStoresRequest") -> Tuple[date, date, List[str], str]:
    """Validate and normalize the request. Raises HTTPException on error."""
    try:
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end_date   = datetime.strptime(request.end_date,   "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

    requested_types = [t for t in (request.report_types or []) if t in REPORT_CONFIGS]
    if not requested_types:
        raise HTTPException(
            status_code=400,
            detail=f"report_types must contain at least one of: {list(REPORT_CONFIGS.keys())}",
        )
    variant = _normalize_variant(request.variant, ("pos", "olo", "all"))
    return start_date, end_date, requested_types, variant


async def _run_picks_multi_core(
    start_date: date,
    end_date: date,
    restaurant_ids: Optional[List[str]],
    variant: str,
    requested_types: List[str],
    progress_cb: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> Dict:
    """
    Core multi-pick computation. Fetches orders once per store and aggregates
    each requested report_type from the same orders pass.

    progress_cb is an optional async callable invoked as `await progress_cb(done, total)`
    once after the store list is known (done=0) and once after each store completes.
    Used by the async-job endpoint to update SQLite-backed progress.
    """
    start_bdate = int(to_business_date(start_date))
    end_bdate   = int(to_business_date(end_date))

    async with httpx.AsyncClient(timeout=120.0) as client:
        locs = await get_all_active_locations(client)
        if not locs:
            return {"pairingSource": "orders", "results": {t: {"stores": []} for t in requested_types}}
        if restaurant_ids:
            wanted = {g.strip() for g in restaurant_ids if g.strip()}
            locs   = [l for l in locs if l.restaurantGuid in wanted]
        if not locs:
            return {"pairingSource": "orders", "results": {t: {"stores": []} for t in requested_types}}

        store_name = {l.restaurantGuid: format_location_display_name(l) for l in locs}
        total_stores = len(store_name)
        if progress_cb is not None:
            try:
                await progress_cb(0, total_stores)
            except Exception:
                pass

        # Prefetch menu tags in parallel — same pattern as single-type endpoint
        pre_sem = asyncio.Semaphore(10)
        async def prefetch(g: str):
            async with pre_sem:
                await get_menu_tags_mapping(client, g)
        await asyncio.gather(*[prefetch(g) for g in store_name], return_exceptions=True)

        done_count = {"n": 0}
        progress_lock = asyncio.Lock()
        sem = asyncio.Semaphore(8)

        async def one_store(rguid: str):
            """Fetch orders once, run aggregator for each requested report_type."""
            async with sem:
                _t_store_start = time.perf_counter()
                per_type: Dict[str, Dict] = {}
                try:
                    await get_menu_tags_mapping(client, rguid)
                    mod_guid_to_name = _get_modifier_guid_to_name(rguid)
                    _t_fetch_start = time.perf_counter()
                    orders = await _fetch_orders_for_restaurant_date_range(client, rguid, start_date, end_date)
                    _t_fetch = time.perf_counter() - _t_fetch_start

                    _t_compute_start = time.perf_counter()
                    for rt in requested_types:
                        cfg = _get_config(rt)
                        per_type[rt] = _aggregate_pick_from_orders(
                            rguid, orders, start_bdate, end_bdate, variant, cfg, mod_guid_to_name
                        )
                    _t_compute = time.perf_counter() - _t_compute_start
                    logger.info(
                        "PROFILE multi store=%s orders=%d fetch=%.2fs compute=%.2fs types=%s",
                        rguid[:8], len(orders), _t_fetch, _t_compute, ",".join(requested_types),
                    )
                except Exception as e:
                    logger.warning("Picks multi all-stores: %s: %s", rguid[:8], e)
                    for rt in requested_types:
                        per_type[rt] = {
                            "restaurantGuid": rguid, "totalPick3Quantity": 0,
                            "parentItems": [], "selections": [], "topCombos": [],
                        }
                if progress_cb is not None:
                    async with progress_lock:
                        done_count["n"] += 1
                        n = done_count["n"]
                    try:
                        await progress_cb(n, total_stores)
                    except Exception:
                        pass
                return rguid, per_type, time.perf_counter() - _t_store_start

        _t_gather_start = time.perf_counter()
        results = await asyncio.gather(*[one_store(g) for g in store_name])
        _t_gather = time.perf_counter() - _t_gather_start
        _t_store_sum = sum(secs for _, _, secs in results)
        logger.info(
            "PROFILE multi all-stores wall=%.2fs sum_per_store=%.2fs stores=%d range=%s..%s variant=%s types=%s parallelism=%.1fx",
            _t_gather, _t_store_sum, total_stores, start_date.isoformat(), end_date.isoformat(),
            variant, ",".join(requested_types),
            (_t_store_sum / _t_gather) if _t_gather > 0 else 0.0,
        )

        out: Dict[str, Dict] = {t: {"stores": []} for t in requested_types}
        for rguid, per_type, _secs in results:
            rname = store_name.get(rguid, "Unknown")
            for rt in requested_types:
                r = per_type.get(rt) or {}
                out[rt]["stores"].append({
                    "restaurantGuid":     rguid,
                    "restaurantName":     rname,
                    "totalPick3Quantity": r.get("totalPick3Quantity", 0),
                    "parentItems":        r.get("parentItems", []),
                    "selections":         r.get("selections", []),
                    "topCombos":          r.get("topCombos", []),
                })

        return {"pairingSource": "orders", "results": out}


@router.post("/api/reports/picks/drill-down-all-stores-multi")
async def picks_drill_down_all_stores_multi(request: PickMultiDrillDownAllStoresRequest = Body(...)):
    """
    SYNC multi-report drill-down (blocking HTTP). Use the /start + /status
    pair instead for large requests that may exceed nginx's proxy_read_timeout.

    Response shape:
      {
        "pairingSource": "orders",
        "results": {
          "pick3":         { "stores": [ ... ] },
          "pick2":         { "stores": [ ... ] },
          "full_pairings": { "stores": [ ... ] }
        }
      }
    """
    start_date, end_date, requested_types, variant = _validate_multi_request(request)
    try:
        return await _run_picks_multi_core(
            start_date, end_date, request.restaurant_ids, variant, requested_types, progress_cb=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Picks multi drill-down-all-stores: %s: %s", type(e).__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Async job endpoints — start + poll. Use these from the Menu Performance
# export flow so the HTTP request doesn't have to stay open for ~10+ minutes
# (which Cloudways' default nginx proxy_read_timeout of 60s would kill).
# ---------------------------------------------------------------------------

async def _run_picks_multi_job(job_id: str, request: PickMultiDrillDownAllStoresRequest) -> None:
    """Background task body. Updates pick_export_jobs SQLite as it progresses."""
    try:
        start_date, end_date, requested_types, variant = _validate_multi_request(request)
    except HTTPException as he:
        pick_export_jobs.finish_error(job_id, f"invalid request: {he.detail}")
        return
    except Exception as e:
        pick_export_jobs.finish_error(job_id, f"invalid request: {e}")
        return

    async def progress_cb(done: int, total: int) -> None:
        # SQLite writes are sync — offload so we don't block the event loop.
        await asyncio.to_thread(pick_export_jobs.update_progress, job_id, done, total)

    try:
        result = await _run_picks_multi_core(
            start_date, end_date, request.restaurant_ids, variant, requested_types, progress_cb=progress_cb,
        )
        await asyncio.to_thread(pick_export_jobs.finish_done, job_id, result)
    except Exception as e:
        logger.error("Picks multi job %s failed: %s", job_id, e, exc_info=True)
        try:
            await asyncio.to_thread(pick_export_jobs.finish_error, job_id, str(e))
        except Exception:
            pass


@router.post("/api/reports/picks/drill-down-all-stores-multi/start")
async def picks_drill_down_all_stores_multi_start(request: PickMultiDrillDownAllStoresRequest = Body(...)):
    """Kick off a multi-pick export as a background task. Returns {job_id}."""
    # Validate up front so callers get a sync 400 instead of a useless job_id.
    _validate_multi_request(request)
    job_id = uuid.uuid4().hex
    await asyncio.to_thread(pick_export_jobs.create, job_id)
    # Fire-and-forget on the current worker's event loop. With --workers 2, the
    # status poll may land on the other worker — that's fine because state lives
    # in SQLite, not in this process's memory.
    asyncio.create_task(_run_picks_multi_job(job_id, request))
    logger.info("Picks multi job %s started range=%s..%s types=%s stores=%d",
                job_id, request.start_date, request.end_date,
                ",".join(request.report_types or []),
                len(request.restaurant_ids or []))
    return {"job_id": job_id, "status": "running"}


@router.get("/api/reports/picks/drill-down-all-stores-multi/status/{job_id}")
async def picks_drill_down_all_stores_multi_status(job_id: str):
    """Poll a multi-pick export job. Returns full result when status=='done'."""
    # Best-effort cleanup of jobs older than 1 hour every time we poll.
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


@router.post("/api/reports/pick3/from-csv")
async def pick3_breakdown_from_csv(
    file: Optional[UploadFile] = File(None,  description="Toast Product Mix Items.csv"),
    modifiers_file: Optional[UploadFile] = File(None, description="Toast Product Mix Modifiers.csv"),
    selected_levels_file: Optional[UploadFile] = File(None, description="Toast Product Mix Selected_levels.csv"),
    variant: str = Query("pos", description="pos | olo | all"),
    report_type: str = Query("pick3", description="pick3 | pick2 | full_pairings"),
):
    """
    Parse Toast Product Mix CSV exports.
    Selected_levels.csv (recommended) gives accurate parent→pairing counts.
    Falls back to Items.csv + optional Modifiers.csv.
    """
    cfg = _get_config(report_type)
    variant = (variant or "pos").strip().lower()
    if variant not in ("pos", "olo", "all"):
        variant = "pos"

    has_sl    = selected_levels_file and selected_levels_file.filename and selected_levels_file.filename.lower().endswith(".csv")
    has_items = file and file.filename and file.filename.lower().endswith(".csv")

    if not has_sl and not has_items:
        raise HTTPException(status_code=400, detail="Upload at least one file: Selected_levels.csv (recommended) or Items.csv.")

    # --- Selected_levels path (preferred) ---
    if has_sl:
        raw = await selected_levels_file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Selected_levels file too large (max 20 MB).")
        return _parse_selected_levels_csv(raw, variant, cfg)

    # --- Items.csv (+ optional Modifiers.csv) path ---
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB).")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded.")

    rows   = list(csv.reader(io.StringIO(text)))
    header = [c.strip() for c in rows[0]] if rows else []
    if not rows:
        raise HTTPException(status_code=400, detail="Items CSV is empty.")

    col_master = col_parent = col_item = col_qty = None
    for i, h in enumerate(header):
        hl = h.lower() if h else ""
        if hl == "masterid":  col_master = i
        if hl == "parentid":  col_parent = i
        if hl == "item":      col_item   = i
        if col_qty is None and hl == "qty sold":  col_qty = i
        elif col_qty is None and "qty" in hl and "sold" in hl and "voided" not in hl:
            col_qty = i
    if col_item is None or col_qty is None:
        raise HTTPException(status_code=400, detail="Items.csv must have 'Item' and 'Qty sold' columns.")

    def is_pick3_item(name: str) -> bool:
        if not name:
            return False
        if variant == "pos":  return cfg["pos_parent"] in name
        if variant == "olo":  return cfg["olo_parent"] in name
        return cfg["pos_parent"] in name or cfg["olo_parent"] in name

    parent_totals: Dict[str, float] = {}
    pick3_master_ids: set = set()
    for row in rows[1:]:
        if len(row) <= max(col_item, col_qty):
            continue
        item_name = (row[col_item] or "").strip()
        master_id = (row[col_master] or "").strip() if col_master is not None and len(row) > col_master else ""
        if not is_pick3_item(item_name):
            continue
        qty = _parse_qty(row[col_qty])
        if qty <= 0:
            continue
        parent_totals[item_name] = parent_totals.get(item_name, 0.0) + qty
        if master_id:
            pick3_master_ids.add(master_id)

    selections: List[Dict] = []
    if modifiers_file and modifiers_file.filename and modifiers_file.filename.lower().endswith(".csv"):
        try:
            mod_raw  = await modifiers_file.read()
            mod_text = mod_raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            mod_rows = list(csv.reader(io.StringIO(mod_text)))
            if mod_rows:
                mh = [c.strip() for c in mod_rows[0]]
                mc_parent = mc_mod = mc_qty = None
                for i, c in enumerate(mh):
                    cl = c.lower() if c else ""
                    if cl == "parentid":   mc_parent = i
                    if cl == "modifier":   mc_mod    = i
                    if mc_qty is None and cl == "qty sold": mc_qty = i
                    elif mc_qty is None and "qty" in cl and "sold" in cl and "voided" not in cl:
                        mc_qty = i
                if mc_parent is not None and mc_mod is not None and mc_qty is not None:
                    pt: Dict[str, float] = {}
                    for mrow in mod_rows[1:]:
                        if len(mrow) <= max(mc_parent, mc_mod, mc_qty):
                            continue
                        pid      = (mrow[mc_parent] or "").strip()
                        mod_name = _normalize_modifier_name((mrow[mc_mod] or "").strip())
                        if pid not in pick3_master_ids or not mod_name:
                            continue
                        if "(Side)" in mod_name or _is_pick3_premod_name(mod_name):
                            continue
                        if variant == "olo":
                            if not _is_olo_pairing_choice(mod_name): continue
                        elif variant == "pos":
                            if "(POS Pairing Choice)" not in mod_name: continue
                        else:
                            if "(POS Pairing Choice)" not in mod_name and not _is_olo_pairing_choice(mod_name): continue
                        qty = _parse_qty(mrow[mc_qty])
                        if qty > 0:
                            pt[mod_name] = pt.get(mod_name, 0.0) + qty
                    total_p3  = sum(parent_totals.values())
                    total_raw = sum(pt.values())
                    max_p     = cfg["max_pairings_per_entree"] * total_p3 if total_p3 else 0
                    if max_p > 0 and total_raw > max_p:
                        scale = max_p / total_raw
                        pt = {n: q * scale for n, q in pt.items()}
                    selections = [
                        {"itemName": n, "quantity": int(round(q))}
                        for n, q in sorted(pt.items(), key=lambda x: (-x[1], x[0]))
                    ]
        except Exception:
            pass

    total_pick3 = sum(parent_totals.values())
    return {
        "pairingSource": "toast_product_mix_csv",
        "totalPick3Quantity": int(round(total_pick3)),
        "parentItems": [
            {"itemName": n, "quantity": int(round(q))}
            for n, q in sorted(parent_totals.items(), key=lambda x: (-x[1], x[0]))
        ],
        "selections": selections,
        "restaurantName": "All locations (from CSV)",
    }


@router.post("/api/reports/pick3/excel")
async def pick3_excel_from_selected_levels(
    file: UploadFile = File(..., description="Toast Product Mix Selected_levels.csv"),
    variant: str = Query("all", description="pos | olo | all"),
    report_type: str = Query("pick3", description="pick3 | pick2 | full_pairings"),
):
    """
    Generate a styled two-tab Excel report (POS + OLO) from Selected_levels.csv.
    Returns a downloadable .xlsx file.
    """
    cfg = _get_config(report_type)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload Selected_levels.csv.")
    variant = (variant or "all").strip().lower()
    if variant not in ("pos", "olo", "all"):
        variant = "all"

    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB).")

    # Parse with variant="all" so we get both POS and OLO in one pass
    data = _parse_selected_levels_csv(raw, variant, cfg)

    xlsx_bytes = _build_pick3_excel(data["parentItems"], data["selections"], variant, cfg)
    safe_name  = cfg["display_name"].replace(" ", "")
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Report.xlsx"'},
    )