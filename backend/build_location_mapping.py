"""
Build Aloha → Toast Location Mapping
======================================
Pulls store names from Aloha SQL + restaurant names/GUIDs from the Toast API,
fuzzy-matches them, and writes backend/data/aloha/aloha_location_mapping.json.

Usage:
    # Auto-match (review output, then confirm):
    python build_location_mapping.py

    # After reviewing, write the file:
    python build_location_mapping.py --write

    # Show current mapping status (if mapping file exists):
    python build_location_mapping.py --status

    # Interactive: manually confirm/fix each match before writing:
    python build_location_mapping.py --interactive --write

Requires:
    - ALOHA_SQL_* in backend/.env  (for store names from Aloha DB)
    - Backend running at localhost:8000  (for Toast restaurant list)
      OR set TOAST_LOCATIONS_URL env var to your backend URL
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
MAPPING_OUTPUT     = os.path.join(SCRIPT_DIR, "data", "aloha", "aloha_location_mapping.json")
BACKEND_URL        = os.getenv("TOAST_LOCATIONS_URL", "http://localhost:8000")

try:
    import pyodbc
    import pandas as pd
    import httpx
except ImportError as e:
    print(f"ERROR: Missing dependency — {e}")
    print("Run: pip install pyodbc pandas httpx")
    sys.exit(1)


# ── Connection helpers ─────────────────────────────────────────────────────────

def _build_connection_string() -> str:
    server     = os.getenv("ALOHA_SQL_SERVER")
    database   = os.getenv("ALOHA_SQL_DATABASE")
    username   = os.getenv("ALOHA_SQL_USERNAME")
    password   = os.getenv("ALOHA_SQL_PASSWORD")
    driver     = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted    = os.getenv("ALOHA_SQL_TRUSTED_CONNECTION", "false").lower() in ("1", "true", "yes")
    encrypt    = os.getenv("ALOHA_SQL_ENCRYPT", "true").lower() in ("1", "true", "yes")
    trust_cert = os.getenv("ALOHA_SQL_TRUST_SERVER_CERT", "false").lower() in ("1", "true", "yes")

    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    if trust_cert:
        parts.append("TrustServerCertificate=yes")
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={username}")
        parts.append(f"PWD={password}")
    return ";".join(parts)


# ── Aloha: get store ID → name from SQL ───────────────────────────────────────

def _get_aloha_stores() -> Dict[str, str]:
    """
    Returns {store_id_str: store_name} from Aloha DB.
    Tries several common table/column patterns.
    """
    print("Connecting to Aloha SQL Server...")
    conn = pyodbc.connect(_build_connection_string())
    print("  ✓ Connected")

    store_map: Dict[str, str] = {}

    # Try candidate queries in order of preference
    candidates = [
        # (description, sql)
        ("dbo.Store with Id + Name",
         "SELECT Id, Name FROM dbo.Store ORDER BY Id"),
        ("dbo.Store with storeid + storename",
         "SELECT storeid, storename FROM dbo.Store ORDER BY storeid"),
        ("dbo.Store with fkstoreid + longname",
         "SELECT fkstoreid, longname FROM dbo.Store ORDER BY fkstoreid"),
        ("dbo.Location with Id + Name",
         "SELECT Id, Name FROM dbo.Location ORDER BY Id"),
        ("dbo.Restaurant with Id + Name",
         "SELECT Id, Name FROM dbo.Restaurant ORDER BY Id"),
        # Fallback: just get distinct store IDs from MenuMix (no names)
        ("MenuMix distinct fkstoreid (no names available)",
         "SELECT DISTINCT fkstoreid AS Id, CAST(fkstoreid AS VARCHAR) AS Name "
         "FROM dbo.MenuMix ORDER BY fkstoreid"),
    ]

    for desc, sql in candidates:
        try:
            df = pd.read_sql_query(sql, conn)
            if df.empty:
                continue
            cols = [c.lower() for c in df.columns]
            id_col   = df.columns[0]
            name_col = df.columns[1]
            store_map = {str(row[id_col]).strip(): str(row[name_col]).strip()
                         for _, row in df.iterrows()
                         if str(row[id_col]).strip()}
            print(f"  ✓ Loaded {len(store_map)} Aloha stores via: {desc}")
            break
        except Exception:
            continue

    conn.close()

    if not store_map:
        print("  WARNING: Could not load store names from Aloha DB.")
        print("  Falling back to CSV-derived store IDs (no names).")
        store_map = _get_aloha_stores_from_csvs()

    return store_map


def _get_aloha_stores_from_csvs() -> Dict[str, str]:
    """
    Fallback: collect distinct Location IDs from existing CSV files.
    Returns {store_id: store_id} (name = id since we have no names).
    """
    import csv
    data_dir = os.path.join(SCRIPT_DIR, "data", "aloha")
    store_ids = set()
    for fname in os.listdir(data_dir):
        if not fname.startswith("aloha_") or not fname.endswith(".csv"):
            continue
        path = os.path.join(data_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                if "Location" not in header:
                    continue
                loc_idx = header.index("Location")
                for row in reader:
                    if len(row) > loc_idx and row[loc_idx].strip():
                        store_ids.add(row[loc_idx].strip())
        except Exception:
            continue
    return {sid: sid for sid in sorted(store_ids)}


# ── Toast: get restaurant GUID → name from running API ───────────────────────

def _get_toast_restaurants() -> List[Dict]:
    """
    Returns list of {guid, name, externalId} from the backend /locations endpoint.
    """
    url = f"{BACKEND_URL}/locations"
    print(f"\nFetching Toast restaurant list from {url} ...")
    try:
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        restaurants = []
        for loc in data:
            restaurants.append({
                "guid":       loc.get("restaurantGuid") or loc.get("guid") or "",
                "name":       loc.get("restaurantName") or loc.get("name") or "",
                "externalId": str(loc.get("externalId") or loc.get("restaurantExternalId") or ""),
            })
        print(f"  ✓ Found {len(restaurants)} Toast restaurants")
        return restaurants
    except httpx.ConnectError:
        print(f"  WARNING: Could not connect to backend at {url}")
        print("  Make sure the backend is running, or set TOAST_LOCATIONS_URL env var.")
        return []
    except Exception as e:
        print(f"  WARNING: Error fetching locations: {e}")
        return []


# ── Name normalization & fuzzy matching ───────────────────────────────────────

def _norm(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = str(name or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip common suffixes
    for suffix in ("newks", "newk's", "newk s", "eatery", "cafe", "restaurant"):
        s = s.replace(suffix, "").strip()
    return s


def _token_overlap(a: str, b: str) -> float:
    """Simple token overlap score 0-1."""
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _extract_store_id_from_toast_name(name: str) -> Optional[str]:
    """
    Extract the 4-digit Aloha store number embedded in Toast restaurant names.
    Toast restaurants follow the pattern: "City, ST - 1002 - Newk's City, ST"
    Returns the store ID string, or None if not found.
    """
    # Match patterns like " - 1002 - " or " - 1002" or "1002 -"
    m = re.search(r'(?:^|\D)(\d{4})(?:\D|$)', name)
    if m:
        return m.group(1)
    return None


def _match_stores_to_restaurants(
    aloha_stores: Dict[str, str],
    toast_restaurants: List[Dict],
) -> List[Dict]:
    """
    For each Aloha store, find the best-matching Toast restaurant.

    Match priority:
      1. External ID exact match    (score=1.0, source=externalId)
      2. Store number in Toast name (score=1.0, source=storeNumberInName)
      3. Token name overlap         (score=0.0–0.69, source=name)
    """
    # Pre-build index: store_number_extracted_from_toast_name → restaurant
    toast_by_embedded_id: Dict[str, Dict] = {}
    for rest in toast_restaurants:
        embedded = _extract_store_id_from_toast_name(rest["name"])
        if embedded and embedded not in toast_by_embedded_id:
            toast_by_embedded_id[embedded] = rest

    # Pre-build index: external_id → restaurant
    toast_by_ext_id: Dict[str, Dict] = {
        r["externalId"]: r for r in toast_restaurants if r["externalId"]
    }

    results = []

    for aloha_id, aloha_name in sorted(
        aloha_stores.items(),
        key=lambda x: int(x[0]) if x[0].isdigit() else x[0]
    ):
        # --- Priority 1: External ID exact match ---
        if aloha_id in toast_by_ext_id:
            rest = toast_by_ext_id[aloha_id]
            results.append({
                "aloha_id":     aloha_id,
                "aloha_name":   aloha_name,
                "toast_guid":   rest["guid"],
                "toast_name":   rest["name"],
                "toast_ext_id": rest["externalId"],
                "match_score":  1.0,
                "match_source": "externalId",
                "needs_review": False,
            })
            continue

        # --- Priority 2: Store number embedded in Toast name ---
        if aloha_id in toast_by_embedded_id:
            rest = toast_by_embedded_id[aloha_id]
            results.append({
                "aloha_id":     aloha_id,
                "aloha_name":   aloha_name,
                "toast_guid":   rest["guid"],
                "toast_name":   rest["name"],
                "toast_ext_id": rest["externalId"],
                "match_score":  1.0,
                "match_source": "storeNumberInName",
                "needs_review": False,
            })
            continue

        # --- Priority 3: Fuzzy name overlap fallback ---
        best_score = 0.0
        best_toast = None
        for rest in toast_restaurants:
            score = _token_overlap(aloha_name, rest["name"])
            if score > best_score:
                best_score = score
                best_toast = rest

        results.append({
            "aloha_id":     aloha_id,
            "aloha_name":   aloha_name,
            "toast_guid":   best_toast["guid"]       if best_toast else "",
            "toast_name":   best_toast["name"]       if best_toast else "",
            "toast_ext_id": best_toast["externalId"] if best_toast else "",
            "match_score":  round(best_score, 2),
            "match_source": "name" if best_toast else "none",
            "needs_review": True,
        })

    return results


# ── Output helpers ─────────────────────────────────────────────────────────────

def _print_matches(matches: List[Dict]) -> None:
    high    = [m for m in matches if m["match_score"] >= 0.7]
    medium  = [m for m in matches if 0.3 <= m["match_score"] < 0.7]
    low     = [m for m in matches if m["match_score"] < 0.3]

    def _fmt(m):
        flag = "✓" if m["match_score"] >= 0.7 else ("?" if m["match_score"] >= 0.3 else "✗")
        src  = f"[{m['match_source']}]" if m['match_source'] != 'none' else ""
        return (f"  {flag} Aloha {m['aloha_id']:>6} '{m['aloha_name']:<35}' "
                f"→ Toast '{m['toast_name']:<40}' "
                f"(score={m['match_score']:.2f}) {src}")

    print(f"\n{'='*80}")
    print(f"  HIGH CONFIDENCE matches ({len(high)})  — score ≥ 0.7")
    print('='*80)
    for m in high:
        print(_fmt(m))

    if medium:
        print(f"\n{'='*80}")
        print(f"  MEDIUM CONFIDENCE matches ({len(medium)})  — score 0.3–0.69  ← REVIEW THESE")
        print('='*80)
        for m in medium:
            print(_fmt(m))

    if low:
        print(f"\n{'='*80}")
        print(f"  LOW CONFIDENCE / NO MATCH ({len(low)})  — score < 0.3  ← NEEDS MANUAL MAPPING")
        print('='*80)
        for m in low:
            print(_fmt(m))

    print(f"\nSummary: {len(high)} high | {len(medium)} medium | {len(low)} low/unmatched")


def _interactive_review(matches: List[Dict], toast_restaurants: List[Dict]) -> List[Dict]:
    """Walk through uncertain matches and let user pick correct Toast restaurant."""
    uncertain = [m for m in matches if m["match_score"] < 0.7]
    if not uncertain:
        print("\nAll matches are high-confidence. No review needed.")
        return matches

    print(f"\n{'='*60}")
    print(f"  Reviewing {len(uncertain)} uncertain matches")
    print("  Press Enter to accept suggestion, or type a number to pick")
    print('='*60)

    rest_list = [(i + 1, r) for i, r in enumerate(toast_restaurants)]

    for m in uncertain:
        print(f"\nAloha {m['aloha_id']} — '{m['aloha_name']}'")
        if m["toast_guid"]:
            print(f"  Suggested: [{m['match_score']:.2f}] '{m['toast_name']}' ({m['toast_guid'][:8]}...)")
        else:
            print("  No match found.")

        # Show top 5 candidates sorted by name similarity
        ranked = sorted(
            toast_restaurants,
            key=lambda r: _token_overlap(m["aloha_name"], r["name"]),
            reverse=True
        )[:10]
        print("  Top candidates:")
        for i, r in enumerate(ranked, 1):
            score = _token_overlap(m["aloha_name"], r["name"])
            print(f"    {i:2}. [{score:.2f}] {r['name']} (extId={r['externalId']})")

        choice = input("  Enter number (or Enter to accept suggestion, 's' to skip): ").strip().lower()
        if choice == "s":
            print("    Skipped (will write empty GUID for this store)")
            m["toast_guid"] = ""
            m["toast_name"] = ""
        elif choice == "":
            print(f"    Accepted: {m['toast_name']}")
        else:
            try:
                idx = int(choice) - 1
                picked = ranked[idx]
                m["toast_guid"] = picked["guid"]
                m["toast_name"] = picked["name"]
                m["match_score"] = 1.0
                m["match_source"] = "manual"
                print(f"    Set to: {picked['name']}")
            except (ValueError, IndexError):
                print("    Invalid choice — keeping suggestion")

    return matches


def _write_mapping(matches: List[Dict]) -> None:
    """Write the Toast GUID → Aloha ID mapping JSON."""
    os.makedirs(os.path.dirname(MAPPING_OUTPUT), exist_ok=True)

    mapping = {}
    skipped = []
    for m in matches:
        if m["toast_guid"]:
            mapping[m["toast_guid"]] = m["aloha_id"]
        else:
            skipped.append(m["aloha_id"])

    with open(MAPPING_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)

    print(f"\n✓ Wrote {len(mapping)} mappings to: {MAPPING_OUTPUT}")
    if skipped:
        print(f"  WARNING: {len(skipped)} Aloha stores had no Toast match and were skipped:")
        for sid in skipped:
            print(f"    - {sid}")
        print("  Add these manually to the JSON file if needed.")


def _show_status() -> None:
    """Show current mapping coverage."""
    if not os.path.exists(MAPPING_OUTPUT):
        print("No mapping file found.")
        return

    with open(MAPPING_OUTPUT, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    # Get all known Aloha IDs from CSVs
    aloha_ids = set(_get_aloha_stores_from_csvs().keys())
    mapped_aloha = set(mapping.values())

    print(f"\nMapping file: {MAPPING_OUTPUT}")
    print(f"  Toast GUIDs mapped:       {len(mapping)}")
    print(f"  Distinct Aloha IDs in file: {len(mapped_aloha)}")
    print(f"  Aloha IDs in CSVs:        {len(aloha_ids)}")
    unmapped = aloha_ids - mapped_aloha
    if unmapped:
        print(f"  Aloha IDs NOT yet mapped: {sorted(unmapped)}")
    else:
        print("  ✓ All CSV Aloha IDs are mapped!")

    print("\nCurrent mappings:")
    for guid, aloha_id in sorted(mapping.items(), key=lambda x: int(x[1]) if x[1].isdigit() else x[1]):
        print(f"  {guid[:8]}...  →  Aloha {aloha_id}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build Aloha→Toast location mapping.")
    parser.add_argument("--write",       action="store_true", help="Write the mapping JSON file")
    parser.add_argument("--interactive", action="store_true", help="Manually confirm uncertain matches")
    parser.add_argument("--status",      action="store_true", help="Show current mapping coverage")
    parser.add_argument("--no-sql",      action="store_true", help="Skip Aloha SQL lookup (use CSV IDs only)")
    args = parser.parse_args()

    if args.status:
        _show_status()
        return

    # Step 1: Get Aloha stores
    if args.no_sql:
        aloha_stores = _get_aloha_stores_from_csvs()
        print(f"  Using {len(aloha_stores)} Aloha IDs from existing CSVs (no SQL)")
    else:
        aloha_stores = _get_aloha_stores()

    if not aloha_stores:
        print("ERROR: No Aloha stores found.")
        sys.exit(1)

    # Step 2: Get Toast restaurants
    toast_restaurants = _get_toast_restaurants()

    if not toast_restaurants:
        print("\nWARNING: No Toast restaurants fetched. Running in offline mode.")
        print("The mapping will have empty GUIDs — fill them in manually.")
        toast_restaurants = []

    # Step 3: Match
    print(f"\nMatching {len(aloha_stores)} Aloha stores to {len(toast_restaurants)} Toast restaurants...")
    matches = _match_stores_to_restaurants(aloha_stores, toast_restaurants)

    # Step 4: Print results
    _print_matches(matches)

    # Step 5: Interactive review (optional)
    if args.interactive and toast_restaurants:
        matches = _interactive_review(matches, toast_restaurants)

    # Step 6: Write (optional)
    if args.write:
        _write_mapping(matches)
    else:
        print("\nDry run. Use --write to save the mapping file.")
        print("Use --interactive --write to review uncertain matches before saving.")

        # Still show what the file would contain
        preview = {m["toast_guid"]: m["aloha_id"] for m in matches if m["toast_guid"]}
        print(f"\nPreview ({len(preview)} entries):")
        for guid, aloha_id in list(preview.items())[:10]:
            name = next((m["toast_name"] for m in matches if m["toast_guid"] == guid), "")
            aloha_name = next((m["aloha_name"] for m in matches if m["aloha_id"] == aloha_id), "")
            print(f"  {guid[:8]}...  →  {aloha_id}  |  '{name}' ↔ '{aloha_name}'")
        if len(preview) > 10:
            print(f"  ... and {len(preview)-10} more")


if __name__ == "__main__":
    main()
