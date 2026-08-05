r"""
esite_push.py — Push eSite Analytics CSV exports via sFTP (OUTBOUND).

Python replacement for the legacy WinSCP/PowerShell uploader on newks-sql:
  D:\\ftp_scripts\\send_eSite_files.ps1

Part of the newks-sql decommission. The FIVE eSite CSVs (Stores, Sales,
Transactions, OSAT, Customers) are produced WEEKLY (Mon) by data-driven SSRS
subscriptions on SQL-PROD (/TechSupport/Vendor Exports/eSite export - *). Those
subscriptions were repointed from \\newks-sql\d$\sql_output\eSite to
\\sql-prod\d$\sql_output\eSite so the data drop survives newks-sql going away.

This script runs on dev-jan02 (the integration box), reads that folder over the
SMB mount, and pushes each CSV to eSite Analytics — the last newks-sql tie for
this feed. It mirrors vendor_ftp.py / sysco_ftp.py, but INVERTED: the source is
LOCAL and the destination is REMOTE, and archiving happens on the LOCAL side
(move the file out of the drop folder after a successful upload, so it is sent
exactly once — the same thing the old .ps1 did with Move-Item).

  - paramiko (pure-Python SFTP, no .NET / WinSCP).
  - Credentials from a .env file in the same directory (never committed).
  - ~/.ssh/known_hosts for host-key trust (auto-add on first use, TOFU).
  - Failure alerts via Gmail SMTP (same pattern as vendor_ftp.py).
  - Logs to /var/log/esite_push/ with daily rotation (30-day retention).

USAGE:
    python esite_push.py                # upload every *.csv, then archive
    python esite_push.py --dry-run      # connect + list what WOULD be sent
    python esite_push.py --no-archive   # upload but leave files in place (debug)

DEPLOYMENT (dev-jan02): place alongside vendor_ftp.py (shares that .env's
GMAIL_*). Requires \\sql-prod\d$ mounted (default source
/mnt/sql-prod-d/sql_output/eSite — override with ESITE_LOCAL_SRC). See the
deploy notes in chat / backend/.env.example.
"""

import argparse
import logging
import os
import posixpath
import smtplib
import socket
import ssl
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List

import paramiko
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

LOG_DIR = Path(os.getenv("ESITE_LOG_DIR", "/var/log/esite_push"))

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_TO           = os.getenv("ESITE_ALERT_TO",
                               os.getenv("VENDOR_FTP_ALERT_TO", "itdepartment@newks.com"))

# ──────────────────────────────────────────────────────────────────────────────
# Config (all env-overridable). The three non-secret defaults come straight from
# the old send_eSite_files.ps1 WinSCP session; only the PASSWORD is required from
# .env. LABEL is used in log/alert subject lines.
# ──────────────────────────────────────────────────────────────────────────────
LABEL         = "eSite Data Transfer"
HOST          = os.getenv("ESITE_SFTP_HOST", "ftp.esiteanalytics.com")
PORT          = int(os.getenv("ESITE_SFTP_PORT", "1122"))
USER          = os.getenv("ESITE_SFTP_USER", "newks")
PASSWORD      = os.getenv("ESITE_SFTP_PASSWORD", "")
REMOTE_DIR    = os.getenv("ESITE_REMOTE_DIR", "/")           # PutFiles(..., "/")
# \\sql-prod\d$\sql_output\eSite over the SMB mount. The SSRS subscriptions were
# repointed here from the newks-sql share (2026-08). Override if the mount differs.
LOCAL_SRC     = Path(os.getenv("ESITE_LOCAL_SRC", "/mnt/sql-prod-d/sql_output/eSite"))
# Local archive: files move here after a successful upload so each is sent once
# (matches the .ps1 Move-Item to ...\eSite\archive\). Default = <src>/archive.
LOCAL_ARCHIVE = Path(os.getenv("ESITE_LOCAL_ARCHIVE", str(LOCAL_SRC / "archive")))
MASK          = "*.csv"


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("esite_push")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = TimedRotatingFileHandler(LOG_DIR / "esite_push.log", when="midnight", backupCount=30)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ──────────────────────────────────────────────────────────────────────────────
# Alert email (Gmail SMTP — same path as vendor_ftp.py)
# ──────────────────────────────────────────────────────────────────────────────

def send_alert(subject: str, body: str) -> bool:
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_TO):
        print(f"WARNING: alert email skipped (Gmail/recipient not configured): {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Newk's Reports <{GMAIL_ADDRESS}>"
        msg["To"] = ALERT_TO
        msg.attach(MIMEText(body, "plain"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo(); server.starttls(context=ctx); server.ehlo()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [ALERT_TO], msg.as_string())
        return True
    except Exception as exc:
        print(f"WARNING: failed to send alert email: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Outbound sFTP push
# ──────────────────────────────────────────────────────────────────────────────

class EsitePusher:
    def __init__(self, log: logging.Logger):
        self.log = log
        self.ssh = None
        self.sftp = None

    def __enter__(self) -> "EsitePusher":
        if not (HOST and USER and PASSWORD):
            raise RuntimeError(
                "eSite SFTP credentials missing — set ESITE_SFTP_PASSWORD "
                "(and optionally ESITE_SFTP_USER/HOST) in .env."
            )
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # TOFU like vendor_ftp.py
        self.log.info("Connecting to %s:%s as %s", HOST, PORT, USER)
        self.ssh.connect(hostname=HOST, port=PORT, username=USER, password=PASSWORD,
                         timeout=30, banner_timeout=30, auth_timeout=30)
        self.sftp = self.ssh.open_sftp()
        self.log.info("Connected to %s", HOST)
        return self

    def __exit__(self, *args):
        for closer in (self.sftp, self.ssh):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass

    def upload(self, local_path: Path) -> None:
        assert self.sftp is not None
        remote_path = posixpath.join(REMOTE_DIR, local_path.name)
        self.sftp.put(str(local_path), remote_path)
        self.log.info("Uploaded %s -> %s:%s", local_path.name, HOST, remote_path)


def local_files() -> List[Path]:
    """Every *.csv sitting in the drop folder (never recurse into archive/)."""
    if not LOCAL_SRC.is_dir():
        raise RuntimeError(
            f"Source folder {LOCAL_SRC} not found — is \\\\sql-prod\\d$ mounted? "
            f"(override with ESITE_LOCAL_SRC in .env)"
        )
    return sorted(p for p in LOCAL_SRC.glob(MASK) if p.is_file())


def archive_local(path: Path, log: logging.Logger) -> None:
    """Move a sent file to the local archive, name--YYYYmmdd_HHMMSS.ext (as the .ps1 did)."""
    LOCAL_ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = LOCAL_ARCHIVE / f"{path.stem}--{stamp}{path.suffix}"
    path.replace(dst)
    log.info("Archived locally: %s -> %s", path.name, dst)


def run(args, log: logging.Logger) -> int:
    files = local_files()
    if not files:
        log.info("**** There are no %s files to upload. ****", LABEL)
        return 0
    log.info("%d file(s) to send: %s", len(files), ", ".join(p.name for p in files))

    sent: List[Path] = []
    with EsitePusher(log) as pusher:
        for path in files:
            if args.dry_run:
                log.info("[dry-run] would upload %s (%d bytes)", path.name, path.stat().st_size)
                continue
            try:
                pusher.upload(path)
                sent.append(path)
            except Exception as exc:
                log.error("Upload of %s failed: %s", path.name, exc)

    if not args.no_archive and not args.dry_run:
        for path in sent:
            try:
                archive_local(path, log)
            except Exception as exc:
                log.warning("Could not archive %s: %s", path.name, exc)

    log.info("%s run complete: %d uploaded%s",
             LABEL, len(sent), " (dry-run)" if args.dry_run else "")
    # If some files were present but none uploaded (and not a dry run), that's a failure.
    if files and not sent and not args.dry_run:
        raise RuntimeError(f"{len(files)} file(s) present but none uploaded successfully.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Connect + list what would be sent; do not upload or move.")
    p.add_argument("--no-archive", action="store_true",
                   help="Upload but do NOT move files to the local archive (debug only).")
    args = p.parse_args()

    log = setup_logger()
    log.info("=" * 70)
    log.info("esite_push.py on=%s src=%s%s",
             socket.gethostname(), LOCAL_SRC, " [dry-run]" if args.dry_run else "")
    log.info("=" * 70)
    try:
        return run(args, log)
    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        send_alert(
            subject=f"[{LABEL}] FAILED on {socket.gethostname()}",
            body=(f"{LABEL} upload failed at {datetime.now().isoformat()}.\n\n"
                  f"Error: {exc}\n\nSource: {LOCAL_SRC}\n"
                  f"Most recent log: {LOG_DIR}/esite_push.log\n"),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
