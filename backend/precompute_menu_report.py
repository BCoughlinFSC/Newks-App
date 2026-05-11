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
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import httpx


from precompute_cache import CONSOLIDATED_TAGS_SUPERSET

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
