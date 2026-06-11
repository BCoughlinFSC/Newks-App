# Contacts Outbox Drainer — Runbook

## What this is

Hourly cron job on **dev-jan02** that pulls pending edits from the
Cloudways `contacts_outbox` queue and replays them against SQL-PROD's
`contacts` database. Closes the loop between the in-app UI (which only
writes to the Cloudways SQLite mirror) and the canonical SQL Server
data that SSRS reports + Yext + other Newk's systems read from.

```
┌──────────────────────────┐                     ┌──────────────────────┐
│ Cloudways app (FastAPI)  │  ──── HTTPS ───→    │ dev-jan02            │
│ writes edits → outbox    │                     │ runs drainer hourly  │
│ SQLite contacts_outbox   │  ←── HTTPS ────     │ pymssql → sql-prod   │
└──────────────────────────┘                     └──────────────────────┘
        UI lives here                              SQL-PROD lives here
```

## One-time setup on dev-jan02

### 1. Clone / pull the latest app code

```bash
ssh dev-jan02
cd ~/toast_net_sales_app
git pull
```

### 2. Install pymssql + requests

```bash
pip3 install --user pymssql requests
```

### 3. Create the env file

```bash
nano ~/.contacts_drainer.env
```

Paste this template, then fill in values:

```bash
# Cloudways app (where the outbox queue lives)
export APP_BASE_URL='https://phpstack-1474989-6237770.cloudwaysapps.com'

# PREFERRED auth: the drainer logs itself in each run and mints a fresh
# token, so token expiry can never silently kill it (which is what
# happened 2026-06-04..12 — the static token aged out at ~30 days and
# every hourly run 401'd until someone noticed).
export APP_ADMIN_EMAIL='admin@newks.com'
export APP_ADMIN_PASSWORD='get-from-keepass'

# Legacy fallback (only used when email/password are unset). Expires!
# export APP_ADMIN_TOKEN='paste-your-admin-token-here'

# SQL-PROD write account — MUST have INSERT/UPDATE/DELETE permission
# on every contacts.dbo.* table the app edits. The legacy "reports" user
# is read-only, so we need a different account.
export CONTACTS_SQL_HOST='sql-prod.nfc.local'
export CONTACTS_SQL_USER='kaylesworth'           # or a dedicated drainer account
export CONTACTS_SQL_PASSWORD='get-from-keepass'
export CONTACTS_SQL_DATABASE='contacts'
export CONTACTS_SQL_SCHEMA='dbo'

# Email alerts on terminal failures (optional but recommended)
export SMTP_HOST='smtp.gmail.com'
export SMTP_PORT='587'
export SMTP_USER='your-gmail@gmail.com'
export SMTP_PASSWORD='16-char-app-password'      # NOT your Gmail login
export ALERT_TO='kaylesworth@newks.com'
```

Lock it down:

```bash
chmod 600 ~/.contacts_drainer.env
```

### 4. Auth

With `APP_ADMIN_EMAIL` + `APP_ADMIN_PASSWORD` set (preferred, see env
template above), there's nothing else to do — the drainer logs in and
mints a fresh token every run, and re-logins automatically if it ever
sees a 401 mid-run.

<details>
<summary>Legacy static-token fallback (avoid)</summary>

1. Open the app in a browser, log in as admin
2. Devtools → Application → Local Storage → copy `auth_token`
3. Paste into `APP_ADMIN_TOKEN`

Tokens last ~30 days, then every cron run 401s until someone notices —
this is exactly the failure mode that silently killed the drainer from
2026-06-04 to 06-12. Use email/password auth instead.
</details>

### 5. Test with a dry run

Make a small edit in the Cloudways UI first (e.g. tweak a company name)
so there's at least one outbox row to look at. Then on dev-jan02:

```bash
source ~/.contacts_drainer.env
cd ~/toast_net_sales_app/backend
python3 contacts_outbox_drainer.py --dry-run
```

You should see something like:

```
Claiming batch of up to 100 pending outbox rows from https://...
Got 1 rows. SQL Server: sql-prod.nfc.local as kaylesworth
DRY-RUN MODE — no SQL writes will be made, no Cloudways updates posted.
Row 1: UPDATE Company pk=42 (attempt 1/3)
  (would apply: UPDATE on Company, payload={'CompanyName': 'New Name'})
Drain complete: applied=0  failed_terminal=0  failed_retry=0  skipped=1
```

If that looks right, run for real:

```bash
python3 contacts_outbox_drainer.py
```

Then check `/contacts-outbox.html` on Cloudways — the row should now
show `status=success`, `applied_at` populated. And in SSMS, query the
contacts table to confirm the edit really landed.

### 6. Add the cron entry

```bash
crontab -e
```

Append:

```cron
# Drain Cloudways contacts outbox → SQL-PROD every 15 minutes
*/15 * * * * . $HOME/.contacts_drainer.env && /usr/bin/python3 $HOME/toast_net_sales_app/backend/contacts_outbox_drainer.py >> $HOME/contacts_drainer.log 2>&1
```

> Note: the `. $HOME/.contacts_drainer.env &&` prefix sources the env
> file inside the cron context. Without that, cron has no env vars.
>
> Cadence: `*/15 * * * *` = every 15 minutes. The original deployment
> used `15 * * * *` (hourly at :15 past) — easy to misread as
> every-15-minutes. Pick whichever; the drainer is idempotent and a
> run with an empty queue takes ~2 seconds.

### 7. Flip the feature flag on the Cloudways side

Up until now, edits in the UI haven't been captured in the outbox
because `CONTACTS_OUTBOX_ENABLED=0` (default). To enable:

```powershell
# From your Windows laptop
ssh master_newksxl@165.227.79.143
nano /home/master/applications/wpftndhcuj/public_html/backend/.env

# Add this line:
CONTACTS_OUTBOX_ENABLED=1

# Save, then restart:
cd /home/master/applications/wpftndhcuj/public_html && ./restart.sh
```

After this restart, every contacts edit on Cloudways gets captured in
the outbox and the next cron run will sync it to SQL-PROD.

## Day-to-day operation

### Where to look

- **`/contacts.html`** — yellow banner shows pending count + last drain time.
- **`/contacts-outbox.html`** — admin-only inspector. Filter by
  Pending / Failed / Applied. Click `Retry` on a failed row to reset
  it to pending (drainer picks it up next run).
- **`~/contacts_drainer.log`** on dev-jan02 — full drain history.
- **Your inbox** — terminal failures (attempts ≥ 3) trigger an email.

### Typical states

| Banner color | Meaning |
|---|---|
| Green: "queue empty" | Healthy — nothing to do |
| Amber: "N edit(s) pending" | Normal — drainer just hasn't run yet, or there's a backlog |
| Red: "N failed" | Needs attention — open `/contacts-outbox.html`, inspect, fix root cause, click Retry |

### Common failure modes + fixes

**`401 Unauthorized` from Cloudways API in the cron log**
→ Admin token expired. Re-grab it from browser localStorage, update
`~/.contacts_drainer.env`, run manually to confirm.

**`Login failed for user 'kaylesworth'` from pymssql**
→ Password rotated. Update `CONTACTS_SQL_PASSWORD` in the env file.

**`UPDATE matched 0 rows in {table} where {pk}={val}`**
→ The local SQLite said the row exists, but SQL Server doesn't have it.
Either it was deleted in SQL Server out-of-band (e.g. someone manually
ran a DELETE in SSMS), or the local mirror is out of sync. Fix:
re-export with `contacts_export_from_sqlserver.py` to refresh the
mirror, then click Retry on the affected outbox row.

**`INSERT sync not yet supported`**
→ Someone added a new row in the UI (e.g. a new Company). v1 of the
drainer can't auto-INSERT yet (IDENTITY_INSERT complexity). Workaround:
add the row in SSMS manually, then re-export the mirror to pick it up
locally. Then click Retry — the row will probably now succeed via the
idempotent path. INSERT support is on the Phase 3 backlog.

**`KeyError` or other pythonic crash**
→ Bug. Email the traceback, file an issue. The drainer rolls back its
SQL transaction so partial state shouldn't leak.

### Catch-up after a long outage

If the drainer's been off for a day or three (vacation, dev-jan02 outage),
the queue could be hundreds of rows. To catch up faster:

```bash
source ~/.contacts_drainer.env
cd ~/toast_net_sales_app/backend
python3 contacts_outbox_drainer.py --batch-size 1000
```

The drainer is safe to run multiple times — it claims rows atomically
on the API side, so two concurrent runs won't double-apply.

### Pause the drainer temporarily

To pause sync without disabling the outbox capture (so edits keep
queueing for later):

```bash
crontab -e
# Comment out the drainer line by adding # at the start
```

The outbox banner on `/contacts.html` will go amber as edits accumulate
— that's normal. Uncomment the cron line + run manually to catch up.

To pause the capture itself (no edits queued, behavior reverts to v1):
flip `CONTACTS_OUTBOX_ENABLED=0` in the Cloudways .env and restart.

## Sync from SQL (mirror refresh via the UI button)

The Contacts landing page has a **⟳ Sync from SQL** button (admin only,
in the outbox banner). Because Cloudways can't reach SQL-PROD, the
button doesn't pull directly — it queues a request that THIS drainer
fulfills:

1. Button → `POST /api/contacts/sync-request` (idempotent; one pending
   request at a time)
2. Drainer, at the end of each cron run: `GET /api/contacts/sync-request`
3. If a request is pending AND the outbox is fully drained (pending=0 —
   so a refresh can never clobber unsynced local edits), the drainer:
   - runs `contacts_export_from_sqlserver.py --output /tmp/...`
     (needs `CONTACTS_SOURCE_PASSWORD` in the env; falls back to
     `CONTACTS_SQL_PASSWORD`)
   - uploads the fresh file to `POST /api/contacts/import-db`
4. The app validates the upload (expected tables, sane Store count),
   copies `contacts_outbox` + `contacts_sync_requests` into the new
   file, swaps it in via the sqlite backup API, and marks the request
   fulfilled. No restart needed.

If fulfillment fails (exporter error, upload error), the request stays
pending, an alert email goes out, and the next cron run retries.
`--skip-sync` skips fulfillment for outbox-only runs.

This replaces the old manual flow (run exporter → SCP to Cloudways) for
routine refreshes. The manual flow in contacts_export_runbook.md still
works and remains the fallback.

## What v1 does NOT do (Phase 3 backlog)

- **INSERT sync** — adding a new lookup row (Company, FBC, etc.) via
  the UI captures an outbox row, but the drainer can't replay it yet
  because of the IDENTITY_INSERT complexity. New rows must be added in
  SSMS for now.
- **Conflict detection** — if the same row was edited in SSMS between
  the mirror's last export and the drainer's apply, the drainer just
  overwrites. We don't compare current values.
- **Bulk operations** — the drainer applies row-by-row. Fine for
  hundreds of edits but slow at thousands.
- **Service-account token** — currently uses a human admin token,
  which expires. A dedicated service account would last forever.
