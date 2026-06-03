"""
sysco_ftp.py — Pull Sysco Invoices and POs via sFTP.

Python replacement for the legacy PowerShell scripts on newks-sql:
  D:\\ftp_scripts\\get_Sysco_Invoices.ps1
  D:\\ftp_scripts\\get_Sysco_POs.ps1

Both originals relied on WinSCP.NET, hardcoded credentials, a pinned SSH
fingerprint, and SMTP via dev-jan02 — any one of which can silently break.
This rewrite:

  - Uses paramiko (pure-Python SFTP, no .NET / WinSCP dependency).
  - Reads credentials from a .env file in the same directory as the script.
  - Uses ~/.ssh/known_hosts for host-key trust (auto-add on first use).
  - Sends failure alerts via Gmail SMTP (same pattern as email_service.py),
    decoupling alerts from dev-jan02's mail config.
  - Logs to /var/log/sysco_ftp/ with daily rotation (30-day retention).
  - Runs on dev-jan02 with the newks-sql destination shares SMB-mounted
    at /mnt/newks-sql/ — destinations are unchanged from the originals, so
    downstream consumers (CDP Service, etc.) keep working as-is.

USAGE:
    python sysco_ftp.py invoices              # 009*.txt
    python sysco_ftp.py pos                   # 011*.txt
    python sysco_ftp.py invoices --dry-run    # connect + list, no download/move
    python sysco_ftp.py pos --no-archive      # download but skip remote archive

DEPLOYMENT (dev-jan02):
    See sysco_ftp_deploy.md in this directory.
"""

import argparse
import logging
import os
import re
import shutil
import smtplib
import socket
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Optional, Pattern

import paramiko
from dotenv import load_dotenv

# Load .env from this script's directory. On dev-jan02 this is alongside the
# checked-out .py file. Doesn't override real shell env vars.
load_dotenv(Path(__file__).parent / ".env")

# ──────────────────────────────────────────────────────────────────────────────
# Config (env-driven; sensible defaults match the legacy PowerShell paths)
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path(os.getenv("SYSCO_LOG_DIR", "/var/log/sysco_ftp"))

SYSCO_HOST = os.getenv("SYSCO_SFTP_HOST", "sb2b.sysco.com")
SYSCO_USER = os.getenv("SYSCO_SFTP_USER", "")
SYSCO_PASS = os.getenv("SYSCO_SFTP_PASSWORD", "")

REMOTE_INBOX   = os.getenv("SYSCO_REMOTE_INBOX",   "/Outbound/")
REMOTE_ARCHIVE = os.getenv("SYSCO_REMOTE_ARCHIVE", "/Outbound/Archive/")

# Local destinations. Defaults map to where the legacy PowerShell wrote — once
# the SMB mount is in place on dev-jan02 these resolve to the same Windows
# shares the CDP Service already watches, so no downstream change needed.
INV_TEMP      = Path(os.getenv("SYSCO_INVOICES_TEMP",
                               "/var/sysco/invoices_staging"))
INV_DEST      = Path(os.getenv("SYSCO_INVOICES_DEST",
                               "/mnt/newks-sql/CDP_Service/upload/"))
INV_NOT_ON_CT = Path(os.getenv("SYSCO_INVOICES_NOT_ON_CT_DEST",
                               "/mnt/newks-sql/cdp_temp/Sysco/NotOnCT/"))
POS_DEST      = Path(os.getenv("SYSCO_POS_DEST",
                               "/mnt/newks-sql/cdp_service/Sysco_PO_Archive/"))

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_TO           = os.getenv("SYSCO_ALERT_TO", "kaylesworth@newks.com")

# Stores NOT on CrunchTime — invoices for these go to a separate folder so
# CDP Service doesn't try to import them. Preserved verbatim from the
# original PowerShell $filterLists. If a store starts/stops using CT,
# update this set (or move it to a config table later).
NOT_ON_CT_STORES = frozenset({
    "224953", "224954", "224957",
    "715534", "715537", "715539", "715545", "715547",
    "794103", "794104", "794107", "794108", "794111",
    "857516", "857517",
    "870229", "870230", "870231", "870233", "870234", "870235", "870237", "870239",
    "911515",
    "946777", "946787", "946789",
})

# Filename → store-ID parser. Matches the original PowerShell logic:
#     $file.BaseName.Substring(10, 6)
# That's 6 chars at index 10..15 of the BaseName, which for an invoice like
# "009XXXXXXSSSSSS.txt" gives the 6-digit Sysco store number. The same offset
# applies to both Invoice (009) and PO (011) filenames; we only filter by it
# on invoices.
STORE_ID_AT_OFFSET_RE = re.compile(r"^.{10}(\d{6})", re.IGNORECASE)

PO_FILENAME_RE  = re.compile(r"^011.*\.txt$", re.IGNORECASE)
INV_FILENAME_RE = re.compile(r"^009.*\.txt$", re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logger(mode: str) -> logging.Logger:
    """Per-mode logger with daily rotation (30-day retention) + stdout."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger(f"sysco_ftp.{mode}")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = TimedRotatingFileHandler(LOG_DIR / f"sysco_{mode}.log",
                                  when="midnight", backupCount=30)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ──────────────────────────────────────────────────────────────────────────────
# Alert email (Gmail SMTP — independent of dev-jan02's mail relay)
# ──────────────────────────────────────────────────────────────────────────────

def send_alert(subject: str, body: str) -> bool:
    """Best-effort: never raises; logs to stdout if it can't send.

    Uses Gmail SMTP on port 587 with STARTTLS instead of 465 SMTPS.
    Confirmed working from dev-jan02 → Gmail; port 465 hits 'Connection
    reset by peer' from this network path (some appliance between
    dev-jan02 and Gmail does SMTP inspection on 465).
    """
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_TO):
        print(f"WARNING: alert email skipped (Gmail/recipient not configured): {subject}")
        return False
    try:
        import ssl
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Newk's Reports <{GMAIL_ADDRESS}>"
        msg["To"] = ALERT_TO
        msg.attach(MIMEText(body, "plain"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [ALERT_TO], msg.as_string())
        return True
    except Exception as exc:
        print(f"WARNING: failed to send alert email: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Sysco sFTP session
# ──────────────────────────────────────────────────────────────────────────────

class SyscoFetcher:
    """Context manager wrapping paramiko SFTPClient with logging."""

    def __init__(self, log: logging.Logger):
        self.log = log
        self.ssh: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None

    def __enter__(self) -> "SyscoFetcher":
        if not (SYSCO_HOST and SYSCO_USER and SYSCO_PASS):
            raise RuntimeError(
                "Sysco SFTP credentials missing — check .env for "
                "SYSCO_SFTP_HOST, SYSCO_SFTP_USER, SYSCO_SFTP_PASSWORD."
            )

        self.ssh = paramiko.SSHClient()
        # The legacy script pinned the host key fingerprint. We use
        # known_hosts via TOFU instead — easier to recover when Sysco
        # rotates keys. ~/.ssh/known_hosts on dev-jan02 caches it after
        # the first successful run.
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        self.log.info("Connecting to %s as %s", SYSCO_HOST, SYSCO_USER)
        self.ssh.connect(
            hostname=SYSCO_HOST, port=22,
            username=SYSCO_USER, password=SYSCO_PASS,
            timeout=30, banner_timeout=30, auth_timeout=30,
        )
        self.sftp = self.ssh.open_sftp()
        self.log.info("Connected.")
        return self

    def __exit__(self, *args):
        for closer in (self.sftp, self.ssh):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass

    def list_files(self, pattern_regex: Pattern) -> List[str]:
        """Return filenames in REMOTE_INBOX whose name matches pattern_regex."""
        assert self.sftp is not None
        entries = self.sftp.listdir(REMOTE_INBOX)
        matches = sorted(f for f in entries if pattern_regex.match(f))
        self.log.info(
            "Listing %s: %d matching of %d total entries",
            REMOTE_INBOX, len(matches), len(entries),
        )
        return matches

    def download(self, remote_filename: str, local_dir: Path) -> Path:
        assert self.sftp is not None
        local_dir.mkdir(parents=True, exist_ok=True)
        remote_path = REMOTE_INBOX + remote_filename
        local_path = local_dir / remote_filename
        self.sftp.get(remote_path, str(local_path))
        self.log.info("Downloaded %s → %s", remote_filename, local_path)
        return local_path

    def archive_remote(self, remote_filename: str) -> bool:
        """Move file from REMOTE_INBOX to REMOTE_ARCHIVE. Best-effort."""
        assert self.sftp is not None
        src = REMOTE_INBOX + remote_filename
        dst = REMOTE_ARCHIVE + remote_filename
        try:
            self.sftp.rename(src, dst)
            self.log.info("Archived on remote: %s", remote_filename)
            return True
        except IOError as exc:
            self.log.warning("Could not archive %s on remote: %s", remote_filename, exc)
            return False


# ──────────────────────────────────────────────────────────────────────────────
# Mode implementations
# ──────────────────────────────────────────────────────────────────────────────

def parse_store_id(filename: str) -> Optional[str]:
    """Extract the 6-digit Sysco store ID from a Sysco-format filename."""
    m = STORE_ID_AT_OFFSET_RE.match(filename)
    return m.group(1) if m else None


def run_pos(args, log: logging.Logger) -> int:
    """Download 011*.txt PO files to POS_DEST and archive on remote."""
    POS_DEST.mkdir(parents=True, exist_ok=True)
    downloaded: List[str] = []
    with SyscoFetcher(log) as f:
        files = f.list_files(PO_FILENAME_RE)
        if not files:
            log.info("**** There are no POs to download. ****")
            return 0
        for name in files:
            if args.dry_run:
                log.info("[dry-run] would download %s", name)
                continue
            try:
                f.download(name, POS_DEST)
                downloaded.append(name)
            except Exception as exc:
                log.error("Failed to download %s: %s", name, exc)
        if not args.no_archive and not args.dry_run:
            for name in downloaded:
                f.archive_remote(name)
    log.info("PO run complete: %d downloaded", len(downloaded))
    return 0


def run_invoices(args, log: logging.Logger) -> int:
    """Download 009*.txt invoice files, split CT vs not-on-CT, route to dests."""
    for d in (INV_TEMP, INV_DEST, INV_NOT_ON_CT):
        d.mkdir(parents=True, exist_ok=True)

    downloaded: List[Path] = []
    with SyscoFetcher(log) as f:
        files = f.list_files(INV_FILENAME_RE)
        if not files:
            log.info("**** There are no invoices to download. ****")
            return 0
        for name in files:
            if args.dry_run:
                log.info("[dry-run] would download %s", name)
                continue
            try:
                local = f.download(name, INV_TEMP)
                downloaded.append(local)
            except Exception as exc:
                log.error("Failed to download %s: %s", name, exc)
        if not args.no_archive and not args.dry_run:
            for local in downloaded:
                f.archive_remote(local.name)

    if args.dry_run:
        return 0

    # Split + route. Local-only file moves; no network.
    to_cdp = 0
    to_notct = 0
    for local in downloaded:
        store_id = parse_store_id(local.name)
        if store_id and store_id in NOT_ON_CT_STORES:
            dst = INV_NOT_ON_CT / local.name
            shutil.move(str(local), str(dst))
            log.info("Filtering (not on CT, store %s): %s → %s",
                     store_id, local.name, dst)
            to_notct += 1
        else:
            dst = INV_DEST / local.name
            shutil.move(str(local), str(dst))
            log.info("Sending to CDP: %s → %s", local.name, dst)
            to_cdp += 1

    log.info("Invoice run complete: %d downloaded · %d → CDP · %d → NotOnCT",
             len(downloaded), to_cdp, to_notct)
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("mode", choices=("invoices", "pos"),
                   help="Which Sysco file type to pull.")
    p.add_argument("--dry-run", action="store_true",
                   help="Connect + list remote files; do not download or move.")
    p.add_argument("--no-archive", action="store_true",
                   help="Download but do NOT archive on the remote (debug only).")
    args = p.parse_args()

    log = setup_logger(args.mode)
    log.info("=" * 70)
    log.info("sysco_ftp.py mode=%s host=%s user=%s on=%s",
             args.mode, SYSCO_HOST, SYSCO_USER, socket.gethostname())
    log.info("=" * 70)

    try:
        if args.mode == "invoices":
            rc = run_invoices(args, log)
        else:
            rc = run_pos(args, log)
    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        send_alert(
            subject=f"[Sysco {args.mode}] FAILED on {socket.gethostname()}",
            body=(
                f"Sysco {args.mode} pull failed at {datetime.now().isoformat()}.\n\n"
                f"Error: {exc}\n\n"
                f"Most recent log: {LOG_DIR}/sysco_{args.mode}.log\n"
            ),
        )
        return 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
