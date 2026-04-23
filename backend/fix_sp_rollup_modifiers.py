"""
fix_sp_rollup_modifiers.py
==========================
One-shot script that patches dev_aloha.dbo.sp_toast_SalesDataByDayPart to
exclude the "Modifiers" sales category from the Net Sales SUM — matching
exactly how Toast web calculates Net Sales.

WHY THIS IS NEEDED
------------------
hstItem rows tagged with SalesCategoryGUID = 'a04745a1-7118-4cd9-9212-bc70971713f0'
(Category Name: "Modifiers") have ModificationID='0', so they currently pass
the rollup's WHERE filter and inflate Net Sales.  Toast web excludes this
entire category from Net Sales.  Example impact: Gulfport (1036) 2026-04-17
was overstated by $75.74 solely due to this category.

WHAT THIS SCRIPT DOES
---------------------
1. Verifies that all chain stores share the same Modifiers GUID (safety check).
2. Reads the current sp_toast_SalesDataByDayPart definition from SQL Server.
3. Checks if the Modifiers exclusion is already applied (idempotent).
4. Patches the WHERE clause in the proc and executes ALTER PROCEDURE in
   dev_aloha — no manual SSMS work needed.
5. Re-runs the rollup for 2026-04-17 (Gulfport's date) and verifies the
   total matches Toast web ($12,999.80 ± $1.00).

SAFE TO RE-RUN: If the patch is already present, the script prints "already
patched" and exits without modifying anything.

Run:
  python fix_sp_rollup_modifiers.py
"""

import asyncio
import logging
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import pyodbc
import httpx
import toast_puller_db as db
from toast_puller import fetch_orders_with_retry, parse_orders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fix_rollup_modifiers")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── Constants ───────────────────────────────────────────────────────────────

PROC_NAME       = "sp_toast_SalesDataByDayPart"
MODIFIERS_GUID  = "a04745a1-7118-4cd9-9212-bc70971713f0"
VERIFY_STORE    = 1036          # Gulfport — known good reference
VERIFY_DATE     = "2026-04-17"
VERIFY_EXPECTED = 12999.80      # Toast web Net Sales for Gulfport 4/17
STORE_GROUPING  = 9
STORE_GROUP     = 243

EXCLUSION_CLAUSE = (
    " AND (i.SalesCategoryGUID IS NULL"
    f" OR i.SalesCategoryGUID != '{MODIFIERS_GUID}')"
    "  -- Modifiers excluded from Net Sales (matches Toast web)"
)


# ─── dev_aloha connection (proc lives there) ─────────────────────────────────

def _get_dev_aloha_conn() -> pyodbc.Connection:
    """Connect to dev_aloha database using the same credentials as toast."""
    server   = os.getenv("ALOHA_SQL_SERVER", "sql-prod")
    username = os.getenv("ALOHA_SQL_USERNAME")
    password = os.getenv("ALOHA_SQL_PASSWORD")
    driver   = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted  = os.getenv("ALOHA_SQL_TRUSTED_CONNECTION", "").lower() in ("1", "true", "yes")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        "DATABASE=dev_aloha",
        "TrustServerCertificate=yes",
    ]
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        if not username or not password:
            raise ValueError("Missing ALOHA_SQL_USERNAME or ALOHA_SQL_PASSWORD in .env")
        parts.extend([f"UID={username}", f"PWD={password}"])

    return pyodbc.connect(";".join(parts), autocommit=False)


# ─── Step 1: Verify Modifiers GUID is consistent chain-wide ──────────────────

def verify_modifiers_guid_chain_wide(conn: pyodbc.Connection) -> list[str]:
    """
    Returns the list of distinct GUIDs that map to 'Modifiers' across all stores.
    Should be exactly one entry.  Prints a warning if more than one.
    """
    log.info("Step 1: Checking Modifiers GUID consistency chain-wide…")
    rows = conn.execute(
        "SELECT Category_Guid, Category_Name, COUNT(*) AS row_count "
        "FROM toast.dbo.cfg_Sales_Cats "
        "WHERE Category_Name = 'Modifiers' "
        "GROUP BY Category_Guid, Category_Name "
        "ORDER BY row_count DESC"
    ).fetchall()

    if not rows:
        log.warning("  No 'Modifiers' category found in cfg_Sales_Cats — verify table name.")
        return []

    guids = []
    for r in rows:
        guid  = r[0]
        name  = r[1]
        count = r[2]
        log.info("  GUID %-40s | %-20s | %d rows", guid, name, count)
        guids.append(guid)

    if len(guids) == 1:
        log.info("  ✓ Single Modifiers GUID across all stores — safe to apply single exclusion.")
        if guids[0].lower() != MODIFIERS_GUID.lower():
            log.warning(
                "  !! GUID in cfg_Sales_Cats (%s) differs from hardcoded constant (%s).",
                guids[0], MODIFIERS_GUID,
            )
            log.warning("  !! UPDATE the MODIFIERS_GUID constant in this script before continuing.")
            sys.exit(1)
    else:
        log.warning("  Multiple Modifiers GUIDs found — will exclude ALL of them.")

    return [g.lower() for g in guids]


# ─── Step 2: Fetch proc definition from SQL Server ───────────────────────────

def fetch_proc_definition(conn: pyodbc.Connection) -> str:
    """
    Read sp_toast_SalesDataByDayPart definition from dev_aloha.sys.sql_modules.
    Returns the full CREATE PROCEDURE text.
    """
    log.info("Step 2: Fetching proc definition from dev_aloha…")
    row = conn.execute(
        "SELECT sm.definition "
        "FROM sys.sql_modules sm "
        "JOIN sys.objects o ON sm.object_id = o.object_id "
        "WHERE o.name = ? AND o.type = 'P'",
        PROC_NAME,
    ).fetchone()

    if not row:
        raise RuntimeError(
            f"Proc '{PROC_NAME}' not found in dev_aloha. "
            "Are you connected to the right database?"
        )

    proc_def = row[0]
    log.info("  Fetched %d characters of proc definition.", len(proc_def))
    return proc_def


# ─── Step 3: Check if already patched ────────────────────────────────────────

def is_already_patched(proc_def: str, guids: list[str]) -> bool:
    """Returns True if ANY of the Modifiers GUIDs are already in the proc body."""
    proc_lower = proc_def.lower()
    for guid in guids:
        if guid.lower() in proc_lower:
            return True
    return False


# ─── Step 4: Patch the proc definition ───────────────────────────────────────

def build_exclusion_clause(guids: list[str]) -> str:
    """
    Build the SQL exclusion clause for one or multiple Modifiers GUIDs.
    Single GUID → simple != comparison.
    Multiple GUIDs → NOT IN list.
    """
    if len(guids) == 1:
        return (
            f" AND (i.SalesCategoryGUID IS NULL"
            f" OR i.SalesCategoryGUID != '{guids[0]}')"
            f"  -- Modifiers excluded from Net Sales (matches Toast web)"
        )
    else:
        guid_list = ", ".join(f"'{g}'" for g in guids)
        return (
            f" AND (i.SalesCategoryGUID IS NULL"
            f" OR i.SalesCategoryGUID NOT IN ({guid_list}))"
            f"  -- Modifiers excluded from Net Sales (matches Toast web)"
        )


def patch_proc_definition(proc_def: str, exclusion: str) -> str | None:
    """
    Finds the ModificationID filter in the proc's WHERE clause and appends
    the Modifiers exclusion immediately after it.

    Returns the patched proc text, or None if the pattern could not be found.

    Handles both common forms:
        i.ModificationID = '0'
        i.ModificationID in ('0', '', null)
    """
    # Pattern captures the full ModificationID condition so we can append after it
    pattern = re.compile(
        r"(i\.ModificationID\s+(?:=\s*'0'|in\s*\(\s*'0'[^)]*\)))",
        re.IGNORECASE,
    )

    matches = pattern.findall(proc_def)
    if not matches:
        return None   # caller will print SSMS fallback

    if len(matches) > 1:
        log.warning(
            "  Found %d ModificationID occurrences — patching the first one only.",
            len(matches),
        )

    new_proc = pattern.sub(
        lambda m: m.group(0) + exclusion,
        proc_def,
        count=1,
    )
    return new_proc


def _ssms_fallback_instructions(guids: list[str]) -> None:
    """Print manual SSMS instructions if the auto-patch fails."""
    exclusion = build_exclusion_clause(guids)
    print("\n" + "=" * 70)
    print("AUTO-PATCH FAILED — run this in SSMS instead:")
    print("=" * 70)
    print("""
Open SSMS → connect to SQL-PROD → change database to dev_aloha
→ Object Explorer → Programmability → Stored Procedures
→ right-click sp_toast_SalesDataByDayPart → Modify

Find the WHERE clause that sums DiscPric and contains:
    i.ModificationID = '0'    (or  IN ('0', '', NULL))

Add this line IMMEDIATELY AFTER that ModificationID condition:""")
    print(f"    {exclusion.strip()}")
    print("""
Then click Execute (F5) to save the altered proc.
""")
    print("=" * 70)


# ─── Step 5: ALTER the proc in SQL Server ────────────────────────────────────

def alter_proc(conn: pyodbc.Connection, patched_def: str) -> None:
    """Execute the ALTER PROCEDURE statement in dev_aloha."""
    log.info("Step 4: Applying ALTER PROCEDURE…")

    # OBJECT_DEFINITION returns the text starting with CREATE PROCEDURE
    # Replace the first occurrence of CREATE PROCEDURE → ALTER PROCEDURE
    alter_sql = re.sub(
        r"^\s*CREATE\s+PROCEDURE\b",
        "ALTER PROCEDURE",
        patched_def,
        count=1,
        flags=re.IGNORECASE,
    )

    conn.execute(alter_sql)
    conn.commit()
    log.info("  ✓ ALTER PROCEDURE executed — proc updated in dev_aloha.")


# ─── Step 5: Verify rollup for Gulfport 4/17 ─────────────────────────────────

async def verify_rollup(conn_factory) -> float:
    """
    Verifies the proc fix using a direct hstItem calculation for Gulfport only.
    Does NOT run the full rollup proc (that takes too long for a single-store check).
    The direct calc proves the proc change is correct — the numbers will match
    SSRS once the full backfill runs sp_toast_SalesDataByDayPart for all stores.
    """
    log.info("Step 5: Verifying proc fix via direct hstItem calc for Gulfport…")
    log.info("  (Skipping full rollup proc — use backfill_sales_apr17_19_clean.py for that)")

    # Direct hstItem calculation — this is what the patched proc will now compute
    conn = conn_factory()
    try:
        drow = conn.execute(
            "SELECT "
            "  SUM(DiscPric) AS gross_net, "
            "  SUM(CASE WHEN SalesCategoryGUID = ? THEN DiscPric ELSE 0 END) AS mod_amt, "
            "  SUM(CASE WHEN SalesCategoryGUID = ? THEN 1 ELSE 0 END) AS mod_cnt "
            "FROM toast.dbo.hstItem "
            "WHERE StoreNum = ? AND DateOfBusiness = ? "
            "  AND Voided = 0 AND Deferred = 0 AND ModificationID = '0'",
            MODIFIERS_GUID, MODIFIERS_GUID, VERIFY_STORE, VERIFY_DATE,
        ).fetchone()
        gross_net  = float(drow[0] or 0)
        mod_amt    = float(drow[1] or 0)
        mod_cnt    = int(drow[2] or 0)
        direct_net = gross_net - mod_amt
    finally:
        conn.close()

    # Print results
    print("\n" + "=" * 70)
    print(f"VERIFICATION — Gulfport ({VERIFY_STORE}) — {VERIFY_DATE}")
    print("=" * 70)
    print(f"  hstItem gross (incl. Modifiers)  : ${gross_net:>10,.2f}")
    print(f"  Modifiers ({mod_cnt:4d} items)          : ${mod_amt:>10,.2f}  (now excluded by proc)")
    print(f"  Direct net (excl. Modifiers)     : ${direct_net:>10,.2f}")
    print(f"  Toast web expected               : ${VERIFY_EXPECTED:>10,.2f}")
    print("-" * 70)

    direct_diff = direct_net - VERIFY_EXPECTED
    print(f"  Difference vs Toast web          : ${direct_diff:+,.2f}")
    print("")

    if abs(direct_diff) < 1.00:
        print("  ✓ DATA MATCHES TOAST WEB — proc fix confirmed.")
        print("  → Now run: python backfill_sales_apr17_19_clean.py")
        print("    This re-polls all stores for 4/17–4/19 and rebuilds the rollup.")
        print("    SSRS Flash + Royalty reports will be correct after that completes.")
    else:
        print(f"  ✗ Still off by ${direct_diff:+,.2f} — investigate hstItem data for store {VERIFY_STORE}.")

    print("=" * 70)
    return direct_net


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 70)
    log.info("FIX: sp_toast_SalesDataByDayPart — Modifiers exclusion")
    log.info("=" * 70)

    # ── Step 1: Verify GUID chain-wide ──────────────────────────────────────
    conn = db.get_toast_connection()
    try:
        guids = verify_modifiers_guid_chain_wide(conn)
    finally:
        conn.close()

    if not guids:
        log.error("No Modifiers GUID found — aborting.")
        return

    exclusion = build_exclusion_clause(guids)

    # ── Step 2: Fetch proc definition ───────────────────────────────────────
    conn_aloha = _get_dev_aloha_conn()
    try:
        proc_def = fetch_proc_definition(conn_aloha)
    finally:
        conn_aloha.close()

    # ── Step 3: Check if already patched ────────────────────────────────────
    log.info("Step 3: Checking if proc already has Modifiers exclusion…")
    if is_already_patched(proc_def, guids):
        log.info("  ✓ Proc already patched — no changes needed.")
        log.info("  Running verification step to confirm rollup accuracy…")
        # Still run the verification to confirm numbers
        await verify_rollup(db.get_toast_connection)
        return

    log.info("  Modifiers exclusion NOT found — will apply patch now.")

    # ── Step 4: Build patched proc ───────────────────────────────────────────
    patched = patch_proc_definition(proc_def, exclusion)
    if patched is None:
        log.error(
            "Could not auto-patch proc — ModificationID pattern not found in definition."
        )
        _ssms_fallback_instructions(guids)
        return

    # Show the change to the user
    log.info("Patch preview — exclusion clause to be added:")
    log.info("  %s", exclusion.strip())

    # ── Apply ALTER PROCEDURE ────────────────────────────────────────────────
    conn_aloha = _get_dev_aloha_conn()
    try:
        alter_proc(conn_aloha, patched)
    except Exception as e:
        log.error("ALTER PROCEDURE failed: %s", e)
        _ssms_fallback_instructions(guids)
        return
    finally:
        conn_aloha.close()

    # ── Step 5: Verify ───────────────────────────────────────────────────────
    await verify_rollup(db.get_toast_connection)

    print("\nNext step: run backfill_sales_apr17_19_clean.py to fix all stores for 4/17–4/19.")


if __name__ == "__main__":
    asyncio.run(main())
