#!/usr/bin/env python3
"""
Smoke tests for v1 and v2 API endpoints.
Run this script to verify that all critical endpoints are working.

Usage:
    python test_smoke.py [--base-url http://localhost:8000]
"""

import sys
import httpx
import argparse
from typing import Dict, Any

def test_endpoint(client: httpx.Client, method: str, url: str, expected_status: int = 200, **kwargs) -> Dict[str, Any]:
    """Test an endpoint and return result"""
    try:
        if method.upper() == "GET":
            response = client.get(url, timeout=10.0, **kwargs)
        elif method.upper() == "POST":
            response = client.post(url, timeout=10.0, **kwargs)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        if response.status_code != expected_status:
            return {
                "success": False,
                "error": f"Expected status {expected_status}, got {response.status_code}",
                "response": response.text[:200]
            }
        
        try:
            data = response.json()
        except:
            data = response.text
        
        return {
            "success": True,
            "status": response.status_code,
            "data": data
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

def run_tests(base_url: str = "http://localhost:8000"):
    """Run all smoke tests"""
    print(f"Running smoke tests against {base_url}\n")
    
    client = httpx.Client()
    failures = []
    
    # V1 Endpoints
    print("=" * 60)
    print("V1 ENDPOINTS")
    print("=" * 60)
    
    # Test /health
    print("\n1. Testing GET /health")
    result = test_endpoint(client, "GET", f"{base_url}/health")
    if result["success"]:
        print(f"   ✓ Status: {result['status']}")
        if "status" in result.get("data", {}):
            print(f"   ✓ Response: {result['data']}")
        else:
            print(f"   ⚠ Unexpected response format: {result['data']}")
    else:
        print(f"   ✗ FAILED: {result['error']}")
        failures.append("/health")
    
    # Test /locations
    print("\n2. Testing GET /locations")
    result = test_endpoint(client, "GET", f"{base_url}/locations")
    if result["success"]:
        print(f"   ✓ Status: {result['status']}")
        data = result.get("data", [])
        if isinstance(data, list):
            print(f"   ✓ Found {len(data)} locations")
        else:
            print(f"   ⚠ Unexpected response format (expected list)")
    else:
        print(f"   ✗ FAILED: {result['error']}")
        failures.append("/locations")
    
    # Test /api/report-types
    print("\n3. Testing GET /api/report-types")
    result = test_endpoint(client, "GET", f"{base_url}/api/report-types")
    if result["success"]:
        print(f"   ✓ Status: {result['status']}")
        data = result.get("data", {})
        if "reportTypes" in data:
            print(f"   ✓ Found {len(data['reportTypes'])} report types")
        else:
            print(f"   ⚠ Unexpected response format (expected reportTypes)")
    else:
        print(f"   ✗ FAILED: {result['error']}")
        failures.append("/api/report-types")
    
    # V2 Endpoints
    print("\n" + "=" * 60)
    print("V2 ENDPOINTS")
    print("=" * 60)
    
    # Test /api/v2/health
    print("\n4. Testing GET /api/v2/health")
    result = test_endpoint(client, "GET", f"{base_url}/api/v2/health")
    if result["success"]:
        print(f"   ✓ Status: {result['status']}")
        data = result.get("data", {})
        if data.get("version") == "v2":
            print(f"   ✓ Response: {data}")
        else:
            print(f"   ⚠ Unexpected version in response: {data}")
    else:
        print(f"   ✗ FAILED: {result['error']}")
        failures.append("/api/v2/health")
    
    # Test /api/v2/locations
    print("\n5. Testing GET /api/v2/locations")
    result = test_endpoint(client, "GET", f"{base_url}/api/v2/locations")
    if result["success"]:
        print(f"   ✓ Status: {result['status']}")
        data = result.get("data", [])
        if isinstance(data, list):
            print(f"   ✓ Found {len(data)} locations")
        else:
            print(f"   ⚠ Unexpected response format (expected list)")
    else:
        print(f"   ✗ FAILED: {result['error']}")
        failures.append("/api/v2/locations")
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if failures:
        print(f"\n✗ {len(failures)} test(s) FAILED:")
        for failure in failures:
            print(f"   - {failure}")
        print("\n⚠ Some endpoints are not working correctly!")
        return 1
    else:
        print("\n✓ All smoke tests passed!")
        return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run smoke tests for v1 and v2 API endpoints")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the API server (default: http://localhost:8000)"
    )
    args = parser.parse_args()
    
    try:
        exit_code = run_tests(args.base_url)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nTests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        sys.exit(1)

