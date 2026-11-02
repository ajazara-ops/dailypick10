"""Run the actual scoring function with synthetic OHLCV and provider inputs only."""
import ast
from datetime import datetime
import math
from pathlib import Path
import sys
import types
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scan_contract import Observation, SourceDataError, TIMEZONES, number, price


def analyze(info, naver):
    dates = pd.bdate_range(end='2026-10-30', periods=130)
    hist = pd.DataFrame({'Close':[100 + i % 2 for i in range(130)],
                         'High':[102]*130,'Low':[99]*130,'Volume':[1000]*130}, index=dates)
    class Ticker:
        def __init__(self, _): self.info = info
        def history(self, **_): return hist.copy()
    names = {'safe_float', 'calculate_indicators', 'analyze_stock'}
    tree = ast.parse((ROOT/'daily_stock_scanner.py').read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    scope = dict(pd=pd, math=math, yf=types.SimpleNamespace(Ticker=Ticker),
                 get_kr_fundamental=lambda _: naver, datetime=datetime,
                 Observation=Observation, SourceDataError=SourceDataError,
                 TIMEZONES=TIMEZONES, number=number, price=price)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual_scoring_functions', 'exec'), scope)
    return scope['analyze_stock']('000001.KS', 'KR').stock


class FinancialFallbackTests(unittest.TestCase):
    def test_missing_primary_margin_cannot_erase_known_loss_filter(self):
        self.assertIsNone(analyze({'operatingMargins':-.1}, {'op_margin':None,'per':None,'pbr':None}))

    def test_partial_primary_keeps_independent_available_values(self):
        result = analyze({'operatingMargins':.2,'forwardPE':8,'priceToBook':.7},
                         {'op_margin':None,'per':12,'pbr':None})
        self.assertEqual(result['financials']['op_margin'], .2)
        self.assertEqual(result['financials']['per'], 12)
        self.assertIn('저PBR', result['aiReason'])
        self.assertIn('흑자 기업', result['aiReason'])

    def test_real_zero_is_not_missing_and_overrides_negative_fallback(self):
        result = analyze({'operatingMargins':-.1,'forwardPE':8,'priceToBook':.7},
                         {'op_margin':0,'per':0,'pbr':0})
        self.assertIsNotNone(result)
        self.assertEqual(result['financials']['op_margin'], 0)
        self.assertEqual(result['financials']['per'], 0)
        self.assertNotIn('적정 PER', result['aiReason'])
        self.assertNotIn('저PBR', result['aiReason'])

    def test_invalid_primary_cannot_erase_valid_fallback(self):
        for invalid in [float('nan'),float('inf'),True,'unavailable']:
            with self.subTest(value=invalid):
                self.assertIsNone(analyze({'operatingMargins':-.1}, {'op_margin':invalid,'per':None,'pbr':None}))

    def test_both_missing_remain_optional_and_unavailable(self):
        result = analyze({}, {'op_margin':None,'per':None,'pbr':None})
        self.assertIsNotNone(result)
        self.assertIsNone(result['financials']['op_margin'])
        self.assertIsNone(result['financials']['per'])

    def test_valid_primary_still_has_same_priority(self):
        self.assertIsNotNone(analyze({'operatingMargins':-.1}, {'op_margin':.1,'per':None,'pbr':None}))


if __name__ == '__main__': unittest.main()
