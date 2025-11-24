# Newk's Toast Net Sales Web App

A simple internal tool for Newk's Restaurant Group that:

- Connects to Toast's Analytics API (read-only)
- Fetches net sales by day, by location, for a date range
- Displays data in a spreadsheet/pivot table format
- Allows CSV export for further analysis
- Uses a FastAPI backend and static HTML/JS frontend

## 🚀 Quick Start (One-Click Launch)

**Just double-click `START_APP.bat`** - it handles everything automatically!

The launcher will:
1. ✅ Check for Python (prompts to install if missing)
2. ✅ Create virtual environment (first time only)
3. ✅ Install dependencies (first time only)
4. ✅ Check for credentials (prompts if missing)
5. ✅ Start the server
6. ✅ Open your browser automatically

## 📋 First-Time Setup

### Option 1: Automated Setup (Recommended)

1. **Run setup script:**
   - Double-click `setup.bat`
   - This creates the virtual environment and installs dependencies

2. **Add Toast API credentials:**
   - Edit `backend\.env`
   - Add your `TOAST_API_CLIENT_ID` and `TOAST_API_CLIENT_SECRET`
   - Set `TOAST_API_BASE_URL` (usually `https://ws-api.toasttab.com/era/v1`)

3. **Launch the app:**
   - Double-click `START_APP.bat`
   - The app opens automatically in your browser

### Option 2: Manual Setup

1. **Install Python 3.10+** (if not already installed)
   - Download from https://www.python.org/
   - Make sure to check "Add Python to PATH" during installation

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

## 📁 Structure

- `backend/`
  - `main.py` — FastAPI app, including endpoints and static file serving
  - `requirements.txt` — Python dependencies
  - `.env.example` — sample environment config
  - `README.md` — backend-specific instructions

- `frontend/`
  - `index.html` — static HTML/JS UI

- `START_APP.bat` — **One-click launcher** (double-click to run)
- `setup.bat` — Automated setup script
- `GITHUB_SETUP.md` — Instructions for sharing via GitHub

## 🔐 Security

- **Never commit `.env` files** - they contain your API credentials
- The `.gitignore` file automatically excludes sensitive files
- Share credentials separately via secure channel

## 📤 Sharing with Colleagues

### Option 1: GitHub (Recommended)

See `GITHUB_SETUP.md` for detailed instructions. Benefits:
- Easy updates (push changes, colleagues pull)
- Version control
- Professional workflow

### Option 2: Direct Share

1. Zip the project (excluding `venv/` and `.env`)
2. Share the zip file
3. Share credentials separately
4. Colleagues run `setup.bat`, then `START_APP.bat`

## 🐛 Troubleshooting

- **"Python is not installed"**: Install Python 3.10+ from python.org
- **"Port 8000 already in use"**: Another app is using port 8000. Close it or change the port in `START_APP.bat`
- **"Module not found"**: Run `setup.bat` to install dependencies
- **"No credentials"**: Edit `backend\.env` with your Toast API credentials

## 📝 What You'll See

- **Filters card**: Select date range and locations
- **Results table**: Spreadsheet-style pivot table with locations as rows, dates as columns
- **Download CSV button**: Export data for Excel/Google Sheets
- **Summary**: Total net sales across selected locations and dates

## 🔄 Updates

If you got this from GitHub:
```powershell
git pull
```

If you got this as a zip, download the latest version.

---

**Questions?** See `backend/README.md` for detailed backend documentation.
