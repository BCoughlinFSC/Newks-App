"""
ipa_export.py — IPA payroll-vendor sales export.

Generates rows matching the "IPA Sample Sales File" template at
(date x store x dining_option) grain. SHIFTPART is left blank — Toast ERA
does not support DAYPART as a groupBy key, so we cannot break sales out by
daypart through that API. Revisit once per-check daypart resolution is wired
up against the Standard API (`/config/v2/restaurantServices`).

Row composition per (date D, store S): one row per unique IPA
(SEGMENT_NAME, ORDER_CHANNEL) bucket, with TY and LY both filled when
the data is available.

  TY:  always Toast ERA on D, grouped by DINING_OPTION and collapsed
       onto IPA segments via _IPA_SEGMENT_MAP.
  LY:  Toast ERA on ly_date(D) if switchover_date <= ly_date(D);
       otherwise Aloha channel-column data (aloha_sales_data) at
       ly_date(D), mapped to IPA segments via _ALOHA_TO_IPA_SEGMENT.

For Aloha-LY rows, TransLY is filled honestly: zero on non-catering
per-channel rows (Aloha never tracked per-channel check counts), the
real catering check count on the Catering - Pickup row, and the residual
non-catering check count on one trailing 'Unallocated Checks (Non-Catering)'
row. NetSalesLY sums exactly to Aloha's daily net_sales (excluding GNG, an
item-level category that sits inside the channel cols).

Aloha's `delivery_net_sales` is a single bucket — Door Dash / Uber Eats /
Grub Hub / Dispatch / Rails are not separable on the LY side. Emitted as
SEGMENT_NAME="Delivery", ORDER_CHANNEL="3PD". `gng_net_sales` is intentionally
skipped to avoid double-counting.

Rows with TY=0 AND LY=0 are dropped.
"""

import asyncio
import csv
import io
import logging
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional

import httpx

import aloha_sales_db
import toast_config_db

logger = logging.getLogger(__name__)


IPA_COLUMNS = [
    "DAYDATE", "Location", "WeekEndingDate", "FiscalYear",
    "FISCALWEEKKEY", "FISCALPERIODKEY", "SEGMENT_NAME", "ORDER_CHANNEL",
    "SHIFTPART", "COMP_FLAG", "NetSalesTY", "TransTY", "NetSalesLY", "TransLY",
]


# Toast dining-option name -> (IPA SEGMENT_NAME, IPA ORDER_CHANNEL).
# Multiple Toast names intentionally collapse onto the same IPA segment
# (e.g. kiosk / OLO variants of "Dine In" all become "Eat In") — TY/LY net
# sales and check counts get summed across the collapse.
#
# Unknown Toast names fall through: SEGMENT_NAME is passed raw, ORDER_CHANNEL
# is left blank. Watch for blank channel rows in the CSV — they flag new
# dining options that need to be added here.
_IPA_SEGMENT_MAP: Dict[str, tuple] = {
    # Retail
    "Dine In":              ("Eat In",            "Retail"),
    "X-Dine-In (K)":        ("Eat In",            "Retail"),
    "X-Dine-In-OLO":        ("Eat In",            "Retail"),
    "ToGo":                 ("Carry Out",         "Retail"),
    "X-To Go (K)":          ("Carry Out",         "Retail"),
    "Call-In":              ("Phone In Tender",   "Retail"),
    "X-Call In-OLO":        ("Phone In Tender",   "Retail"),
    # Catering
    "Catering":             ("Catering - Pickup", "Catering"),
    # 1PD (1st-party delivery / online / curbside / dispatch)
    "X-ToGo-OLO":           ("Online Ordering",   "1PD"),
    "X-Curbside-OLO":       ("Curbside",          "1PD"),
    "X-Dispatch DD":        ("Dispatch",          "1PD"),
    "X-Dispatch DL":        ("Dispatch",          "1PD"),
    "X-Dispatch PM":        ("Dispatch",          "1PD"),
    "X-Dispatch (Generic)": ("Dispatch",          "1PD"),
    # 3PD (3rd-party marketplaces)
    "X-Doordash (R)":       ("Door Dash",         "3PD"),
    "X-UberEats (R)":       ("Uber Eats",         "3PD"),
    "X-GrubHub (R)":        ("Grub Hub",          "3PD"),
    "X-Rails (Generic)":    ("Rails",             "3PD"),
    # Employee
    "Employee Meal":        ("Employee Meal",     "Employee Meal"),
}


def _map_segment(toast_dining_option: str) -> tuple:
    """Map a Toast dining-option name to (IPA SEGMENT_NAME, IPA ORDER_CHANNEL).
    Unknown names pass through with raw name + blank channel."""
    return _IPA_SEGMENT_MAP.get(toast_dining_option, (toast_dining_option, ""))


def _aggregate_to_ipa(era_rows: list) -> Dict[tuple, dict]:
    """
    Collapse ERA rows (one per Toast dining-option) onto IPA's grain by
    summing net_sales + orders for every Toast name that maps to the same
    (ipa_segment, ipa_channel) pair.

    Returns: {(ipa_segment, ipa_channel): {"net_sales": float, "orders": float}}
    """
    out: Dict[tuple, dict] = {}
    for row in era_rows:
        if not isinstance(row, dict):
            continue
        toast_seg = str(row.get("diningOption", "") or "").strip()
        if not toast_seg:
            continue
        ipa_seg, ipa_ch = _map_segment(toast_seg)
        key = (ipa_seg, ipa_ch)
        bucket = out.setdefault(key, {"net_sales": 0.0, "orders": 0.0})
        bucket["net_sales"] += float(row.get("netSalesAmount", 0) or 0)
        bucket["orders"]    += float(row.get("ordersCount",    0) or 0)
    return out


def _build_fiscal_index(start: date, end: date) -> Dict[str, dict]:
    """
    For each date in [start, end] present in fiscal_calendar, return
        {date_iso: {ly_date, fiscal_year (str "Y2026"), week_start, week_end, period_start}}

    Pads the SQL pull by ~1 year either side so the (fiscal_year, fiscal_week)
    and (fiscal_year, fiscal_period) min/max-date lookups stay complete even
    when the requested range starts or ends inside a week/period.
    """
    conn = aloha_sales_db.get_connection()
    try:
        pad_start = (start - timedelta(days=400)).isoformat()
        pad_end   = (end   + timedelta(days=400)).isoformat()
        rows = conn.execute(
            "SELECT date, ly_date, fiscal_year, fiscal_period, fiscal_week"
            "  FROM fiscal_calendar"
            " WHERE date >= ? AND date <= ?",
            (pad_start, pad_end),
        ).fetchall()
    finally:
        conn.close()

    by_week:   Dict[tuple, List[str]] = {}
    by_period: Dict[tuple, List[str]] = {}
    by_date:   Dict[str, dict] = {}
    for d_str, ly_str, fy, fp, fw in rows:
        if fy is None or fw is None:
            continue
        by_week.setdefault((fy, fw), []).append(d_str)
        if fp is not None:
            by_period.setdefault((fy, fp), []).append(d_str)
        by_date[d_str] = {"ly_date": ly_str, "fy": fy, "fp": fp, "fw": fw}

    out: Dict[str, dict] = {}
    d = start
    while d <= end:
        d_iso = d.isoformat()
        meta = by_date.get(d_iso)
        if not meta:
            d += timedelta(days=1)
            continue
        wk = sorted(by_week.get((meta["fy"], meta["fw"]), []))
        pd_ = sorted(by_period.get((meta["fy"], meta["fp"]), [])) if meta["fp"] is not None else []
        out[d_iso] = {
            "ly_date":      meta["ly_date"],
            "fiscal_year":  f"Y{meta['fy']}",
            "week_start":   wk[0]   if wk  else d_iso,
            "week_end":     wk[-1]  if wk  else d_iso,
            "period_start": pd_[0]  if pd_ else d_iso,
        }
        d += timedelta(days=1)
    return out


# Aloha's day-level channel columns -> (IPA SEGMENT_NAME, IPA ORDER_CHANNEL).
# Aligned with _DINING_OPTION_TO_DB_COL in main.py (which documents how Newk's
# splits Toast dining options into these Aloha buckets). Multiple Aloha cols can
# collapse to the same IPA segment (callin + olo_callin -> Phone In Tender;
# curbside + curbside_olo -> Curbside) — values get summed.
#
# `gng_net_sales` is intentionally absent: GNG is an item category that sits
# inside the channel cols above, not a separate channel. Including it would
# double-count revenue.
_ALOHA_TO_IPA_SEGMENT: List[tuple] = [
    # (aloha_col,            ipa_segment,         ipa_channel)
    ("dinein_net_sales",       "Eat In",            "Retail"),
    ("togo_net_sales",         "Carry Out",         "Retail"),
    ("callin_net_sales",       "Phone In Tender",   "Retail"),
    ("olo_callin_net_sales",   "Phone In Tender",   "Retail"),
    ("catering_net_sales",     "Catering - Pickup", "Catering"),
    ("olo_net_sales",          "Online Ordering",   "1PD"),
    ("curbside_net_sales",     "Curbside",          "1PD"),
    ("curbside_olo_net_sales", "Curbside",          "1PD"),
    ("delivery_net_sales",     "Delivery",          "3PD"),
]


def _aloha_ly_rows(location: str, biz_date: str) -> List[dict]:
    """
    Return per-channel Aloha LY rows for (location, business_date), already
    collapsed to IPA's (segment, channel) grain.

    Aloha tracks net sales per channel but check_count only at the daily level
    (plus a separate `checks_minus_catering` which lets us back out the
    catering check count). We honor that:

      * Per-channel rows carry real NetSalesLY, with TransLY=0 — we don't
        invent per-channel check counts that the source never tracked.
      * The Catering - Pickup row gets TransLY = check_count - checks_minus_catering,
        which is the real catering check count.
      * One trailing row, SEGMENT_NAME='Unallocated Checks (Non-Catering)',
        carries the residual non-catering check count (checks_minus_catering)
        with NetSalesLY=0 — clearly labeled so it's obvious in BI.

    Sum of NetSalesLY across the per-channel rows reconciles to the disjoint
    Aloha channel total (i.e. day net sales minus GNG, which is an item-level
    cross-cut already inside the channel cols). Sum of TransLY reconciles to
    Aloha's daily check_count exactly.

    Returns [{ipa_segment, ipa_channel, net_sales, trans}, ...].
    """
    cols = [a for (a, _s, _c) in _ALOHA_TO_IPA_SEGMENT]
    select = ", ".join(cols + ["check_count", "checks_minus_catering"])
    conn = aloha_sales_db.get_connection()
    try:
        row = conn.execute(
            f"SELECT {select} FROM aloha_sales_data"
            f" WHERE business_date = ? AND location = ?",
            (biz_date, str(location)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    values         = [float(v or 0) for v in row[:len(cols)]]
    total_checks   = float(row[len(cols)]     or 0)
    noncat_checks  = float(row[len(cols) + 1] or 0)
    catering_checks = max(total_checks - noncat_checks, 0.0)

    # Sum revenue by IPA (segment, channel) — collapsing callin+olo_callin,
    # curbside+curbside_olo per the existing flash mapping.
    buckets: Dict[tuple, float] = {}
    for (col, ipa_seg, ipa_ch), val in zip(_ALOHA_TO_IPA_SEGMENT, values):
        if val == 0:
            continue
        key = (ipa_seg, ipa_ch)
        buckets[key] = buckets.get(key, 0.0) + val

    out: List[dict] = []
    for (ipa_seg, ipa_ch), revenue in buckets.items():
        trans = catering_checks if ipa_seg == "Catering - Pickup" else 0.0
        out.append({
            "ipa_segment": ipa_seg,
            "ipa_channel": ipa_ch,
            "net_sales":   revenue,
            "trans":       trans,
        })

    # Residual non-catering check count — emitted as a clearly-labeled row so
    # BI sees the daily total without us pretending to know per-channel split.
    if noncat_checks:
        out.append({
            "ipa_segment": "Unallocated Checks (Non-Catering)",
            "ipa_channel": "",
            "net_sales":   0.0,
            "trans":       noncat_checks,
        })

    return out


async def _era_dining_option_day(
    client: httpx.AsyncClient,
    biz_date: date,
    store_guid: str,
    *,
    request_toast_report,
    fetch_toast_report,
) -> List[dict]:
    """Fetch ERA metrics for one (date, store) grouped by DINING_OPTION."""
    report_guid = await request_toast_report(
        client, biz_date, biz_date, [store_guid],
        report_type="metrics", group_by=["DINING_OPTION"],
    )
    return await fetch_toast_report(client, report_guid, report_type="metrics") or []


async def build_ipa_rows(
    start_date: date,
    end_date: date,
    store_guids: List[str],
    *,
    request_toast_report: Callable,
    fetch_toast_report: Callable,
    guid_to_location: Dict[str, str],
    progress_cb: Optional[Callable[[int, int], None]] = None,
    is_cancelled_cb: Optional[Callable[[], bool]] = None,
) -> List[dict]:
    """Build IPA-template rows for the given range and store list."""
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    if not store_guids:
        return []

    fiscal = _build_fiscal_index(start_date, end_date)
    if not fiscal:
        raise RuntimeError(
            f"fiscal_calendar has no rows in {start_date}..{end_date}. "
            "Load it via the fiscal-CSV importer before running this export."
        )

    switchover = (toast_config_db.get_switchover_date_map()
                  if toast_config_db.db_available() else {})
    store_info = aloha_sales_db.get_store_info()

    # Per-store metadata: location (aloha id, used as Toast store number too),
    # comp flag from store_info, and switchover date if known.
    store_meta: Dict[str, dict] = {}
    for guid in store_guids:
        loc = guid_to_location.get(guid)
        if not loc:
            logger.warning("IPA export: no aloha-location mapping for store_guid=%s — Location column will be blank", guid)
        info = store_info.get(loc, {}) if loc else {}
        store_meta[guid] = {
            "location":   loc or "",
            "comp_flag":  "Y" if (info.get("comping") == "Y") else "N",
            "switchover": switchover.get(guid),
        }

    all_rows: List[dict] = []
    fiscal_dates = sorted(fiscal.keys())
    total_units = len(fiscal_dates) * len(store_guids)
    done_units = 0

    async with httpx.AsyncClient(timeout=180.0) as client:
        for d_iso in fiscal_dates:
            if is_cancelled_cb and is_cancelled_cb():
                logger.info("IPA export cancelled at date %s", d_iso)
                return all_rows

            meta = fiscal[d_iso]
            d = date.fromisoformat(d_iso)
            ly_d_iso = meta["ly_date"]
            ly_d = date.fromisoformat(ly_d_iso) if ly_d_iso else None

            # Fan out TY (and Toast-LY where eligible) for all stores on this date.
            ty_tasks = {
                guid: asyncio.create_task(_era_dining_option_day(
                    client, d, guid,
                    request_toast_report=request_toast_report,
                    fetch_toast_report=fetch_toast_report,
                ))
                for guid in store_guids
            }
            ly_tasks: Dict[str, asyncio.Task] = {}
            for guid in store_guids:
                sd = store_meta[guid]["switchover"]
                if ly_d and sd and sd <= ly_d_iso:
                    ly_tasks[guid] = asyncio.create_task(_era_dining_option_day(
                        client, ly_d, guid,
                        request_toast_report=request_toast_report,
                        fetch_toast_report=fetch_toast_report,
                    ))

            for guid in store_guids:
                if is_cancelled_cb and is_cancelled_cb():
                    return all_rows

                try:
                    ty_rows = await ty_tasks[guid]
                except Exception as exc:
                    logger.warning("IPA export: TY fetch failed (date=%s, store=%s): %s", d_iso, guid, exc)
                    ty_rows = []

                ly_rows: List[dict] = []
                if guid in ly_tasks:
                    try:
                        ly_rows = await ly_tasks[guid]
                    except Exception as exc:
                        logger.warning("IPA export: LY Toast fetch failed (date=%s, store=%s): %s", d_iso, guid, exc)
                        ly_rows = []

                # Roll Toast's per-dining-option rows up to IPA segment grain
                # (e.g. Dine In + X-Dine-In (K) + X-Dine-In-OLO all sum into "Eat In").
                ty_by_ipa = _aggregate_to_ipa(ty_rows)

                # LY: from Toast ERA if the store was on Toast for the LY date,
                # otherwise from Aloha channel cols mapped to IPA segments.
                ly_by_ipa: Dict[tuple, dict] = {}
                if guid in ly_tasks:
                    ly_by_ipa = _aggregate_to_ipa(ly_rows)
                elif ly_d_iso and store_meta[guid]["location"]:
                    for ly in _aloha_ly_rows(store_meta[guid]["location"], ly_d_iso):
                        key = (ly["ipa_segment"], ly["ipa_channel"])
                        ly_by_ipa[key] = {"net_sales": ly["net_sales"], "orders": ly["trans"]}

                sm = store_meta[guid]
                base_row = {
                    "DAYDATE":         d_iso,
                    "Location":        sm["location"],
                    "WeekEndingDate":  meta["week_end"],
                    "FiscalYear":      meta["fiscal_year"],
                    "FISCALWEEKKEY":   meta["week_start"],
                    "FISCALPERIODKEY": meta["period_start"],
                    "SHIFTPART":       "",
                    "COMP_FLAG":       sm["comp_flag"],
                }

                # Union of IPA-segment keys across TY and LY — emit one row per
                # unique (segment, channel) so each store-day-segment appears once.
                for key in set(ty_by_ipa.keys()) | set(ly_by_ipa.keys()):
                    ipa_seg, ipa_ch = key
                    ty = ty_by_ipa.get(key, {"net_sales": 0.0, "orders": 0.0})
                    ly = ly_by_ipa.get(key, {"net_sales": 0.0, "orders": 0.0})
                    net_ty, trans_ty = ty["net_sales"], ty["orders"]
                    net_ly, trans_ly = ly["net_sales"], ly["orders"]
                    if not (net_ty or trans_ty or net_ly or trans_ly):
                        continue
                    all_rows.append({
                        **base_row,
                        "SEGMENT_NAME":  ipa_seg,
                        "ORDER_CHANNEL": ipa_ch,
                        "NetSalesTY":    net_ty,
                        "TransTY":       trans_ty,
                        "NetSalesLY":    net_ly,
                        "TransLY":       trans_ly,
                    })

                done_units += 1
                if progress_cb:
                    try:
                        progress_cb(done_units, total_units)
                    except Exception:
                        pass

    return all_rows


def rows_to_csv(rows: List[dict]) -> bytes:
    """Serialize IPA rows to CSV bytes (UTF-8, no BOM)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=IPA_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")
