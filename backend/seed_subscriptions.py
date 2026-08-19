#!/usr/bin/env python3
"""
seed_subscriptions.py — create the five Monkey/Catering report subscriptions
that replace their SSRS originals.

Recipients and send times mirror the SSRS subscriptions as captured in
SSRS_subscription_recipients_2026-08-14.csv and the ReportServer schedule dump
(SQL DATABSE DOCUMENTATION/get_monkey_subscription_schedules.sql). Everything
here is editable afterwards in Admin & Data -> Report Subscriptions, so treat
this as the starting point, not the source of truth.

Idempotent: a subscription whose name already exists is left alone unless
--force is passed, in which case it is updated in place.

    python seed_subscriptions.py            # create missing, report what exists
    python seed_subscriptions.py --force    # also overwrite existing rows
    python seed_subscriptions.py --dry-run  # show what would happen

New subscriptions are created DISABLED unless --enable is passed, so nothing
mails out before you have previewed it.
"""
import argparse
import sys

import subscriptions_db

# Recipients, times, BCC, Reply-To and store scopes below are transcribed from the
# live ReportServer.Subscriptions rows (pulled 2026-08-19). Anything marked NEW had
# no SSRS subscription.
_BCC      = "reports@newks.com"        # every SSRS sub BCC'd this — the archive copy
_REPLY_TO = "itdepartment@newks.com"   # every SSRS sub set this as ReplyTo

SEEDS = [
    # SSRS ran TWO Orders Due subscriptions off one report, same recipients,
    # differing only by StoreGroup (79 = Corporate, 8 = Franchise). Two emails
    # land each morning, so we reproduce two subscriptions rather than merging.
    {
        "reportKey": "monkey_orders_due",
        "name": "Monkey Orders Due For Today — Corporate",
        "recipientsTo": "salesteam; companygms; fbcs",
        "recipientsCc": "",
        "recipientsBcc": _BCC,
        "replyTo": _REPLY_TO,
        "daysOfWeek": "mon,tue,wed,thu,fri,sat,sun",
        "hour": 7, "minute": 50,
        "params": {"dayOffset": 0, "scope": "company"},
        "attachXlsx": True,
        "sendIfEmpty": False,
        "notes": "SSRS '[REPORT] - Monkey Media - Corporate Stores - Orders Due For Today' "
                 "(StoreGrouping 9 / StoreGroup 79). Daily 07:50.",
    },
    {
        "reportKey": "monkey_orders_due",
        "name": "Monkey Orders Due For Today — Franchise",
        "recipientsTo": "salesteam; companygms; fbcs",
        "recipientsCc": "",
        "recipientsBcc": _BCC,
        "replyTo": _REPLY_TO,
        "daysOfWeek": "mon,tue,wed,thu,fri,sat,sun",
        "hour": 7, "minute": 50,
        "params": {"dayOffset": 0, "scope": "franchise"},
        "attachXlsx": True,
        "sendIfEmpty": False,
        "notes": "SSRS '[REPORT] - Monkey Media - Franchise Stores - Orders Due For Today' "
                 "(StoreGrouping 9 / StoreGroup 8). Daily 07:50.",
    },
    {
        "reportKey": "period_catering_by_week",
        "name": "Period Catering by Week",
        "recipientsTo": "salesteam",
        "recipientsCc": "",
        "recipientsBcc": _BCC,
        "replyTo": _REPLY_TO,
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        # SSRS passed [Year] = datepart(year, getdate()) — the whole year, not a
        # trailing window. weeks=0 matches that; htmlWeeks keeps the body readable.
        "params": {"weeks": 0, "htmlWeeks": 4, "basis": "subtotal"},
        "attachXlsx": True,
        "sendIfEmpty": True,
        "notes": "SSRS '[REPORT] - Period Catering by Week'. Mondays 07:45.",
    },
    {
        "reportKey": "monkey_refunds_by_user",
        "name": "Monkey — Refunds by User",
        "recipientsTo": "accounting; salesteam",
        "recipientsCc": "itdepartment",
        "recipientsBcc": _BCC,
        "replyTo": _REPLY_TO,
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        # SSRS: StartDate = last Monday, EndDate = last Sunday -> last_full_week.
        # StoreGroup 79 -> Corporate only.
        "params": {"window": "last_full_week", "scope": "company"},
        "attachXlsx": True,
        "sendIfEmpty": False,
        "notes": "SSRS '[REPORT] - Refunded Monkey Media Orders LCW' "
                 "(StoreGrouping 9 / StoreGroup 79). Mondays 07:45.",
    },
    {
        "reportKey": "monkey_sales_by_channel_weekday",
        "name": "Monkey Sales by Channel by Weekday",
        "recipientsTo": "mhayward",
        "recipientsCc": "",
        "recipientsBcc": _BCC,
        "replyTo": _REPLY_TO,
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        "params": {"window": "last_full_week", "groupBy": "system"},
        "attachXlsx": True,
        "sendIfEmpty": True,
        "notes": "NEW — no SSRS subscription existed (interactive only, run by "
                 "Matt Hayward). Recipients confirmed by Kim 2026-08-19.",
    },
]

# Seed rows that an earlier version of this script created under a different
# name. The first cut merged Corporate + Franchise "Orders Due" into one row;
# the ReportServer dump showed SSRS actually ran two, so the row was split and
# renamed. Clean up the superseded row rather than leaving a confusing orphan —
# but only if nobody has enabled or hand-edited it.
RETIRED_NAMES = ["Monkey Orders Due For Today"]


def _clean_retired(dry_run: bool) -> int:
    removed = 0
    for sub in subscriptions_db.list_subscriptions():
        if sub["name"] not in RETIRED_NAMES:
            continue
        if sub["enabled"] or sub.get("created_by") != "seed_subscriptions.py":
            print(f"  KEEP    {sub['name']!r} (id {sub['id']}) — enabled or hand-edited; "
                  f"delete it yourself once you've confirmed the replacements look right")
            continue
        if dry_run:
            print(f"  remove  {sub['name']!r} (id {sub['id']}) — superseded by the "
                  f"Corporate/Franchise split")
        else:
            subscriptions_db.delete_subscription(sub["id"])
            print(f"  removed {sub['name']!r} (id {sub['id']}) — superseded by the "
                  f"Corporate/Franchise split")
        removed += 1
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="overwrite existing subscriptions")
    ap.add_argument("--enable", action="store_true", help="create/leave them enabled")
    ap.add_argument("--dry-run", action="store_true", help="show actions, change nothing")
    args = ap.parse_args()

    subscriptions_db.initialize_schema()
    existing = {s["name"]: s for s in subscriptions_db.list_subscriptions()}

    created = updated = skipped = 0
    for seed in SEEDS:
        payload = dict(seed)
        payload["enabled"] = bool(args.enable)
        cur = existing.get(seed["name"])

        if cur and not args.force:
            print(f"  skip    {seed['name']!r} — already exists (id {cur['id']})")
            skipped += 1
            continue

        label = subscriptions_db.schedule_label(seed["daysOfWeek"], seed["hour"], seed["minute"])
        to = "; ".join(subscriptions_db.parse_recipients(seed["recipientsTo"]))
        if args.dry_run:
            verb = "update " if cur else "create "
            print(f"  {verb} {seed['name']!r} — {label} -> {to}")
            continue

        if cur:
            subscriptions_db.update_subscription(cur["id"], payload)
            print(f"  updated {seed['name']!r} — {label} -> {to}")
            updated += 1
        else:
            row = subscriptions_db.create_subscription(payload, created_by="seed_subscriptions.py")
            state = "ENABLED" if args.enable else "disabled"
            print(f"  created {seed['name']!r} (id {row['id']}, {state}) — {label} -> {to}")
            created += 1

    removed = _clean_retired(args.dry_run)

    print(f"\n{created} created, {updated} updated, {skipped} skipped, {removed} removed.")
    if created and not args.enable:
        print("They are DISABLED. Preview each one in Admin & Data -> Report "
              "Subscriptions, then flip it on.")
    print("Restart the app (./restart.sh) so the scheduler picks up the new jobs, "
          "or toggle each subscription in the UI (that re-registers them live).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
