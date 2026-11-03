"""Standalone roster, results, and payroll mapping. No app login or HTTP API."""
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from meal_card_organization import EXPECTED_MERCHANT


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class LocalBackend:
    def __init__(self, base_dir, roster_path=None, dry_run=False):
        self.base_dir = Path(base_dir)
        path = Path(roster_path) if roster_path else self.base_dir / 'roster.json'
        self.roster = json.loads(path.read_text(encoding='utf-8'))
        self.cards = []
        seen = set()
        for original in self.roster['cards']:
            card = dict(original)
            number = str(card['card_number']).strip()
            if not number.isdigit() or number in seen:
                raise ValueError('Roster must contain unique numeric card numbers')
            seen.add(number)
            card['card_number'] = number
            if card.get('target_amount') is None:
                card['target_amount'] = self.roster['default_target']
            if card.get('active', True):
                self.cards.append(card)
        self.cards.sort(key=lambda c: c.get('sort_order', 0))
        self.scope = 'standalone:' + EXPECTED_MERCHANT
        self.dry_run = dry_run
        self.run_id = None
        self.year = self.period = None

    def results_path(self, year, period):
        return self.base_dir / 'state' / f'results-{year}-{period:02d}.json'

    def worklist(self, year=None, period=None, include_done=False):
        now = datetime.now()
        year, period = year or now.year, period or now.month
        if not 1 <= period <= 12:
            raise ValueError('Period must be a month from 1 to 12')
        path = self.results_path(year, period)
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        cards = [dict(c) for c in self.cards if include_done or saved.get(c['card_number'], {}).get('status') not in {'done', 'skipped'}]
        return {'year': year, 'period': period, 'default_target': self.roster['default_target'], 'cards': cards}

    def start_run(self, year, period):
        self.year, self.period = year, period
        self.run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        return self.run_id

    def log_line(self, text):
        # The main logger already writes all progress to disk.
        pass

    def record_card(self, card_number, status, amount=None, balance_before=None, detail=''):
        if self.dry_run:
            return
        path = self.results_path(self.year, self.period)
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        saved[str(card_number)] = {'status': status, 'amount': amount,
                                   'balance_before': balance_before, 'detail': detail,
                                   'run_id': self.run_id}
        write_json(path, saved)

    def finish_run(self, completed, skipped, errors, log):
        write_json(self.base_dir / 'state' / 'runs' / f'{self.run_id}.json',
                   {'run_id': self.run_id, 'year': self.year, 'period': self.period,
                    'dry_run': self.dry_run, 'completed': completed, 'skipped': skipped,
                    'errors': errors, 'log': log[-200000:]})

    def build_reports(self, raw, filename):
        # Source checkout uses the shared backend module; the distributable ZIP
        # includes that exact file beside this module.
        try:
            from mealcard_payroll import build_reports
        except ModuleNotFoundError as exc:
            if exc.name != 'mealcard_payroll':
                raise
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from mealcard_payroll import build_reports
        names = {c['card_number']: c['name'] for c in self.cards}
        by_card = {str(c['card_number']): dict(c) for c in self.roster.get('employee_cards', [])}
        by_name = {m['name']: dict(m) for m in self.roster.get('employee_names', [])}
        result = build_reports(raw, filename, names, by_card, by_name,
                               self.roster.get('mapping_source', 'Local roster mappings; verify before payroll'))
        return {'stats': result['stats'], 'unmapped': result['unmapped'], 'files': [
            {'filename': result['names_filename'], 'data': base64.b64encode(result['names_xlsx']).decode('ascii')},
            {'filename': result['payroll_filename'], 'data': base64.b64encode(result['payroll_xlsx']).decode('ascii')},
        ]}
