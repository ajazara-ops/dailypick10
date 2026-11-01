"""Synthetic score contracts only: never imports or executes the live scanner."""
from copy import deepcopy
from pathlib import Path
import ast
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scan_contract import Observation, scan_market, score_inputs_available

NOW = '2026-10-31T12:00:00+00:00'
def row(**extra):
    return dict(dict(id='A', market='US', currentPrice=9.5, score=85, rsi=0,
                     financials={'op_margin': None, 'rev_growth': None, 'per': None}), **extra)
def scan(stock, previous=None):
    return scan_market('US', lambda: ['A'], lambda *_: Observation(stock, '2026-10-30'), previous or {}, NOW)

class ScoreContractTests(unittest.TestCase):
    def test_optional_gaps_and_real_zero_values(self):
        for stock in [row(), row(rsi=None, rvol=0, score=0), row(rsi=None, scoreInputs={'macd':0,'signal':0}), row(rsi=None, scoreInputs={'ma60':9})]:
            rows, meta = scan(stock)
            self.assertEqual(meta['errorCount'],0)
            self.assertEqual(rows[0]['scoreAsOf'],NOW)
            self.assertEqual(rows[0]['score'],stock['score'])
            self.assertEqual(rows[0]['rank'],1)

    def test_bad_evidence_is_error_not_a_successfully_ranked_candidate(self):
        for stock in [row(currentPrice=None),row(score=None),row(score=-1),row(rsi=None),row(rsi=101,rvol=-1),row(rsi=None,scoreInputs={'macd':0}),row(rsi=None,scoreInputs={'stochK':101})]:
            rows, meta = scan(stock)
            self.assertEqual(rows,[])
            self.assertEqual(meta['status'],'error')
            self.assertEqual((meta['errorCount'],meta['filteredCount']),(1,0))

    def test_failure_preserves_actual_previous_score_date_without_redating(self):
        previous={'stocks':[row(scoreAsOf='2026-10-20T23:15:00Z',generatedAt='2026-10-20T23:15:00Z')],
                  'markets':{'US':{'status':'fresh','generatedAt':'2026-10-20T23:15:00Z','quoteAsOf':'2026-10-20'}}}
        before=deepcopy(previous)
        rows, meta=scan(row(rsi=None),previous)
        self.assertEqual(rows,previous['stocks']);self.assertEqual(previous,before)
        self.assertTrue(meta['stale']);self.assertEqual(meta['status'],'error')
        self.assertNotEqual(rows[0]['scoreAsOf'],NOW)

    def test_missing_previous_date_is_not_invented(self):
        previous={'stocks':[row()]}
        rows,_=scan(row(rsi=None),previous)
        self.assertNotIn('scoreAsOf',rows[0]);self.assertNotIn('generatedAt',rows[0])

    def test_new_scanner_emits_all_calculated_alternative_inputs(self):
        tree=ast.parse((ROOT/'daily_stock_scanner.py').read_text('utf-8'))
        dictionaries=[node for node in ast.walk(tree) if isinstance(node,ast.Dict)]
        exported=[value for node in dictionaries for key,value in zip(node.keys,node.values) if isinstance(key,ast.Constant) and key.value=='scoreInputs']
        self.assertEqual(len(exported),1)
        self.assertEqual({key.value for key in exported[0].keys},{'bbLower','ma60','ma120','macd','signal','stochK'})
        self.assertFalse(score_inputs_available(row(rsi=None,scoreInputs={'macd':True,'signal':0})))

if __name__=='__main__': unittest.main()
