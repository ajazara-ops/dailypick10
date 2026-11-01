import unittest
from pathlib import Path
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notification_policy import push_eligible


class NotificationPolicyTests(unittest.TestCase):
    def test_deletion_marker_and_tombstone_exclude_even_malformed_values(self):
        self.assertTrue(push_eligible({}))
        for value in [None, {}, {'state': 'completed'}]:
            self.assertFalse(push_eligible({'deletionRequest': value}))
        self.assertFalse(push_eligible({}, blocked=True))
        self.assertFalse(push_eligible(None))

    def test_scanner_uses_predicate_without_importing_deferred_worker(self):
        from test_contract import isolated_scanner
        scanner = isolated_scanner()
        self.assertIs(scanner.push_eligible, push_eligible)
        source = (Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py').read_text('utf-8')
        self.assertNotIn('from account_deletion import', source)

    def test_sender_uses_only_eligible_db_tokens_and_deduplicates(self):
        from test_contract import isolated_scanner
        scanner = isolated_scanner()
        # Synthetic identifiers only; no actual device tokens or external calls.
        scanner.get_all_user_tokens = lambda: ['TEST_ONLY_A', 'TEST_ONLY_A', 'TEST_ONLY_B']
        scanner.requests.post = Mock()
        scanner.send_push_notification('synthetic', 'synthetic')
        import json
        recipients = [json.loads(call.kwargs['data'])['to'] for call in scanner.requests.post.call_args_list]
        self.assertEqual(recipients, ['TEST_ONLY_A', 'TEST_ONLY_B'])

    def test_empty_or_failed_eligible_lookup_cannot_fall_back_to_fixed_recipient(self):
        from test_contract import isolated_scanner
        scanner = isolated_scanner()
        scanner.get_all_user_tokens = lambda: []
        scanner.requests.post = Mock()
        scanner.send_push_notification('synthetic', 'synthetic')
        scanner.requests.post.assert_not_called()
        source = (Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py').read_text('utf-8')
        self.assertNotIn('ExponentPushToken[', source)


if __name__ == '__main__':
    unittest.main()
