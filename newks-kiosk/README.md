# Newk's Kiosk (React Web Demo)

A kiosk-style React web app demo that supports:
- Menu browse
- Item detail with modifier selections
- Cart with quantity controls
- Checkout + demo submission (no payments, no POS/Kitchen routing)

## Run it locally

1) Install deps
```bash
npm install
```

2) Start backend API
```bash
npm run dev:server
```

3) Start dev server
```bash
npm run dev
```

Open the URL shown (usually http://localhost:5173).

## Backend endpoints (demo)

- `GET /api/menu` -> menu JSON used by the UI
- `GET /api/toast/menu` -> Toast menu proxy (returns mock if no creds)
- `POST /api/toast/prices` -> Toast pricing proxy (demo calc if no creds)
- `GET /api/toast/locations` -> Toast locations (demo location if no creds)

## Toast env vars

Create a `.env` file at the repo root (or use the provided `.env.example` template) and run the backend with it:

```bash
TOAST_ACCESS_TOKEN=...
TOAST_MENU_URL=...
TOAST_PRICING_URL=...
TOAST_LOCATIONS_URL=...
TOAST_API_CLIENT_ID=...
TOAST_API_CLIENT_SECRET=...
TOAST_OAUTH_TOKEN_URL=...
TOAST_FORCE_DEMO=true
```

If Toast authentication fails, the kiosk now shows a setup error screen with actions to retry or switch to demo mode. Set `TOAST_FORCE_DEMO=true` to always use the demo menu and pricing (handy for demos without credentials).

## Configuration API fallback

If the Menus V2 endpoint returns 404 (commonly due to external ID mismatches or unpublished menus), the kiosk will attempt a fallback build using the Configuration API (`/config/v2`), which reconstructs the menu via multiple calls. This fallback uses the Standard API credentials.

## Build for the unified app

Running `npm run build:kiosk` outputs a static build to `backend/kiosk`, which is served by the FastAPI backend at `/kiosk`.

## How to use in Cursor

- Open this folder in Cursor
- Let the agent run `npm install` and `npm run dev`
- Ask it to:
  - Replace `mockMenu` with a Toast-backed provider
  - Add a backend proxy for Toast / Olo credentials
  - Add a nicer kiosk layout / accessibility / idle attract loop

## Where to wire Toast / Olo later

- `src/kiosk/providersImpl.ts`
  - Replace `MockMenuProvider` with `ToastMenuProvider`
  - Replace `SimplePricingProvider` with a Toast `/prices` proxy
  - Replace `MockOrderSubmitter` with `OloOrderSubmitter` when you have Ordering API creds

