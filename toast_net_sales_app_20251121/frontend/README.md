# Frontend for Newk's Net Sales App

This is a very small HTML + JavaScript frontend that calls the FastAPI backend to:

- Load the list of active locations
- Run a net sales report for a given date range and set of locations
- Display the results in a simple table, plus a quick summary total

## How it's served

The FastAPI backend in `../backend/main.py` mounts this folder as static files at the root (`/`).
So once the backend is running, you can open:

- `http://localhost:8000/` — main UI

No additional build steps are required. It's just a static HTML file with inline JS.

## Where to extend

If you want to extend the UI:

- Add more filters (e.g., per-day totals vs per-location totals).
- Add export to CSV (you can build a CSV in JS and trigger a download).
- Add charts using a JS chart library (e.g., Chart.js) if allowed.

For LLMs helping with frontend work:

- Treat this as a single-file HTML/JS app.
- If you need to add more complexity (multiple files, frameworks), describe the desired
  structure clearly and show exactly where to insert `<script>` tags or imports.
