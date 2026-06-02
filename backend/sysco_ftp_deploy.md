# `sysco_ftp.py` — deploy to dev-jan02

Python replacement for `get_Sysco_Invoices.ps1` and `get_Sysco_POs.ps1`
that used to run on newks-sql.

## What this does end-to-end

```
[Sysco sb2b]  --sFTP-->  [dev-jan02 cron @ 07:45 / 08:00 / 13:00]
                              |
                              | paramiko, .env credentials
                              v
                       /var/sysco/invoices_staging   (or pos_staging)
                              |
                              | for invoices: filter 28 non-CT stores
                              | for POs: direct
                              v
                       /mnt/newks-sql/CDP_Service/upload/        ← invoices CT
                       /mnt/newks-sql/cdp_temp/Sysco/NotOnCT/    ← invoices non-CT
                       /mnt/newks-sql/cdp_service/Sysco_PO_Archive/  ← POs

                       (SMB-mounted shares on dev-jan02 → newks-sql)
```

CDP Service and the other downstream consumers on newks-sql keep seeing
files in the exact same Windows folders they were watching before — they
don't know dev-jan02 is the one putting them there now.

## One-time setup on dev-jan02

### 1. Install dependencies

```bash
ssh root@dev-jan02.nfc.local

# OS packages
dnf install -y python3 python3-pip cifs-utils

# Python packages (system-wide is fine; or use a venv if you prefer)
pip3 install paramiko python-dotenv
```

### 2. Pull the code

The script lives in the same `toast_net_sales_app` repo as your other
backend code. Clone it (or `git pull` if already cloned):

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/aylesworthkim/Newks-App.git toast_net_sales_app
# Or, if already cloned:
# cd /opt/toast_net_sales_app && git pull
```

The script is at `/opt/toast_net_sales_app/backend/sysco_ftp.py`.

### 3. SMB-mount the newks-sql shares

Create a credentials file (root-only readable):

```bash
cat > /root/.smb_newks_sql <<'EOF'
username=kaylesworth
password=YOUR_NEWKS_AD_PASSWORD_HERE
domain=NEWKS
EOF
chmod 600 /root/.smb_newks_sql
```

Add the mount to `/etc/fstab` so it persists across reboots:

```bash
mkdir -p /mnt/newks-sql

cat >> /etc/fstab <<'EOF'
//newks-sql/c$ /mnt/newks-sql cifs credentials=/root/.smb_newks_sql,iocharset=utf8,uid=root,gid=root,file_mode=0664,dir_mode=0775,_netdev,nofail 0 0
EOF

mount -a
ls /mnt/newks-sql/    # should show CDP_Service, CDP_Temp, cdp_service, etc.
```

If you see "Permission denied" — your AD account doesn't have admin on
newks-sql's `c$` share. Use an account that does (Don's old `dwilson`
account if it still works, or have IT grant your account access).

### 4. Configure `.env`

The script reads `/opt/toast_net_sales_app/backend/.env`. Create it:

```bash
cat > /opt/toast_net_sales_app/backend/.env <<'EOF'
# Sysco SFTP
SYSCO_SFTP_HOST=sb2b.sysco.com
SYSCO_SFTP_USER=NEWKSEAT
SYSCO_SFTP_PASSWORD=GET_THIS_FROM_KEEPASS    # rotated password if any

# Sysco remote paths (defaults match the legacy script — change only if Sysco moves them)
SYSCO_REMOTE_INBOX=/Outbound/
SYSCO_REMOTE_ARCHIVE=/Outbound/Archive/

# Local destinations (these are the SMB-mounted Windows shares)
SYSCO_INVOICES_TEMP=/var/sysco/invoices_staging
SYSCO_INVOICES_DEST=/mnt/newks-sql/CDP_Service/upload/
SYSCO_INVOICES_NOT_ON_CT_DEST=/mnt/newks-sql/cdp_temp/Sysco/NotOnCT/
SYSCO_POS_DEST=/mnt/newks-sql/cdp_service/Sysco_PO_Archive/

# Logs (rotates daily, 30-day retention)
SYSCO_LOG_DIR=/var/log/sysco_ftp

# Alert email (Gmail SMTP — independent of dev-jan02's mail relay)
GMAIL_ADDRESS=reports@newks.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SYSCO_ALERT_TO=kaylesworth@newks.com
EOF
chmod 600 /opt/toast_net_sales_app/backend/.env
```

### 5. Smoke-test (dry-run first — no downloads, no moves)

```bash
cd /opt/toast_net_sales_app/backend

# Lists what's on the Sysco server without downloading anything
python3 sysco_ftp.py invoices --dry-run
python3 sysco_ftp.py pos --dry-run
```

Expected output (good case):
```
... [INFO] Connecting to sb2b.sysco.com as NEWKSEAT
... [INFO] Connected.
... [INFO] Listing /Outbound/: 4 matching of 12 total entries
... [INFO] [dry-run] would download 009XXXXXXX224953.txt
```

Common failure modes:
- `Authentication failed` → password is wrong (or got rotated). Check KeePass.
- `Bad host key` → Sysco rotated their host key. Run once manually so paramiko's `AutoAddPolicy` updates `~/.ssh/known_hosts`.
- `No such file` on `/Outbound/` → Sysco moved the directory. Update `SYSCO_REMOTE_INBOX`.

### 6. Real run

```bash
python3 sysco_ftp.py invoices
python3 sysco_ftp.py pos
```

Verify a few things:
- Files appeared at the destination folders on newks-sql (check from RDP).
- Files moved to `/Outbound/Archive/` on Sysco (re-run `--dry-run` and confirm the list is now empty or smaller).
- Log file written: `tail /var/log/sysco_ftp/sysco_invoices.log`.

### 7. Schedule via cron

```bash
cat > /etc/cron.d/sysco-ftp <<'EOF'
# Sysco sFTP pulls — replaces newks-sql PowerShell scripts.
# Times match the original schedule (07:45 / 08:00 / 13:00 daily).
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""

45 7  * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py invoices >> /var/log/sysco_ftp/cron.log 2>&1
0  8  * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py invoices >> /var/log/sysco_ftp/cron.log 2>&1
0  13 * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py invoices >> /var/log/sysco_ftp/cron.log 2>&1

45 7  * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py pos      >> /var/log/sysco_ftp/cron.log 2>&1
0  8  * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py pos      >> /var/log/sysco_ftp/cron.log 2>&1
0  13 * * * root cd /opt/toast_net_sales_app/backend && /usr/bin/python3 sysco_ftp.py pos      >> /var/log/sysco_ftp/cron.log 2>&1
EOF

chmod 644 /etc/cron.d/sysco-ftp
systemctl restart crond    # reload
```

### 8. Decommission the old PowerShell scripts

Once the Python version has run cleanly for a few days:

```powershell
# On newks-sql (RDP), disable the Task Scheduler tasks first (don't delete yet —
# we want to be able to roll back fast if anything blows up).
Get-ScheduledTask | Where-Object {$_.TaskName -like "*Sysco*"} | Disable-ScheduledTask
```

Leave the `.ps1` files in `D:\ftp_scripts\` for a few weeks as backup, then
delete (or move to `D:\ftp_scripts\archive\`).

## Operational notes

### Where to look when it breaks

- **Per-run log**: `/var/log/sysco_ftp/sysco_{invoices,pos}.log` (rotates daily, 30-day retention)
- **Cron output**: `/var/log/sysco_ftp/cron.log`
- **Email alert**: lands at `kaylesworth@newks.com` on any exception. Subject: `[Sysco invoices] FAILED on dev-jan02`.

### Maintaining the non-CT store list

The 28 stores in `NOT_ON_CT_STORES` are hardcoded in `sysco_ftp.py`
(matching the original PowerShell). When a store moves on/off CrunchTime,
edit that frozenset and re-deploy.

Long-term: replace with a SQL lookup against `contacts.dbo.Store` or a
config table — but right now there's no convenient source-of-truth for
"is this store on CT" that's already maintained. Tech debt for later.

### Why this lives in toast_net_sales_app

It reuses the same `.env` pattern, Gmail SMTP approach, and Python version
the existing backend uses. Keeps the operational footprint small —
one repo, one deploy mechanism (git pull), one credential file.

The Cloudways copy of this repo doesn't run `sysco_ftp.py` — only dev-jan02
does, via cron. Cloudways' `.env` doesn't need any of the SYSCO_* vars.
