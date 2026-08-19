"""
subscriptions_db.py — emailed report subscriptions (the SSRS subscription
replacement for the reports that stay in this app).

One row per subscription: which report, on what cron, to whom, in what format.
`report_key` maps to a renderer in report_email.py; `params_json` carries the
report-specific knobs (lookback window, basis, groupBy, ...).

APScheduler in main.py registers one cron job per enabled row and re-registers
whenever a row is created/updated/deleted. Every send — success or failure —
lands in subscription_runs so the Admin page can show a real run history.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SUBS_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "subscriptions", "subscriptions.db")

# Day-of-week tokens, in APScheduler's own spelling so schedule strings can be
# handed straight to CronTrigger(day_of_week=...).
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key    TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    days_of_week  TEXT    NOT NULL DEFAULT 'mon,tue,wed,thu,fri,sat,sun',
    hour          INTEGER NOT NULL DEFAULT 7,
    minute        INTEGER NOT NULL DEFAULT 45,
    recipients_to TEXT    NOT NULL DEFAULT '',
    recipients_cc TEXT    NOT NULL DEFAULT '',
    recipients_bcc TEXT   NOT NULL DEFAULT '',
    reply_to      TEXT    NOT NULL DEFAULT '',
    attach_xlsx   INTEGER NOT NULL DEFAULT 1,
    send_if_empty INTEGER NOT NULL DEFAULT 0,
    params_json   TEXT    NOT NULL DEFAULT '{}',
    notes         TEXT,
    last_run_at   TEXT,
    last_status   TEXT,
    last_error    TEXT,
    last_row_count INTEGER,
    created_by    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    name            TEXT,
    report_key      TEXT,
    ran_at          TEXT    NOT NULL,
    status          TEXT    NOT NULL,      -- success | skipped_empty | failed
    row_count       INTEGER,
    recipients      TEXT,
    duration_s      REAL,
    triggered_by    TEXT,                  -- schedule | <username>
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_subruns_sub  ON subscription_runs(subscription_id);
CREATE INDEX IF NOT EXISTS idx_subruns_time ON subscription_runs(ran_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: str = SUBS_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize_schema(db_path: str = SUBS_DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(_SCHEMA)
        # Added after the first deploy — the SSRS originals all BCC'd
        # reports@newks.com and set Reply-To to itdepartment@newks.com.
        existing = {r[1] for r in conn.execute("PRAGMA table_info(report_subscriptions)")}
        for col in ("recipients_bcc", "reply_to"):
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE report_subscriptions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        conn.commit()
    finally:
        conn.close()


# ── helpers ──────────────────────────────────────────────────────────

def parse_recipients(raw: str) -> List[str]:
    """Split a ';'/','/newline-separated recipient string into clean addresses.

    Bare aliases (SSRS stored them as 'salesteam', 'fbcs', ...) are expanded to
    @newks.com so the SSRS lists can be pasted in verbatim.
    """
    if not raw:
        return []
    out: List[str] = []
    for chunk in str(raw).replace("\n", ";").replace(",", ";").split(";"):
        addr = chunk.strip()
        if not addr:
            continue
        if "@" not in addr:
            addr = f"{addr}@newks.com"
        if addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    return out


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["attachXlsx"] = bool(d.get("attach_xlsx"))
    d["sendIfEmpty"] = bool(d.get("send_if_empty"))
    try:
        d["params"] = json.loads(d.get("params_json") or "{}")
    except (TypeError, ValueError):
        d["params"] = {}
    d["toList"] = parse_recipients(d.get("recipients_to") or "")
    d["ccList"] = parse_recipients(d.get("recipients_cc") or "")
    d["bccList"] = parse_recipients(d.get("recipients_bcc") or "")
    d["replyTo"] = (d.get("reply_to") or "").strip()
    d["daysList"] = [x for x in (d.get("days_of_week") or "").split(",") if x]
    d["scheduleLabel"] = schedule_label(d.get("days_of_week") or "", d.get("hour") or 0,
                                        d.get("minute") or 0)
    return d


def schedule_label(days_of_week: str, hour: int, minute: int) -> str:
    """Human-readable schedule, e.g. 'Daily at 7:50 AM' / 'Mondays at 7:45 AM'."""
    days = [d for d in (days_of_week or "").split(",") if d]
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    clock = f"{h12}:{minute:02d} {ampm}"
    if len(days) == 7:
        when = "Daily"
    elif set(days) == {"mon", "tue", "wed", "thu", "fri"}:
        when = "Weekdays"
    elif len(days) == 1:
        when = {"mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays",
                "thu": "Thursdays", "fri": "Fridays", "sat": "Saturdays",
                "sun": "Sundays"}.get(days[0], days[0].title())
    else:
        when = ", ".join(d.title() for d in days)
    return f"{when} at {clock}"


# ── CRUD ─────────────────────────────────────────────────────────────

def list_subscriptions(db_path: str = SUBS_DB_PATH) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM report_subscriptions ORDER BY report_key, name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_subscription(sub_id: int, db_path: str = SUBS_DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM report_subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


_FIELDS = ("report_key", "name", "enabled", "days_of_week", "hour", "minute",
           "recipients_to", "recipients_cc", "recipients_bcc", "reply_to",
           "attach_xlsx", "send_if_empty", "params_json", "notes")


def _coerce(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Accept camelCase from the UI, normalise to column names."""
    src = {
        "report_key":    payload.get("reportKey",     payload.get("report_key")),
        "name":          payload.get("name"),
        "enabled":       payload.get("enabled", True),
        "days_of_week":  payload.get("daysOfWeek",    payload.get("days_of_week")),
        "hour":          payload.get("hour"),
        "minute":        payload.get("minute"),
        "recipients_to": payload.get("recipientsTo",  payload.get("recipients_to", "")),
        "recipients_cc": payload.get("recipientsCc",  payload.get("recipients_cc", "")),
        "recipients_bcc": payload.get("recipientsBcc", payload.get("recipients_bcc", "")),
        "reply_to":      payload.get("replyTo",       payload.get("reply_to", "")),
        "attach_xlsx":   payload.get("attachXlsx",    payload.get("attach_xlsx", True)),
        "send_if_empty": payload.get("sendIfEmpty",   payload.get("send_if_empty", False)),
        "notes":         payload.get("notes"),
    }
    params = payload.get("params")
    src["params_json"] = json.dumps(params if isinstance(params, dict) else {})

    if isinstance(src["days_of_week"], (list, tuple)):
        src["days_of_week"] = ",".join(src["days_of_week"])
    src["days_of_week"] = (src["days_of_week"] or ",".join(DAYS)).lower()
    src["hour"] = max(0, min(23, int(src["hour"] if src["hour"] is not None else 7)))
    src["minute"] = max(0, min(59, int(src["minute"] if src["minute"] is not None else 0)))
    src["enabled"] = 1 if src["enabled"] else 0
    src["attach_xlsx"] = 1 if src["attach_xlsx"] else 0
    src["send_if_empty"] = 1 if src["send_if_empty"] else 0
    return src


def create_subscription(payload: Dict[str, Any], created_by: str = "",
                        db_path: str = SUBS_DB_PATH) -> Dict[str, Any]:
    data = _coerce(payload)
    now = _now()
    conn = get_connection(db_path)
    try:
        cols = ", ".join(_FIELDS) + ", created_by, created_at, updated_at"
        ph = ", ".join("?" * (len(_FIELDS) + 3))
        vals = [data[f] for f in _FIELDS] + [created_by, now, now]
        cur = conn.execute(
            f"INSERT INTO report_subscriptions ({cols}) VALUES ({ph})", vals
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get_subscription(new_id, db_path)


def update_subscription(sub_id: int, payload: Dict[str, Any],
                        db_path: str = SUBS_DB_PATH) -> Optional[Dict[str, Any]]:
    if not get_subscription(sub_id, db_path):
        return None
    data = _coerce(payload)
    conn = get_connection(db_path)
    try:
        sets = ", ".join(f"{f} = ?" for f in _FIELDS) + ", updated_at = ?"
        vals = [data[f] for f in _FIELDS] + [_now(), sub_id]
        conn.execute(f"UPDATE report_subscriptions SET {sets} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()
    return get_subscription(sub_id, db_path)


def set_enabled(sub_id: int, enabled: bool, db_path: str = SUBS_DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE report_subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), sub_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_subscription(sub_id, db_path)


def delete_subscription(sub_id: int, db_path: str = SUBS_DB_PATH) -> bool:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("DELETE FROM report_subscriptions WHERE id = ?", (sub_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── run history ──────────────────────────────────────────────────────

def record_run(sub_id: int, name: str, report_key: str, status: str,
               row_count: Optional[int] = None, recipients: str = "",
               duration_s: Optional[float] = None, triggered_by: str = "schedule",
               error: str = "", db_path: str = SUBS_DB_PATH) -> None:
    now = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO subscription_runs "
            "(subscription_id, name, report_key, ran_at, status, row_count, "
            " recipients, duration_s, triggered_by, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sub_id, name, report_key, now, status, row_count, recipients,
             duration_s, triggered_by, error[:2000] if error else ""),
        )
        conn.execute(
            "UPDATE report_subscriptions SET last_run_at = ?, last_status = ?, "
            "last_error = ?, last_row_count = ? WHERE id = ?",
            (now, status, (error or "")[:2000], row_count, sub_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_runs(limit: int = 100, sub_id: Optional[int] = None,
              db_path: str = SUBS_DB_PATH) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        if sub_id is not None:
            rows = conn.execute(
                "SELECT * FROM subscription_runs WHERE subscription_id = ? "
                "ORDER BY ran_at DESC LIMIT ?", (sub_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM subscription_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
