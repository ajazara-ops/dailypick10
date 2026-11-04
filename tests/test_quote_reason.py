"""No network or real market data: prove bounded diagnosis and stale preservation."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import patch

import scan_contract

stubs = {name: types.ModuleType(name) for name in ('yfinance', 'pandas', 'requests')}
stubs['firebase_admin'] = None
with patch.dict(sys.modules, stubs), contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location('quote_diagnosis_scanner', Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)


class Date:
    def strftime(self, fmt):
        return '2026-11-03'


class Frame:
    empty = False
    index = [Date()]
    def __init__(self, value, actions=None):
        self.value = value
        self.actions = actions or {}
    def __getitem__(self, column):
        if column == 'Close':
            return types.SimpleNamespace(iloc=[self.value])
        return types.SimpleNamespace(iloc=[self.actions[column]])


class QuoteReasonTest(unittest.TestCase):
    def run_scan(self, value, actions=None):
        ticker = types.SimpleNamespace(history=lambda **kwargs: Frame(value, actions))
        previous = {'stocks': [{'id': '000001.KS', 'market': 'KR', 'currentPrice': 123}],
                    'markets': {'KR': {'quoteAsOf': '2026-11-02', 'generatedAt': '2026-11-02T09:00:00+00:00'}}}
        before = json.dumps(previous, sort_keys=True)
        logs = io.StringIO()
        with patch.object(scanner.yf, 'Ticker', return_value=ticker, create=True), \
             patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
             contextlib.redirect_stderr(logs):
            rows, meta = scan_contract.scan_market('KR', lambda: ['000001.KS'], scanner.analyze_stock,
                                                   previous, '2026-11-03T09:30:00+00:00')
        self.assertEqual(json.dumps(previous, sort_keys=True), before)
        self.assertEqual(rows, previous['stocks'])
        self.assertEqual((meta['status'], meta['stale'], meta['errorCount'], meta['observedCount']), ('error', True, 1, 0))
        return json.loads(logs.getvalue())

    def test_actual_scanner_reports_safe_distinct_reasons(self):
        for value, code in [(None, 'invalid_quote_missing'), (float('nan'), 'invalid_quote_nan'),
                            (float('inf'), 'invalid_quote_infinite'), (0, 'invalid_quote_nonpositive'),
                            (-9, 'invalid_quote_nonpositive'), (True, 'invalid_quote_unreadable')]:
            with self.subTest(code=code, value=value):
                row = self.run_scan(value)
                self.assertEqual((row['stage'], row['category'], row['errorCode']), ('quote_validation', 'source_data', code))
                self.assertIsNone(row['httpStatus'])

    def test_invalid_value_text_is_never_emitted(self):
        row = self.run_scan('private-token-not-a-price')
        wire = json.dumps(row)
        self.assertEqual(row['errorCode'], 'invalid_quote_unreadable')
        self.assertNotIn('private-token', wire)

    def test_nan_with_action_has_bounded_category_without_action_value(self):
        row = self.run_scan(float('nan'), {'Dividends': 737373.0})
        self.assertEqual(row['errorCode'], 'invalid_quote_nan_with_action')
        self.assertNotIn('737373', json.dumps(row))
        # An unrecognized/missing action value is not interpreted as no action.
        row = self.run_scan(float('nan'), {'Dividends': None})
        self.assertEqual(row['errorCode'], 'invalid_quote_nan')

    def test_legacy_invalid_quote_stays_understood(self):
        from scan_diagnostics import failure_record
        row = failure_record(scan_contract.SourceDataError('invalid_quote_price'), market='KR', ticker='000001.KS',
                             stage='stock_analysis', generated_at='2026-11-03T09:30:00+00:00',
                             source_error_type=scan_contract.SourceDataError)
        self.assertEqual((row['stage'], row['errorCode']), ('quote_validation', 'invalid_quote_price'))


if __name__ == '__main__':
    unittest.main()
