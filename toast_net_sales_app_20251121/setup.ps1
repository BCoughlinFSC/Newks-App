# Newk's Toast Net Sales App - Setup Script (PowerShell)
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Newk's Toast Net Sales App - Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Python is installed
try {
    $pythonVersion = python --version 2>&1
    Write-Host "[1/4] Python found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Python is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Python 3.10+ from https://www.python.org/" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

# Create virtual environment
Write-Host ""
Write-Host "[2/4] Creating virtual environment..." -ForegroundColor Yellow
Set-Location backend

if (Test-Path venv) {
    Write-Host "Virtual environment already exists. Skipping creation." -ForegroundColor Gray
} else {
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to create virtual environment" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "Virtual environment created." -ForegroundColor Green
}

# Install dependencies
Write-Host ""
Write-Host "[3/4] Installing dependencies..." -ForegroundColor Yellow
& .\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "Dependencies installed successfully." -ForegroundColor Green

# Setup .env file
Write-Host ""
Write-Host "[4/4] Setting up environment file..." -ForegroundColor Yellow
if (Test-Path .env) {
    Write-Host ".env file already exists. Skipping." -ForegroundColor Gray
} else {
    if (Test-Path .env.example) {
        Copy-Item .env.example .env
        Write-Host "Created .env file from .env.example" -ForegroundColor Green
        Write-Host ""
        Write-Host "IMPORTANT: You need to edit backend\.env and add your Toast API credentials!" -ForegroundColor Yellow
    } else {
        Write-Host "WARNING: .env.example not found. You'll need to create .env manually." -ForegroundColor Yellow
    }
}

Set-Location ..

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Edit backend\.env and add your Toast API credentials:" -ForegroundColor White
Write-Host "   - TOAST_API_BASE_URL" -ForegroundColor Cyan
Write-Host "   - TOAST_API_CLIENT_ID" -ForegroundColor Cyan
Write-Host "   - TOAST_API_CLIENT_SECRET" -ForegroundColor Cyan
Write-Host "2. Start the server:" -ForegroundColor White
Write-Host "   cd backend" -ForegroundColor Cyan
Write-Host "   .\venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host "   uvicorn main:app --reload" -ForegroundColor Cyan
Write-Host "3. Visit http://localhost:8000/ in your browser" -ForegroundColor White
Write-Host ""
Read-Host "Press Enter to exit"

