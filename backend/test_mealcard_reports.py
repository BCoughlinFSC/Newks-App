"""Read-only reporting tests. No browser, Paytronix or production app calls."""
import base64
import csv
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from unittest.mock import Mock
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent / "mealcard_bot"))
import meal_card_reports as reports
import meal_card_reload as bot
import mealcard_payroll
from meal_card_local import LocalBackend

DAY = date(2026, 11, 2)
CARDS = [{"card_number": "100", "name": "Test Employee"}]
HEADER = ["Card Number", "Dollars Added", "Date", "Store Name"]


def export(*rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["Paytronix export"])
    writer.writerow(HEADER)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def row(card="100", amount="15.03", day="11/02/2026 01:00:00 PM", store="Flowood MS"):
    return [card, amount, day, store]


class ReportTests(unittest.TestCase):
    def test_excludes_retired_and_non_roster_gift_cards(self):
        raw = export(row(), row("200"), row("300"))
        result = reports.filter_report(raw, CARDS + [{"card_number": "200"}], {"200"}, DAY, DAY)
        parsed = list(csv.DictReader(io.StringIO(result.decode("utf-8-sig"))))
        self.assertEqual([r["Card Number"] for r in parsed], ["100"])

    def test_missing_recent_load_blocks_payroll(self):
        with self.assertRaisesRegex(ValueError, "caught up"):
            reports.filter_report(export(row(amount="10")), CARDS, set(), DAY, DAY, {"100": "15.03"})

    def test_voids_are_net_against_added_amount(self):
        with self.assertRaisesRegex(ValueError, "caught up"):
            reports.filter_report(export(row(), row(amount="-15.03")), CARDS, set(), DAY, DAY, {"100": "15.03"})

    def test_bad_export_or_wrong_store_or_date_is_rejected(self):
        for raw in (b"<html>Login</html>", export(row(store="Jackson MS")),
                    export(row(day="11/01/2026 01:00:00 PM")), export(row(amount="NaN"))):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                reports.filter_report(raw, CARDS, set(), DAY, DAY)

    def test_empty_eligible_export_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "No eligible"):
            reports.filter_report(export(row("999")), CARDS, set(), DAY, DAY)

    def test_actual_payroll_builder_round_trip(self):
        filtered = reports.filter_report(export(row()), CARDS, set(), DAY, DAY, {"100": "15.03"})
        mapping = {"100": {"employee_id": "123", "entity": "90219", "name": "Test Employee"}}

        with tempfile.TemporaryDirectory() as folder:
            import json
            roster = {'default_target': 160, 'cards': CARDS, 'employee_cards': list(mapping.values()), 'employee_names': []}
            roster['employee_cards'][0]['card_number'] = '100'
            (Path(folder) / 'roster.json').write_text(json.dumps(roster))
            client = LocalBackend(folder)
            source = Path(folder) / "MealCardLoads-20261102-filtered.csv"
            source.write_bytes(filtered)
            result, paths = reports.build_workbooks(client, source, folder)
            self.assertEqual(result["stats"]["payroll_total"], 15.03)
            self.assertEqual(len(paths), 2)
            payroll = load_workbook(paths[1], data_only=True).worksheets[0]
            self.assertEqual(payroll["A5"].value, "123")
            self.assertEqual(payroll["B5"].value, 15.03)
            self.assertEqual(payroll["F5"].value, "Test Employee")

    def test_report_only_never_enters_reload_path(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sys, "argv", ["bot", "--report-only"]), \
             patch.object(bot, "__file__", str(Path(folder) / "bot.py")), \
             patch.object(bot, "connect_to_backend") as connect, \
             patch.object(bot, "report_job") as job, \
             patch.object(bot, "process_card") as process:
            bot.main()
            job.assert_called_once()
            process.assert_not_called()
            connect.return_value.start_run.assert_not_called()
            connect.return_value.record_card.assert_not_called()

    def test_rejects_report_only_with_reload_flags(self):
        with patch.object(sys, "argv", ["bot", "--report-only", "--dry-run"]):
            with self.assertRaises(SystemExit):
                bot.parse_args()

    def test_rejects_server_path_traversal(self):
        response = {"stats": {}, "files": [{"filename": "../escape.xlsx", "data": "UEs="}] * 2}
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            client.build_reports.return_value = response
            source = Path(folder) / "input.csv"
            source.write_bytes(b"test")
            with self.assertRaisesRegex(ValueError, "filename"):
                reports.build_workbooks(client, source, folder)


if __name__ == "__main__":
    unittest.main()
