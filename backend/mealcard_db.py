"""
mealcard_db.py - SQLite store for the monthly meal card reload.

Replaces the two files the desktop scripts used to read:

  * "Gift Card _ Meal Card Numbers - Support Center 2026.xlsx" - the card
    roster plus a tick per period. Now mealcard_cards + mealcard_periods.
  * MealCardsToAllPay.csv, exported by hand from dev_aloha - the card to
    employee-ID mapping the payroll report joins to. Now mealcard_employee_map.

The reload bot runs on someone's PC (it drives their logged-in Paytronix
Chrome), so it asks this app for its worklist and posts each card's outcome
back. That makes these tables the record of what was loaded, instead of a
spreadsheet that has to be open on one person's machine.

See main.py /api/mealcard/* and backend/mealcard_bot/.
"""
import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from openpyxl import load_workbook

MEALCARD_DB_PATH = os.getenv(
    "MEALCARD_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "mealcard", "mealcard.db"),
)

DEFAULT_TARGET = 160.00

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mealcard_cards (
    card_number   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    target_amount REAL,                      -- NULL means "use the default"
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS mealcard_periods (
    card_number    TEXT    NOT NULL,
    year           INTEGER NOT NULL,
    period         INTEGER NOT NULL,
    status         TEXT    NOT NULL,         -- done | skipped | error
    amount         REAL,                     -- what the bot added
    balance_before REAL,
    detail         TEXT,
    run_id         INTEGER,
    recorded_at    TEXT    NOT NULL,
    PRIMARY KEY (card_number, year, period)
);

CREATE TABLE IF NOT EXISTS mealcard_employee_map (
    card_number TEXT PRIMARY KEY,
    employee_id TEXT,
    entity      TEXT,
    name        TEXT,
    source      TEXT,
    updated_at  TEXT
);

-- Weaker name-matched fallback, used only for cards the card map misses.
CREATE TABLE IF NOT EXISTS mealcard_employee_map_by_name (
    name        TEXT PRIMARY KEY,
    employee_id TEXT,
    entity      TEXT,
    source      TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS mealcard_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    period       INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'running',  -- running | finished | abandoned
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    requested_by TEXT,
    completed    INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    log          TEXT
);

CREATE INDEX IF NOT EXISTS idx_mealcard_periods_year ON mealcard_periods(year, period);
CREATE INDEX IF NOT EXISTS idx_mealcard_runs_started ON mealcard_runs(started_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: str = MEALCARD_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(db_path: str = MEALCARD_DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def clean_card(v) -> str:
    """Card numbers arrive as floats from Excel and as strings everywhere else."""
    if isinstance(v, float):
        return str(int(v))
    return str(v).strip()


def parse_target_amount(name: str) -> float:
    """Pull a custom amount out of a name like 'Cynthia Baxter (Board Meeting $600.00)'.

    The roster carried per-card exceptions in the name text, so seeding reads
    them the same way the desktop bot did. Once a card is in the table its
    target_amount is editable on its own.
    """
    match = re.search(r"\$(\d+(?:\.\d{2})?)", name or "")
    return float(match.group(1)) if match else DEFAULT_TARGET


# -- Cards --------------------------------------------------------------------
def list_cards(active_only: bool = True, db_path: str = MEALCARD_DB_PATH) -> List[Dict]:
    initialize_db(db_path)
    sql = "SELECT * FROM mealcard_cards"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, name"
    with get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql)]


def upsert_card(card_number: str, name: str, target_amount: Optional[float] = None,
                active: bool = True, sort_order: int = 0, notes: str = "",
                db_path: str = MEALCARD_DB_PATH) -> None:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO mealcard_cards
                   (card_number, name, target_amount, active, sort_order, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(card_number) DO UPDATE SET
                   name = excluded.name,
                   target_amount = excluded.target_amount,
                   active = excluded.active,
                   sort_order = excluded.sort_order,
                   notes = excluded.notes,
                   updated_at = excluded.updated_at""",
            (clean_card(card_number), name.strip(), target_amount,
             1 if active else 0, sort_order, notes, _now()),
        )
        conn.commit()


def card_names(db_path: str = MEALCARD_DB_PATH) -> Dict[str, str]:
    """Card number -> name, for every card including inactive ones.

    The payroll report looks up names for whatever Paytronix reported, which
    can include a card retired mid-month, so this deliberately ignores `active`.
    """
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        return {r["card_number"]: r["name"]
                for r in conn.execute("SELECT card_number, name FROM mealcard_cards")}


def target_for(card: Dict) -> float:
    t = card.get("target_amount")
    return float(t) if t is not None else DEFAULT_TARGET


# -- Importing the roster workbook -------------------------------------------
# Column A is the name, column B the card number, and each period gets its own
# column from C on - so period N lives in column 2 + N.
FIRST_PERIOD_COL = 3


class RosterError(ValueError):
    """The workbook is not the gift card roster we expect."""


def parse_roster(source, sheet: str) -> List[Dict]:
    """Read the gift card workbook. `source` is a path or the file's bytes.

    Parsing is kept separate from writing so a caller can show what an import
    would do before it does it - this overwrites names and targets, so it is
    worth looking at first.
    """
    wb = load_workbook(
        io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source,
        data_only=True,
    )
    try:
        if sheet not in wb.sheetnames:
            raise RosterError(
                f"That workbook has no {sheet!r} sheet. It has: {', '.join(wb.sheetnames)}")
        ws = wb[sheet]

        first, second = ws.cell(1, 1).value, ws.cell(1, 2).value
        if str(first or "").strip().lower() != "name" or "card" not in str(second or "").lower():
            raise RosterError(
                "That sheet does not look like the roster - the first two columns should "
                f"be Name and Card Number, but they are {first!r} and {second!r}.")

        rows = []
        for r in range(2, ws.max_row + 1):
            name_cell, card_cell = ws.cell(r, 1).value, ws.cell(r, 2).value
            if not name_cell or not card_cell:
                continue
            name = str(name_cell).strip()
            rows.append({
                "card_number": clean_card(card_cell),
                "name": name,
                "target_amount": parse_target_amount(name),
                "sort_order": r,
                # Only TRUE counts as loaded; blank and FALSE both mean "not yet".
                "periods": [p for p in range(1, 13)
                            if ws.cell(r, FIRST_PERIOD_COL + p - 1).value is True],
            })
    finally:
        wb.close()

    if not rows:
        raise RosterError(f"No cards found on sheet {sheet!r}.")
    return rows


def import_roster(rows: List[Dict], year: int, source: str = "",
                  db_path: str = MEALCARD_DB_PATH) -> Dict:
    """Write parsed roster rows in. Cards upsert, ticks upsert per period.

    A card that has disappeared from the sheet is left alone rather than
    deleted - retiring someone is a deliberate act, not a side effect of
    re-importing. Deactivate them on the page instead.
    """
    initialize_db(db_path)
    cards = ticks = 0
    for row in rows:
        upsert_card(
            card_number=row["card_number"],
            name=row["name"],
            # Only store a target when the name carried an exception; the rest
            # stay NULL so changing the default moves them all.
            target_amount=(row["target_amount"]
                           if row["target_amount"] != DEFAULT_TARGET else None),
            sort_order=row["sort_order"],
            db_path=db_path,
        )
        cards += 1
        for period in row["periods"]:
            record_period(row["card_number"], year, period, "done",
                          detail=f"imported from {source}" if source else "imported",
                          db_path=db_path)
            ticks += 1
    return {"cards": cards, "ticks": ticks, "year": year}


# -- Period completion --------------------------------------------------------
def period_status(year: int, period: int, db_path: str = MEALCARD_DB_PATH) -> Dict[str, Dict]:
    """card_number -> that card's row for this period, if it has one."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM mealcard_periods WHERE year = ? AND period = ?",
            (year, period),
        )
        return {r["card_number"]: dict(r) for r in rows}


def year_grid(year: int, db_path: str = MEALCARD_DB_PATH) -> Dict[str, Dict[int, str]]:
    """card_number -> {period: status} for a whole year, for the roster table."""
    initialize_db(db_path)
    grid: Dict[str, Dict[int, str]] = {}
    with get_connection(db_path) as conn:
        for r in conn.execute(
            "SELECT card_number, period, status FROM mealcard_periods WHERE year = ?",
            (year,),
        ):
            grid.setdefault(r["card_number"], {})[r["period"]] = r["status"]
    return grid


def record_period(card_number: str, year: int, period: int, status: str,
                  amount: Optional[float] = None, balance_before: Optional[float] = None,
                  detail: str = "", run_id: Optional[int] = None,
                  db_path: str = MEALCARD_DB_PATH) -> None:
    """Write one card's outcome for a period. Re-running a card overwrites it."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO mealcard_periods
                   (card_number, year, period, status, amount, balance_before,
                    detail, run_id, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(card_number, year, period) DO UPDATE SET
                   status = excluded.status,
                   amount = excluded.amount,
                   balance_before = excluded.balance_before,
                   detail = excluded.detail,
                   run_id = excluded.run_id,
                   recorded_at = excluded.recorded_at""",
            (clean_card(card_number), year, period, status, amount, balance_before,
             detail, run_id, _now()),
        )
        conn.commit()


def clear_period(card_number: str, year: int, period: int,
                 db_path: str = MEALCARD_DB_PATH) -> bool:
    """Un-tick one card for a period, so the next run picks it up again."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM mealcard_periods WHERE card_number = ? AND year = ? AND period = ?",
            (clean_card(card_number), year, period),
        )
        conn.commit()
        return cur.rowcount > 0


# -- Employee mapping ---------------------------------------------------------
def replace_employee_map(rows: List[Dict], source: str,
                         db_path: str = MEALCARD_DB_PATH) -> int:
    """Replace the card -> employee mapping wholesale.

    A fresh export from dev_aloha.dbo.MealCardsToAllPay is the whole truth, so
    importing one drops what was there. Merging would keep resurrecting cards
    that the export deliberately no longer has.
    """
    initialize_db(db_path)
    stamped = _now()
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM mealcard_employee_map")
        conn.executemany(
            """INSERT INTO mealcard_employee_map
                   (card_number, employee_id, entity, name, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(clean_card(r.get("card_number", "")), str(r.get("employee_id", "") or ""),
              str(r.get("entity", "") or ""), str(r.get("name", "") or ""), source, stamped)
             for r in rows if clean_card(r.get("card_number", ""))],
        )
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM mealcard_employee_map").fetchone()[0]


def upsert_employee_names(rows: List[Dict], source: str,
                          db_path: str = MEALCARD_DB_PATH) -> int:
    """Merge into the name-matched fallback map.

    Unlike the card map this merges rather than replaces: each seed covers only
    the people reloaded that month, so replacing would shrink it every time.
    """
    initialize_db(db_path)
    stamped = _now()
    with get_connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO mealcard_employee_map_by_name
                   (name, employee_id, entity, source, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   employee_id = excluded.employee_id,
                   entity = excluded.entity,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            [(str(r.get("name", "")).strip(), str(r.get("employee_id", "") or ""),
              str(r.get("entity", "") or ""), source, stamped)
             for r in rows if str(r.get("name", "")).strip()],
        )
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM mealcard_employee_map_by_name").fetchone()[0]


def employee_map(db_path: str = MEALCARD_DB_PATH) -> Tuple[Dict, Dict, str]:
    """(by_card, by_name, description) - the same shape the desktop script used.

    by_card is the authoritative dev_aloha mapping. by_name is the weaker
    fallback lifted from past payroll exports, used only where by_card misses.
    """
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        by_card = {
            r["card_number"]: {"employee_id": r["employee_id"] or "",
                               "entity": r["entity"] or "",
                               "name": r["name"] or ""}
            for r in conn.execute("SELECT * FROM mealcard_employee_map")
        }
        by_name = {
            r["name"]: {"employee_id": r["employee_id"] or "",
                        "entity": r["entity"] or "",
                        "name": r["name"]}
            for r in conn.execute("SELECT * FROM mealcard_employee_map_by_name")
        }
        card_src = conn.execute(
            "SELECT source FROM mealcard_employee_map LIMIT 1").fetchone()
        name_src = conn.execute(
            "SELECT source FROM mealcard_employee_map_by_name LIMIT 1").fetchone()

    if by_card:
        desc = f"{card_src['source'] if card_src else 'card map'} ({len(by_card)} cards) - authoritative"
        if by_name:
            desc += f", plus {len(by_name)} name-matched fallback entries"
        return by_card, by_name, desc
    if by_name:
        return {}, by_name, (f"{len(by_name)} names from "
                             f"{name_src['source'] if name_src else 'payroll exports'} "
                             "- FALLBACK, name-matched")
    return {}, {}, "NONE - no mapping imported yet"


def mapping_summary(db_path: str = MEALCARD_DB_PATH) -> Dict:
    by_card, by_name, desc = employee_map(db_path)
    return {"by_card": len(by_card), "by_name": len(by_name),
            "description": desc, "authoritative": bool(by_card)}


# -- Runs ---------------------------------------------------------------------
def start_run(year: int, period: int, requested_by: str = "",
              db_path: str = MEALCARD_DB_PATH) -> int:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO mealcard_runs (year, period, status, started_at, requested_by)
               VALUES (?, ?, 'running', ?, ?)""",
            (year, period, _now(), requested_by),
        )
        conn.commit()
        return cur.lastrowid


def finish_run(run_id: int, completed: int = 0, skipped: int = 0, errors: int = 0,
               log: str = "", status: str = "finished",
               db_path: str = MEALCARD_DB_PATH) -> None:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            """UPDATE mealcard_runs
                  SET status = ?, finished_at = ?, completed = ?, skipped = ?,
                      errors = ?, log = ?
                WHERE id = ?""",
            (status, _now(), completed, skipped, errors, log, run_id),
        )
        conn.commit()


def append_run_log(run_id: int, text: str, db_path: str = MEALCARD_DB_PATH) -> None:
    """Append a line to a running job's log, so the page can show progress."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE mealcard_runs SET log = COALESCE(log, '') || ? WHERE id = ?",
            (text if text.endswith("\n") else text + "\n", run_id),
        )
        conn.commit()


def get_run(run_id: int, db_path: str = MEALCARD_DB_PATH) -> Optional[Dict]:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM mealcard_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 25, db_path: str = MEALCARD_DB_PATH) -> List[Dict]:
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT id, year, period, status, started_at, finished_at, requested_by,
                      completed, skipped, errors
                 FROM mealcard_runs ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]


# -- The worklist the bot asks for -------------------------------------------
def worklist(year: int, period: int, include_done: bool = False,
             db_path: str = MEALCARD_DB_PATH) -> List[Dict]:
    """Active cards for this period, with the target and what already happened.

    Cards already marked done are left out unless include_done, which is what
    stops a second run of the month from loading everyone twice.
    """
    done = period_status(year, period, db_path)
    out = []
    for card in list_cards(active_only=True, db_path=db_path):
        prior = done.get(card["card_number"])
        if prior and prior["status"] in ("done", "skipped") and not include_done:
            continue
        out.append({
            "card_number": card["card_number"],
            "name": card["name"],
            "target_amount": target_for(card),
            "already": prior["status"] if prior else None,
        })
    return out
