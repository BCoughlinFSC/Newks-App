"""
build_onboarding_catalog.py — one-time generator for the NRO onboarding catalog.

Parses the three "opening" HTML prototypes Kim built (Franchisee Opening Hub,
IT NRO Kit Internal, Marketing Opening Toolkit), normalizes their embedded task
data into the app's track/group/task shape, applies the content rules the boss
signed off on, and writes a committed JSON the app loads at runtime:

    backend/data/onboarding/catalog_data.json

Content rules applied here (see plan clever-painting-hummingbird.md):
  • Grubbrr removed everywhere (tasks, the OLO-Pay note clause, contacts row).
  • FSC Managed Network language is canonical — the old ISS / MD-Tech / "Managed
    Network Partner" references are rewritten to FSC-managed wording so a
    franchisee sees one consistent, current policy.
  • Owner mapping (franchisee vs newks) drives who can tick a task.

The three HTML files are a BUILD-TIME input only (default: the user's Downloads
folder). The generated JSON is the runtime source of truth — the app never reads
the HTML. To regenerate, re-run this script with the source files present:

    python backend/build_onboarding_catalog.py [--src <dir>]

This is intentionally dependency-free (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

SRC_FILES = {
    "hub": "Newk's Franchisee Opening Hub.html",
    "it": "Newk's IT NRO Kit (Internal).html",
    "mkt": "Newk's Marketing Opening Toolkit.html",
}

OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "onboarding", "catalog_data.json")

# FSC contact used to replace the retired ISS network-security vendor rows.
FSC_IT_EMAIL = "itdepartment@newks.com"


# ─── extraction ──────────────────────────────────────────────────────────────

def extract_D(path: str) -> dict:
    """Pull the embedded `const D = {...}` object out of a prototype HTML file
    via brace matching (the payload is minified onto one line)."""
    txt = open(path, encoding="utf-8").read()
    i = txt.index("const D")
    j = txt.index("{", i)
    depth = 0
    k = j
    instr = False
    esc = False
    q = ""
    while k < len(txt):
        ch = txt[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == q:
                instr = False
        else:
            if ch in "\"'":
                instr = True
                q = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        k += 1
    return json.loads(txt[j:k + 1])


# ─── content transforms ──────────────────────────────────────────────────────

GRUBBRR_RE = re.compile(r"grubbrr", re.I)
GRUBBRR_NOTE_RE = re.compile(r"\s*[-–]\s*Note this is important for the Grubbrr Signup Process\.?", re.I)

# Report of every content rewrite, printed at the end for Kim to eyeball.
REPORT: list[str] = []


def report(msg: str) -> None:
    REPORT.append(msg)


def strip_grubbrr_note(text: str) -> str:
    return GRUBBRR_NOTE_RE.sub("", text)


# Ordered, explicit network rewrites keyed by a stable substring of the original
# text. Each entry: (match_substring, full_replacement_text, new_contact_dict|None)
# Applied to a task's primary text field; when new_contact is given, the task's
# company/contact/contactInfo are replaced too.
FSC_NETWORK = "FSC Managed Network"
FSC_CONTACT = {"company": "FSC IT", "contact": "FSC IT", "contactInfo": FSC_IT_EMAIL}

MASTER_REWRITES = {
    "Contact Managed Network Partner to Provide Hardware Firewall": (
        "Contact FSC IT to arrange the FSC Managed Network. FSC IT provides and "
        "manages the network equipment, wireless networks, and cellular backup "
        "connectivity for Toast (included in the franchise agreement). Contact FSC "
        "IT early to coordinate equipment placement, installation, and low-voltage cabling.",
        FSC_CONTACT,
    ),
    "Follow up with ISS - on Internet Connectivity": (
        "Confirm the FSC Managed Network install date, and confirm install dates "
        "with your local high-speed internet and voice/telephone providers.",
        FSC_CONTACT,
    ),
}

ITFULL_REWRITES = {
    "Contact Managed Network Partner to Provide Hardware [Managed Network] for POS": (
        "Contact FSC IT — the FSC Managed Network provides and manages the network "
        "hardware for POS (included in the franchise agreement).",
        None,
    ),
}

# Substring replacements applied broadly to tracked task text + reference text.
TEXT_SUBS = [
    ("Approved Firewall Vendor (ISS)", FSC_NETWORK),
    ("Managed Network Partner", FSC_NETWORK),
    ("ISS Network ordered and scheduled (ISP, Phone, and Firewall)",
     "FSC Managed Network ordered and scheduled (network, internet, phone & firewall)"),
]
# Catch-all word-boundary neutralization of the retired vendor tokens.
TOKEN_SUBS = [
    (re.compile(r"Interface Security Systems", re.I), FSC_NETWORK),
    (re.compile(r"Interface Systems", re.I), FSC_NETWORK),
    (re.compile(r"\bISS\b"), FSC_NETWORK),
    (re.compile(r"\bMD Tech\b", re.I), FSC_NETWORK),
]


def apply_text_subs(text: str) -> str:
    if not text:
        return text
    for a, b in TEXT_SUBS:
        text = text.replace(a, b)
    for rx, b in TOKEN_SUBS:
        text = rx.sub(b, text)
    return text


# ─── owner mapping ───────────────────────────────────────────────────────────

MKT_FRANCHISEE_RE = re.compile(r"^(direct store|ask store|confirm with store|ask the store)", re.I)


def owner_master(tags: list) -> str:
    return "franchisee" if any(t.lower() == "franchisee" for t in (tags or [])) else "newks"


def owner_itfull(who: str) -> str:
    return "franchisee" if "franchise" in (who or "").lower() else "newks"


def owner_marketing(activity: str) -> str:
    return "franchisee" if MKT_FRANCHISEE_RE.search(activity or "") else "newks"


# ─── track builders ──────────────────────────────────────────────────────────

def build_master(hub: dict) -> dict:
    phases = hub["master"]["phases"]
    groups = [
        {"id": str(k), "label": phases[k]["title"], "range": phases[k].get("range", ""),
         "blurb": phases[k].get("blurb", ""), "order": int(k)}
        for k in sorted(phases, key=lambda x: int(x))
    ]
    tasks = []
    dropped = 0
    for i, t in enumerate(hub["master"]["tasks"]):
        line = t.get("line") if isinstance(t.get("line"), int) else i + 1
        # Strip the Grubbrr note-clause first so the OLO-Pay task survives; only
        # tasks that are *about* Grubbrr after that are dropped wholesale.
        activity = strip_grubbrr_note(t.get("activity", ""))
        if GRUBBRR_RE.search(activity):
            dropped += 1
            report(f"[master] DROPPED (Grubbrr): {activity[:70]}")
            continue
        comp, cont, cinfo = t.get("company", ""), t.get("contact", ""), t.get("contactInfo", "")
        for sub, (repl, newc) in MASTER_REWRITES.items():
            if sub in activity:
                report(f"[master] FSC rewrite: {activity[:60]}...")
                activity = repl
                if newc:
                    comp, cont, cinfo = newc["company"], newc["contact"], newc["contactInfo"]
        activity = apply_text_subs(activity)
        comp, cinfo = apply_text_subs(comp), apply_text_subs(cinfo)
        tasks.append({
            "key": f"mstr_{line:03d}",
            "track": "master",
            "group": str(t["phase"]),
            "order": line,
            "title": activity,
            "owner": owner_master(t.get("tags")),
            "dept": t.get("resource", ""),
            "tags": t.get("tags", []),
            "lead": t.get("weeksLabel", ""),
            "desc": "",
            "contact": cont,
            "contact_info": cinfo,
            "company": comp,
            "kb": "",
            "form_url": "",
        })
    report(f"[master] {len(tasks)} tasks ({dropped} dropped)")
    return {"label": "Master Opening Checklist", "audience": "franchisee",
            "blurb": "The full cross-functional 22-week opening plan.",
            "infoFields": hub["master"].get("infoFields", []),
            "groups": groups, "tasks": tasks}


def build_itfull(it: dict) -> dict:
    seen, groups = set(), []
    for t in it["ittasks"]:
        s = t["section"]
        if s not in seen:
            seen.add(s)
            groups.append({"id": s, "label": s.title() if s.isupper() else s,
                           "order": len(groups)})
    tasks = []
    dropped = 0
    for i, t in enumerate(it["ittasks"]):
        task = t.get("task", "")
        if GRUBBRR_RE.search(task):
            dropped += 1
            report(f"[it_full] DROPPED (Grubbrr): {task[:70]}")
            continue
        for sub, (repl, _n) in ITFULL_REWRITES.items():
            if sub in task:
                report(f"[it_full] FSC rewrite: {task[:60]}...")
                task = repl
        task = apply_text_subs(task)
        tasks.append({
            "key": f"itf_{i:03d}",
            "track": "it_full",
            "group": t["section"],
            "order": i,
            "title": task,
            "owner": owner_itfull(t.get("who", "")),
            "dept": t.get("who", ""),
            "applies": t.get("applies", ""),
            "lead": f"{t.get('wks', '')} wks" if str(t.get("wks", "")) != "" else "",
            "desc": apply_text_subs(t.get("notes", "")),
            "contact": "",
            "contact_info": apply_text_subs(t.get("vendor", "")),
            "kb": t.get("kb", ""),
            "form_url": t.get("form_url", ""),
        })
    report(f"[it_full] {len(tasks)} tasks ({dropped} dropped)")
    return {"label": "IT NRO Kit (Full)", "audience": "internal",
            "blurb": "The complete week-by-week IT onboarding checklist.",
            "groups": groups, "tasks": tasks}


def build_marketing(mkt: dict) -> dict:
    phases = mkt["phases"]
    groups = [
        {"id": str(k), "label": phases[k]["title"], "range": phases[k].get("range", ""),
         "blurb": phases[k].get("blurb", ""), "order": int(k)}
        for k in sorted(phases, key=lambda x: int(x))
    ]
    tasks = []
    for i, t in enumerate(mkt["tasks"]):
        activity = apply_text_subs(t.get("activity", ""))
        tasks.append({
            "key": f"mkt_{i:03d}",
            "track": "marketing",
            "group": str(t["phase"]),
            "order": i,
            "title": activity,
            "owner": owner_marketing(t.get("activity", "")),
            "dept": "Marketing",
            "lead": t.get("daysLabel", ""),
            "is_call": bool(t.get("isCall")),
            "desc": apply_text_subs(t.get("notes", "")),
            "contact": "",
            "contact_info": "",
            "kb": "",
            "form_url": "",
        })
    report(f"[marketing] {len(tasks)} tasks")
    return {"label": "Marketing Opening Toolkit", "audience": "franchisee",
            "blurb": "The marketing playbook — 126 days out through training week.",
            "infoFields": mkt.get("infoFields", []),
            "accounts": mkt.get("accounts", []),
            "groups": groups, "tasks": tasks}


# ─── reference builders ──────────────────────────────────────────────────────

def clean_contacts(contacts: dict) -> dict:
    """Drop Grubbrr rows; replace the retired ISS network-security vendor rows
    with a single FSC IT — FSC Managed Network contact."""
    out = {}
    for gk, gv in contacts.items():
        rows, fsc_added, dropped = [], False, 0
        for r in gv.get("rows", []):
            vendor = str(r.get("Vendor", ""))
            if GRUBBRR_RE.search(vendor):
                dropped += 1
                report(f"[contacts:{gk}] DROPPED (Grubbrr): {vendor}")
                continue
            if re.search(r"\bISS\b|Interface Systems", vendor, re.I):
                if not fsc_added:
                    rows.append({
                        "cat": "Network Security",
                        "Vendor": "FSC IT — FSC Managed Network",
                        "Contact Name": "FSC IT Department",
                        "Role": "Managed Network (network, Wi-Fi, cellular backup)",
                        "Email": FSC_IT_EMAIL,
                        "Category": "Network Security",
                        "Required vs Optional": "Required",
                    })
                    fsc_added = True
                    report(f"[contacts:{gk}] ISS vendor rows -> FSC IT — FSC Managed Network")
                continue
            # neutralize any stray ISS text inside kept rows
            r = {k: (apply_text_subs(v) if isinstance(v, str) else v) for k, v in r.items()}
            rows.append(r)
        out[gk] = {"headers": gv.get("headers", []), "rows": rows}
    return out


def clean_systems(systems: list) -> list:
    out = []
    for s in systems:
        if GRUBBRR_RE.search(json.dumps(s)):
            report(f"[systems] DROPPED (Grubbrr): {s.get('name')}")
            continue
        out.append({k: (apply_text_subs(v) if isinstance(v, str) else v) for k, v in s.items()})
    return out


def clean_call45(call45: dict) -> dict:
    secs = []
    for s in call45.get("sections", []):
        items = []
        for it0 in s.get("items", []):
            txt = it0.get("text", "")
            if GRUBBRR_RE.search(txt):
                report(f"[call45] DROPPED (Grubbrr): {txt[:60]}")
                continue
            items.append({"text": apply_text_subs(txt), "yn": bool(it0.get("yn"))})
        secs.append({"title": s.get("title", ""), "items": items})
    return {"sections": secs}


def clean_itquick(itquick: list) -> list:
    out = []
    for g in itquick:
        items = []
        for i0 in g.get("items", []):
            if GRUBBRR_RE.search(json.dumps(i0)):
                report(f"[itquick] DROPPED (Grubbrr): {i0.get('name')}")
                continue
            items.append({k: (apply_text_subs(v) if isinstance(v, str) else v)
                          for k, v in i0.items()})
        out.append({"title": g.get("title", ""), "items": items})
    return out


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    default_src = os.path.join(os.path.expanduser("~"), "Downloads")
    ap.add_argument("--src", default=default_src, help="dir holding the 3 prototype HTML files")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    paths = {tag: os.path.join(args.src, fn) for tag, fn in SRC_FILES.items()}
    missing = [p for p in paths.values() if not os.path.exists(p)]
    if missing:
        print("ERROR: source HTML file(s) not found:", *missing, sep="\n  ")
        print(f"\nPass --src <dir> pointing at the folder with:\n  " +
              "\n  ".join(SRC_FILES.values()))
        return 2

    hub = extract_D(paths["hub"])
    it = extract_D(paths["it"])
    mkt = extract_D(paths["mkt"])

    catalog = {
        "_generated_by": "build_onboarding_catalog.py",
        "tracks": {
            "master": build_master(hub),
            "it_full": build_itfull(it),
            "marketing": build_marketing(mkt),
        },
        "reference": {
            "contacts": clean_contacts(it.get("contacts", {})),
            "systems": clean_systems(it.get("systems", [])),
            "forms": hub.get("forms", []),
            "call45": clean_call45(hub.get("call45", {})),
            "itquick": clean_itquick(hub.get("itquick", [])),
        },
    }

    # ── self-check: no Grubbrr / no live retired-vendor tokens in tracked text ──
    blob = json.dumps(catalog, ensure_ascii=False)
    problems = []
    if GRUBBRR_RE.search(blob):
        problems.append("Grubbrr still present")
    for rx, label in [(re.compile(r"\bISS\b"), "ISS"),
                      (re.compile(r"Interface S", re.I), "Interface Systems"),
                      (re.compile(r"\bMD Tech\b", re.I), "MD Tech")]:
        if rx.search(blob):
            problems.append(f"{label} still present")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)

    print("\n".join(REPORT))
    print("\n" + "=" * 60)
    counts = {k: len(v["tasks"]) for k, v in catalog["tracks"].items()}
    print("Track task counts:", counts)
    ref = catalog["reference"]
    print("Reference: contacts groups=%d, systems=%d, forms=%d, call45 sections=%d, itquick groups=%d"
          % (len(ref["contacts"]), len(ref["systems"]), len(ref["forms"]),
             len(ref["call45"]["sections"]), len(ref["itquick"])))
    print("Wrote:", args.out)
    if problems:
        print("\n*** SELF-CHECK FAILED:", "; ".join(problems))
        return 1
    print("Self-check OK: no Grubbrr / no retired-vendor tokens remain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
