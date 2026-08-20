# NRO Onboarding — runbook

The New Restaurant Opening checklist app: an internal dashboard plus a
password-free magic link for the franchisee, with per-task questions that email
the NRO owners the moment they're asked.

## Where things live

| Piece | Path |
|---|---|
| Internal dashboard | `frontend/onboarding.html` |
| Franchisee magic-link page | `frontend/onboarding-journey.html` |
| Shared styles | `frontend/onboarding-theme.css` |
| Data layer (SQLite) | `backend/onboarding_db.py` |
| API + digest job | `backend/main.py` (search `NRO IT Onboarding Journey`) |
| Email templates | `backend/email_service.py` (search `NRO Onboarding notifications`) |
| Runtime content | `backend/data/onboarding/catalog_data.json` |
| Live database | `backend/data/onboarding/onboarding.db` |

**Content sources** (committed, so a rebuild is reproducible):

| Track | Built from | By |
|---|---|---|
| `master` (124 tasks, 9 departments) | `data/onboarding/source/180-day-checklist.xlsx` | `build_master_from_workbook.py` |
| `it` (28 tasks, 4 sections) | the same workbook's **IT Department** tab | `build_master_from_workbook.py` |
| `it_full`, `marketing`, all `reference` | Kim's prototype HTML files | `build_onboarding_catalog.py` |

## Editing the checklist

Edit the workbook, drop it over `data/onboarding/source/180-day-checklist.xlsx`, then:

```bash
python backend/build_master_from_workbook.py --dry-run
```

Read the report before writing. It tells you three things worth checking:

- **the owner split** — which tasks the franchisee can tick vs. which are
  read-only for them. Anything misfiled is fixed in `OWNER_OVERRIDES`,
  `NEWKS_ONLY_TABS` or `NEWKS_ACTOR_RE` at the top of the script.
- **IT track changes** — new or retired tasks against the curated list.
- **key migration** — old keys that map to new ones, and any that don't.

Then drop `--dry-run` to write `catalog_data.json`, and follow **Deploying** below.

> Task keys are a hash of the activity text (`mstr_<tab>_<hash>`), which is why
> re-ordering or inserting rows is safe. **Materially rewording an activity mints
> a new key** and orphans that task's completion history — the build reports it,
> and `migrate_onboarding_keys.py` moves the history across.

## Deploying

```bash
ssh master_newksxl@<server>
cd ~/applications/wpftndhcuj/public_html/backend
```

1. Pull the code + the regenerated `catalog_data.json` and `key_migration.json`.
2. **Migrate recorded completions to the new keys — once, before anyone opens the
   dashboard.** It backs the DB up first and is safe to re-run:
   ```bash
   python migrate_onboarding_keys.py --dry-run   # read the plan
   python migrate_onboarding_keys.py             # apply
   ```
3. Restart the app (supervisor — see `supervisor.conf`). New tables
   (`task_questions`, `activity`) are created on startup by
   `initialize_schema()`; no manual DDL.

## Notifications

| Event | Who | When |
|---|---|---|
| Franchisee asks a question | `NRO_NOTIFY_TO` | immediately |
| Staff answers a question | the franchisee | immediately |
| Tasks completed / re-opened | `NRO_NOTIFY_TO` | one digest per opening, 5:00 PM |

`NRO_NOTIFY_TO` is a comma-separated env var, defaulting to
`kaylesworth@newks.com,bcoughlin@fscfranchiseco.com`. Set it in the app's `.env`
to change recipients without a code change.

Digest events are stamped `notified_at` **only after a successful send**, so an
SMTP outage rolls them into the next day's digest rather than losing them.
Questions are stamped at insert, so they can never also appear in a digest.

To send a digest on demand (testing, or catching up after an outage):

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" https://<host>/api/onboarding/send-digest
```

## Gotchas

- **A franchisee can ask a question about *any* task, including ones they can't
  tick.** That's deliberate — "this isn't mine to action" is the commonest reason
  to ask. Only *ticking* is owner-restricted.
- **A failed notification never fails the request.** The question is recorded and
  visible on the dashboard regardless; the email is best-effort on top. The
  franchisee's toast tells them which happened.
- **The magic-link token is never echoed back** to the franchisee page. Rotating
  it (dashboard → Issue new link) invalidates the old link immediately.
- **`data_only=True` when reading the workbook** — the header band on each tab is
  formulas pointing at the NRO Info tab, and openpyxl returns `None` for those
  without it. The build script already does this.
- **Escalation contacts are franchisee-visible**; billing contacts are not. See
  `_INTERNAL_CONTACT_GROUPS` in `onboarding_db.py`.
- Running the app locally needs `azure-storage-blob` for an unrelated startup
  step. To eyeball just the onboarding pages without it, serve `frontend/` and
  stub the `/api/onboarding/*` routes against a copy of the SQLite file.
