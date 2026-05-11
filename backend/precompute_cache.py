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
