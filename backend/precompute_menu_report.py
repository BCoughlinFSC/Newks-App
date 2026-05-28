"""
Precompute menu-report cache entries.

Runs as a CLI (`python precompute_menu_report.py`) or via the
/api/admin/precompute-menu endpoint (which imports `run_default_shapes`).

The job posts each shape to /api/reports/run with the X-Force-Refresh
header set so the live endpoint bypasses its own cache and actually does
the work, then writes the fresh result back via cache_set. Subsequent
user requests with the matching shape get a cache HIT and return in
milliseconds instead of running the ~1M-row pivot.

Default shapes cover the most common menu-report queries:
  - last 7 / 30 / 90 days
  - all stores (empty restaurantIds)
  - the canonical 15 Consolidated tags (matches what legacy.html sends)
  - YoY enabled (fiscal mode) with includeZeroSales=False

To add or change shapes, edit DEFAULT_SHAPES below and redeploy. The
admin UI button triggers the same shapes via the FastAPI endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx


from precompute_cache import CONSOLIDATED_TAGS_SUPERSET

logger = logging.getLogger(__name__)

# Single source of truth for the Consolidated tag list lives in
# precompute_cache (the cache layer needs it to do superset-fallback
# lookups). Sort for stable cache-key bytes and stable log output.
CONSOLIDATED_TAGS = sorted(CONSOLIDATED_TAGS_SUPERSET)


def _shape(label: str, **overrides: Any) -> Dict[str, Any]:
    """Construct a ReportConfig dict with sensible menu-report defaults."""
    base = {
        "reportType":       "menu",
        "dateRangeType":    "last_7_days",
        "customStartDate":  None,
        "customEndDate":    None,
        "restaurantIds":    [],
        "groupBy":          [],
        "itemTags":         CONSOLIDATED_TAGS,
        "includeZeroSales": False,
        "yoyEnabled":       True,
        "yoyMode":          "fiscal",
        "alohaDate":        None,
        "orderModeFilter":  None,
        "debug":            False,
    }
    base.update(overrides)
    return {"label": label, "config": base}


DEFAULT_SHAPES: List[Dict[str, Any]] = [
    _shape("last_7_days · YoY fiscal · all stores · consolidated tags",
           dateRangeType="last_7_days"),
    _shape("last_30_days · YoY fiscal · all stores · consolidated tags",
           dateRangeType="last_30_days"),
    # NOTE: last_90_days was removed from defaults — it produces ~3M modifier
    # rows and takes ~45 minutes per run, which isn't useful as a "precompute"
    # and risks the same OOM that motivated this work. Add back here only if
    # users actually request 90-day windows; you'd also need to bump the
    # admin endpoint's default timeout to ~3600s.
]


# ──────────────────────────────────────────────────────────────────────────────
# Runtime shape builder — used by the APScheduler nightly job
#
# Unlike DEFAULT_SHAPES (which is a compile-time constant for the CLI), the
# nightly job needs shapes that mirror the four most common Menu Performance
# slices an analyst opens in the morning:
#
#   bases:  last_7_days · last_30_days · current_fiscal_period_MTD
#   groups: all_stores  · Company · Franchise · Comping
#
# build_nightly_shapes() composes these into up to 12 shapes by reading the
# live ownership + comp-set data via loopback HTTP (no main.py import → no
# circular dep). Groups with no active stores are skipped; the fiscal-MTD
# base is skipped if the fiscal calendar hasn't been imported yet.
#
# This is also the natural hook for future email-subscription jobs: a
# subscription declares one of these shapes, the morning email task reads
# the matching cache entry, builds the Excel, and sends. No second pivot.
# ──────────────────────────────────────────────────────────────────────────────

async def _discover_store_groups(base_url: str) -> Dict[str, List[str]]:
    """
    Build {group_name: [restaurant_guids]} for the four canonical groups.
    Each list is restricted to currently-active locations (matches what the
    frontend's location picker offers, so cache keys line up).
    """
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        active_resp = await client.get(f"{base}/locations")
        active_resp.raise_for_status()
        active_set = {loc["restaurantGuid"] for loc in active_resp.json()}

        own_resp = await client.get(f"{base}/api/location-ownership")
        own_resp.raise_for_status()
        by_guid: Dict[str, str] = (own_resp.json() or {}).get("byGuid", {}) or {}

        comp_resp = await client.get(f"{base}/api/locations/comping")
        comp_resp.raise_for_status()
        comping_guids: List[str] = (comp_resp.json() or {}).get("guids", []) or []

    company   = sorted(g for g, t in by_guid.items() if t == "Company"   and g in active_set)
    franchise = sorted(g for g, t in by_guid.items() if t == "Franchise" and g in active_set)
    comping   = sorted(g for g in comping_guids if g in active_set)
    return {
        # "all_stores" uses an empty list → backend resolves it to every active
        # location at call time, which is what the frontend sends when "Select
        # All Locations" is checked. Matches that cache key.
        "all_stores": [],
        "Company":    company,
        "Franchise":  franchise,
        "Comping":    comping,
    }


def _current_fiscal_period_range(today: date) -> Optional[Tuple[date, date]]:
    """
    Look up `today`'s fiscal year + period in the aloha_sales_db
    fiscal_calendar table, then return (period_start_date, yesterday). The
    "MTD" range ends on yesterday so the precompute matches the report a
    user would run in the morning (Toast has fully settled yesterday's data
    by 5 AM).

    Returns None if the fiscal calendar table is missing, empty, has no row
    for `today`, or yesterday is before the period start.
    """
    try:
        # Lazy import — aloha_sales_db only needed for this one optional path,
        # and importing it eagerly would pull SQLite init into every CLI call.
        import aloha_sales_db as _adb  # noqa: WPS433
    except Exception as exc:
        logger.warning("fiscal_calendar unavailable (import failed): %s", exc)
        return None

    try:
        if not _adb.fiscal_calendar_available():
            return None
        with _adb.get_connection() as conn:
            row = conn.execute(
                "SELECT fiscal_year, fiscal_period FROM fiscal_calendar WHERE date = ?",
                (today.isoformat(),),
            ).fetchone()
            if not row:
                return None
            fy, fp = row
            start_row = conn.execute(
                "SELECT MIN(date) FROM fiscal_calendar"
                " WHERE fiscal_year = ? AND fiscal_period = ?",
                (fy, fp),
            ).fetchone()
        if not start_row or not start_row[0]:
            return None
        period_start = date.fromisoformat(start_row[0])
    except Exception as exc:
        logger.warning("fiscal_calendar query failed: %s", exc)
        return None

    end_date = today - timedelta(days=1)
    if end_date < period_start:
        # First day of the period — no completed-day window to precompute yet.
        return None
    return (period_start, end_date)


async def build_nightly_shapes(
    base_url: str,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Compose the runtime list of Menu Performance shapes for the nightly job.

    Returns up to 12 shapes (3 date bases × 4 store groups). Skips shapes
    whose group has no active stores or whose base date range can't be
    resolved (fiscal-MTD if the calendar isn't loaded).

    Each shape's `config` matches the exact normalized cache key the
    frontend produces when a user opens that view — so the morning
    /api/reports/run hits a fresh disk cache.
    """
    today = today or date.today()

    try:
        groups = await _discover_store_groups(base_url)
    except Exception as exc:
        logger.error("Could not discover store groups: %s", exc)
        # Still return all_stores variants — they don't depend on grouping.
        groups = {"all_stores": []}

    bases: List[Dict[str, Any]] = [
        {"label": "last_7_days",  "dateRangeType": "last_7_days"},
        {"label": "last_30_days", "dateRangeType": "last_30_days"},
    ]
    fp_range = _current_fiscal_period_range(today)
    if fp_range is not None:
        fp_start, fp_end = fp_range
        bases.append({
            "label":           f"fiscal_period_MTD ({fp_start.isoformat()}..{fp_end.isoformat()})",
            "dateRangeType":   "custom",
            "customStartDate": fp_start.isoformat(),
            "customEndDate":   fp_end.isoformat(),
        })
    else:
        logger.info("Nightly precompute: skipping fiscal_period_MTD (calendar not available for today)")

    shapes: List[Dict[str, Any]] = []
    for base in bases:
        base_label = base.pop("label")
        for group_name, guids in groups.items():
            # all_stores uses [] (the frontend's "Select All" default); other
            # groups MUST have at least one GUID to be meaningful.
            if group_name != "all_stores" and not guids:
                continue
            shape_label = f"{base_label} · {group_name}"
            shapes.append(_shape(
                shape_label,
                **base,
                restaurantIds=list(guids),
            ))

    return shapes


# ──────────────────────────────────────────────────────────────────────────────
# Library entry point used by /api/admin/precompute-menu
# ──────────────────────────────────────────────────────────────────────────────

async def run_default_shapes(
    base_url: str,
    auth_header: Optional[str] = None,
    *,
    shapes: Optional[List[Dict[str, Any]]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    request_timeout_s: float = 1800.0,
) -> Dict[str, Any]:
    """
    POST each shape to /api/reports/run with X-Force-Refresh: 1.

    `auth_header` is optional — /api/reports/run is currently unauthenticated.
    Pass through the caller's bearer token if that ever changes.

    `on_progress` (if supplied) is called with a dict after each shape
    completes — used by the admin endpoint to update its live status.

    Returns a summary dict with per-shape status and timings.
    """
    shapes = shapes if shapes is not None else DEFAULT_SHAPES
    results: List[Dict[str, Any]] = []
    started_at = time.time()

    headers: Dict[str, str] = {
        "X-Force-Refresh": "1",
        "Content-Type":   "application/json",
    }
    if auth_header:
        headers["Authorization"] = auth_header

    # Long timeout — the whole point is that these requests take a long time;
    # we don't want the precompute job to give up before the work finishes.
    async with httpx.AsyncClient(timeout=request_timeout_s) as client:
        for idx, shape in enumerate(shapes):
            label = shape.get("label") or f"shape #{idx + 1}"
            cfg = shape["config"]
            t0 = time.time()
            entry: Dict[str, Any] = {
                "index":      idx,
                "total":      len(shapes),
                "label":      label,
                "started_at": t0,
            }
            if on_progress:
                on_progress({**entry, "status": "running"})
            try:
                resp = await client.post(f"{base_url.rstrip('/')}/api/reports/run", json=cfg, headers=headers)
                duration_s = time.time() - t0
                cache_marker = resp.headers.get("X-Menu-Cache", "")
                cache_key_hdr = resp.headers.get("X-Menu-Cache-Key", "")
                if resp.status_code == 200:
                    entry.update({
                        "status":      "ok",
                        "http_status": 200,
                        "duration_s":  round(duration_s, 2),
                        "cache":       cache_marker,
                        "cache_key":   cache_key_hdr,
                    })
                else:
                    entry.update({
                        "status":      "error",
                        "http_status": resp.status_code,
                        "duration_s":  round(duration_s, 2),
                        "error":       (resp.text or "")[:400],
                    })
            except Exception as exc:
                # httpx TimeoutException has an empty str(); fall back to
                # repr() / class name so the UI shows something actionable.
                err_msg = str(exc) or repr(exc) or exc.__class__.__name__
                entry.update({
                    "status":      "error",
                    "duration_s":  round(time.time() - t0, 2),
                    "error":       err_msg,
                })
            results.append(entry)
            if on_progress:
                on_progress(entry)

    return {
        "started_at":   started_at,
        "completed_at": time.time(),
        "total_shapes": len(shapes),
        "ok_count":     sum(1 for r in results if r.get("status") == "ok"),
        "error_count":  sum(1 for r in results if r.get("status") != "ok"),
        "shapes":       results,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Warm the menu-report precompute cache.")
    p.add_argument(
        "--base-url",
        default=os.environ.get("PRECOMPUTE_BASE_URL", "http://127.0.0.1:8000"),
        help="Base URL of the running FastAPI app (default: http://127.0.0.1:8000)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("PRECOMPUTE_ADMIN_TOKEN"),
        help="Optional admin bearer token (read PRECOMPUTE_ADMIN_TOKEN env). "
             "/api/reports/run is currently unauthenticated so this is usually unnecessary.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("PRECOMPUTE_TIMEOUT_S", "600")),
        help="Per-shape HTTP timeout in seconds (default: 600 = 10min)",
    )
    return p.parse_args(argv)


def _cli_main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    def _print_progress(entry: Dict[str, Any]) -> None:
        status = entry.get("status", "?")
        label = entry.get("label", "")
        idx = entry.get("index", 0) + 1
        total = entry.get("total", 0)
        if status == "running":
            print(f"  [{idx}/{total}] running: {label}", flush=True)
        elif status == "ok":
            d = entry.get("duration_s", 0)
            cache = entry.get("cache", "")
            print(f"  [{idx}/{total}] ok ({d}s, cache={cache}): {label}", flush=True)
        else:
            err = entry.get("error", "")
            print(f"  [{idx}/{total}] FAILED: {label} — {err}", flush=True)

    print(f"Precompute starting · base_url={args.base_url} · shapes={len(DEFAULT_SHAPES)}", flush=True)
    summary = asyncio.run(run_default_shapes(
        base_url=args.base_url,
        auth_header=f"Bearer {args.token}" if args.token else None,
        on_progress=_print_progress,
        request_timeout_s=args.timeout,
    ))
    elapsed = summary["completed_at"] - summary["started_at"]
    print(
        f"Done in {elapsed:.1f}s · ok={summary['ok_count']} · errors={summary['error_count']}",
        flush=True,
    )
    return 0 if summary["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli_main())
