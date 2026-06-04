# Contacts data — export from SQL Server → SQLite on Cloudways

## When to run this

- **First time:** to populate the new Contacts UI on Cloudways with all the
  existing AreaDir / Company data from SQL-PROD.
- **Periodically:** as long as SQL Server is still being written to by Yext
  closures, Cloudways edits, or other sources, the Cloudways copy will drift.
  Re-export to refresh it. Once SQL Server's contacts DB is retired, this
  becomes one-way (Cloudways → wherever).
- **After schema changes:** if a new table gets added to the list of tables
  the UI cares about, edit `DEFAULT_TABLES` in
  `contacts_export_from_sqlserver.py` and re-run.

## How to run

The exporter runs on **dev-jan02** (or any box on the Newks internal network
that can reach `sql-prod` and has `pyodbc` + an ODBC SQL Server driver
installed).

### 🐧 On **dev-jan02**

```bash
# 1. SSH in
ssh root@dev-jan02.nfc.local

# 2. Pull latest code if needed
cd /opt/toast_net_sales_app/backend
git pull origin main

# 3. Set the SQL Server password env var (get it from KeePass)
export CONTACTS_SOURCE_PASSWORD='REDACTED_FROM_KEEPASS'

# 4. Run the export — produces backend/data/contacts/contacts.db
python3 contacts_export_from_sqlserver.py

# Or with custom tables / output:
# python3 contacts_export_from_sqlserver.py --tables AreaDir,Company --output /tmp/contacts.db
```

Expected output:
```
2026-06-04 15:30:00 [INFO] Output: backend/data/contacts/contacts.db
2026-06-04 15:30:00 [INFO] Tables: ['AreaDir', 'Company']
2026-06-04 15:30:00 [INFO] Connecting to SQL Server: sql-prod.nfc.local as reports
2026-06-04 15:30:00 [INFO] ─── Exporting AreaDir ───
2026-06-04 15:30:00 [INFO]   copied 68 rows (pk=AreaDirID, cols=11)
2026-06-04 15:30:01 [INFO] ─── Exporting Company ───
2026-06-04 15:30:01 [INFO]   copied N rows (pk=CompanyID, cols=...)
2026-06-04 15:30:01 [INFO] Export complete:
2026-06-04 15:30:01 [INFO]   AreaDir                       68 rows
2026-06-04 15:30:01 [INFO]   Company                       NN rows
```

## SCP to Cloudways

After a successful export, copy the SQLite file onto Cloudways:

### 🐧 On **dev-jan02**

```bash
scp /opt/toast_net_sales_app/backend/data/contacts/contacts.db \
    master_newksxl@phpstack-1474989-6237770.cloudwaysapps.com:/home/master/applications/wpftndhcuj/public_html/backend/data/contacts/contacts.db
# (Cloudways master_* password from KeePass)
```

Or, if you don't have direct SCP set up between dev-jan02 and Cloudways:

1. SCP from dev-jan02 → your laptop
2. SCP from your laptop → Cloudways

No restart needed afterwards — uvicorn re-opens the SQLite file on every
request (WAL mode means hot-swapping the file is safe-ish, but if you're
paranoid run `./restart.sh` to be sure).

## Verify on Cloudways

### 🐧 On **Cloudways**

```bash
cd /home/master/applications/wpftndhcuj/public_html
source venv/bin/activate
python -c "
from backend import contacts_db
print(contacts_db.get_metadata())
"
```

Should show `{"available": true, "path": "...", "tables": {"AreaDir": 68, "Company": NN, ...}}`.

Then open https://your-cloudways-url/contacts-areadir.html — should show
all 68 area directors.

## Adding more tables to the export

Edit `backend/contacts_export_from_sqlserver.py`. Find `DEFAULT_TABLES` at
the top. Add the table name to the list. Re-run the exporter.

The script auto-introspects column types and primary keys from SQL
Server's INFORMATION_SCHEMA — no manual schema definition needed.

If a new table needs custom logic (e.g., joining lookups, denormalizing
columns), don't add it to `DEFAULT_TABLES` — write a dedicated function
in this same file and call it from `main()`.
