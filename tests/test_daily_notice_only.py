"""Synthetic daily dedup tests: no live quotes, Firebase or push calls."""
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import types
import unittest
import uuid
from unittest.mock import patch

from market_calendar import session_close_utc

stubs = {name:types.ModuleType(name) for name in ('yfinance','pandas','requests')}
stubs['firebase_admin'] = None
with patch.dict(sys.modules, stubs), contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location('daily_notice_scanner_under_test',
                                                  Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)

DAY='2026-11-02'
NOW='2026-11-02T10:00:00+00:00'
CLOSE=datetime(2026,11,2,6,30,tzinfo=timezone.utc)

def metadata(**changes):
    return {'status':'fresh','stale':False,'timezone':'Asia/Seoul','quoteAsOf':DAY,
            'generatedAt':'2026-11-02T09:00:00+00:00','attemptedCount':100,
            'observedCount':100,'errorCount':0,**changes}

def previous(meta=None):
    return {'stocks':[{'id':'000001.KS','symbol':'SYNTH','market':'KR','currentPrice':100,'score':50}],
            'markets':{'KR':metadata() if meta is None else meta},'market_status':{}}


class DailyNoticeTests(unittest.TestCase):
    def setUp(self):
        old=Path.cwd(); synthetic=old/'.synthetic-test-data'/uuid.uuid4().hex
        synthetic.mkdir(parents=True); os.chdir(synthetic); self.addCleanup(os.chdir,old)
        self.time=patch.object(scanner,'utc_stamp',return_value=NOW); self.time.start(); self.addCleanup(self.time.stop)
        self.day=patch.object(scanner,'market_session_date',return_value=DAY); self.day.start(); self.addCleanup(self.day.stop)
        self.close=patch.object(scanner,'session_close_utc',return_value=CLOSE); self.close.start(); self.addCleanup(self.close.stop)
        for method in ('connect','connect_ex'):
            guard=patch.object(socket.socket,method,side_effect=AssertionError('external networking forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def write_previous(self, obj):
        scanner.write_latest('todays_recommendation.json',obj)

    def notify(self):
        with patch.object(sys,'argv',['scanner','--mode','notify','--notify']), contextlib.redirect_stdout(io.StringIO()):
            return scanner.main()

    def test_completed_duplicate_skips_scan_history_and_notice(self):
        prior=previous();self.write_previous(prior);old=Path('todays_recommendation.json').read_bytes()
        with patch.object(scanner,'scan_market',side_effect=AssertionError('unexpected scan')) as scan, \
             patch.object(scanner,'analyze_market_condition',side_effect=AssertionError('unexpected index')), \
             patch.object(scanner,'send_push_notification') as send:
            self.assertEqual(scanner.generate_daily('KR'),prior)
            self.notify();scan.assert_not_called();send.assert_not_called()
        self.assertEqual(Path('todays_recommendation.json').read_bytes(),old)
        self.assertFalse(Path('daily_data').exists())

    def test_incomplete_ambiguous_or_before_close_never_reused(self):
        cases=[metadata(status='partial'),metadata(stale=True),metadata(errorCount=1),metadata(observedCount=99),
               metadata(attemptedCount=0,observedCount=0),metadata(quoteAsOf='2026-10-30'),
               metadata(generatedAt='2026-11-02T06:00:00+00:00'),metadata(generatedAt='2026-11-02T09:00:00'),
               metadata(generatedAt='2026-11-02T11:00:00+00:00'),metadata(timezone='America/New_York')]
        for meta in cases:
            with self.subTest(meta=meta):self.assertFalse(scanner.matching_completed_daily(previous(meta),'KR',DAY,NOW))
        self.assertFalse(scanner.matching_completed_daily(previous(),'KR','2026-11-03',NOW))
        with patch.object(scanner,'session_close_utc',return_value=None):
            self.assertFalse(scanner.matching_completed_daily(previous(),'KR',DAY,NOW))
        with patch.object(scanner,'session_close_utc',side_effect=RuntimeError('synthetic')):
            self.assertFalse(scanner.matching_completed_daily(previous(),'KR',DAY,NOW))
        for invalid in ({'id':'','symbol':'TEST','market':'KR','currentPrice':100,'score':50},
                        {'id':'000001.KS','symbol':'','market':'KR','currentPrice':100,'score':50},
                        {'id':'000001.KS','symbol':'TEST','market':'KR','currentPrice':0,'score':50},
                        {'id':'000001.KS','symbol':'TEST','market':'KR','currentPrice':100,'score':-1},
                        'invalid-record'):
            p=previous();p['stocks']=[invalid]
            self.assertFalse(scanner.matching_completed_daily(p,'KR',DAY,NOW))
        p=previous();p['stocks'].append(deepcopy(p['stocks'][0]))
        self.assertFalse(scanner.matching_completed_daily(p,'KR',DAY,NOW))

    def test_empty_success_requires_complete_observations_and_no_prior_rows(self):
        p=previous(metadata(status='empty'));p['stocks']=[]
        self.assertTrue(scanner.matching_completed_daily(p,'KR',DAY,NOW))
        p['stocks']=[{'id':'000001.KS','market':'KR'}]
        self.assertFalse(scanner.matching_completed_daily(p,'KR',DAY,NOW))

    def test_preclose_same_date_is_not_mistaken_for_finished_session(self):
        p=previous(metadata(generatedAt='2026-11-02T06:00:00+00:00'))
        self.assertFalse(scanner.matching_completed_daily(p,'KR',DAY,NOW))

    def test_all_target_skips_only_market_with_verified_completed_session(self):
        stamp='2026-11-02T23:30:00+00:00'
        us={'status':'fresh','stale':False,'timezone':'America/New_York','quoteAsOf':'2026-11-02',
            'generatedAt':'2026-11-02T22:00:00+00:00','attemptedCount':3,'observedCount':3,'errorCount':0}
        prior={'stocks':[{'id':'SYNTH','symbol':'SYNTH','market':'US','currentPrice':10,'score':50}],
               'markets':{'US':us},'market_status':{}}
        self.write_previous(prior)
        meta=metadata(quoteAsOf='2026-11-03',generatedAt=stamp,attemptedCount=1,observedCount=1)
        rows=[{'id':'000001.KS','symbol':'TEST','market':'KR','currentPrice':100,'score':50}]
        def close(market,day): return datetime(2026,11,2,21,tzinfo=timezone.utc) if market=='US' else None
        with patch.object(scanner,'utc_stamp',return_value=stamp), \
             patch.object(scanner,'market_session_date',side_effect=lambda m:'2026-11-02' if m=='US' else '2026-11-03'), \
             patch.object(scanner,'session_close_utc',side_effect=close), \
             patch.object(scanner,'scan_market',return_value=(rows,meta)) as scan, \
             patch.object(scanner,'process_news_for_list'),patch.object(scanner,'analyze_market_condition',return_value={}):
            result=scanner.generate_daily('ALL')
        self.assertEqual(scan.call_count,1);self.assertEqual(scan.call_args.args[0],'KR')
        self.assertEqual(result['markets']['US'],us)
        self.assertEqual({row['market'] for row in result['stocks']},{'US','KR'})
        self.assertNotIn('noticeKind',result['notification'])

    def test_daily_notice_claim_suppresses_same_workspace_retry(self):
        self.write_previous({})
        rows=[{'id':'000001.KS','symbol':'TEST','market':'KR','currentPrice':100,'score':50}]
        meta=metadata(attemptedCount=1,observedCount=1)
        with patch.object(scanner,'scan_market',return_value=(rows,meta)), \
             patch.object(scanner,'process_news_for_list'),patch.object(scanner,'analyze_market_condition',return_value={}):
            scanner.generate_daily('KR')
        with patch.object(scanner,'send_push_notification',side_effect=RuntimeError('synthetic')) as send:
            with self.assertRaises(RuntimeError):self.notify()
            self.notify();self.assertEqual(send.call_count,1)

    def test_first_run_then_new_attempt_uses_saved_result_without_new_history_or_notice(self):
        self.write_previous({})
        rows=[{'id':'000001.KS','symbol':'TEST','market':'KR','currentPrice':100,'score':50}]
        meta=metadata(generatedAt=NOW,attemptedCount=1,observedCount=1)
        with patch.object(scanner,'scan_market',return_value=(rows,meta)) as scan, \
             patch.object(scanner,'process_news_for_list'),patch.object(scanner,'analyze_market_condition',return_value={}), \
             patch.object(scanner,'send_push_notification') as send:
            scanner.generate_daily('KR');self.notify()
            saved=Path('todays_recommendation.json').read_bytes()
            history={p.name:p.read_bytes() for p in Path('daily_data').iterdir()}
            with patch.object(scanner,'utc_stamp',return_value='2026-11-02T10:05:00+00:00'):
                scanner.generate_daily('KR');self.notify()
            self.assertEqual(scan.call_count,1);self.assertEqual(send.call_count,1)
        self.assertEqual(Path('todays_recommendation.json').read_bytes(),saved)
        self.assertEqual({p.name:p.read_bytes() for p in Path('daily_data').iterdir()},history)

    def test_prior_saved_report_after_unknown_delivery_skips_automatic_resend(self):
        self.write_previous(previous())
        with patch.object(scanner,'send_push_notification') as send, patch.object(scanner,'scan_market') as scan:
            scanner.generate_daily('KR');self.notify()
            send.assert_not_called();scan.assert_not_called()

    def test_claim_failure_blocks_send(self):
        scanner.write_latest('.pending_notification.json',{'noticeKind':'daily-v1','title':'synthetic','body':'synthetic'})
        with patch.object(scanner,'write_latest',side_effect=OSError('synthetic')), \
             patch.object(scanner,'send_push_notification') as send:
            with self.assertRaises(OSError):self.notify()
            send.assert_not_called()

    def test_calendar_close_dst_early_closing_and_holidays(self):
        self.assertEqual(session_close_utc('KR','2026-11-02'),CLOSE)
        self.assertEqual(session_close_utc('US','2026-03-06').hour,21)
        self.assertEqual(session_close_utc('US','2026-03-09').hour,20)
        self.assertEqual(session_close_utc('US','2026-11-27').hour,18)
        self.assertIsNone(session_close_utc('KR','2026-10-09'))
        self.assertIsNone(session_close_utc('US','2026-12-25'))


if __name__ == '__main__': unittest.main()
