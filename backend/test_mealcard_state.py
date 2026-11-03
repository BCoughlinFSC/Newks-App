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
                     'assert_organization', 'fill_adjustment_form', 'confirm_adjustment', 'verify_results', 'get_current_balance'):
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
            for name in ('lookup_card', 'navigate_to_adjust_wallet', 'select_stored_value_wallet', 'fill_adjustment_form'):
                stack.enter_context(patch.object(bot, name))
            stack.enter_context(patch.object(bot, 'get_current_balance', return_value=100))
            guard = stack.enter_context(patch.object(bot, 'assert_organization', side_effect=[None, None, RuntimeError('organization changed')]))
            confirm = stack.enter_context(patch.object(bot, 'confirm_adjustment'))
            with self.assertRaises(PendingSubmission):
                bot.process_card(None, CARD, False, state, organization_page=Mock())
            confirm.assert_not_called()
            self.assertEqual(state.get('100')['status'], 'pending')
