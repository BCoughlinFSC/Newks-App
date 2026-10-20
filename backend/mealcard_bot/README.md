# Meal Card Reload Bot

Reloads the Newk's meal cards on Paytronix once a month, then records what it
did in the reporting app.

You downloaded this from the app's **Meal Cards** page. It is already pointed at
the right app and your email address (see `config.json`).

## First time on this PC

1. Unzip this folder somewhere you can find it again (your Desktop is fine).
2. Right-click **setup.ps1** and choose **Run with PowerShell**.

That creates a private Python environment here and installs two packages. It
needs Python on the machine - if it is missing, the script tells you where to
get it. Nothing else is installed and nothing is changed outside this folder.

## Every month

Right-click **Run Meal Card Reload.ps1** and choose **Run with PowerShell**.

It will:

1. Open a separate Chrome window on the Paytronix login page.
2. Wait for you to log in and land on **Account Lookup**.
3. Offer a **dry run** - reads every card's balance and prints what it *would*
   load, without adding a cent. Say yes to this if anything has changed.
4. Ask for your app password, so it can fetch the card list.
5. Work through the cards, then print a summary.

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
