"""
contacts_outbox_drainer.py — Drain the Cloudways contacts_outbox queue
into SQL-PROD.

WHY THIS EXISTS
Cloudways can't reach the internal sql-prod host directly. The reporting
app captures every contacts edit (Store, AreaDir, Company, lookup tables,
etc.) into a local SQLite "outbox" table when CONTACTS_OUTBOX_ENABLED=1.
This script — which DOES run on the internal Newk's network (dev-jan02
or any box that can reach sql-prod) — periodically:

    1. Fetches pending outbox rows from Cloudways over HTTPS
    2. Replays them against SQL Server using pymssql
    3. Posts the result (success / failure) back to Cloudways

WHERE THIS RUNS
dev-jan02.nfc.local, via cron (hourly). See contacts_outbox_runbook.md.

USAGE
    # Dry run — print what would be applied, don't touch SQL Server
    python contacts_outbox_drainer.py --dry-run

    # Live run (what cron does)
    python contacts_outbox_drainer.py

    # Override batch size for one-off catch-up
    python contacts_outbox_drainer.py --batch-size 500

ENV (set in ~/.contacts_drainer.env on dev-jan02)
    APP_BASE_URL              https://your-cloudways-host
    APP_ADMIN_EMAIL           admin login email — PREFERRED auth: the drainer
    APP_ADMIN_PASSWORD        logs itself in each run and mints a fresh token,
                              so the 30-day token expiry can never kill it
                              (which is exactly what happened 2026-06-04..12)
    APP_ADMIN_TOKEN           static bearer token — legacy fallback, only used
                              when email/password are not set; expires ~30 days
    CONTACTS_SQL_HOST         defaults to sql-prod.nfc.local
    CONTACTS_SQL_USER         SQL Server account WITH WRITE access
    CONTACTS_SQL_PASSWORD     password
    CONTACTS_SQL_DATABASE     defaults to "contacts"
    CONTACTS_SQL_SCHEMA       defaults to "dbo"
    CONTACTS_SOURCE_PASSWORD  read password for contacts_export_from_sqlserver
                              (Sync-from-SQL fulfillment); defaults to
                              CONTACTS_SQL_PASSWORD if unset

    # Optional email alerts on errors
    SMTP_HOST                 defaults to smtp.gmail.com
    SMTP_PORT                 defaults to 587 (STARTTLS)
    SMTP_USER                 sender Gmail address
    SMTP_PASSWORD             app password
    ALERT_TO                  recipient address(es), semicolon-separated

SCOPE (v1 — Phase 2)
- UPDATE and DELETE operations: fully supported.
- INSERT operations: NOT yet supported (requires SET IDENTITY_INSERT
  handling per table). INSERT rows are marked 'error' with a message
  asking the admin to add the row directly in SQL Server via SSMS,
  then re-export the SQLite mirror. INSERT support is planned for
  Phase 3.

RETRY POLICY
Each pending row's attempt counter is bumped each time we claim it.
- If attempts <= MAX_ATTEMPTS-1 and apply fails: leave it pending,
  next cron run will retry.
- If attempts >= MAX_ATTEMPTS and apply fails: mark 'error', stops
  being retried automatically. Admin can hit Retry in the UI to reset.
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import traceback
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config (from env) ────────────────────────────────────────────────────────
APP_BASE_URL       = os.getenv("APP_BASE_URL", "").rstrip("/")
APP_ADMIN_TOKEN    = os.getenv("APP_ADMIN_TOKEN", "")
APP_ADMIN_EMAIL    = os.getenv("APP_ADMIN_EMAIL", "")
APP_ADMIN_PASSWORD = os.getenv("APP_ADMIN_PASSWORD", "")
SQL_HOST        = os.getenv("CONTACTS_SQL_HOST",     "sql-prod.nfc.local")
SQL_USER        = os.getenv("CONTACTS_SQL_USER",     "")
SQL_PASSWORD    = os.getenv("CONTACTS_SQL_PASSWORD", "")
SQL_DATABASE    = os.getenv("CONTACTS_SQL_DATABASE", "contacts")
SQL_SCHEMA      = os.getenv("CONTACTS_SQL_SCHEMA",   "dbo")

SMTP_HOST       = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER       = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD   = os.getenv("SMTP_PASSWORD", "")
ALERT_TO        = os.getenv("ALERT_TO",      "")

MAX_ATTEMPTS    = 3
DEFAULT_BATCH   = 100
HTTP_TIMEOUT    = 30   # seconds


# ── Cache of audit columns per table (one INFORMATION_SCHEMA query per run) ──
_audit_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}


def _get_audit_cols(cur, table: str) -> Tuple[Optional[str], Optional[str]]:
    """Detect which audit-column pair this table uses.
    Returns (audit_by_col, audit_at_col). Some tables use LastUpdatedBy/
    LastUpdated; older ones (StorePhysical, DeliveryPartnersByStore) use
    LastEditedBy/LastEdited. Cached per-run since INFORMATION_SCHEMA is
    relatively expensive to hit repeatedly."""
    if table in _audit_cache:
        return _audit_cache[table]
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM   contacts.INFORMATION_SCHEMA.COLUMNS
        WHERE  TABLE_NAME = %s
          AND  COLUMN_NAME IN ('LastUpdatedBy', 'LastUpdated', 'LastEditedBy', 'LastEdited')
        """,
        (table,),
    )
    cols = {r[0] for r in cur.fetchall()}
    by = "LastUpdatedBy" if "LastUpdatedBy" in cols else (
         "LastEditedBy"  if "LastEditedBy"  in cols else None)
    at = "LastUpdated"   if "LastUpdated"   in cols else (
         "LastEdited"    if "LastEdited"    in cols else None)
    _audit_cache[table] = (by, at)
    return (by, at)


# ── HTTP client (Cloudways API) ──────────────────────────────────────────────
# Auth: if APP_ADMIN_EMAIL/PASSWORD are set, the drainer logs itself in and
# mints a fresh token every run — immune to the ~30-day token expiry that
# silently killed the drainer from 2026-06-04 to 06-12. APP_ADMIN_TOKEN is
# the legacy fallback for environments that only have a static token.

_token = APP_ADMIN_TOKEN


def _login() -> bool:
    """Mint a fresh bearer token from APP_ADMIN_EMAIL/PASSWORD.
    Returns True on success; False (with a log line) otherwise."""
    global _token
    if not (APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD):
        return False
    import requests
    try:
        r = requests.post(
            f"{APP_BASE_URL}/api/auth/login",
            json={"email": APP_ADMIN_EMAIL, "password": APP_ADMIN_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Login request failed: %s", exc)
        return False
    if r.status_code != 200:
        logger.error("Login failed (HTTP %d): %s", r.status_code, r.text[:200])
        return False
    _token = (r.json() or {}).get("access_token") or ""
    if _token:
        logger.info("Logged in as %s — fresh token minted.", APP_ADMIN_EMAIL)
    return bool(_token)


def _headers():
    return {"Authorization": f"Bearer {_token}"}


def _api(method: str, path: str, **kwargs):
    """requests wrapper with one automatic re-login retry on 401."""
    import requests
    url = f"{APP_BASE_URL}{path}"
    r = requests.request(method, url, headers=_headers(),
                         timeout=kwargs.pop("timeout", HTTP_TIMEOUT), **kwargs)
    if r.status_code == 401 and _login():
        r = requests.request(method, url, headers=_headers(),
                             timeout=HTTP_TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def claim_batch(batch_size: int):
    return _api("POST", "/api/contacts/outbox/claim",
                params={"batch_size": batch_size}).json()


def mark_applied(row_id: int, success: bool, error_message: Optional[str] = None):
    _api("POST", f"/api/contacts/outbox/{row_id}/applied",
         json={"success": success, "error_message": error_message})


# ── SQL Server connection ────────────────────────────────────────────────────

def _connect_sql():
    try:
        import pymssql
    except ImportError:
        sys.exit("ERROR: pymssql not installed. Run: pip3 install pymssql")
    return pymssql.connect(
        server=SQL_HOST,
        user=SQL_USER,
        password=SQL_PASSWORD,
        database=SQL_DATABASE,
        timeout=30,
        login_timeout=15,
    )


def _qualified(table: str) -> str:
    """Schema-qualified table name. We always run as ``contacts`` DB, so
    {database}.{schema}.{table} is overkill — {schema}.{table} suffices —
    but the explicit form is unambiguous and matches what the exporter does."""
    return f"{SQL_DATABASE}.{SQL_SCHEMA}.{table}"


# ── Replay logic ─────────────────────────────────────────────────────────────

def _apply_update(cur, row: Dict[str, Any]) -> None:
    """Replay an UPDATE. Honors the per-table audit-column convention so the
    SQL-Server-side audit triggers fire correctly."""
    table   = row["table_name"]
    pk_col  = row["pk_col"]
    pk_val  = row["pk_val"]
    payload = json.loads(row.get("payload_json") or "{}")
    user    = row.get("source_user") or "outbox-drainer"

    if not payload:
        # No-op update (the user opened + saved with no changes). Skip cleanly.
        logger.info("Row %s: UPDATE with empty payload, skipping", row["id"])
        return

    audit_by, audit_at = _get_audit_cols(cur, table)

    set_parts = [f"{k} = %s" for k in payload.keys()]
    values: List[Any] = list(payload.values())
    if audit_by:
        set_parts.append(f"{audit_by} = %s")
        values.append(user)
    if audit_at:
        set_parts.append(f"{audit_at} = GETDATE()")
    values.append(pk_val)
    sql = f"UPDATE {_qualified(table)} SET {', '.join(set_parts)} WHERE {pk_col} = %s"
    cur.execute(sql, tuple(values))
    if cur.rowcount == 0:
        # The local SQLite said the row exists, but SQL Server doesn't have
        # it. Could mean: row was deleted out-of-band, or PK mismatch from
        # an out-of-sync mirror. Raise so this surfaces as an error.
        raise RuntimeError(
            f"UPDATE matched 0 rows in {table} where {pk_col}={pk_val!r}. "
            "Row may have been deleted out-of-band in SQL Server, or the "
            "local SQLite mirror is out of sync."
        )


def _apply_delete(cur, row: Dict[str, Any]) -> None:
    """Replay a DELETE. Stamps the audit-by column BEFORE the delete so
    SQL Server triggers (TR_*_AUDIT → AuditDataChanges) capture who
    initiated the delete — same pattern Don's PHP used."""
    table  = row["table_name"]
    pk_col = row["pk_col"]
    pk_val = row["pk_val"]
    user   = row.get("source_user") or "outbox-drainer"

    audit_by, audit_at = _get_audit_cols(cur, table)

    if audit_by or audit_at:
        sets, vals = [], []
        if audit_by:
            sets.append(f"{audit_by} = %s")
            vals.append(user)
        if audit_at:
            sets.append(f"{audit_at} = GETDATE()")
        vals.append(pk_val)
        cur.execute(
            f"UPDATE {_qualified(table)} SET {', '.join(sets)} WHERE {pk_col} = %s",
            tuple(vals),
        )

    cur.execute(
        f"DELETE FROM {_qualified(table)} WHERE {pk_col} = %s",
        (pk_val,),
    )
    # If 0 rows deleted, the row was already gone — treat as success
    # (DELETE is idempotent). We don't raise here.


def _apply_insert(cur, row: Dict[str, Any]) -> None:
    """Phase 3: INSERT support. For v1 (Phase 2) we mark these as errors
    with a helpful message asking the admin to add the row in SSMS instead."""
    raise NotImplementedError(
        "INSERT sync not yet supported. To add this row to SQL Server, "
        "use SSMS or run a manual INSERT against the contacts DB, then "
        "re-export the SQLite mirror with contacts_export_from_sqlserver.py "
        f"to pick it up locally. Payload: {row.get('payload_json')!r}"
    )


def apply_row(cur, row: Dict[str, Any]) -> None:
    op = row["operation"]
    if op == "UPDATE":
        _apply_update(cur, row)
    elif op == "DELETE":
        _apply_delete(cur, row)
    elif op == "INSERT":
        _apply_insert(cur, row)
    else:
        raise ValueError(f"Unknown outbox operation: {op!r}")


# ── Email alerts ─────────────────────────────────────────────────────────────

def _send_alert(subject: str, body: str) -> None:
    """Best-effort email alert. Reuses the Gmail STARTTLS / port 587 pattern
    from sysco_ftp.py (port 465 gets reset by some network appliances)."""
    if not (SMTP_USER and SMTP_PASSWORD and ALERT_TO):
        logger.warning("Email alert NOT sent — SMTP_USER/SMTP_PASSWORD/ALERT_TO missing.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = ALERT_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.sendmail(SMTP_USER, [a.strip() for a in ALERT_TO.split(";") if a.strip()],
                          msg.as_string())
        logger.info("Email alert sent: %s", subject)
    except Exception as exc:
        logger.error("Email alert FAILED to send: %s", exc, exc_info=True)


# ── Main drain loop ──────────────────────────────────────────────────────────

def drain(batch_size: int, dry_run: bool) -> Dict[str, int]:
    """Drain pending outbox rows. Returns counters {applied, failed_terminal,
    failed_retry, skipped}."""
    counters = {"applied": 0, "failed_terminal": 0, "failed_retry": 0, "skipped": 0}
    errors: List[str] = []

    if not APP_BASE_URL:
        sys.exit("ERROR: APP_BASE_URL must be set.")
    if not (APP_ADMIN_TOKEN or (APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD)):
        sys.exit("ERROR: set APP_ADMIN_EMAIL + APP_ADMIN_PASSWORD (preferred) "
                 "or APP_ADMIN_TOKEN.")
    if not SQL_USER or not SQL_PASSWORD:
        sys.exit("ERROR: CONTACTS_SQL_USER and CONTACTS_SQL_PASSWORD must be set.")

    # Mint a fresh token up front when creds are available — don't even try
    # a possibly-stale static token.
    if APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD and not _login():
        sys.exit("ERROR: login to the app failed — check APP_ADMIN_EMAIL/PASSWORD.")

    logger.info("Claiming batch of up to %d pending outbox rows from %s", batch_size, APP_BASE_URL)
    rows = claim_batch(batch_size)
    if not rows:
        logger.info("Outbox empty — nothing to drain.")
        return counters

    logger.info("Got %d rows. SQL Server: %s as %s", len(rows), SQL_HOST, SQL_USER)
    if dry_run:
        logger.info("DRY-RUN MODE — no SQL writes will be made, no Cloudways updates posted.")

    sql_conn = None
    try:
        if not dry_run:
            sql_conn = _connect_sql()

        for row in rows:
            row_id   = row["id"]
            op       = row["operation"]
            table    = row["table_name"]
            pk_val   = row["pk_val"]
            attempts = row.get("attempts", 0)

            logger.info("Row %d: %s %s pk=%s (attempt %d/%d)",
                        row_id, op, table, pk_val, attempts, MAX_ATTEMPTS)

            if dry_run:
                # Print the planned SQL but don't execute. For UPDATE/DELETE
                # this is a quick consistency check; for INSERT it just notes
                # that we'll mark it as error.
                payload = json.loads(row.get("payload_json") or "{}")
                logger.info("  (would apply: %s on %s, payload=%s)", op, table, payload)
                counters["skipped"] += 1
                continue

            cur = sql_conn.cursor()
            try:
                apply_row(cur, row)
                sql_conn.commit()
                mark_applied(row_id, success=True)
                counters["applied"] += 1
                logger.info("  ✓ applied")
            except NotImplementedError as exc:
                # INSERT case — mark as terminal error every time (no retry).
                sql_conn.rollback()
                err = str(exc)
                mark_applied(row_id, success=False, error_message=err)
                counters["failed_terminal"] += 1
                errors.append(f"Row {row_id} {op} {table}: {err.splitlines()[0]}")
                logger.error("  ✗ %s", err.splitlines()[0])
            except Exception as exc:
                sql_conn.rollback()
                err = f"{type(exc).__name__}: {exc}"[:500]
                if attempts >= MAX_ATTEMPTS:
                    # Terminal failure — mark error, alert.
                    mark_applied(row_id, success=False, error_message=err)
                    counters["failed_terminal"] += 1
                    errors.append(f"Row {row_id} {op} {table}: {err}")
                    logger.error("  ✗ TERMINAL FAILURE after %d attempts: %s", attempts, err)
                else:
                    # Soft failure — leave pending so next cron picks it up.
                    counters["failed_retry"] += 1
                    logger.warning("  ⚠ failed (attempt %d/%d), will retry next run: %s",
                                   attempts, MAX_ATTEMPTS, err)
    finally:
        if sql_conn is not None:
            try:
                sql_conn.close()
            except Exception:
                pass

    # Summary
    logger.info("=" * 60)
    logger.info("Drain complete: applied=%d  failed_terminal=%d  failed_retry=%d  skipped=%d",
                counters["applied"], counters["failed_terminal"],
                counters["failed_retry"], counters["skipped"])
    logger.info("=" * 60)

    # Alert on any terminal failures.
    if counters["failed_terminal"] > 0 and not dry_run:
        body = (
            f"contacts_outbox_drainer drained {counters['applied']} row(s) successfully, "
            f"but {counters['failed_terminal']} row(s) failed terminally and need review.\n\n"
            f"Visit {APP_BASE_URL}/contacts-outbox.html to inspect and retry.\n\n"
            f"Failures:\n" + "\n".join(f"  - {e}" for e in errors[:20])
        )
        _send_alert(
            subject=f"[Newk's Contacts] {counters['failed_terminal']} outbox row(s) failed",
            body=body,
        )

    return counters


# ── Sync-from-SQL fulfillment ────────────────────────────────────────────────
# The Cloudways UI's "Sync from SQL" button queues a request the app itself
# can't fulfill (it can't reach SQL-PROD). We fulfill it here: export a fresh
# contacts.db with contacts_export_from_sqlserver.py and upload it to the
# app's /api/contacts/import-db, which preserves the outbox queue and swaps
# the mirror. Only runs when the outbox is FULLY drained, so a refresh can
# never clobber edits that haven't reached SQL Server yet.

EXPORT_TIMEOUT = 1800  # seconds — the full export takes a few minutes


def maybe_fulfill_sync_request(dry_run: bool) -> None:
    import subprocess
    import tempfile

    status = _api("GET", "/api/contacts/sync-request").json()
    pending_req = status.get("pending")
    if not pending_req:
        return
    logger.info("Sync-from-SQL request #%s pending (requested %s by %s)",
                pending_req.get("id"), pending_req.get("requested_at"),
                pending_req.get("requested_by"))

    stats = _api("GET", "/api/contacts/outbox/stats").json()
    if stats.get("pending"):
        logger.info("Deferring sync fulfillment — %s outbox row(s) still pending. "
                    "Will fulfill on a later run once the queue is empty.",
                    stats.get("pending"))
        return

    if dry_run:
        logger.info("DRY-RUN — would export SQL-PROD → contacts.db and upload now.")
        return

    out_path = os.path.join(tempfile.gettempdir(), f"contacts_sync_{os.getpid()}.db")
    export_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "contacts_export_from_sqlserver.py")
    env = dict(os.environ)
    # The exporter authenticates to SQL-PROD with CONTACTS_SOURCE_USER/PASSWORD
    # (defaulting to the read-only 'reports' account). If those aren't set in
    # the env, fall back to the drainer's own working SQL creds so it can
    # connect as the same account that just drained successfully.
    env.setdefault("CONTACTS_SOURCE_USER", SQL_USER or "")
    env.setdefault("CONTACTS_SOURCE_PASSWORD", SQL_PASSWORD or "")

    logger.info("Running exporter: %s --output %s", export_script, out_path)
    try:
        # NOTE: dev-jan02 runs Python 3.6 — subprocess.run's capture_output=
        # and text= kwargs are 3.7+. Use the 3.6-compatible equivalents
        # (stdout/stderr=PIPE + universal_newlines).
        proc = subprocess.run(
            [sys.executable, export_script, "--output", out_path],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=EXPORT_TIMEOUT,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"exporter exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '')[-500:]}")

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        logger.info("Export complete (%.1f MB). Uploading to %s ...", size_mb, APP_BASE_URL)
        with open(out_path, "rb") as fh:
            r = _api("POST", "/api/contacts/import-db",
                     files={"file": ("contacts.db", fh, "application/octet-stream")},
                     timeout=300)
        logger.info("Sync-from-SQL fulfilled: %s", r.json())
    except Exception:
        logger.error("Sync-from-SQL fulfillment FAILED:\n%s", traceback.format_exc())
        _send_alert(
            subject="[Newk's Contacts] Sync-from-SQL fulfillment failed",
            body=f"The drainer could not fulfill the pending sync request.\n\n"
                 f"{traceback.format_exc()}\n\n"
                 f"The request stays pending; the next cron run will retry.",
        )
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser(description="Drain Cloudways contacts outbox → SQL Server.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be applied without actually touching SQL Server.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                   help=f"Max rows to claim per run (default {DEFAULT_BATCH}).")
    p.add_argument("--skip-sync", action="store_true",
                   help="Skip Sync-from-SQL fulfillment (drain the outbox only).")
    args = p.parse_args()

    try:
        counters = drain(args.batch_size, args.dry_run)
    except SystemExit:
        raise
    except Exception:
        # Catch-all so cron always logs something useful instead of just exiting.
        logger.error("Drainer crashed with unhandled exception:\n%s", traceback.format_exc())
        _send_alert(
            subject="[Newk's Contacts] outbox drainer CRASHED",
            body=f"The drainer crashed before completing.\n\n{traceback.format_exc()}",
        )
        sys.exit(1)

    # Fulfill a queued "Sync from SQL" request, if any (and queue is empty).
    # Failures here alert + leave the request pending; they don't affect the
    # drain result above.
    if not args.skip_sync:
        try:
            maybe_fulfill_sync_request(args.dry_run)
        except Exception:
            logger.error("Sync-request check failed:\n%s", traceback.format_exc())

    # Non-zero exit if there were terminal failures, so cron's MAILTO catches it.
    if counters["failed_terminal"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
