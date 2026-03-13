# Newk's Menu Board (React Web App)

A digital menu board React web app that supports:
- Menu board display with customizable layouts
- Free-form grid layout editor with drag-and-drop
- Multi-screen support for TV displays
- Image and video content blocks
- Auto-refresh for live TV deployment
- Admin authentication for settings protection

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

## Production deployment

1) Build the app
```bash
npm run build:prod
```

2) Start production server
```bash
npm start
```

The app will be served at `/menuboard/`. TVs access:
`https://yourdomain.com/menuboard/menu-board?screen=1&location=LOCATION_ID`

## Backend endpoints

- `GET /api/menu` -> menu JSON used by the UI
- `GET /api/toast/menu` -> Toast menu proxy (returns mock if no creds)
- `POST /api/toast/prices` -> Toast pricing proxy (demo calc if no creds)
- `GET /api/toast/locations` -> Toast locations (demo location if no creds)
- `GET /api/board-config/:locationId` -> board config for a location
- `POST /api/board-config/:locationId` -> save board config (admin-protected)
- `GET /api/board-config/:locationId/version` -> config version hash (for polling)

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
ADMIN_TOKEN=your-secret-token
```

If Toast authentication fails, the app shows a setup error screen with actions to retry or switch to demo mode. Set `TOAST_FORCE_DEMO=true` to always use the demo menu and pricing (handy for demos without credentials).

Set `ADMIN_TOKEN` to protect settings/uploads with a password. Leave blank to disable auth.

## Configuration API fallback

If the Menus V2 endpoint returns 404 (commonly due to external ID mismatches or unpublished menus), the app will attempt a fallback build using the Configuration API (`/config/v2`), which reconstructs the menu via multiple calls. This fallback uses the Standard API credentials.

## Build for the unified app

Running `npm run build:menuboard` outputs a static build to `backend/menuboard`, which is served by the FastAPI backend at `/menuboard`.

## Where to wire Toast / Olo later

- `src/core/providersImpl.ts`
  - Replace `MockMenuProvider` with `ToastMenuProvider`
  - Replace `SimplePricingProvider` with a Toast `/prices` proxy
  - Replace `MockOrderSubmitter` with `OloOrderSubmitter` when you have Ordering API creds
