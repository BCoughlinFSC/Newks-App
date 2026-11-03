# Newk's Meal Card Bot

Run the monthly reload, verify balances, and build payroll reports from one
launcher. This uses the Paytronix merchant frame inside Access Evo.

## Setup and run

1. Extract the ZIP into a permanent folder on your PC.
2. Run **1 - SETUP - run me first.cmd** once.
3. Run **2 - TEST RUN - adds no money.cmd** to test the installation.
4. For the monthly workflow use **3 - RUN THE RELOAD - adds money.cmd**.
   Log into Access Evo in the Chrome window it opens, select **Newk's Eatery**
   in **Switch Organisation**, open **Loyalty / Paytronix**, and press Enter
   in the launcher. Reloads, verification, and reports then run together.

**No reporting app URL, account, login, or server is required.** The ZIP includes
`roster.json`, the payroll builder, and employee mappings. Only Access Evo needs
an authenticated browser session. Existing config.json files are ignored.

## Organization, account, and store checks

Both the Access Evo switcher and Paytronix merchant header must identify
**Newk's Eatery**; the merchant header must be **Newk's Eatery (1085)**.
The bot stops when either cannot be verified. It never chooses an organization
for you and never falls back to an unrelated standalone Paytronix tab.

Lookup stays on the embedded Paytronix host. Returned card number and card
merchant must match before opening the wallet. Checks repeat before submitting
an adjustment. The adjustment store must be **1003 - Flowood MS**. Merchant
1085 and store 1003 are separate identifiers and must not be interchanged.

## Reload rules and recovery

Cards and targets come from the local `roster.json`. Add only target minus current
balance; skip cards at or above target. Adam Karveller, Kim Aylesworth, and Nick
Nastasi remain excluded. The test launcher includes already-completed cards so an installation can be
validated after a monthly reload. It reads balances without filling or
submitting adjustment amounts or marking cards complete.

Before final submission, save the card's planned adjustment as **pending** in
`state/standalone-YYYY-PP.json`. Reopen the wallet after submission and require the expected
balance before saving **verified** and recording completion locally.

A confirmation timeout or failed verification never triggers another submission.
It stays pending for transaction-history review. Do not delete the journal to
retry. Verified journal entries prevent repeat loads even if the local results file could not
be saved; rerunning restores those verified results locally.

A folder lock prevents overlapping local runs. It does not coordinate separate
PCs or extracted copies. Use one designated folder on one PC. After a forced
termination, reconcile pending transactions before removing `state/run.lock`.

## Reports

After reloads, the same browser connection exports **Stored Value Sales and
Added Value Detail Report** for Flowood, filters it to eligible roster cards,
and uses the bundled payroll builder. The original CSV, filtered CSV,
named transaction workbook, payroll workbook, and report summary are saved under
`reports/<timestamp>/`.

Recent transactions can be delayed. The report stage retries every minute for
up to 45 minutes without reloading wallets. `--report-wait-minutes 0` disables
the wait. If export still fails, use **4 - REPORT ONLY - adds no money.cmd**;
choose the original load date. Report-only never adjusts wallets.

Employee IDs retain their mapping provenance in `roster.json`. Card-to-payroll mappings take
priority over fallback IDs. Fallback IDs have not been independently verified;
review the mapping source and Unmapped tab before using the file for payroll.
AllPayID 0 entries remain excluded from payroll.

## Validation

On November 3, 2026 the Access Evo organization/merchant header, account lookup,
card identity, wallet selection, current-balance field, and Flowood store option
were inspected in the signed-in browser. No wallet adjustments were submitted.
Local tests cover navigation binding, card/merchant mismatches, organization
changes, confirmation timeouts, journal recovery, report retries, and the combined
reload/report workflow. The complete installed bot has not yet been exercised
on the user's PC; use the test launcher before the next live monthly run.

## Local roster and maintenance

`roster.json` contains the existing repository roster, individual targets, and
fallback employee IDs from prior payroll exports. Historical month checkmarks
are not eligibility rules and are not used to schedule employees. Review the
roster when employees change. Set `active` to false to remove an account from
future runs; the three explicit exclusions remain enforced in the bot too.
Card-number mappings in `employee_cards` override name-based `employee_names`.
These fallback IDs are not newly verified; unmapped cards remain listed for review.

Progress is stored under `state/`, logs beside the launcher, and reports under
`reports/`. Keep those files between runs. The first standalone run has its own
journal; no reporting-app data is imported or contacted.

Source lives in `backend/mealcard_bot/`. The shared `backend/mealcard_payroll.py`
is bundled as `mealcard_payroll.py` beside the bot when creating the ZIP.
Python and Playwright attach to the launcher's debug Chrome on local port 9222;
setup does not download separate browser binaries.
