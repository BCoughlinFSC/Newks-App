# Meal Card Reload Bot - one-time setup
# Creates a private Python environment in this folder and installs the two
# packages the bot needs. Run this once; after that use "Run Meal Card Reload".

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "       MEAL CARD RELOAD BOT - First-time setup"     -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Find Python
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Host "ERROR: Python is not installed (or not on your PATH)." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it from https://www.python.org/downloads/ and tick" -ForegroundColor Yellow
    Write-Host "'Add python.exe to PATH' on the first screen, then run this again." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}
Write-Host "Found Python: $python" -ForegroundColor Gray

# 2. Create the virtual environment
$venvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "Virtual environment already exists - reusing it." -ForegroundColor Gray
} else {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    & $python -m venv .venv
    if (-not (Test-Path $venvPython)) {
        Write-Host "ERROR: could not create the virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
}

# 3. Install the packages
# Deliberately NOT running 'playwright install'. The bot attaches to the Chrome
# you are already logged into (connect_over_cdp) and never launches a browser of
# its own, so Playwright's ~400MB of bundled browsers are not needed.
Write-Host "Installing packages (playwright, httpx)..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: package install failed. See the messages above." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "You can now run 'Run Meal Card Reload.ps1' each month." -ForegroundColor Green
Read-Host "Press Enter to close"
