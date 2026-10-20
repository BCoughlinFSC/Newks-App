# Meal Card Reload - one-click monthly launcher
# Opens Chrome in debug mode to Paytronix, waits for you to log in,
# then runs the reload bot against the card list in the reporting app.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "        MEAL CARD RELOAD - Monthly Run"            -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ("Period this run: {0}  (auto-set from current month)" -f (Get-Date).Month) -ForegroundColor Gray
Write-Host ""

# 0. Make sure setup has been run
$venvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "This looks like the first run on this PC." -ForegroundColor Yellow
    Write-Host "Running setup.ps1 for you..." -ForegroundColor Yellow
    Write-Host ""
    & (Join-Path $ScriptDir "setup.ps1")
    if (-not (Test-Path $venvPython)) {
        Write-Host "Setup did not finish. Fix the errors above and try again." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
}

# 1. Locate Chrome
$chromeCandidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    Write-Host "ERROR: Could not find chrome.exe. Is Google Chrome installed?" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

# 2. Launch Chrome in debug mode, opening the Paytronix Account Lookup page
Write-Host "Opening Chrome (debug mode) to Paytronix..." -ForegroundColor Yellow
Start-Process $chrome -ArgumentList @(
    '--remote-debugging-port=9222',
    '--user-data-dir=C:/chrome-debug',
    'https://www.pxsweb.com/accountlookup/accountlookup'
)

# 3. Wait for the human login step
Write-Host ""
Write-Host "  STEP 1: In the Chrome window that just opened, log into Paytronix." -ForegroundColor Green
Write-Host "  STEP 2: Make sure you land on the Account Lookup page." -ForegroundColor Green
Write-Host ""
Read-Host "When you are logged in and ready, press Enter"

# 4. Offer a dry run first - reads every balance, loads nothing
Write-Host ""
Write-Host "A dry run reads each card's balance and shows what WOULD be loaded," -ForegroundColor Gray
Write-Host "without adding any money. Worth doing if anything has changed." -ForegroundColor Gray
$dry = Read-Host "Dry run first? (y/N)"

Write-Host ""
if ($dry -match '^[Yy]') {
    Write-Host "Running a DRY RUN (nothing will be loaded)..." -ForegroundColor Yellow
    & $venvPython (Join-Path $ScriptDir "meal_card_reload.py") --dry-run
    Write-Host ""
    $go = Read-Host "Dry run finished. Run the real reload now? (y/N)"
    if ($go -notmatch '^[Yy]') {
        Write-Host "Stopped. Nothing was loaded." -ForegroundColor Cyan
        Read-Host "Press Enter to close this window"
        exit 0
    }
    Write-Host ""
}

Write-Host "Running the reload bot..." -ForegroundColor Yellow
& $venvPython (Join-Path $ScriptDir "meal_card_reload.py")

Write-Host ""
Write-Host "Finished. A timestamped log was saved in this folder," -ForegroundColor Cyan
Write-Host "and the run is recorded on the Meal Cards page in the app." -ForegroundColor Cyan
Read-Host "Press Enter to close this window"
