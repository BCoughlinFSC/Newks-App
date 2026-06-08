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

  TY:  Toast ERA on D when the store was on Toast (switchover <= D and
       Toast actually returned rows). Otherwise — for historical backfills
       and stores that never made it onto Toast — falls back to Aloha
       channel-column data at D, mapped to IPA segments via
       _ALOHA_TO_IPA_SEGMENT. Same helper that builds the LY-Aloha rows
       (just keyed into NetSalesTY/TransTY in the output instead of LY).
  LY:  Toast ERA on ly_date(D) if switchover_date <= ly_date(D);
       otherwise Aloha channel-column data (aloha_sales_data) at
       ly_date(D), mapped to IPA segments via _ALOHA_TO_IPA_SEGMENT.

For Aloha-LY rows, TransLY is filled honestly: zero on non-catering
per-channel rows (Aloha never tracked per-channel check counts), the
real catering check count on the Catering - Pickup row, and the residual
non-catering check count on one trailing 'Unallocated Checks (Non-Catering)'
row. NetSalesLY sums exactly to Aloha's daily net_sales (excluding GNG, an
item-level category that sits inside the channel cols).

When the daypart-grain enrichment table `aloha_sales_daypart` has data for
the LY date (loaded from dev_aloha.dbo.tbl_SalesDataByDayPart via the SSMS
export), per-shift SHIFTPART, per-3PD segment splits, and per-shift
check counts all become available. When that table has no rows for the
date, we fall back to the day-level aloha_sales_data path (one row per
channel, SHIFTPART blank). `gng_net_sales` is intentionally skipped to
avoid double-counting (item-level category, not a channel).

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
import aloha_daypart_db
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


# Richer mapping used when daypart-grain rows exist in aloha_sales_daypart.
# Source has per-3PD splits (DoorDash, UberEats, GrubHub, Postmates, etc.)
# AND per-shift granularity, so we can fill SHIFTPART and break delivery out.
#
# `delivery_net_sales` is the LEGACY combined column in the source — newer rows
# put the real 3PD revenue in the named columns and leave delivery_net_sales=0.
# Including it here is safe (zero values produce no rows) and protects against
# any older rows where the per-3PD split wasn't populated.
_ALOHA_DAYPART_TO_IPA_SEGMENT: List[tuple] = [
    # Retail
    ("dinein_net_sales",         "Eat In",            "Retail"),
    ("olo_dinein_net_sales",     "Eat In",            "Retail"),
    ("togo_net_sales",           "Carry Out",         "Retail"),
    ("drivethru_net_sales",      "Carry Out",         "Retail"),
    ("olo_drivethru_net_sales",  "Carry Out",         "Retail"),
    ("callin_net_sales",         "Phone In Tender",   "Retail"),
    ("olo_callin_net_sales",     "Phone In Tender",   "Retail"),
    # Catering
    ("catering_net_sales",       "Catering - Pickup", "Catering"),
    ("ezcater_net_sales",        "Catering - Pickup", "Catering"),
    # 1PD (online / curbside / Newk's-own delivery)
    ("olo_net_sales",            "Online Ordering",   "1PD"),
    ("curbside_net_sales",       "Curbside",          "1PD"),
    ("curbside_olo_net_sales",   "Curbside",          "1PD"),
    ("nwk_delivery_net_sales",   "Dispatch",          "1PD"),
    ("dispatch_olo_net_sales",   "Dispatch",          "1PD"),
    # 3PD splits (the new wins)
    ("doordash_net_sales",       "Door Dash",         "3PD"),
    ("ubereats_net_sales",       "Uber Eats",         "3PD"),
    ("grubhub_net_sales",        "Grub Hub",          "3PD"),
    ("postmates_net_sales",      "Postmates",         "3PD"),
    ("bitesquad_net_sales",      "Bite Squad",        "3PD"),
    ("favor_net_sales",          "Favor",             "3PD"),
    ("foodsby_net_sales",        "Foodsby",           "3PD"),
    ("google_net_sales",         "Google",            "3PD"),
    ("deliverclub_net_sales",    "DeliverClub",       "3PD"),
    ("rails_olo_net_sales",      "Rails",             "3PD"),
    ("waitr_net_sales",          "Waitr",             "3PD"),
    ("delivery_net_sales",       "Delivery",          "3PD"),  # legacy combined; usually 0
    # gng_net_sales intentionally skipped (item-category cross-cut)
]


def _normalize_shiftpart(daypart: str) -> str:
    """Aloha daypart label -> IPA SHIFTPART label. Empty/'NULL' becomes blank."""
    if not daypart or str(daypart).strip().upper() == "NULL":
        return ""
    return str(daypart).strip()


def _emit_buckets_with_check_split(
    buckets: dict,
    total_checks: float,
    noncat_checks: float,
    shiftpart: str,
) -> List[dict]:
    """
    Convert a (segment, channel) -> net_sales dict into IPA rows, attaching
    the real catering check count to the Catering - Pickup row, residual
    non-catering checks to one 'Unallocated Checks (Non-Catering)' row, and
    SHIFTPART to every emitted row. TransLY on all non-catering channel rows
    is 0 — Aloha never tracked per-channel check counts.
    """
    catering_checks = max(total_checks - noncat_checks, 0.0)
    out: List[dict] = []
    for (ipa_seg, ipa_ch), revenue in buckets.items():
        trans = catering_checks if ipa_seg == "Catering - Pickup" else 0.0
        out.append({
            "ipa_segment": ipa_seg,
            "ipa_channel": ipa_ch,
            "shiftpart":   shiftpart,
            "net_sales":   revenue,
            "trans":       trans,
        })
    if noncat_checks:
        out.append({
            "ipa_segment": "Unallocated Checks (Non-Catering)",
            "ipa_channel": "",
            "shiftpart":   shiftpart,
            "net_sales":   0.0,
            "trans":       noncat_checks,
        })
    return out


def _aloha_ly_rows_from_daypart(location: str, biz_date: str) -> List[dict]:
    """
    Build LY rows from the daypart-grain enrichment table (aloha_sales_daypart).

    Per-segment revenue (and catering check counts) is *summed across all
    dayparts* and emitted at SHIFTPART="" so it can merge cleanly with the
    matching TY row in build_ipa_rows. Per-shift granularity that's truly
    useful for labor planning (non-catering check counts per shift) is
    preserved on dedicated 'Unallocated Checks (Non-Catering)' rows with
    SHIFTPART filled (Lunch / Dinner / Breakfast / Off Hours).

    Per-3PD splits (Door Dash / Uber Eats / Grub Hub / Postmates / ...) come
    for free since the per-segment aggregation happens after the daypart-grain
    column mapping.

    Returns [] when no rows in the table — caller falls back to day-level.
    """
    daypart_rows = aloha_daypart_db.get_daypart_rows(location, biz_date)
    if not daypart_rows:
        return []

    # Aggregate revenue per (segment, channel) across ALL shifts.
    seg_buckets: Dict[tuple, float] = {}
    total_checks_sum   = 0.0
    catering_checks_sum = 0.0
    noncat_per_shift: Dict[str, float] = {}

    for dp_row in daypart_rows:
        shiftpart    = _normalize_shiftpart(dp_row.get("daypart", ""))
        total_checks = float(dp_row.get("check_count")            or 0)
        noncat_chks  = float(dp_row.get("checks_minus_catering")  or 0)
        catering_checks_sum += max(total_checks - noncat_chks, 0.0)
        total_checks_sum    += total_checks
        if noncat_chks:
            noncat_per_shift[shiftpart] = noncat_per_shift.get(shiftpart, 0.0) + noncat_chks

        for (col, ipa_seg, ipa_ch) in _ALOHA_DAYPART_TO_IPA_SEGMENT:
            val = float(dp_row.get(col) or 0)
            if val == 0:
                continue
            key = (ipa_seg, ipa_ch)
            seg_buckets[key] = seg_buckets.get(key, 0.0) + val

    out: List[dict] = []

    # One row per (segment, channel) at SHIFTPART="" so the call site can
    # merge with the matching TY row. Catering checks land on the
    # Catering - Pickup row.
    for (ipa_seg, ipa_ch), revenue in seg_buckets.items():
        trans = catering_checks_sum if ipa_seg == "Catering - Pickup" else 0.0
        out.append({
            "ipa_segment": ipa_seg,
            "ipa_channel": ipa_ch,
            "shiftpart":   "",
            "net_sales":   revenue,
            "trans":       trans,
        })

    # Per-shift Unallocated Checks rows — kept split so BI can see when
    # checks actually happened, for labor planning.
    for shiftpart, noncat in noncat_per_shift.items():
        out.append({
            "ipa_segment": "Unallocated Checks (Non-Catering)",
            "ipa_channel": "",
            "shiftpart":   shiftpart,
            "net_sales":   0.0,
            "trans":       noncat,
        })

    return out


def _aloha_ly_rows_from_day_level(location: str, biz_date: str) -> List[dict]:
    """
    Fallback: build LY rows from the day-level aloha_sales_data when the
    daypart-grain table has no rows for this (location, date). SHIFTPART is
    blank, per-3PD splits aren't available (one combined 'Delivery' row).
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
    values        = [float(v or 0) for v in row[:len(cols)]]
    total_checks  = float(row[len(cols)]     or 0)
    noncat_checks = float(row[len(cols) + 1] or 0)

    buckets: Dict[tuple, float] = {}
    for (col, ipa_seg, ipa_ch), val in zip(_ALOHA_TO_IPA_SEGMENT, values):
        if val == 0:
            continue
        key = (ipa_seg, ipa_ch)
        buckets[key] = buckets.get(key, 0.0) + val

    return _emit_buckets_with_check_split(buckets, total_checks, noncat_checks, "")


def _aloha_ly_rows(location: str, biz_date: str) -> List[dict]:
    """
    Build IPA LY rows for stores that were on Aloha on the LY date.

    Prefers the daypart-grain enrichment table (real SHIFTPART, per-3PD splits,
    per-shift check counts). Falls back to the day-level aloha_sales_data
    table when daypart-grain rows aren't available for (location, biz_date).

    Returns [{ipa_segment, ipa_channel, shiftpart, net_sales, trans}, ...].
    """
    rows = _aloha_ly_rows_from_daypart(location, biz_date)
    if rows:
        return rows
    return _aloha_ly_rows_from_day_level(location, biz_date)


async def _era_dining_option_day_batch(
    client: httpx.AsyncClient,
    biz_date: date,
    store_guids: List[str],
    *,
    request_toast_report,
    fetch_toast_report,
    max_attempts: int = 6,
) -> Dict[str, List[dict]]:
    """
    One ERA call covers all `store_guids` for `biz_date` (groupBy=DINING_OPTION),
    split back into {store_guid: [rows...]} on the way out. This collapses the
    93 stores * 18 days = ~1700 per-store fan-out into one call per date — well
    under Toast's rate limits and a fraction of the wall-clock time.

    Retries on 429 (rate limit) with exponential backoff. Total max wait of
    ~3 minutes (10s, 20s, 40s, 60s, 60s) — Toast's rate-limit windows are
    typically 60s so 6 attempts virtually guarantees recovery from a
    transient throttle. Other transient failures (502/503/504) are already
    retried inside request_toast_report.
    """
    if not store_guids:
        return {}

    delay = 10.0
    for attempt in range(max_attempts):
        try:
            report_guid = await request_toast_report(
                client, biz_date, biz_date, list(store_guids),
                report_type="metrics", group_by=["DINING_OPTION"],
            )
            all_rows = await fetch_toast_report(
                client, report_guid, report_type="metrics",
            ) or []
            break
        except Exception as exc:
            is_429 = "429" in str(exc) or "rate limit" in str(exc).lower()
            if is_429 and attempt < max_attempts - 1:
                logger.warning(
                    "IPA export: ERA 429 on %s (batch of %d stores), backing off %.1fs (attempt %d/%d)",
                    biz_date, len(store_guids), delay, attempt + 1, max_attempts,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise

    # Split the response by restaurantGuid. Rows for guids not in the request
    # (defensive) get dropped; guids with no rows return an empty list.
    out: Dict[str, List[dict]] = {g: [] for g in store_guids}
    for row in all_rows:
        if not isinstance(row, dict):
            continue
        rg = str(row.get("restaurantGuid", "") or "")
        if rg in out:
            out[rg].append(row)
    return out


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

            # Optimization for historical backfills: if EVERY selected store
            # was not yet on Toast on this date, skip the Toast ERA call
            # entirely and go straight to the Aloha-TY fallback below.
            # Saves ~3 min/date of pointless retries on Toast 429s (the API
            # rate-limits hard when we ask for data on dates where nothing
            # was sending it any orders yet).
            #
            # Condition: every store has a KNOWN switchover_date AND that
            # date is strictly after d_iso. If we don't have a switchover
            # for a store, we err on the side of calling Toast in case the
            # switchover map is stale.
            all_pre_switchover = all(
                store_meta[g]["switchover"] and d_iso < store_meta[g]["switchover"]
                for g in store_guids
            )

            if all_pre_switchover:
                logger.info("IPA export: %s — all %d stores pre-switchover, skipping Toast ERA",
                            d_iso, len(store_guids))
                ty_by_store = {g: [] for g in store_guids}
            else:
                # One batched ERA call per date — all stores at once — avoids
                # rate-limit rejections that a per-store fan-out triggers at chain scale.
                try:
                    ty_by_store = await _era_dining_option_day_batch(
                        client, d, store_guids,
                        request_toast_report=request_toast_report,
                        fetch_toast_report=fetch_toast_report,
                    )
                except Exception as exc:
                    logger.warning("IPA export: TY batch fetch failed (date=%s, stores=%d): %s",
                                   d_iso, len(store_guids), exc)
                    ty_by_store = {g: [] for g in store_guids}

            # LY batch — only for stores whose Toast switchover predates ly_date.
            ly_eligible = [
                g for g in store_guids
                if ly_d and store_meta[g]["switchover"]
                and store_meta[g]["switchover"] <= ly_d_iso
            ]
            ly_by_store: Dict[str, List[dict]] = {}
            if ly_eligible:
                try:
                    ly_by_store = await _era_dining_option_day_batch(
                        client, ly_d, ly_eligible,
                        request_toast_report=request_toast_report,
                        fetch_toast_report=fetch_toast_report,
                    )
                except Exception as exc:
                    logger.warning("IPA export: LY batch fetch failed (date=%s, stores=%d): %s",
                                   ly_d_iso, len(ly_eligible), exc)
                    ly_by_store = {}

            for guid in store_guids:
                if is_cancelled_cb and is_cancelled_cb():
                    return all_rows

                ty_rows = ty_by_store.get(guid, [])
                ly_rows = ly_by_store.get(guid, [])

                # Roll Toast's per-dining-option rows up to IPA segment grain
                # (e.g. Dine In + X-Dine-In (K) + X-Dine-In-OLO all sum into "Eat In").
                ty_by_ipa = _aggregate_to_ipa(ty_rows)
                ty_extra_rows: List[dict] = []

                # Aloha-TY fallback for historical backfill. When the export
                # date precedes a store's Toast switchover (or the switchover
                # is unknown), Toast ERA returns 0 rows. Mirror the LY logic:
                # pull TY from the local Aloha SQLite using the same
                # _aloha_ly_rows() helper. Same merge shape (segment, channel)
                # rolls into ty_by_ipa; per-shift "Unallocated Checks" lines
                # become ty_extra_rows (TY-side counterparts to ly_extra_rows).
                #
                # Trigger: Toast returned NO rows for this (store, date). We
                # don't trigger on "Toast returned rows but they're all zero"
                # because that's a legitimate slow day, not missing data.
                switchover_iso = store_meta[guid]["switchover"]
                is_pre_switchover = (not switchover_iso) or (d_iso < switchover_iso)
                if not ty_rows and is_pre_switchover and store_meta[guid]["location"]:
                    for ty in _aloha_ly_rows(store_meta[guid]["location"], d_iso):
                        if ty["shiftpart"]:
                            ty_extra_rows.append(ty)
                            continue
                        key = (ty["ipa_segment"], ty["ipa_channel"])
                        bucket = ty_by_ipa.setdefault(key, {"net_sales": 0.0, "orders": 0.0})
                        bucket["net_sales"] += ty["net_sales"]
                        bucket["orders"]    += ty["trans"]

                # Build LY at (segment, channel) grain so we can co-locate it
                # with the matching TY on one row. Extra rows that don't fit
                # the (segment, channel) merge (per-shift Unallocated Checks)
                # are collected separately and appended after.
                ly_by_ipa: Dict[tuple, dict] = {}
                ly_extra_rows: List[dict] = []

                if guid in ly_by_store:
                    # Toast-LY-eligible: ERA gives us per-dining-option grain.
                    ly_by_ipa = _aggregate_to_ipa(ly_rows)
                elif ly_d_iso and store_meta[guid]["location"]:
                    # Aloha-LY: prefer daypart-grain (per-3PD splits) but flatten
                    # to (segment, channel) for the merge; per-shift Unallocated
                    # rows stay split out as "extras".
                    for ly in _aloha_ly_rows(store_meta[guid]["location"], ly_d_iso):
                        if ly["shiftpart"]:
                            ly_extra_rows.append(ly)
                            continue
                        key = (ly["ipa_segment"], ly["ipa_channel"])
                        bucket = ly_by_ipa.setdefault(key, {"net_sales": 0.0, "orders": 0.0})
                        bucket["net_sales"] += ly["net_sales"]
                        bucket["orders"]    += ly["trans"]

                sm = store_meta[guid]
                base_row = {
                    "DAYDATE":         d_iso,
                    "Location":        sm["location"],
                    "WeekEndingDate":  meta["week_end"],
                    "FiscalYear":      meta["fiscal_year"],
                    "FISCALWEEKKEY":   meta["week_start"],
                    "FISCALPERIODKEY": meta["period_start"],
                    "COMP_FLAG":       sm["comp_flag"],
                }

                # One row per unique (segment, channel) — TY and LY share the row.
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
                        "SHIFTPART":     "",
                        "NetSalesTY":    net_ty,
                        "TransTY":       trans_ty,
                        "NetSalesLY":    net_ly,
                        "TransLY":       trans_ly,
                    })

                # Per-shift Unallocated Checks rows — emit as standalone LY-only
                # rows with SHIFTPART filled. These carry the non-catering check
                # counts that Aloha never tracked at channel grain.
                for ly in ly_extra_rows:
                    all_rows.append({
                        **base_row,
                        "SEGMENT_NAME":  ly["ipa_segment"],
                        "ORDER_CHANNEL": ly["ipa_channel"],
                        "SHIFTPART":     ly["shiftpart"],
                        "NetSalesTY":    0.0,
                        "TransTY":       0.0,
                        "NetSalesLY":    ly["net_sales"],
                        "TransLY":       ly["trans"],
                    })

                # Same idea for TY-side Aloha extras (only populated when the
                # date predates this store's Toast switchover and Aloha had
                # daypart-grain data for the day).
                for ty in ty_extra_rows:
                    all_rows.append({
                        **base_row,
                        "SEGMENT_NAME":  ty["ipa_segment"],
                        "ORDER_CHANNEL": ty["ipa_channel"],
                        "SHIFTPART":     ty["shiftpart"],
                        "NetSalesTY":    ty["net_sales"],
                        "TransTY":       ty["trans"],
                        "NetSalesLY":    0.0,
                        "TransLY":       0.0,
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
