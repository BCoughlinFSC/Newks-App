# Newk's Toast Net Sales Web App

A simple internal tool for Newk's Restaurant Group that:

- Connects to Toast's Analytics API (read-only)
- Fetches net sales by day, by location, for a date range
- Displays data in a spreadsheet/pivot table format
- Allows CSV export for further analysis
- Uses a FastAPI backend and static HTML/JS frontend

## Structure

- `backend/`
  - `main.py` — FastAPI app, including endpoints and static file serving
  - `requirements.txt` — Python dependencies
  - `.env.example` — sample environment config
  - `README.md` — backend-specific instructions

- `frontend/`
  - `index.html` — static HTML/JS UI
  - `README.md` — frontend notes

- `toast-reporting-api.yaml` — Toast API definition (for reference only)
- `LLM_INSTRUCTIONS.md` — suggestions for how to work with another LLM while extending/debugging

## Quick Start (Windows)

### Option 1: Automated Setup (Recommended)

1. **Run the setup script:**
   - Double-click `setup.bat` (or `setup.ps1` in PowerShell)
   - This will create a virtual environment and install dependencies

2. **Configure Toast API credentials:**
   - Edit `backend\.env`
   - Add your `TOAST_API_CLIENT_ID` and `TOAST_API_CLIENT_SECRET`
   - Set `TOAST_API_BASE_URL` (usually `https://ws-api.toasttab.com/era/v1`)

3. **Start the server:**
   ```powershell
   cd backend
   .\venv\Scripts\Activate.ps1
   uvicorn main:app --reload
   ```

4. **Visit `http://localhost:8000/` in your browser**

### Option 2: Manual Setup

1. **Install Python 3.10+** (if not already installed)
   - Download from https://www.python.org/

2. **Create virtual environment:**
   ```powershell
   cd backend
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```

4. **Configure environment:**
   - Copy `backend\.env.example` to `backend\.env`
   - Edit `backend\.env` with your Toast API credentials

5. **Start the server:**
   ```powershell
   uvicorn main:app --reload
   ```

6. **Visit `http://localhost:8000/`**

## What You'll See

- **Filters card**: Select date range and locations
- **Results table**: Spreadsheet-style pivot table with locations as rows, dates as columns
- **Download CSV button**: Export data for Excel/Google Sheets
- **Summary**: Total net sales across selected locations and dates

## Packaging for Distribution

To share this with colleagues:

1. **Exclude these from the package:**
   - `backend/venv/` (virtual environment - let each person create their own)
   - `backend/__pycache__/` (Python cache)
   - `backend/.env` (contains your credentials - never share!)
   - Any `*.pyc` files

2. **Include these files:**
   - All `*.py` files in `backend/`
   - `backend/requirements.txt`
   - `backend/.env.example`
   - `frontend/index.html`
   - `README.md` (this file)
   - `setup.bat` and `setup.ps1`
   - `.gitignore`

3. **Share the credentials separately:**
   - Send Toast API credentials via secure channel
   - Each person creates their own `backend/.env` file

## Troubleshooting

- **Port 8000 already in use**: Use `uvicorn main:app --reload --port 8001`
- **Import errors**: Make sure virtual environment is activated
- **429 Rate Limit errors**: Wait a few minutes between requests
- **See `backend/README.md`** for detailed backend troubleshooting
- **See `LLM_INSTRUCTIONS.md`** for help with LLM-assisted debugging
