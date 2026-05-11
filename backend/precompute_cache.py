"""
Precomputed menu-report cache.

The menu report (POST /api/reports/run with reportType=menu) does heavy work
— Toast Analytics API per week + a ~1M-row pivot + YoY enrichment — and
regularly hits nginx's ~60s upstream timeout for full-month, all-stores,
modifier-breakdown requests, sometimes crashing the worker outright.

To avoid that, we cache the *final result* of menu-report runs on disk,
keyed by a hash of the inputs that materially affect output. A nightly
precompute job (backend/precompute_menu_report.py, fired by cron at 6 AM)
warms the cache for the common shapes (last 7/14/28 days, current fiscal
period). The /api/reports/run handler does a read-through check at the top
of the menu branch; on hit, it returns the cached JSON immediately. On
miss, it computes fresh as before, then writes the result back to the
cache so even ad-hoc runs help warm it.

Cache hits ignore the debug flag (debug runs always bypass the cache so
fresh debug info is produced).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_DIR = Path(__file__).parent / "data" / "precomputed"
INDEX_PATH = CACHE_DIR / "index.json"

# Default freshness window. Nightly job runs at 06:00; we keep results valid
# for 26h so an oncall query at 5:30 AM still gets a hit before the new job
# completes.
DEFAULT_MAX_AGE_HOURS = 26.0

# Canonical "all Consolidated tags" superset — must mirror the same list in
# precompute_menu_report.py CONSOLIDATED_TAGS. When a user requests any
# non-empty *subset* of these tags, we can satisfy them by reading the
# superset cache entry and filtering down (see filter_menu_result_by_tags).
# Pivots use MULTI_COUNT mode so each item contributes its full quantity to
# each of its tags — that makes subset filtering of the superset result
# numerically identical to a live run with just the subset selected.
CONSOLIDATED_TAGS_SUPERSET = frozenset({
    "* Consolidated  Soups",  # double-space is intentional — real Toast tag name
    "* Consolidated 2oz 4oz Dressings",
    "* Consolidated Bulk GnG",
    "* Consolidated Catering",
    "* Consolidated Desserts",
    "* Consolidated Extras and Misc",
    "* Consolidated Modifiers",
    "* Consolidated NA Bev",
    "* Consolidated Non Food",
    "* Consolidated Pasta and Mac",
    "* Consolidated Pizza",
    "* Consolidated Salads",
    "* Consolidated Sandwiches",
    "* Consolidated Sides",
    "* Consolidated Wraps",
})


def _normalize_config_for_key(
    config_dict: Dict[str, Any],
    resolved_start: str,
    resolved_end: str,
) -> Dict[str, Any]:
    return {
        "reportType":       str(config_dict.get("reportType") or "").strip().lower(),
        "start":            resolved_start,
        "end":              resolved_end,
        "restaurantIds":    sorted(config_dict.get("restaurantIds") or []),
        "groupBy":          sorted(config_dict.get("groupBy") or []),
        "itemTags":         sorted(config_dict.get("itemTags") or []),
        "includeZeroSales": bool(config_dict.get("includeZeroSales", False)),
        "yoyEnabled":       bool(config_dict.get("yoyEnabled", False)),
        "yoyMode":          str(config_dict.get("yoyMode") or "same_date"),
        "alohaDate":        config_dict.get("alohaDate"),
        "orderModeFilter":  config_dict.get("orderModeFilter"),
    }


def cache_key(config_dict: Dict[str, Any], resolved_start: str, resolved_end: str) -> str:
    """Stable hash over the output-affecting subset of a ReportConfig."""
    normalized = _normalize_config_for_key(config_dict, resolved_start, resolved_end)
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def cache_path(key: str) -> Path:
    return CACHE_DIR / f"menu_{key}.json"


def cache_get_fresh(key: str, max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> Optional[Dict[str, Any]]:
    """Return the cached result if present and younger than max_age_hours."""
    path = cache_path(key)
    if not path.exists():
        return None
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age_seconds > max_age_hours * 3600.0:
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_set(key: str, result: Dict[str, Any], label: Optional[str] = None) -> None:
    """Atomically write `result` to the cache for `key`. Best-effort — failure is logged but not raised."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = cache_path(key)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(result, f)
        os.replace(tmp, path)
        _update_index(key, label)
    except Exception as exc:
        # Caching is opportunistic — never break a live request because the disk
        # is full or the path is missing. Caller already has the result.
        import logging
        logging.getLogger(__name__).warning("precompute_cache: failed to write %s: %s", key, exc)


def _update_index(key: str, label: Optional[str] = None) -> None:
    try:
        with INDEX_PATH.open("r", encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = {"entries": {}}
    try:
        size = cache_path(key).stat().st_size
    except OSError:
        size = 0
    entries = idx.setdefault("entries", {})
    entry = entries.get(key, {})
    entry.update({
        "computed_at": time.time(),
        "file_size":   size,
    })
    if label:
        entry["label"] = label
    entries[key] = entry
    try:
        with INDEX_PATH.open("w", encoding="utf-8") as f:
            json.dump(idx, f, indent=2)
    except Exception:
        pass


def list_index() -> Dict[str, Any]:
    """Return the index of all cached entries (for the admin UI)."""
    try:
        with INDEX_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"entries": {}}


def filter_menu_result_by_tags(result: Dict[str, Any], allowed_tags: set) -> Dict[str, Any]:
    """
    Filter a cached menu-report result down to only the rows/columns
    belonging to `allowed_tags`. Used when serving a strict subset of a
    cached superset (e.g. user picks 5 of the 15 Consolidated tags).

    The pivot in main.py uses MULTI_COUNT (each tag of an item gets the
    item's full quantity), so filtering the superset by a subset is the
    same as having run the report with just the subset.
    """
    if not isinstance(result, dict):
        return result

    out = dict(result)

    # ── List-of-rows fields with an `itemTagName` discriminator ───────────
    for list_key in ("itemBreakdown", "itemBreakdownDaily", "totals"):
        rows = out.get(list_key)
        if isinstance(rows, list):
            out[list_key] = [r for r in rows
                             if isinstance(r, dict) and r.get("itemTagName") in allowed_tags]

    # ── pivotTable: each row is {restaurantName, tag1: qty, tag2: qty, ...}
    pivot = out.get("pivotTable")
    if isinstance(pivot, list):
        new_pivot: List[Dict[str, Any]] = []
        for row in pivot:
            if not isinstance(row, dict):
                continue
            new_row: Dict[str, Any] = {}
            for col, val in row.items():
                # Always keep non-tag columns; for tag columns, keep only allowed.
                if col == "restaurantName" or col in allowed_tags:
                    new_row[col] = val
            new_pivot.append(new_row)
        out["pivotTable"] = new_pivot

    # ── orderModeSplit: nested OLO / In-Store channel views ───────────────
    split = out.get("orderModeSplit")
    if isinstance(split, dict):
        new_split: Dict[str, Any] = {}
        for ch_key, ch_val in split.items():
            if isinstance(ch_val, dict):
                ch_copy = dict(ch_val)
                for inner in ("itemBreakdown", "itemBreakdownDaily", "totals"):
                    rows = ch_copy.get(inner)
                    if isinstance(rows, list):
                        ch_copy[inner] = [r for r in rows
                                          if isinstance(r, dict) and r.get("itemTagName") in allowed_tags]
                new_split[ch_key] = ch_copy
            else:
                new_split[ch_key] = ch_val
        out["orderModeSplit"] = new_split

    return out


def lookup_with_superset_fallback(
    config_dict: Dict[str, Any],
    resolved_start: str,
    resolved_end: str,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> Optional[Dict[str, Any]]:
    """
    Try the exact cache key first; if that misses and the requested
    `itemTags` are a non-empty strict subset of CONSOLIDATED_TAGS_SUPERSET,
    try the superset key and filter the result before returning.

    Returns (None) on miss. The caller wraps this with logging / header
    bookkeeping.
    """
    primary_key = cache_key(config_dict, resolved_start, resolved_end)
    exact = cache_get_fresh(primary_key, max_age_hours)
    if exact is not None:
        return {"result": exact, "key": primary_key, "match": "exact"}

    requested = set(config_dict.get("itemTags") or [])
    # Empty itemTags means "all items, no tag filter" — semantically different
    # from "all Consolidated tags", so don't try the superset fallback there.
    if not requested:
        return None
    if not requested.issubset(CONSOLIDATED_TAGS_SUPERSET):
        return None
    if requested == CONSOLIDATED_TAGS_SUPERSET:
        # Already matches the superset shape; nothing to fall back to.
        return None

    superset_cfg = dict(config_dict)
    superset_cfg["itemTags"] = sorted(CONSOLIDATED_TAGS_SUPERSET)
    superset_key = cache_key(superset_cfg, resolved_start, resolved_end)
    superset_hit = cache_get_fresh(superset_key, max_age_hours)
    if superset_hit is None:
        return None

    filtered = filter_menu_result_by_tags(superset_hit, requested)
    return {"result": filtered, "key": superset_key, "match": "superset"}


def clear_cache() -> int:
    """Delete every cached menu_*.json file. Returns count deleted."""
    if not CACHE_DIR.exists():
        return 0
    deleted = 0
    for p in CACHE_DIR.glob("menu_*.json"):
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    try:
        INDEX_PATH.unlink()
    except OSError:
        pass
    return deleted
