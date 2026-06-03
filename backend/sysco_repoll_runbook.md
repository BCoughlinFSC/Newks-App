# How to Trigger a Sysco Pull

**When to use this:** Today's invoices or POs didn't land in
`\\newks-sql\c$\cdp_service\upload\` or `Sysco_PO_Archive\`, or someone
asks "did we get a file from Sysco for X?" and you want to force-fetch
outside the cron schedule.

Open PowerShell. SSH in (will prompt for password — get from KeePass
entry **"2025 - Post - Linux Dev Root"**):

```
ssh root@dev-jan02
```

Change to the Sysco script directory:

```
cd /opt/sysco
```

## See what Sysco has waiting (no download)

```
python3 sysco_ftp.py invoices --dry-run
python3 sysco_ftp.py pos --dry-run
```

## Pull and process

```
python3 sysco_ftp.py invoices
python3 sysco_ftp.py pos
```

Runtime: usually under 1 minute each. When you see
`Invoice run complete: N downloaded · N → CDP · 0 → NotOnCT`
(or `**** There are no invoices to download. ****`), it's done.

## Verify files landed

```
ls -la /mnt/newks-sql/cdp_service/upload/ | tail
ls -la /mnt/newks-sql/cdp_service/Sysco_PO_Archive/ | tail
```

## If something looks off

```
tail -50 /var/log/sysco_ftp/sysco_invoices.log
tail -50 /var/log/sysco_ftp/sysco_pos.log
```

## Notes

- These scripts are also scheduled via cron at **07:45 / 08:00 / 13:00** daily —
  manual runs are usually not needed.
- The scripts pull whatever Sysco has in `/Outbound/` *right now*. There's no
  "pull files for a specific date" option — Sysco only keeps files until we
  archive them, so if you missed a day, contact Sysco support to re-drop the
  files in `/Outbound/`.
- Safe to run multiple times in a row; once a file is downloaded and archived
  on Sysco's side, it won't re-appear.
- Safe to run alongside other scripts on dev-jan02 (`nightly_pull.py`,
  `nightly_labor_pull.py`, etc.) — different endpoints, no shared resources.
- Code lives at `/opt/sysco/sysco_ftp.py` on dev-jan02; canonical copy is in
  this repo at `backend/sysco_ftp.py`. Re-deploy via SCP after pulling repo
  changes locally.
- Old PowerShell scripts (`D:\ftp_scripts\get_Sysco_*.ps1` on newks-sql) are
  **disabled** as of June 2026. Don't re-enable — they have a hardcoded
  WinSCP fingerprint that's stale and were causing silent failures.
