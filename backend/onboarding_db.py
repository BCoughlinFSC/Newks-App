"""
onboarding_db.py — SQLite layer for the NRO IT Onboarding Journey.

Turns the static "Newk's NRO IT Onboarding" email into a tracked, interactive
journey. One row per New Restaurant Opening; each opening carries a copy of the
task checklist (status only — the task *template* lives in code, below, so we can
tweak wording/links without a migration).

Two audiences, deliberately kept separate:
  • Internal staff (IT / FSC / Marketing / Ops) — authenticated via the app's
    normal JWT accounts. They create openings, send invites, and can tick any
    task. Handled in main.py with Depends(get_current_admin_user).
  • Franchisees (external) — never get an app account. Each opening has an
    unguessable public token; the franchisee journey page authenticates with
    that token alone (get_opening_by_token). They can only tick FRANCHISEE-owned
    tasks. No password, nothing in users.json.

Storage: backend/data/onboarding/onboarding.db (WAL, like the other app-local
SQLite mirrors). App-owned — no SQL Server / outbox sync.
"""

import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ONBOARDING_DB_PATH = os.getenv(
    "ONBOARDING_SQLITE_PATH",
    os.path.join(os.path.dirname(__file__), "data", "onboarding", "onboarding.db"),
)

# Task ownership: who is expected to complete the task.
OWNER_FRANCHISEE = "franchisee"
OWNER_NEWKS = "newks"

# Priority values (drive the pills in the UI). Loosely enforced.
PRIORITIES = ["req", "rec", "opt", "cond"]

# ─────────────────────────────────────────────────────────────────────────────
# TASK TEMPLATE — the checklist, straight from the NRO IT onboarding email
# (Newks_NRO_IT_Onboarding_Email, Rev. 7/16/26). Edit wording / links / owner
# here; existing openings pick up changes immediately (status is keyed by
# `key`, so keep keys stable — renaming a key orphans prior completions).
#
# Fields: key · step · title · prio · lead · owner · dept · desc · kb
# ─────────────────────────────────────────────────────────────────────────────
KB = "https://newksit.freshdesk.com"
TICKET = "https://fscticket.freshdesk.com/support/tickets/new"

ONBOARDING_TASKS: List[Dict[str, Any]] = [
    # ---- Step 1 · Documents & Setup — Complete First ----
    dict(key="franchisee_info_form", step=1, title="Newk's Franchisee Information Form",
         prio="req", lead="180 days out", owner=OWNER_FRANCHISEE, dept="",
         desc="Ownership structure, contacts, business emails, above-store management.",
         kb=f"{KB}/support/solutions/articles/5000880525-newk-s-website-information-form-for-new-restaurant-openings-nro-"),
    dict(key="systems_questionnaire", step=1, title="Restaurant Systems Questionnaire",
         prio="req", lead="180 days out", owner=OWNER_FRANCHISEE, dept="",
         desc="Establishes catering fees, contacts and billing — drives several downstream builds.",
         kb=f"{KB}/en/support/solutions/articles/5000880546-msi-catering-platform-questionnaire-for-new-restaurant-openings-nro-"),
    dict(key="account_logins", step=1, title="Log Into Your Restaurant System Accounts",
         prio="req", lead="180 days out", owner=OWNER_FRANCHISEE, dept="",
         desc="Newk's IT provides credentials — log in, confirm access, gather account details.",
         kb=f"{KB}/support/solutions/articles/5000880548-logging-to-accounts-for-new-restaurant-openings-nro-"),
    dict(key="low_voltage_plans", step=1, title="Review Low-Voltage Plans with POS Vendor",
         prio="req", lead="Construction phase", owner=OWNER_FRANCHISEE, dept="",
         desc="Confirm data/voice drops, terminal placement and local power for all equipment.",
         kb=f"{KB}/en/support/solutions/articles/5000880593-low-voltage-vendors-and-review-plans-with-pos-vendor-for-new-restaur"),

    # ---- Step 2 · National IT Vendor Agreements ----
    dict(key="toast_pos", step=2, title="Toast — POS & Credit Card Processing",
         prio="req", lead="Min. 8 weeks prior", owner=OWNER_NEWKS, dept="IT / FSC",
         desc="Site is provisioned in the Newk's Toast instance. IT completes the back-end setup.",
         kb=TICKET),
    dict(key="managed_network", step=2, title="FSC Managed Network",
         prio="req", lead="Contact early", owner=OWNER_NEWKS, dept="IT / FSC",
         desc="FSC IT provides and manages the restaurant's network equipment, wireless "
              "networks, and cellular backup connectivity for Toast services. Included in "
              "the franchise agreement — contact FSC IT early so equipment placement, "
              "installation requirements, and low-voltage cabling can be coordinated.",
         kb=TICKET),
    dict(key="olo_olopay", step=2, title="OLO & OLO Pay",
         prio="req", lead="50 days prior", owner=OWNER_FRANCHISEE, dept="",
         desc="Digital ordering + OLO Pay card-present. Complete both the onboarding and OLO Pay forms.",
         kb=f"{KB}/en/support/solutions/articles/5000880674-olo-and-olo-pay-for-new-restaurant-openings-nro-"),
    dict(key="paytronix", step=2, title="Paytronix Gift Cards",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="Complete the Franchisee ACH Participation Form for the gift-card program.",
         kb=f"{KB}/support/solutions/articles/5000892718-nro-checklist-paytronix-gift-card-participation-form"),
    dict(key="r365", step=2, title="Restaurant 365 (R365)",
         prio="req", lead="Details to follow", owner=OWNER_NEWKS, dept="FSC Team",
         desc="New labor/scheduling + inventory platform. FSC sends sign-up instructions later.",
         kb=""),
    dict(key="opus_elearning", step=2, title="Opus (eLearning)",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="eLearning / LMS sign-up. Course assignments are handled later by Ops Services.",
         kb=f"{KB}/en/support/solutions/articles/5000891349-nro-checklist-sign-up-with-opus-training-elearning-platform"),
    dict(key="fiserv_mid", step=2, title="FiServ MID (Monkey Catering Payments)",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="Payment gateway required to process cards through Monkey Catering.",
         kb=f"{KB}/en/support/solutions/articles/5000880736-plug-and-pay-for-new-restaurant-openings-and-catering-payments-nro-"),
    dict(key="amex", step=2, title="American Express",
         prio="rec", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="National Amex vendor for better rates — needs two MIDs (Toast POS + Catering).",
         kb=f"{KB}/en/support/solutions/articles/5000866209-american-express-setup-process-for-new-restaurant-opening-nro-"),
    dict(key="cctv", step=2, title="DTiQ or Envysion — CCTV / Cameras",
         prio="rec", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="Approved CCTV partners for loss prevention and security cameras.",
         kb=f"{KB}/en/support/solutions/articles/5000880749-dtiq-or-envysion-cctv-cameras-for-new-restaurant-openings-nro-"),
    # (Grubbrr Kiosks task retired 2026-08 — removed per FSC direction.)

    # ---- Step 3 · Local Agreements & Vendors ----
    dict(key="alarm", step=3, title="Burglar & Fire Alarm Services",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="ADT is the national partner; local sourcing allowed. Must include a cellular unit.",
         kb=f"{KB}/en/support/solutions/articles/5000881313-nro-burglar-and-fire-alarm-sourcing"),
    dict(key="internet_local", step=3, title="High-Speed Internet Provider (Local)",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="The franchisee is responsible for selecting and contracting with a local "
              "high-speed internet provider. FSC IT is available to review available "
              "provider options and service speeds with the franchisee.",
         kb=f"{KB}/en/support/solutions/articles/5000881312-nro-high-speed-internet-provider"),
    dict(key="voip_local", step=3, title="Voice / Telephone Provider (Local)",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="",
         desc="The franchisee is responsible for selecting and contracting with a telephone "
              "or VOIP provider. Telephone service may be purchased through the internet "
              "provider or another local vendor. Any required cabling should be coordinated "
              "with the low-voltage installer.",
         kb=f"{KB}/en/support/solutions/articles/5000880748-nro-checklist-choose-a-voice-telephone-provider-nro-"),

    # ---- Step 4 · Managed by Other Departments ----
    dict(key="punchh", step=4, title="Punchh Loyalty & Local Promotions",
         prio="req", lead="", owner=OWNER_NEWKS, dept="Marketing",
         desc="Loyalty, promotion and discounting platform.",
         kb=f"{KB}/en/support/solutions/articles/5000881320-nro-punchh-loyalty-promotion-and-discounting-platform"),
    dict(key="yext", step=4, title="Yext — Local Listings",
         prio="req", lead="", owner=OWNER_NEWKS, dept="Marketing",
         desc="Verify accuracy of local listings across the web.",
         kb=f"{KB}/support/solutions/articles/5000892719-nro-checklist-verify-accuracy-of-local-listings-via-yext-platform"),
    dict(key="newksgear", step=4, title="NewksGear (Threds) — Uniforms",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="Marketing",
         desc="Order team uniforms through NewksGear.",
         kb=f"{KB}/en/support/solutions/articles/5000881321-nro-newksgear-com"),
    dict(key="toolkit", step=4, title="Newk's Toolkit — Grand-Opening Marketing",
         prio="req", lead="", owner=OWNER_FRANCHISEE, dept="Marketing",
         desc="Access grand-opening marketing assets and toolkit.",
         kb=f"{KB}/en/support/solutions/articles/5000881322-nro-newk-s-marketing-toolkit-access"),
    dict(key="directv", step=4, title="DirecTV / Music Choice — In-Store Audio",
         prio="rec", lead="", owner=OWNER_FRANCHISEE, dept="Marketing",
         desc="In-store audio / TV programming.",
         kb=f"{KB}/support/solutions/articles/5000881276-nro-checklist-directv-music-choice-for-new-restaurant-openings-nro-"),
    dict(key="prep_n_print", step=4, title="Prep n Print Flex — Labeling",
         prio="req", lead="", owner=OWNER_NEWKS, dept="Ops Services",
         desc="Food labeling platform setup.",
         kb=f"{KB}/en/support/solutions/articles/5000881323-nro-prep-n-print"),
    dict(key="opus_courses", step=4, title="Opus — Courses & Curriculum",
         prio="req", lead="", owner=OWNER_NEWKS, dept="Ops Services",
         desc="Ops Services / PDC assigns the training curriculum.",
         kb=f"{KB}/en/support/solutions/articles/5000891349-nro-checklist-sign-up-with-opus-training-elearning-platform"),
    dict(key="ezcater", step=4, title="ezCater — Catering Marketplace",
         prio="rec", lead="", owner=OWNER_FRANCHISEE, dept="Ops Services",
         desc="List on the ezCater catering marketplace.",
         kb=f"{KB}/en/support/solutions/articles/5000881316-nro-ezcater-catering-sales-marketplace"),
    dict(key="third_party_delivery", step=4, title="DoorDash, UberEats, GrubHub, ASAP, Favor",
         prio="rec", lead="", owner=OWNER_FRANCHISEE, dept="Ops Services",
         desc="Establish accounts with 3rd-party delivery marketplaces.",
         kb=f"{KB}/en/support/solutions/articles/5000881319-nro-establishing-accounts-with-3rd-party-delivery-marketplaces"),
    dict(key="tripleimpact", step=4, title="TripleImpact — Catering Call Center",
         prio="opt", lead="", owner=OWNER_NEWKS, dept="Ops Services",
         desc="Optional catering call-center service.",
         kb=f"{KB}/en/support/solutions/articles/5000881317-nro-triple-impact-catering-call-center-services"),
]

STEP_TITLES = {
    1: "Documents & Setup — Complete First",
    2: "National IT Vendor Agreements",
    3: "Local Agreements & Vendors",
    4: "Managed by Other Departments",
}

# ─────────────────────────────────────────────────────────────────────────────
# MULTI-TRACK CATALOG
#
# The curated IT list above is the "it" track (audience: internal). Three more
# tracks — the full week-by-week IT kit, the cross-functional Master checklist,
# and the Marketing toolkit — plus read-only reference (vendor contacts, systems
# grid, forms, the 45-day call, the IT quick-start) are generated from Kim's
# prototype HTML by build_onboarding_catalog.py and loaded from JSON here.
#
# task_status keys per task, so adding tracks needs no schema change as long as
# keys stay globally unique (curated keys are bare; imported keys are prefixed
# itf_/mstr_/mkt_).
# ─────────────────────────────────────────────────────────────────────────────

import json as _json  # noqa: E402

_CATALOG_JSON_PATH = os.path.join(os.path.dirname(__file__), "data", "onboarding", "catalog_data.json")

# Which tracks a franchisee (magic-link) may see, and in what order.
#
# The franchisee sees the 180 Day Checklist workbook and nothing else: "master"
# is its nine department sheets and "it" is its IT Department sheet, and the UI
# renders one tab per sheet. The two prototype-era tracks — the week-by-week
# "it_full" IT kit and the "marketing" toolkit — have no page in that workbook
# and are grouped separately, internal-side only, so the franchisee's navigation
# matches the spreadsheet exactly.
FRANCHISEE_TRACKS = ["master", "it"]
INTERNAL_TRACK_ORDER = ["it", "master", "it_full", "marketing"]

# Reference-block audience: these keys are hidden from franchisees entirely.
_INTERNAL_REFERENCE = {"systems"}
# Contact groups hidden from franchisees (matched case-insensitively on label).
# Escalation contacts ARE restaurant-facing — that vendor workbook is titled
# "Restaurant Facing – IT Vendor Contacts – NRO & Escalation" and the franchisee
# is the one who needs an escalation path. Billing stays internal (Newk's AP).
_INTERNAL_CONTACT_GROUPS = re.compile(r"billing", re.I)


def _normalize_curated() -> None:
    """Give the curated IT tasks the same track/group shape as imported tasks."""
    for t in ONBOARDING_TASKS:
        t.setdefault("track", "it")
        t["group"] = str(t["step"])
        t["group_label"] = STEP_TITLES.get(t["step"], "")


_normalize_curated()

# Public metadata for the curated IT track (mirrors the imported tracks' meta).
# This is the NRO IT Onboarding email turned into a tracked checklist — shown to
# both internal staff and franchisees (see FRANCHISEE_TRACKS).
_IT_TRACK_META = {
    "label": "IT Setup Checklist",
    "audience": "all",
    "blurb": "Your IT vendor & systems setup — the key accounts and agreements to "
             "sign, in the order we recommend tackling them.",
    "groups": [{"id": str(s), "label": STEP_TITLES[s], "order": s} for s in sorted(STEP_TITLES)],
}


def _load_catalog() -> Dict[str, Any]:
    try:
        with open(_CATALOG_JSON_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning("onboarding catalog_data.json not loaded: %s", exc)
        return {"tracks": {}, "reference": {}}


_CATALOG = _load_catalog()
_IMPORTED_TRACKS = _CATALOG.get("tracks", {})
REFERENCE: Dict[str, Any] = _CATALOG.get("reference", {})

# The "it" track now ships in catalog_data.json too, generated from the IT
# Department tab of Kim's 180 Day Checklist workbook by
# build_master_from_workbook.py. When it's there it supersedes the ONBOARDING_TASKS
# list above — that list stays as the fallback for a missing/short catalog file,
# and as the origin of the reviewed owner mapping the generator carries forward.
_IT_FROM_JSON = bool(_IMPORTED_TRACKS.get("it", {}).get("tasks"))

# Combined public track metadata (meta only — no task rows).
TRACK_META: Dict[str, Dict[str, Any]] = {"it": _IT_TRACK_META}
for _tr, _data in _IMPORTED_TRACKS.items():
    TRACK_META[_tr] = {k: v for k, v in _data.items() if k != "tasks"}

# Flat template of every task across every track.
ONBOARDING_TASKS_ALL: List[Dict[str, Any]] = [] if _IT_FROM_JSON else list(ONBOARDING_TASKS)
for _tr, _data in _IMPORTED_TRACKS.items():
    ONBOARDING_TASKS_ALL.extend(_data.get("tasks", []))

# Fast lookup: key -> template dict (spans all tracks).
_TASK_BY_KEY = {t["key"]: t for t in ONBOARDING_TASKS_ALL}
# "Required" progress stays scoped to the IT track (the other tracks carry no
# req/rec priority — they're tracked by done/total instead).
_IT_TASKS_LIVE = [t for t in ONBOARDING_TASKS_ALL if t.get("track") == "it"] or ONBOARDING_TASKS
_REQUIRED_KEYS = {t["key"] for t in _IT_TASKS_LIVE if t.get("prio") == "req"}


def task_template(task_key: str) -> Optional[Dict[str, Any]]:
    """The static template for a task key, or None if the key is unknown."""
    return _TASK_BY_KEY.get(task_key)


@contextmanager
def get_connection():
    """Yield a sqlite3 connection in WAL mode (concurrent-read-friendly)."""
    os.makedirs(os.path.dirname(ONBOARDING_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(ONBOARDING_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_available() -> bool:
    """Cheap connectivity smoke-test for health checks."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception as exc:
        logger.warning("onboarding_db unavailable: %s", exc)
        return False


def initialize_schema() -> None:
    """Create tables if they don't exist. Idempotent; called on app startup."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS openings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token          TEXT NOT NULL UNIQUE,   -- unguessable magic-link token
                store_name     TEXT NOT NULL,          -- "Newk's Eatery"
                city           TEXT,
                state          TEXT,
                target_open    TEXT,                   -- 'YYYY-MM-DD' or NULL
                franchisee_name  TEXT,
                franchisee_email TEXT,
                status         TEXT NOT NULL DEFAULT 'active',  -- active | archived
                created_by     TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
                invited_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS task_status (
                opening_id  INTEGER NOT NULL REFERENCES openings(id) ON DELETE CASCADE,
                task_key    TEXT NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0,
                done_by     TEXT,
                done_at     TEXT,
                note        TEXT,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (opening_id, task_key)
            );
            CREATE INDEX IF NOT EXISTS idx_task_status_opening ON task_status(opening_id);

            -- A franchisee (or staffer) asking about a specific task. Distinct
            -- from `task_status.note`: a question is addressed to someone, has
            -- an open/answered lifecycle, and notifies immediately.
            CREATE TABLE IF NOT EXISTS task_questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                opening_id   INTEGER NOT NULL REFERENCES openings(id) ON DELETE CASCADE,
                task_key     TEXT NOT NULL,
                body         TEXT NOT NULL,
                asked_by     TEXT,
                asked_at     TEXT NOT NULL DEFAULT (datetime('now')),
                answer       TEXT,
                answered_by  TEXT,
                answered_at  TEXT,
                status       TEXT NOT NULL DEFAULT 'open'   -- open | answered
            );
            CREATE INDEX IF NOT EXISTS idx_questions_opening ON task_questions(opening_id);
            CREATE INDEX IF NOT EXISTS idx_questions_status  ON task_questions(status);

            -- Append-only event feed powering the dashboard's activity panel and
            -- the end-of-day completion digest. `notified_at` is stamped once an
            -- event has been included in a digest, so digests never repeat.
            CREATE TABLE IF NOT EXISTS activity (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                opening_id  INTEGER NOT NULL REFERENCES openings(id) ON DELETE CASCADE,
                kind        TEXT NOT NULL,   -- task_done|task_undone|question|answer|invite
                task_key    TEXT,
                actor       TEXT,
                detail      TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                notified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_activity_opening ON activity(opening_id);
            CREATE INDEX IF NOT EXISTS idx_activity_pending
                ON activity(notified_at) WHERE notified_at IS NULL;
            """
        )
        conn.commit()


# ─── template helpers ────────────────────────────────────────────────────────

def catalog() -> Dict[str, Any]:
    """The static task template + track metadata + reference (labels for the UI)."""
    return {
        "tasks": ONBOARDING_TASKS,      # curated IT (back-compat)
        "steps": STEP_TITLES,           # curated IT groups (back-compat)
        "tracks": TRACK_META,
        "reference": REFERENCE,
    }


def _assemble_tasks(status_rows: List[sqlite3.Row],
                    questions: Optional[Dict[str, List[Dict[str, Any]]]] = None
                    ) -> List[Dict[str, Any]]:
    """Merge the code+JSON template with an opening's stored status rows."""
    by_key = {r["task_key"]: r for r in status_rows}
    questions = questions or {}
    out = []
    for t in ONBOARDING_TASKS_ALL:
        s = by_key.get(t["key"])
        qs = questions.get(t["key"], [])
        out.append({
            **t,
            "done": bool(s["done"]) if s else False,
            "done_by": s["done_by"] if s else None,
            "done_at": s["done_at"] if s else None,
            "note": (s["note"] if s else None),
            "questions": qs,
            "open_questions": sum(1 for q in qs if q["status"] == "open"),
        })
    return out


def _progress(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Overall + per-track done/total, plus the legacy required-task counters
    (still scoped to the curated IT track)."""
    by_track: Dict[str, Dict[str, int]] = {}
    for t in tasks:
        b = by_track.setdefault(t["track"], {"done": 0, "total": 0})
        b["total"] += 1
        if t["done"]:
            b["done"] += 1
    done = sum(b["done"] for b in by_track.values())
    total = sum(b["total"] for b in by_track.values())
    req_done = sum(1 for t in tasks if t.get("track") == "it" and t.get("prio") == "req" and t["done"])
    req_total = len(_REQUIRED_KEYS)
    return {
        "done": done,
        "total": total,
        "pct": round(done / total * 100) if total else 0,
        "by_track": {
            tr: {**b, "pct": round(b["done"] / b["total"] * 100) if b["total"] else 0}
            for tr, b in by_track.items()
        },
        "required_done": req_done,
        "required_total": req_total,
        "required_open": req_total - req_done,
    }


def _build_track_views(tasks: List[Dict[str, Any]], for_franchisee: bool) -> List[Dict[str, Any]]:
    """Group an opening's (already status-merged) tasks into track → group → task
    structures the UI renders directly. Franchisees see only their tracks, and
    tasks they don't own are flagged read-only (`locked`)."""
    order = FRANCHISEE_TRACKS if for_franchisee else INTERNAL_TRACK_ORDER
    views = []
    for tr in order:
        meta = TRACK_META.get(tr)
        if not meta:
            continue
        tks = [t for t in tasks if t.get("track") == tr]
        if not tks:
            continue
        if for_franchisee:
            for t in tks:
                t["locked"] = t.get("owner") != OWNER_FRANCHISEE
        groups = []
        for g in meta.get("groups", []):
            gitems = [t for t in tks if str(t.get("group")) == str(g["id"])]
            if gitems:
                groups.append({**g, "tasks": gitems})
        done = sum(1 for t in tks if t["done"])
        total = len(tks)
        views.append({
            "track": tr,
            "label": meta.get("label", tr),
            "blurb": meta.get("blurb", ""),
            "audience": meta.get("audience", "internal"),
            "accounts": meta.get("accounts", []),
            "done": done,
            "total": total,
            "pct": round(done / total * 100) if total else 0,
            "groups": groups,
        })
    return views


def _reference_view(for_franchisee: bool) -> Dict[str, Any]:
    """Reference blocks for the payload, filtered for the franchisee audience."""
    if not for_franchisee:
        return REFERENCE
    ref = {k: v for k, v in REFERENCE.items() if k not in _INTERNAL_REFERENCE}
    contacts = ref.get("contacts", {})
    ref["contacts"] = {
        k: v for k, v in contacts.items() if not _INTERNAL_CONTACT_GROUPS.search(k)
    }
    return ref


# ─── openings CRUD ───────────────────────────────────────────────────────────

def create_opening(data: Dict[str, Any], created_by: str) -> Dict[str, Any]:
    """Create an opening (with a fresh magic-link token). Tasks are seeded
    lazily — an opening with no status rows simply shows everything not-done —
    so there's nothing to insert here. Returns the new opening row."""
    token = secrets.token_urlsafe(32)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO openings
              (token, store_name, city, state, target_open,
               franchisee_name, franchisee_email, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                token,
                (data.get("store_name") or "Newk's Eatery").strip(),
                (data.get("city") or "").strip() or None,
                (data.get("state") or "").strip() or None,
                (data.get("target_open") or "").strip() or None,
                (data.get("franchisee_name") or "").strip() or None,
                (data.get("franchisee_email") or "").strip() or None,
                created_by,
            ),
        )
        conn.commit()
        return get_opening_by_id(int(cur.lastrowid))


def list_openings(include_archived: bool = False) -> List[Dict[str, Any]]:
    """All openings with computed progress, soonest-to-open first."""
    with get_connection() as conn:
        where = "" if include_archived else "WHERE o.status = 'active'"
        rows = conn.execute(f"SELECT * FROM openings o {where}").fetchall()
        status = conn.execute("SELECT * FROM task_status").fetchall()
        qcounts = dict(conn.execute(
            "SELECT opening_id, COUNT(*) FROM task_questions "
            "WHERE status='open' GROUP BY opening_id"
        ).fetchall())
    by_opening: Dict[int, List[sqlite3.Row]] = {}
    for s in status:
        by_opening.setdefault(s["opening_id"], []).append(s)
    out = []
    for o in rows:
        tasks = _assemble_tasks(by_opening.get(o["id"], []))
        d = dict(o)
        d.pop("token", None)  # never leak the magic-link token to the list view
        d["progress"] = _progress(tasks)
        d["open_questions"] = qcounts.get(o["id"], 0)
        out.append(d)
    # Sort: openings with a target date first (soonest first), undated last.
    out.sort(key=lambda x: (x["target_open"] is None, x["target_open"] or ""))
    return out


def _hydrate(o: sqlite3.Row, conn: sqlite3.Connection,
             for_franchisee: bool = False) -> Dict[str, Any]:
    status = conn.execute(
        "SELECT * FROM task_status WHERE opening_id = ?", (o["id"],)
    ).fetchall()
    qrows = conn.execute(
        "SELECT * FROM task_questions WHERE opening_id = ? ORDER BY asked_at",
        (o["id"],),
    ).fetchall()
    by_task: Dict[str, List[Dict[str, Any]]] = {}
    for q in qrows:
        by_task.setdefault(q["task_key"], []).append(dict(q))
    tasks = _assemble_tasks(status, by_task)
    track_views = _build_track_views(tasks, for_franchisee)
    # Progress is computed over exactly what this audience can see.
    visible = [t for grp in track_views for g in grp["groups"] for t in g["tasks"]]
    d = dict(o)
    d["tasks"] = tasks                       # flat list (back-compat / internal)
    d["track_views"] = track_views           # grouped, render-ready
    d["reference"] = _reference_view(for_franchisee)
    d["progress"] = _progress(visible if for_franchisee else tasks)
    d["steps"] = STEP_TITLES
    d["open_questions"] = sum(1 for q in qrows if q["status"] == "open")
    return d


def get_opening_by_id(opening_id: int) -> Optional[Dict[str, Any]]:
    """Full opening incl. token + tasks (internal view)."""
    with get_connection() as conn:
        o = conn.execute("SELECT * FROM openings WHERE id = ?", (opening_id,)).fetchone()
        return _hydrate(o, conn) if o else None


def get_opening_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Full opening + tasks resolved by magic-link token (franchisee view).
    The token itself is not echoed back."""
    if not token:
        return None
    with get_connection() as conn:
        o = conn.execute("SELECT * FROM openings WHERE token = ?", (token,)).fetchone()
        if not o:
            return None
        d = _hydrate(o, conn, for_franchisee=True)
        d.pop("token", None)
        return d


def update_opening(opening_id: int, data: Dict[str, Any]) -> bool:
    """Update the opening's descriptive fields (internal only)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE openings
               SET store_name = ?, city = ?, state = ?, target_open = ?,
                   franchisee_name = ?, franchisee_email = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (
                (data.get("store_name") or "Newk's Eatery").strip(),
                (data.get("city") or "").strip() or None,
                (data.get("state") or "").strip() or None,
                (data.get("target_open") or "").strip() or None,
                (data.get("franchisee_name") or "").strip() or None,
                (data.get("franchisee_email") or "").strip() or None,
                opening_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def archive_opening(opening_id: int) -> bool:
    """Soft-delete: hide from the active portfolio but keep the record."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE openings SET status='archived', updated_at=datetime('now') WHERE id = ?",
            (opening_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_invited(opening_id: int) -> None:
    """Stamp the time an invite was (re)sent."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE openings SET invited_at=datetime('now'), updated_at=datetime('now') WHERE id = ?",
            (opening_id,),
        )
        conn.commit()


def rotate_token(opening_id: int) -> Optional[str]:
    """Issue a fresh magic-link token (invalidates the old link)."""
    token = secrets.token_urlsafe(32)
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE openings SET token=?, updated_at=datetime('now') WHERE id = ?",
            (token, opening_id),
        )
        conn.commit()
        return token if cur.rowcount > 0 else None


def get_token(opening_id: int) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT token FROM openings WHERE id = ?", (opening_id,)).fetchone()
        return row["token"] if row else None


# ─── task status ─────────────────────────────────────────────────────────────

def set_task(opening_id: int, task_key: str, *, done: Optional[bool] = None,
             note: Optional[str] = None, actor: str = "",
             restrict_owner: Optional[str] = None) -> bool:
    """Upsert a task's status/note for an opening.

    `restrict_owner` (when set) enforces that the task's template owner matches —
    used by the franchisee endpoint so a franchisee can only touch their own
    tasks. Returns True on success, False if the key is unknown or the
    owner restriction fails. `done`/`note` are each applied only when provided.
    """
    tmpl = _TASK_BY_KEY.get(task_key)
    if not tmpl:
        return False
    if restrict_owner is not None and tmpl["owner"] != restrict_owner:
        return False

    with get_connection() as conn:
        row = conn.execute(
            "SELECT done, note FROM task_status WHERE opening_id=? AND task_key=?",
            (opening_id, task_key),
        ).fetchone()
        cur_done = bool(row["done"]) if row else False
        cur_note = row["note"] if row else None

        new_done = cur_done if done is None else bool(done)
        new_note = cur_note if note is None else (note.strip() or None)
        done_at = "datetime('now')" if new_done else "NULL"
        done_by = actor if new_done else None

        conn.execute(
            f"""
            INSERT INTO task_status (opening_id, task_key, done, done_by, done_at, note, updated_at)
            VALUES (?, ?, ?, ?, {done_at}, ?, datetime('now'))
            ON CONFLICT(opening_id, task_key) DO UPDATE SET
                done = excluded.done,
                done_by = CASE WHEN excluded.done = 1 THEN excluded.done_by ELSE NULL END,
                done_at = CASE WHEN excluded.done = 1 THEN datetime('now') ELSE NULL END,
                note = excluded.note,
                updated_at = datetime('now')
            """,
            (opening_id, task_key, 1 if new_done else 0, done_by, new_note),
        )
        conn.execute(
            "UPDATE openings SET updated_at=datetime('now') WHERE id=?", (opening_id,)
        )
        # Log only real transitions — re-saving a note shouldn't look like
        # progress, and the digest counts these rows.
        if new_done != cur_done:
            conn.execute(
                """INSERT INTO activity (opening_id, kind, task_key, actor, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (opening_id, "task_done" if new_done else "task_undone",
                 task_key, actor or None, tmpl.get("title", task_key)),
            )
        conn.commit()
        return True


# ─── questions ───────────────────────────────────────────────────────────────

def add_question(opening_id: int, task_key: str, body: str,
                 asked_by: str = "") -> Optional[Dict[str, Any]]:
    """Raise a question against a task. Returns the stored row (with the task
    title resolved) so the caller can build a notification, or None if the task
    key is unknown or the body is empty."""
    tmpl = _TASK_BY_KEY.get(task_key)
    if not tmpl:
        return None
    body = (body or "").strip()
    if not body:
        return None
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO task_questions (opening_id, task_key, body, asked_by)
               VALUES (?, ?, ?, ?)""",
            (opening_id, task_key, body, asked_by or None),
        )
        qid = int(cur.lastrowid)
        # notified_at is stamped at insert: a question emails out immediately,
        # so it must never also turn up in the end-of-day digest.
        conn.execute(
            """INSERT INTO activity (opening_id, kind, task_key, actor, detail, notified_at)
               VALUES (?, 'question', ?, ?, ?, datetime('now'))""",
            (opening_id, task_key, asked_by or None, body[:200]),
        )
        conn.execute(
            "UPDATE openings SET updated_at=datetime('now') WHERE id=?", (opening_id,)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM task_questions WHERE id=?", (qid,)).fetchone()
    d = dict(row)
    d["task_title"] = tmpl.get("title", task_key)
    d["track"] = tmpl.get("track")
    d["group"] = tmpl.get("group")
    return d


def answer_question(question_id: int, answer: str, answered_by: str) -> Optional[Dict[str, Any]]:
    """Record an internal answer and close the question. Returns the updated row
    (with task title + the opening's franchisee details) so the caller can reply
    by email, or None if the id is unknown or already answered."""
    answer = (answer or "").strip()
    if not answer:
        return None
    with get_connection() as conn:
        q = conn.execute("SELECT * FROM task_questions WHERE id=?", (question_id,)).fetchone()
        if not q or q["status"] == "answered":
            return None
        conn.execute(
            """UPDATE task_questions
                  SET answer=?, answered_by=?, answered_at=datetime('now'), status='answered'
                WHERE id=?""",
            (answer, answered_by or None, question_id),
        )
        conn.execute(
            """INSERT INTO activity (opening_id, kind, task_key, actor, detail, notified_at)
               VALUES (?, 'answer', ?, ?, ?, datetime('now'))""",
            (q["opening_id"], q["task_key"], answered_by or None, answer[:200]),
        )
        conn.commit()
        row = conn.execute(
            """SELECT tq.*, o.store_name, o.city, o.state,
                      o.franchisee_name, o.franchisee_email
                 FROM task_questions tq
                 JOIN openings o ON o.id = tq.opening_id
                WHERE tq.id = ?""",
            (question_id,),
        ).fetchone()
    d = dict(row)
    d["task_title"] = (_TASK_BY_KEY.get(d["task_key"]) or {}).get("title", d["task_key"])
    return d


def list_questions(opening_id: Optional[int] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Questions, newest first. Filter by opening and/or status ('open')."""
    clauses, params = [], []
    if opening_id is not None:
        clauses.append("tq.opening_id = ?")
        params.append(opening_id)
    if status:
        clauses.append("tq.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT tq.*, o.store_name, o.city, o.state, o.franchisee_name
                  FROM task_questions tq
                  JOIN openings o ON o.id = tq.opening_id
                  {where}
                 ORDER BY tq.asked_at DESC""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        tmpl = _TASK_BY_KEY.get(d["task_key"]) or {}
        d["task_title"] = tmpl.get("title", d["task_key"])
        d["track"] = tmpl.get("track")
        d["group"] = tmpl.get("group")
        out.append(d)
    return out


def questions_for_opening(opening_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """All of an opening's questions, bucketed by task_key for the UI."""
    by_task: Dict[str, List[Dict[str, Any]]] = {}
    for q in list_questions(opening_id=opening_id):
        by_task.setdefault(q["task_key"], []).append(q)
    for v in by_task.values():
        v.sort(key=lambda x: x["asked_at"])
    return by_task


# ─── activity feed & digest ──────────────────────────────────────────────────

def recent_activity(limit: int = 60, opening_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Newest-first event feed for the internal dashboard."""
    where, params = "", []
    if opening_id is not None:
        where = "WHERE a.opening_id = ?"
        params.append(opening_id)
    params.append(int(limit))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT a.*, o.store_name, o.city, o.state
                  FROM activity a
                  JOIN openings o ON o.id = a.opening_id
                  {where}
                 ORDER BY a.id DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def pending_digest_events() -> Dict[int, Dict[str, Any]]:
    """Un-notified completion events, grouped by opening, for the daily digest.

    Only task_done / task_undone appear here — questions and answers are sent
    the moment they happen and are stamped notified at insert time."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT a.*, o.store_name, o.city, o.state, o.target_open,
                      o.franchisee_name
                 FROM activity a
                 JOIN openings o ON o.id = a.opening_id
                WHERE a.notified_at IS NULL
                  AND a.kind IN ('task_done','task_undone')
                  AND o.status = 'active'
                ORDER BY a.id"""
        ).fetchall()
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        b = out.setdefault(d["opening_id"], {
            "opening_id": d["opening_id"],
            "store_name": d["store_name"],
            "city": d["city"],
            "state": d["state"],
            "target_open": d["target_open"],
            "franchisee_name": d["franchisee_name"],
            "events": [],
        })
        b["events"].append(d)
    return out


def mark_digest_sent(event_ids: List[int]) -> int:
    """Stamp events as included in a digest so the next run skips them."""
    if not event_ids:
        return 0
    with get_connection() as conn:
        qs = ",".join("?" for _ in event_ids)
        cur = conn.execute(
            f"UPDATE activity SET notified_at=datetime('now') WHERE id IN ({qs})",
            event_ids,
        )
        conn.commit()
        return cur.rowcount
