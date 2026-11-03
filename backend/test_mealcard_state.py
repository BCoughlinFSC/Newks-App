"""Financial retry and recovery regressions; no live network or wallet changes."""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent / 'mealcard_bot'))
import meal_card_reload as bot
from meal_card_state import RunState, PendingSubmission, local_run_lock

CARD = {'name': 'Test', 'card_number': '100', 'target_amount': 160}

class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'period.json'
        self.state = RunState(self.path, 'https://test.invalid')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        for name in ('lookup_card', 'navigate_to_adjust_wallet', 'select_stored_value_wallet',
                     'assert_session', 'verify_account', 'fill_adjustment_form', 'confirm_adjustment', 'verify_results', 'get_current_balance'):
            self.mocks[name] = self.stack.enter_context(patch.object(bot, name))
        self.mocks['verify_results'].return_value = True
        self.mocks['get_current_balance'].side_effect = [100, 160]

    def test_verified_survives_restart_and_prevents_second_load(self):
        self.assertEqual(bot.process_card(None, CARD, False, self.state, organization_page=object())[0], 'done')
        fresh = RunState(self.path, 'https://test.invalid')
        self.assertEqual(bot.process_card(None, CARD, False, fresh, organization_page=object())[0], 'skip')
        self.mocks['confirm_adjustment'].assert_called_once()
        self.assertEqual(fresh.get('100')['amount'], 60)

    def test_confirmation_timeout_is_persisted_and_never_retried(self):
        self.mocks['confirm_adjustment'].side_effect = TimeoutError('response lost')
        with self.assertRaises(PendingSubmission):
            bot.process_card(None, CARD, False, self.state, organization_page=object())
        fresh = RunState(self.path, 'https://test.invalid')
        self.assertEqual(fresh.get('100')['status'], 'pending')
        with self.assertRaises(PendingSubmission):
            bot.process_card(None, CARD, False, fresh, organization_page=object())
        self.mocks['confirm_adjustment'].assert_called_once()

    def test_wrong_balance_is_not_marked_complete(self):
        self.mocks['get_current_balance'].side_effect = [100, 159]
        with self.assertRaises(PendingSubmission):
            bot.process_card(None, CARD, False, self.state, organization_page=object())
        self.assertEqual(self.state.get('100')['status'], 'pending')

    def test_dry_run_does_not_fill_or_create_journal(self):
        self.assertEqual(bot.process_card(None, CARD, True, self.state)[0], 'dry-run')
        self.mocks['fill_adjustment_form'].assert_not_called()
        self.mocks['confirm_adjustment'].assert_not_called()
        self.assertFalse(self.path.exists())

    def test_exclusive_lock_releases_after_failure(self):
        path = Path(self.folder.name) / 'run.lock'
        with self.assertRaises(ValueError):
            with local_run_lock(path):
                with self.assertRaises(RuntimeError):
                    with local_run_lock(path):
                        self.fail('overlap allowed')
                raise ValueError('interrupted')
        self.assertFalse(path.exists())

    def test_journal_cannot_be_used_for_another_app(self):
        self.state.pending('100', 100, 60, 160)
        with self.assertRaises(RuntimeError):
            RunState(self.path, 'https://other.invalid')

if __name__ == '__main__':
    unittest.main()

class OrganizationTests(unittest.TestCase):
    def test_wrong_or_missing_organization_stops(self):
        from unittest.mock import Mock
        from meal_card_organization import assert_organization
        page = Mock()
        switch = page.get_by_role.return_value
        switch.count.return_value = 1
        switch.inner_text.return_value = "Another Organization"
        with self.assertRaisesRegex(RuntimeError, "Select Newk's Eatery"):
            assert_organization(page)
        switch.count.return_value = 0
        with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
            assert_organization(page)

    def test_verified_name_accepts_typographic_apostrophe(self):
        from unittest.mock import Mock
        from meal_card_organization import assert_organization
        page = Mock()
        page.get_by_role.return_value.count.return_value = 1
        page.get_by_role.return_value.inner_text.return_value = "Newk’s Eatery"
        assert_organization(page)

    def test_org_change_before_confirm_keeps_pending_and_never_submits(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            state = RunState(Path(folder) / 'period.json', 'https://test.invalid')
            for name in ('lookup_card', 'navigate_to_adjust_wallet', 'select_stored_value_wallet', 'verify_account', 'fill_adjustment_form'):
                stack.enter_context(patch.object(bot, name))
            stack.enter_context(patch.object(bot, 'get_current_balance', return_value=100))
            guard = stack.enter_context(patch.object(bot, 'assert_session', side_effect=[None, None, None, RuntimeError('organization changed')]))
            confirm = stack.enter_context(patch.object(bot, 'confirm_adjustment'))
            with self.assertRaises(PendingSubmission):
                bot.process_card(None, CARD, False, state, organization_page=Mock())
            confirm.assert_not_called()
            self.assertEqual(state.get('100')['status'], 'pending')

class AdapterTests(unittest.TestCase):
    def test_lookup_stays_on_evo_host_and_merchant(self):
        from unittest.mock import Mock
        frame = Mock(url='https://azr1px.px.us.access-evo.com/merchant/csr/editwallet.srv')
        self.assertEqual(bot.lookup_url(frame), 'https://azr1px.px.us.access-evo.com/accountlookup/accountlookup?currentMerchantId=1085')
        for url in ('https://www.pxsweb.com/accountlookup/accountlookup', 'https://example.invalid', 'http://azr1px.px.us.access-evo.com'):
            frame.url = url
            with self.assertRaises(RuntimeError):
                bot.lookup_url(frame)

    def test_wrong_card_or_merchant_is_rejected(self):
        from unittest.mock import Mock
        frame = Mock()
        for text in ("Card Number:\t999\tCard Merchant:\tNewk's Eatery\n", "Card Number:\t100\tCard Merchant:\tOther Brand\n", 'No accounts found'):
            frame.inner_text.return_value = text
            with self.assertRaises(RuntimeError):
                bot.verify_account(frame, '100')
        frame.inner_text.return_value = "Card Number:\t100\tCard Merchant:\tNewk's Eatery\nTemplate:\tGift Card"
        bot.verify_account(frame, '100')

    def test_report_retry_never_invokes_wallet_processing(self):
        import meal_card_reports as reports
        from datetime import date
        from unittest.mock import Mock
        day = date(2026, 11, 3)
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            source = Path(folder) / 'report.csv'
            source.write_bytes(b'csv')
            exporter = stack.enter_context(patch.object(reports, 'export_report', return_value=source))
            stack.enter_context(patch.object(reports, 'filter_report', side_effect=[reports.ReportNotReady('not yet'), b'filtered']))
            stack.enter_context(patch.object(reports.time, 'sleep'))
            stack.enter_context(patch.object(reports.time, 'monotonic', side_effect=[0, 1]))
            stack.enter_context(patch.object(reports, 'build_workbooks', return_value=({'stats': {'total_added': 60, 'payroll_total': 60}}, [])))
            process = stack.enter_context(patch.object(bot, 'process_card'))
            reports.run_reports(Mock(), Mock(), [CARD], set(), folder, day, day, {'100': 60})
            self.assertEqual(exporter.call_count, 2)
            process.assert_not_called()

    def test_single_run_uses_same_browser_for_report(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['bot']))
            stack.enter_context(patch.object(bot, '__file__', str(Path(folder) / 'bot.py')))
            app = Mock(scope='standalone:Newks-1085')
            app.worklist.return_value = {'year': 2026, 'period': 11, 'default_target': 160, 'cards': [CARD]}
            stack.enter_context(patch.object(bot, 'connect_to_backend', return_value=app))
            runtime = stack.enter_context(patch.object(bot, 'sync_playwright'))
            context = Mock(pages=[])
            stack.enter_context(patch.object(bot, 'connect_to_chrome', return_value=Mock(contexts=[context])))
            stack.enter_context(patch.object(bot, 'find_reload_surface', return_value=(Mock(), Mock())))
            process = stack.enter_context(patch.object(bot, 'process_card', return_value=('done', 'verified', 60, 100)))
            report = stack.enter_context(patch.object(bot, 'report_job'))
            bot.main()
            process.assert_called_once()
            report.assert_called_once()
            self.assertIs(report.call_args.kwargs['context'], context)
            self.assertEqual(report.call_args.args[4], {'100': 60})

class StandaloneTests(unittest.TestCase):
    def make_backend(self, folder, dry_run=False):
        import json
        from meal_card_local import LocalBackend
        roster = {'default_target': 160, 'cards': [dict(CARD)], 'employee_names': []}
        (Path(folder) / 'roster.json').write_text(json.dumps(roster))
        return LocalBackend(folder, dry_run=dry_run)

    def test_roster_and_progress_work_without_network_or_credentials(self):
        import socket
        with tempfile.TemporaryDirectory() as folder, patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
            backend = self.make_backend(folder)
            work = backend.worklist(2026, 11)
            self.assertEqual(work['cards'][0]['card_number'], '100')
            backend.start_run(2026, 11)
            backend.record_card('100', 'done', 60, 100)
            backend.finish_run(1, 0, 0, 'completed locally')
            restarted = self.make_backend(folder)
            self.assertEqual(restarted.worklist(2026, 11)['cards'], [])
            self.assertEqual(len(restarted.worklist(2026, 12)['cards']), 1)
            self.assertEqual(len(restarted.worklist(2026, 11, include_done=True)['cards']), 1)

    def test_dry_run_cannot_mark_local_card_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = self.make_backend(folder, dry_run=True)
            backend.start_run(2026, 11)
            backend.record_card('100', 'done', 60, 100)
            self.assertFalse(backend.results_path(2026, 11).exists())

    def test_no_url_or_password_prompt_in_connection(self):
        with tempfile.TemporaryDirectory() as folder, patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            self.make_backend(folder)
            from argparse import Namespace
            backend = bot.connect_to_backend(Path(folder), Namespace(roster=None, dry_run=True))
            self.assertEqual(len(backend.worklist(2026, 11)['cards']), 1)

    def test_duplicate_roster_cards_rejected(self):
        import json
        from meal_card_local import LocalBackend
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'roster.json'
            path.write_text(json.dumps({'default_target': 160, 'cards': [CARD, CARD]}))
            with self.assertRaisesRegex(ValueError, 'unique'):
                LocalBackend(folder)

class MerchantLabelTests(unittest.TestCase):
    def surface(self, text, href='/merchant/home.srv?currentMerchantId=1085'):
        from unittest.mock import Mock
        frame = Mock()
        frame.locator.return_value.inner_text.return_value = text
        frame.get_by_role.return_value.count.return_value = 1
        frame.get_by_role.return_value.get_attribute.return_value = href
        return frame

    def test_wrapped_typographic_label_is_accepted(self):
        from meal_card_organization import assert_merchant
        frame = self.surface("px PXS Newk’s\u00a0Eatery\n(1085) Home Report Center")
        assert_merchant(frame, timeout_seconds=0)
        frame.get_by_text.assert_not_called()

    def test_wrong_merchant_number_is_rejected(self):
        from meal_card_organization import assert_merchant
        with self.assertRaisesRegex(RuntimeError, 'Wrong Paytronix merchant'):
            assert_merchant(self.surface("Newk's Eatery (9999)"), timeout_seconds=0)

    def test_url_alone_cannot_satisfy_check(self):
        from meal_card_organization import assert_merchant
        with self.assertRaisesRegex(RuntimeError, 'visible Paytronix merchant'):
            assert_merchant(self.surface('Home Report Center'), timeout_seconds=0)

    def test_conflicting_home_merchant_is_rejected(self):
        from meal_card_organization import assert_merchant
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            assert_merchant(self.surface("Newk's Eatery (1085)", '/merchant/home.srv?currentMerchantId=9999'), timeout_seconds=0)
