#!/usr/bin/env python3
"""
build_group_report.py -- Toast location groups x store ownership, as one HTML report.

Reads every .txt in ./pastes/ and joins the membership to backend/store_ownership.json
(the Company / Franchise designation the app already treats as authoritative).

Input accepted, per file:
  * canonical   "== <group name>" then "STORE: ..." / "GROUP: <name> | <desc>"
  * RAW ToastWeb paste -- the group page copied straight out of the browser:
        <group name>
        Edit info
        Add
        Name / Description / Groups / Locations      <- table header
        <member name>
        <description>
        <groups count>            "-" for a store, a number for a child group
        <locations count>
        ...
    so pastes can be dropped in verbatim without reformatting.

Store numbers are pulled from "City, ST - 1234 - Newk's ..." and tolerate the
"z.Closed.1144" prefix Toast uses for closed locations.

Output: group_ownership_report.html
"""

import json
import re
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASTES = HERE / "pastes"
OWNERSHIP = HERE.parent / "store_ownership.json"
OUT = HERE / "group_ownership_report.html"

# "City, ST - 1234 - Newk's ..."  and  "City - z.Closed.1144 - Newk's ..."
STORE_RE = re.compile(r"-\s*(z\.Closed\.)?(\d{3,5})\s*-", re.I)

NOISE = {
    "edit info", "add", "name", "description", "groups", "locations",
    "welcome to newk's eatery!", "-", "",
}


def parse_canonical(lines):
    groups, cur = {}, None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("== "):
            cur = line[3:].strip()
            groups.setdefault(cur, {"stores": [], "child_groups": []})
        elif line.startswith("STORE:") and cur:
            m = STORE_RE.search(line)
            if m:
                groups[cur]["stores"].append(
                    {"store": m.group(2), "closed": bool(m.group(1)),
                     "raw": line[len("STORE:"):].strip()}
                )
        elif line.startswith("GROUP:") and cur:
            groups[cur]["child_groups"].append(line[len("GROUP:"):].strip())
    return groups


def parse_raw(lines):
    """Parse a ToastWeb group page copied straight from the browser."""
    clean = [l.strip() for l in lines if l.strip()]
    groups, i = {}, 0
    while i < len(clean):
        # a group header is the line immediately followed by "Edit info"
        if i + 1 < len(clean) and clean[i + 1].lower() == "edit info":
            name = clean[i]
            groups.setdefault(name, {"stores": [], "child_groups": []})
            j = i + 2
            while j < len(clean) and clean[j].lower() != "locations":
                j += 1
            j += 1                                    # past the table header
            while j < len(clean):
                if j + 1 < len(clean) and clean[j + 1].lower() == "edit info":
                    break                             # next group starts here
                line = clean[j]
                m = STORE_RE.search(line)
                if m:
                    groups[name]["stores"].append(
                        {"store": m.group(2), "closed": bool(m.group(1)), "raw": line}
                    )
                    j += 1
                    continue
                if line.lower() in NOISE or line.isdigit():
                    j += 1
                    continue
                # a child-group record: name, description, <count>, <count>
                if (j + 3 < len(clean) and clean[j + 2].isdigit()
                        and clean[j + 3].isdigit()):
                    groups[name]["child_groups"].append(f"{line} | {clean[j + 1]}")
                    j += 4
                    continue
                j += 1
            i = j
            continue
        i += 1
    return groups


def load_groups():
    merged = defaultdict(lambda: {"stores": [], "child_groups": []})
    for f in sorted(PASTES.glob("*.txt")):
        lines = f.read_text(encoding="utf-8").splitlines()
        parsed = parse_canonical(lines) if any(l.startswith("== ") for l in lines) \
            else parse_raw(lines)
        for g, data in parsed.items():
            seen = {s["store"] for s in merged[g]["stores"]}
            for s in data["stores"]:
                if s["store"] not in seen:
                    merged[g]["stores"].append(s)
                    seen.add(s["store"])
            for c in data["child_groups"]:
                if c not in merged[g]["child_groups"]:
                    merged[g]["child_groups"].append(c)
    return dict(merged)


def main() -> int:
    if not PASTES.exists():
        print(f"missing {PASTES}")
        return 1
    own = json.loads(OWNERSHIP.read_text(encoding="utf-8")) if OWNERSHIP.exists() else {}
    groups = load_groups()
    if not groups:
        print("no groups parsed -- is pastes/ empty?")
        return 1

    def info(num):
        rec = own.get(num) or {}
        return (rec.get("ownershipType") or "unknown",
                rec.get("companyName") or "",
                rec.get("restaurantName") or "")

    # ---- per-group rollup -----------------------------------------------------
    rows, flags = [], []
    store_groups = defaultdict(list)
    for gname in sorted(groups):
        members = groups[gname]["stores"]
        counts = defaultdict(int)
        for s in members:
            counts[info(s["store"])[0]] += 1
            store_groups[s["store"]].append(gname)
        rows.append((gname, members, dict(counts), groups[gname]["child_groups"]))

        is_co = "company only" in gname.lower()
        franchise = [s["store"] for s in members if info(s["store"])[0] == "Franchise"]
        unknown = [s["store"] for s in members if info(s["store"])[0] == "unknown"]
        if is_co and franchise:
            flags.append(("error", f'"{gname}" is named Company Only but contains '
                                   f'{len(franchise)} FRANCHISE store(s): {", ".join(sorted(franchise))}'))
        if unknown:
            flags.append(("warn", f'"{gname}" has {len(unknown)} store(s) not in '
                                  f'store_ownership.json: {", ".join(sorted(unknown))}'))

    # ---- company stores that are in NO Company Only group ---------------------
    co_members = set()
    for gname, members, _c, _cg in rows:
        if "company only" in gname.lower():
            co_members |= {s["store"] for s in members}
    all_company = {k for k, v in own.items() if (v.get("ownershipType") or "") == "Company"}
    missing_co = sorted(all_company - co_members)

    # ---- a store in more than one plain Dine In tier --------------------------
    def is_plain_tier(g):
        return bool(re.match(r"^\s*[\d.]*\s*-?\s*Tier\s*\d+\s*-\s*Dine In\s*$", g, re.I))
    multi = {s: [g for g in gs if is_plain_tier(g)]
             for s, gs in store_groups.items()
             if len([g for g in gs if is_plain_tier(g)]) > 1}
    for s, gs in sorted(multi.items()):
        flags.append(("error", f"store {s} is in {len(gs)} plain Dine In tiers: {', '.join(gs)}"))

    # ---- every store that appears in more than one group ----------------------
    # A store legitimately appears twice when it is in "Tier N - Dine In" AND
    # "Tier N - Dine In - Company Only" -- same tier, so no conflict. Anything else
    # (two different tier numbers) is worth a look.
    def tier_no(g):
        m = re.search(r"tier\s*(\d+)", g, re.I)
        return m.group(1) if m else None

    multi_rows = []
    for s, gs in sorted(store_groups.items()):
        if len(gs) < 2:
            continue
        nums = {tier_no(g) for g in gs}
        unresolved = None in nums
        distinct = {n for n in nums if n is not None}
        if not unresolved and len(distinct) == 1:
            verdict, cls = f"expected — Tier {distinct.pop()} plain + Company Only", "ok"
        else:
            verdict, cls = "REVIEW — spans different tiers", "bad"
            flags.append(("error", f"store {s} ({info(s)[2]}) is in groups from different "
                                   f"tiers: {', '.join(gs)}"))
        multi_rows.append((s, gs, verdict, cls))

    # ---- HTML -----------------------------------------------------------------
    def badge(kind):
        colour = {"Company": "#1a7f37", "Franchise": "#9a3412", "unknown": "#6b7280"}[kind]
        return f'<span style="color:{colour};font-weight:600">{kind}</span>'

    parts = []
    for gname, members, counts, child in rows:
        co = "company only" in gname.lower()
        head_bg = "#eef6ff" if co else "#f7f7f7"
        summary = " · ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no stores"
        body = []
        for s in sorted(members, key=lambda m: m["store"]):
            o, ent, nm = info(s["store"])
            body.append(
                f"<tr><td>{escape(s['store'])}{' <em>(closed)</em>' if s['closed'] else ''}</td>"
                f"<td>{escape(nm or s['raw'])}</td><td>{badge(o)}</td>"
                f"<td>{escape(ent)}</td></tr>"
            )
        childhtml = ("<p><strong>Child groups:</strong> "
                     + ", ".join(f"<code>{escape(c)}</code>" for c in child) + "</p>") if child else ""
        parts.append(f"""
<section>
  <h3 style="background:{head_bg};padding:.5rem .7rem;border-radius:6px">
    {escape(gname)} <span style="font-weight:400;color:#555">— {len(members)} store(s) · {escape(summary)}</span>
  </h3>
  {childhtml}
  <table><tr><th>Store</th><th>Name</th><th>Ownership</th><th>Entity</th></tr>
  {''.join(body) or '<tr><td colspan=4><em>none</em></td></tr>'}</table>
</section>""")

    flaghtml = "".join(
        f'<li style="color:{"#b91c1c" if k == "error" else "#92400e"}">'
        f'<strong>{k.upper()}</strong> — {escape(m)}</li>' for k, m in flags
    ) or "<li>None.</li>"

    multi_html = "".join(
        f'<tr><td>{escape(st)}</td><td>{badge(info(st)[0])}</td>'
        f'<td>{escape(info(st)[2])}</td><td>{len(gs)}</td>'
        f'<td>{escape(" | ".join(gs))}</td>'
        f'<td style="color:{"#b91c1c;font-weight:600" if cls == "bad" else "#1a7f37"}">'
        f'{escape(v)}</td></tr>'
        for st, gs, v, cls in multi_rows
    ) or '<tr><td colspan=6><em>No store appears in more than one group.</em></td></tr>'

    xref = "".join(
        f"<tr><td>{escape(s)}</td><td>{badge(info(s)[0])}</td>"
        f"<td>{escape(info(s)[2])}</td><td>{escape(' | '.join(gs))}</td></tr>"
        for s, gs in sorted(store_groups.items())
    )

    OUT.write_text(f"""<!doctype html>
<meta charset="utf-8">
<title>Newk's location groups × ownership</title>
<style>
 body {{ font:14px/1.55 system-ui,sans-serif; max-width:70rem; margin:2rem auto; padding:0 1rem; color:#111 }}
 table {{ border-collapse:collapse; width:100%; margin:.4rem 0 1.4rem }}
 th,td {{ border-bottom:1px solid #e5e5e5; padding:.3rem .55rem; text-align:left; font-size:13px }}
 th {{ background:#fafafa }}
 code {{ background:#f3f3f3; padding:.05rem .3rem; border-radius:3px }}
 h3 {{ margin:1.4rem 0 .3rem; font-size:15px }}
 .box {{ background:#fff8e5; border-left:4px solid #e0a800; padding:.7rem 1rem; margin:1rem 0 }}
</style>
<h1>Location groups × store ownership</h1>
<p>{len(groups)} group(s) · {len(store_groups)} distinct store(s) ·
   ownership from <code>backend/store_ownership.json</code> ({len(own)} stores).
   Generated {datetime.now():%Y-%m-%d %H:%M}.</p>

<div class="box">
<h2 style="margin-top:0;font-size:15px">Flags</h2>
<ul>{flaghtml}</ul>
<p><strong>Company-owned stores in NO "Company Only" group ({len(missing_co)}):</strong><br>
{escape(', '.join(missing_co)) or 'none'}</p>
</div>

<h2>Groups</h2>
{''.join(parts)}

<h2>Locations in more than one group ({len(multi_rows)})</h2>
<p>A store in <code>Tier N - Dine In</code> <em>and</em> <code>Tier N - Dine In - Company
Only</code> is expected &mdash; same tier, both rows apply and the more specific one wins.
Rows marked <strong>REVIEW</strong> span different tier numbers, which usually is not
intended.</p>
<table><tr><th>Store</th><th>Ownership</th><th>Name</th><th>#</th><th>Groups</th><th>Verdict</th></tr>
{multi_html}</table>

<h2>Store → groups</h2>
<table><tr><th>Store</th><th>Ownership</th><th>Name</th><th>Groups</th></tr>{xref}</table>
""", encoding="utf-8")

    print(f"wrote {OUT}")
    print(f"  groups           : {len(groups)}")
    print(f"  distinct stores  : {len(store_groups)}")
    print(f"  flags            : {len(flags)}")
    for k, m in flags:
        print(f"    [{k}] {m}")
    print(f"  company stores in no Company Only group: {len(missing_co)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
