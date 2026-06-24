"""
vendor_ftp.py — Pull vendor files via sFTP. Generic, config-driven.

Python replacement for the legacy WinSCP/PowerShell scripts on newks-sql:
  D:\\ftp_scripts\\get_plateiq_invoices.ps1   -> vendor "plateiq"
  D:\\ftp_scripts\\get_nproduce_bids.ps1      -> vendor "nproduce"
  D:\\ftp_scripts\\get_punchh_files.ps1       -> vendor "punchh"

Each original relied on WinSCP.NET, hardcoded credentials, a pinned SSH
fingerprint, and SMTP via dev-jan02. This rewrite mirrors sysco_ftp.py:

  - paramiko (pure-Python SFTP, no .NET / WinSCP).
  - Credentials from a .env file in the same directory as the script.
  - ~/.ssh/known_hosts for host-key trust (auto-add on first use).
  - Failure alerts via Gmail SMTP (same pattern as sysco_ftp.py).
  - Logs to /var/log/vendor_ftp/ with daily rotation (30-day retention).
  - Local destinations are the SAME Windows shares the originals wrote to
    (SMB-mounted on dev-jan02), so downstream consumers (CDP Service, the
    Punchh importer) keep working unchanged.

Each vendor is one entry in VENDORS below — adding another is one dict.
Per-vendor values are env-overridable (see .env keys in each entry); only
the USER and PASSWORD are required from .env (never committed to source).

USAGE:
    python vendor_ftp.py plateiq               # download + archive
    python vendor_ftp.py nproduce --dry-run    # connect + list, no download/move
    python vendor_ftp.py punchh --no-archive   # download but skip remote archive

DEPLOYMENT (dev-jan02): place alongside sysco_ftp.py (shares that .env's
GMAIL_* + SMB mounts). See the deploy notes in chat.
"""

import argparse
import fnmatch
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
from typing import List, Optional

import paramiko
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

LOG_DIR = Path(os.getenv("VENDOR_FTP_LOG_DIR", "/var/log/vendor_ftp"))

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_TO           = os.getenv("VENDOR_FTP_ALERT_TO",
                               os.getenv("SYSCO_ALERT_TO", "itdepartment@newks.com"))

# ──────────────────────────────────────────────────────────────────────────────
# Vendor catalog. Each value is overridable per-vendor via env (KEY shown):
#   {VENDOR}_SFTP_HOST / _SFTP_USER / _SFTP_PASSWORD   (USER + PASSWORD required)
#   {VENDOR}_REMOTE_INBOX / _REMOTE_ARCHIVE / _LOCAL_DEST
# masks: which remote filenames to grab. archive: "move" (keep name) or
# "datestamp" (append -YYYYmmdd_HHMMSS before the extension, like the nProduce
# original). port defaults to 22.
# ──────────────────────────────────────────────────────────────────────────────
VENDORS = {
    "plateiq": {
        "label":          "PlateIQ Invoices",
        "host":           "ediftp.plateiq.com",
        "remote_inbox":   "/",
        "remote_archive": "/processed/",
        "masks":          ["*.txt"],
        "local_dest":     "/mnt/newks-sql/CDP_Service/upload/",
        "archive":        "move",
    },
    "nproduce": {
        "label":          "nProduce Bid Files",
        "host":           "sftp.nproduce.com",
        "remote_inbox":   "/Bid Files",
        "remote_archive": "/Archive",
        "masks":          ["*.txt", "*.xls", "*.xlsx"],  # old RunType 1/2/3, in one pass
        "local_dest":     "/mnt/newks-sql/CDP_Service/upload/",
        "archive":        "datestamp",
    },
    "punchh": {
        "label":          "Punchh Files",
        "host":           "165.227.79.143",
        "remote_inbox":   "/public_html/ftp/punchh/",
        "remote_archive": "/public_html/ftp/punchh/Archive/",
        "masks":          ["*.csv"],
        # \\newks-sql\d$\import\Punchh\ — the D$ share. Confirm this mount on
        # dev-jan02 (or set PUNCHH_LOCAL_DEST in .env).
        "local_dest":     "/mnt/newks-sql-d/import/Punchh/",
        "archive":        "move",
    },
}


def cfg(vendor_key: str, spec: dict, field: str, env_suffix: str, default=None):
    """Per-vendor value with a {VENDOR}_{SUFFIX} env override."""
    env_val = os.getenv(f"{vendor_key.upper()}_{env_suffix}")
    if env_val:
        return env_val
    return spec.get(field, default)


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logger(vendor: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"vendor_ftp.{vendor}")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = TimedRotatingFileHandler(LOG_DIR / f"{vendor}.log", when="midnight", backupCount=30)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ──────────────────────────────────────────────────────────────────────────────
# Alert email (Gmail SMTP — same path as sysco_ftp.py)
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
# Generic sFTP fetch
# ──────────────────────────────────────────────────────────────────────────────

class VendorFetcher:
    def __init__(self, vendor: str, spec: dict, log: logging.Logger):
        self.vendor = vendor
        self.spec = spec
        self.log = log
        self.host = cfg(vendor, spec, "host", "SFTP_HOST")
        self.user = os.getenv(f"{vendor.upper()}_SFTP_USER", "")
        self.password = os.getenv(f"{vendor.upper()}_SFTP_PASSWORD", "")
        self.port = int(os.getenv(f"{vendor.upper()}_SFTP_PORT", "22"))
        self.remote_inbox   = cfg(vendor, spec, "remote_inbox", "REMOTE_INBOX")
        self.remote_archive = cfg(vendor, spec, "remote_archive", "REMOTE_ARCHIVE")
        self.local_dest = Path(cfg(vendor, spec, "local_dest", "LOCAL_DEST"))
        self.masks = spec["masks"]
        self.archive_mode = spec.get("archive", "move")
        self.ssh: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None

    def __enter__(self) -> "VendorFetcher":
        if not (self.host and self.user and self.password):
            raise RuntimeError(
                f"{self.vendor} SFTP credentials missing — set "
                f"{self.vendor.upper()}_SFTP_USER and {self.vendor.upper()}_SFTP_PASSWORD "
                f"in .env (host defaults to {self.host or '<unset>'})."
            )
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # TOFU like sysco_ftp.py
        self.log.info("Connecting to %s:%s as %s", self.host, self.port, self.user)
        self.ssh.connect(hostname=self.host, port=self.port,
                         username=self.user, password=self.password,
                         timeout=30, banner_timeout=30, auth_timeout=30)
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

    def list_matching(self) -> List[str]:
        assert self.sftp is not None
        entries = self.sftp.listdir(self.remote_inbox)
        matches = sorted(f for f in entries
                         if any(fnmatch.fnmatch(f.lower(), m.lower()) for m in self.masks))
        self.log.info("Listing %s: %d matching (%s) of %d entries",
                      self.remote_inbox, len(matches), ",".join(self.masks), len(entries))
        return matches

    def download(self, name: str) -> Path:
        assert self.sftp is not None
        self.local_dest.mkdir(parents=True, exist_ok=True)
        remote_path = posixpath.join(self.remote_inbox, name)
        local_path = self.local_dest / name
        self.sftp.get(remote_path, str(local_path))
        self.log.info("Downloaded %s -> %s", name, local_path)
        return local_path

    def archive(self, name: str) -> bool:
        assert self.sftp is not None
        src = posixpath.join(self.remote_inbox, name)
        if self.archive_mode == "datestamp":
            stem, dot, ext = name.rpartition(".")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_name = f"{stem}-{stamp}.{ext}" if dot else f"{name}-{stamp}"
            dst = posixpath.join(self.remote_archive, new_name)
        else:
            dst = posixpath.join(self.remote_archive, name)
        try:
            self.sftp.rename(src, dst)
            self.log.info("Archived on remote: %s -> %s", name, dst)
            return True
        except IOError as exc:
            self.log.warning("Could not archive %s: %s", name, exc)
            return False


def run_vendor(vendor: str, args, log: logging.Logger) -> int:
    spec = VENDORS[vendor]
    downloaded: List[str] = []
    with VendorFetcher(vendor, spec, log) as f:
        files = f.list_matching()
        if not files:
            log.info("**** There are no %s files to download. ****", spec["label"])
            return 0
        for name in files:
            if args.dry_run:
                log.info("[dry-run] would download %s", name)
                continue
            try:
                f.download(name)
                downloaded.append(name)
            except Exception as exc:
                log.error("Failed to download %s: %s", name, exc)
        if not args.no_archive and not args.dry_run:
            for name in downloaded:
                f.archive(name)
    log.info("%s run complete: %d downloaded%s",
             spec["label"], len(downloaded), " (dry-run)" if args.dry_run else "")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vendor", choices=sorted(VENDORS), help="Which vendor to pull.")
    p.add_argument("--dry-run", action="store_true",
                   help="Connect + list remote files; do not download or move.")
    p.add_argument("--no-archive", action="store_true",
                   help="Download but do NOT archive on the remote (debug only).")
    args = p.parse_args()

    log = setup_logger(args.vendor)
    log.info("=" * 70)
    log.info("vendor_ftp.py vendor=%s on=%s%s",
             args.vendor, socket.gethostname(), " [dry-run]" if args.dry_run else "")
    log.info("=" * 70)
    try:
        return run_vendor(args.vendor, args, log)
    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        send_alert(
            subject=f"[{VENDORS[args.vendor]['label']}] FAILED on {socket.gethostname()}",
            body=(f"{VENDORS[args.vendor]['label']} pull failed at "
                  f"{datetime.now().isoformat()}.\n\nError: {exc}\n\n"
                  f"Most recent log: {LOG_DIR}/{args.vendor}.log\n"),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
