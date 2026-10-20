"""
Meal Card Reload Bot - Paytronix Automation

Connects to a Chrome you are already logged into, asks the Newk's reporting app
which cards to load this period, reloads each card's Stored Value wallet on
pxsweb.com, and posts each result back to the app.

Usage:
  1. Run "Run Meal Card Reload.ps1" (it opens Chrome in debug mode for you), or
     launch Chrome yourself with:
     chrome.exe --remote-debugging-port=9222 --user-data-dir="C:/chrome-debug"
  2. Log into pxsweb.com and land on Account Lookup
  3. python meal_card_reload.py            (add --dry-run for a no-write pass)

The card list, the per-card target amounts and the record of what was loaded all
live in the app, not in a spreadsheet - so this folder holds no data and never
goes stale. Period defaults to the current calendar month.

Note on third-party widgets: Paytronix embeds Wootric (NPS survey), Intercom
(chat) and Access-Evo (button) on pxsweb pages. These break automation two ways
- the Wootric modal covers the page and swallows clicks, and its hidden
select/textarea get picked up by generic selectors. Intercom also holds an open
connection forever, so "networkidle" never fires. We block all three at the
network layer and strip any leftovers from the DOM.
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

# -- Config ------------------------------------------------------------------
CHROME_DEBUG_PORT = 9222
STORE_VALUE = "1003"  # 1003 - Flowood MS. Every reload posts to this store.
DEFAULT_TIMEOUT_MS = 20000
# pxsweb can take 25s+ to reach domcontentloaded, so navigation gets its own budget
NAV_TIMEOUT_MS = 45000

LOOKUP_URL = "https://www.pxsweb.com/accountlookup/accountlookup"

# Retries per card. Most failures are transient page-load hiccups, so a second
# or third attempt usually goes through.
CARD_ATTEMPTS = 3

CONFIG_FILE = "config.json"

# Third-party widgets to block. Killing wootric-sdk.js at the network layer is
# what stops the survey from ever appearing. Must be a compiled regex - the
# glob form ("**wootric.com**") silently matches nothing here.
WIDGET_URL_RE = re.compile(r"(wootric|intercom|access-evo)", re.I)

# Selector fragments that exclude widget-owned form controls, so
# locator("select").last can't land on Wootric's hidden survey dropdown.
# (Only still needed on the wallet-selection page, whose select is unnamed.
# The Input Amount form is addressed by name: storenum / delta / comment / op.)
NOT_WIDGET = ':not([id^="wootric"]):not([name^="wootric"]):not([class*="wootric"])'
MAIN_SELECT = f"select{NOT_WIDGET}:visible"


# -- Logger ------------------------------------------------------------------
class Logger:
    def __init__(self, log_file):
        self.terminal = sys.stdout
        self.log = open(log_file, "w", encoding="utf-8")
        self.lines = []

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()
        self.lines.append(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def text(self):
        return "".join(self.lines)

    def close(self):
        self.log.close()


class StepError(Exception):
    """Failure tagged with the workflow step it happened in."""


def step(name, fn, *args, **kwargs):
    """Run a workflow step, tagging any failure with the step name."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        lines = str(e).strip().splitlines()
        detail = lines[0] if lines else e.__class__.__name__
        raise StepError(f"{name}: {detail}") from e


# -- The app ------------------------------------------------------------------
class AppClient:
    """Talks to the Newk's reporting app: the worklist in, results out.

    Authentication is the same login the website uses, so there is no extra
    password or API key to manage - and whoever runs the bot is recorded as
    having run it.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        self.run_id = None

    @classmethod
    def sign_in(cls, base_url: str, email: str, password: str) -> "AppClient":
        r = httpx.post(f"{base_url.rstrip('/')}/api/auth/login",
                       json={"email": email, "password": password}, timeout=60.0)
        if r.status_code == 401:
            raise SystemExit("That email and password were not accepted by the app.")
        r.raise_for_status()
        return cls(base_url, r.json()["access_token"])

    def worklist(self, year=None, period=None, include_done=False) -> dict:
        params = {"include_done": str(include_done).lower()}
        if year:
            params["year"] = year
        if period:
            params["period"] = period
        r = self.client.get("/api/mealcard/worklist", params=params)
        r.raise_for_status()
        return r.json()

    def start_run(self, year: int, period: int) -> int:
        r = self.client.post("/api/mealcard/runs", json={"year": year, "period": period})
        r.raise_for_status()
        self.run_id = r.json()["run_id"]
        return self.run_id

    def record_card(self, card_number, status, amount=None, balance_before=None, detail=""):
        """Post one card's outcome. Never fatal: a reload that happened must not
        be lost to a network blip, so this warns and carries on."""
        if self.run_id is None:
            return
        try:
            r = self.client.post(
                f"/api/mealcard/runs/{self.run_id}/card",
                json={"card_number": card_number, "status": status, "amount": amount,
                      "balance_before": balance_before, "detail": detail},
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  WARNING - could not record this card in the app ({e.__class__.__name__}). "
                  f"Tick it by hand: {card_number} = {status}")

    def finish_run(self, completed, skipped, errors, log):
        if self.run_id is None:
            return
        try:
            self.client.post(
                f"/api/mealcard/runs/{self.run_id}/finish",
                json={"completed": completed, "skipped": skipped, "errors": errors,
                      "log": log[-200000:]},   # keep the tail if a run got chatty
            )
        except Exception as e:
            print(f"  WARNING - could not close out the run in the app ({e.__class__.__name__}).")


def load_config(base_dir: Path) -> dict:
    path = base_dir / CONFIG_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print(f"WARNING - {CONFIG_FILE} is not valid JSON; ignoring it.")
        return {}


def connect_to_app(base_dir: Path, args) -> AppClient:
    cfg = load_config(base_dir)
    base_url = args.app_url or os.getenv("MEALCARD_APP_URL") or cfg.get("app_url")
    email = args.email or os.getenv("MEALCARD_APP_EMAIL") or cfg.get("email")

    if not base_url:
        base_url = input("Reporting app URL: ").strip()
    if not email:
        email = input("Your app email: ").strip()

    password = os.getenv("MEALCARD_APP_PASSWORD")
    if not password:
        password = getpass.getpass(f"App password for {email}: ")

    print(f"Signing in to {base_url} as {email}")
    return AppClient.sign_in(base_url, email, password)


# -- Chrome connection --------------------------------------------------------
def describe_targets() -> list:
    """List Chrome's debug targets over plain HTTP (works even when CDP is stuck)."""
    url = f"http://localhost:{CHROME_DEBUG_PORT}/json/list"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def connect_to_chrome(playwright):
    """Attach to the debug Chrome, explaining clearly if we can't.

    The usual cause of a hang here is an open Chrome dialog (an alert titled
    something like "... says ..."). A pending dialog blocks that tab's renderer,
    so Playwright's attach handshake stalls instead of failing fast.
    """
    try:
        return playwright.chromium.connect_over_cdp(
            f"http://localhost:{CHROME_DEBUG_PORT}", timeout=20000
        )
    except Exception as e:
        print()
        print("=" * 60)
        print("Could not attach to Chrome's debugging port.")
        print("=" * 60)
        try:
            targets = describe_targets()
        except Exception:
            print(f"Chrome is not listening on port {CHROME_DEBUG_PORT} at all.")
            print("Start it with the 'Run Meal Card Reload' launcher, log in, then re-run.")
            raise SystemExit(1)

        print("Chrome IS running. Its open tabs are:")
        for t in targets:
            if t.get("type") == "page":
                print(f"  - {t.get('title', '')[:60]}")
                print(f"      {t.get('url', '')[:80]}")
        print()
        print("Most likely a dialog box is open in one of those tabs, which freezes")
        print("it and blocks the bot from attaching. To fix:")
        print("  1. Switch to the debug Chrome window.")
        print("  2. Read and dismiss any pop-up dialog (click OK / Leave).")
        print("  3. Re-run this script.")
        print()
        print("If there is no dialog, close that Chrome window entirely and start")
        print("over with the launcher (you will need to log in again).")
        print()
        print(f"(underlying error: {str(e).splitlines()[0]})")
        raise SystemExit(1)


# -- Third-party widget suppression -------------------------------------------
def block_widgets(context):
    """Abort requests to Wootric / Intercom / Access-Evo so they never load."""
    def handler(route):
        try:
            route.abort()
        except Exception:
            pass  # page navigated away mid-request
    context.route(WIDGET_URL_RE, handler)


def dismiss_overlays(page):
    """Strip any widget DOM that slipped through (e.g. already-open survey)."""
    try:
        page.evaluate(
            """() => {
                const sel = '#wootric-modal, #wootric-target, #wootric-settings,'
                    + '[id^="wootric"], [class*="wootric"],'
                    + '#intercom-frame, .intercom-lightweight-app,'
                    + '.intercom-launcher-frame, #access-button-iframe';
                document.querySelectorAll(sel).forEach(e => e.remove());
            }"""
        )
    except Exception:
        pass  # page mid-navigation; next call will catch it


# -- Paytronix automation ------------------------------------------------------
def lookup_card(page, card_number: str):
    """Navigate to Account Lookup and search for a card number."""
    page.goto(LOOKUP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    # Deliberately NOT wait_for_load_state("networkidle") - Intercom keeps a
    # connection open, so networkidle never fires and every card times out.
    dismiss_overlays(page)

    card_input = page.locator("#lookup-input-cardNumber")
    card_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    card_input.fill(card_number)

    find_btn = page.locator('button:has-text("Find Account")')
    find_btn.click()
    # wait_until="domcontentloaded" matters: the default is "load", which never
    # fires on these pages, so the wait timed out even after the URL was correct.
    try:
        page.wait_for_url(lambda url: "csrmainmenu" in url,
                          wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except Exception:
        if "csrmainmenu" not in page.url:
            raise Exception(f"account page never loaded (stuck at {page.url})")


def navigate_to_adjust_wallet(page):
    """From CSR Main Menu, click Adjust Wallet link."""
    dismiss_overlays(page)
    link = page.locator('a[href*="editwallet"]').first
    link.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    link.click()
    try:
        page.wait_for_url(lambda url: "editwallet" in url,
                          wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except Exception:
        if "editwallet" not in page.url:
            raise Exception(f"adjust-wallet page never loaded (stuck at {page.url})")


def select_stored_value_wallet(page):
    """Step 1: Select 'Stored Value' wallet and click Next."""
    dismiss_overlays(page)
    page.wait_for_timeout(500)
    wallet_select = page.locator(MAIN_SELECT).last
    wallet_select.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    wallet_select.select_option(label="Stored Value")
    page.wait_for_timeout(300)

    page.locator('input[type="submit"][value="Next"]').first.click(timeout=10000)
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(1000)


def get_current_balance(page) -> float:
    """Read the 'Current Balance:' value from the Input Amount page."""
    dismiss_overlays(page)
    page.wait_for_timeout(500)
    all_text = page.inner_text("body")
    match = re.search(r"Current Balance:\s*([\d,]+\.\d{2})", all_text)
    if match:
        return float(match.group(1).replace(",", ""))
    raise Exception("Could not read current balance from page")


def fill_adjustment_form(page, amount: float, name: str):
    """Step 2: Fill in store, amount, comments and click Next."""
    amount_str = str(int(amount)) if amount == int(amount) else f"{amount:.2f}"

    dismiss_overlays(page)

    # Named fields on the Input Amount form. These beat positional lookups
    # (locator("select").last etc.) which broke whenever a widget injected an
    # extra control into the page.
    page.locator('select[name="storenum"]').select_option(value=STORE_VALUE)

    # Ensure 'Add' radio is checked (it is by default, but don't rely on it)
    add_radio = page.locator('input[name="op"][value="add"]')
    if not add_radio.is_checked():
        add_radio.check()

    page.locator('input[name="delta"]').fill(amount_str)
    page.locator('textarea[name="comment"]').fill(name)

    page.wait_for_timeout(1000)

    # Click Next (let click() handle scrolling itself; manual scroll fails if page is settling)
    next_btn = page.locator('input[type="submit"][value="Next"]').first
    next_btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    next_btn.click(timeout=10000)
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(1000)


def confirm_adjustment(page):
    """Step 3: Click 'Adjust One Wallet' on the preview page."""
    dismiss_overlays(page)
    adjust_btn = page.locator('input[type="submit"][value="Adjust One Wallet"]').first
    adjust_btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    adjust_btn.click(timeout=10000)
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(1000)


def verify_results(page) -> bool:
    """Step 4: Check the Results page for success."""
    try:
        page.wait_for_timeout(1000)
        body_text = page.inner_text("body")
        return "Results" in body_text or "Finished" in body_text
    except Exception:
        return False


def reset_to_lookup(page) -> bool:
    """Get back to a clean Account Lookup page after a failure."""
    for attempt in range(3):
        try:
            dismiss_overlays(page)
            # Let any in-flight navigation finish first, otherwise goto() dies
            # with "interrupted by another navigation" and the run cascades.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            page.goto(LOOKUP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.locator("#lookup-input-cardNumber").wait_for(
                state="visible", timeout=DEFAULT_TIMEOUT_MS
            )
            return True
        except Exception:
            page.wait_for_timeout(2000 * (attempt + 1))
    return False


def process_card(page, card, dry_run: bool):
    """Run one card end to end. Returns (outcome, message, amount, balance)."""
    name = card["name"]
    target = float(card["target_amount"])

    step("lookup", lookup_card, page, card["card_number"])
    step("open adjust wallet", navigate_to_adjust_wallet, page)
    step("select Stored Value", select_stored_value_wallet, page)

    balance = step("read balance", get_current_balance, page)
    reload_amount = round(target - balance, 2)

    if reload_amount <= 0:
        return ("skip", f"Balance: ${balance:.2f} | already at or above target",
                None, balance)

    if dry_run:
        return ("dry-run", f"Balance: ${balance:.2f} | WOULD reload ${reload_amount:.2f}",
                reload_amount, balance)

    step("fill form", fill_adjustment_form, page, reload_amount, name)
    step("confirm", confirm_adjustment, page)

    detail = f"Balance: ${balance:.2f} | Reloaded ${reload_amount:.2f} to Flowood MS"
    if verify_results(page):
        return "done", detail, reload_amount, balance
    return "unverified", detail, reload_amount, balance


# -- Main ----------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Reload Newk's meal cards on Paytronix.")
    p.add_argument("--dry-run", action="store_true",
                   help="Read each balance and show what would be loaded; change nothing")
    p.add_argument("--period", type=int, help="Period to load (default: this month)")
    p.add_argument("--year", type=int, help="Year (default: this year)")
    p.add_argument("--include-done", action="store_true",
                   help="Also include cards already ticked for this period")
    p.add_argument("--max-cards", type=int, help="Stop after this many cards (for testing)")
    p.add_argument("--app-url", help="Reporting app URL (overrides config.json)")
    p.add_argument("--email", help="Your app email (overrides config.json)")
    return p.parse_args()


def main():
    args = parse_args()
    base_dir = Path(__file__).parent
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = base_dir / f"meal_card_reload_log_{timestamp}.txt"

    logger = Logger(log_file)
    sys.stdout = logger
    sys.stderr = logger

    app = connect_to_app(base_dir, args)
    work = app.worklist(year=args.year, period=args.period, include_done=args.include_done)
    year, period = work["year"], work["period"]
    cards = work["cards"]

    print(f"Meal Card Reload Bot - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Period: {period} ({year})")
    print(f"Default target: ${work['default_target']:.2f}")
    print(f"Attempts per card: {CARD_ATTEMPTS}")
    if args.dry_run:
        print("DRY RUN: balances will be read, nothing will be loaded")
    print("-" * 60)

    if not cards:
        print(f"Nothing to do - every active card is already ticked for period {period}.")
        print("Re-run with --include-done to load them anyway.")
        logger.close()
        sys.stdout = logger.terminal
        return

    if args.max_cards:
        cards = cards[:args.max_cards]
        print(f"TEST MODE: first {args.max_cards} card(s) only")
    total = len(cards)
    print(f"{total} card(s) to process")
    print()

    if not args.dry_run:
        app.start_run(year, period)
        print(f"App run #{app.run_id} started")
        print()

    completed = 0
    skipped = 0
    errors = 0
    retried = 0
    failed_cards = []

    with sync_playwright() as p:
        browser = connect_to_chrome(p)
        context = browser.contexts[0]

        block_widgets(context)
        print("Blocked third-party widgets (Wootric survey, Intercom, Access-Evo)")

        print(f"Connected to Chrome. {len(context.pages)} tab(s) visible to bot.")
        for i, p_ in enumerate(context.pages):
            print(f"  Tab {i}: {p_.url}")

        # Prefer the pxsweb tab that has a merchant session (currentMerchantId in URL)
        page = next((pg for pg in context.pages
                     if "pxsweb.com" in pg.url and "currentMerchantId" in pg.url), None)
        # Fall back to any pxsweb tab
        if page is None:
            page = next((pg for pg in context.pages if "pxsweb.com" in pg.url), None)
        if page is None:
            raise RuntimeError(
                "No pxsweb.com tab found. Open https://www.pxsweb.com in your debug "
                "Chrome and log in first."
            )

        # Auto-dismiss alert()/beforeunload prompts, which otherwise freeze the
        # whole run behind a modal Chrome dialog.
        page.on("dialog", lambda d: d.accept())

        # Don't try to close ghost tabs - Paytronix pages have beforeunload prompts that hang.
        # We've already locked onto the right page above; just bring it to front.
        page.bring_to_front()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        print(f"Bot will drive: {page.url}")
        print()

        for idx, card in enumerate(cards, start=1):
            name = card["name"]
            target = float(card["target_amount"])

            print(f"[{idx}/{total}] {name} (Card: {card['card_number']}, Target: ${target:.2f})")

            for attempt in range(1, CARD_ATTEMPTS + 1):
                try:
                    outcome, detail, amount, balance = process_card(page, card, args.dry_run)

                    if outcome == "skip":
                        print(f"  {detail}")
                        print("  SKIPPED - balance already at or above target")
                        skipped += 1
                        if not args.dry_run:
                            app.record_card(card["card_number"], "skipped",
                                            balance_before=balance, detail=detail)
                    elif outcome == "dry-run":
                        print(f"  {detail}")
                    else:
                        print(f"  {detail}")
                        if outcome == "unverified":
                            print("  WARNING - could not verify results page")
                        print("  COMPLETED")
                        completed += 1
                        app.record_card(card["card_number"], "done", amount=amount,
                                        balance_before=balance, detail=detail)

                    if attempt > 1:
                        retried += 1
                    break

                except Exception as e:
                    lines = str(e).strip().splitlines()
                    msg = lines[0] if lines else e.__class__.__name__
                    if attempt < CARD_ATTEMPTS:
                        print(f"  attempt {attempt}/{CARD_ATTEMPTS} failed ({msg}) - retrying")
                        if not reset_to_lookup(page):
                            print("  could not get back to Account Lookup")
                    else:
                        print(f"  ERROR after {CARD_ATTEMPTS} attempts: {msg}")
                        errors += 1
                        failed_cards.append((idx, name, msg))
                        if not args.dry_run:
                            app.record_card(card["card_number"], "error", detail=msg)
                        if not reset_to_lookup(page):
                            print("  Could not recover - you may need to restart")

            print()

        print("-" * 60)
        if args.dry_run:
            print(f"DRY RUN SUMMARY: {skipped} already at target, {errors} errors, "
                  f"{total - skipped - errors} would be loaded")
        else:
            print(f"SUMMARY: {completed} completed, {skipped} skipped, {errors} errors")
        if retried:
            print(f"({retried} card(s) succeeded only after a retry)")
        if failed_cards:
            print()
            print("Cards that need attention:")
            for idx, name, msg in failed_cards:
                print(f"  [{idx}] {name} - {msg}")
            print()
            print("Re-run the bot to pick them up again - cards that succeeded are")
            print("already ticked in the app and will be skipped automatically.")
        print(f"Log saved to: {log_file}")

    if not args.dry_run:
        app.finish_run(completed, skipped, errors, logger.text())
        print("Run closed out in the app.")

    logger.close()
    sys.stdout = logger.terminal


if __name__ == "__main__":
    main()
