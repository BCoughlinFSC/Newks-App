"""
OLO Ordering API client for Order Export (Signature / Server-to-Server auth).
Used for Digital Orders by Platform report: list batches, download orderbatch XML, aggregate by platform.
"""
import hashlib
import hmac
import base64
import os
import zipfile
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
import httpx
from dotenv import load_dotenv

# Always load from backend directory so credentials are found regardless of cwd
_backend_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_backend_dir, ".env"))

# Production by default; set OLO_ORDERING_BASE_URL only if you need sandbox
OLO_BASE_URL = os.getenv("OLO_ORDERING_BASE_URL", "https://ordering.api.olo.com/v1.1")
OLO_CLIENT_ID = os.getenv("OLO_CLIENT_ID", "").strip()
OLO_CLIENT_SECRET = os.getenv("OLO_CLIENT_SECRET", "").strip()


def _path_and_query(path_suffix: str) -> str:
    """Path for signing: must include version segment, e.g. /v1.1/orderexports."""
    parsed = urlparse(OLO_BASE_URL.rstrip("/"))
    base_path = parsed.path or "/v1.1"
    return (base_path.rstrip("/") + "/" + path_suffix.lstrip("/")).replace(" ", "%20")


def _rfc1123_date() -> str:
    """RFC1123 format: ddd, dd MMM yyyy HH:mm:ss GMT"""
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")


def _build_olo_signature(
    client_id: str,
    client_secret: str,
    method: str,
    content_type: str,
    path_and_query: str,
    date_str: str,
    body: bytes = b"",
) -> str:
    """
    Build OloSignature per OLO docs: HMAC-SHA256 of message, base64.
    Message = ClientId + newline + HttpVerb + ContentType + Base64(SHA256(body)) + PathAndQuery + Timestamp
    """
    body_hash = hashlib.sha256(body).digest()
    body_b64 = base64.b64encode(body_hash).decode("ascii")
    message = "\n".join([client_id, method, content_type, body_b64, path_and_query, date_str])
    sig = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(sig).decode("ascii")


def _request_headers(
    method: str,
    path_and_query: str,
    body: bytes = b"",
    content_type: str = "",
) -> Dict[str, str]:
    """Build Authorization and Date headers for OLO Signature auth."""
    date_str = _rfc1123_date()
    signature = _build_olo_signature(
        OLO_CLIENT_ID,
        OLO_CLIENT_SECRET,
        method,
        content_type,
        path_and_query,
        date_str,
        body,
    )
    headers = {
        "Authorization": f"OloSignature {OLO_CLIENT_ID}:{signature}",
        "Date": date_str,
    }
    if content_type:
        headers["Content-Type"] = content_type
    # Required for Signature auth: end user IP (use a placeholder for server-to-server export)
    headers["X-Forwarded-For"] = "127.0.0.1"
    return headers


async def list_order_batches() -> List[Dict[str, Any]]:
    """
    GET /orderexports -> list of { batchId, generated }.
    Batches are in reverse chronological order (newest first).
    """
    if not OLO_CLIENT_ID or not OLO_CLIENT_SECRET:
        raise ValueError("OLO_CLIENT_ID and OLO_CLIENT_SECRET must be set in .env")
    base = OLO_BASE_URL.rstrip("/")
    path = _path_and_query("orderexports")
    url = base + "/" + "orderexports".lstrip("/")
    headers = _request_headers("GET", path)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(url, headers=headers, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
    batches = data.get("batches") or []
    return batches


async def get_batch_download_url(batch_id: int) -> str:
    """
    POST /orderexports/{batchId} -> 301 redirect to signed ZIP URL.
    Returns the Location URL (valid ~5 minutes).
    """
    if not OLO_CLIENT_ID or not OLO_CLIENT_SECRET:
        raise ValueError("OLO_CLIENT_ID and OLO_CLIENT_SECRET must be set in .env")
    base = OLO_BASE_URL.rstrip("/")
    path = _path_and_query(f"orderexports/{batch_id}")
    url = base + "/" + f"orderexports/{batch_id}".lstrip("/")
    headers = _request_headers("POST", path)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.post(url, headers=headers, timeout=60.0)
        if resp.status_code != 301:
            resp.raise_for_status()
        location = resp.headers.get("location")
        if not location:
            raise ValueError("No redirect location in batch download response")
        return location


async def download_batch_zip(batch_id: int) -> bytes:
    """Get redirect URL then download the ZIP bytes."""
    url = await get_batch_download_url(batch_id)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, timeout=120.0)
        resp.raise_for_status()
        return resp.content


def parse_order_batch_xml(xml_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Parse orderbatch.xml from the export ZIP.
    Returns list of order dicts with: storeName, storeRef, timePlaced, timeClosed,
    source, orderingProvider_name, orderingProvider_slug, clientPlatform, handoff,
    total, subtotal, discount, tax, tip, deliveryFee, etc.
    """
    root = ET.fromstring(xml_bytes)
    orders = []
    orders_el = root.find("orders")
    if orders_el is None:
        return orders

    def text(el: Optional[ET.Element]) -> str:
        return (el.text or "").strip() if el is not None else ""

    for order_el in orders_el.findall("order"):
        o = {}
        o["oloRef"] = text(order_el.find("oloRef"))
        o["posRef"] = text(order_el.find("posRef"))
        o["storeName"] = text(order_el.find("storeName"))
        o["storeRef"] = text(order_el.find("storeRef"))
        o["timePlaced"] = text(order_el.find("timePlaced"))
        o["timeClosed"] = text(order_el.find("timeClosed"))
        o["timeWanted"] = text(order_el.find("timeWanted"))
        o["source"] = text(order_el.find("source"))  # Web, MobileWeb, SMS, API, CallCenter
        o["handoff"] = text(order_el.find("handoff"))  # CounterPickup, CurbsidePickup, Dispatch, etc.
        o["clientPlatform"] = text(order_el.find("clientPlatform"))
        op = order_el.find("orderingProvider")
        if op is not None:
            o["orderingProvider_name"] = text(op.find("name"))
            o["orderingProvider_slug"] = text(op.find("slug"))
        else:
            o["orderingProvider_name"] = ""
            o["orderingProvider_slug"] = ""

        for tag in ("subtotal", "discount", "tax", "tip", "deliveryFee", "customFeeTotal", "donationsTotal", "total"):
            node = order_el.find(tag)
            if node is not None and node.text:
                try:
                    o[tag] = float(node.text.strip())
                except ValueError:
                    o[tag] = 0.0
            else:
                o[tag] = 0.0
        orders.append(o)
    return orders


def extract_orders_from_zip(zip_bytes: bytes) -> List[Dict[str, Any]]:
    """Open the export ZIP and parse orderbatch.xml."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        xml_name = None
        for n in names:
            if n.endswith("orderbatch.xml") or n == "orderbatch.xml":
                xml_name = n
                break
        if not xml_name:
            raise ValueError("No orderbatch.xml found in ZIP")
        with zf.open(xml_name) as f:
            return parse_order_batch_xml(f.read())


def _parse_batch_generated(generated: str) -> Optional[datetime]:
    """Parse OLO batch 'generated' (e.g. '20210204 03:27' or ISO) to date for filtering."""
    if not generated:
        return None
    s = (generated or "").strip()
    # "yyyymmdd hh:mm" format
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            pass
    # ISO-style
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    return None


def filter_batches_by_date_range(
    batches: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """
    Filter batches to those whose generated date (date part) falls within [start_date, end_date].
    start_date, end_date are YYYY-MM-DD strings.
    """
    try:
        start = datetime.strptime(start_date.strip()[:10], "%Y-%m-%d").date()
        end = datetime.strptime(end_date.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return batches
    out = []
    for b in batches:
        gen = _parse_batch_generated(b.get("generated") or "")
        if gen and start <= gen.date() <= end:
            out.append(b)
    return out


def aggregate_digital_orders_by_platform(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Aggregate orders into report rows: Platform, Client Platform, Order Count, Net Sales.
    Platform = orderingProvider name/slug if present, else source, else 'Unknown'.
    Client Platform = OLO clientPlatform (iOS, Android, Web, Kiosk, ThirdParty, etc.).
    Net sales = order total (same as OLO 'total' field).
    """
    # Key: (platform, client_platform) -> counts and net sales
    buckets: Dict[tuple, Dict[str, Any]] = {}

    for o in orders:
        platform = (
            o.get("orderingProvider_name") or o.get("orderingProvider_slug")
            or o.get("source")
            or "Unknown"
        )
        client_platform = (o.get("clientPlatform") or "").strip() or "Unknown"
        key = (platform, client_platform)

        if key not in buckets:
            buckets[key] = {
                "platform": platform,
                "clientPlatform": client_platform,
                "orderCount": 0,
                "netSales": 0.0,
            }
        b = buckets[key]
        b["orderCount"] += 1
        b["netSales"] += float(o.get("total") or 0)

    result = list(buckets.values())
    result.sort(key=lambda x: (x["platform"], x["clientPlatform"]))
    return result
