"""
build_master_from_workbook.py — regenerate the NRO onboarding "master" and "it"
tracks from Kim's maintained 180 Day Checklist workbook.

The workbook is now the source of truth for the opening checklist, replacing the
prototype HTML that build_onboarding_catalog.py parsed. This script patches only
the `master` and `it` tracks inside catalog_data.json — `it_full`, `marketing`
and the whole `reference` block are left exactly as they are, so this can be
re-run whenever Kim edits the workbook without disturbing anything else.

    python backend/build_master_from_workbook.py [--src <xlsx>] [--dry-run]

Inputs (committed, so the build is reproducible):
    backend/data/onboarding/source/180-day-checklist.xlsx

Output:
    backend/data/onboarding/catalog_data.json   (tracks.master, tracks.it)
    backend/data/onboarding/key_migration.json  (old key -> new key, by title)
    stdout: an owner-assignment report for Kim to eyeball

──────────────────────────────────────────────────────────────────────────────
TASK KEYS ARE DERIVED FROM THE TITLE TEXT.

    key = "mstr_" + <tab slug> + "_" + sha1(normalized title)[:8]

Stable across re-runs, re-orders and inserted rows — which positional keys
(mstr_001…) were not. The trade-off: materially rewording an activity mints a
new key and orphans that task's completion history. key_migration.json records
old->new so migrate_onboarding_keys.py can carry completions across; anything it
cannot match is listed for a human to resolve.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import openpyxl
except ImportError:  # pragma: no cover
    print("openpyxl is required:  pip install openpyxl", file=sys.stderr)
    raise

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(HERE, "data", "onboarding", "source", "180-day-checklist.xlsx")
CATALOG_PATH = os.path.join(HERE, "data", "onboarding", "catalog_data.json")
MIGRATION_PATH = os.path.join(HERE, "data", "onboarding", "key_migration.json")

OWNER_FRANCHISEE = "franchisee"
OWNER_NEWKS = "newks"

# Tabs that are not checklist content.
SKIP_TABS = {"NRO Info"}
IT_TAB = "IT Department"

# Header row / first data row on the restyled workbook (title banner + subtitle
# + two header-band rows now sit above the table).
CHECK_HEADER_ROW = 7
IT_HEADER_ROW = 7

# ─── owner assignment ────────────────────────────────────────────────────────
# Default: the franchisee owns (and can tick) the task. These two tabs are
# Support-Center-run back-office work the restaurant never actions itself.
NEWKS_ONLY_TABS = {"Accounting", "Legal"}

# ...plus any activity whose text names a Newk's-side team as the actor.
NEWKS_ACTOR_RE = re.compile(
    r"^\s*(?:the\s+)?"
    r"(?:field\s+marketing|marketing|sales|training|pd&c|support\s+center|"
    r"newk'?s\s+support\s+center|fsc|ops\s+services)"
    r"\s+(?:team\s+)?(?:adds|creates|adds\b|provides|adds|assigns|sends|places|"
    r"orders|posts|handles|manages|sets\s+up|will)",
    re.I,
)
# Explicit overrides win over both rules above. Keyed by exact activity text.
OWNER_OVERRIDES: Dict[str, str] = {
    "Marketing adds location to Yext Platform/Website as a coming soon location": OWNER_NEWKS,
    "Field Marketing team creates all online listing accounts (Google Business, Imaps, etc) "
    "& Facebook page, Facebook pre-opening campaign begins": OWNER_NEWKS,
    "45 Day Call": OWNER_NEWKS,
    "30 Day Call": OWNER_NEWKS,
    "15 Day Out Call - Marketing Update": OWNER_NEWKS,
    "Management candidate planning call": OWNER_NEWKS,
}

# Workbook IT-tab title -> curated onboarding_db title, for the handful of rows
# whose wording diverged too far for a safe automatic match. Keeping these
# explicit means the curated owner mapping is carried across deliberately rather
# than by a threshold that happens to land right.
IT_TITLE_ALIASES: Dict[str, str] = {
    "Get Logged Into Restaurant System Accounts": "Log Into Your Restaurant System Accounts",
    "R365 Labor Inventory & COGS": "Restaurant 365 (R365)",
    "Review Low Voltage Construction Plans with POS Vendor": "Review Low-Voltage Plans with POS Vendor",
    "DoorDash, UberEats, GrubHub, ASAP, Favor, etc.": "DoorDash, UberEats, GrubHub, ASAP, Favor",
}

# Franchisee-visible track ordering is decided in onboarding_db; this script only
# emits content. `audience` here is advisory metadata for the UI.
TRACK_AUDIENCE = "franchisee"


def norm(s: Optional[str]) -> str:
    """Whitespace/pun­ctuation-insensitive form used for keys and title matching."""
    if not s:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"[’‘']", "", s)          # Newk's -> newks, not "newk s"
    s = re.sub(r"[–—]", "-", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str, n: int = 4) -> str:
    words = norm(s).split()
    return "".join(w[0] for w in words)[:n] or "x"


def clean(v: Any) -> str:
    """Cell value -> trimmed string ('' for blanks). Excel leaves NBSPs about."""
    if v is None:
        return ""
    return str(v).replace("\xa0", " ").strip()


def weeks_label(v: Any) -> str:
    """0.3 -> 'Opening week' · 1.2 -> '1 wk out' · 22 -> '22 wks out'."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return clean(v)
    if f < 1:
        return "Opening week"
    n = int(f)
    return f"{n} wk out" if n == 1 else f"{n} wks out"


def assign_owner(tab: str, activity: str) -> str:
    if activity in OWNER_OVERRIDES:
        return OWNER_OVERRIDES[activity]
    if tab in NEWKS_ONLY_TABS:
        return OWNER_NEWKS
    if NEWKS_ACTOR_RE.search(activity):
        return OWNER_NEWKS
    return OWNER_FRANCHISEE


# ─── readers ─────────────────────────────────────────────────────────────────

def read_checklist_tab(ws) -> List[Dict[str, Any]]:
    """One department tab -> task dicts. Columns are fixed by the workbook:
    A Weeks Out · B Completion Date · C Completed · D Activity · E Company
    · F Vendor Contact · G Vendor Contact Info. · H Newk's Resources"""
    out = []
    for r in range(CHECK_HEADER_ROW + 1, ws.max_row + 1):
        activity = clean(ws.cell(row=r, column=4).value)
        if not activity:
            continue
        weeks = ws.cell(row=r, column=1).value
        out.append({
            "title": activity,
            "lead": weeks_label(weeks),
            "weeks": float(weeks) if isinstance(weeks, (int, float)) else None,
            "company": clean(ws.cell(row=r, column=5).value),
            "contact": clean(ws.cell(row=r, column=6).value),
            "contact_info": clean(ws.cell(row=r, column=7).value),
            "dept": clean(ws.cell(row=r, column=8).value) or ws.title,
        })
    return out


def read_it_tab(ws) -> List[Dict[str, Any]]:
    """The IT Department tab -> task dicts. Its shape differs from the
    department tabs: A Vendor/Document · B Description · C Department
    · D Required? · E Timing · F KB Article. Section bands (a value in A with
    B and C empty) become groups."""
    out = []
    group = "General"
    for r in range(IT_HEADER_ROW + 1, ws.max_row + 1):
        a = clean(ws.cell(row=r, column=1).value)
        b = clean(ws.cell(row=r, column=2).value)
        c = clean(ws.cell(row=r, column=3).value)
        if not a:
            continue
        if not b and not c:
            if a.upper().startswith("LEGEND"):
                continue
            group = a
            continue
        prio_raw = clean(ws.cell(row=r, column=4).value)
        out.append({
            "title": a,
            "desc": b,
            "dept": c,
            "prio": prio_from(prio_raw),
            "lead": clean(ws.cell(row=r, column=5).value).replace("—", "").strip(),
            "kb": kb_url(ws.cell(row=r, column=6)),
            "group_label": group,
        })
    return out


def read_nro_info(ws) -> Dict[str, Any]:
    """The NRO Info cover sheet -> the Support Center contact directory and the
    restaurant-information field labels, so the app can render that sheet as a
    tab like every other one.

    The restaurant values themselves are NOT read: on the web they come from the
    opening record, which is the live source. Only the labels travel, so the tab
    lists the same fields in the same order as the sheet."""
    contacts = []
    for r in range(6, 40):
        email = clean(ws.cell(row=r, column=5).value)
        dept = clean(ws.cell(row=r, column=6).value)
        rep = clean(ws.cell(row=r, column=7).value)
        if not (email or dept):
            if r > 14:
                break
            continue
        contacts.append({"email": email, "dept": dept, "rep": rep})
    # Rows 5-14 only: row 17 is the Restaurant Managers table header, whose
    # "Manager Name" cell sits in the same column and would otherwise be read
    # as an eleventh restaurant field.
    fields = []
    for r in range(5, 15):
        lab = clean(ws.cell(row=r, column=2).value)
        if lab:
            fields.append(lab.rstrip(":"))
    note = ""
    for r in range(20, ws.max_row + 1):
        v = clean(ws.cell(row=r, column=1).value)
        if v.startswith("⚠"):
            note = v
            break
    return {"contacts": contacts, "fields": fields, "note": note}


def prio_from(raw: str) -> str:
    r = raw.lower()
    if "conditional" in r:
        return "cond"
    if "recommend" in r:
        return "rec"
    if "optional" in r:
        return "opt"
    return "req"


def kb_url(cell) -> str:
    """The article link for a KB Article / Reference cell.

    Prefer the cell's hyperlink target: add_workbook_links.py replaces the raw
    URL text with a readable "KB Article ↗" and moves the URL onto the
    hyperlink, so reading the displayed text alone would lose every link. Falls
    back to the text for a cell that still holds a bare URL, and yields "" for a
    prose pointer with no link."""
    target = getattr(cell.hyperlink, "target", None) if cell.hyperlink else None
    if target and target.lower().startswith("http"):
        return target
    raw = clean(cell.value)
    return raw if raw.lower().startswith("http") else ""


# ─── track builders ──────────────────────────────────────────────────────────

def build_master(wb) -> Tuple[Dict[str, Any], List[Tuple[str, str, str]]]:
    """9 department tabs -> the master track, one group per department."""
    tabs = [ws.title for ws in wb.worksheets
            if ws.title not in SKIP_TABS and ws.title != IT_TAB]
    groups, tasks, report = [], [], []
    for gi, tab in enumerate(tabs, start=1):
        rows = read_checklist_tab(wb[tab])
        if not rows:
            continue
        groups.append({"id": tab, "label": tab, "order": gi})
        # Furthest-out first, matching how the workbook itself reads.
        rows.sort(key=lambda t: -(t["weeks"] if t["weeks"] is not None else -1))
        for oi, row in enumerate(rows):
            owner = assign_owner(tab, row["title"])
            key = f"mstr_{slug(tab)}_{hashlib.sha1(norm(row['title']).encode()).hexdigest()[:8]}"
            tasks.append({
                "key": key,
                "track": "master",
                "group": tab,
                "order": oi,
                "title": row["title"],
                "owner": owner,
                "dept": row["dept"],
                "lead": row["lead"],
                # Raw workbook value as well as the prose label — the table view
                # renders the sheet's own "Weeks Out" column.
                "weeks": row["weeks"],
                "desc": "",
                "contact": row["contact"],
                "contact_info": row["contact_info"],
                "company": row["company"],
                "kb": "",
                "form_url": "",
            })
            report.append((tab, owner, row["title"]))
    meta = {
        "label": "Master Opening Checklist",
        "audience": TRACK_AUDIENCE,
        "blurb": "The full 180-day opening checklist, by department. Tick each item "
                 "as it's done — anything you're unsure about, use Ask a question and "
                 "the Newk's team is notified straight away.",
        "groups": groups,
        "tasks": tasks,
    }
    return meta, report


def match_prev(title: str, index: Dict[str, List[Dict[str, Any]]]
               ) -> List[Dict[str, Any]]:
    """Candidate curated tasks this workbook row could correspond to, best first.

    Returns a LIST, not a single task: the workbook legitimately carries two rows
    that normalize identically ("High-Speed Internet Provider (Local)" appears
    under both the national and the local section), so the caller needs the next
    candidate when the best one is already spoken for.

    Exact normalized match first. Failing that, the workbook often carries the
    bare vendor name ("Toast") where the curated list is descriptive ("Toast —
    POS & Credit Card Processing"), so fall back to full token containment, then
    a prefix match, then a clear-winner token overlap. Ambiguous matches are
    refused rather than guessed."""
    if title in IT_TITLE_ALIASES:
        aliased = index.get(norm(IT_TITLE_ALIASES[title]))
        if aliased:
            return list(aliased)
    n = norm(title)
    if n in index:
        return list(index[n])
    toks = set(n.split())
    if not toks:
        return []
    # One title's words fully contained in the other's ("Newks Toolkit" inside
    # "Newks Toolkit Grand-Opening Marketing") is a strong, unambiguous signal.
    subset = [p for k, ps in index.items() for p in ps
              if toks <= set(k.split()) or set(k.split()) <= toks]
    if len(subset) == 1:
        return subset
    cands = [p for k, ps in index.items() for p in ps
             if k.startswith(n) or n.startswith(k)]
    if len(cands) == 1:
        return cands
    scored = []
    for k, ps in index.items():
        kt = set(k.split())
        overlap = len(toks & kt) / max(1, min(len(toks), len(kt)))
        if overlap >= 0.75:
            scored.extend((overlap, p) for p in ps)
    scored.sort(key=lambda x: -x[0])
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
        return [scored[0][1]]
    return []


def build_it(wb, prev_tasks: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """The IT Department tab -> the `it` track.

    Existing curated keys are reused wherever a title matches (so completions
    already recorded against them survive); the curated owner assignment is
    carried across too, since the workbook has no owner column."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for t in prev_tasks:
        index.setdefault(norm(t.get("title")), []).append(t)
    rows = read_it_tab(wb[IT_TAB])
    seen_groups: List[str] = []
    tasks, notes = [], []
    used_keys: set = set()
    for oi, row in enumerate(rows):
        if row["group_label"] not in seen_groups:
            seen_groups.append(row["group_label"])
        prev = next((p for p in match_prev(row["title"], index)
                     if p["key"] not in used_keys), None)
        if prev:
            key, owner = prev["key"], prev.get("owner", OWNER_FRANCHISEE)
        else:
            # Group is part of the seed because the workbook lists "High-Speed
            # Internet Provider (Local)" under both the national and the local
            # section; the suffix then guarantees uniqueness even if a future
            # edit puts two identical titles in the SAME section.
            seed = f"{row['group_label']}|{row['title']}"
            base = f"it_{hashlib.sha1(norm(seed).encode()).hexdigest()[:8]}"
            key, n = base, 2
            while key in used_keys:
                key = f"{base}_{n}"
                n += 1
            owner = OWNER_FRANCHISEE
            notes.append(f"NEW IT task (no curated match, owner defaulted to franchisee): {row['title']}")
        used_keys.add(key)
        tasks.append({
            "key": key,
            "track": "it",
            "group": row["group_label"],
            "order": oi,
            "title": row["title"],
            "owner": owner,
            "dept": row["dept"],
            "prio": row["prio"],
            "lead": row["lead"],
            "desc": row["desc"],
            "kb": row["kb"],
            "contact": "",
            "contact_info": "",
            "company": "",
            "form_url": "",
        })
    for t in prev_tasks:
        if t["key"] not in used_keys:
            notes.append(f"RETIRED curated IT task (no workbook row maps to it): {t.get('title')}")
    meta = {
        "label": "IT Setup Checklist",
        "audience": "all",
        "blurb": "Your IT vendor & systems setup — the accounts and agreements to sign, "
                 "in the order we recommend tackling them.",
        "groups": [{"id": g, "label": g, "order": i + 1} for i, g in enumerate(seen_groups)],
        "tasks": tasks,
    }
    return meta, notes


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=DEFAULT_SRC, help="180 Day Checklist .xlsx")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print(f"source workbook not found: {args.src}", file=sys.stderr)
        return 1

    # data_only so the header-band formulas don't come back as strings;
    # hyperlinks survive either way and carry the KB article targets.
    wb = openpyxl.load_workbook(args.src, data_only=True)
    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    prev_master = catalog.get("tracks", {}).get("master", {}).get("tasks", [])
    # The curated IT track lives in onboarding_db as code, not in the JSON —
    # that's where its reviewed owner (franchisee vs Newk's) mapping is, and it
    # is the thing worth preserving across this rebuild.
    prev_it = catalog.get("tracks", {}).get("it", {}).get("tasks", [])
    if not prev_it:
        sys.path.insert(0, HERE)
        try:
            import onboarding_db  # noqa: E402
            prev_it = list(onboarding_db.ONBOARDING_TASKS)
        except Exception as exc:
            print(f"WARNING: could not import curated IT tasks from onboarding_db "
                  f"({exc}) — IT owners will all default to franchisee", file=sys.stderr)

    master, owner_report = build_master(wb)
    it_track, it_notes = build_it(wb, prev_it)

    for name, track in (("master", master), ("it", it_track)):
        keys = [t["key"] for t in track["tasks"]]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            print(f"ERROR: duplicate task keys in {name} track: {sorted(dupes)}",
                  file=sys.stderr)
            return 2

    # old key -> new key, matched on normalized title, so a live DB's recorded
    # completions can be carried across (see migrate_onboarding_keys.py).
    #
    # The map ACCUMULATES across rebuilds. It has to: it is consumed once, on the
    # server at deploy time, and Kim will rebuild whenever the workbook changes.
    # A rebuild that simply recomputed old->new would compare the new keys
    # against themselves, find nothing, and blow away the mapping that had not
    # been applied yet.
    prior = {"master": {}, "unmatched": []}
    if os.path.exists(MIGRATION_PATH):
        try:
            with open(MIGRATION_PATH, encoding="utf-8") as f:
                prior = json.load(f)
        except Exception as exc:
            print(f"WARNING: could not read existing {MIGRATION_PATH} ({exc}) — "
                  f"starting a fresh map", file=sys.stderr)

    new_by_title = {norm(t["title"]): t["key"] for t in master["tasks"]}
    migration, unmatched = {}, []
    for t in prev_master:
        nk = new_by_title.get(norm(t.get("title")))
        if nk and nk != t["key"]:
            migration[t["key"]] = nk
        elif not nk:
            unmatched.append({"key": t.get("key"), "title": t.get("title")})

    # Re-point earlier entries whose target moved again in this run, then merge.
    merged = {old_k: migration.get(mid, mid) for old_k, mid in prior.get("master", {}).items()}
    merged.update(migration)
    # An old key that now resolves is no longer unmatched.
    by_key = {u["key"]: u for u in prior.get("unmatched", []) + unmatched}
    unmatched = [u for k, u in by_key.items() if k not in merged]
    migration = merged

    catalog.setdefault("tracks", {})["master"] = master
    catalog["tracks"]["it"] = it_track
    if "NRO Info" in wb.sheetnames:
        catalog.setdefault("reference", {})["nro_info"] = read_nro_info(wb["NRO Info"])
    catalog["_generated_by"] = (
        "build_onboarding_catalog.py (it_full, marketing, reference) + "
        "build_master_from_workbook.py (master, it)"
    )

    print(f"master track : {len(master['tasks'])} tasks across "
          f"{len(master['groups'])} departments  (was {len(prev_master)})")
    print(f"it track     : {len(it_track['tasks'])} tasks across "
          f"{len(it_track['groups'])} sections  (was {len(prev_it)})")

    fr = sum(1 for _, o, _ in owner_report if o == OWNER_FRANCHISEE)
    print(f"\nowner split  : {fr} franchisee-tickable, "
          f"{len(owner_report) - fr} Newk's-owned")
    print("\n--- Newk's-owned master tasks (franchisee sees these read-only) ---")
    for tab, owner, title in owner_report:
        if owner == OWNER_NEWKS:
            print(f"  [{tab}] {title[:88]}")

    if it_notes:
        print("\n--- IT track changes ---")
        for n in it_notes:
            print(f"  {n}")

    print(f"\nkey migration: {len(migration)} old master keys map to new keys")
    if unmatched:
        print(f"  {len(unmatched)} old keys have NO match in the workbook "
              f"(their completions cannot be carried over):")
        for u in unmatched[:15]:
            print(f"    {u['key']}  {str(u['title'])[:80]}")
        if len(unmatched) > 15:
            print(f"    … and {len(unmatched) - 15} more")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    with open(MIGRATION_PATH, "w", encoding="utf-8") as f:
        json.dump({"master": migration, "unmatched": unmatched}, f,
                  ensure_ascii=False, indent=1)
    print(f"\nwrote {CATALOG_PATH}")
    print(f"wrote {MIGRATION_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
