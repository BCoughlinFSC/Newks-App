"""
onboarding_alerts.py — weeks-out alerting for the NRO Onboarding journey.

Every task carries a "Weeks Out" (weeks before the target open date), so once an
opening has a target date each task has a real calendar due date. This module
turns that into two emails:

  • Weekly digest — one per active opening. What's coming up in the next
    LOOKAHEAD_DAYS and what is already past its date and still unticked.
  • Overdue escalation — mid-week, and only when something is actually overdue,
    so it reads as a nudge rather than another full report.

Recipients follow one rule (chosen with Kim, 2026-08-31):
  • the franchisee sees only tasks they own, and only if the opening has an
    email on file;
  • one internal address gets the whole picture. It defaults to the existing
    NRO_NOTIFY_TO env list and can be overridden from the dashboard, which
    writes the `alert_email` setting.

Nothing here writes task state — it only reads and sends.
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import onboarding_db
from email_service import NRO_NOTIFY_TO, send_onboarding_schedule_email

logger = logging.getLogger(__name__)

# How far ahead the digest looks. Two weeks is one digest of warning plus one
# digest of "this is now imminent" before anything can quietly go overdue.
LOOKAHEAD_DAYS = 14

SETTING_ENABLED = "alerts_enabled"
SETTING_EMAIL = "alert_email"


def alerts_enabled() -> bool:
    """Alerts are on unless someone has explicitly switched them off."""
    return (onboarding_db.get_setting(SETTING_ENABLED, "1") or "1") != "0"


def internal_recipients() -> List[str]:
    """The one internal address (or comma-separated list) that sees everything."""
    raw = onboarding_db.get_setting(SETTING_EMAIL)
    if raw:
        return [e.strip() for e in raw.split(",") if e.strip()]
    return list(NRO_NOTIFY_TO)


def _bucket(tasks: List[Dict[str, Any]], today: date,
            horizon: date) -> Dict[str, List[Dict[str, Any]]]:
    """Split open tasks into overdue / due within the horizon.

    A task only participates if it has a due date at all — no weeks-out or no
    target open date means there is nothing to be late for.
    """
    overdue, upcoming = [], []
    iso_today, iso_horizon = today.isoformat(), horizon.isoformat()
    for t in tasks:
        if t.get("done") or not t.get("due_date"):
            continue
        if t["due_date"] < iso_today:
            overdue.append(t)
        elif t["due_date"] <= iso_horizon:
            upcoming.append(t)
    overdue.sort(key=lambda t: t["due_date"])
    upcoming.sort(key=lambda t: t["due_date"])
    return {"overdue": overdue, "upcoming": upcoming}


def schedule_for_opening(opening: Dict[str, Any],
                         today: Optional[date] = None) -> Dict[str, Any]:
    """The alerting view of one opening: its overdue and upcoming tasks, split
    into everything (internal) and franchisee-owned only."""
    today = today or date.today()
    horizon = today + timedelta(days=LOOKAHEAD_DAYS)
    tasks = opening.get("tasks") or []
    fr = [t for t in tasks if t.get("owner") == onboarding_db.OWNER_FRANCHISEE]
    return {
        "opening": opening,
        "all": _bucket(tasks, today, horizon),
        "franchisee": _bucket(fr, today, horizon),
        "appointments": upcoming_appointments(opening, today, horizon),
    }


def upcoming_appointments(opening: Dict[str, Any], today: date,
                          horizon: date) -> List[Dict[str, Any]]:
    """Scheduled appointments falling inside the digest window."""
    out = [a for a in (opening.get("appointments") or [])
           if a.get("on_date") and today.isoformat() <= a["on_date"] <= horizon.isoformat()]
    out.sort(key=lambda a: (a["on_date"], a.get("at_time") or ""))
    return out


def _active_openings() -> List[Dict[str, Any]]:
    """Full (hydrated) active openings that have a target open date — without
    one there are no due dates and nothing to alert on."""
    out = []
    for row in onboarding_db.list_openings():
        if not row.get("target_open"):
            continue
        full = onboarding_db.get_opening_by_id(row["id"])
        if full:
            out.append(full)
    return out


def _send_for(sched: Dict[str, Any], *, kind: str, base_url: str) -> Dict[str, int]:
    """Send one opening's alert to the internal address and (optionally) the
    franchisee. Returns how many of each actually went out."""
    o = sched["opening"]
    sent = {"internal": 0, "franchisee": 0}

    internal = internal_recipients()
    if internal and (sched["all"]["overdue"] or sched["all"]["upcoming"]
                     or sched["appointments"]):
        if send_onboarding_schedule_email(
            internal, opening=o, buckets=sched["all"],
            appointments=sched["appointments"], kind=kind, audience="internal",
            dashboard_url=f"{base_url}/onboarding.html",
        ):
            sent["internal"] = len(internal)

    fr_email = (o.get("franchisee_email") or "").strip()
    fr = sched["franchisee"]
    if fr_email and (fr["overdue"] or fr["upcoming"] or sched["appointments"]):
        token = onboarding_db.get_token(o["id"])
        if send_onboarding_schedule_email(
            [fr_email], opening=o, buckets=fr,
            appointments=sched["appointments"], kind=kind, audience="franchisee",
            dashboard_url=f"{base_url}/onboarding-journey.html?t={token}",
        ):
            sent["franchisee"] = 1
    return sent


def run_weekly_digest(base_url: str, today: Optional[date] = None) -> Dict[str, Any]:
    """One digest per active opening: what's due in the next two weeks, plus
    anything already overdue."""
    if not alerts_enabled():
        logger.info("NRO alerts disabled — skipping weekly digest")
        return {"skipped": "disabled"}
    stats = {"openings": 0, "internal": 0, "franchisee": 0}
    for o in _active_openings():
        sched = schedule_for_opening(o, today)
        if not (sched["all"]["overdue"] or sched["all"]["upcoming"] or sched["appointments"]):
            continue
        sent = _send_for(sched, kind="weekly", base_url=base_url)
        stats["openings"] += 1
        stats["internal"] += sent["internal"]
        stats["franchisee"] += sent["franchisee"]
    logger.info("NRO weekly digest: %s", stats)
    return stats


def run_overdue_escalation(base_url: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Mid-week nudge — sent only for openings that actually have overdue tasks."""
    if not alerts_enabled():
        logger.info("NRO alerts disabled — skipping overdue escalation")
        return {"skipped": "disabled"}
    stats = {"openings": 0, "internal": 0, "franchisee": 0}
    for o in _active_openings():
        sched = schedule_for_opening(o, today)
        if not sched["all"]["overdue"]:
            continue
        # An escalation is about what is late; drop the upcoming list so the
        # email says one thing.
        sched["all"]["upcoming"] = []
        sched["franchisee"]["upcoming"] = []
        sched["appointments"] = []
        sent = _send_for(sched, kind="overdue", base_url=base_url)
        stats["openings"] += 1
        stats["internal"] += sent["internal"]
        stats["franchisee"] += sent["franchisee"]
    logger.info("NRO overdue escalation: %s", stats)
    return stats


def preview(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """What the next digest would say, for the dashboard's "Alerts" panel and
    for checking the wiring without sending anything."""
    out = []
    for o in _active_openings():
        s = schedule_for_opening(o, today)
        out.append({
            "id": o["id"],
            "store_name": o.get("store_name"),
            "city": o.get("city"),
            "state": o.get("state"),
            "target_open": o.get("target_open"),
            "franchisee_email": o.get("franchisee_email"),
            "overdue": len(s["all"]["overdue"]),
            "upcoming": len(s["all"]["upcoming"]),
            "franchisee_overdue": len(s["franchisee"]["overdue"]),
            "franchisee_upcoming": len(s["franchisee"]["upcoming"]),
            "appointments": len(s["appointments"]),
            "overdue_tasks": [
                {"key": t["key"], "title": t["title"], "due_date": t["due_date"],
                 "weeks": t.get("weeks"), "owner": t.get("owner"), "dept": t.get("dept")}
                for t in s["all"]["overdue"][:25]
            ],
            "upcoming_tasks": [
                {"key": t["key"], "title": t["title"], "due_date": t["due_date"],
                 "weeks": t.get("weeks"), "owner": t.get("owner"), "dept": t.get("dept")}
                for t in s["all"]["upcoming"][:25]
            ],
        })
    out.sort(key=lambda x: (-x["overdue"], x["target_open"] or ""))
    return out
