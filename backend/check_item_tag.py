#!/usr/bin/env python3
"""
Diagnostic script to check if a specific item has a specific tag in the Toast menu API.
Usage: python check_item_tag.py <restaurant_guid> <item_guid> <tag_name>
"""

import asyncio
import httpx
import sys
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOAST_STANDARD_API_BASE_URL = os.getenv("TOAST_STANDARD_API_BASE_URL", "https://ws-api.toasttab.com")
TOAST_STANDARD_API_CLIENT_ID = os.getenv("TOAST_STANDARD_API_CLIENT_ID")
TOAST_STANDARD_API_CLIENT_SECRET = os.getenv("TOAST_STANDARD_API_CLIENT_SECRET")

async def get_standard_api_auth_headers():
    """Get authorization headers for Standard API (Menus API)"""
    if not TOAST_STANDARD_API_CLIENT_ID or not TOAST_STANDARD_API_CLIENT_SECRET:
        raise Exception("Standard API credentials required. Set TOAST_STANDARD_API_CLIENT_ID and TOAST_STANDARD_API_CLIENT_SECRET")
    
    standard_auth_url = f"{TOAST_STANDARD_API_BASE_URL}/authentication/v1/authentication/login"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_STANDARD_API_CLIENT_ID,
            "clientSecret": TOAST_STANDARD_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(standard_auth_url, json=auth_body)
        if resp.status_code != 200:
            raise Exception(f"Failed to authenticate with Standard API: {resp.status_code} - {resp.text}")
        
        data = resp.json()
        if isinstance(data, dict):
            if "token" in data and isinstance(data["token"], dict):
                token = data["token"].get("accessToken") or data["token"].get("token")
            else:
                token = data.get("accessToken") or data.get("token") or data.get("access_token")
        else:
            token = data
        
        if not token:
            raise Exception("No token received from Standard API authentication")
        
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

async def check_item_tag(restaurant_guid: str, item_guid: str, tag_name: str):
    """Check if an item has a specific tag"""
    print(f"Checking item {item_guid} for tag '{tag_name}' in restaurant {restaurant_guid}...")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get auth headers
        try:
            headers = await get_standard_api_auth_headers()
        except Exception as e:
            print(f"ERROR: Failed to authenticate: {e}")
            return
        
        headers_with_guid = {
            **headers,
            "Toast-Restaurant-External-ID": restaurant_guid
        }
        
        # Try V3 first
        print("\n=== Trying V3 Menus API ===")
        menus_url_v3 = f"{TOAST_STANDARD_API_BASE_URL}/menus/v3/menus"
        try:
            resp = await client.get(menus_url_v3, headers=headers_with_guid)
            if resp.status_code == 200:
                menus_data = resp.json()
                found = search_item_in_menus(menus_data, item_guid, tag_name, "V3")
                if found:
                    return
            elif resp.status_code == 403:
                print("V3 API not available (403), trying V2...")
            else:
                print(f"V3 API returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"Error with V3 API: {e}")
        
        # Try V2
        print("\n=== Trying V2 Menus API ===")
        menus_url_v2 = f"{TOAST_STANDARD_API_BASE_URL}/menus/v2/menus"
        try:
            resp = await client.get(menus_url_v2, headers=headers_with_guid)
            if resp.status_code == 200:
                menus_data = resp.json()
                search_item_in_menus(menus_data, item_guid, tag_name, "V2")
            else:
                print(f"V2 API returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"Error with V2 API: {e}")

def search_item_in_menus(menus_data: dict, item_guid: str, tag_name: str, api_version: str):
    """Search for item in menus data"""
    print(f"\nSearching in {api_version} menus data...")
    
    found_item = False
    found_in_items = False
    found_in_modifiers = False
    
    # Search in menu items
    for menu in menus_data.get("menus", []):
        for group in menu.get("menuGroups", []):
            for item in group.get("menuItems", []):
                if item.get("guid") == item_guid:
                    found_item = True
                    found_in_items = True
                    item_name = item.get("name") or item.get("displayName", "Unknown")
                    print(f"\n✓ FOUND ITEM in menuItems:")
                    print(f"  Name: {item_name}")
                    print(f"  GUID: {item.get('guid')}")
                    
                    # Check tags
                    tags = item.get("itemTags", [])
                    print(f"  Tags ({len(tags)}):")
                    tag_found = False
                    for tag in tags:
                        tag_guid = tag.get("guid", "")
                        tag_name_api = tag.get("name", "")
                        print(f"    - {tag_name_api} (guid: {tag_guid})")
                        if tag_name_api == tag_name or tag_name_api.lower() == tag_name.lower():
                            tag_found = True
                            print(f"    ✓ MATCH! This is the tag we're looking for!")
                    
                    if not tag_found:
                        print(f"  ✗ Tag '{tag_name}' NOT FOUND in this item's tags")
                    
                    return tag_found
    
    # Search in modifier groups
    modifier_group_refs = menus_data.get("modifierGroupReferences", {})
    for mod_group_guid, mod_group in modifier_group_refs.items():
        for mod_option in mod_group.get("modifierOptions", []):
            if mod_option.get("guid") == item_guid:
                found_item = True
                found_in_modifiers = True
                mod_name = mod_option.get("name") or mod_option.get("displayName", "Unknown")
                print(f"\n✓ FOUND ITEM in modifierOptions:")
                print(f"  Name: {mod_name}")
                print(f"  GUID: {mod_option.get('guid')}")
                
                # Check tags
                tags = mod_option.get("itemTags", [])
                print(f"  Tags ({len(tags)}):")
                tag_found = False
                for tag in tags:
                    tag_guid = tag.get("guid", "")
                    tag_name_api = tag.get("name", "")
                    print(f"    - {tag_name_api} (guid: {tag_guid})")
                    if tag_name_api == tag_name or tag_name_api.lower() == tag_name.lower():
                        tag_found = True
                        print(f"    ✓ MATCH! This is the tag we're looking for!")
                
                if not tag_found:
                    print(f"  ✗ Tag '{tag_name}' NOT FOUND in this item's tags")
                
                return tag_found
    
    # Search in modifier option references
    modifier_option_refs = menus_data.get("modifierOptionReferences", {})
    for mod_option_id, mod_option in modifier_option_refs.items():
        mod_option_guid = mod_option.get("guid", "")
        if mod_option_guid == item_guid:
            found_item = True
            found_in_modifiers = True
            mod_name = mod_option.get("name") or mod_option.get("displayName", "Unknown")
            print(f"\n✓ FOUND ITEM in modifierOptionReferences:")
            print(f"  Name: {mod_name}")
            print(f"  GUID: {mod_option_guid}")
            print(f"  ID: {mod_option_id}")
            
            # Check tags
            tags = mod_option.get("itemTags", [])
            print(f"  Tags ({len(tags)}):")
            tag_found = False
            for tag in tags:
                tag_guid = tag.get("guid", "")
                tag_name_api = tag.get("name", "")
                print(f"    - {tag_name_api} (guid: {tag_guid})")
                if tag_name_api == tag_name or tag_name_api.lower() == tag_name.lower():
                    tag_found = True
                    print(f"    ✓ MATCH! This is the tag we're looking for!")
            
            if not tag_found:
                print(f"  ✗ Tag '{tag_name}' NOT FOUND in this item's tags")
            
            return tag_found
    
    if not found_item:
        print(f"\n✗ ITEM NOT FOUND in {api_version} menus data")
        print(f"  Searched in:")
        print(f"    - menuItems: {sum(len(menu.get('menuGroups', [])) for menu in menus_data.get('menus', []))} menu groups")
        print(f"    - modifierGroupReferences: {len(modifier_group_refs)} groups")
        print(f"    - modifierOptionReferences: {len(modifier_option_refs)} options")
    
    return False

async def main():
    if len(sys.argv) < 4:
        print("Usage: python check_item_tag.py <restaurant_guid> <item_guid> <tag_name>")
        print("\nExample:")
        print("  python check_item_tag.py abc123 01629662-5cf7-4ec4-a2a2-e6ef73ea4f40 2026LTO1")
        sys.exit(1)
    
    restaurant_guid = sys.argv[1]
    item_guid = sys.argv[2]
    tag_name = sys.argv[3]
    
    await check_item_tag(restaurant_guid, item_guid, tag_name)

if __name__ == "__main__":
    asyncio.run(main())

