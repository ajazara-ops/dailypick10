"""Synthetic market-index safety tests. No external requests."""
import contextlib
import importlib.util
import io
import math
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import patch

stubs = {name: types.ModuleType(name) for name in ('yfinance', 'pandas', 'requests')}
stubs['firebase_admin'] = None
with patch.dict(sys.modules, stubs), contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location('index_scanner', Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)


class Values:
    def __init__(self, values):
        self.iloc = values


class History:
    def __init__(self, values):
        self.values = values
    def __len__(self):
        return len(self.values)
    def __getitem__(self, key):
        assert key == 'Close'
        return Values(self.values)


class IndexValidation(unittest.TestCase):
    def index_result(self, values):
        ticker = types.SimpleNamespace(history=lambda **kwargs: History(values))
        with patch.object(scanner.yf, 'Ticker', return_value=ticker, create=True), \
             patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
             contextlib.redirect_stdout(io.StringIO()):
            return scanner.analyze_market_condition()

    def test_invalid_latest_or_previous_price_is_unknown_not_panic(self):
        for value in (None, float('nan'), float('inf'), -1.0, 0.0):
            for values in ([100.0, value], [value, 100.0]):
                with self.subTest(values=values):
                    result = self.index_result(values)
                    for index in result.values():
                        self.assertEqual(index['status'], 'UNKNOWN')
                        self.assertIsNone(index['current'])
                        self.assertIsNone(index['change'])

    def test_valid_price_keeps_existing_classification(self):
        result = self.index_result([100.0, 101.0])
        self.assertEqual((result['KR']['current'], result['KR']['change'], result['KR']['status']), (101.0, 1.0, 'VERY_GOOD'))
        self.assertEqual((result['VIX']['current'], result['VIX']['status']), (101.0, 'PANIC'))

    def test_nonfinite_computed_return_is_unknown(self):
        result = self.index_result([1e-308, 1e308])
        self.assertEqual(result['KR']['status'], 'UNKNOWN')
        self.assertIsNone(result['KR']['current'])

    def test_one_invalid_market_does_not_hide_valid_indexes(self):
        histories = {'^GSPC': [100.0, 101.0], '^KS11': [100.0, float('nan')], '^VIX': [15.0, 16.0]}
        def ticker(name):
            return types.SimpleNamespace(history=lambda **kwargs: History(histories[name]))
        with patch.object(scanner.yf, 'Ticker', side_effect=ticker, create=True), \
             patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
             contextlib.redirect_stdout(io.StringIO()):
            result = scanner.analyze_market_condition()
        self.assertEqual(result['KR']['status'], 'UNKNOWN')
        self.assertEqual(result['US']['status'], 'VERY_GOOD')
        self.assertEqual(result['VIX']['status'], 'NEUTRAL')


if __name__ == '__main__':
    unittest.main()
