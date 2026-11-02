"""Read-only Paytronix export followed by the app's existing payroll builder."""
import base64
import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse


REPORT_TITLE = "Stored Value Sales and Added Value Detail Report"
REQUIRED_COLUMNS = {"Card Number", "Dollars Added", "Date", "Store Name"}


def report_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Report dates must use YYYY-MM-DD.") from exc


def find_paytronix_surface(context):
    """Return the owner Page and Paytronix Page/Frame, including Access Evo."""
    candidates = []
    for page in context.pages:
        for surface in [page] + list(page.frames)[1:]:
            host = (urlparse(surface.url).hostname or "").lower()
            if host == "www.pxsweb.com" or host == "pxsweb.com" or host.endswith(".px.us.access-evo.com"):
                # Login pages are not a usable merchant session.
                if surface.locator('button:has-text("Report Center")').count():
                    candidates.append((page, surface))
    if len(candidates) != 1:
        raise RuntimeError("Open exactly one signed-in Paytronix merchant session in debug Chrome (standalone or Access Evo).")
    return candidates[0]


def export_report(context, destination, start, end, store="1003"):
    """Use the report UI; this path never visits any wallet adjustment form."""
    page, surface = find_paytronix_surface(context)
    page.bring_to_front()
    surface.get_by_role("button", name="Report Center", exact=True).click()
    surface.get_by_role("menuitem", name="Run Reports", exact=True).click()
    report = surface.locator('[data-slot="item"]').filter(has_text="Sales and Added Value")
    report.locator('button[data-slot="button"]').filter(has_text="Detail").click()
    surface.locator("#reportinputform").wait_for(state="visible", timeout=45000)
    if REPORT_TITLE not in surface.inner_text("body"):
        raise RuntimeError("Paytronix did not open the Stored Value detail report.")
    for prefix, day in (("start", start), ("stop", end)):
        for part, value in (("month", day.month), ("day", day.day), ("year", day.year)):
            surface.locator(f'input[name="date_{prefix}_{part}"]').fill(str(value))
    surface.locator('input[name="store"]').fill(store)
    surface.locator('select[name="output_type"]').select_option(label="CSV")
    surface.get_by_role("button", name="Submit", exact=True).click()
    link = surface.get_by_role("link", name="Download Report", exact=True)
    link.wait_for(state="visible", timeout=180000)
    with page.expect_download(timeout=180000) as pending:
        link.click()
    download = pending.value
    # Do not trust a remote filename as a filesystem path.
    target = Path(destination) / Path(download.suggested_filename.replace("\\", "/")).name
    download.save_as(str(target))
    return target


def filter_report(raw, cards, excluded, start, end, expected=None):
    """Keep roster loads only and reject stale exports missing known reloads.

    The unfiltered CSV is retained separately for audit. Payroll must never
    accidentally include ordinary gift-card purchases at Flowood.
    """
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    header_index = next((i for i, row in enumerate(rows[:5])
                         if REQUIRED_COLUMNS.issubset({v.strip() for v in row})), None)
    if header_index is None:
        raise ValueError("The export is not a Stored Value detail CSV (required columns missing).")
    header = [v.strip() for v in rows[header_index]]
    indexes = {name: header.index(name) for name in REQUIRED_COLUMNS}
    allowed = {str(c["card_number"]).strip() for c in cards} - set(excluded)
    selected, totals = [], {}
    for row in rows[header_index + 1:]:
        if not row or not any(v.strip() for v in row):
            continue
        if len(row) != len(header):
            raise ValueError("The Paytronix export has a malformed row; no payroll files were built.")
        card = row[indexes["Card Number"]].strip()
        if card not in allowed:
            continue
        value = row[indexes["Dollars Added"]].strip().replace("$", "").replace(",", "")
        try:
            amount = Decimal(value or "0")
        except InvalidOperation as exc:
            raise ValueError("Invalid Dollars Added amount in the Paytronix export.") from exc
        if not amount.is_finite():
            raise ValueError("Invalid Dollars Added amount in the Paytronix export.")
        text = row[indexes["Date"]].strip()
        parsed = None
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None or not start <= parsed <= end:
            raise ValueError("A roster transaction has an invalid or out-of-range date.")
        if row[indexes["Store Name"]].strip() != "Flowood MS":
            raise ValueError("The export contains roster loads outside Flowood MS.")
        selected.append(row)
        totals[card] = totals.get(card, Decimal("0")) + amount
    missing = [card for card, amount in (expected or {}).items()
               if totals.get(card, Decimal("0")) < Decimal(str(amount))]
    if missing:
        raise ValueError(
            "Paytronix report has not caught up with this run's loads. Reports can lag up to 45 minutes. "
            "Retry REPORT ONLY later; do not reload the cards. Missing card endings: "
            + ", ".join(card[-4:] for card in missing))
    if not selected:
        raise ValueError("No eligible meal-card transactions in this export. Retry REPORT ONLY after Paytronix catches up.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(selected)
    return output.getvalue().encode("utf-8-sig")


def build_workbooks(client, filtered_path, destination):
    """Use the same authenticated endpoint as the Meal Cards upload button."""
    response = client.post("/api/mealcard/payroll-report", json={
        "filename": filtered_path.name,
        "content_b64": base64.b64encode(filtered_path.read_bytes()).decode("ascii"),
    }, timeout=180.0)
    response.raise_for_status()
    result = response.json()
    files = result.get("files", [])
    if len(files) != 2:
        raise ValueError("The app did not return both payroll workbooks.")
    decoded = []
    for item in files:
        name = item["filename"]
        if name != Path(name).name or "/" in name or "\\" in name or not name.endswith(".xlsx"):
            raise ValueError("The app returned an invalid workbook filename.")
        data = base64.b64decode(item["data"], validate=True)
        if not data.startswith(b"PK"):
            raise ValueError("The app returned an invalid workbook.")
        decoded.append((Path(destination) / name, data))
    for path, data in decoded:
        path.write_bytes(data)
    (Path(destination) / "report_summary.json").write_text(
        json.dumps({"stats": result["stats"], "unmapped": result.get("unmapped", [])}, indent=2),
        encoding="utf-8")
    return result, [path for path, _ in decoded]


def run_reports(context, app, cards, excluded, directory, start, end, expected=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Exporting Paytronix report: {start} through {end}, Flowood MS")
    source = export_report(context, directory, start, end)
    print(f"Original Paytronix CSV saved: {source}")
    filtered = filter_report(source.read_bytes(), cards, excluded, start, end, expected)
    filtered_path = directory / f"MealCardLoads-{end:%Y%m%d}-filtered.csv"
    filtered_path.write_bytes(filtered)
    result, paths = build_workbooks(app.client, filtered_path, directory)
    for path in paths:
        print(f"Workbook saved: {path}")
    print(f"Report total added: ${result['stats']['total_added']:.2f}")
    print(f"Payroll total: ${result['stats']['payroll_total']:.2f}")
    if result.get("unmapped"):
        print("ATTENTION: Some cards are excluded from payroll or missing employee mappings. Review the Unmapped tab.")
    return result
