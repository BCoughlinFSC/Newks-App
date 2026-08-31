"""
onboarding_forms.py — turn a Microsoft Forms response export into something readable.

The forms Newk's sends out (Franchise Restaurant Information Form, Restaurant
Systems Questionnaire, ...) come back as an .xlsx that is one row per response
and one column per question — 35 columns wide for the Restaurant Information
Form alone. Readable only by scrolling sideways with a finger on the header row.

This module parses that shape into per-response question/answer pairs, which the
app then stores against an opening and renders vertically.

Deliberately generic: any Forms export works. The only assumptions are Forms'
own conventions —

  • the first row is the header;
  • the leading columns are Forms' own metadata (Id, Start time, Completion
    time, Email, Name, Last modified time) rather than questions;
  • everything after that is a question, in the order the form asks them.

That last point is why answers are kept in order and not regrouped: whoever
built the form already put the questions in a sensible sequence, so replaying
that order reads better than any grouping this module could invent.
"""

import io
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Forms' own columns, matched case-insensitively on the trimmed header.
META_COLS = {
    "id": "response_id",
    "start time": "started_at",
    "completion time": "submitted_at",
    "last modified time": "modified_at",
    "email": "submitter_email",
    "name": "submitter",
}

# Values Forms/Excel leave behind for "not answered". "None" shows up as a
# literal string in these exports, not a null.
_EMPTY = {"", "none", "n/a", "na", "-", "--"}

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
# "Restaurant Site ID (4 digits)" and friends.
_SITE_ID_HINT = re.compile(r"site\s*id|restaurant\s*#|store\s*(number|#)|site\s*number", re.I)
_LOCATION_HINT = re.compile(r"(restaurant|store|site)\s*(location\s*)?name", re.I)
_GROUP_HINT = re.compile(r"franchise\s*(group|entity)\s*name|ownership\s*group", re.I)


def _clean(v: Any) -> str:
    """Normalise a cell to a trimmed string, with Forms' non-answers as ''."""
    if v is None:
        return ""
    if hasattr(v, "isoformat"):          # datetimes come back as objects
        return v.isoformat(sep=" ", timespec="seconds")
    s = str(v).strip()
    return "" if s.lower() in _EMPTY else s


def parse_export(data: bytes, form_name: str = "") -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse a Forms .xlsx export.

    Returns (responses, warnings). Each response is:
        {response_id, submitted_at, started_at, submitter, submitter_email,
         site_id, location_name, franchise_group, answers:[{q, a}, ...]}

    Unanswered questions are dropped from `answers` — a readable summary should
    not be mostly "(blank)".
    """
    try:
        import openpyxl
    except ImportError:                      # pragma: no cover
        raise RuntimeError("openpyxl is required to read form exports")

    warnings: List[str] = []
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return [], ["The file has no rows at all."]

        cols: List[Tuple[int, str]] = [
            (i, str(h).strip()) for i, h in enumerate(header) if h is not None and str(h).strip()
        ]
        if not cols:
            return [], ["The first row has no column headings."]

        meta_idx: Dict[str, int] = {}
        q_cols: List[Tuple[int, str]] = []
        for i, label in cols:
            key = META_COLS.get(label.lower())
            if key and key not in meta_idx:
                meta_idx[key] = i
            else:
                q_cols.append((i, label))

        if "response_id" not in meta_idx:
            warnings.append(
                "No 'Id' column — responses will be numbered by row, so re-uploading "
                "this file may duplicate them rather than update in place."
            )
        if not q_cols:
            return [], ["Found only Forms metadata columns — no questions to read."]

        out: List[Dict[str, Any]] = []
        for n, row in enumerate(rows, start=2):
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue                                    # trailing blank row
            get = lambda i: _clean(row[i]) if i is not None and i < len(row) else ""
            answers = [{"q": label, "a": get(i)} for i, label in q_cols if get(i)]
            if not answers:
                warnings.append(f"Row {n} had no answers — skipped.")
                continue
            rec = {
                "response_id": get(meta_idx.get("response_id")) or f"row{n}",
                "started_at": get(meta_idx.get("started_at")),
                "submitted_at": get(meta_idx.get("submitted_at")),
                "submitter": get(meta_idx.get("submitter")),
                "submitter_email": get(meta_idx.get("submitter_email")),
                "answers": answers,
                "form_name": form_name,
            }
            # Identity fields, lifted out of the answers so a response can be
            # matched to an opening and labelled in a list.
            rec["site_id"] = _first_match(answers, _SITE_ID_HINT, digits=True)
            rec["location_name"] = _first_match(answers, _LOCATION_HINT)
            rec["franchise_group"] = _first_match(answers, _GROUP_HINT)
            # A submitter email is often only inside an answer ("anonymous" form).
            if not rec["submitter_email"] or rec["submitter_email"].lower() == "anonymous":
                rec["submitter_email"] = _first_email(answers)
            out.append(rec)
        return out, warnings
    finally:
        wb.close()


def _first_match(answers: List[Dict[str, str]], pattern: re.Pattern,
                 digits: bool = False) -> str:
    for a in answers:
        if pattern.search(a["q"]):
            if not digits:
                return a["a"]
            m = re.search(r"\d{3,5}", a["a"])
            if m:
                return m.group(0)
    return ""


def _first_email(answers: List[Dict[str, str]]) -> str:
    for a in answers:
        if "submitter" in a["q"].lower():
            m = _EMAIL_RE.search(a["a"])
            if m:
                return m.group(0)
    for a in answers:
        m = _EMAIL_RE.search(a["a"])
        if m:
            return m.group(0)
    return ""


# ─── matching a response to an opening ───────────────────────────────────────

def _norm(s: str) -> str:
    """Loose comparison key: lowercase letters and digits only, and with the
    Newk's brand word dropped — every location name starts with it, so it
    carries no signal and only helps things match that shouldn't."""
    s = re.sub(r"newk'?s?\s*(eatery)?", " ", str(s or ""), flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_opening(response: Dict[str, Any],
                  openings: List[Dict[str, Any]]) -> Tuple[Optional[int], str]:
    """Best opening for a response, as (opening_id, how). Returns (None, reason)
    when nothing is confident enough — an unmatched response waits in the inbox
    rather than being filed against the wrong restaurant.
    """
    site = (response.get("site_id") or "").strip()
    if site:
        hits = [o for o in openings
                if (o.get("restaurant_number") or "").strip() == site]
        if len(hits) == 1:
            return hits[0]["id"], f"restaurant # {site}"
        if len(hits) > 1:
            return None, f"more than one opening has restaurant # {site}"

    loc = _norm(response.get("location_name"))
    if loc:
        # The location name usually carries the city ("Newks Brookhaven"), so
        # match it against each opening's city and store name.
        hits = []
        for o in openings:
            city, name = _norm(o.get("city")), _norm(o.get("store_name"))
            if (city and (city in loc or loc in city)) or (name and name == loc):
                hits.append(o)
        if len(hits) == 1:
            return hits[0]["id"], f"location name “{response.get('location_name')}”"
        if len(hits) > 1:
            return None, f"“{response.get('location_name')}” matches {len(hits)} openings"

    if site:
        return None, f"no opening has restaurant # {site}"
    return None, "no restaurant # or recognisable location name in the response"


# ─── the cover sheet ─────────────────────────────────────────────────────────
# The Restaurant Information Form asks for most of what the NRO Info cover sheet
# wants, so a matched response can offer to fill it in. Question-text patterns
# rather than fixed columns, so a reworded form still lands.

COVER_SHEET_MAP = [
    ("restaurant_number", re.compile(r"site\s*id|restaurant\s*#", re.I), True),
    ("phone", re.compile(r"^site\s*(catering\s*)?phone|phone\s*number", re.I), False),
    ("address", re.compile(r"site\s*address\s*\(street", re.I), False),
    ("city", re.compile(r"^city", re.I), False),
    ("state", re.compile(r"^state", re.I), False),
]

_STATES = {
    "alabama": "AL", "arkansas": "AR", "florida": "FL", "georgia": "GA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "mississippi": "MS",
    "missouri": "MO", "north carolina": "NC", "oklahoma": "OK",
    "south carolina": "SC", "tennessee": "TN", "texas": "TX", "virginia": "VA",
}


def cover_sheet_values(response: Dict[str, Any]) -> Dict[str, str]:
    """Cover-sheet fields this response can fill, as {column: value}."""
    answers = response.get("answers") or []
    out: Dict[str, str] = {}
    for col, pattern, digits in COVER_SHEET_MAP:
        v = _first_match(answers, pattern, digits=digits)
        if not v:
            continue
        if col == "state":
            v = _STATES.get(v.strip().lower(), v.strip())
            if len(v) > 2:
                continue          # not a state we recognise — leave it alone
        out[col] = v
    return out


def answers_json(response: Dict[str, Any]) -> str:
    return json.dumps(response.get("answers") or [], ensure_ascii=False)
