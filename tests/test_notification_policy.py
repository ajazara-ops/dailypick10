import unittest
from pathlib import Path
import sys
from unittest.mock import Mock
from contextlib import redirect_stdout
import io
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notification_policy import push_eligible


class NotificationPolicyTests(unittest.TestCase):
    def test_both_supported_token_prefixes_survive_a_malformed_profile_token(self):
        from test_contract import isolated_scanner
        scanner = isolated_scanner(); scanner.FIREBASE_AVAILABLE = True
        scanner.firebase_admin = SimpleNamespace(_apps={'synthetic': object()})
        modern = 'ExpoPushToken' + '[' + 'SYNTHETIC_NOT_A_DEVICE' + ']'
        legacy = 'ExponentPushToken' + '[' + 'SYNTHETIC_NOT_A_DEVICE' + ']'
        docs = [Mock(to_dict=Mock(return_value=p)) for p in [
            {'pushToken': 123}, {'pushToken': modern}, {'pushToken': legacy},
            {'pushToken': modern, 'deletionRequest': {}}, {'pushToken': 'invalid'}]]
        scanner.firestore = Mock(); scanner.firestore.client.return_value.collection.return_value.stream.return_value = docs
        self.assertEqual(scanner.get_all_user_tokens(), [modern, legacy])

    def test_http_or_ticket_failures_are_not_reported_as_success(self):
        from test_contract import isolated_scanner
        for kind in ['http', 'ticket', 'malformed', 'timeout']:
            with self.subTest(kind=kind):
                scanner = isolated_scanner(); scanner.get_all_user_tokens = lambda: ['TEST_ONLY_A']
                response = Mock(); response.json.return_value = {'data': {'status': 'error'}} if kind == 'ticket' else {}
                if kind == 'http': response.raise_for_status.side_effect = RuntimeError('synthetic HTTP failure')
                scanner.requests.post = Mock(return_value=response)
                if kind == 'timeout': scanner.requests.post.side_effect = TimeoutError('synthetic timeout')
                with self.assertRaises(ValueError): scanner.send_push_notification('synthetic', 'synthetic')
                self.assertEqual(scanner.requests.post.call_count, 1)  # no duplicate-send retry

    def test_failed_send_logs_do_not_expose_recipient_or_exception_payload(self):
        from test_contract import isolated_scanner
        scanner = isolated_scanner(); scanner.get_all_user_tokens = lambda: ['SYNTHETIC_PRIVATE_RECIPIENT']
        scanner.requests.post = Mock(side_effect=RuntimeError('SYNTHETIC_PRIVATE_EXCEPTION'))
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(ValueError): scanner.send_push_notification('synthetic', 'synthetic')
        self.assertNotIn('SYNTHETIC_PRIVATE', output.getvalue())

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
        scanner.requests.post.return_value.json.return_value = {'data': {'status': 'ok', 'id': 'synthetic-ticket'}}
        scanner.send_push_notification('synthetic', 'synthetic')
        import json
        recipients = [json.loads(call.kwargs['data'])['to'] for call in scanner.requests.post.call_args_list]
        self.assertEqual(recipients, ['TEST_ONLY_A', 'TEST_ONLY_B'])
        self.assertTrue(all(call.kwargs['timeout'] == 15 for call in scanner.requests.post.call_args_list))

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
