"""Local synthetic tests: actual scanner entry point; no external libraries or calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest.mock import patch

for name in ('yfinance', 'pandas', 'requests'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['firebase_admin'] = None
with contextlib.redirect_stdout(io.StringIO()):
    import daily_stock_scanner as scanner

STAMP = '2026-11-07T08:30:00+00:00'
DATE = '2026-11-07'


class WeeklyNoticeTests(unittest.TestCase):
    def setUp(self):
        previous = os.getcwd()
        synthetic_dir = Path(previous) / '.synthetic-test-data' / uuid.uuid4().hex
        synthetic_dir.mkdir(parents=True)
        os.chdir(synthetic_dir)
        self.addCleanup(os.chdir, previous)
        Path('daily_data').mkdir()
        self.clock = patch.object(scanner, 'utc_stamp', return_value=STAMP)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.settle = patch.object(scanner, 'settle_weekly', return_value=([], {'KR':{}, 'US':{}}))
        self.settle_mock = self.settle.start(); self.addCleanup(self.settle.stop)

    def main(self, *args):
        with patch.object(sys, 'argv', ['daily_stock_scanner.py', *args]), contextlib.redirect_stdout(io.StringIO()):
            return scanner.main()

    def test_first_weekly_sends_once_and_repeated_generation_keeps_frozen_bytes(self):
        self.main('--mode', 'weekly_summary')
        path = Path('weekly_reports') / f'{DATE}_v3_weekly.json'
        first = path.read_bytes()
        with patch.object(scanner, 'send_push_notification') as send:
            self.main('--mode','notify','--notify')
            self.assertEqual(send.call_count, 1)
            self.main('--mode','notify','--notify')
            self.assertEqual(send.call_count, 1)
            self.main('--mode', 'weekly_summary')
            self.assertEqual(path.read_bytes(), first)
            self.main('--mode','notify','--notify')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(json.loads(Path('.pending_notification.json').read_text('utf-8'))['reason'], 'weekly_report_already_exists')
        self.assertEqual(self.settle_mock.call_count, 1)

    def test_failed_or_unknown_delivery_is_not_resent_by_a_new_weekly_run(self):
        self.main('--mode','weekly')
        with patch.object(scanner, 'send_push_notification', side_effect=RuntimeError('unknown')) as send:
            with self.assertRaises(RuntimeError): self.main('--mode','notify','--notify')
            self.main('--mode','notify','--notify')
            self.main('--mode','weekly')
            self.main('--mode','notify','--notify')
            self.assertEqual(send.call_count, 1)

    def test_invalid_frozen_report_stops_without_replacing_history_or_notification(self):
        self.main('--mode','weekly')
        report=Path('weekly_reports')/f'{DATE}_v3_weekly.json'
        report.write_text('{bad', encoding='utf-8')
        pending=Path('.pending_notification.json').read_bytes()
        index=Path('history_index.json').read_bytes()
        with self.assertRaises(ValueError): self.main('--mode','weekly')
        self.assertEqual(report.read_text('utf-8'),'{bad')
        self.assertEqual(Path('.pending_notification.json').read_bytes(),pending)
        self.assertEqual(Path('history_index.json').read_bytes(),index)

    def test_claim_write_failure_blocks_send(self):
        self.main('--mode','weekly')
        with patch.object(scanner, 'write_latest', side_effect=OSError('synthetic')), \
             patch.object(scanner, 'send_push_notification') as send:
            with self.assertRaises(OSError): self.main('--mode','notify','--notify')
            send.assert_not_called()

    def test_existing_report_does_not_imply_prior_delivery_and_is_not_auto_resent(self):
        scanner.generate_weekly_report(DATE, now=STAMP)
        self.main('--mode','weekly')
        with patch.object(scanner, 'send_push_notification') as send:
            self.main('--mode','notify','--notify')
            send.assert_not_called()

    def test_daily_notification_contract_is_unchanged(self):
        scanner.write_latest('.pending_notification.json', {'title':'daily','body':'synthetic'})
        with patch.object(scanner, 'send_push_notification') as send:
            self.main('--mode','notify','--notify')
            send.assert_called_once_with('daily','synthetic')

    def test_original_generate_api_still_returns_path(self):
        first=scanner.generate_weekly_report(DATE, now=STAMP)
        second=scanner.generate_weekly_report(DATE, now=STAMP)
        self.assertIsInstance(first,Path)
        self.assertEqual(first,second)


if __name__ == '__main__': unittest.main()
