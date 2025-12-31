"""
Quick test script to check Toast Menus API access
Run this to test if your credentials work for Menus API
"""
import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

TOAST_API_BASE_URL = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com/era/v1")
TOAST_API_CLIENT_ID = os.getenv("TOAST_API_CLIENT_ID")
TOAST_API_CLIENT_SECRET = os.getenv("TOAST_API_CLIENT_SECRET")

# Try different possible Menus API base URLs
MENUS_API_BASE_URLS = [
    "https://ws-api.toasttab.com/menus/v3",  # Most likely
    "https://ws-api.toasttab.com/menus/v2",
    "https://ws-api.toasttab.com/menus/v1",
    "https://ws-api.toasttab.com/menus",
]

async def get_auth_token():
    """Get OAuth2 token"""
    auth_base = TOAST_API_BASE_URL.replace("/era/v1", "")
    auth_url = f"{auth_base}/authentication/v1/authentication/login"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        auth_body = {
            "clientId": TOAST_API_CLIENT_ID,
            "clientSecret": TOAST_API_CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        
        resp = await client.post(auth_url, json=auth_body)
        if resp.status_code != 200:
            print(f"❌ Auth failed: {resp.status_code} - {resp.text}")
            return None
        
        data = resp.json()
        if "token" in data and isinstance(data["token"], dict):
            return data["token"].get("accessToken") or data["token"].get("token")
        return data.get("accessToken") or data.get("token")

async def test_menus_api():
    """Test different Menus API endpoints"""
    token = await get_auth_token()
    if not token:
        print("❌ Could not get auth token")
        return
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    # Test endpoints to try
    test_endpoints = [
        "/menus",
        "/menus?limit=10",
        "/menu-groups",
        "/menu-items",
        "/menu-items?limit=10",
    ]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for base_url in MENUS_API_BASE_URLS:
            print(f"\n🔍 Testing: {base_url}")
            for endpoint in test_endpoints:
                url = f"{base_url}{endpoint}"
                try:
                    resp = await client.get(url, headers=headers)
                    print(f"  {endpoint}: {resp.status_code}")
                    if resp.status_code == 200:
                        print(f"  ✅ SUCCESS! Found working endpoint: {url}")
                        data = resp.json()
                        print(f"  Response type: {type(data)}")
                        if isinstance(data, list) and len(data) > 0:
                            print(f"  First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}")
                        elif isinstance(data, dict):
                            print(f"  Top-level keys: {list(data.keys())}")
                        return url, data
                    elif resp.status_code == 404:
                        print(f"  ⚠️  Not found (404)")
                    else:
                        print(f"  ❌ Error: {resp.status_code} - {resp.text[:200]}")
                except Exception as e:
                    print(f"  ❌ Exception: {str(e)}")
    
    print("\n❌ Could not find working Menus API endpoint")
    return None, None

if __name__ == "__main__":
    result = asyncio.run(test_menus_api())
    if result[0]:
        print(f"\n✅ Working endpoint: {result[0]}")

