# Meal Card Reload - TEST RUN
#
# Does everything the real monthly run does EXCEPT add money: it signs into the
# app, fetches the card list, looks each card up in Paytronix and reads its
# balance, and works out what it would add. The adjustment form is never filled
# in or submitted, and no card is ticked off.
#
# This is the one to use to check your setup works before a real reload.
# It cannot move money - there is no prompt here that turns it into a real run.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "      MEAL CARD RELOAD - TEST RUN (no money)"       -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Nothing will be added to any card. This reads balances only." -ForegroundColor Green
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

# 4. Offer to check only the first few cards, so a test does not take as long
#    as a real run just to prove the setup works.
Write-Host ""
Write-Host "The full list takes a while. For a quick check, try the first few." -ForegroundColor Gray
$count = Read-Host "How many cards should it check? (Enter for all)"

$botArgs = @((Join-Path $ScriptDir "meal_card_reload.py"), "--dry-run")
if ($count -match '^\d+$') {
    $botArgs += @("--max-cards", $count)
    Write-Host ""
    Write-Host "Checking the first $count card(s)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Running the TEST (nothing will be loaded)..." -ForegroundColor Yellow
& $venvPython @botArgs

Write-Host ""
Write-Host "Test finished. No money was added to any card." -ForegroundColor Green
Write-Host "A log was saved in this folder, and the run shows on the Meal Cards" -ForegroundColor Cyan
Write-Host "page in the app labelled as a dry run." -ForegroundColor Cyan
Read-Host "Press Enter to close this window"
