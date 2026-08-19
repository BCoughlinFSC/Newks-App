#!/usr/bin/env python3
"""
seed_subscriptions.py — create the four Monkey/Catering report subscriptions
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

SEEDS = [
    {
        "reportKey": "monkey_orders_due",
        "name": "Monkey Orders Due For Today",
        # SSRS had two subscriptions (Franchise grp 8 + Corporate grp 79) with
        # identical recipient lists; the app report isn't store-group scoped, so
        # one subscription covers both.
        "recipientsTo": "salesteam; companygms; fbcs",
        "recipientsCc": "",
        "daysOfWeek": "mon,tue,wed,thu,fri,sat,sun",
        "hour": 7, "minute": 50,
        "params": {"dayOffset": 0},
        "attachXlsx": True,
        "sendIfEmpty": False,
        "notes": "Replaces SSRS /Monkey Media/Monkey Orders Due For Today - Summary by Store "
                 "(Franchise grp 8 + Corporate grp 79, both -> salesteam; companygms; fbcs).",
    },
    {
        "reportKey": "period_catering_by_week",
        "name": "Period Catering by Week",
        "recipientsTo": "salesteam",
        "recipientsCc": "",
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        "params": {"weeks": 4, "basis": "subtotal"},
        "attachXlsx": True,
        "sendIfEmpty": True,   # a zero week is itself the news for this one
        "notes": "Replaces SSRS /Catering Sales and Menu/Period Catering by Week.",
    },
    {
        "reportKey": "monkey_refunds_by_user",
        "name": "Monkey — Refunds by User",
        "recipientsTo": "accounting; salesteam",
        "recipientsCc": "itdepartment",
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        "params": {"window": "last_full_week"},
        "attachXlsx": True,
        "sendIfEmpty": False,  # no refunds last week = no email, same as SSRS
        "notes": "Replaces SSRS /Catering Sales and Menu/Monkey - Refunds by User "
                 "(Corporate grp 79).",
    },
    {
        "reportKey": "monkey_sales_by_channel_weekday",
        "name": "Monkey Sales by Channel by Weekday",
        "recipientsTo": "mhayward",
        "recipientsCc": "",
        "daysOfWeek": "mon",
        "hour": 7, "minute": 45,
        "params": {"window": "last_full_week", "groupBy": "system"},
        "attachXlsx": True,
        "sendIfEmpty": True,
        "notes": "NEW — the SSRS report had no subscription (interactive only, "
                 "run by Matt Hayward). Recipients confirmed by Kim 2026-08-19.",
    },
]


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

    print(f"\n{created} created, {updated} updated, {skipped} skipped.")
    if created and not args.enable:
        print("They are DISABLED. Preview each one in Admin & Data -> Report "
              "Subscriptions, then flip it on.")
    print("Restart the app (./restart.sh) so the scheduler picks up the new jobs, "
          "or toggle each subscription in the UI (that re-registers them live).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
