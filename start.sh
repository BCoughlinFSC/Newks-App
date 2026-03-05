#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Newk's Reporting Platform – Cloudways Startup Script
# ─────────────────────────────────────────────────────────────
# Run this once manually to test, then let Supervisor manage it.
# Supervisor config: supervisor.conf
# ─────────────────────────────────────────────────────────────

set -e

APP_DIR="$(cd "$(dirname "$0")/backend" && pwd)"

echo "Starting Newk's Reporting Platform..."
echo "App directory: $APP_DIR"
echo ""

cd "$APP_DIR"

# Activate virtual environment
source venv/bin/activate

# Start the server — listens on all interfaces so Nginx can proxy to it
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
