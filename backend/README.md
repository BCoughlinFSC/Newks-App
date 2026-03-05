# Backend (FastAPI) for Newk's Toast Reporting Platform

This folder contains a FastAPI backend that integrates with Toast APIs and provides:

**Core Reports:**
- Listing locations
- Getting net sales by day, by location, for a date range

**Menu Sync:**
- Fetching menu structure from Toast Menus API (menus → groups → items → modifiers)

**Category Management (R365-style dimension layer):**
- Creating and managing custom report categories
- Mapping Toast menu items/modifiers to categories
- Bulk operations for category assignments

## 1. Prerequisites

- Python 3.10+ recommended
- Access to Toast's read-only Analytics API for your enterprise
- A bearer token with permission to call the `/era/v1` analytics endpoints

## 2. Setup

From this `backend` folder:

```bash
python -m venv .venv
source .venv/bin/activate    # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create your `.env` file:

```bash
cp .env.example .env
```

Edit `.env` and set:

**Analytics API (for sales/metrics):**
- `TOAST_API_BASE_URL` — e.g. `https://ws-api.toasttab.com/era/v1`
- `TOAST_API_CLIENT_ID` — OAuth2 client ID (Analytics API)
- `TOAST_API_CLIENT_SECRET` — OAuth2 client secret (Analytics API)
- `TOAST_API_TOKEN` — (Optional) Direct bearer token (alternative to OAuth2)

**Standard API (for menus):**
- `TOAST_STANDARD_API_CLIENT_ID` — OAuth2 client ID (Standard API)
- `TOAST_STANDARD_API_CLIENT_SECRET` — OAuth2 client secret (Standard API)
- `TOAST_STANDARD_API_BASE_URL` — e.g. `https://ws-api.toasttab.com` (defaults to this)

You can set these as environment variables instead of `.env` if you prefer.

> **Note for LLMs helping with debugging:**  
> Never log or print the real token in responses. If you need to show example headers,
> use a fake token like `Bearer FAKE_TOKEN_FOR_DOCS`.

## 3. Running the backend

From this `backend` folder, with the virtualenv activated:

```bash
uvicorn main:app --reload
```

By default this starts the API at `http://localhost:8000`.

The backend also serves the frontend in `../frontend` as static files, so you can
go directly to:

- `http://localhost:8000/` — Net Sales report UI
- `http://localhost:8000/report-builder` — Report Builder (menu mapping)
- `http://localhost:8000/configuration` — Category management
- `http://localhost:8000/docs` — interactive API docs (Swagger UI)
- `http://localhost:8000/redoc` — alternative API docs

## 4. Key endpoints

### Core Reports

### `GET /health`

Simple health check. Returns `{"status": "ok"}` if the app is running and the
environment variables are set well enough to start.

### `GET /locations`

Fetches all active, non-archived, non-test-mode locations from Toast
(`/era/v1/restaurants-information`) and returns them as:

```json
[
  {
    "restaurantGuid": "GUID1",
    "restaurantName": "Newk's Jackson"
  },
  ...
]
```

### `GET /sales/net`

Query parameters:

- `start_date` (required) — `YYYY-MM-DD`
- `end_date` (required) — `YYYY-MM-DD`
- `restaurant_ids` (optional, repeatable) — one or more `restaurantGuid`s

Examples:

```bash
# All active locations, last 7 days
curl "http://localhost:8000/sales/net?start_date=2025-02-01&end_date=2025-02-07"

# Two specific locations
curl "http://localhost:8000/sales/net?start_date=2025-02-01&end_date=2025-02-07&restaurant_ids=GUID1&restaurant_ids=GUID2"
```

Response:

```json
[
  {
    "restaurantGuid": "GUID1",
    "restaurantName": "Newk's Jackson",
    "businessDate": "2025-02-01",
    "netSalesAmount": 12345.67
  },
  ...
]
```

### `GET /api/toast-pricing-export`

Exports menu pricing to CSV. Supports location grouping and tier grouping.

Query parameters (optional):
- `group_by_tier=true` — use `backend/price_tier_mapping.csv` to aggregate by pricing tier.
- `per_location=true` — export by location (default behavior when `group_by_tier=false`).

Example:
```bash
curl "http://localhost:8000/api/toast-pricing-export?group_by_tier=true"
```

### `GET /api/toast-pricing-export?group_by_tier=true&diagnostics=true`

Returns JSON diagnostics for tier mapping and (optionally) item-level price traces.

Optional:
- `diagnostics_item=Chicken%20Bacon%20Club` (name contains match) or item GUID.

Example:
```bash
curl "http://localhost:8000/api/toast-pricing-export?group_by_tier=true&diagnostics=true&diagnostics_item=Chicken%20Bacon%20Club"
```

The backend internally:

1. Converts `start_date` / `end_date` to Toast `businessDate` format (`YYYYMMDD`).
2. Calls `POST /eras/v1/metrics` to request a report.
3. Polls `GET /era/v1/metrics/{reportRequestGuid}` until the report is ready.
4. Normalizes the rows to `NetSalesRow` and returns them.

### `POST /api/menu/export-missing-consolidated`

Exports menu items and modifiers with sales in the last 90 days that do **not**
match a ConsolidatedCat in the mapping sheets. Writes an Excel file to:

- `Missing_ConsolidatedCat_Items.xlsx` (repo root)

Response includes output path and counts.

### `GET /api/menu-performance`

Get menu item sales performance data, enriched with custom report categories.

Query parameters:
- `start_date` (required) — `YYYY-MM-DD`
- `end_date` (required) — `YYYY-MM-DD`
- `restaurant_ids` (optional, repeatable) — one or more `restaurantGuid`s
- `category_ids` (optional, repeatable) — filter to items with at least one of these category IDs
- `include_uncategorized` (optional, boolean, default: true) — include items without category assignments

Examples:

```bash
# All active locations, last 7 days, all categories
curl "http://localhost:8000/api/menu-performance?start_date=2025-02-01&end_date=2025-02-07"

# Specific locations with category filter
curl "http://localhost:8000/api/menu-performance?start_date=2025-02-01&end_date=2025-02-07&restaurant_ids=GUID1&restaurant_ids=GUID2&category_ids=cat-uuid-1&include_uncategorized=false"
```

Response:

```json
[
  {
    "restaurantGuid": "guid1",
    "restaurantName": "Newk's Jackson",
    "businessDate": "2025-02-01",
    "menuItemGuid": "item-guid-123",
    "menuItemName": "Italian Sandwich",
    "categoryIds": ["cat-uuid-1", "cat-uuid-2"],
    "categoryNames": ["Sandwiches", "LTO"],
    "quantitySold": 45.0,
    "netSalesAmount": 675.50
  },
  ...
]
```

The backend internally:

1. Converts `start_date` / `end_date` to Toast `businessDate` format (`YYYYMMDD`).
2. Calls `POST /era/v1/menu` (or `/menu/{timeRange}`) with `groupBy: ["MENU_ITEM"]` to request a menu performance report.
3. Polls `GET /era/v1/menu/{reportRequestGuid}` until the report is ready.
4. Loads local category mappings from `item_category_assignments.json`.
5. Enriches each row with category IDs and names.
6. Applies category and uncategorized filters.
7. Returns enriched `MenuPerformanceRow` objects.

### `GET /menus`

Fetches menu structure from Toast Menus API v2. Returns hierarchical menu data:
menus → menuGroups → menuItems → modifierGroups → modifierOptions.

Query parameters:
- `restaurant_guid` (optional) — Restaurant GUID. If not provided, uses first available location.
- `refresh` (optional, boolean) — Force refresh from API, bypassing cache (default: false)

Menu data is cached for 5 minutes to reduce API calls.

Response structure:
```json
{
  "menus": [
    {
      "guid": "...",
      "name": "Main Menu",
      "menuGroups": [
        {
          "guid": "...",
          "name": "Sandwiches",
          "menuItems": [...],
          ...
        }
      ]
    }
  ],
  "modifierGroupReferences": {...},
  "modifierOptionReferences": {...}
}
```

### Category Management (Report Categories)

**`GET /api/report-categories`**

Returns all custom report categories:
```json
[
  {
    "id": "uuid",
    "name": "Salads",
    "description": "All salad items"
  },
  ...
]
```

**`POST /api/report-categories`**

Creates a new category. Body:
```json
{
  "name": "Salads",
  "description": "All salad items"  // optional
}
```

Enforces no duplicate names (case-insensitive).

**`PUT /api/report-categories/{category_id}`**

Updates an existing category. Same body as POST.

**`DELETE /api/report-categories/{category_id}`**

Deletes a category. Also removes it from all item assignments.

### Item-Category Mappings

**`GET /api/item-categories/{item_guid}`**

Returns categories assigned to a specific Toast menu item/modifier GUID:
```json
[
  {
    "id": "uuid",
    "name": "Salads",
    "description": "All salad items"
  },
  ...
]
```

Returns empty array `[]` if no categories assigned.

**`POST /api/item-categories/bulk-assign`**

Assigns a category to multiple items. Body:
```json
{
  "itemGuids": ["guid1", "guid2", ...],
  "categoryId": "category-uuid"
}
```

Idempotent - safe to call multiple times with same data.

**`POST /api/item-categories/bulk-remove`**

Removes a category from multiple items. Body:
```json
{
  "itemGuids": ["guid1", "guid2", ...],
  "categoryId": "category-uuid"
}
```

No error if category wasn't assigned.

### Data Persistence

Categories and mappings are stored in JSON files:
- `report_categories.json` — All report categories
- `item_category_assignments.json` — Item GUID → category ID mappings

These files persist across server restarts and are auto-created when first used.

## 5. Architecture Notes

**Toast Integration (Read-Only):**
- This app is a reporting/dimension layer on top of Toast
- Toast remains the system of record for menus, prices, and live configuration
- All data flows one-way: Toast → App
- Menu data is cached for 5 minutes to reduce API calls

**R365-Style Dimension Layer:**
- Custom categories are stored locally in this app (not in Toast)
- Categories can be assigned to any Toast menu item/modifier by GUID
- This allows creating Newk's-specific reporting dimensions that don't exist in Toast

## 6. Troubleshooting tips

If you get errors:

- `RuntimeError: TOAST_API_TOKEN is not set` or missing credentials  
  → Create `.env` file with required credentials (see Setup section above).

- 401 / 403 from Toast Analytics API  
  → Token may be invalid, expired, or not scoped for Analytics. Confirm with Toast docs and your admin.

- 401 / 403 from Toast Menus API  
  → Standard API credentials may be missing or invalid. Ensure `TOAST_STANDARD_API_CLIENT_ID` and `TOAST_STANDARD_API_CLIENT_SECRET` are set. You may need to contact Toast support to enable Menus API access.

- 404 from `/menus` endpoint  
  → No published menu data found for the restaurant. Ensure the restaurant GUID is correct and menu data has been published in Toast.

- 504 "Timed out waiting for metrics report"  
  → Toast is taking too long. Try narrowing the date range or limiting restaurant IDs.

- Category duplicate name error  
  → Category names are case-insensitive and must be unique. Check existing categories.

If you want to change how the metrics are requested (e.g. groupBy, revenue centers),
look at `request_metrics_report()` in `main.py`. That is the main customization point.

## 7. Importing Aloha data (SQL)

YoY and menu-comparison reports can use historical Aloha data stored under `backend/data/aloha/`. You already have a small subset (e.g. Jan 2024). To **import more** from your Aloha SQL Server:

### 1. Configure environment

In `backend/.env` (copy from `.env.example` if needed), set:

- `ALOHA_SQL_SERVER` — SQL Server host
- `ALOHA_SQL_DATABASE` — Database name
- `ALOHA_SQL_USERNAME` / `ALOHA_SQL_PASSWORD` — Or use `ALOHA_SQL_TRUSTED_CONNECTION=true` on Windows
- `ALOHA_SQL_QUERY` — Query template with `{start_date}` and `{end_date}` (YYYY-MM-DD). It must return columns that map to: **Item**, **Quantity**, **Sales**, **BusinessDate** (column names are matched case-insensitively). You can optionally include **Location** (or Store, Restaurant, etc.) to keep data per location.

**All locations (single total per item per day):**

```env
ALOHA_SQL_QUERY=SELECT Item, Quantity, Sales, BusinessDate FROM AlohaSales WHERE BusinessDate >= '{start_date}' AND BusinessDate <= '{end_date}'
```

**Per-location (so YoY can filter by selected Toast locations):**

```env
ALOHA_SQL_QUERY=SELECT Item, Quantity, Sales, BusinessDate, StoreId AS Location FROM AlohaSales WHERE BusinessDate >= '{start_date}' AND BusinessDate <= '{end_date}'
```

**Order mode (channel breakdown):** The importer recognizes **OrderMode** (or name-style columns like OrderMode_PrintName, OrderTypeName) and **FKOrderModeId** / OrderModeId. If your query returns only `FKOrderModeId`, it is mapped to OrderMode and stored as the numeric ID (e.g. "1", "2"). To store human-readable names instead, JOIN to your Aloha OrderMode lookup table and return the name as `OrderMode`, e.g.:

```sql
SELECT m.Item, m.Quantity, m.Sales, m.DateOfBusiness AS BusinessDate,
       om.PrintName AS OrderMode
FROM dbo.MenuMix m
LEFT JOIN dbo.OrderMode om ON om.Id = m.FKOrderModeId
WHERE m.DateOfBusiness >= '{start_date}' AND m.DateOfBusiness <= '{end_date}'
```

(Adjust table/column names to match your schema; run `python schema_discovery.py` to list columns.)

If your query returns a **Location** (or Store, Restaurant, etc.) column, the importer keeps it. Each day’s CSV will then have one row per (Location, Item) with Quantity and Sales. The app can then filter Aloha data by location when you run a report for specific Toast restaurants (see **Location mapping** below).

### 2. Run the importer from the backend folder

**Custom date range** (e.g. more of 2024, or a new year):

```bash
cd backend
python import_aloha_data.py --sql --sql-start 2024-02-01 --sql-end 2024-12-31
```

**Last N days** (e.g. last 90 days from today):

```bash
python import_aloha_data.py --sql-last-days 90
```

**Overwrite existing dates** (re-import and replace files you already have):

```bash
python import_aloha_data.py --sql --sql-start 2024-01-01 --sql-end 2024-01-31 --overwrite
```

**Larger batches** (if the default 31-day batches are slow or timing out):

```bash
python import_aloha_data.py --sql --sql-start 2024-01-01 --sql-end 2024-06-30 --batch-days 14
```

### 3. Where data goes

- One CSV per day: `backend/data/aloha/aloha_YYYY-MM-DD.csv` (columns: Item, Quantity, Sales; or Item, Quantity, Sales, Location if your query includes location).
- Index: `backend/data/aloha/index.json` (updated automatically).

The app uses these files when you run menu reports with YoY enabled and select an “Aloha date” for comparison.

### 4. Separating by location

- **Does the import pull data for every location?**  
  Yes. Your SQL query can return all locations (e.g. no `WHERE` on store), and the importer writes one row per (Location, Item) per day when you include a Location column.

- **How do I separate Aloha data by location in reports?**  
  1. **Include Location in your SQL query** (e.g. `StoreId AS Location` or your Aloha location column). Re-import the date range so the CSVs have the Location column.  
  2. **Add a mapping** from Toast restaurant GUID to Aloha location ID: create `backend/data/aloha/aloha_location_mapping.json` (copy from `aloha_location_mapping.example.json`). Format:
     ```json
     {
       "toast-restaurant-guid-from-toast": "aloha_store_id_or_name",
       "another-toast-guid": "2"
     }
     ```
     Use the same Toast location GUIDs you see when selecting locations in the report (or from the Toast API). Use the same Location values that appear in your Aloha SQL/CSV (e.g. store number or name).  
  3. When you run a menu report with YoY and select specific **locations**, the app will use only Aloha rows whose Location matches the mapping for those Toast locations. If no mapping file exists (or a location has no mapping), Aloha data is used for all locations combined.







  runas /netonly /user:NEWKS\kaylesworth "cmd /k cd /d `"C:\Users\KimAylesworth\OneDrive - Newk's Eatery\Desktop\Kim\toast_net_sales_app\backend`" && python import_aloha_data.py --sql --sql-start 2024-01-01 --sql-end 2025-01-01"
