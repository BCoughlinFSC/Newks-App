# ToastWeb bulk edit tools

One-click edits to the **Location Prices** table on a ToastWeb menu item, delivered as
bookmarklets from `/toast-tools.html` in the Cloudways app. They run in your own
signed-in browser: nothing here touches or defeats Toast's bot protection, it just does
the clicking you would otherwise do by hand.

## Adding a new job

```bash
cp jobs/01-pick3-company-only.json jobs/03-my-new-job.json   # edit the rules
python build_tools_page.py                                   # validates + rebuilds the page
python tests/build_harness.py                                # regenerate the test pages
# open tests/harness-<id>.html in a browser -> must say HARNESS PASS
git add -A && git commit && deploy, then re-drag that job's two buttons
```

## Job format

```json
{
  "id": "kebab-id",
  "name": "Shown on the button",
  "description": "One line for the page",
  "guidance": "How to find the right items",
  "neverTouch": ["Catering", "Box Salad", "To Go", "Custom ", "*System"],
  "rules": [
    { "type": "setPrice",  "match": "^...$", "price": 12.99 },
    { "type": "ensureRow", "rowLabel": "Tier 8 - Dine In - Company Only",
                           "locationId": "1100000019181590750", "price": 13.49 },
    { "type": "removeRow", "match": "^...$" }
  ]
}
```

- **setPrice** — every row whose whole label matches gets that price.
- **ensureRow** — add the row if missing, correct its price if wrong. Idempotent.
- **removeRow** — click the trashcan on every row whose whole label matches.
- **neverTouch** — substring veto applied on top of everything else.

Known Company Only location ids (global, identical on every item):

| Group | locationId | price used Aug 2026 |
|---|---|---|
| Tier 8 - Dine In - Company Only  | 1100000019181590750 | 13.49 |
| Tier 10 - Dine In - Company Only | 1100000019182217600 | 13.99 |
| Tier 12 - Dine In - Company Only | 1100000019182223700 | 14.49 |

To find another group's id, use the **Probe options** button on an item — it lists every
option the item's location dropdown offers and copies the JSON to your clipboard.

## Why the build refuses things

Each rule below exists because of a bug that actually happened.

| Guard | The bug it prevents |
|---|---|
| `match` must be anchored `^...$` | `Tier 8 - Dine In` as a substring also matches `Tier 8 - Dine In - Company Only`, a different price group. |
| A rule may not match its own job's `ensureRow` labels | The job would fight itself — set a price then immediately re-target the row it just added. |
| `neverTouch` may not veto the job's own `ensureRow` label | Silently makes the job a no-op. |
| Canary labels | Every job is tested against real near-miss labels (`Tier 8 Catering`, `Tier 8 - Box Salad Price`, `Tier 7 & 8 - Box Sandwich & Wrap`, `Tier 8 - To Go`, `*System`, `Custom MDH`). |
| `node --check` on every generated bookmarklet | A mangled escape once turned a button into a **silent** no-op: no error, no alert, nothing. |

## Engine invariants

- Toast's hidden add template (`id` contains `INDEX`, `class="template"`) is never a
  target. Its label is the entire ~15k-char location picker and matches almost anything.
- A row we just **added** shows a `<select>`, whose `innerText` is every option
  concatenated — such rows are labelled from the **selected option**, never `innerText`,
  or the engine cannot see the row it just created.
- Toast may insert a cloned row on a **later tick**, so adds poll for up to 5s.
- A row whose trash has been clicked stays in the DOM (trash hidden, "undo remove" shown)
  until save — treated as already gone.
- Location groups are resolved **by id**, then the option label is **verified** before
  committing. Option order is not stable between items (306 vs 307 options seen).
- **Every** edit is re-read and verified before Save. Anything unexpected aborts
  *without saving*.

## Files

| Path | What |
|---|---|
| `engine.js` | Generic, job-agnostic engine. Change with care; rerun the harness. |
| `jobs/*.json` | The jobs. Data only — this is what you edit for a new task. |
| `validate_jobs.mjs` | Job validation in real JS regex semantics. |
| `build_tools_page.py` | Validates, syntax-checks, writes `frontend/toast-tools.html`. |
| `tests/fixture_item.html` | Fake ToastWeb item page mirroring the real markup. |
| `tests/build_harness.py` | One self-asserting test page per job, generic oracle. |
