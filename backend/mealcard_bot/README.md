# Meal Card Reload Bot

Reloads the Newk's meal cards on Paytronix once a month, then records what it
did in the reporting app.

You downloaded this from the app's **Meal Cards** page. It is already pointed at
the right app and your email address (see `config.json`).

## Use the .cmd files, not the .ps1 files

Windows refuses to run a PowerShell script that arrived from the internet, so
double-clicking a `.ps1` here looks like it does nothing at all. The numbered
`.cmd` files are the way in. They run the matching `.ps1` for that one launch
only and change no setting on the machine.

## First time on this PC

1. **Unzip this folder first** - right-click the download, Extract All, and put
   it somewhere you can find again (your Desktop is fine). Running from inside
   the zip will not work.
2. **Double-click `1 - SETUP - run me first.cmd`.**

That creates a private Python environment in this folder and installs two
packages. Python has to be on the machine: if it is not, setup offers to
install it for you, and if your PC blocks that it tells you where to get it.
(If you install it by hand, tick **Add python.exe to PATH** on the installer's
first screen - that is the box people miss.)

Nothing is installed outside this folder except Python itself, and nothing else
on the machine is changed.

## Try it first — this one cannot spend anything

**Double-click `2 - TEST RUN - adds no money.cmd`.**

It does everything the real run does except add money: signs into the app, pulls
the card list, looks each card up in Paytronix, reads its balance and works out
what it would add. The adjustment form is never filled in or submitted, and no
card is ticked off. There is no prompt in it that turns it into a real run.

It offers to check only the first few cards, which is enough to prove your setup
works without sitting through the whole list.

Use this to check everything is working before the real reload is due. The test
does show up on the Meal Cards page, labelled as a dry run, so whoever owns the
process can see your setup is good.

## Every month

**Double-click `3 - RUN THE RELOAD - adds money.cmd`.**
**This one does add money.**

It will:

1. Open a separate Chrome window on the Paytronix login page.
2. Wait for you to log in and land on **Account Lookup**.
3. Offer a **dry run** - reads every card's balance and prints what it *would*
   load, without adding a cent. Say yes to this if anything has changed.
4. Ask for your app password, so it can fetch the card list.
5. Work through the cards, then print a summary.
6. Export today's **Stored Value Sales and Added Value Detail Report** as CSV
   for **1003 - Flowood MS**, keep only eligible roster cards, and send that CSV
   to the app's existing payroll report builder.
7. Save the original CSV, filtered meal-card CSV, both payroll workbooks and a
   summary under `reports/<timestamp>/` beside the bot.

## Reports without reloading cards

Use **`4 - REPORT ONLY - adds no money.cmd`** after a manual reload or to retry
a report. Enter the load date (YYYY-MM-DD), or press Enter for today. It never
visits wallet adjustment pages, starts a reload run or ticks cards complete.
Sign into Paytronix in the debug Chrome window; this reporting step supports
both standalone Paytronix and Paytronix embedded in Access Evo. The reload
automation itself still requires the standalone Paytronix tab.

Paytronix warns that reports can lag up to **45 minutes**. After an automatic
reload, the export is checked for the amounts added in that run before either
workbook is built. If those loads are missing, wait and use REPORT ONLY. A
report-only run cannot independently reconcile a previous manual run, so check
the reported total against the loads you performed. Never rerun the reload just
to retry an export.

Kim Aylesworth, Adam Karveller and Nick Nastasi are excluded from automated
report input as well as reloads. Ordinary gift-card sales outside the meal-card
roster are excluded. Employee-ID mapping and AllPayID 0 treatment are unchanged;
review the workbook's **Unmapped** tab before handing it to payroll.

Command-line options:

```
python meal_card_reload.py --report-only --report-from 2026-11-02 --report-to 2026-11-02
python meal_card_reload.py --skip-report
```

The report dates default to the local date the command starts. Dry runs never
export or build payroll reports. The app must already provide
`/api/mealcard/payroll-report`; no new server endpoint is required.

The log is saved in this folder, and the run also shows up on the Meal Cards
page in the app.

## What it does per card

Looks the card up, opens **Adjust Wallet**, reads the Stored Value balance, and
adds the difference between that balance and the card's target. A card already
at or above its target is skipped. Everything posts to store **1003 - Flowood
MS**.

The card list and the targets live in the app, not in this folder - so there is
no spreadsheet here to go stale, and nothing to re-download when someone is
added. Cards already ticked for the period are skipped, so re-running after a
failure only picks up what is left.

## If something goes wrong

**"Could not attach to Chrome's debugging port"** - almost always a pop-up
dialog sitting open in the debug Chrome window. Switch to it, dismiss the
dialog, and run again. The bot prints the open tabs to help you find it.

**A card fails three times** - it is listed at the end and left unticked. Run
the bot again; it will retry just the ones that did not finish.

**"Could not record this card in the app"** - the reload itself went through,
but the app did not hear about it. The message tells you which card to tick by
hand on the Meal Cards page.

## Notes for whoever maintains this

The source lives in the reporting app repo at `backend/mealcard_bot/`, and the
app zips that folder on the fly for the download button - so changes here ship
with the next app deploy, with no separate release step.

`config.json` is **not** in the repo. The download endpoint writes it per
download, so the zip already carries the right app URL and the email of whoever
clicked the button.

The bot attaches to an existing Chrome over CDP (`connect_over_cdp`) and never
launches a browser itself. That is why `setup.ps1` does not run
`playwright install` - the ~400MB of bundled browsers would never be opened.
It also means Paytronix sees an ordinary logged-in session rather than a fresh
automated profile.
