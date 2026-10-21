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
function Find-Python {
    foreach ($candidate in @("python", "python3", "py")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Windows ships a fake "python" that only opens the Microsoft Store.
        # It answers Get-Command but is not an interpreter, so make it prove it.
        $version = & $cmd.Source --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$version" -match "Python 3") {
            return $cmd.Source
        }
    }
    return $null
}

$python = Find-Python

if (-not $python) {
    Write-Host "Python is not installed on this PC (or is not on your PATH)." -ForegroundColor Yellow
    Write-Host ""
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "It can be installed for you now. This installs Python and nothing" -ForegroundColor Gray
        Write-Host "else, and changes no other setting on the machine." -ForegroundColor Gray
        Write-Host ""
        $go = Read-Host "Install Python now? (y/N)"
        if ($go -match '^[Yy]') {
            Write-Host ""
            Write-Host "Installing Python - this takes a couple of minutes..." -ForegroundColor Yellow
            & winget install --id Python.Python.3.12 --source winget `
                --accept-package-agreements --accept-source-agreements
            # winget adds Python to the PATH of NEW shells, not this one, so
            # pull the updated PATH in rather than asking for a restart.
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") +
                        ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            $python = Find-Python
        }
    }
}

if (-not $python) {
    Write-Host ""
    Write-Host "Python is still not available, so setup cannot continue." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it from https://www.python.org/downloads/ - and on the FIRST" -ForegroundColor Yellow
    Write-Host "screen of the installer, tick 'Add python.exe to PATH'. That tick box" -ForegroundColor Yellow
    Write-Host "is the one people miss; without it this script cannot find Python." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Then run this setup again." -ForegroundColor Yellow
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
