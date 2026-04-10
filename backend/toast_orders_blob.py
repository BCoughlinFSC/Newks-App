"""
toast_orders_blob.py  —  Azure Blob Storage persistence layer for cached Toast Orders API data.

Drop-in replacement for toast_orders_db.py (SQLite). Uses Azure Blob Storage
instead of a local SQLite file — designed to eliminate the 85GB orders.db
from Cloudways disk.

Layout inside the container
---------------------------
  orders/{restaurant_guid}/{YYYYMMDD}.json.gz
      Gzipped JSON array of order dicts. Custom metadata:
        order_count      : int  (number of orders in the blob)
        completed_at     : ISO timestamp
        net_sales_cents  : int  (precomputed sum of checks[0].amount * 100,
                                 excluding voided orders — used by the
                                 Aloha/Toast validation endpoint)

  status/failed/{restaurant_guid}/{YYYYMMDD}.json
      Tiny JSON: {"error": "...", "completed_at": "..."}

  status/inprogress/{restaurant_guid}/{YYYYMMDD}.json
      Tiny JSON: {"started_at": "..."}

Config (env vars)
-----------------
  AZURE_STORAGE_CONNECTION_STRING   Connection string for the storage account
  AZURE_ORDERS_CONTAINER            Container name (default: toast-orders)

Public interface
----------------
Mirrors toast_orders_db.py so callers can swap the import:

  initialize_db, db_available,
  mark_import_started, store_orders, mark_import_failed, delete_date,
  is_date_cached, get_cached_orders, get_cached_dates, get_failed_imports,
  get_stats, get_daily_net_sales

Note: get_connection() has no blob equivalent. The single caller that used it
(main.py validation endpoint) must be rewritten to use get_daily_net_sales().
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings
    from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    BlobServiceClient = None  # type: ignore
    ContainerClient = None  # type: ignore
    ContentSettings = None  # type: ignore
    ResourceNotFoundError = Exception  # type: ignore
    ResourceExistsError = Exception  # type: ignore

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONTAINER = os.getenv("AZURE_ORDERS_CONTAINER", "toast-orders")
CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

_ORDERS_PREFIX = "orders/"
_FAILED_PREFIX = "status/failed/"
_INPROGRESS_PREFIX = "status/inprogress/"


# ──────────────────────────────────────────────────────────────────────────────
# Client (lazy)
# ──────────────────────────────────────────────────────────────────────────────

_container_client: Optional["ContainerClient"] = None


def _get_container() -> "ContainerClient":
    """Return a cached ContainerClient. Creates the container on first call."""
    global _container_client
    if _container_client is not None:
        return _container_client

    if not _SDK_AVAILABLE:
        raise RuntimeError(
            "azure-storage-blob is not installed. Run: pip install azure-storage-blob"
        )
    if not CONNECTION_STRING:
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING is not set in the environment."
        )

    service = BlobServiceClient.from_connection_string(CONNECTION_STRING)
    container = service.get_container_client(DEFAULT_CONTAINER)
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    _container_client = container
    return container


def initialize_db(db_path: Optional[str] = None) -> None:
    """Ensure the blob container exists. db_path is ignored (kept for API compat)."""
    _get_container()


def db_available(db_path: Optional[str] = None) -> bool:
    """Return True if the blob container is reachable."""
    if not _SDK_AVAILABLE or not CONNECTION_STRING:
        return False
    try:
        _get_container().get_container_properties()
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def _orders_blob_name(restaurant_guid: str, business_date: str) -> str:
    return f"{_ORDERS_PREFIX}{restaurant_guid}/{business_date}.json.gz"


def _failed_blob_name(restaurant_guid: str, business_date: str) -> str:
    return f"{_FAILED_PREFIX}{restaurant_guid}/{business_date}.json"


def _inprogress_blob_name(restaurant_guid: str, business_date: str) -> str:
    return f"{_INPROGRESS_PREFIX}{restaurant_guid}/{business_date}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_net_sales_cents(orders: List[Dict]) -> int:
    """
    Precomputed aggregate used by the validation endpoint.
    Sums checks[0].amount (in cents) across non-voided orders.
    Mirrors the logic of the legacy raw-SQL query in main.py.
    """
    total = 0.0
    for o in orders:
        if o.get("voided"):
            continue
        checks = o.get("checks") or []
        if checks and isinstance(checks[0], dict):
            amt = checks[0].get("amount") or 0
            try:
                total += float(amt)
            except (TypeError, ValueError):
                pass
    return int(round(total * 100))


# ──────────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────────

def mark_import_started(
    restaurant_guid: str,
    business_date: str,
    db_path: Optional[str] = None,
) -> None:
    """Write an in-progress marker blob. Overwrites any existing marker."""
    container = _get_container()
    payload = json.dumps({"started_at": _now_iso()}).encode("utf-8")
    container.get_blob_client(_inprogress_blob_name(restaurant_guid, business_date)) \
             .upload_blob(payload, overwrite=True)
    # Clear any previous failure marker
    try:
        container.delete_blob(_failed_blob_name(restaurant_guid, business_date))
    except ResourceNotFoundError:
        pass


def store_orders(
    restaurant_guid: str,
    business_date: str,
    orders: List[Dict],
    db_path: Optional[str] = None,
) -> int:
    """
    Replace all cached orders for (restaurant, date). Writes a single gzipped
    JSON blob with summary metadata, then clears any in-progress/failed markers.
    Returns the number of orders stored.
    """
    container = _get_container()

    # Filter to orders with a GUID (matches legacy behavior)
    clean = [o for o in orders if (o.get("guid") or o.get("orderGuid"))]
    count = len(clean)

    raw = json.dumps(clean, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)

    metadata = {
        "order_count": str(count),
        "completed_at": _now_iso(),
        "net_sales_cents": str(_compute_net_sales_cents(clean)),
    }

    container.get_blob_client(_orders_blob_name(restaurant_guid, business_date)) \
             .upload_blob(
                 compressed,
                 overwrite=True,
                 metadata=metadata,
                 content_settings=ContentSettings(
                     content_type="application/json",
                     content_encoding="gzip",
                 ),
             )

    # Clear status markers
    for name in (
        _inprogress_blob_name(restaurant_guid, business_date),
        _failed_blob_name(restaurant_guid, business_date),
    ):
        try:
            container.delete_blob(name)
        except ResourceNotFoundError:
            pass

    return count


def mark_import_failed(
    restaurant_guid: str,
    business_date: str,
    error_msg: str,
    db_path: Optional[str] = None,
) -> None:
    """Write a failure marker blob."""
    container = _get_container()
    payload = json.dumps({
        "error": error_msg[:4000],
        "completed_at": _now_iso(),
    }).encode("utf-8")
    container.get_blob_client(_failed_blob_name(restaurant_guid, business_date)) \
             .upload_blob(payload, overwrite=True)
    try:
        container.delete_blob(_inprogress_blob_name(restaurant_guid, business_date))
    except ResourceNotFoundError:
        pass


def delete_date(
    restaurant_guid: str,
    business_date: str,
    db_path: Optional[str] = None,
) -> int:
    """Delete data + status blobs for one (restaurant, date). Returns 1 if data blob existed, else 0."""
    container = _get_container()
    deleted = 0
    for name, is_data in (
        (_orders_blob_name(restaurant_guid, business_date), True),
        (_inprogress_blob_name(restaurant_guid, business_date), False),
        (_failed_blob_name(restaurant_guid, business_date), False),
    ):
        try:
            container.delete_blob(name)
            if is_data:
                deleted = 1
        except ResourceNotFoundError:
            pass
    return deleted


# ──────────────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────────────

def is_date_cached(
    restaurant_guid: str,
    business_date: str,
    db_path: Optional[str] = None,
) -> bool:
    """True if the orders blob exists for (restaurant, date)."""
    if not db_available():
        return False
    try:
        blob = _get_container().get_blob_client(
            _orders_blob_name(restaurant_guid, business_date)
        )
        return blob.exists()
    except Exception as exc:
        logger.debug("is_date_cached error: %s", exc)
        return False


def get_cached_orders(
    restaurant_guid: str,
    business_date: str,
    db_path: Optional[str] = None,
) -> List[Dict]:
    """Download, decompress, and parse the cached orders. Returns [] if not present."""
    if not db_available():
        return []
    try:
        blob = _get_container().get_blob_client(
            _orders_blob_name(restaurant_guid, business_date)
        )
        data = blob.download_blob().readall()
    except ResourceNotFoundError:
        return []
    except Exception as exc:
        logger.warning("get_cached_orders error for %s|%s: %s",
                       restaurant_guid[:8], business_date, exc)
        return []

    try:
        # Try gzip first; fall back to raw JSON for legacy (pre-gzip) blobs
        if data[:2] == b'\x1f\x8b':
            raw = gzip.decompress(data)
        else:
            raw = data
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to parse cached orders %s|%s: %s",
                       restaurant_guid[:8], business_date, exc)
        return []


def get_cached_dates(
    restaurant_guid: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict]:
    """
    List all cached (restaurant, date) pairs by scanning the orders/ prefix.
    Returns [{restaurant_guid, business_date, order_count, completed_at}].
    """
    if not db_available():
        return []
    container = _get_container()
    prefix = f"{_ORDERS_PREFIX}{restaurant_guid}/" if restaurant_guid else _ORDERS_PREFIX
    out: List[Dict] = []
    try:
        for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            # Name is orders/{rguid}/{YYYYMMDD}.json.gz
            parts = blob.name.split("/")
            if len(parts) != 3 or not parts[2].endswith(".json.gz"):
                continue
            rguid = parts[1]
            bdate = parts[2][:-len(".json.gz")]
            meta = blob.metadata or {}
            out.append({
                "restaurant_guid": rguid,
                "business_date": bdate,
                "order_count": int(meta.get("order_count", "0") or 0),
                "completed_at": meta.get("completed_at"),
            })
    except Exception as exc:
        logger.warning("get_cached_dates error: %s", exc)
        return []
    out.sort(key=lambda r: r["business_date"], reverse=True)
    return out


def get_failed_imports(db_path: Optional[str] = None) -> List[Dict]:
    """List all failed import markers."""
    if not db_available():
        return []
    container = _get_container()
    out: List[Dict] = []
    try:
        for blob in container.list_blobs(name_starts_with=_FAILED_PREFIX):
            parts = blob.name.split("/")
            # status/failed/{rguid}/{YYYYMMDD}.json
            if len(parts) != 4 or not parts[3].endswith(".json"):
                continue
            rguid = parts[2]
            bdate = parts[3][:-len(".json")]
            try:
                raw = container.get_blob_client(blob.name).download_blob().readall()
                info = json.loads(raw)
            except Exception:
                info = {}
            out.append({
                "restaurant_guid": rguid,
                "business_date": bdate,
                "error_message": info.get("error"),
                "completed_at": info.get("completed_at"),
            })
    except Exception as exc:
        logger.warning("get_failed_imports error: %s", exc)
        return []
    out.sort(key=lambda r: r.get("completed_at") or "", reverse=True)
    return out


def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Summary: total_orders, total_restaurants, min_date, max_date, dates_cached,
             db_size_mb (total compressed blob size), last_import_at, failed_count.
    """
    empty: Dict[str, Any] = {
        "total_orders": 0,
        "total_restaurants": 0,
        "min_date": None,
        "max_date": None,
        "dates_cached": 0,
        "db_size_mb": 0.0,
        "last_import_at": None,
        "failed_count": 0,
    }
    if not db_available():
        return empty

    container = _get_container()
    total_orders = 0
    total_bytes = 0
    dates_cached = 0
    restaurants: set = set()
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    last_import: Optional[str] = None

    try:
        for blob in container.list_blobs(name_starts_with=_ORDERS_PREFIX, include=["metadata"]):
            parts = blob.name.split("/")
            if len(parts) != 3 or not parts[2].endswith(".json.gz"):
                continue
            rguid = parts[1]
            bdate = parts[2][:-len(".json.gz")]
            meta = blob.metadata or {}
            try:
                total_orders += int(meta.get("order_count", "0") or 0)
            except ValueError:
                pass
            total_bytes += blob.size or 0
            dates_cached += 1
            restaurants.add(rguid)
            if min_date is None or bdate < min_date:
                min_date = bdate
            if max_date is None or bdate > max_date:
                max_date = bdate
            completed = meta.get("completed_at")
            if completed and (last_import is None or completed > last_import):
                last_import = completed

        failed_count = sum(
            1 for _ in container.list_blobs(name_starts_with=_FAILED_PREFIX)
        )
    except Exception as exc:
        logger.warning("get_stats error: %s", exc)
        return empty

    return {
        "total_orders": total_orders,
        "total_restaurants": len(restaurants),
        "min_date": min_date,
        "max_date": max_date,
        "dates_cached": dates_cached,
        "db_size_mb": round(total_bytes / (1024 * 1024), 2),
        "last_import_at": last_import,
        "failed_count": failed_count,
    }


def get_daily_net_sales(start_yyyymmdd: str, end_yyyymmdd: str) -> List[tuple]:
    """
    Replacement for the raw-SQL validation query in main.py:10212.
    Returns [(business_date, restaurant_guid, net_sales_dollars), ...] by
    reading precomputed summary metadata from the orders blobs in the range.
    No blob downloads required — metadata only.
    """
    if not db_available():
        return []
    container = _get_container()
    out: List[tuple] = []
    try:
        for blob in container.list_blobs(name_starts_with=_ORDERS_PREFIX, include=["metadata"]):
            parts = blob.name.split("/")
            if len(parts) != 3 or not parts[2].endswith(".json.gz"):
                continue
            bdate = parts[2][:-len(".json.gz")]
            if bdate < start_yyyymmdd or bdate > end_yyyymmdd:
                continue
            rguid = parts[1]
            meta = blob.metadata or {}
            try:
                cents = int(meta.get("net_sales_cents", "0") or 0)
            except ValueError:
                cents = 0
            out.append((bdate, rguid, cents / 100.0))
    except Exception as exc:
        logger.warning("get_daily_net_sales error: %s", exc)
        return []
    return out
