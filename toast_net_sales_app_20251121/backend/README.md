# Backend (FastAPI) for Newk's Toast Net Sales App

This folder contains a small FastAPI backend that talks to the Toast Analytics API
and exposes a simple REST API for:

- Listing locations
- Getting net sales by day, by location, for a date range

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

- `TOAST_API_BASE_URL` — something like `https://api.toasttab.com/era/v1` (or your env)
- `TOAST_API_TOKEN` — your bearer token (keep this secret)

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

- `http://localhost:8000/` — main UI
- `http://localhost:8000/docs` — interactive API docs (Swagger UI)
- `http://localhost:8000/redoc` — alternative API docs

## 4. Key endpoints

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

The backend internally:

1. Converts `start_date` / `end_date` to Toast `businessDate` format (`YYYYMMDD`).
2. Calls `POST /eras/v1/metrics` to request a report.
3. Polls `GET /era/v1/metrics/{reportRequestGuid}` until the report is ready.
4. Normalizes the rows to `NetSalesRow` and returns them.

## 5. Troubleshooting tips

If you get errors:

- `RuntimeError: TOAST_API_TOKEN is not set`  
  → Create `.env` or set env vars before starting uvicorn.

- 401 / 403 from Toast  
  → Token may be invalid, expired, or not scoped for Analytics. Confirm with Toast docs
  and your admin.

- 504 "Timed out waiting for metrics report"  
  → Toast is taking too long. Try narrowing the date range or limiting restaurant IDs.

If you want to change how the metrics are requested (e.g. groupBy, revenue centers),
look at `request_metrics_report()` in `main.py`. That is the main customization point.
