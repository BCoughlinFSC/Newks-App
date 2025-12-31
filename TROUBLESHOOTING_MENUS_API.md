# Troubleshooting Menus API Access

## Current Status

You're getting **401 Unauthorized** when trying to authenticate with Standard API credentials.

This means:
- ✅ Credentials are being read from `.env`
- ✅ Code is trying to authenticate
- ❌ Authentication is failing

## Possible Causes

### 1. Wrong Credentials
- Double-check `TOAST_STANDARD_API_CLIENT_ID` and `TOAST_STANDARD_API_CLIENT_SECRET` in `backend\.env`
- Make sure there are no extra spaces or quotes
- Verify you copied the correct credentials (Standard API, not Analytics API)

### 2. Credentials Don't Have Menus API Access
- Standard API credentials might not have permission for Menus API
- You may need to request Menus API access from Toast
- Check with your Toast administrator about API scopes/permissions

### 3. Different Authentication Method
- Some Toast APIs might use different authentication
- Menus API might require different scopes or setup

## How to Verify Credentials

1. **Check your `.env` file:**
   ```
   TOAST_STANDARD_API_CLIENT_ID=your_client_id_here
   TOAST_STANDARD_API_CLIENT_SECRET=your_client_secret_here
   ```
   - No quotes around values
   - No extra spaces
   - Exact values from Toast

2. **Verify with Toast:**
   - Contact Toast support to confirm:
     - Do these credentials have Menus API access?
     - What scopes/permissions are needed?
     - Is there a different API endpoint for menus?

## Alternative Approaches

### Option 1: Use Analytics API Menu Reporting
The Analytics API has menu reporting endpoints that return menu items with sales data. We could:
- Use `/menu` endpoint (POST request with date range)
- Get menu items that have been sold
- Build menu structure from that data

### Option 2: Request Menus API Access
- Contact Toast to request Menus API v3 access
- They may need to enable it for your credentials
- May require different credentials or additional setup

### Option 3: Manual Menu Import
- Export menu structure from Toast UI
- Import as JSON/CSV
- Use that for the Report Builder

## Next Steps

1. **Double-check credentials** in `backend\.env`
2. **Contact Toast support** to verify Menus API access
3. **Try Analytics API menu reporting** as an alternative
4. **Consider manual import** if API access isn't available

Let me know what Toast says about Menus API access, and we can adjust the approach!

