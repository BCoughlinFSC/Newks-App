from datetime import date, datetime, timedelta
from typing import List, Optional, Dict
import time
import sys
import json
import uuid
import hashlib

import asyncio
import os

import httpx
from httpx import HTTPStatusError
from fastapi import FastAPI, HTTPException, Query, Body, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from jose import JWTError, jwt
import bcrypt

load_dotenv()

# ------------------
# Config
# ------------------

TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")
TOAST_API_TOKEN = os.getenv("TOAST_API_TOKEN")

TOAST_STANDARD_API_CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
TOAST_STANDARD_API_CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")
TOAST_STANDARD_API_BASE_URL = os.getenv("TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com")

# Slack webhook configuration
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")  # Slack incoming webhook URL

# Authentication configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days

# Super Admin configuration (for initial setup)
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").lower()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "Super Admin")

# Super Admin configuration (for initial setup)
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").lower()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "Super Admin")

# Password hashing - using bcrypt directly
security = HTTPBearer()

if "/era/v1" in TOAST_API_BASE_URL:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.replace("/era/v1", "")
else:
    TOAST_AUTH_BASE_URL = TOAST_API_BASE_URL.rsplit("/", 1)[0] if "/" in TOAST_API_BASE_URL else TOAST_API_BASE_URL

TOAST_AUTH_URL = f"{TOAST_AUTH_BASE_URL}/authentication/v1/authentication/login"

if not TOAST_API_TOKEN and (not TOAST_API_CLIENT_ID or not TOAST_API_CLIENT_SECRET):
    raise RuntimeError(
        "Either TOAST_API_TOKEN or both TOAST_API_CLIENT_ID and TOAST_API_CLIENT_SECRET must be set."
    )

_token_cache: Dict[str, any] = {"token": None, "expires_at": 0}
_standard_token_cache: Dict[str, any] = {"token": None, "expires_at": 0}
_menu_tags_cache: Dict[str, dict] = {}  # {restaurant_guid: {hash: ..., mapping: {...}}}

# ------------------
# Data Models
# ------------------

class Location(BaseModel):
    restaurantGuid: str
    restaurantName: str

class ReportConfig(BaseModel):
    reportType: str  # "metrics" or "menu"
    dateRangeType: str  # "last_7_days", "last_30_days", "custom", etc.
    customStartDate: Optional[str] = None
    customEndDate: Optional[str] = None
    restaurantIds: List[str] = []  # empty = all locations
    groupBy: List[str] = []  # Toast groupBy options
    itemTags: List[str] = []  # For menu reports - filter by item tags

class SavedReport(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    createdBy: str
    createdAt: str
    lastModified: str
    config: ReportConfig

class SavedReportCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    createdBy: str
    config: ReportConfig

class ItemTagsQuantitiesRequest(BaseModel):
    startBusinessDate: str  # YYYYMMDD
    endBusinessDate: str  # YYYYMMDD
    restaurantIds: List[str]
    daily: bool = False
    tagAttributionMode: str = "MULTI_COUNT"  # MULTI_COUNT | SPLIT | PRIMARY_ONLY

class HelpTicketRequest(BaseModel):
    name: str
    email: str
    subject: str
    priority: int  # 1=Low, 2=Medium, 3=High, 4=Urgent
    description: str

# ------------------
# User Authentication Models
# ------------------

class User(BaseModel):
    id: str
    email: str
    name: str
    createdAt: str
    lastLogin: Optional[str] = None
    preferences: Optional[Dict] = {}  # User preferences (default locations, etc.)
    role: Optional[str] = "user"  # User role: "user" or "admin"

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: Optional[str] = "user"

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    preferences: Optional[Dict] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    user: User

# ------------------
# Auth Helpers
# ------------------

async def get_auth_headers() -> Dict[str, str]:
    if TOAST_API_TOKEN:
        return {"Authorization": f"Bearer {TOAST_API_TOKEN}", "Content-Type": "application/json"}
    
    current_time = time.time()
    if _token_cache["token"] and current_time < _token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_token_cache['token']}", "Content-Type": "application/json"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_API_CLIENT_ID,
            "clientSecret": TOAST_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(TOAST_AUTH_URL, json=auth_body)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Failed to authenticate: {resp.text}")
        
        data = resp.json()
        if isinstance(data, dict):
            if "token" in data and isinstance(data["token"], dict):
                token = data["token"].get("accessToken") or data["token"].get("token")
                expires_in = data["token"].get("expiresIn", 3600)
            else:
                token = data.get("accessToken") or data.get("token") or data.get("access_token")
                expires_in = data.get("expiresIn", 3600)
        else:
            token = data
            expires_in = 3600
        
        if not token:
            raise HTTPException(status_code=500, detail="No token received from authentication")
        
        _token_cache["token"] = token
        _token_cache["expires_at"] = current_time + max(expires_in - 300, 60)
        
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

async def get_standard_api_auth_headers() -> Dict[str, str]:
    """Get authorization headers for Standard API (Menus API)"""
    if not TOAST_STANDARD_API_CLIENT_ID or not TOAST_STANDARD_API_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Standard API credentials required for Menus API access."
        )
    
    current_time = time.time()
    if _standard_token_cache["token"] and current_time < _standard_token_cache["expires_at"]:
        return {"Authorization": f"Bearer {_standard_token_cache['token']}", "Content-Type": "application/json"}
    
    standard_auth_url = f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_STANDARD_API_CLIENT_ID,
            "clientSecret": TOAST_STANDARD_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(standard_auth_url, json=auth_body)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Failed to authenticate with Standard API: {resp.text}")
        
        data = resp.json()
        if isinstance(data, dict):
            if "token" in data and isinstance(data["token"], dict):
                token = data["token"].get("accessToken") or data["token"].get("token")
                expires_in = data["token"].get("expiresIn", 3600)
            else:
                token = data.get("accessToken") or data.get("token") or data.get("access_token")
                expires_in = data.get("expiresIn", 3600)
        else:
            token = data
            expires_in = 3600
        
        if not token:
            raise HTTPException(status_code=500, detail="No token received from Standard API authentication")
        
        _standard_token_cache["token"] = token
        _standard_token_cache["expires_at"] = current_time + max(expires_in - 300, 60)
        
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ------------------
# Toast API Helpers
# ------------------

def to_business_date(d: date) -> str:
    return d.strftime("%Y%m%d")

async def get_all_active_locations(client: httpx.AsyncClient) -> List[Location]:
    url = f"{TOAST_API_BASE_URL}/restaurants-information"
    headers = await get_auth_headers()
    resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Error fetching locations: {resp.text}")
    data = resp.json()
    
    active = [
        Location(restaurantGuid=item["restaurantGuid"], restaurantName=item["restaurantName"])
        for item in data
        if item.get("active") and not item.get("archived") and not item.get("testMode")
    ]
    return active

async def request_toast_report(
    client: httpx.AsyncClient,
    start: date,
    end: date,
    restaurant_ids: List[str],
    report_type: str,
    group_by: List[str]
) -> str:
    days_diff = (end - start).days + 1
    
    # Determine endpoint based on date range and report type
    if days_diff == 1:
        time_range = "day"
    elif days_diff <= 7:
        time_range = "week"
    elif days_diff <= 31:
        time_range = "month"
    elif days_diff <= 366:
        time_range = "year"
    else:
        time_range = None
    
    body = {
        "startBusinessDate": to_business_date(start),
        "endBusinessDate": to_business_date(end),
        "restaurantIds": restaurant_ids,
        "excludedRestaurantIds": [],
    }
    
    # Add groupBy if provided (only supported for day/week time ranges per Toast API)
    if group_by and time_range in ["day", "week"]:
        body["groupBy"] = group_by
    
    # Choose endpoint
    endpoint_base = "metrics" if report_type == "metrics" else "menu"
    
    if time_range and time_range in ["day", "week", "month", "year"]:
        url = f"{TOAST_API_BASE_URL}/{endpoint_base}/{time_range}"
    else:
        url = f"{TOAST_API_BASE_URL}/{endpoint_base}"
    
    logger.info(f"Requesting {report_type} report: URL={url}, Body={body}")
    
    headers = await get_auth_headers()
    resp = await client.post(url, headers=headers, json=body)
    
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Toast API rate limit exceeded. Please wait before trying again.")
    
    if resp.status_code != 200:
        error_msg = resp.text
        try:
            error_json = resp.json()
            error_msg = error_json.get("message", error_json.get("detail", error_msg))
        except:
            pass
        raise HTTPException(status_code=resp.status_code, detail=f"Error creating report: {error_msg}")
    
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip().strip('"')
    
    if isinstance(data, str):
        return data.strip().strip('"')
    
    return data.get("reportRequestGuid") or data.get("guid") or resp.text.strip().strip('"')

async def fetch_toast_report(
    client: httpx.AsyncClient,
    report_guid: str,
    report_type: str,
    max_attempts: int = 60,
    delay_seconds: float = 1.0,
) -> list:
    endpoint_base = "metrics" if report_type == "metrics" else "menu"
    url = f"{TOAST_API_BASE_URL}/{endpoint_base}/{report_guid}"
    params = {"fetchRestaurantNames": "true"}
    
    for attempt in range(max_attempts):
        headers = await get_auth_headers()
        resp = await client.get(url, headers=headers, params=params)
        
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"{report_type.capitalize()} report returned {len(data)} rows")
            return data
        
        if resp.status_code == 202:
            if attempt == 0:
                logger.info(f"Report {report_guid} is still processing, polling...")
            elif attempt % 5 == 0:  # Log every 5th attempt
                logger.info(f"Still waiting for report {report_guid} (attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(delay_seconds)
            # Exponential backoff
            delay_seconds = min(delay_seconds * 1.5, 10.0)
            continue
        
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail="Toast API rate limit exceeded while retrieving report.")
        
        error_msg = resp.text
        try:
            error_json = resp.json()
            error_msg = error_json.get("message", error_json.get("detail", error_msg))
        except:
            pass
        
        raise HTTPException(status_code=resp.status_code, detail=f"Error retrieving report: {error_msg}")
    
    raise HTTPException(status_code=504, detail=f"Timed out waiting for report {report_guid}. Try narrowing the date range.")

# ------------------
# Menus API V3 - Tags Mapping
# ------------------

async def get_menu_tags_mapping(client: httpx.AsyncClient, restaurant_guid: str) -> Dict[str, List[Dict]]:
    """
    Get cached mapping of menuItemGuid -> [{tagGuid, tagName}, ...]
    Uses V3 metadata to check if menu changed
    """
    global _menu_tags_cache
    
    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }
    
    # Check metadata first
    metadata_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/metadata"
    metadata_resp = await client.get(metadata_url, headers=headers_with_guid)
    
    if metadata_resp.status_code != 200:
        # 403 is expected if V3 metadata isn't available - silently fall back to V2
        if metadata_resp.status_code != 403:
            logger.warning(f"Failed to fetch menu metadata: {metadata_resp.status_code}")
        # Fall back to V2 without caching
        return await get_menu_tags_mapping_v2(client, restaurant_guid)
    
    metadata = metadata_resp.json()
    current_hash = metadata.get("hash") or metadata.get("version") or str(metadata)
    
    # Check cache
    if restaurant_guid in _menu_tags_cache:
        cached = _menu_tags_cache[restaurant_guid]
        if cached.get("hash") == current_hash:
            logger.info(f"Menu tags cache HIT for restaurant {restaurant_guid}")
            return cached["mapping"]
    
    logger.info(f"Menu tags cache MISS for restaurant {restaurant_guid}, fetching from API")
    
    # Fetch menus V3
    menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus"
    menus_resp = await client.get(menus_url, headers=headers_with_guid)
    
    if menus_resp.status_code != 200:
        logger.warning(f"Failed to fetch menus V3: {menus_resp.status_code}, falling back to V2")
        return await get_menu_tags_mapping_v2(client, restaurant_guid)
    
    menus_data = menus_resp.json()
    
    # Build mapping: menuItemGuid/modifierGuid -> [{tagGuid, tagName}, ...]
    mapping = {}
    
    # Process menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                if not item_guid:
                    continue
                
                tags = []
                for tag in item.get("itemTags", []):
                    tag_guid = tag.get("guid", "")
                    tag_name = tag.get("name", "")
                    if tag_name:  # Only include if name exists
                        tags.append({
                            "tagGuid": tag_guid,
                            "tagName": tag_name
                        })
                
                if tags:
                    mapping[item_guid] = tags
    
    # Process modifiers from modifierGroupReferences and modifierOptionReferences
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    
    logger.info(f"DEBUG (V3): Found {len(modifier_group_refs)} modifier groups and {len(modifier_option_refs)} modifier option references")
    
    all_modifier_guids_from_menus = set()
    # Process modifier groups
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if not mod_option_guid:
                continue
            
            all_modifier_guids_from_menus.add(mod_option_guid)
            
            # Debug: Check for target modifier
            if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
                logger.info(f"DEBUG (V3): Found target modifier in modifierGroupReferences - guid={mod_option_guid}, name='{mod_option_name}', group_guid={mod_group_guid}")
            
            tags = []
            for tag in mod_option.get("itemTags", []):
                tag_guid = tag.get("guid", "")
                tag_name = tag.get("name", "")
                if tag_name:
                    tags.append({
                        "tagGuid": tag_guid,
                        "tagName": tag_name
                    })
            
            # Include in mapping even if no tags (for GUID lookup)
            if tags:
                mapping[mod_option_guid] = tags
            else:
                mapping[mod_option_guid] = []
    
    # Process modifier option references directly
    # NOTE: modifierOptionReferences keys are numeric IDs, but each option has a "guid" field with the actual UUID
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log structure for target modifier
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.info(f"DEBUG (V3): Modifier option structure - id={mod_option_id}, keys={list(mod_option.keys())}, guid={mod_option_guid}")
            # Check for alternative GUID fields
            for key in ["guid", "modifierOptionGuid", "modifierGuid", "id"]:
                if key in mod_option:
                    logger.info(f"DEBUG (V3):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            # If no guid field, use the key as fallback (might be numeric ID)
            mod_option_guid = str(mod_option_id)
        
        all_modifier_guids_from_menus.add(mod_option_guid)
        
        # Debug: Check for target modifier
        if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
            logger.info(f"DEBUG (V3): Found target modifier in modifierOptionReferences - id={mod_option_id}, guid={mod_option_guid}, name='{mod_option_name}'")
        
        tags = []
        for tag in mod_option.get("itemTags", []):
            tag_guid = tag.get("guid", "")
            tag_name = tag.get("name", "")
            if tag_name:
                tags.append({
                    "tagGuid": tag_guid,
                    "tagName": tag_name
                })
        
        # Include in mapping even if no tags (for GUID lookup)
        # Use the actual GUID, not the numeric ID key
        if tags:
            mapping[mod_option_guid] = tags
        else:
            mapping[mod_option_guid] = []
    
    # Debug: Check if target GUID is in our mapping
    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_guid in mapping:
        logger.info(f"DEBUG (V3): Target GUID {target_guid} IS in mapping with {len(mapping[target_guid])} tags")
    else:
        logger.warning(f"DEBUG (V3): Target GUID {target_guid} NOT in mapping. Total modifier GUIDs from Menus API: {len(all_modifier_guids_from_menus)}")
        # Show some sample GUIDs
        sample_guids = list(all_modifier_guids_from_menus)[:10]
        logger.info(f"DEBUG (V3): Sample modifier GUIDs from Menus API: {sample_guids}")
    
    # Cache it
    _menu_tags_cache[restaurant_guid] = {
        "hash": current_hash,
        "mapping": mapping
    }
    
    # Also build a name-to-GUID mapping for modifiers (fallback matching)
    modifier_name_to_guid = {}
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                name_key = mod_option_name.lower().strip()
                modifier_name_to_guid[name_key] = mod_option_guid
                # Debug: log if this is our target modifier
                if "half black" in name_key and "bleu" in name_key:
                    logger.info(f"DEBUG: Found target modifier in Menus API (V3) - name='{mod_option_name}', guid={mod_option_guid}, normalized='{name_key}'")
    
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        if not mod_option_guid:
            mod_option_guid = str(mod_option_id)  # Fallback to ID if no guid
        
        if mod_option_guid and mod_option_name:
            name_key = mod_option_name.lower().strip()
            modifier_name_to_guid[name_key] = mod_option_guid
            # Debug: log if this is our target modifier
            if "half black" in name_key and "bleu" in name_key:
                logger.info(f"DEBUG: Found target modifier in Menus API (V3 option refs) - name='{mod_option_name}', id={mod_option_id}, guid={mod_option_guid}, normalized='{name_key}'")
    
    logger.info(f"Built menu tags mapping: {len(mapping)} items/modifiers with tags")
    logger.info(f"Built modifier name mapping: {len(modifier_name_to_guid)} modifier names")
    
    # Debug: Check if target modifier is in name mapping
    target_name_variations = [
        "half black & bleu (olo pairing choice)",
        "half black & bleu",
        "half black and bleu"
    ]
    for var in target_name_variations:
        if var in modifier_name_to_guid:
            logger.info(f"DEBUG: Target modifier name variation '{var}' found in name mapping -> {modifier_name_to_guid[var]}")
    
    # Store name mapping in cache for later use
    if restaurant_guid in _menu_tags_cache:
        _menu_tags_cache[restaurant_guid]["name_mapping"] = modifier_name_to_guid
    
    return mapping

async def get_menu_tags_mapping_v2(client: httpx.AsyncClient, restaurant_guid: str) -> Dict[str, List[Dict]]:
    """Fallback to V2 menus API if V3 not available"""
    standard_headers = await get_standard_api_auth_headers()
    headers_with_guid = {
        **standard_headers,
        "Toast-Restaurant-External-ID": restaurant_guid
    }
    
    menus_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
    menus_resp = await client.get(menus_url, headers=headers_with_guid)
    
    if menus_resp.status_code != 200:
        raise HTTPException(status_code=menus_resp.status_code, detail="Failed to fetch menu data")
    
    menus_data = menus_resp.json()
    
    mapping = {}
    
    # Process menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                item_guid = item.get("guid")
                if not item_guid:
                    continue
                
                tags = []
                for tag in item.get("itemTags", []):
                    tag_guid = tag.get("guid", "")
                    tag_name = tag.get("name", "")
                    if tag_name:
                        tags.append({
                            "tagGuid": tag_guid,
                            "tagName": tag_name
                        })
                
                if tags:
                    mapping[item_guid] = tags
    
    # Process modifiers from modifierGroupReferences and modifierOptionReferences
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    
    logger.info(f"DEBUG (V2): Found {len(modifier_group_refs)} modifier groups and {len(modifier_option_refs)} modifier option references")
    
    all_modifier_guids_from_menus = set()
    # Process modifier groups
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            if not mod_option_guid:
                continue
            
            tags = []
            for tag in mod_option.get("itemTags", []):
                tag_guid = tag.get("guid", "")
                tag_name = tag.get("name", "")
                if tag_name:
                    tags.append({
                        "tagGuid": tag_guid,
                        "tagName": tag_name
                    })
            
            if tags:
                mapping[mod_option_guid] = tags
    
    # Process modifier option references directly
    # NOTE: modifierOptionReferences keys are numeric IDs, but each option has a "guid" field with the actual UUID
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log structure for target modifier
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.info(f"DEBUG (V2): Modifier option structure - id={mod_option_id}, keys={list(mod_option.keys())}, guid={mod_option_guid}")
            # Check for alternative GUID fields
            for key in ["guid", "modifierOptionGuid", "modifierGuid", "id", "externalId"]:
                if key in mod_option:
                    logger.info(f"DEBUG (V2):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            # If no guid field, use the key as fallback (might be numeric ID)
            mod_option_guid = str(mod_option_id)
        
        all_modifier_guids_from_menus.add(mod_option_guid)
        
        # Debug: Check for target modifier
        if mod_option_guid == "6d02e1aa-41c4-489e-bd72-90d12d79edf2" or (mod_option_name and "half black" in mod_option_name.lower()):
            logger.info(f"DEBUG (V2): Found target modifier in modifierOptionReferences - id={mod_option_id}, guid={mod_option_guid}, name='{mod_option_name}'")
        
        tags = []
        for tag in mod_option.get("itemTags", []):
            tag_guid = tag.get("guid", "")
            tag_name = tag.get("name", "")
            if tag_name:
                tags.append({
                    "tagGuid": tag_guid,
                    "tagName": tag_name
                })
        
        # Include in mapping even if no tags (for GUID lookup)
        # Use the actual GUID, not the numeric ID key
        if tags:
            mapping[mod_option_guid] = tags
        else:
            mapping[mod_option_guid] = []
    
    # Debug: Check if target GUID is in our mapping
    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_guid in mapping:
        logger.info(f"DEBUG (V2): Target GUID {target_guid} IS in mapping with {len(mapping[target_guid])} tags")
    else:
        logger.warning(f"DEBUG (V2): Target GUID {target_guid} NOT in mapping. Total modifier GUIDs from Menus API: {len(all_modifier_guids_from_menus)}")
        # Show some sample GUIDs
        sample_guids = list(all_modifier_guids_from_menus)[:10]
        logger.info(f"DEBUG (V2): Sample modifier GUIDs from Menus API: {sample_guids}")
    
    # Also build a name-to-GUID mapping for modifiers (fallback matching)
    modifier_name_to_guid = {}
    for mod_group_guid, mod_group in modifier_group_refs.items():
        modifier_options = mod_group.get("modifierOptions", [])
        for mod_option in modifier_options:
            mod_option_guid = mod_option.get("guid")
            mod_option_name = mod_option.get("name", "")
            if mod_option_guid and mod_option_name:
                modifier_name_to_guid[mod_option_name.lower().strip()] = mod_option_guid
    
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")  # Get the actual GUID from the option object
        mod_option_name = mod_option.get("name", "")
        
        # Debug: Log full structure for target modifier to find UUID
        if mod_option_name and "half black" in mod_option_name.lower() and "bleu" in mod_option_name.lower():
            logger.info(f"DEBUG (V2 name mapping): Modifier option full structure - id={mod_option_id}")
            logger.info(f"DEBUG (V2 name mapping):   All keys: {list(mod_option.keys())}")
            for key in mod_option.keys():
                logger.info(f"DEBUG (V2 name mapping):   {key}={mod_option.get(key)}")
        
        if not mod_option_guid:
            mod_option_guid = str(mod_option_id)  # Fallback to ID if no guid
        
        if mod_option_guid and mod_option_name:
            name_key = mod_option_name.lower().strip()
            modifier_name_to_guid[name_key] = mod_option_guid
            # Debug: log if this is our target modifier
            if "half black" in name_key and "bleu" in name_key:
                logger.info(f"DEBUG (V2 name mapping): Added to name mapping - name='{mod_option_name}', normalized='{name_key}', guid={mod_option_guid}")
    
    logger.info(f"Built modifier name mapping (V2): {len(modifier_name_to_guid)} modifier names")
    
    return mapping

def aggregate_quantities_by_tag(
    analytics_rows: List[dict],
    tags_mapping: Dict[str, List[Dict]],
    attribution_mode: str = "MULTI_COUNT"
) -> List[dict]:
    """
    Aggregate analytics rows by tag
    
    attribution_mode:
    - MULTI_COUNT: each tag gets full quantity
    - SPLIT: divide quantity evenly across tags
    - PRIMARY_ONLY: use only first tag
    """
    aggregation = {}
    untagged_count = 0
    
    for row in analytics_rows:
        restaurant_guid = row.get("restaurantGuid", "")
        restaurant_name = row.get("restaurantName", "Unknown")
        business_date = row.get("businessDate", "")
        menu_item_guid = row.get("menuItemGuid", "")
        quantity_sold = row.get("quantitySold", 0)
        
        if not menu_item_guid or menu_item_guid not in tags_mapping:
            # Untagged item
            untagged_count += 1
            key = f"{restaurant_guid}|{business_date}|UNTAGGED"
            
            if key not in aggregation:
                aggregation[key] = {
                    "restaurantGuid": restaurant_guid,
                    "restaurantName": restaurant_name,
                    "businessDate": business_date,
                    "tagGuid": "UNTAGGED",
                    "tagName": "Untagged",
                    "quantitySold": 0
                }
            
            aggregation[key]["quantitySold"] += quantity_sold
            continue
        
        # Get tags for this item
        tags = tags_mapping[menu_item_guid]
        
        if attribution_mode == "PRIMARY_ONLY":
            tags = tags[:1]  # Only first tag
        
        # Calculate quantity per tag
        if attribution_mode == "SPLIT" and len(tags) > 1:
            qty_per_tag = quantity_sold / len(tags)
        else:
            qty_per_tag = quantity_sold
        
        for tag in tags:
            tag_guid = tag["tagGuid"]
            tag_name = tag["tagName"]
            
            key = f"{restaurant_guid}|{business_date}|{tag_guid}"
            
            if key not in aggregation:
                aggregation[key] = {
                    "restaurantGuid": restaurant_guid,
                    "restaurantName": restaurant_name,
                    "businessDate": business_date,
                    "tagGuid": tag_guid,
                    "tagName": tag_name,
                    "quantitySold": 0
                }
            
            aggregation[key]["quantitySold"] += qty_per_tag
    
    logger.info(f"Aggregation complete: {len(aggregation)} tag groups, {untagged_count} untagged items")
    
    return list(aggregation.values())

# ------------------
# User Storage & Authentication
# ------------------

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

def load_users() -> Dict[str, dict]:
    """Load users from JSON file"""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading users: {e}")
        return {}

def save_users(users: Dict[str, dict]) -> None:
    """Save users to JSON file"""
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving users: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save users: {str(e)}")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt directly"""
    # Password should already be truncated, but double-check to be safe
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password = truncate_password_to_72_bytes(password)
        password_bytes = password.encode('utf-8')
    
    # Use bcrypt directly
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password using bcrypt directly"""
    # Truncate if needed (same as hash_password)
    plain_password = truncate_password_to_72_bytes(plain_password)
    password_bytes = plain_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hashed_bytes)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    """Get current authenticated user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    users = load_users()
    user_data = users.get(user_id)
    if user_data is None:
        raise credentials_exception
    
    return User(**{k: v for k, v in user_data.items() if k != "password"})

async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Get current user and verify they are an admin"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# ------------------
# Initialize Super Admin (if configured)
# ------------------

def initialize_super_admin():
    """Create super admin user if configured and doesn't exist"""
    if not SUPER_ADMIN_EMAIL or not SUPER_ADMIN_PASSWORD:
        return
    
    users = load_users()
    
    # Check if admin already exists
    for user_id, user in users.items():
        if user.get("email", "").lower() == SUPER_ADMIN_EMAIL:
            # Update existing admin if password changed
            if SUPER_ADMIN_PASSWORD:
                user["password"] = hash_password(truncate_password_to_72_bytes(SUPER_ADMIN_PASSWORD))
                user["role"] = "admin"
                users[user_id] = user
                save_users(users)
                logger.info(f"Super admin updated: {SUPER_ADMIN_EMAIL}")
            return
    
    # Create new super admin
    user_id = str(uuid.uuid4())
    hashed_password = hash_password(truncate_password_to_72_bytes(SUPER_ADMIN_PASSWORD))
    
    admin_user = {
        "id": user_id,
        "email": SUPER_ADMIN_EMAIL,
        "name": SUPER_ADMIN_NAME,
        "password": hashed_password,
        "createdAt": datetime.now().isoformat(),
        "lastLogin": None,
        "preferences": {},
        "role": "admin"
    }
    
    users[user_id] = admin_user
    save_users(users)
    logger.info(f"Super admin created: {SUPER_ADMIN_EMAIL}")

# Note: initialize_super_admin() is called at the end of the file
# after truncate_password_to_72_bytes is defined

# ------------------
# Saved Reports Storage
# ------------------

SAVED_REPORTS_FILE = os.path.join(os.path.dirname(__file__), "saved_reports.json")

def load_saved_reports() -> List[SavedReport]:
    if not os.path.exists(SAVED_REPORTS_FILE):
        default_report = SavedReport(
            id=str(uuid.uuid4()),
            name="Net Sales by Location - Weekly",
            description="Weekly net sales breakdown by location",
            createdBy="System",
            createdAt=datetime.now().isoformat(),
            lastModified=datetime.now().isoformat(),
            config=ReportConfig(
                reportType="metrics",
                dateRangeType="last_7_days",
                restaurantIds=[],
                groupBy=[]
            )
        )
        save_saved_reports([default_report])
        return [default_report]
    
    try:
        with open(SAVED_REPORTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return [SavedReport(**item) for item in data]
    except Exception as e:
        logger.error(f"Error loading saved reports: {e}")
        return []

def save_saved_reports(reports: List[SavedReport]) -> None:
    try:
        with open(SAVED_REPORTS_FILE, 'w', encoding='utf-8') as f:
            json.dump([r.dict() for r in reports], f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving reports: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save reports: {str(e)}")

# ------------------
# Report Data Transformation
# ------------------

def pivot_metrics_by_location_and_date(raw_rows: list) -> dict:
    """
    Transform metrics data into pivot table format:
    - Rows: Restaurant locations
    - Columns: Business dates
    - Values: Net sales
    
    Returns:
    {
        "pivotTable": [
            {"restaurantName": "Location A", "20251210": 1000, "20251211": 1200, ...},
            ...
        ],
        "totals": [
            {"date": "20251210", "totalNetSales": 5000},
            ...
        ]
    }
    """
    if not raw_rows:
        return {"pivotTable": [], "totals": []}
    
    location_data = {}
    all_dates = set()
    date_totals = {}  # {date: total_net_sales}
    
    for row in raw_rows:
        restaurant_name = row.get("restaurantName", "Unknown")
        business_date = row.get("businessDate", "")
        # Toast API returns netSalesAmount, not netSales
        net_sales = row.get("netSalesAmount", 0) or 0
        
        if restaurant_name not in location_data:
            location_data[restaurant_name] = {}
        
        location_data[restaurant_name][business_date] = net_sales
        all_dates.add(business_date)
        
        # Accumulate totals by date
        if business_date not in date_totals:
            date_totals[business_date] = 0
        date_totals[business_date] += net_sales
    
    sorted_dates = sorted(list(all_dates))
    sorted_locations = sorted(location_data.keys())
    
    # Build pivot rows
    pivot_rows = []
    for location in sorted_locations:
        row_data = {"restaurantName": location}
        for date in sorted_dates:
            row_data[date] = location_data[location].get(date, 0)
        pivot_rows.append(row_data)
    
    # Build totals section
    totals_rows = [
        {"date": date, "totalNetSales": date_totals[date]}
        for date in sorted_dates
    ]
    
    return {
        "pivotTable": pivot_rows,
        "totals": totals_rows
    }

def pivot_menu_items_by_tag(
    analytics_rows: List[dict],
    tags_mapping: Dict[str, List[Dict]],
    selected_tags: List[str],
    item_names_mapping: Dict[str, str] = None,
    menu_item_guids: set = None
) -> dict:
    """
    Transform menu items into pivot table format by restaurant and tag.
    
    Returns:
    {
        "pivotTable": [
            {"restaurantName": "Location A", "Tag1": 100, "Tag2": 50, ...},
            ...
        ],
        "totals": [
            {"itemTagName": "Tag1", "totalQty": 175},
            ...
        ],
        "itemBreakdown": [
            {"itemTagName": "Tag1", "menuItemGuid": "guid1", "menuItemName": "Item Name", "totalQty": 50},
            ...
        ]
    }
    """
    if not analytics_rows:
        return {"pivotTable": [], "totals": [], "itemBreakdown": []}
    
    if item_names_mapping is None:
        item_names_mapping = {}
    
    if menu_item_guids is None:
        menu_item_guids = set()
    
    # Aggregate by restaurant + tag
    restaurant_tag_data = {}  # {restaurantGuid: {restaurantName: str, tags: {tagName: qty}}}
    tag_totals = {}  # {tagName: total_qty}
    tag_item_breakdown = {}  # {tagName: {menuItemGuid: total_qty}}
    
    modifier_count_in_pivot = 0
    modifier_count_in_breakdown = 0
    skipped_no_tags = 0
    
    for row in analytics_rows:
        restaurant_guid = row.get("restaurantGuid", "")
        restaurant_name = row.get("restaurantName", "Unknown")
        menu_item_guid = row.get("menuItemGuid", "")
        menu_item_name = row.get("menuItemName", "") or item_names_mapping.get(menu_item_guid, f"Item {menu_item_guid[:8]}")
        quantity_sold = row.get("quantitySold", 0) or 0
        
        if not menu_item_guid:
            continue
        
        if menu_item_guid not in tags_mapping:
            skipped_no_tags += 1
            # Log first few skipped items to debug
            if skipped_no_tags <= 3:
                logger.debug(f"Skipping row: guid={menu_item_guid}, name='{menu_item_name}' - not in tags_mapping")
            continue
        
        # Determine if this is a modifier (not in the original menu_item_guids set)
        is_modifier = menu_item_guid not in menu_item_guids
        
        # Get tags for this item (works for both menu items and modifiers)
        item_tags = tags_mapping[menu_item_guid]
        
        # Filter to only selected tags (if any selected)
        if selected_tags:
            item_tags = [t for t in item_tags if t["tagName"] in selected_tags]
            if not item_tags:
                continue
        
        # Initialize restaurant data if needed (only for menu items)
        if not is_modifier:
            if restaurant_guid not in restaurant_tag_data:
                restaurant_tag_data[restaurant_guid] = {
                    "restaurantName": restaurant_name,
                    "tags": {}
                }
        
        # Add quantity to each tag (MULTI_COUNT mode - each tag gets full quantity)
        # Process modifiers and menu items the SAME way for breakdown
        for tag in item_tags:
            tag_name = tag["tagName"]
            
            # Add to restaurant-tag aggregation (pivot table) - only for menu items
            if not is_modifier:
                if tag_name not in restaurant_tag_data[restaurant_guid]["tags"]:
                    restaurant_tag_data[restaurant_guid]["tags"][tag_name] = 0
                restaurant_tag_data[restaurant_guid]["tags"][tag_name] += quantity_sold
                
                # Add to totals (pivot table totals) - only for menu items
                if tag_name not in tag_totals:
                    tag_totals[tag_name] = 0
                tag_totals[tag_name] += quantity_sold
            
            # Track item-level breakdown by tag (SAME for both menu items and modifiers)
            if tag_name not in tag_item_breakdown:
                tag_item_breakdown[tag_name] = {}
            if menu_item_guid not in tag_item_breakdown[tag_name]:
                tag_item_breakdown[tag_name][menu_item_guid] = {
                    "menuItemName": menu_item_name,
                    "totalQty": 0
                }
            tag_item_breakdown[tag_name][menu_item_guid]["totalQty"] += quantity_sold
            
            if is_modifier:
                modifier_count_in_breakdown += 1
    
    # Build pivot table
    # Get all unique tag names (sorted)
    all_tag_names = sorted(set(tag_totals.keys()))
    
    # Build pivot rows
    pivot_rows = []
    for restaurant_guid, data in sorted(restaurant_tag_data.items(), key=lambda x: x[1]["restaurantName"]):
        row = {"restaurantName": data["restaurantName"]}
        for tag_name in all_tag_names:
            row[tag_name] = data["tags"].get(tag_name, 0)
        pivot_rows.append(row)
    
    # Build totals section
    totals_rows = [
        {"itemTagName": tag_name, "totalQty": tag_totals[tag_name]}
        for tag_name in all_tag_names
    ]
    
    # Build item breakdown section
    item_breakdown_rows = []
    modifier_breakdown_totals = {}  # Track modifier totals across all tags for debugging
    for tag_name in all_tag_names:
        if tag_name in tag_item_breakdown:
            for menu_item_guid, item_data in sorted(tag_item_breakdown[tag_name].items(), 
                                                     key=lambda x: x[1]["totalQty"], 
                                                     reverse=True):
                item_breakdown_rows.append({
                    "itemTagName": tag_name,
                    "menuItemGuid": menu_item_guid,
                    "menuItemName": item_data["menuItemName"],
                    "totalQty": item_data["totalQty"]
                })
                
                # Track modifier totals for debugging
                if menu_item_guid not in menu_item_guids:
                    if menu_item_guid not in modifier_breakdown_totals:
                        modifier_breakdown_totals[menu_item_guid] = {
                            "name": item_data["menuItemName"],
                            "totalQty": 0,
                            "tags": []
                        }
                    modifier_breakdown_totals[menu_item_guid]["totalQty"] += item_data["totalQty"]
                    modifier_breakdown_totals[menu_item_guid]["tags"].append(tag_name)
    
    # Log modifier totals for debugging
    target_modifier_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
    if target_modifier_guid in modifier_breakdown_totals:
        mod_info = modifier_breakdown_totals[target_modifier_guid]
        logger.info(f"TARGET MODIFIER in breakdown: guid={target_modifier_guid}, name='{mod_info['name']}'")
        logger.info(f"  Total quantity across all tags: {mod_info['totalQty']}")
        logger.info(f"  Appears in {len(mod_info['tags'])} tags: {mod_info['tags']}")
        # Log per-tag breakdown
        for tag_name in mod_info['tags']:
            if tag_name in tag_item_breakdown and target_modifier_guid in tag_item_breakdown[tag_name]:
                tag_qty = tag_item_breakdown[tag_name][target_modifier_guid]["totalQty"]
                logger.info(f"  -> Tag '{tag_name}': {tag_qty}")
    
    logger.info(f"Pivot processing: {modifier_count_in_pivot} modifiers excluded from pivot totals, {modifier_count_in_breakdown} modifier entries in breakdown, {skipped_no_tags} rows skipped (not in tags_mapping)")
    
    return {
        "pivotTable": pivot_rows,
        "totals": totals_rows,
        "itemBreakdown": item_breakdown_rows
    }

# ------------------
# FastAPI App
# ------------------

app = FastAPI(title="Newk's Custom Reporting Platform")

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------
# Core Endpoints
# ------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2025-12-17-analytics-tags"}

# ------------------
# Authentication Endpoints
# ------------------

def truncate_password_to_72_bytes(password: str) -> str:
    """Truncate password to 72 bytes maximum for bcrypt compatibility"""
    password_bytes = password.encode('utf-8')
    if len(password_bytes) <= 72:
        return password
    
    # Truncate to exactly 72 bytes
    password_bytes = password_bytes[:72]
    # Decode back, handling potential incomplete UTF-8 sequences
    try:
        return password_bytes.decode('utf-8')
    except UnicodeDecodeError:
        # If we truncated in the middle of a multi-byte character, remove incomplete bytes
        while len(password_bytes) > 0:
            try:
                return password_bytes.decode('utf-8')
            except UnicodeDecodeError:
                password_bytes = password_bytes[:-1]
        return ""

@app.post("/api/auth/register", response_model=Token)
async def register(user_data: UserCreate = Body(...)):
    """Register a new user"""
    try:
        # Truncate password to 72 bytes BEFORE validation (bcrypt requirement)
        original_password = user_data.password
        user_data.password = truncate_password_to_72_bytes(user_data.password)
        
        if len(user_data.password) < 6:
            logger.warning(f"Registration attempt with password too short: {len(user_data.password)} chars")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 6 characters long."
            )
        
        if not user_data.name or len(user_data.name.strip()) == 0:
            logger.warning("Registration attempt without name")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Name is required."
            )
        
        users = load_users()
        
        # Check if user already exists
        for user_id, user in users.items():
            if user.get("email", "").lower() == user_data.email.lower():
                logger.warning(f"Registration attempt with existing email: {user_data.email}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered"
                )
        
        # Create new user
        user_id = str(uuid.uuid4())
        hashed_password = hash_password(user_data.password)
        
        new_user = {
            "id": user_id,
            "email": user_data.email.lower(),
            "name": user_data.name.strip(),
            "password": hashed_password,
            "createdAt": datetime.now().isoformat(),
            "lastLogin": None,
            "preferences": {},
            "role": "user"  # Default role
        }
        
        users[user_id] = new_user
        save_users(users)
        
        # Create access token
        access_token = create_access_token(data={"sub": user_id})
        
        user_obj = User(**{k: v for k, v in new_user.items() if k != "password"})
        
        logger.info(f"New user registered: {user_data.email}")
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user_obj
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during registration: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )

@app.post("/api/auth/login", response_model=Token)
async def login(login_data: UserLogin = Body(...)):
    """Login and get access token"""
    # Truncate password to 72 bytes (bcrypt requirement)
    login_data.password = truncate_password_to_72_bytes(login_data.password)
    
    users = load_users()
    
    # Find user by email
    user_id = None
    user_data = None
    for uid, user in users.items():
        if user.get("email", "").lower() == login_data.email.lower():
            user_id = uid
            user_data = user
            break
    
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    # Verify password
    if not verify_password(login_data.password, user_data.get("password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    # Update last login
    user_data["lastLogin"] = datetime.now().isoformat()
    users[user_id] = user_data
    save_users(users)
    
    # Create access token
    access_token = create_access_token(data={"sub": user_id})
    
    user_obj = User(**{k: v for k, v in user_data.items() if k != "password"})
    
    logger.info(f"User logged in: {login_data.email}")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_obj
    }

@app.get("/api/auth/me", response_model=User)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    return current_user

@app.put("/api/auth/me", response_model=User)
async def update_current_user(
    user_update: UserUpdate = Body(...),
    current_user: User = Depends(get_current_user)
):
    """Update current user information"""
    users = load_users()
    user_data = users.get(current_user.id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update fields
    if user_update.name is not None:
        user_data["name"] = user_update.name
    if user_update.preferences is not None:
        user_data["preferences"] = user_update.preferences
    
    users[current_user.id] = user_data
    save_users(users)
    
    return User(**{k: v for k, v in user_data.items() if k != "password"})

# ------------------
# Admin Endpoints
# ------------------

@app.get("/api/admin/users", response_model=List[User])
async def list_all_users(admin_user: User = Depends(get_current_admin_user)):
    """List all users (admin only)"""
    users = load_users()
    return [User(**{k: v for k, v in user.items() if k != "password"}) for user in users.values()]

@app.post("/api/admin/users", response_model=User)
async def create_user(
    user_data: UserCreate = Body(...),
    admin_user: User = Depends(get_current_admin_user)
):
    """Create a new user (admin only)"""
    # Truncate password
    user_data.password = truncate_password_to_72_bytes(user_data.password)
    
    if len(user_data.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters long."
        )
    
    users = load_users()
    
    # Check if user already exists
    for user_id, user in users.items():
        if user.get("email", "").lower() == user_data.email.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
    
    # Create new user
    user_id = str(uuid.uuid4())
    hashed_password = hash_password(user_data.password)
    
    new_user = {
        "id": user_id,
        "email": user_data.email.lower(),
        "name": user_data.name.strip(),
        "password": hashed_password,
        "createdAt": datetime.now().isoformat(),
        "lastLogin": None,
        "preferences": {},
        "role": user_data.role if user_data.role in ["user", "admin"] else "user"
    }
    
    users[user_id] = new_user
    save_users(users)
    
    logger.info(f"Admin {admin_user.email} created user: {user_data.email}")
    return User(**{k: v for k, v in new_user.items() if k != "password"})

@app.put("/api/admin/users/{user_id}/role", response_model=User)
async def update_user_role(
    user_id: str,
    role: str = Body(..., embed=True),
    admin_user: User = Depends(get_current_admin_user)
):
    """Update a user's role (admin only)"""
    if role not in ["user", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be 'user' or 'admin'"
        )
    
    users = load_users()
    user_data = users.get(user_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_data["role"] = role
    users[user_id] = user_data
    save_users(users)
    
    logger.info(f"Admin {admin_user.email} updated user {user_data.get('email')} role to {role}")
    return User(**{k: v for k, v in user_data.items() if k != "password"})

@app.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    admin_user: User = Depends(get_current_admin_user)
):
    """Delete a user (admin only)"""
    users = load_users()
    user_data = users.get(user_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prevent deleting yourself
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account"
        )
    
    # Delete the user
    del users[user_id]
    save_users(users)
    
    logger.info(f"Admin {admin_user.email} deleted user {user_data.get('email')}")
    return {"message": "User deleted successfully"}

@app.get("/locations", response_model=List[Location])
async def list_locations():
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await get_all_active_locations(client)

# ------------------
# Saved Reports API
# ------------------

@app.get("/api/saved-reports")
async def get_saved_reports():
    return load_saved_reports()

@app.get("/api/saved-reports/{report_id}")
async def get_saved_report(report_id: str):
    reports = load_saved_reports()
    report = next((r for r in reports if r.id == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report

@app.post("/api/saved-reports")
async def create_saved_report(report: SavedReportCreate = Body(...)):
    reports = load_saved_reports()
    
    if any(r.name.lower() == report.name.lower() for r in reports):
        raise HTTPException(status_code=400, detail="A report with this name already exists")
    
    new_report = SavedReport(
        id=str(uuid.uuid4()),
        name=report.name,
        description=report.description or "",
        createdBy=report.createdBy,
        createdAt=datetime.now().isoformat(),
        lastModified=datetime.now().isoformat(),
        config=report.config
    )
    reports.append(new_report)
    save_saved_reports(reports)
    
    return new_report

@app.put("/api/saved-reports/{report_id}")
async def update_saved_report(report_id: str, report: SavedReportCreate = Body(...)):
    reports = load_saved_reports()
    
    index = None
    for i, r in enumerate(reports):
        if r.id == report_id:
            index = i
            break
    
    if index is None:
        raise HTTPException(status_code=404, detail="Report not found")
    
    if any(r.name.lower() == report.name.lower() and r.id != report_id for r in reports):
        raise HTTPException(status_code=400, detail="A report with this name already exists")
    
    updated_report = SavedReport(
        id=report_id,
        name=report.name,
        description=report.description or "",
        createdBy=reports[index].createdBy,
        createdAt=reports[index].createdAt,
        lastModified=datetime.now().isoformat(),
        config=report.config
    )
    reports[index] = updated_report
    save_saved_reports(reports)
    
    return updated_report

@app.delete("/api/saved-reports/{report_id}")
async def delete_saved_report(report_id: str):
    reports = load_saved_reports()
    original_count = len(reports)
    reports = [r for r in reports if r.id != report_id]
    
    if len(reports) == original_count:
        raise HTTPException(status_code=404, detail="Report not found")
    
    save_saved_reports(reports)
    return {"message": "Report deleted successfully"}

# ------------------
# Report Execution API
# ------------------

@app.post("/api/reports/run")
async def run_report(report_config: ReportConfig = Body(...)):
    """Run a report with the given configuration"""
    
    # Calculate date range
    if report_config.dateRangeType == "custom":
        if not report_config.customStartDate or not report_config.customEndDate:
            raise HTTPException(status_code=400, detail="Custom date range requires start and end dates")
        start_date = date.fromisoformat(report_config.customStartDate)
        end_date = date.fromisoformat(report_config.customEndDate)
    elif report_config.dateRangeType == "last_7_days":
        end_date = date.today()
        start_date = end_date - timedelta(days=7)
    elif report_config.dateRangeType == "last_30_days":
        end_date = date.today()
        start_date = end_date - timedelta(days=30)
    elif report_config.dateRangeType == "last_90_days":
        end_date = date.today()
        start_date = end_date - timedelta(days=90)
    else:
        raise HTTPException(status_code=400, detail="Invalid date range type")
    
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get restaurant IDs
            restaurant_ids = report_config.restaurantIds
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                raise HTTPException(status_code=400, detail="No restaurants available")
            
            # Special handling for menu reports with tag filtering
            # Use Analytics API with groupBy: MENU_ITEM + join to tags
            if report_config.reportType == "menu" and report_config.itemTags and len(report_config.itemTags) > 0:
                logger.info(f"Using Analytics API with MENU_ITEM groupBy for tag filtering: {report_config.itemTags}")
                
                # Get menu tags mapping - try multiple restaurants to get complete coverage
                # Start with first restaurant, then try a few more if needed
                all_tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
                logger.info(f"Initial menu mapping from first restaurant: {len(all_tags_mapping)} items")
                
                # Get modifier name mapping from cache (for fallback matching)
                modifier_name_to_guid = {}
                if restaurant_ids[0] in _menu_tags_cache and "name_mapping" in _menu_tags_cache[restaurant_ids[0]]:
                    modifier_name_to_guid = _menu_tags_cache[restaurant_ids[0]]["name_mapping"]
                    logger.info(f"Loaded modifier name mapping: {len(modifier_name_to_guid)} modifier names")
                    # Debug: Check if target name is in mapping
                    target_name_key = "half black & bleu (olo pairing choice)"
                    if target_name_key in modifier_name_to_guid:
                        logger.info(f"DEBUG: Target name '{target_name_key}' found in name mapping -> {modifier_name_to_guid[target_name_key]}")
                    else:
                        # Check for similar names
                        similar = [n for n in modifier_name_to_guid.keys() if "half black" in n and "bleu" in n]
                        logger.info(f"DEBUG: Target name not found. Similar names: {similar[:5]}")
                
                # Try a few more restaurants to catch items that might not be in the first one
                # Limit to 3 additional restaurants to avoid too many API calls
                restaurants_to_check = min(4, len(restaurant_ids))
                for i in range(1, restaurants_to_check):
                    try:
                        additional_mapping = await get_menu_tags_mapping(client, restaurant_ids[i])
                        # Merge mappings (items from later restaurants will override if same GUID)
                        for item_guid, tags in additional_mapping.items():
                            if item_guid not in all_tags_mapping:
                                all_tags_mapping[item_guid] = tags
                        logger.info(f"After checking restaurant {i+1}/{restaurants_to_check}: {len(all_tags_mapping)} total items in mapping")
                        
                        # Also merge name mappings
                        if restaurant_ids[i] in _menu_tags_cache and "name_mapping" in _menu_tags_cache[restaurant_ids[i]]:
                            additional_name_mapping = _menu_tags_cache[restaurant_ids[i]]["name_mapping"]
                            modifier_name_to_guid.update(additional_name_mapping)
                    except Exception as e:
                        logger.warning(f"Failed to get menu mapping from restaurant {restaurant_ids[i]}: {e}")
                        continue
                
                # Filter mapping to only selected tags
                filtered_mapping = {}
                for item_guid, tags in all_tags_mapping.items():
                    matching_tags = [t for t in tags if t["tagName"] in report_config.itemTags]
                    if matching_tags:
                        filtered_mapping[item_guid] = matching_tags
                
                logger.info(f"Filtered to {len(filtered_mapping)} items with selected tags")
                
                if len(filtered_mapping) == 0:
                    return {"pivotTable": [], "totals": []}
                
                # Split date range into week-sized chunks if needed (groupBy only works for day/week)
                days_diff = (end_date - start_date).days + 1
                all_analytics_rows = []
                
                if days_diff > 7:
                    # Split into week-sized chunks
                    logger.info(f"Splitting {days_diff} day range into week-sized chunks for groupBy support")
                    current_start = start_date
                    while current_start <= end_date:
                        # Calculate chunk end (max 7 days)
                        chunk_end = min(current_start + timedelta(days=6), end_date)
                        
                        logger.info(f"Requesting chunk: {current_start} to {chunk_end}")
                        report_guid = await request_toast_report(
                            client,
                            current_start,
                            chunk_end,
                            restaurant_ids,
                            "menu",
                            ["MENU_ITEM"]
                        )
                        
                        logger.info(f"Fetching report for chunk {current_start} to {chunk_end} (guid: {report_guid})")
                        chunk_rows = await fetch_toast_report(client, report_guid, "menu")
                        if chunk_rows:
                            all_analytics_rows.extend(chunk_rows)
                        
                        # Move to next chunk
                        current_start = chunk_end + timedelta(days=1)
                else:
                    # Single request for ranges <= 7 days
                    report_guid = await request_toast_report(
                        client,
                        start_date,
                        end_date,
                        restaurant_ids,
                        "menu",
                        ["MENU_ITEM"]
                    )
                    
                    all_analytics_rows = await fetch_toast_report(client, report_guid, "menu")
                
                # Track count before adding modifiers
                menu_item_count = len(all_analytics_rows)
                
                # Also request modifier-level data
                logger.info("Requesting Analytics menu report with MODIFIER groupBy...")
                
                if days_diff > 7:
                    # Split into week-sized chunks for modifiers too
                    logger.info(f"Splitting modifier data into week-sized chunks")
                    current_start = start_date
                    while current_start <= end_date:
                        chunk_end = min(current_start + timedelta(days=6), end_date)
                        
                        logger.info(f"Requesting MODIFIER chunk: {current_start} to {chunk_end}")
                        modifier_report_guid = await request_toast_report(
                            client,
                            current_start,
                            chunk_end,
                            restaurant_ids,
                            "menu",
                            ["MODIFIER"]  # Group by modifier instead
                        )
                        
                        logger.info(f"Fetching MODIFIER report for chunk {current_start} to {chunk_end} (guid: {modifier_report_guid})")
                        modifier_chunk_rows = await fetch_toast_report(client, modifier_report_guid, "menu")
                        if modifier_chunk_rows:
                            # Log sample row to see what fields are returned
                            if len(modifier_chunk_rows) > 0:
                                logger.info(f"Sample MODIFIER row keys: {list(modifier_chunk_rows[0].keys())}")
                            
                            # Rename the field from modifierGuid to menuItemGuid for consistency
                            for row in modifier_chunk_rows:
                                # Handle different possible field names
                                if "modifierGuid" in row:
                                    row["menuItemGuid"] = row.pop("modifierGuid")
                                elif "modifierOptionGuid" in row:
                                    row["menuItemGuid"] = row.pop("modifierOptionGuid")
                                
                                if "modifierName" in row:
                                    row["menuItemName"] = row.pop("modifierName")
                                elif "modifierOptionName" in row:
                                    row["menuItemName"] = row.pop("modifierOptionName")
                            
                            # Before adding, check for duplicates with menu items
                            menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                            deduplicated_chunk = [
                                r for r in modifier_chunk_rows 
                                if r.get("menuItemGuid") not in menu_item_guids_set
                            ]
                            if len(deduplicated_chunk) < len(modifier_chunk_rows):
                                logger.info(f"Removed {len(modifier_chunk_rows) - len(deduplicated_chunk)} duplicate modifiers from chunk {current_start} to {chunk_end}")
                            all_analytics_rows.extend(deduplicated_chunk)
                        
                        current_start = chunk_end + timedelta(days=1)
                else:
                    # Single request for ranges <= 7 days
                    modifier_report_guid = await request_toast_report(
                        client,
                        start_date,
                        end_date,
                        restaurant_ids,
                        "menu",
                        ["MODIFIER"]
                    )
                    
                    logger.info(f"Fetching MODIFIER report (guid: {modifier_report_guid})")
                    modifier_rows = await fetch_toast_report(client, modifier_report_guid, "menu")
                    if modifier_rows:
                        # Log sample row to see what fields are returned
                        if len(modifier_rows) > 0:
                            logger.info(f"Sample MODIFIER row keys: {list(modifier_rows[0].keys())}")
                        
                        # Rename fields for consistency
                        for row in modifier_rows:
                            # Handle different possible field names
                            if "modifierGuid" in row:
                                row["menuItemGuid"] = row.pop("modifierGuid")
                            elif "modifierOptionGuid" in row:
                                row["menuItemGuid"] = row.pop("modifierOptionGuid")
                            
                            if "modifierName" in row:
                                row["menuItemName"] = row.pop("modifierName")
                            elif "modifierOptionName" in row:
                                row["menuItemName"] = row.pop("modifierOptionName")
                        
                        # Before adding, check for duplicates with menu items
                        menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                        deduplicated_modifier_rows = [
                            r for r in modifier_rows 
                            if r.get("menuItemGuid") not in menu_item_guids_set
                        ]
                        if len(deduplicated_modifier_rows) < len(modifier_rows):
                            logger.info(f"Removed {len(modifier_rows) - len(deduplicated_modifier_rows)} duplicate modifiers from single request")
                        all_analytics_rows.extend(deduplicated_modifier_rows)
                
                logger.info(f"Total rows after adding modifiers: {len(all_analytics_rows)} (was {menu_item_count} menu items, added {len(all_analytics_rows) - menu_item_count} modifiers)")
                
                # CRITICAL: Deduplicate across ALL chunks to prevent cross-chunk duplicates
                # When splitting date ranges, the same sale might appear in multiple chunks
                logger.info(f"Total rows before cross-chunk deduplication: {len(all_analytics_rows)}")
                
                # Store the original menu item GUIDs before deduplication
                original_menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                
                # Deduplicate across ALL rows using (restaurantGuid, businessDate, menuItemGuid, quantitySold) as key
                seen = set()
                deduplicated_rows = []
                for row in all_analytics_rows:
                    key = (
                        row.get("restaurantGuid"),
                        row.get("businessDate"),
                        row.get("menuItemGuid"),
                        row.get("quantitySold", 0)
                    )
                    if key not in seen:
                        seen.add(key)
                        deduplicated_rows.append(row)
                
                duplicates_removed = len(all_analytics_rows) - len(deduplicated_rows)
                if duplicates_removed > 0:
                    logger.warning(f"Removed {duplicates_removed} cross-chunk duplicates")
                all_analytics_rows = deduplicated_rows
                logger.info(f"Total rows after cross-chunk deduplication: {len(all_analytics_rows)}")
                
                # After deduplication, recalculate menu_item_count
                # Count how many deduplicated rows are menu items (those with GUIDs from original menu items)
                menu_item_count = len([r for r in all_analytics_rows if r.get("menuItemGuid") in original_menu_item_guids])
                
                # DEBUG: Check for duplicate GUIDs across menu items and modifiers
                menu_item_guids_section = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                modifier_guids_section = {r.get("menuItemGuid") for r in all_analytics_rows[menu_item_count:] if r.get("menuItemGuid")}
                duplicate_guids = menu_item_guids_section.intersection(modifier_guids_section)
                
                if duplicate_guids:
                    logger.warning(f"FOUND DUPLICATE GUIDs in both menu items and modifiers: {len(duplicate_guids)} duplicates")
                    logger.warning(f"Sample duplicates: {list(duplicate_guids)[:10]}")
                    
                    # Check if target modifier is duplicated
                    if "6d02e1aa-41c4-489e-bd72-90d12d79edf2" in duplicate_guids:
                        logger.warning(f"TARGET MODIFIER IS DUPLICATED!")
                        # Count total qty from both sections
                        menu_section_qty = sum(r.get("quantitySold", 0) for r in all_analytics_rows[:menu_item_count] 
                                              if r.get("menuItemGuid") == "6d02e1aa-41c4-489e-bd72-90d12d79edf2")
                        mod_section_qty = sum(r.get("quantitySold", 0) for r in all_analytics_rows[menu_item_count:] 
                                             if r.get("menuItemGuid") == "6d02e1aa-41c4-489e-bd72-90d12d79edf2")
                        logger.warning(f"  In menu items section: {menu_section_qty}")
                        logger.warning(f"  In modifiers section: {mod_section_qty}")
                        logger.warning(f"  Combined (DOUBLE COUNT): {menu_section_qty + mod_section_qty}")
                    
                    # Deduplicate: remove modifier rows whose GUID already exists in menu items
                    menu_item_guids_set = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                    deduplicated_modifier_rows = [
                        r for r in all_analytics_rows[menu_item_count:] 
                        if r.get("menuItemGuid") not in menu_item_guids_set
                    ]
                    
                    # Replace the modifier section with deduplicated version
                    all_analytics_rows = all_analytics_rows[:menu_item_count] + deduplicated_modifier_rows
                    logger.warning(f"After deduplication: {len(all_analytics_rows)} total rows (removed {len(all_analytics_rows[menu_item_count:]) - len(deduplicated_modifier_rows)} duplicate modifier rows)")
                else:
                    logger.info("No duplicate GUIDs found between menu items and modifiers ✓")
                
                # Log sample of modifier rows to verify they're there
                # Modifiers should be in the rows after menu_item_count
                if len(all_analytics_rows) > menu_item_count:
                    modifier_rows_sample = all_analytics_rows[menu_item_count:menu_item_count+5]  # First 5 modifier rows
                    logger.info(f"Sample modifier rows found: {len(all_analytics_rows) - menu_item_count} total modifiers")
                    for i, mod_row in enumerate(modifier_rows_sample):
                        logger.info(f"  Modifier {i+1}: guid={mod_row.get('menuItemGuid')}, name='{mod_row.get('menuItemName')}', qty={mod_row.get('quantitySold', 0)}, restaurant={mod_row.get('restaurantName', 'Unknown')}, date={mod_row.get('businessDate', 'N/A')}")
                        # Check if it's in tags mapping
                        mod_guid = mod_row.get('menuItemGuid')
                        if mod_guid:
                            in_mapping = mod_guid in all_tags_mapping
                            logger.info(f"    -> In tags_mapping: {in_mapping}")
                            if in_mapping:
                                tags = all_tags_mapping[mod_guid]
                                logger.info(f"    -> Tags: {[t['tagName'] for t in tags]}")
                    
                    # Check for specific modifier and sum its quantities across all rows
                    target_modifier_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
                    target_modifier_name = "Half Black & Bleu (OLO Pairing Choice)"
                    total_qty_for_target = 0
                    rows_for_target = []
                    for mod_row in all_analytics_rows[menu_item_count:]:
                        mod_guid = mod_row.get('menuItemGuid')
                        mod_name = mod_row.get('menuItemName', '')
                        if mod_guid == target_modifier_guid or target_modifier_name.lower() in mod_name.lower():
                            qty = mod_row.get('quantitySold', 0)
                            total_qty_for_target += qty
                            rows_for_target.append({
                                'restaurant': mod_row.get('restaurantName', 'Unknown'),
                                'date': mod_row.get('businessDate', 'N/A'),
                                'qty': qty
                            })
                    if rows_for_target:
                        logger.info(f"TARGET MODIFIER '{target_modifier_name}' (guid: {target_modifier_guid}):")
                        logger.info(f"  Total quantity across all rows (raw API data): {total_qty_for_target}")
                        logger.info(f"  Number of rows: {len(rows_for_target)}")
                        logger.info(f"  Sample rows (first 10): {rows_for_target[:10]}")
                        # Check tags for this modifier
                        if target_modifier_guid in all_tags_mapping:
                            mod_tags = all_tags_mapping[target_modifier_guid]
                            logger.info(f"  Modifier tags: {[t['tagName'] for t in mod_tags]}")
                            if report_config.itemTags:
                                matching_tags = [t for t in mod_tags if t['tagName'] in report_config.itemTags]
                                logger.info(f"  Selected tags: {report_config.itemTags}")
                                logger.info(f"  Matching tags: {[t['tagName'] for t in matching_tags]}")
                                logger.info(f"  Will be included in breakdown: {len(matching_tags) > 0}")
                    
                    # Also search for the specific modifier we're looking for
                    target_guid = "6d02e1aa-41c4-489e-bd72-90d12d79edf2"
                    target_name = "Half Black & Bleu (OLO Pairing Choice)"
                    for mod_row in all_analytics_rows[menu_item_count:]:
                        mod_guid = mod_row.get('menuItemGuid')
                        mod_name = mod_row.get('menuItemName', '')
                        if mod_guid == target_guid or target_name.lower() in mod_name.lower():
                            logger.info(f"FOUND TARGET MODIFIER: guid={mod_guid}, name='{mod_name}', qty={mod_row.get('quantitySold', 0)}")
                            # Check name mapping
                            name_key = mod_name.lower().strip()
                            in_name_mapping = name_key in modifier_name_to_guid if modifier_name_to_guid else False
                            logger.info(f"  -> In name_mapping: {in_name_mapping}")
                            logger.info(f"  -> Looking for normalized name: '{name_key}'")
                            
                            # Show similar names in mapping
                            similar_names = []
                            for map_name in modifier_name_to_guid.keys():
                                if "black" in map_name and "bleu" in map_name:
                                    similar_names.append(map_name)
                                elif "half" in map_name and ("black" in map_name or "bleu" in map_name):
                                    similar_names.append(map_name)
                            
                            if similar_names:
                                logger.info(f"  -> Similar names in mapping (first 10): {similar_names[:10]}")
                            
                            if in_name_mapping:
                                mapped_guid = modifier_name_to_guid[name_key]
                                logger.info(f"  -> Mapped GUID: {mapped_guid}")
                                if mapped_guid in all_tags_mapping:
                                    tags = all_tags_mapping[mapped_guid]
                                    logger.info(f"  -> Tags for mapped GUID: {[t['tagName'] for t in tags]}")
                            break
                else:
                    logger.warning(f"No modifier rows found - expected {menu_item_count} menu items, got {len(all_analytics_rows)} total rows")
                
                if not all_analytics_rows:
                    return {"pivotTable": [], "totals": [], "itemBreakdown": []}
                
                # Filter analytics rows - include items that are in filtered_mapping OR have tags in all_tags_mapping
                # This handles cases where items might have different GUIDs in menu vs analytics
                # For modifiers, also try name-based matching as fallback
                filtered_rows = []
                modifier_count_in_filtered = 0
                menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                
                for row in all_analytics_rows:
                    menu_item_guid = row.get("menuItemGuid", "")
                    menu_item_name = row.get("menuItemName", "")
                    if not menu_item_guid:
                        continue
                    
                    is_modifier = menu_item_guid not in menu_item_guids
                    matched_guid = menu_item_guid
                    
                    # Check if item is in filtered mapping (has selected tags)
                    if menu_item_guid in filtered_mapping:
                        filtered_rows.append(row)
                        if is_modifier:
                            modifier_count_in_filtered += 1
                    # Also check if item has tags in all_tags_mapping (might have different GUID)
                    elif menu_item_guid in all_tags_mapping:
                        # Check if any of its tags match selected tags
                        item_tags = all_tags_mapping[menu_item_guid]
                        matching_tags = [t for t in item_tags if t["tagName"] in report_config.itemTags]
                        if matching_tags:
                            # Add to filtered_mapping so it's included in the pivot
                            filtered_mapping[menu_item_guid] = matching_tags
                            filtered_rows.append(row)
                            if is_modifier:
                                modifier_count_in_filtered += 1
                    # For modifiers: ONLY use GUID-based matching (no name-based fallback)
                    # Name-based matching can cause duplicates and inflated counts
                    # If the GUID isn't in the mapping, skip the modifier
                    elif is_modifier:
                        # Skip - modifiers must be found by GUID in all_tags_mapping
                        # Don't use name-based matching as it can cause inflation
                        pass
                        # Try to find modifier by name
                        name_key = menu_item_name.lower().strip()
                        
                        # Normalize variations: "&" -> "and", remove extra spaces
                        name_key_normalized = name_key.replace("&", "and").replace("  ", " ").strip()
                        
                        # Debug logging for the specific modifier we're looking for
                        if "half black" in name_key.lower() or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                            logger.info(f"DEBUG: Looking for modifier - guid={menu_item_guid}, name='{menu_item_name}', normalized='{name_key}', normalized_v2='{name_key_normalized}'")
                            logger.info(f"DEBUG: Name in mapping? {name_key in modifier_name_to_guid}")
                            logger.info(f"DEBUG: Normalized name in mapping? {name_key_normalized in modifier_name_to_guid}")
                            if name_key in modifier_name_to_guid:
                                logger.info(f"DEBUG: Found GUID: {modifier_name_to_guid[name_key]}")
                            elif name_key_normalized in modifier_name_to_guid:
                                logger.info(f"DEBUG: Found GUID with normalized name: {modifier_name_to_guid[name_key_normalized]}")
                            else:
                                # Try to find similar names
                                similar = [n for n in modifier_name_to_guid.keys() if "half black" in n or ("bleu" in n and "black" in n)]
                                logger.info(f"DEBUG: Similar names in mapping: {similar[:10]}")
                        
                        # Try exact match first
                        matched_guid = None
                        if name_key in modifier_name_to_guid:
                            matched_guid = modifier_name_to_guid[name_key]
                        # Try normalized version (with & -> and)
                        elif name_key_normalized in modifier_name_to_guid:
                            matched_guid = modifier_name_to_guid[name_key_normalized]
                        
                        if matched_guid:
                            matched_guid = modifier_name_to_guid[name_key]
                            if matched_guid in all_tags_mapping:
                                item_tags = all_tags_mapping[matched_guid]
                                matching_tags = [t for t in item_tags if t["tagName"] in report_config.itemTags]
                                if matching_tags:
                                    # Update the row's GUID to the matched one and add to filtered_mapping
                                    row["menuItemGuid"] = matched_guid
                                    filtered_mapping[matched_guid] = matching_tags
                                    filtered_rows.append(row)
                                    modifier_count_in_filtered += 1
                                    logger.info(f"Matched modifier by name: '{menu_item_name}' (guid: {menu_item_guid}) -> {matched_guid}")
                                else:
                                    if "half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                                        logger.warning(f"DEBUG: Modifier matched by name but tags don't match selected tags. Tags: {[t['tagName'] for t in item_tags]}, Selected: {report_config.itemTags}")
                            else:
                                if "half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid:
                                    logger.warning(f"DEBUG: Modifier matched by name but GUID {matched_guid} not in all_tags_mapping")
                        else:
                            # Try fuzzy matching - check if any mapping name contains key parts
                            # Extract key words from the name
                            key_words = set()
                            for word in name_key.split():
                                # Remove common words and punctuation
                                clean_word = word.strip("()&,").lower()
                                if len(clean_word) > 2 and clean_word not in ["the", "and", "or", "for", "with"]:
                                    key_words.add(clean_word)
                            
                            # Try to find a match with at least 3 key words in common
                            best_match = None
                            best_score = 0
                            for map_name, map_guid in modifier_name_to_guid.items():
                                map_words = set(map_name.split())
                                common_words = key_words.intersection(map_words)
                                if len(common_words) >= 3:  # At least 3 words in common
                                    score = len(common_words)
                                    if score > best_score:
                                        best_score = score
                                        best_match = (map_name, map_guid)
                            
                            if best_match and ("half black" in name_key or "6d02e1aa-41c4-489e-bd72-90d12d79edf2" == menu_item_guid):
                                map_name, map_guid = best_match
                                logger.info(f"DEBUG: Found potential fuzzy match: '{map_name}' -> {map_guid} (score: {best_score})")
                            
                            if best_match:
                                map_name, map_guid = best_match
                                if map_guid in all_tags_mapping:
                                    item_tags = all_tags_mapping[map_guid]
                                    matching_tags = [t for t in item_tags if t["tagName"] in report_config.itemTags]
                                    if matching_tags:
                                        row["menuItemGuid"] = map_guid
                                        filtered_mapping[map_guid] = matching_tags
                                        filtered_rows.append(row)
                                        modifier_count_in_filtered += 1
                                        logger.info(f"Matched modifier by fuzzy name: '{menu_item_name}' -> '{map_name}' (guid: {map_guid})")
                
                logger.info(f"Filtered analytics rows: {len(filtered_rows)} from {len(all_analytics_rows)} total (includes {modifier_count_in_filtered} modifiers)")
                
                # Log sample of filtered modifier rows
                # Get menu item GUIDs from first part (menu items)
                menu_item_guids = {r.get("menuItemGuid") for r in all_analytics_rows[:menu_item_count] if r.get("menuItemGuid")}
                filtered_modifier_rows = [r for r in filtered_rows if r.get("menuItemGuid") and r.get("menuItemGuid") not in menu_item_guids]
                if filtered_modifier_rows:
                    logger.info(f"Filtered modifier rows: {len(filtered_modifier_rows)} modifiers will be included in pivot")
                    for i, mod_row in enumerate(filtered_modifier_rows[:5]):  # Log first 5
                        logger.info(f"  Filtered Modifier {i+1}: guid={mod_row.get('menuItemGuid')}, name='{mod_row.get('menuItemName')}', qty={mod_row.get('quantitySold', 0)}")
                else:
                    logger.warning(f"No modifiers in filtered_rows (out of {len(all_analytics_rows) - menu_item_count} total modifiers) - modifiers may not have matching tags")
                
                # Debug: Check if the specific GUIDs are in the data
                missing_guids = ["6d02e1aa-41c4-489e-bd72-90d12d79edf2", "99746810-e792-4e7a-b99f-0e30b470a2bd"]
                for guid in missing_guids:
                    in_mapping = guid in all_tags_mapping
                    in_analytics = any(row.get("menuItemGuid") == guid for row in all_analytics_rows)
                    in_filtered = guid in filtered_mapping
                    in_filtered_rows = any(row.get("menuItemGuid") == guid for row in filtered_rows)
                    logger.info(f"Item GUID {guid}: in_menu_mapping={in_mapping}, in_analytics={in_analytics}, in_filtered={in_filtered}, in_filtered_rows={in_filtered_rows}")
                    if in_analytics:
                        # Find the analytics row to see item name
                        analytics_row = next((row for row in all_analytics_rows if row.get("menuItemGuid") == guid), None)
                        if analytics_row:
                            item_name = analytics_row.get("menuItemName", "Unknown")
                            qty = analytics_row.get("quantitySold", 0)
                            logger.info(f"  Analytics data: name='{item_name}', qty={qty}")
                        # Check what tags it has
                        if in_mapping:
                            tags = all_tags_mapping[guid]
                            logger.info(f"  Tags for {guid}: {[t['tagName'] for t in tags]}")
                        else:
                            logger.warning(f"  GUID {guid} found in analytics but not in menu mapping from first restaurant!")
                            logger.warning(f"  This item will be excluded because we can't determine its tags.")
                
                # Build item names mapping (empty for now, will use menuItemName from analytics if available)
                item_names_mapping = {}
                
                # Transform into pivot table format
                result = pivot_menu_items_by_tag(
                    filtered_rows,
                    all_tags_mapping,
                    report_config.itemTags,
                    item_names_mapping,
                    menu_item_guids  # Pass menu item GUIDs to identify modifiers
                )
                
                return result
            
            # Default: Use ERA reporting API
            report_guid = await request_toast_report(
                client,
                start_date,
                end_date,
                restaurant_ids,
                report_config.reportType,
                report_config.groupBy
            )
            
            raw_rows = await fetch_toast_report(client, report_guid, report_config.reportType)
            
            if not raw_rows:
                return []
            
            # Transform metrics data to pivot table format if needed
            if report_config.reportType == "metrics" and not report_config.groupBy:
                pivot_data = pivot_metrics_by_location_and_date(raw_rows)
                return pivot_data
            
            return raw_rows
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running report: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error running report: {str(e)}")

@app.post("/api/reports/item-tags-quantities")
async def run_item_tags_quantities_report(request: ItemTagsQuantitiesRequest = Body(...)):
    """
    Generate quantities report grouped by item tags
    Uses Analytics API + Menus API tag mapping
    """
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            restaurant_ids = request.restaurantIds
            
            if not restaurant_ids or len(restaurant_ids) == 0:
                locations = await get_all_active_locations(client)
                restaurant_ids = [loc.restaurantGuid for loc in locations]
            
            # Parse dates
            start_date = datetime.strptime(request.startBusinessDate, "%Y%m%d").date()
            end_date = datetime.strptime(request.endBusinessDate, "%Y%m%d").date()
            
            # Get menu tags mapping
            logger.info("Fetching menu tags mapping...")
            tags_mapping = await get_menu_tags_mapping(client, restaurant_ids[0])
            
            # Request Analytics menu report with MENU_ITEM grouping
            logger.info("Requesting Analytics menu report with MENU_ITEM grouping...")
            report_guid = await request_toast_report(
                client,
                start_date,
                end_date,
                restaurant_ids,
                "menu",
                ["MENU_ITEM"]
            )
            
            # Fetch analytics data
            logger.info(f"Fetching report {report_guid}...")
            analytics_rows = await fetch_toast_report(client, report_guid, "menu")
            
            if not analytics_rows:
                return []
            
            # Aggregate by tags
            logger.info(f"Aggregating {len(analytics_rows)} rows by tags...")
            result = aggregate_quantities_by_tag(
                analytics_rows,
                tags_mapping,
                request.tagAttributionMode
            )
            
            # If not daily, sum across dates
            if not request.daily:
                aggregated = {}
                for row in result:
                    key = f"{row['restaurantGuid']}|{row['tagGuid']}"
                    if key not in aggregated:
                        aggregated[key] = {
                            "restaurantGuid": row["restaurantGuid"],
                            "restaurantName": row["restaurantName"],
                            "tagGuid": row["tagGuid"],
                            "tagName": row["tagName"],
                            "quantitySold": 0
                        }
                    aggregated[key]["quantitySold"] += row["quantitySold"]
                
                result = list(aggregated.values())
            
            # Sort by restaurant, tag name
            result.sort(key=lambda x: (x["restaurantName"], x["tagName"]))
            
            logger.info(f"Returning {len(result)} aggregated rows")
            return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in item tags quantities report: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating report: {str(e)}")

# ------------------
# Menu Items & Tags API
# ------------------

@app.get("/api/menu-items")
async def get_menu_items(restaurant_guid: Optional[str] = Query(None)):
    """Get all menu items, modifiers, and nested modifiers with their tags"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if not restaurant_guid:
                locations = await get_all_active_locations(client)
                if not locations:
                    raise HTTPException(status_code=404, detail="No active locations found")
                restaurant_guid = locations[0].restaurantGuid
            
            standard_headers = await get_standard_api_auth_headers()
            menus_v2_url = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
            headers_with_guid = {
                **standard_headers,
                "Toast-Restaurant-External-ID": restaurant_guid
            }
            
            resp = await client.get(menus_v2_url, headers=headers_with_guid)
            
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Error fetching menu data")
            
            menu_data = resp.json()
            
            items_with_tags = []
            all_tags = set()
            
            mod_group_refs = menu_data.get("modifierGroupReferences", {})
            mod_option_refs = menu_data.get("modifierOptionReferences", {})
            
            for menu in menu_data.get("menus", []):
                for group in menu.get("menuGroups", []):
                    for item in group.get("menuItems", []):
                        item_tags = [tag.get("name") for tag in item.get("itemTags", []) if tag.get("name")]
                        
                        if item_tags:
                            all_tags.update(item_tags)
                            items_with_tags.append({
                                "guid": item.get("guid"),
                                "name": item.get("name"),
                                "type": "item",
                                "tags": item_tags
                            })
            
            return {
                "items": items_with_tags,
                "allTags": sorted(list(all_tags))
            }
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in get_menu_items: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error fetching menu items: {str(e)}")

# ------------------
# Report Types Info
# ------------------

@app.get("/api/report-types")
async def get_report_types():
    """Get available report types and their options"""
    return {
        "reportTypes": [
            {
                "id": "metrics",
                "name": "Metrics Report",
                "description": "Sales metrics by date and location",
                "groupByOptions": [
                    {"id": "", "name": "No grouping", "description": "Total by date and location only"},
                    {"id": "REVENUE_CENTER", "name": "Revenue Center", "description": "Group by revenue center"},
                    {"id": "DINING_OPTION", "name": "Dining Option", "description": "Group by dining option (Dine In, Takeout, etc.)"},
                    {"id": "EMPLOYEE", "name": "Employee", "description": "Group by employee"},
                    {"id": "JOB", "name": "Job", "description": "Group by job role"},
                ]
            },
            {
                "id": "menu",
                "name": "Menu Performance",
                "description": "Sales by menu item",
                "groupByOptions": [
                    {"id": "MENU_ITEM", "name": "Menu Item", "description": "Group by menu item"}
                ],
                "supportsItemTagFilters": True
            }
        ],
        "dateRangeTypes": [
            {"id": "last_7_days", "name": "Last 7 Days"},
            {"id": "last_30_days", "name": "Last 30 Days"},
            {"id": "last_90_days", "name": "Last 90 Days"},
            {"id": "custom", "name": "Custom Range"}
        ]
    }

# ------------------
# Help Desk Integration
# ------------------

@app.post("/api/help/ticket")
async def create_help_ticket(ticket: HelpTicketRequest = Body(...)):
    """Create a help desk ticket via Slack webhook"""
    
    if not SLACK_WEBHOOK_URL:
        raise HTTPException(
            status_code=500,
            detail="Slack integration not configured. Please set SLACK_WEBHOOK_URL environment variable."
        )
    
    # Map priority values to emoji and text
    priority_map = {
        1: ("🟢", "Low"),
        2: ("🟡", "Medium"),
        3: ("🟠", "High"),
        4: ("🔴", "Urgent")
    }
    priority_emoji, priority_text = priority_map.get(ticket.priority, ("🟡", "Medium"))
    
    # Check if this is a workflow webhook (contains /workflows/) or regular webhook
    # For workflow webhooks, send raw data. For regular webhooks, send formatted blocks.
    is_workflow = "/workflows/" in SLACK_WEBHOOK_URL or "workflow" in SLACK_WEBHOOK_URL.lower()
    
    if is_workflow:
        # Send raw data for workflow to format
        # Slack workflows can access these as variables: {{name}}, {{email}}, etc.
        slack_message = {
            "name": ticket.name,
            "email": ticket.email,
            "subject": ticket.subject,
            "priority": priority_text,
            "priority_emoji": priority_emoji,
            "priority_value": str(ticket.priority),  # 1, 2, 3, or 4
            "description": ticket.description,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            "source": "Newk's Reporting Platform"
        }
    else:
        # Format Slack message with blocks for direct webhook
        slack_message = {
            "text": f"{priority_emoji} New Help Request from {ticket.name}",  # Fallback for notifications
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{priority_emoji} New Help Request - {priority_text} Priority",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*👤 Name:*\n{ticket.name}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*📧 Email:*\n<mailto:{ticket.email}|{ticket.email}>"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*⚡ Priority:*\n{priority_text} {priority_emoji}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*📋 Subject:*\n{ticket.subject}"
                        }
                    ]
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*📝 Description:*\n```{ticket.description}```"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"🕐 Submitted from Newk's Reporting Platform • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    ]
                }
            ]
        }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try sending with blocks first
            resp = await client.post(SLACK_WEBHOOK_URL, json=slack_message)
            
            # Log the response for debugging
            logger.info(f"Slack webhook response: {resp.status_code}")
            if resp.status_code != 200:
                logger.info(f"Response body: {resp.text[:500]}")
            
            if resp.status_code == 200:
                logger.info(f"Sent help ticket to Slack for {ticket.email}")
                return {
                    "success": True,
                    "ticketId": f"SLACK-{int(time.time())}",
                    "message": "Help request submitted successfully. Our team will respond soon!"
                }
            else:
                # If blocks fail, try simple text format as fallback
                logger.warning(f"Block format failed, trying simple text format")
                simple_message = {
                    "text": f"{priority_emoji} *New Help Request*\n\n*Name:* {ticket.name}\n*Email:* {ticket.email}\n*Priority:* {priority_text} ({priority_emoji})\n*Subject:* {ticket.subject}\n\n*Description:*\n{ticket.description}\n\n_Submitted from Newk's Reporting Platform at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
                }
                resp2 = await client.post(SLACK_WEBHOOK_URL, json=simple_message)
                
                if resp2.status_code == 200:
                    logger.info(f"Sent help ticket to Slack (simple format) for {ticket.email}")
                    return {
                        "success": True,
                        "ticketId": f"SLACK-{int(time.time())}",
                        "message": "Help request submitted successfully. Our team will respond soon!"
                    }
                
                error_msg = resp.text or resp2.text
                logger.error(f"Failed to send Slack message: {resp.status_code} / {resp2.status_code} - {error_msg}")
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Failed to submit request: {error_msg}"
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending Slack message: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error submitting request: {str(e)}")

# ------------------
# Static Frontend
# ------------------

# ------------------
# Initialize Super Admin (after all functions are defined)
# ------------------

# Initialize super admin on startup (after truncate_password_to_72_bytes is defined)
initialize_super_admin()

# ------------------
# Static Frontend
# ------------------

frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")

app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")