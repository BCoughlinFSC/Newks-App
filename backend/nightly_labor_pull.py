"""
nightly_labor_pull.py — Toast labor ingestion: shifts (toast.dbo.hstShift)
AND the employee master (toast.dbo.Employees).

Fills two gaps left by Don's external pullers:
  • The PHP labor cron stopped writing hstShift after 2026-05-13.
  • The "API" employee puller (toast.dbo.Employees / EmployeeJobsByStore /
    EmployeeWagesByJob) stopped after its 2026-05-14 run, freezing the
    employee master chain-wide. New hires, rehires, and cross-store workers
    then had shifts but no Employees row, so the payroll export
    (sp_rpt_Company_Payroll_Export_TOAST) silently dropped them.

Shifts: pulls /labor/v1/timeEntries per restaurant (chunked at 30 days),
maps to hstShift, DELETE+INSERTs per (Store, date range), then triggers
sp_process_pulled_labor so tbl_LaborDataByDayPart catches up.

Employees: pulls /labor/v1/employees per restaurant (full current roster,
NOT date-ranged) and MERGEs into toast.dbo.Employees on GUID (never deletes,
so past-period payroll still sees people who've left). Runs every invocation
unless --skip-employees.

USAGE
-----
  python nightly_labor_pull.py                          # yesterday shifts + employee sync, all stores
  python nightly_labor_pull.py 2026-05-17               # one date shifts + employee sync
  python nightly_labor_pull.py --start-date 2026-05-14 --end-date 2026-05-17
                                                        # date range (backfill) + employee sync
  python nightly_labor_pull.py --days-back 3            # yesterday + 3 prior days + employee sync
  python nightly_labor_pull.py 2026-05-17 --store 1051  # one date, one store
  python nightly_labor_pull.py --employees-only         # ONLY refresh toast.dbo.Employees
                                                        # (no shift pull, no rollup) —
                                                        # use before payroll to land new hires
  python nightly_labor_pull.py --skip-employees ...     # shifts only, old behavior
  python nightly_labor_pull.py --start-date 2026-05-14 --end-date 2026-05-17 \
                                                       --skip-rollup
                                                        # backfill hstShift only (+ employee sync)

NOTES
-----
  • DELETE is scoped per (Store, DateOfBusiness in range) — other stores' rows
    for the same dates are untouched.
  • Skips entries with outDate=null (still clocked in).
  • sp_process_pulled_labor re-merges all of hstShift since 2025-03-24 every run
    (hard-coded inside the proc). That's wasteful but safe for backfill —
    once hstShift is populated, one rollup call picks up every gap day.
  • Safe to run alongside nightly_pull.py (sales) — different tables, no shared
    staging.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import httpx
import toast_puller_db as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nightly_labor_pull")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOAST_STANDARD_API_BASE_URL = os.getenv(
    "TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com"
)
CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")

# Default concurrency for parallel API fetches across stores. Unlike the
# sales puller, the labor write path has no shared staging table — each
# store's DELETE/INSERT is scoped by Store guid — so we can parallelize
# the whole pull_store_labor function safely.
DEFAULT_CONCURRENCY = int(os.getenv("TOAST_PULL_CONCURRENCY", "5"))


# ─── Toast auth ──────────────────────────────────────────────────────────────

_token_cache = {"token": None, "expires_at": 0.0}


async def get_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "TOAST_STANDARD_API_CLIENT_ID / _SECRET missing from .env"
        )

    resp = await client.post(
        f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login",
        json={
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Standard API auth failed ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    tok_obj = data.get("token") or {}
    token = tok_obj.get("accessToken") or tok_obj.get("token")
    expires_in = int(tok_obj.get("expiresIn", 3600))
    if not token:
        raise RuntimeError(f"No accessToken in auth response: {data}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in - 60
    return token


# ─── API field mapping ───────────────────────────────────────────────────────

def _parse_business_date(value) -> str | None:
    """Toast returns businessDate as int (YYYYMMDD) or sometimes a string."""
    if value is None:
        return None
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+0000").replace(".000+0000", "+0000")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def map_entry_to_shift(entry: dict, store_guid: str, store_num: int) -> dict | None:
    """Map a Toast /labor/v1/timeEntries entry to a toast.dbo.hstShift row.

    Returns None if the entry should be skipped (still clocked in, invalid, etc.).
    """
    if entry.get("outDate") is None:
        return None
    if entry.get("deleted"):
        return None

    biz_date = _parse_business_date(entry.get("businessDate"))
    if not biz_date:
        return None

    # ShiftID — prefer Toast's guid; synthesize one if absent (defensive)
    shift_id = entry.get("guid") or str(uuid.uuid4())

    job_ref = entry.get("jobReference") or {}
    emp_ref = entry.get("employeeReference") or {}

    reg_h = float(entry.get("regularHours") or 0)
    ot_h = float(entry.get("overtimeHours") or 0)
    wage = entry.get("hourlyWage")
    wage_f = float(wage) if wage is not None else None
    reg_dollars = reg_h * wage_f if wage_f is not None else 0.0
    ot_dollars = ot_h * wage_f * 1.5 if wage_f is not None else 0.0

    clock_in = _parse_iso(entry.get("inDate"))
    clock_out = _parse_iso(entry.get("outDate"))

    return {
        "ShiftID": str(shift_id)[:36],
        "Store": store_guid,
        "DateOfBusiness": biz_date,
        "Employee": (emp_ref.get("guid") or "")[:50],
        "JobCode": (job_ref.get("guid") or "")[:50],
        "ClockIn": clock_in,
        "ClockOut": clock_out,
        "RegularHours": reg_h,
        "OvertimeHOurs": ot_h,  # NOTE: column is spelled "OvertimeHOurs" in DB
        "HourlyWage": wage_f if wage_f is not None else 0.0,
        "RegularDollars": reg_dollars,
        "OTDollars": ot_dollars,
        "DeclaredCashTips": float(entry.get("declaredCashTips") or 0),
        "NonCashTips": float(entry.get("nonCashTips") or 0),
        # Deleted = 0 explicitly. Entries with API deleted=true are already
        # filtered out at the top of this function. sp_rpt_Company_Payroll_Export_TOAST
        # joins WHERE hs.Deleted = 0, so a NULL here silently drops the row.
        "Deleted": 0,
    }


# ─── Fetch ───────────────────────────────────────────────────────────────────

async def fetch_time_entries(
    client: httpx.AsyncClient,
    token: str,
    store_guid: str,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[list[dict], list[str]]:
    """GET /labor/v1/timeEntries for a single store across a date range.

    Toast caps the window at 30 days per request — we chunk if needed.
    Returns (entries, errors).
    """
    all_entries: list[dict] = []
    errors: list[str] = []
    chunk_start = start_dt
    url = f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/timeEntries"

    while chunk_start <= end_dt:
        chunk_end = min(chunk_start + timedelta(days=29), end_dt)
        params = {
            "startDate": chunk_start.strftime("%Y-%m-%dT00:00:00.000+0000"),
            "endDate": chunk_end.strftime("%Y-%m-%dT23:59:59.999+0000"),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Toast-Restaurant-External-ID": store_guid,
        }
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=60.0)
            if resp.status_code != 200:
                errors.append(
                    f"HTTP {resp.status_code} for {chunk_start.date()}–{chunk_end.date()}: "
                    f"{resp.text[:200]}"
                )
            else:
                entries = resp.json()
                if isinstance(entries, dict):
                    entries = entries.get("timeEntries", [])
                if isinstance(entries, list):
                    all_entries.extend(entries)
        except Exception as exc:
            errors.append(f"Exception for {chunk_start.date()}–{chunk_end.date()}: {exc}")
        chunk_start = chunk_end + timedelta(days=1)

    return all_entries, errors


# ─── DB writes ───────────────────────────────────────────────────────────────

INSERT_SHIFT_SQL = """
INSERT INTO toast.dbo.hstShift (
    ShiftID, Store, DateOfBusiness, Employee, JobCode,
    ClockIn, ClockOut,
    RegularHours, OvertimeHOurs, HourlyWage,
    RegularDollars, OTDollars,
    DeclaredCashTips, NonCashTips,
    Deleted
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def write_shifts(
    conn,
    store_guid: str,
    start_date: str,
    end_date: str,
    shifts: list[dict],
) -> int:
    """DELETE rows in (store, date range) then INSERT new rows. Returns insert count."""
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM toast.dbo.hstShift "
        "WHERE Store = ? AND DateOfBusiness BETWEEN ? AND ?",
        store_guid, start_date, end_date,
    )
    deleted = cur.rowcount
    inserted = 0
    for s in shifts:
        if not (start_date <= s["DateOfBusiness"] <= end_date):
            continue  # skip entries Toast returned outside our window
        cur.execute(
            INSERT_SHIFT_SQL,
            s["ShiftID"], s["Store"], s["DateOfBusiness"], s["Employee"], s["JobCode"],
            s["ClockIn"], s["ClockOut"],
            s["RegularHours"], s["OvertimeHOurs"], s["HourlyWage"],
            s["RegularDollars"], s["OTDollars"],
            s["DeclaredCashTips"], s["NonCashTips"],
            s["Deleted"],
        )
        inserted += 1
    conn.commit()
    log.debug("  DELETE %d / INSERT %d for store %s [%s..%s]",
              deleted, inserted, store_guid, start_date, end_date)
    return inserted


# ─── Per-store orchestration ─────────────────────────────────────────────────

async def pull_store_labor(
    client: httpx.AsyncClient,
    token: str,
    store_guid: str,
    store_num: int,
    store_name: str,
    start_date: str,
    end_date: str,
    dry_run: bool,
    debug_first: bool,
) -> dict:
    result = {
        "store_num": store_num,
        "store_name": store_name,
        "ok": False,
        "fetched": 0,
        "inserted": 0,
        "error": None,
    }
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    try:
        entries, errors = await fetch_time_entries(
            client, token, store_guid, start_dt, end_dt
        )
        result["fetched"] = len(entries)

        if errors and not entries:
            result["error"] = "; ".join(errors)[:300]
            log.error("  [%s] API failed: %s", store_num, errors[0])
            return result

        # One-time diagnostic dump on the first entry of the first store,
        # so an operator can sanity-check field names against the live response.
        if debug_first and entries:
            log.info("  [%s] Sample entry (for field-name verification):", store_num)
            log.info("\n%s", json.dumps(entries[0], indent=2, default=str))

        shifts: list[dict] = []
        skipped = 0
        for e in entries:
            shift = map_entry_to_shift(e, store_guid, store_num)
            if shift is None:
                skipped += 1
            else:
                shifts.append(shift)

        if dry_run:
            log.info("  [%s] %s — fetched=%d, mapped=%d, skipped=%d (DRY RUN)",
                     store_num, store_name, len(entries), len(shifts), skipped)
            result["ok"] = True
            return result

        conn = db.get_toast_connection()
        try:
            inserted = write_shifts(conn, store_guid, start_date, end_date, shifts)
        finally:
            conn.close()
        result["inserted"] = inserted
        result["ok"] = True
        log.info("  [%s] %s — fetched=%d, inserted=%d, skipped=%d",
                 store_num, store_name, len(entries), inserted, skipped)
    except Exception as exc:
        result["error"] = str(exc)
        log.error("  [%s] %s — Unexpected error: %s",
                  store_num, store_name, exc, exc_info=True)
    return result


# ─── Employee master sync ────────────────────────────────────────────────────
# Don's external "API" employee puller (which fed toast.dbo.Employees,
# EmployeeJobsByStore, EmployeeWagesByJob) stopped after its 2026-05-14 run,
# freezing the employee master chain-wide. Symptom: new hires + rehires +
# cross-store workers have shifts in hstShift but no Employees row, so the
# payroll export (sp_rpt_Company_Payroll_Export_TOAST) silently drops them.
#
# This pulls each live store's roster from Toast Standard API
# /labor/v1/employees and MERGEs into toast.dbo.Employees. It is NOT
# date-ranged — it's a full current-roster snapshot — so it runs once per
# invocation regardless of the labor date range.


async def fetch_employees(
    client: httpx.AsyncClient,
    token: str,
    store_guid: str,
) -> tuple[list[dict], list[str]]:
    """GET /labor/v1/employees for one restaurant. Handles page-token
    pagination (Toast returns the next-page token in a response header).
    Returns (employees, errors)."""
    url = f"{TOAST_STANDARD_API_BASE_URL}/labor/v1/employees"
    all_emps: list[dict] = []
    errors: list[str] = []
    page_token = None
    for _ in range(50):  # hard cap on pages — defensive against a paging loop
        headers = {
            "Authorization": f"Bearer {token}",
            "Toast-Restaurant-External-ID": store_guid,
        }
        params = {}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=60.0)
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
                break
            body = resp.json()
            emps = body.get("employees", body) if isinstance(body, dict) else body
            if isinstance(emps, list):
                all_emps.extend(emps)
            # Toast paginates labor endpoints via this response header.
            page_token = resp.headers.get("Toast-Next-Page-Token")
            if not page_token:
                break
        except Exception as exc:
            errors.append(f"Exception: {exc}")
            break
    return all_emps, errors


def map_employee(emp: dict, store_guid: str, store_num: int) -> dict | None:
    """Map a Toast /labor/v1/employees object to a toast.dbo.Employees row.

    Field names are defensive (Toast has used a few variants); run with
    --debug-first to dump a live employee object and confirm against these.
    """
    guid = emp.get("guid")
    if not guid:
        return None

    deleted_bool = bool(emp.get("deleted"))
    phone = emp.get("phoneNumber") or emp.get("phone") or ""

    return {
        "GUID":               str(guid)[:50],
        "StoreGUID":          store_guid,
        "StoreID":            store_num,
        "First":              emp.get("firstName") or emp.get("first") or "",
        "Last":               emp.get("lastName") or emp.get("last") or "",
        "ChosenName":         emp.get("chosenName") or "",
        "Email":              emp.get("email") or "",
        "Phone":              phone,
        "ExternalId":         emp.get("externalId") or "",
        # ExternalEmployeeId = the ADP File #. Set per-employee in Toast; blank
        # here means HR hasn't entered a payroll ID for them (e.g. Nichols).
        "ExternalEmployeeId": emp.get("externalEmployeeId") or "",
        "CreatedDate":        _parse_iso(emp.get("createdDate")),
        "Deleted":            "1" if deleted_bool else "0",
        "DeletedDate":        _parse_iso(emp.get("deletedDate")),
        "ModifiedDate":       _parse_iso(emp.get("modifiedDate")),
        "v2EmployeeGuid":     str(emp.get("v2EmployeeGuid") or "")[:50],
    }


async def pull_store_employees(
    client: httpx.AsyncClient,
    token: str,
    store_guid: str,
    store_num: int,
    store_name: str,
    dry_run: bool,
    debug_first: bool,
) -> dict:
    result = {"store_num": store_num, "store_name": store_name, "ok": False,
              "fetched": 0, "inserted": 0, "updated": 0, "error": None}
    try:
        emps, errors = await fetch_employees(client, token, store_guid)
        result["fetched"] = len(emps)
        if errors and not emps:
            result["error"] = "; ".join(errors)[:300]
            log.error("  [%s] employees API failed: %s", store_num, errors[0])
            return result

        if debug_first and emps:
            log.info("  [%s] Sample employee (for field-name verification):", store_num)
            log.info("\n%s", json.dumps(emps[0], indent=2, default=str))

        rows = [m for m in (map_employee(e, store_guid, store_num) for e in emps) if m]

        if dry_run:
            blank_file = sum(1 for r in rows if not r["ExternalEmployeeId"])
            log.info("  [%s] %s — employees fetched=%d, mapped=%d, blank File#=%d (DRY RUN)",
                     store_num, store_name, len(emps), len(rows), blank_file)
            result["ok"] = True
            return result

        conn = db.get_toast_connection()
        try:
            inserted, updated = db.upsert_employees(conn, rows)
        finally:
            conn.close()
        result["inserted"], result["updated"], result["ok"] = inserted, updated, True
        blank_file = sum(1 for r in rows if not r["ExternalEmployeeId"])
        log.info("  [%s] %s — employees: fetched=%d, new=%d, updated=%d, blank File#=%d",
                 store_num, store_name, len(emps), inserted, updated, blank_file)
    except Exception as exc:
        result["error"] = str(exc)
        log.error("  [%s] %s — employee sync error: %s", store_num, store_name, exc, exc_info=True)
    return result


async def run_employee_sync(
    client: httpx.AsyncClient,
    token: str,
    stores: list[dict],
    concurrency: int,
    dry_run: bool,
    debug_first: bool,
) -> list[dict]:
    """Refresh toast.dbo.Employees for every store in scope (parallel)."""
    log.info("-" * 70)
    log.info("EMPLOYEE MASTER SYNC — %d store(s)", len(stores))
    sem = asyncio.Semaphore(concurrency)

    async def one(idx: int, s: dict) -> dict:
        async with sem:
            return await pull_store_employees(
                client, token,
                store_guid=str(s["guid"]),
                store_num=int(s["store_num"]),
                store_name=s.get("name", f"Store {s['store_num']}"),
                dry_run=dry_run,
                debug_first=(debug_first and idx == 0),
            )

    results = await asyncio.gather(
        *[one(i, s) for i, s in enumerate(stores)], return_exceptions=True
    )
    out: list[dict] = []
    tot_new = tot_upd = 0
    for s, r in zip(stores, results):
        if isinstance(r, Exception):
            out.append({"store_num": int(s["store_num"]), "ok": False,
                        "error": f"task raised {type(r).__name__}: {r}",
                        "inserted": 0, "updated": 0})
            continue
        out.append(r)
        tot_new += r.get("inserted", 0)
        tot_upd += r.get("updated", 0)
    log.info("Employee sync totals: new=%d, updated=%d, failed_stores=%d",
             tot_new, tot_upd, sum(1 for r in out if not r.get("ok")))
    return out


# ─── Main ────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Pull Toast labor (shift) data into toast.dbo.hstShift.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("target_date", nargs="?",
                    help="Business date YYYY-MM-DD (default: yesterday). "
                         "Ignored if --start-date/--end-date or --days-back provided.")
    ap.add_argument("--start-date", help="Range start (YYYY-MM-DD) for backfill.")
    ap.add_argument("--end-date", help="Range end inclusive (YYYY-MM-DD).")
    ap.add_argument("--days-back", type=int, default=0, metavar="N",
                    help="Shortcut for --start-date/--end-date: repoll yesterday "
                         "and N prior days (N+1 distinct dates total). Catches "
                         "late-edited shifts. Example: --days-back 3 pulls "
                         "yesterday + 3 prior = 4 dates.")
    ap.add_argument("--store", "--stores", dest="store",
                    help="Comma-separated store_num filter.")
    ap.add_argument("--skip-rollup", action="store_true",
                    help="Skip sp_process_pulled_labor at the end.")
    ap.add_argument("--skip-employees", action="store_true",
                    help="Skip the employee-master sync (toast.dbo.Employees). "
                         "By default it runs every invocation alongside the labor pull.")
    ap.add_argument("--employees-only", action="store_true",
                    help="Run ONLY the employee-master sync — no shift pull, no rollup. "
                         "Use for an immediate roster refresh (e.g. before payroll).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and map but do not write to SQL.")
    ap.add_argument("--debug-first", action="store_true",
                    help="Dump first API entry's full JSON for field verification.")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
                    help=f"Max concurrent Toast API fetches across stores "
                         f"(default {DEFAULT_CONCURRENCY}, or $TOAST_PULL_CONCURRENCY). "
                         f"Set to 1 for fully sequential.")
    args = ap.parse_args(argv)

    # Guard against contradictory inputs. --days-back is mutually exclusive
    # with both the positional date and explicit start/end.
    if args.days_back and (args.target_date or args.start_date or args.end_date):
        ap.error("--days-back is mutually exclusive with target_date / "
                 "--start-date / --end-date.")
    if args.days_back < 0:
        ap.error("--days-back must be 0 or greater.")
    if args.concurrency < 1:
        ap.error("--concurrency must be 1 or greater.")
    if args.employees_only and args.skip_employees:
        ap.error("--employees-only and --skip-employees are contradictory.")
    return args


async def main():
    overall_start = time.time()
    args = _parse_args(sys.argv[1:])

    # Resolve date range. Precedence:
    #   --days-back N  → yesterday + N prior days (N+1 distinct dates)
    #   --start-date / --end-date  → explicit range (must come together)
    #   positional target_date     → single date
    #   nothing                    → yesterday only (default cron behavior)
    yesterday = date.today() - timedelta(days=1)
    if args.days_back:
        start_date = (yesterday - timedelta(days=args.days_back)).strftime("%Y-%m-%d")
        end_date   = yesterday.strftime("%Y-%m-%d")
    elif args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            print("ERROR: --start-date and --end-date must be used together.")
            sys.exit(1)
        start_date = args.start_date
        end_date = args.end_date
    elif args.target_date:
        start_date = end_date = args.target_date
    else:
        start_date = end_date = yesterday.strftime("%Y-%m-%d")

    for d in (start_date, end_date):
        try:
            date.fromisoformat(d)
        except ValueError:
            print(f"ERROR: invalid date '{d}'")
            sys.exit(1)
    if start_date > end_date:
        print(f"ERROR: start-date {start_date} > end-date {end_date}")
        sys.exit(1)

    store_filter: set[int] = set()
    if args.store:
        for token in args.store.split(","):
            token = token.strip()
            if token:
                try:
                    store_filter.add(int(token))
                except ValueError:
                    print(f"ERROR: --store value '{token}' is not an integer")
                    sys.exit(1)

    log.info("=" * 70)
    log.info("LABOR PULL — %s..%s%s",
             start_date, end_date,
             "" if not store_filter else f" — STORES {','.join(str(s) for s in sorted(store_filter))}")
    if args.dry_run:
        log.info("  --dry-run: no DB writes")
    if args.skip_rollup:
        log.info("  --skip-rollup: sp_process_pulled_labor will NOT be called")
    if args.concurrency > 1:
        log.info("  Concurrency: %d parallel API fetches", args.concurrency)
    log.info("=" * 70)

    conn = db.get_toast_connection()
    try:
        raw_stores = db.get_live_toast_stores(conn)
    finally:
        conn.close()
    stores = [s for s in raw_stores if s.get("guid") and s.get("store_num")]
    if store_filter:
        stores = [s for s in stores if int(s["store_num"]) in store_filter]
        missing = store_filter - {int(s["store_num"]) for s in stores}
        if missing:
            log.warning("--store values not found in vw_live_toast_stores: %s",
                        ",".join(str(m) for m in sorted(missing)))
        if not stores:
            log.error("No matching stores after --store filter — aborting.")
            sys.exit(1)
    log.info("Stores in scope: %d", len(stores))

    failures: list[dict] = []
    totals = {"fetched": 0, "inserted": 0}

    # Per-store work runs in parallel — labor has no shared staging table,
    # each store's DELETE+INSERT is scoped by Store guid + date range so
    # concurrent writes don't contend with each other.
    sem = asyncio.Semaphore(args.concurrency)

    async def pull_with_sem(idx: int, s: dict) -> dict:
        async with sem:
            return await pull_store_labor(
                client,
                token=token,
                store_guid=str(s["guid"]),
                store_num=int(s["store_num"]),
                store_name=s.get("name", f"Store {s['store_num']}"),
                start_date=start_date,
                end_date=end_date,
                dry_run=args.dry_run,
                # Only the first store dumps the debug payload (and only if
                # explicitly requested). With parallel execution, "first"
                # means lowest index — not first-to-finish.
                debug_first=(args.debug_first and idx == 0),
            )

    emp_failures: list[dict] = []
    emp_totals = {"new": 0, "updated": 0}

    async with httpx.AsyncClient(timeout=120.0) as client:
        token = await get_token(client)
        log.info("Standard API auth: OK")

        # ── Labor (shift) pull — skipped entirely in --employees-only mode ──
        if not args.employees_only:
            pull_tasks = [pull_with_sem(idx, s) for idx, s in enumerate(stores)]
            results = await asyncio.gather(*pull_tasks, return_exceptions=True)

            for s, r in zip(stores, results):
                if isinstance(r, Exception):
                    failures.append({
                        "store_num":  int(s["store_num"]),
                        "store_name": s.get("name", f"Store {s['store_num']}"),
                        "ok":         False,
                        "error":      f"task raised {type(r).__name__}: {r}",
                        "fetched":    0,
                        "inserted":   0,
                    })
                    continue
                if not r["ok"]:
                    failures.append(r)
                totals["fetched"]  += r["fetched"]
                totals["inserted"] += r["inserted"]
        else:
            log.info("--employees-only: skipping labor (shift) pull.")

        # ── Employee-master sync — runs unless explicitly skipped ──
        if not args.skip_employees:
            emp_results = await run_employee_sync(
                client, token, stores,
                concurrency=args.concurrency,
                dry_run=args.dry_run,
                debug_first=args.debug_first,
            )
            for r in emp_results:
                if not r.get("ok"):
                    emp_failures.append(r)
                emp_totals["new"]     += r.get("inserted", 0)
                emp_totals["updated"] += r.get("updated", 0)
        else:
            log.info("--skip-employees: employee-master sync NOT run.")

    # Rollup — only relevant to the labor pull; never in --employees-only mode
    rollup_ok = None
    if args.employees_only or args.skip_rollup or args.dry_run:
        log.info("-" * 70)
        log.info("sp_process_pulled_labor SKIPPED.")
    else:
        log.info("-" * 70)
        log.info("Running toast.dbo.sp_process_pulled_labor…")
        rollup_ok = False
        try:
            conn = db.get_toast_connection()
            try:
                conn.execute("EXEC toast.dbo.sp_process_pulled_labor")
                conn.commit()
            finally:
                conn.close()
            rollup_ok = True
            log.info("Rollup complete (tbl_LaborDataByDayPart refreshed).")
        except Exception as exc:
            log.error("Rollup FAILED: %s", exc)

    dur = time.time() - overall_start
    log.info("=" * 70)
    log.info("PULL COMPLETE")
    if not args.employees_only:
        log.info("  Range       : %s..%s", start_date, end_date)
        log.info("  Labor       : %d stores / %d failed — fetched=%d, inserted=%d",
                 len(stores), len(failures), totals["fetched"], totals["inserted"])
        log.info("  Rollup      : %s", "SKIPPED" if rollup_ok is None
                 else ("OK" if rollup_ok else "FAILED"))
    if not args.skip_employees:
        log.info("  Employees   : %d stores / %d failed — new=%d, updated=%d",
                 len(stores), len(emp_failures), emp_totals["new"], emp_totals["updated"])
    log.info("  Runtime     : %.1f min", dur / 60)
    log.info("=" * 70)

    if failures:
        log.warning("FAILED LABOR STORES (%d):", len(failures))
        for r in failures:
            log.warning("  Store %s — %s", r["store_num"], r["error"])
    if emp_failures:
        log.warning("FAILED EMPLOYEE-SYNC STORES (%d):", len(emp_failures))
        for r in emp_failures:
            log.warning("  Store %s — %s", r["store_num"], r.get("error"))

    if failures or emp_failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
