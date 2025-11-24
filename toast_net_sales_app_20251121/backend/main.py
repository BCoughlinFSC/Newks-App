from datetime import date, datetime
from typing import List, Optional, Dict
import time
import sys

import asyncio
import os

import httpx
from httpx import HTTPStatusError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ------------------
# Config
# ------------------

"""
This app expects the following environment variables:

TOAST_API_BASE_URL  - e.g. https://ws-api.toasttab.com/era/v1
TOAST_API_CLIENT_ID - OAuth2 client ID (or use TOAST_API_TOKEN for direct bearer token)
TOAST_API_CLIENT_SECRET - OAuth2 client secret
TOAST_API_TOKEN     - Optional: Direct bearer token (alternative to OAuth2)

Edit .env and use something like `uvicorn main:app --reload` from this backend directory.
"""

TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")
TOAST_API_TOKEN = os.getenv("TOAST_API_TOKEN")  # Optional direct token

# Extract base hostname for authentication endpoint
# e.g., https://ws-api.toasttab.com/era/v1 -> https://ws-api.toasttab.com
if "/era/v1" in TOAST_API_BASE_URL:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.replace("/era/v1", "")
else:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.rsplit("/", 1)[0] if "/" in TOAST_API_BASE_URL else TOAST_API_BASE_URL

TOAST_AUTH_URL = f"{TOAST_AUTH_BASE_URL}/authentication/v1/authentication/login"

# Validate that we have either OAuth2 credentials or a direct token
if not TOAST_API_TOKEN and (not TOAST_API_CLIENT_ID or not TOAST_API_CLIENT_SECRET):
    raise RuntimeError(
        "Either TOAST_API_TOKEN or both TOAST_API_CLIENT_ID and TOAST_API_CLIENT_SECRET must be set. "
        "Create a .env file and set the appropriate credentials."
    )

# Token cache
_token_cache: Dict[str, any] = {
    "token": None,
    "expires_at": 0,
}


async def get_auth_headers() -> Dict[str, str]:
    """
    Get authorization headers, acquiring a token via OAuth2 if needed.
    Caches the token and refreshes when expired.
    """
    # If direct token is provided, use it
    if TOAST_API_TOKEN:
        return {
            "Authorization": f"Bearer {TOAST_API_TOKEN}",
            "Content-Type": "application/json",
        }
    
    # Check if we have a valid cached token
    current_time = time.time()
    if _token_cache["token"] and current_time < _token_cache["expires_at"]:
        return {
            "Authorization": f"Bearer {_token_cache['token']}",
            "Content-Type": "application/json",
        }
    
    # Acquire new token via OAuth2
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_API_CLIENT_ID,
            "clientSecret": TOAST_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        try:
            resp = await client.post(TOAST_AUTH_URL, json=auth_body)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Failed to authenticate with Toast API: {resp.text}",
                )
            
            # Parse response - Toast returns {"@class": "...", "token": {"accessToken": "...", "expiresIn": ...}}
            try:
                data = resp.json()
                if isinstance(data, dict):
                    # Check for nested token object (Toast API format)
                    if "token" in data and isinstance(data["token"], dict):
                        token = data["token"].get("accessToken") or data["token"].get("token")
                        expires_in = data["token"].get("expiresIn", 3600)  # Default to 1 hour
                    else:
                        # Fallback: check top-level fields
                        token = data.get("accessToken") or data.get("token") or data.get("access_token")
                        expires_in = data.get("expiresIn", 3600)
                else:
                    token = data
                    expires_in = 3600
            except ValueError:
                # Response might be plain text
                token = resp.text.strip().strip('"')
                expires_in = 3600
            
            if not token:
                raise HTTPException(
                    status_code=500,
                    detail="No token received from Toast authentication endpoint",
                )
            
            # Cache the token, refresh 5 minutes before expiry
            _token_cache["token"] = token
            _token_cache["expires_at"] = current_time + max(expires_in - 300, 60)  # At least 1 minute
            
            return {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Failed to connect to Toast authentication: {str(e)}",
            )

# ------------------
# Data models
# ------------------


class Location(BaseModel):
    restaurantGuid: str
    restaurantName: str


class NetSalesRow(BaseModel):
    restaurantGuid: str
    restaurantName: Optional[str]
    businessDate: date
    netSalesAmount: float


# ------------------
# Helper functions
# ------------------


def to_business_date(d: date) -> str:
    """
    Convert Python date -> Toast business date string YYYYMMDD.
    """
    return d.strftime("%Y%m%d")


async def get_all_active_locations(client: httpx.AsyncClient) -> List[Location]:
    """
    Call Toast /restaurants-information endpoint and return active, non-archived,
    non-test locations as Location objects.
    """
    url = f"{TOAST_API_BASE_URL}/restaurants-information"
    headers = await get_auth_headers()
    resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error fetching restaurants-information: {resp.text}",
        )
    data = resp.json()  # list of RestaurantsGuidName objects

    active = [
        Location(
            restaurantGuid=item["restaurantGuid"],
            restaurantName=item["restaurantName"],
        )
        for item in data
        if item.get("active") and not item.get("archived") and not item.get("testMode")
    ]
    return active


async def request_metrics_report(
    client: httpx.AsyncClient,
    start: date,
    end: date,
    restaurant_ids: List[str],
) -> str:
    """
    Create a Toast metrics report for the given date range and restaurant IDs.
    Automatically selects the best endpoint based on date range to optimize rate limits:
    - day: 1 day (10 req/min, 60 req/hour)
    - week: 7 or fewer days (10 req/min, 60 req/hour)
    - month: 31 or fewer days (10 req/hour)
    - year: 366 or fewer days (10 req/hour)
    - custom: anything else (10 req/hour)
    
    Returns the reportRequestGuid.
    """
    # Calculate number of days in the range (inclusive)
    days_diff = (end - start).days + 1
    
    # Determine which endpoint to use based on date range
    # This helps us get better rate limits (10/min for day/week vs 10/hour for custom)
    if days_diff == 1:
        time_range = "day"
    elif days_diff <= 7:
        time_range = "week"
    elif days_diff <= 31:
        time_range = "month"
    elif days_diff <= 366:
        time_range = "year"
    else:
        time_range = None  # Use custom endpoint
    
    # Build request body
    body = {
        "startBusinessDate": to_business_date(start),
        "restaurantIds": restaurant_ids,
        "excludedRestaurantIds": [],
        "groupBy": [],  # keep it simple: just net sales by business date & restaurant
    }
    
    # For day range, endBusinessDate is optional per Toast docs
    # For other ranges, include endBusinessDate
    # Actually, let's always include it to be safe - Toast docs say it's optional for day, not forbidden
    body["endBusinessDate"] = to_business_date(end)
    
    # Choose endpoint based on time range
    if time_range:
        # Use specific time range endpoint for better rate limits
        # day/week: 10 req/min, 60 req/hour
        # month/year: 10 req/hour
        url = f"{TOAST_API_BASE_URL}/metrics/{time_range}"
    else:
        # Use custom time range endpoint (10 req/hour)
        url = f"{TOAST_API_BASE_URL}/metrics"
    
    try:
        headers = await get_auth_headers()
        resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        # Catch errors in the HTTP request
        raise HTTPException(
            status_code=500,
            detail=f"Error making request to Toast API: {str(e)}",
        )
    
    if resp.status_code == 429:
        # Rate limit error from Toast API
        rate_limit_msg = "10 requests/minute" if time_range in ["day", "week"] else "10 requests/hour"
        range_name = time_range if time_range else "custom"
        raise HTTPException(
            status_code=429,
            detail=f"Toast API rate limit exceeded ({rate_limit_msg} for {range_name} ranges). Please wait a few minutes before trying again.",
        )
    
    if resp.status_code != 200:
        # Try to extract a meaningful error message
        error_msg = resp.text
        try:
            error_json = resp.json()
            error_msg = error_json.get("message", error_json.get("detail", error_msg))
        except:
            pass
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error creating metrics request: {error_msg}",
        )

    # Some Toast implementations return a bare string; others may wrap it.
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip().strip('"')

    if isinstance(data, str):
        return data.strip().strip('"')

    # Fallback if wrapped in object
    return data.get("reportRequestGuid") or data.get("guid") or resp.text.strip().strip('"')


async def fetch_metrics_report(
    client: httpx.AsyncClient,
    report_guid: str,
    max_attempts: int = 20,
    delay_seconds: float = 1.0,
) -> list:
    """
    Poll Toast metrics/{report_guid} until data is ready (HTTP 200) or until we
    hit max_attempts. Toast may respond with HTTP 202 while the report is being
    generated.
    """
    url = f"{TOAST_API_BASE_URL}/metrics/{report_guid}"
    params = {"fetchRestaurantNames": "true"}

    for attempt in range(max_attempts):
        headers = await get_auth_headers()
        resp = await client.get(url, headers=headers, params=params)

        if resp.status_code == 200:
            return resp.json()  # expected: list of metrics rows

        if resp.status_code == 202:
            # still processing
            await asyncio.sleep(delay_seconds)
            continue

        if resp.status_code == 429:
            # Rate limit error from Toast API
            raise HTTPException(
                status_code=429,
                detail="Toast API rate limit exceeded while retrieving report. Please wait a few minutes before trying again.",
            )

        # Try to extract a meaningful error message
        error_msg = resp.text
        try:
            error_json = resp.json()
            error_msg = error_json.get("message", error_json.get("detail", error_msg))
        except:
            pass

        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error retrieving metrics: {error_msg}",
        )

    raise HTTPException(
        status_code=504,
        detail="Timed out waiting for metrics report. Try narrowing the date range.",
    )


def normalize_row(row: dict) -> NetSalesRow:
    """
    Convert raw Toast metrics row into our NetSalesRow model.
    Expects businessDate as YYYYMMDD.
    """
    bd_str = row.get("businessDate")
    if not bd_str:
        raise ValueError("Missing businessDate in row")
    
    try:
        bd = datetime.strptime(bd_str, "%Y%m%d").date()
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid businessDate format: {bd_str}") from e

    restaurant_guid = row.get("restaurantGuid")
    if not restaurant_guid:
        raise ValueError("Missing restaurantGuid in row")

    net_sales = row.get("netSalesAmount", 0.0)
    try:
        net_sales_float = float(net_sales) if net_sales is not None else 0.0
    except (ValueError, TypeError):
        net_sales_float = 0.0

    return NetSalesRow(
        restaurantGuid=restaurant_guid,
        restaurantName=row.get("restaurantName"),
        businessDate=bd,
        netSalesAmount=net_sales_float,
    )


# ------------------
# FastAPI app
# ------------------

app = FastAPI(
    title="Newk's Toast Net Sales API",
    description="Simple API to query net sales by day, by location from Toast Analytics.",
)

# Configure logging to show errors in console
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Allow the frontend (served from the same or another origin) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # in production, lock this down
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """
    Basic health check endpoint.
    """
    return {"status": "ok", "version": "2025-11-21-with-logging"}


@app.get("/version")
async def version():
    """
    Simple endpoint to verify server is running latest code.
    """
    return {
        "version": "2025-11-21-with-logging",
        "has_test_error": True,
        "has_logging": True
    }


@app.get("/test-error")
async def test_error():
    """
    Test endpoint to verify error logging is working.
    """
    try:
        raise ValueError("This is a test error to verify logging works")
    except Exception as e:
        logger.error(f"Test error caught: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Test error: {str(e)}")


@app.get("/locations", response_model=List[Location])
async def list_locations():
    """
    Return all active, non-archived, non-test locations for the Toast enterprise.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        locations = await get_all_active_locations(client)
    return locations


@app.get("/sales/net", response_model=List[NetSalesRow])
async def get_net_sales(
    start_date: date,
    end_date: date,
    restaurant_ids: Optional[List[str]] = Query(
        None,
        description=(
            "List of restaurantGuids to include. "
            "If omitted, all active locations will be used."
        ),
    ),
):
    """
    Get net sales by business date and location for the given date range.

    - start_date / end_date: ISO dates (YYYY-MM-DD)
    - restaurant_ids: optional list of restaurantGuid values

    Returns a flat list of rows; the frontend can group/summarize as needed.
    """
    # Log the request (don't fail if logging has issues)
    try:
        logger.info(f"get_net_sales called: start={start_date}, end={end_date}, restaurant_ids_count={len(restaurant_ids) if restaurant_ids else 0}")
    except:
        pass  # Don't fail on logging
    
    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="end_date must be on or after start_date",
        )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Filter out any empty, None, or invalid restaurant_ids
            # FastAPI may pass empty strings when frontend sends empty params
            if restaurant_ids:
                restaurant_ids = [
                    rid for rid in restaurant_ids 
                    if rid and isinstance(rid, str) and rid.strip() and len(rid.strip()) > 0
                ]
            
            # If no valid restaurant_ids passed, fetch all active locations
            if not restaurant_ids or len(restaurant_ids) == 0:
                try:
                    locations = await get_all_active_locations(client)
                    restaurant_ids = [loc.restaurantGuid for loc in locations]
                except HTTPException as e:
                    # Re-raise HTTPExceptions from get_all_active_locations
                    raise
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to fetch restaurant locations: {str(e)}",
                    )
            
            # Ensure we have at least one restaurant
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="No restaurants available for the selected criteria. Please select at least one location.",
                )

            # 1) Create metrics request
            try:
                report_guid = await request_metrics_report(client, start_date, end_date, restaurant_ids)
            except HTTPException as e:
                # Re-raise HTTPExceptions from request_metrics_report
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create metrics report request: {str(e)}",
                )

            # 2) Poll until data ready
            try:
                raw_rows = await fetch_metrics_report(client, report_guid)
            except HTTPException as e:
                # Re-raise HTTPExceptions from fetch_metrics_report
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to retrieve metrics report: {str(e)}",
                )

        # 3) Normalize and sort rows
        if not raw_rows:
            return []
        
        result = []
        for r in raw_rows:
            try:
                result.append(normalize_row(r))
            except (ValueError, KeyError, TypeError) as e:
                # Log but skip invalid rows rather than failing entirely
                print(f"Warning: Skipping invalid row: {e}, row data: {r}")
                continue
        
        result.sort(key=lambda r: (r.restaurantName or "", r.businessDate))
        return result
        
    except HTTPException:
        # Re-raise HTTPExceptions as-is
        raise
    except httpx.HTTPStatusError as e:
        # Handle HTTP errors from Toast API
        error_msg = f"Toast API error: {e.response.status_code}"
        if e.response.text:
            try:
                error_data = e.response.json()
                error_msg = error_data.get("message", error_data.get("detail", str(e)))
            except:
                error_msg = e.response.text[:200] if len(e.response.text) < 200 else e.response.text[:200] + "..."
        raise HTTPException(
            status_code=e.response.status_code if e.response.status_code < 500 else 502,
            detail=f"Error from Toast API: {error_msg}",
        )
    except Exception as e:
        # Catch any other unexpected errors and return a 500 with details
        import traceback
        error_details = str(e)
        error_type = type(e).__name__
        error_traceback = traceback.format_exc()
        
        # Log to console and stderr for visibility - this should always show
        print(f"ERROR in get_net_sales ({error_type}): {error_details}", file=sys.stderr)
        print(error_traceback, file=sys.stderr)
        
        # Also try logging (logger should be defined at module level)
        try:
            logger.error(f"Unexpected error in get_net_sales: {error_type}: {error_details}\n{error_traceback}")
        except NameError:
            # Logger not available, that's ok - we already printed to stderr
            pass
        except:
            pass
        
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching net sales data: {error_type}: {error_details}",
        )


# ------------------
# Static files (frontend)
# ------------------

# We mount ../frontend as / so visiting http://localhost:8000/ serves index.html.
# You can also serve frontend separately if you prefer.
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount(
    "/",
    StaticFiles(directory=frontend_path, html=True),
    name="frontend",
)
