"""
seed_olo_vendor_ids.py — one-time seed of the app-owned StoreOLOVendor table.

Loads StoreID -> OLO Vendor ID pairs from
backend/data/contacts/olo_vendor_seed.json (extracted from the "Restaurant
Contact Information Sheet" xlsx export, itself the last output of the legacy
SQL Server OLO.dbo.OLOVendors ZappySys ETL) and upserts them into the local
contacts.db via contacts_db.set_store_olo_vendor().

Once seeded, the OLO Vendor ID lives entirely in this app: it is preserved
across contacts-mirror refreshes and surfaced in the Restaurant Contact
Information report and the store edit UI. The SQL Server OLO ETL — and its
paid ZappySys dependency on NEWKS-SQL — can then be retired.

WHERE THIS RUNS: on Cloudways (where contacts.db lives), from the backend/
directory:

    python seed_olo_vendor_ids.py --dry-run     # preview, no writes
    python seed_olo_vendor_ids.py               # upsert all seed rows
    python seed_olo_vendor_ids.py --only-missing # only fill blanks; don't overwrite

The seed is idempotent — re-running it simply re-asserts the same values.
"""

import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contacts_db  # noqa: E402

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "contacts", "olo_vendor_seed.json")
SEED_USER = "seed_olo_vendor_ids"


def load_seed(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    mapping = data.get("mapping", data)  # tolerate a bare {id: olo} file too
    # Normalize keys to int StoreIDs, values to stripped strings.
    out = {}
    for k, v in mapping.items():
        if str(k).startswith("_"):  # skip _comment / metadata keys
            continue
        try:
            out[int(k)] = str(v).strip()
        except (ValueError, TypeError):
            logger.warning("skipping unparseable seed row: %r -> %r", k, v)
    return out


def main():
    p = argparse.ArgumentParser(description="Seed StoreOLOVendor from the OLO vendor seed JSON.")
    p.add_argument("--seed", default=SEED_PATH, help=f"Seed JSON path (default: {SEED_PATH})")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    p.add_argument("--only-missing", action="store_true",
                   help="Only set stores that don't already have an OLO Vendor ID.")
    args = p.parse_args()

    if not os.path.exists(args.seed):
        sys.exit(f"ERROR: seed file not found: {args.seed}")

    # Make sure the StoreOLOVendor table (and the rest of the app-local schema)
    # exists before we write to it.
    contacts_db.initialize_schema()

    seed = load_seed(args.seed)
    logger.info("Loaded %d OLO Vendor ID pairs from %s", len(seed), args.seed)

    existing = contacts_db.get_olo_vendor_map()

    set_count = skipped_missing = unchanged = no_store = 0
    for store_id, olo in sorted(seed.items()):
        if not contacts_db.get_store(store_id):
            logger.warning("StoreID %s not in Store table — seeding OLO ID %s anyway "
                           "(row will resolve once the store exists)", store_id, olo)
            no_store += 1
        current = existing.get(store_id)
        if args.only_missing and current:
            skipped_missing += 1
            continue
        if current == olo:
            unchanged += 1
            continue
        logger.info("%s: %s -> %s%s", store_id,
                    current or "(none)", olo,
                    " [dry-run]" if args.dry_run else "")
        if not args.dry_run:
            contacts_db.set_store_olo_vendor(store_id, olo, updated_by=SEED_USER)
        set_count += 1

    logger.info("=" * 60)
    logger.info("OLO Vendor ID seed %s:", "PREVIEW" if args.dry_run else "complete")
    logger.info("  set/updated:            %d", set_count)
    logger.info("  unchanged (same value): %d", unchanged)
    if args.only_missing:
        logger.info("  skipped (already set):  %d", skipped_missing)
    if no_store:
        logger.info("  StoreID not in Store:   %d", no_store)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
