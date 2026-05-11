"""
reconcile_flash.py — Diff the stored flash data (`aloha_sales_data`) against a
fresh Toast ERA pull, for a given date range.

Hits POST /api/admin/flash-reconcile on the running server.  Use this to find
which (store, date) pairs have stale snapshots so you can re-import only the
days that need it instead of guessing.

Usage:
    python reconcile_flash.py --start 2026-05-04 --end 2026-05-10
    python reconcile_flash.py --start 2026-05-04 --end 2026-05-10 --token <jwt>
    python reconcile_flash.py --start 2026-05-04 --end 2026-05-10 \
        --min-abs 100 --min-pct 1.0 --out reconcile_5-4_5-10.csv

Server: defaults to the Cloudways prod URL.  Override with --server.
Token : reads ADMIN_TOKEN env var if --token is not provided.
Output: prints a table to stdout, optionally writes a CSV.
"""
import argparse
import csv
import json
import os
import sys
from datetime import date

import httpx


DEFAULT_SERVER = "https://phpstack-1474989-6237770.cloudwaysapps.com"


def main():
    ap = argparse.ArgumentParser(description="Reconcile flash sales vs fresh ERA pull")
    ap.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="Server URL")
    ap.add_argument("--token", default=os.getenv("ADMIN_TOKEN", ""),
                    help="Admin JWT (or set ADMIN_TOKEN env var)")
    ap.add_argument("--min-abs", type=float, default=50.0,
                    help="Flag rows whose |stored - fresh| >= this many dollars (default 50)")
    ap.add_argument("--min-pct", type=float, default=0.5,
                    help="Flag rows whose |pct diff| >= this percent (default 0.5)")
    ap.add_argument("--locations", default="",
                    help="Comma-separated Aloha loc ids to restrict to")
    ap.add_argument("--out", default="",
                    help="Optional CSV path for the discrepancy list")
    ap.add_argument("--json-out", default="",
                    help="Optional path to dump the full JSON response")
    args = ap.parse_args()

    try:
        date.fromisoformat(args.start)
        date.fromisoformat(args.end)
    except ValueError:
        print("ERROR: dates must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)

    if not args.token:
        print("ERROR: --token required (or set ADMIN_TOKEN env var)", file=sys.stderr)
        sys.exit(2)

    payload = {
        "start_date": args.start,
        "end_date": args.end,
        "min_abs_diff": args.min_abs,
        "min_pct_diff": args.min_pct,
    }
    if args.locations:
        payload["locations"] = [s.strip() for s in args.locations.split(",") if s.strip()]

    url = f"{args.server.rstrip('/')}/api/admin/flash-reconcile"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
    }
    print(f"POST {url}")
    print(f"  range={args.start} to {args.end}  thresholds: |Δ| >= ${args.min_abs:.2f} or {args.min_pct:.2f}%")

    with httpx.Client(timeout=1800.0) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  wrote {args.json_out}")

    print()
    print("=" * 78)
    print(f"Chain stored total  : ${result['chain_stored_total']:>15,.2f}")
    print(f"Chain fresh total   : ${result['chain_fresh_total']:>15,.2f}")
    print(f"Chain net diff      : ${result['chain_net_diff']:>15,.2f}  (stored - fresh)")
    print(f"Sum |daily diff|    : ${result['chain_abs_diff_sum_per_date']:>15,.2f}")
    print(f"Discrepancy rows    : {result['discrepancy_count']}")
    print("=" * 78)

    print()
    print("Per-date totals:")
    print(f"  {'date':<12}{'stored':>16}{'fresh':>16}{'diff':>14}")
    for d in result.get("per_date", []):
        if d.get("status") != "ok":
            print(f"  {d.get('business_date', '?'):<12}  {d.get('status'):<20}{d.get('error', '')}")
            continue
        print(f"  {d['business_date']:<12}${d['stored_total']:>15,.2f}${d['fresh_total']:>15,.2f}${d['diff']:>13,.2f}")

    discreps = result.get("discrepancies", [])
    if discreps:
        print()
        print(f"Top discrepancies (showing up to 40 of {len(discreps)}):")
        print(f"  {'date':<12}{'loc':<8}{'stored':>14}{'fresh':>14}{'diff':>12}{'pct':>8}")
        for r in discreps[:40]:
            print(f"  {r['business_date']:<12}{str(r['location']):<8}"
                  f"${r['stored_net_sales']:>13,.2f}${r['fresh_net_sales']:>13,.2f}"
                  f"${r['diff']:>11,.2f}{r['pct_diff']:>7.2f}%")

    if args.out and discreps:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "business_date", "location", "stored_net_sales",
                "fresh_net_sales", "diff", "pct_diff",
            ])
            w.writeheader()
            for r in discreps:
                w.writerow(r)
        print(f"\nWrote {len(discreps)} rows to {args.out}")

    sys.exit(0 if result["discrepancy_count"] == 0 else 0)


if __name__ == "__main__":
    main()
