"""
_audit_plu_gaps.py — Find PLUs in the latest export that don't classify and
propose new ranges to add to Item PLU Ranges.xlsx.

Strategy:
  - Group unclassified PLUs by 6-digit prefix (e.g. 400704, 300522)
  - For each bucket, find the nearest sibling Toast range and suggest a
    parallel range with the same name, suffixed " (OLO)" or "(9-digit)" etc.
  - Output a CSV the user pastes into the bottom of Item PLU Ranges.xlsx.

Usage: python _audit_plu_gaps.py /path/to/exported_menu_performance.xlsx
"""
import csv
import os
import sqlite3
import sys
from collections import defaultdict

import pandas as pd

import toast_config_db

DB = toast_config_db.TOAST_CONFIG_DB_PATH
OUT = os.path.join(os.path.dirname(__file__), "gaps_to_add_to_plu_ranges.csv")


def main(export_path: str):
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        ranges = list(c.execute(
            "SELECT pos_type, range_name, range_start, range_end FROM plu_ranges"
        ))

    def classify(pos_type, plu):
        plu = int(plu)
        for pt, rn, lo, hi in ranges:
            if pt == pos_type and lo <= plu <= hi:
                return rn
        return ""

    df = pd.read_excel(export_path, sheet_name="All Items")
    df = df[df["PLU"].notna() & (df["PLU"] != 0)].copy()
    df["PLU"] = df["PLU"].astype(int)
    df["cls"] = df["Item Classification"].fillna("").astype(str)
    df["sub"] = df["Item Subclass"].fillna("").astype(str)
    df["cat"] = df["Consolidated Category"].fillna("").astype(str)

    gaps = df[(df["cls"] == "") & (df["sub"] == "")].copy()

    # Bucket by 6-digit prefix so similar PLUs land together
    # (4007 is too coarse — collapses Cup Soup OLO with Pick 3 OLO).
    def prefix6(p):
        s = str(p)
        return s[:6] if len(s) >= 6 else s

    gaps["prefix"] = gaps["PLU"].apply(prefix6)

    proposed = []
    for prefix, sub in gaps.groupby("prefix"):
        lo, hi = int(sub["PLU"].min()), int(sub["PLU"].max())
        names = sorted(set(sub["Menu Item Name"]))
        sample = ", ".join(n.split("(")[0].strip() for n in names[:4])[:80]

        # Try sibling range:
        #   4007xxxx → 3007xxxx (OLO mirror of POS pairing)
        #   4006xxxx → 3006xxxx
        #   400xxx   → 300xxx
        suggestion = ""
        ps = str(lo)
        candidates = []
        if ps.startswith("4") and len(ps) >= 7:
            candidates.append("3" + ps[1:])
        if len(ps) >= 9 and ps.startswith("3005"):
            # 9-digit modifier — Toast subclass exists at 8-digit, extend it
            candidates.append(ps[:8])
        for c_str in candidates:
            try:
                sib = classify("Toast", int(c_str))
                if sib:
                    if ps.startswith("4"):
                        suggestion = f"{sib} (OLO)"
                    else:
                        suggestion = sib
                    break
            except ValueError:
                pass
        if not suggestion:
            suggestion = f"AUTO: {prefix}xx — please rename"

        # Round bounds to clean 100-wide ranges
        rng_lo = (lo // 100) * 100
        rng_hi = ((hi // 100) + 1) * 100 - 1

        proposed.append({
            "POS Type": "Toast",
            "Range Name": suggestion,
            "Range Start": rng_lo,
            "Range End": rng_hi,
            "_n_items": len(sub),
            "_sample": sample,
        })

    proposed.sort(key=lambda r: r["Range Start"])

    # Write CSV (just the 4 columns the import script expects)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["POS Type", "Range Name", "Range Start", "Range End"])
        w.writeheader()
        for r in proposed:
            w.writerow({k: r[k] for k in w.fieldnames})

    print(f"Wrote {len(proposed)} proposed ranges to {OUT}")
    print()
    print(f"{'Range Start':>11}  {'Range End':>11}  {'#':>3}  {'Suggested Name':40s}  Sample")
    print("-" * 130)
    for r in proposed:
        print(f"{r['Range Start']:>11}  {r['Range End']:>11}  {r['_n_items']:>3}  "
              f"{r['Range Name']:40s}  {r['_sample']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python _audit_plu_gaps.py /path/to/exported_menu_performance.xlsx")
        sys.exit(1)
    main(sys.argv[1])
