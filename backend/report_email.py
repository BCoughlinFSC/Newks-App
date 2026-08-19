"""
report_email.py — renders a subscribed report into an emailable HTML body plus
an optional .xlsx attachment.

Each entry in REPORTS declares one subscribable report: its label, the params it
accepts, the SSRS report it replaces, and a build() that turns (params, run_date)
into a RenderedReport. subscriptions_db holds the schedule + recipients;
main.py's dispatcher glues the two together and hands the result to
email_service.send_report_email().

Adding a report = add one REPORTS entry. Nothing else changes.
"""
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import monkey_db

logger = logging.getLogger(__name__)

BRAND = "#8B1A1A"          # Newk's red — matches the app header
BRAND_DARK = "#6d1414"
INK = "#1f2933"
MUTED = "#6b7280"
RULE = "#e5e7eb"


# ── rendered payload ─────────────────────────────────────────────────

@dataclass
class Sheet:
    """One logical table — becomes an HTML table and one .xlsx worksheet."""
    title: str
    columns: List[Tuple[str, str, str]]   # (key, header, fmt) fmt: text|int|money|pct
    rows: List[Dict[str, Any]]
    total_row: Optional[Dict[str, Any]] = None


@dataclass
class RenderedReport:
    subject: str
    headline: str
    subhead: str
    sheets: List[Sheet] = field(default_factory=list)
    row_count: int = 0
    filename_stem: str = "report"
    # Cap how many sheets the EMAIL BODY shows. The .xlsx always carries all of
    # them. SSRS treated the Excel attachment as the deliverable and the body as
    # a one-line comment, so a full fiscal year of weeks belongs in the file, not
    # inline. 0 = show everything.
    html_max_sheets: int = 0


# ── date-window resolution ───────────────────────────────────────────

def resolve_window(params: Dict[str, Any], run_date: date) -> Tuple[str, str, str]:
    """Turn a window param into (start, end, label).

    Windows are expressed relative to the morning the subscription fires, so a
    Monday 7:45 AM send of a 'last_full_week' report covers the Mon-Sun that
    just closed — the same coverage the SSRS subscription had.
    """
    window = (params.get("window") or "last_full_week").lower()

    if window == "today":
        d = run_date
        return d.isoformat(), d.isoformat(), d.strftime("%A, %B ") + f"{d.day}, {d.year}"

    if window == "yesterday":
        d = run_date - timedelta(days=1)
        return d.isoformat(), d.isoformat(), d.strftime("%A, %B ") + f"{d.day}, {d.year}"

    if window == "last_full_week":
        # Most recent completed Mon-Sun block.
        last_sunday = run_date - timedelta(days=(run_date.weekday() + 1) % 7 or 7)
        start = last_sunday - timedelta(days=6)
        return start.isoformat(), last_sunday.isoformat(), \
            f"Week of {start.strftime('%b')} {start.day} – {last_sunday.strftime('%b')} {last_sunday.day}, {last_sunday.year}"

    if window == "month_to_date":
        start = run_date.replace(day=1)
        end = run_date - timedelta(days=1)
        if end < start:
            end = start
        return start.isoformat(), end.isoformat(), f"{start.strftime('%B %Y')} to date"

    # last_n_days (default)
    days = int(params.get("days") or 7)
    end = run_date - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat(), \
        f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year} ({days} days)"


# ── value formatting ─────────────────────────────────────────────────

def fmt_value(val: Any, fmt: str) -> str:
    if val is None or val == "":
        return "" if fmt == "text" else "—"
    try:
        if fmt == "money":
            return f"${float(val):,.2f}"
        if fmt == "int":
            return f"{int(val):,}"
        if fmt == "pct":
            return f"{float(val):,.1f}%"
    except (TypeError, ValueError):
        return str(val)
    return str(val)


def _is_numeric(fmt: str) -> bool:
    return fmt in ("money", "int", "pct")


# ── HTML rendering ───────────────────────────────────────────────────

def render_html(rep: RenderedReport, footer_note: str = "") -> str:
    """Email-client-safe HTML: inline styles, table layout, no external CSS."""
    def esc(s: Any) -> str:
        return (str(s if s is not None else "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    parts: List[str] = []
    parts.append(
        f'<div style="margin:0;padding:0;background:#f4f5f7;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f4f5f7;padding:24px 0;font-family:-apple-system,BlinkMacSystemFont,'
        f'\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;color:{INK};"><tr><td align="center">'
        f'<table role="presentation" width="920" cellpadding="0" cellspacing="0" '
        f'style="width:920px;max-width:100%;background:#ffffff;border-radius:10px;overflow:hidden;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.08);">'
    )
    # Header band
    parts.append(
        f'<tr><td style="background:{BRAND};padding:20px 28px;">'
        f'<div style="color:#fff;font-size:19px;font-weight:700;letter-spacing:.2px;">{esc(rep.headline)}</div>'
        f'<div style="color:#f3d5d5;font-size:13px;margin-top:4px;">{esc(rep.subhead)}</div>'
        f'</td></tr>'
    )
    parts.append('<tr><td style="padding:22px 28px 8px 28px;">')

    if not rep.sheets or all(not sh.rows for sh in rep.sheets):
        parts.append(
            f'<p style="font-size:14px;color:{MUTED};margin:8px 0 20px 0;">'
            f'No rows for this period.</p>'
        )

    shown = rep.sheets
    hidden = 0
    if rep.html_max_sheets and len(rep.sheets) > rep.html_max_sheets:
        shown = rep.sheets[-rep.html_max_sheets:]
        hidden = len(rep.sheets) - len(shown)
    if hidden:
        parts.append(
            f'<p style="font-size:13px;color:{MUTED};margin:0 0 6px 0;">'
            f'Showing the most recent {len(shown)} of {len(rep.sheets)} — '
            f'the attached spreadsheet has all {len(rep.sheets)}.</p>'
        )

    for sheet in shown:
        if sheet.title:
            parts.append(
                f'<div style="font-size:15px;font-weight:700;color:{INK};'
                f'margin:18px 0 8px 0;">{esc(sheet.title)}</div>'
            )
        if not sheet.rows:
            parts.append(f'<div style="font-size:13px;color:{MUTED};margin-bottom:14px;">No rows.</div>')
            continue

        parts.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:18px;">'
        )
        parts.append('<tr>')
        for _key, header, fmt in sheet.columns:
            align = "right" if _is_numeric(fmt) else "left"
            parts.append(
                f'<th align="{align}" style="background:#f8f9fb;border-bottom:2px solid {RULE};'
                f'padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;'
                f'color:{MUTED};font-weight:700;">{esc(header)}</th>'
            )
        parts.append('</tr>')

        for i, row in enumerate(sheet.rows):
            bg = "#ffffff" if i % 2 == 0 else "#fbfbfc"
            parts.append(f'<tr style="background:{bg};">')
            for key, _header, fmt in sheet.columns:
                align = "right" if _is_numeric(fmt) else "left"
                val = fmt_value(row.get(key), fmt)
                colour = INK
                if fmt in ("money", "pct"):
                    try:
                        if float(row.get(key) or 0) < 0:
                            colour = "#b91c1c"
                    except (TypeError, ValueError):
                        pass
                parts.append(
                    f'<td align="{align}" style="border-bottom:1px solid {RULE};padding:7px 10px;'
                    f'color:{colour};white-space:nowrap;">{esc(val)}</td>'
                )
            parts.append('</tr>')

        if sheet.total_row:
            parts.append(f'<tr style="background:#f4f5f7;">')
            for key, _header, fmt in sheet.columns:
                align = "right" if _is_numeric(fmt) else "left"
                val = fmt_value(sheet.total_row.get(key), fmt) if key in sheet.total_row else ""
                parts.append(
                    f'<td align="{align}" style="border-top:2px solid {RULE};padding:8px 10px;'
                    f'font-weight:700;color:{INK};white-space:nowrap;">{esc(val)}</td>'
                )
            parts.append('</tr>')
        parts.append('</table>')

    parts.append('</td></tr>')
    parts.append(
        f'<tr><td style="padding:6px 28px 22px 28px;border-top:1px solid {RULE};">'
        f'<div style="font-size:11px;color:{MUTED};line-height:1.6;">'
        f'{esc(footer_note)}</div></td></tr>'
    )
    parts.append('</table></td></tr></table></div>')
    return "".join(parts)


# ── XLSX rendering ───────────────────────────────────────────────────

def render_xlsx(rep: RenderedReport) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    head_fill = PatternFill("solid", fgColor="8B1A1A")
    head_font = Font(bold=True, color="FFFFFF", size=10)
    total_font = Font(bold=True)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(bottom=thin)

    used_titles: set = set()
    for sheet in rep.sheets or [Sheet("Report", [("v", "Value", "text")], [])]:
        title = (sheet.title or "Report")[:28] or "Report"
        for ch in "[]:*?/\\":
            title = title.replace(ch, "-")
        base, n = title, 2
        while title in used_titles:
            title = f"{base[:26]}_{n}"
            n += 1
        used_titles.add(title)
        ws = wb.create_sheet(title)

        for c, (_key, header, _fmt) in enumerate(sheet.columns, start=1):
            cell = ws.cell(row=1, column=c, value=header)
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        r = 2
        for row in sheet.rows:
            for c, (key, _header, fmt) in enumerate(sheet.columns, start=1):
                val = row.get(key)
                cell = ws.cell(row=r, column=c, value=val)
                cell.border = border
                if fmt == "money":
                    cell.number_format = '$#,##0.00'
                elif fmt == "int":
                    cell.number_format = '#,##0'
                elif fmt == "pct":
                    cell.number_format = '0.0"%"'
            r += 1

        if sheet.total_row:
            for c, (key, _header, fmt) in enumerate(sheet.columns, start=1):
                cell = ws.cell(row=r, column=c, value=sheet.total_row.get(key))
                cell.font = total_font
                if fmt == "money":
                    cell.number_format = '$#,##0.00'
                elif fmt == "int":
                    cell.number_format = '#,##0'
                elif fmt == "pct":
                    cell.number_format = '0.0"%"'

        for c, (key, header, fmt) in enumerate(sheet.columns, start=1):
            widths = [len(str(header)) + 4]
            widths += [len(str(row.get(key) or "")) + 3 for row in sheet.rows[:200]]
            ws.column_dimensions[get_column_letter(c)].width = min(max(max(widths), 10), 42)
        ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _scope_store_ids(params: Dict[str, Any]) -> Optional[List[int]]:
    """Store filter for a subscription, mirroring the SSRS StoreGroup parameter.

    SSRS scoped these with get_StoresIncluded(grouping 9, group 79=Corporate /
    group 8=Franchise). We resolve the same split off Monkey's own
    stores.region_name — see monkey_db.store_ids_by_scope.
    """
    return monkey_db.store_ids_by_scope(params.get("scope") or "all")


def _scope_label(scope: Optional[str]) -> str:
    return {"company": "Corporate stores", "franchise": "Franchise stores"}.get(
        (scope or "all").lower(), "All stores")


# ══ report builders ══════════════════════════════════════════════════

def _build_orders_due(params: Dict[str, Any], run_date: date) -> RenderedReport:
    """Monkey 'Orders Due For Today - Summary by Store'."""
    offset = int(params.get("dayOffset") or 0)
    biz_date = run_date + timedelta(days=offset)
    store_ids = _scope_store_ids(params)
    rows = monkey_db.rpt_orders_due_by_store(biz_date.isoformat(), store_ids=store_ids)

    out_rows = [{
        "storeNumber": r["storeNumber"],
        "storeName":   r["storeName"],
        "orderCount":  r["orderCount"],
        "totalDollars": r["totalDollars"],
    } for r in rows]

    total = {
        "storeName":  f"Total — {len(out_rows)} store(s)",
        "orderCount": sum(r["orderCount"] for r in out_rows),
        "totalDollars": round(sum(r["totalDollars"] for r in out_rows), 2),
    }
    pretty = biz_date.strftime("%A, %B ") + f"{biz_date.day}, {biz_date.year}"
    scope_lbl = _scope_label(params.get("scope"))
    return RenderedReport(
        subject=f"Monkey Orders Due — {scope_lbl} — {biz_date.isoformat()}",
        headline=f"Monkey Orders Due For Today — {scope_lbl}",
        subhead=f"Summary by store · {pretty}",
        sheets=[Sheet(
            title="",
            columns=[("storeNumber", "Store #", "text"),
                     ("storeName", "Restaurant", "text"),
                     ("orderCount", "Orders", "int"),
                     ("totalDollars", "Total $", "money")],
            rows=out_rows, total_row=total,
        )],
        row_count=len(out_rows),
        filename_stem=f"Monkey_Orders_Due_{(params.get('scope') or 'all')}_{biz_date.isoformat()}",
    )


def _build_refunds_by_user(params: Dict[str, Any], run_date: date) -> RenderedReport:
    """Monkey 'Refunds by User'."""
    start, end, label = resolve_window(params, run_date)
    store_ids = _scope_store_ids(params)
    rows = monkey_db.rpt_refunds_by_user(start, end, store_ids=store_ids)

    cols = [("refundDate", "Refund Date", "text"),
            ("refundedBy", "Refunded By", "text"),
            ("restaurantName", "Restaurant", "text"),
            ("orderNumber", "Order #", "text"),
            ("customerName", "Customer", "text"),
            ("paymentMethod", "Payment Method", "text"),
            ("refundNotes", "Refund Notes", "text"),
            ("orderTotal", "Order Total", "money"),
            ("refundSubTotal", "Refund Subtotal", "money"),
            ("refundTaxAmount", "Refund Tax", "money"),
            ("refundTotal", "Refund Total", "money")]

    total = {
        "refundDate": f"Total — {len(rows)} refund(s)",
        "orderTotal":      round(sum(r["orderTotal"] for r in rows), 2),
        "refundSubTotal":  round(sum(r["refundSubTotal"] for r in rows), 2),
        "refundTaxAmount": round(sum(r["refundTaxAmount"] for r in rows), 2),
        "refundTotal":     round(sum(r["refundTotal"] for r in rows), 2),
    }
    return RenderedReport(
        subject=f"Monkey Refunds by User — {start} to {end}",
        headline="Monkey — Refunds by User",
        subhead=f"{label} · {_scope_label(params.get('scope'))}",
        sheets=[Sheet(title="", columns=cols, rows=rows, total_row=total)],
        row_count=len(rows),
        filename_stem=f"Monkey_Refunds_by_User_{start}_{end}",
    )


def _build_sales_by_channel_weekday(params: Dict[str, Any], run_date: date) -> RenderedReport:
    """Monkey 'Sales by Channel by Weekday'."""
    start, end, label = resolve_window(params, run_date)
    group_by = (params.get("groupBy") or "system").lower()
    data = monkey_db.rpt_sales_by_channel(start, end, group_by)

    channels = data.get("channels", [])
    cols: List[Tuple[str, str, str]] = [("date", "Date", "text"),
                                        ("dayOfWeek", "Weekday", "text")]
    for ch in channels:
        cols.append((f'{ch["key"]}Count', f'{ch["label"]} #', "int"))
        cols.append((ch["key"], f'{ch["label"]} $', "money"))
    cols.append(("total", "Total $", "money"))

    sheets: List[Sheet] = []
    n_rows = 0
    for grp in data.get("groups", []):
        sheets.append(Sheet(title=grp.get("name") or "System Wide",
                            columns=cols, rows=grp.get("rows", []),
                            total_row=grp.get("totals")))
        n_rows += len(grp.get("rows", []))

    group_label = {"system": "System wide", "market": "By market",
                   "store": "By store"}.get(group_by, group_by)
    return RenderedReport(
        subject=f"Monkey Sales by Channel by Weekday — {start} to {end}",
        headline="Monkey Sales by Channel by Weekday",
        subhead=f"{label} · {group_label} · dollars are subtotal net of discount",
        sheets=sheets,
        row_count=n_rows,
        filename_stem=f"Monkey_Sales_by_Channel_{start}_{end}",
    )


def _build_period_catering_by_week(params: Dict[str, Any], run_date: date) -> RenderedReport:
    """'Period Catering by Week' — fiscal weeks, CY vs PY, by Area Director."""
    import main  # lazy: main imports this module at load time

    basis = params.get("basis") or "subtotal"
    # SSRS passed [Year] = datepart(year, getdate()) — i.e. the WHOLE year, not a
    # trailing window. Default to that; htmlWeeks keeps the email body readable.
    weeks_back = int(params.get("weeks") or 0)          # 0 = whole fiscal year
    fiscal_year = params.get("fiscalYear")

    if not fiscal_year:
        # Fiscal years run Jan-ish to Dec-ish; pick the FY containing run_date,
        # falling back to the newest year present in the calendar.
        fiscal_year = _current_fiscal_year(run_date)
    fiscal_dates = main._fiscal_dates_for_year(int(fiscal_year))
    if not fiscal_dates:
        raise RuntimeError(
            f"No fiscal_calendar data for FY{fiscal_year} — load it with load_fiscal_csv.py"
        )

    areadir_map = main._areadir_by_store_number()
    company_only = bool(areadir_map)
    weeks = monkey_db.rpt_period_catering_by_week_fiscal(
        fiscal_dates, areadir_map, basis, monkey_db.MONKEY_DB_PATH, company_only
    )

    # Only weeks that have already finished, most recent last.
    cutoff = run_date.isoformat()
    weeks = [w for w in weeks if w["weekStart"] <= cutoff]
    if weeks_back > 0:
        weeks = weeks[-weeks_back:]

    cols = [("areaDirName", "Area Director", "text"),
            ("storeNumber", "Store #", "text"),
            ("restaurantName", "Restaurant", "text"),
            ("currentYear", "Current Year", "money"),
            ("priorYear", "Prior Year", "money"),
            ("netDifference", "Net Difference", "money"),
            ("percentageChange", "% Change", "pct")]

    sheets: List[Sheet] = []
    n_rows = 0
    for wk in weeks:
        totals = dict(wk["totals"])
        totals["areaDirName"] = "Week Total"
        sheets.append(Sheet(title=f'Week ending {wk["weekStart"]}',
                            columns=cols, rows=wk["rows"], total_row=totals))
        n_rows += len(wk["rows"])

    span = f'{weeks[0]["weekStart"]} – {weeks[-1]["weekStart"]}' if weeks else "no weeks"
    basis_label = {"subtotal": "subtotal (net of discount)",
                   "total": "order total"}.get(basis, basis)
    return RenderedReport(
        subject=f"Period Catering by Week — FY{fiscal_year} ({span})",
        headline="Period Catering by Week",
        subhead=f"FY{fiscal_year} · {len(weeks)} week(s) through {span.split(' – ')[-1]} · {basis_label}",
        sheets=sheets,
        row_count=n_rows,
        filename_stem=f"Period_Catering_by_Week_FY{fiscal_year}",
        html_max_sheets=int(params.get("htmlWeeks") or 4),
    )


def _current_fiscal_year(run_date: date) -> int:
    """FY containing run_date per the fiscal_calendar, else the newest FY there."""
    import aloha_sales_db
    try:
        conn = aloha_sales_db.get_connection()
        try:
            row = conn.execute(
                "SELECT fiscal_year FROM fiscal_calendar WHERE date = ?",
                (run_date.isoformat(),),
            ).fetchone()
            if row and row[0]:
                return int(row[0])
            row = conn.execute(
                "SELECT MAX(fiscal_year) FROM fiscal_calendar WHERE fiscal_year IS NOT NULL"
            ).fetchone()
            if row and row[0]:
                return int(row[0])
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("fiscal year lookup failed: %s", exc)
    return run_date.year


# ══ registry ═════════════════════════════════════════════════════════

REPORTS: Dict[str, Dict[str, Any]] = {
    "monkey_orders_due": {
        "label": "Monkey Orders Due For Today",
        "replaces": "/Monkey Media/Monkey Orders Due For Today - Summary by Store",
        "page": "/catering.html",
        "build": _build_orders_due,
        "defaults": {"daysOfWeek": "mon,tue,wed,thu,fri,sat,sun", "hour": 7, "minute": 50,
                     "params": {"dayOffset": 0, "scope": "company"}},
        "paramSpec": [
            {"key": "dayOffset", "label": "Day offset", "type": "int",
             "help": "0 = orders due today, 1 = tomorrow"},
            {"key": "scope", "label": "Store scope", "type": "select",
             "options": ["all", "company", "franchise"],
             "help": "Mirrors the SSRS StoreGroup param (79=Corporate, 8=Franchise)"},
        ],
    },
    "period_catering_by_week": {
        "label": "Period Catering by Week",
        "replaces": "/Catering Sales and Menu/Period Catering by Week",
        "page": "/catering.html",
        "build": _build_period_catering_by_week,
        "defaults": {"daysOfWeek": "mon", "hour": 7, "minute": 45,
                     "params": {"weeks": 0, "htmlWeeks": 4, "basis": "subtotal"}},
        "paramSpec": [
            {"key": "weeks", "label": "Weeks in the spreadsheet", "type": "int",
             "help": "0 = whole fiscal year, matching the SSRS [Year] parameter"},
            {"key": "htmlWeeks", "label": "Weeks in the email body", "type": "int",
             "help": "Keeps the email readable; the .xlsx always has them all"},
            {"key": "basis", "label": "Dollar basis", "type": "select",
             "options": ["subtotal", "total"]},
            {"key": "fiscalYear", "label": "Fiscal year", "type": "int",
             "help": "Blank = the fiscal year in progress"},
        ],
    },
    "monkey_refunds_by_user": {
        "label": "Monkey — Refunds by User",
        "replaces": "/Catering Sales and Menu/Monkey - Refunds by User",
        "page": "/catering.html",
        "build": _build_refunds_by_user,
        "defaults": {"daysOfWeek": "mon", "hour": 7, "minute": 45,
                     "params": {"window": "last_full_week", "scope": "company"}},
        "paramSpec": [
            {"key": "window", "label": "Date window", "type": "select",
             "options": ["last_full_week", "last_n_days", "month_to_date",
                         "yesterday", "today"]},
            {"key": "days", "label": "Days (for last_n_days)", "type": "int"},
            {"key": "scope", "label": "Store scope", "type": "select",
             "options": ["all", "company", "franchise"],
             "help": "Mirrors the SSRS StoreGroup param (79=Corporate, 8=Franchise)"},
        ],
    },
    "monkey_sales_by_channel_weekday": {
        "label": "Monkey Sales by Channel by Weekday",
        "replaces": "/Monkey Media/Monkey Sales by Channel by Weekday",
        "page": "/catering.html",
        "build": _build_sales_by_channel_weekday,
        "defaults": {"daysOfWeek": "mon", "hour": 7, "minute": 45,
                     "params": {"window": "last_full_week", "groupBy": "system"}},
        "paramSpec": [
            {"key": "window", "label": "Date window", "type": "select",
             "options": ["last_full_week", "last_n_days", "month_to_date",
                         "yesterday", "today"]},
            {"key": "days", "label": "Days (for last_n_days)", "type": "int"},
            {"key": "groupBy", "label": "Group by", "type": "select",
             "options": ["system", "market", "store"]},
        ],
    },
}


def build_report(report_key: str, params: Dict[str, Any],
                 run_date: Optional[date] = None) -> RenderedReport:
    spec = REPORTS.get(report_key)
    if not spec:
        raise KeyError(f"Unknown report_key '{report_key}'")
    builder: Callable[[Dict[str, Any], date], RenderedReport] = spec["build"]
    return builder(params or {}, run_date or date.today())


def catalog() -> List[Dict[str, Any]]:
    """Report registry for the Admin UI (no callables)."""
    return [
        {"key": k, "label": v["label"], "replaces": v.get("replaces", ""),
         "page": v.get("page", ""), "defaults": v.get("defaults", {}),
         "paramSpec": v.get("paramSpec", [])}
        for k, v in REPORTS.items()
    ]
