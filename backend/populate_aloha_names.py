"""
Populate AlohaItemName in Mapping Table
=========================================
Finds the best Aloha item match for each row in the mapping table that
currently has a blank AlohaItemName, using item names collected from the
Aloha CSV files in backend/data/aloha/.

Matching strategy (in priority order):
  1. Exact match after stripping Toast-specific parentheticals
  2. SequenceMatcher ratio (character-level similarity)
  3. Token Jaccard similarity (word overlap)
  Combined score = max(seq_ratio, token_jaccard)

Confidence tiers:
  ≥ 0.90  → HIGH   — auto-applied when --apply is passed
  0.60–0.89 → MEDIUM — written to review sheet in output Excel
  < 0.60  → LOW    — no suggestion (likely a Toast-only item with no Aloha equivalent)

Usage:
    # Dry run — see what would be matched (no changes):
    python populate_aloha_names.py

    # Preview output Excel with suggestions (no mapping table changes):
    python populate_aloha_names.py --output aloha_name_suggestions.xlsx

    # Apply HIGH confidence matches to mapping table + write review Excel:
    python populate_aloha_names.py --apply --output aloha_name_suggestions.xlsx

    # Strict mode — only auto-apply ≥95%:
    python populate_aloha_names.py --apply --min-auto 0.95 --output aloha_name_suggestions.xlsx
"""

import argparse
import csv
import os
import re
import sys
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE  = os.path.join(SCRIPT_DIR, "..", "Updated Mapping Table.xlsx")
DATA_DIR      = os.path.join(SCRIPT_DIR, "data", "aloha")
OUTPUT_FILE   = os.path.join(SCRIPT_DIR, "aloha_name_suggestions.xlsx")

try:
    import pandas as pd
    import openpyxl  # noqa – needed for Excel write
except ImportError as e:
    print(f"ERROR: {e}\nRun: pip install pandas openpyxl")
    sys.exit(1)


# ── Normalization ──────────────────────────────────────────────────────────────

# Parentheticals that are Toast-specific context, not part of the item name
_STRIP_PARENS = re.compile(
    r'\s*\('
    r'(?:olo|pos|entree|full|half|pairing|meal deal|pick [0-9]|pick3|pairing choice'
    r'|full pairing|half pairing|catering|modifier|grab n go|grab &amp; go'
    r'|beverage|breakfast uga|parent items?|parent item|test beverage'
    r'|full salad|half salad|half sandwich|half wrap|full sandwich|full wrap'
    r'|full pizza|half pizza|non-taxable|wine|beer|olo parent'
    r'|[^)]*?modifier[^)]*?|[^)]*?pairing[^)]*?|[^)]*?entree[^)]*?'
    r')[^)]*?\)'
    r'\s*',
    re.IGNORECASE,
)

_EXTRA_SPACE = re.compile(r'\s+')


def _strip_context(name: str) -> str:
    """Remove Toast-specific suffixes/parentheticals to get the base item name."""
    s = str(name or "").strip()
    # Repeatedly strip matching parentheticals until nothing more to strip
    prev = None
    while prev != s:
        prev = s
        s = _STRIP_PARENS.sub(" ", s).strip()
    return _EXTRA_SPACE.sub(" ", s).strip()


def _normalize(name: str) -> str:
    """Lowercase, expand abbreviations, standardize for fuzzy comparison."""
    s = str(name or "").lower()
    s = s.replace("&", "and")
    s = s.replace("'", "")   # Po'Boy → PoBy... handled by token match
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\s/]", " ", s)
    s = _EXTRA_SPACE.sub(" ", s).strip()
    return s


# ── Scoring ───────────────────────────────────────────────────────────────────

def _seq_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_jaccard(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _score(toast_norm: str, aloha_norm: str) -> float:
    """Combined similarity score 0–1."""
    if toast_norm == aloha_norm:
        return 1.0
    seq = _seq_ratio(toast_norm, aloha_norm)
    tok = _token_jaccard(toast_norm, aloha_norm)
    # Weight towards whichever is higher; boost when both agree
    return max(seq, tok) * 0.7 + min(seq, tok) * 0.3


# ── Load data ─────────────────────────────────────────────────────────────────

def load_aloha_item_names() -> Set[str]:
    """Collect all distinct item names that appear in ANY Aloha CSV."""
    names: Set[str] = set()
    if not os.path.isdir(DATA_DIR):
        return names
    for fname in os.listdir(DATA_DIR):
        if not fname.startswith("aloha_") or not fname.endswith(".csv"):
            continue
        path = os.path.join(DATA_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                if "Item" not in header:
                    continue
                idx = header.index("Item")
                for row in reader:
                    if len(row) > idx and row[idx].strip():
                        names.add(row[idx].strip())
        except Exception:
            continue
    return names


def load_mapping_table() -> pd.DataFrame:
    if not os.path.exists(MAPPING_FILE):
        print(f"ERROR: Mapping file not found: {MAPPING_FILE}")
        sys.exit(1)
    df = pd.read_excel(MAPPING_FILE)
    return df


# ── Matching ──────────────────────────────────────────────────────────────────

def find_best_match(
    toast_name: str,
    aloha_names: List[str],
    aloha_norms: List[str],
) -> Tuple[Optional[str], float, str]:
    """
    Returns (best_aloha_name, score, method) for a Toast item name.
    method is one of: 'exact', 'fuzzy', 'none'
    """
    stripped    = _strip_context(toast_name)
    norm_full   = _normalize(toast_name)
    norm_stripped = _normalize(stripped)

    best_name  = None
    best_score = 0.0
    best_method = "none"

    for aloha_raw, aloha_norm in zip(aloha_names, aloha_norms):
        # Exact match on stripped name
        if norm_stripped and norm_stripped == aloha_norm:
            return aloha_raw, 1.0, "exact"

        # Exact match on full toast name (without stripping)
        if norm_full and norm_full == aloha_norm:
            return aloha_raw, 1.0, "exact"

        # Fuzzy on stripped name
        sc_stripped = _score(norm_stripped, aloha_norm) if norm_stripped else 0.0
        # Fuzzy on full name (less common but sometimes better)
        sc_full     = _score(norm_full, aloha_norm)
        sc = max(sc_stripped, sc_full)

        if sc > best_score:
            best_score  = sc
            best_name   = aloha_raw
            best_method = "fuzzy"

    return best_name, best_score, best_method if best_score > 0 else "none"


# ── Main ──────────────────────────────────────────────────────────────────────

def apply_from_review(review_path: str) -> None:
    """
    Read a previously-generated suggestions Excel, find rows where Accept=YES,
    and write those AlohaItemName values back into the mapping table.
    """
    if not os.path.exists(review_path):
        print(f"ERROR: Review file not found: {review_path}")
        sys.exit(1)

    print(f"Loading review file: {review_path}")
    rev = pd.read_excel(review_path, sheet_name="Suggestions")

    # Normalise Accept column — treat YES/yes/y/1/true as accepted
    def _accepted(v) -> bool:
        return str(v).strip().lower() in {"yes", "y", "1", "true", "x"}

    accepted = rev[rev["Accept"].apply(_accepted)].copy()
    print(f"  Total suggestion rows: {len(rev):,}")
    print(f"  Rows marked Accept:    {len(accepted):,}")

    if accepted.empty:
        print("Nothing to apply — set Accept=YES on rows you want applied.")
        return

    print("\nLoading mapping table...")
    df = load_mapping_table()

    # Build index: normalized Toast_Item_Name -> list of df row indices
    # (a Toast name can appear more than once in the mapping table, e.g. duplicate entries)
    name_to_indices: Dict[str, list] = {}
    for idx, row in df.iterrows():
        key = str(row.get("Toast_Item_Name") or "").strip().lower()
        if key:
            name_to_indices.setdefault(key, []).append(idx)

    applied = 0
    skipped = 0
    not_found = []

    for _, rev_row in accepted.iterrows():
        toast_name  = str(rev_row.get("Toast_Item_Name") or "").strip()
        aloha_name  = str(rev_row.get("Suggested_AlohaItemName") or "").strip()
        if not toast_name or not aloha_name:
            skipped += 1
            continue

        indices = name_to_indices.get(toast_name.lower(), [])
        if not indices:
            not_found.append(toast_name)
            continue

        for idx in indices:
            current = str(df.at[idx, "AlohaItemName"] or "").strip()
            # Don't overwrite an already-populated AlohaItemName
            if current and current.lower() not in ("", "nan"):
                skipped += 1
                continue
            df.at[idx, "AlohaItemName"] = aloha_name
            applied += 1

    print(f"\n  Applied:   {applied}")
    print(f"  Skipped:   {skipped}  (already had value or empty suggestion)")
    if not_found:
        print(f"  Not found in mapping table ({len(not_found)}):")
        for n in not_found[:10]:
            print(f"    - {n}")
        if len(not_found) > 10:
            print(f"    ... and {len(not_found) - 10} more")

    if applied:
        try:
            df.to_excel(MAPPING_FILE, index=False)
            print(f"\n✓ Saved updated mapping table to:")
            print(f"  {MAPPING_FILE}")
            print(f"\n  IMPORTANT: Restart the backend to reload the mapping cache.")
        except PermissionError:
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fb = MAPPING_FILE.replace(".xlsx", f"_{ts}.xlsx")
            df.to_excel(fb, index=False)
            print(f"\n  WARNING: Original file is open in Excel. Saved to:")
            print(f"  {fb}")
            print(f"  Close Excel, then rename this file to replace the original.")
    else:
        print("\nNo changes written.")


def main():
    parser = argparse.ArgumentParser(description="Populate AlohaItemName in mapping table.")
    parser.add_argument("--apply",    action="store_true",
                        help="Write HIGH-confidence matches back to mapping table Excel")
    parser.add_argument("--apply-from-review", dest="review_file", default=None,
                        help="Apply Accept=YES rows from a review Excel file produced by this script")
    parser.add_argument("--min-auto", type=float, default=0.90,
                        help="Min score to auto-apply (default 0.90)")
    parser.add_argument("--min-suggest", type=float, default=0.60,
                        help="Min score to include in review suggestions (default 0.60)")
    parser.add_argument("--output",   default=OUTPUT_FILE,
                        help="Output Excel with suggestions")
    args = parser.parse_args()

    # ── Review-file apply mode (early exit) ───────────────────────────────────
    if args.review_file:
        apply_from_review(args.review_file)
        return

    # ── Load ──────────────────────────────────────────────────────────────────
    print("Loading mapping table...")
    df = load_mapping_table()
    total = len(df)

    blank_mask = df["AlohaItemName"].isna() | (
        df["AlohaItemName"].astype(str).str.strip().isin(["", "nan"])
    )
    blank_df  = df[blank_mask].copy()
    filled_df = df[~blank_mask].copy()

    print(f"  Total rows:            {total:,}")
    print(f"  Already have AlohaName: {len(filled_df):,}")
    print(f"  Blank AlohaItemName:    {len(blank_df):,}  <- working on these")

    print("\nLoading Aloha item names from CSV files...")
    aloha_names_set = load_aloha_item_names()
    aloha_names     = sorted(aloha_names_set)
    aloha_norms     = [_normalize(n) for n in aloha_names]
    print(f"  Distinct Aloha items: {len(aloha_names):,}")

    if not aloha_names:
        print("ERROR: No Aloha item names found. Make sure CSVs are in data/aloha/")
        sys.exit(1)

    # ── Match ─────────────────────────────────────────────────────────────────
    print(f"\nMatching {len(blank_df):,} blank rows...")
    results = []
    high_count = medium_count = low_count = 0

    for idx, row in blank_df.iterrows():
        toast_name = str(row.get("Toast_Item_Name") or "").strip()
        if not toast_name:
            continue

        best_name, score, method = find_best_match(toast_name, aloha_names, aloha_norms)
        stripped = _strip_context(toast_name)

        tier = "HIGH" if score >= args.min_auto else (
               "MEDIUM" if score >= args.min_suggest else "LOW")
        if tier == "HIGH":   high_count += 1
        elif tier == "MEDIUM": medium_count += 1
        else:                low_count += 1

        results.append({
            "_df_index":        idx,
            "Toast_Item_Name":  toast_name,
            "Stripped_Name":    stripped,
            "ConsolidatedCat":  row.get("ConsolidatedCat", ""),
            "Suggested_Aloha":  best_name or "",
            "Score":            round(score, 3),
            "Method":           method,
            "Tier":             tier,
        })

    print(f"\n  HIGH confidence (>={args.min_auto:.0%}):    {high_count:>4}")
    print(f"  MEDIUM confidence ({args.min_suggest:.0%}-{args.min_auto:.0%}): {medium_count:>4}")
    print(f"  LOW / no match (<{args.min_suggest:.0%}):     {low_count:>4}")

    # ── Preview ───────────────────────────────────────────────────────────────
    high_results   = [r for r in results if r["Tier"] == "HIGH"]
    medium_results = [r for r in results if r["Tier"] == "MEDIUM"]

    print(f"\nTop HIGH confidence matches (showing up to 20):")
    for r in sorted(high_results, key=lambda x: -x["Score"])[:20]:
        print(f"  [{r['Score']:.2f}] '{r['Toast_Item_Name']}'")
        print(f"         → '{r['Suggested_Aloha']}'  [{r['ConsolidatedCat']}]")

    if medium_results:
        print(f"\nSample MEDIUM confidence matches (showing up to 10):")
        for r in sorted(medium_results, key=lambda x: -x["Score"])[:10]:
            print(f"  [{r['Score']:.2f}] '{r['Toast_Item_Name']}'")
            print(f"         → '{r['Suggested_Aloha']}'  [{r['ConsolidatedCat']}]")

    # ── Write suggestions Excel ───────────────────────────────────────────────
    output_path = args.output
    out_rows = [
        {
            "Toast_Item_Name": r["Toast_Item_Name"],
            "Stripped_Name":   r["Stripped_Name"],
            "Suggested_AlohaItemName": r["Suggested_Aloha"],
            "Score":           r["Score"],
            "Tier":            r["Tier"],
            "Method":          r["Method"],
            "ConsolidatedCat": r["ConsolidatedCat"],
            "Accept": "YES" if r["Tier"] == "HIGH" else "",  # prefill for review
        }
        for r in results if r["Tier"] in ("HIGH", "MEDIUM")
    ]

    if out_rows:
        suggestions_df = pd.DataFrame(out_rows)
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            suggestions_df.to_excel(writer, sheet_name="Suggestions", index=False)
            # Also write the rows with no match for awareness
            no_match = pd.DataFrame([
                {"Toast_Item_Name": r["Toast_Item_Name"],
                 "ConsolidatedCat": r["ConsolidatedCat"],
                 "Note": "No Aloha equivalent found"}
                for r in results if r["Tier"] == "LOW"
            ])
            if not no_match.empty:
                no_match.to_excel(writer, sheet_name="No_Match", index=False)

        print(f"\n✓ Suggestions written to: {output_path}")
        print(f"  Sheet 'Suggestions': {len(out_rows)} rows (review/edit Accept column)")
        print(f"  Sheet 'No_Match':    {low_count} rows (likely Toast-only items, no Aloha equivalent)")
    else:
        print("\nNo suggestions to write.")

    # ── Apply HIGH confidence to mapping table ────────────────────────────────
    if args.apply:
        applied = 0
        for r in high_results:
            if r["Suggested_Aloha"]:
                df.at[r["_df_index"], "AlohaItemName"] = r["Suggested_Aloha"]
                applied += 1

        if applied:
            try:
                df.to_excel(MAPPING_FILE, index=False)
                print(f"\n✓ Applied {applied} HIGH-confidence AlohaItemName values to:")
                print(f"  {MAPPING_FILE}")
                print(f"\n  IMPORTANT: Restart the backend to reload the mapping cache.")
            except PermissionError:
                import datetime
                ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                fb  = MAPPING_FILE.replace(".xlsx", f"_{ts}.xlsx")
                df.to_excel(fb, index=False)
                print(f"\n  WARNING: Could not write to original file (it may be open in Excel).")
                print(f"  Saved to: {fb}")
                print(f"  Manually replace the original mapping file with this one.")
        else:
            print("\nNo HIGH confidence matches to apply.")

        print(f"\nTo apply MEDIUM confidence matches too:")
        print(f"  1. Open {output_path}")
        print(f"  2. Review rows where Tier=MEDIUM, correct Suggested_AlohaItemName if needed")
        print(f"  3. Set Accept=YES for ones you want applied")
        print(f"  4. Run: python populate_aloha_names.py --apply-from-review {output_path}")
    else:
        print(f"\nDry run — no changes made to the mapping table.")
        print(f"Run with --apply to write HIGH confidence matches ({high_count} items).")

    print("\nDone.")


if __name__ == "__main__":
    main()
