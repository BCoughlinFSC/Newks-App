"""
Quick one-off: Amex credit card payments by day for specific stores.
Reads from Azure Blob Storage (toast_orders_blob).

Usage:
  python amex_report.py
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import toast_orders_blob

# ── Config ────────────────────────────────────────────────────────────────────

STORES = {
    "d7ab920e-b4bc-4245-97cb-b749db37dccd": "1004 - Tuscaloosa",
    "4f092f7c-70ec-4293-bda6-8b743eceff6c": "1194 - Magee",
    "a368ce14-2147-48bd-aedd-9936c80245ac": "1017",
}

START_DATE = "20260101"
END_DATE = datetime.now().strftime("%Y%m%d")


def is_amex_payment(payment: dict) -> bool:
    """Check if a payment is American Express."""
    # Check multiple fields — Toast uses different structures
    card_type = (payment.get("cardType") or "").upper()
    if "AMEX" in card_type or "AMERICAN" in card_type:
        return True

    # Check type/otherPayment fields
    ptype = (payment.get("type") or "").upper()
    if "AMEX" in ptype:
        return True

    # Check cardEntryMode or tipAmount won't help, check the whole thing
    raw = json.dumps(payment).upper()
    if "AMEX" in raw or "AMERICAN EXPRESS" in raw:
        return True

    return False


def main():
    # First, let's look at one order's payment structure to understand the data
    print("Scanning for Amex payments...\n")

    # date -> store_name -> total
    results = defaultdict(lambda: defaultdict(float))
    sample_printed = False

    for guid, store_name in STORES.items():
        # Generate all dates in range
        current = datetime.strptime(START_DATE, "%Y%m%d")
        end = datetime.strptime(END_DATE, "%Y%m%d")

        while current <= end:
            bdate = current.strftime("%Y%m%d")
            orders = toast_orders_blob.get_cached_orders(guid, bdate)

            for order in orders:
                if order.get("voided"):
                    continue
                for check in (order.get("checks") or []):
                    for payment in (check.get("payments") or []):
                        # Print first payment as sample to see structure
                        if not sample_printed:
                            print("=== SAMPLE PAYMENT STRUCTURE ===")
                            print(json.dumps(payment, indent=2)[:1000])
                            print("================================\n")
                            sample_printed = True

                        if is_amex_payment(payment):
                            amt = float(payment.get("amount") or payment.get("originalProcessingFee") or 0)
                            if amt > 0:
                                date_str = f"{bdate[:4]}-{bdate[4:6]}-{bdate[6:]}"
                                results[date_str][store_name] += amt

            current += timedelta(days=1)

        print(f"  Done: {store_name}")

    # Output CSV
    outfile = os.path.join(SCRIPT_DIR, "amex_payments_report.csv")
    all_stores = sorted(STORES.values())

    with open(outfile, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date"] + all_stores + ["Daily Total"])

        for date in sorted(results.keys()):
            row = [date]
            daily_total = 0
            for store in all_stores:
                amt = round(results[date].get(store, 0), 2)
                row.append(f"{amt:.2f}")
                daily_total += amt
            row.append(f"{daily_total:.2f}")
            writer.writerow(row)

        # Totals row
        totals = ["TOTAL"]
        grand_total = 0
        for store in all_stores:
            store_total = sum(results[d].get(store, 0) for d in results)
            totals.append(f"{store_total:.2f}")
            grand_total += store_total
        totals.append(f"{grand_total:.2f}")
        writer.writerow(totals)

    print(f"\nReport saved to: {outfile}")
    print(f"Date range: {START_DATE} - {END_DATE}")
    print(f"Total Amex: ${grand_total:,.2f}")


if __name__ == "__main__":
    main()
