# Digital Orders by Platform (OLO) – Setup & Testing

This report uses the **OLO Ordering API** Order Export. It lists available order batches, lets you select batches, then aggregates orders by **Platform** and **Store** (order count, total sales, subtotal, discount, tax, tip, delivery fee).

## Prerequisites

- OLO API credentials with **Signature (Server-to-Server)** authorization and **Order Export** access.  
  Order Export is only available with Signature auth (not Key/API key).  
  In the OLO Developer Portal, the project name usually includes “Ordering” and supports Order Export.

## 1. Configure environment

In `backend/.env` add your OLO credentials:

```env
OLO_CLIENT_ID=your_olo_client_id
OLO_CLIENT_SECRET=your_olo_client_secret
```

The app uses **production** (`https://ordering.api.olo.com/v1.1`) by default. You don't need to set a base URL unless you want to point at sandbox for testing (then set `OLO_ORDERING_BASE_URL=https://ordering.api.olosandbox.com/v1.1`).

Get Client ID and Client Secret from the OLO Developer Portal → your project → API Keys. Only the project owner can see the Client Secret.

## 2. Start the app

From the project root:

```bash
cd backend
uvicorn main:app --reload
```

Or use `START_APP.bat` if you use the launcher.

## 3. Test in the UI

1. Open **http://localhost:8000/** and go to **Reports → Digital Orders by Platform (OLO)**, or open **http://localhost:8000/olo.html** directly.
2. Click **Load batches**.  
   - If credentials are missing or wrong, you’ll see an error (e.g. 400 “OLO_CLIENT_ID and OLO_CLIENT_SECRET must be set” or 403 from OLO).
   - If credentials are valid and Order Export is enabled, you’ll see a list of batches (batch ID and generated date).
3. Select one or more batches (checkboxes), then click **Run Digital Orders by Platform**.
4. Confirm the table shows rows aggregated by Platform and Store, with order count and dollar columns.
5. Click **Export CSV** to download `digital_orders_by_platform.csv`.

## 4. Test via API (optional)

- List batches:
  ```bash
  curl -s http://localhost:8000/api/olo/batches
  ```
- Run report for specific batch IDs (use IDs from the list above):
  ```bash
  curl -s -X POST http://localhost:8000/api/olo/digital-orders-by-platform \
    -H "Content-Type: application/json" \
    -d "{\"batch_ids\": [12345]}"
  ```

## 5. Matching your “Digital Orders by Platform” example

The report columns are: **Platform**, **Store**, **Order count**, **Total sales**, **Subtotal**, **Discount**, **Tax**, **Tip**, **Delivery fee**.

- **Platform** is derived from OLO’s `orderingProvider` (name/slug), then `source` (Web, MobileWeb, SMS, API, CallCenter), then `clientPlatform`.
- If your example Excel uses different column names or groupings (e.g. date ranges, different platform labels), you can either:
  - Adjust the aggregation in `backend/olo_client.py` (`aggregate_digital_orders_by_platform`), or
  - Run the script `backend/read_example_report.py` (after placing your example file at `c:\Users\KimAylesworth\Downloads\Digital Orders by Platform-Example.xlsx`) to print its sheets and columns so we can align the report.

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| “OLO_CLIENT_ID and OLO_CLIENT_SECRET must be set” | Add both to `backend/.env` and restart the server. |
| 403 from OLO on `/api/olo/batches` | Credentials must have **Signature** auth and **Order Export** enabled. Confirm in Developer Portal. |
| No batches listed | Order Export may not be enabled for your brand, or no batches exist yet (e.g. new sandbox). Contact OLO if needed. |
| Report fails on “Run” | One of the selected batches may be expired (batches are available ~30 days). Try a different batch. |

## References

- OLO Order Export: Order Export section in the Ordering API docs (GET/POST/DELETE `/orderexports`).
- Signature auth: “Creating a Signed Authorization Header” in the OLO ordering description doc.
