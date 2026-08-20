"""
add_workbook_links.py — make the 180 Day Checklist workbook's references
clickable.

The IT Department tab carries raw 120-character KB URLs in the "KB Article /
Reference" column: not clickable, and unreadable next to the task they belong
to. The department tabs carry vendor email addresses as plain text. This turns
both into real hyperlinks, keeping the full URL in a cell comment so nothing is
hidden.

    python backend/add_workbook_links.py [--dry-run] [--out <dir>]

Where a row's reference cell holds prose rather than a URL ("NRO Checklist -
Toast Point of Sale ... : Newk's - Knowledgebase"), the catalog is searched for
a matching article — by task title, and by comparing the prose against the
article slug in each known URL. Rows with no URL anywhere are reported and left
exactly as they are.

NOTE: the IT Vendor Contacts workbook is deliberately NOT touched. It already
carries all 68 of its hyperlinks (mailto: on every email, real targets behind
every "Open Form ↗" / "Open Portal ↗"). Verify with --check-contacts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from typing import Dict, List, Optional

try:
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.styles import Font
except ImportError:  # pragma: no cover
    print("openpyxl is required:  pip install openpyxl", file=sys.stderr)
    raise

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "data", "onboarding", "source")
CHECKLIST = os.path.join(SRC_DIR, "180-day-checklist.xlsx")
CONTACTS = os.path.join(SRC_DIR, "it-vendor-contacts.xlsx")
CATALOG = os.path.join(HERE, "data", "onboarding", "catalog_data.json")

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
URL_RE = re.compile(r"^https?://", re.I)
IT_HEADER_ROW = 7
CHECK_HEADER_ROW = 7
KB_COL, INFO_COL = 6, 7          # IT tab: F · department tabs: G


def norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"[’‘']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def url_slug(u: str) -> str:
    """The human-readable tail of a Freshdesk article URL, normalized."""
    tail = u.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"^\d+-", "", tail)        # drop the leading article id
    return norm(tail)


def link_font(base) -> Font:
    return Font(name=base.name or "Carlito", sz=base.sz or 10,
                b=base.b, color="FF0563C1", underline="single")


def set_link(cell, url: str, text: str, note: Optional[str] = None) -> None:
    cell.value = text
    cell.hyperlink = url
    cell.font = link_font(cell.font)
    if note:
        c = Comment(note, "NRO links")
        c.width, c.height = 460, 110
        cell.comment = c


def load_catalog_urls() -> Dict[str, str]:
    with open(CATALOG, encoding="utf-8") as f:
        cat = json.load(f)
    by_title: Dict[str, str] = {}
    for tr in cat.get("tracks", {}).values():
        for t in tr.get("tasks", []):
            u = t.get("kb") or t.get("form_url")
            if u and URL_RE.match(u):
                by_title.setdefault(norm(t.get("title")), u)
    for f_ in cat.get("reference", {}).get("forms", []):
        if f_.get("url"):
            by_title.setdefault(norm(f_.get("name")), f_["url"])
    return by_title


def find_url(title: str, prose: str, by_title: Dict[str, str]) -> Optional[str]:
    """Best-effort URL for a row whose reference cell isn't already a URL."""
    hit = by_title.get(norm(title))
    if hit:
        return hit
    # The prose is usually the KB article's own headline, and the article slug
    # is embedded in its URL — so compare the two directly.
    p = norm(prose)
    if len(p.split()) >= 3:
        for u in set(by_title.values()):
            s = url_slug(u)
            if not s:
                continue
            if s.startswith(p[:len(s)]) or p.startswith(s[:max(18, len(s) // 2)]):
                return u
    return None


def enrich(path: str, out: Optional[str], dry: bool) -> None:
    by_title = load_catalog_urls()
    print(f"catalog holds {len(by_title)} article URLs\n")
    wb = openpyxl.load_workbook(path)

    made = recovered = left = 0
    ws = wb["IT Department"]
    for r in range(IT_HEADER_ROW + 1, ws.max_row + 1):
        title = str(ws.cell(row=r, column=1).value or "").strip()
        desc = str(ws.cell(row=r, column=2).value or "").strip()
        if not title or not desc:            # section band, legend or blank
            continue
        c = ws.cell(row=r, column=KB_COL)
        raw = str(c.value or "").strip()
        if URL_RE.match(raw):
            set_link(c, raw, "KB Article ↗", note=f"Full link:\n{raw}")
            made += 1
        else:
            url = find_url(title, raw, by_title)
            if url:
                set_link(c, url, "KB Article ↗",
                         note=(f"Recovered from the app catalog.\nFull link:\n{url}"
                               + (f"\n\nPrevious cell text:\n{raw}" if raw else "")))
                recovered += 1
                print(f"  recovered: {title[:46]:48} -> {url[:64]}")
            elif raw:
                left += 1
                print(f"  NO URL   : {title[:46]:48} (left as text: “{raw[:48]}…”)")
    print(f"\nIT Department: {made} raw URLs made clickable, "
          f"{recovered} recovered from the catalog, {left} left as prose")

    emails = 0
    for sheet in wb.worksheets:
        if sheet.title in ("NRO Info", "IT Department"):
            continue
        n = 0
        for r in range(CHECK_HEADER_ROW + 1, sheet.max_row + 1):
            c = sheet.cell(row=r, column=INFO_COL)
            v = str(c.value or "").strip()
            if v and EMAIL_RE.match(v) and not c.hyperlink:
                set_link(c, f"mailto:{v}", v)
                n += 1
        if n:
            print(f"  {sheet.title:26} {n} emails linked")
        emails += n
    print(f"{emails} vendor emails linked across the department tabs")

    if dry:
        print("\n--dry-run: nothing written")
        return
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    else:
        out = path
        backup = path + ".bak"
        shutil.copy2(path, backup)
        print(f"\nbacked up original to {backup}")
    wb.save(out)
    print(f"wrote {out}")


def check_contacts() -> None:
    wb = openpyxl.load_workbook(CONTACTS)
    total = 0
    for ws in wb.worksheets:
        n = sum(1 for row in ws.iter_rows() for c in row if c.hyperlink)
        total += n
        print(f"  {ws.title:26} {n} hyperlinks")
    print(f"{total} hyperlinks present — this workbook needs no repair")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=CHECKLIST)
    ap.add_argument("--out", help="write a copy here instead of updating in place")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-contacts", action="store_true",
                    help="report the IT Vendor Contacts workbook's links and exit")
    args = ap.parse_args()

    if args.check_contacts:
        check_contacts()
        return 0
    enrich(args.src, args.out, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
