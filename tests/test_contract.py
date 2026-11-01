import ast
import io
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scan_contract import (Observation, number, price, scan_market, merge_daily, daily_notification,
                           cohort_from_snapshots, settle_cohort, build_report, rank_stocks, write_json_once, write_latest)
from scripts.resolve_scan_event import resolve_event

NOW = '2026-10-30T23:15:00+00:00'
TEST_TEMP = ROOT / '.test-tmp'
TEST_TEMP.mkdir(exist_ok=True)
def row(ticker='A', **fields):
    return dict(id=ticker, symbol=ticker, market='US', score=50, currentPrice=100, rsi=0,
                priceBasis='unadjusted_close', recommendDate='2026-10-23', buyPrice=100,
                recommendationId=ticker, **fields)


class ContractTests(unittest.TestCase):
    def test_decimal_and_low_prices(self):
        self.assertEqual(price(9.975, 'US'), 9.98)
        self.assertEqual(price(.525, 'US'), .525)
        self.assertEqual(price(72500.6, 'KR'), 72501)
        for invalid in [None, 0, -1, float('nan'), float('inf'), True]:
            self.assertIsNone(price(invalid, 'US'))
        self.assertEqual(number(0), 0)

    def test_delayed_schedules_keep_event_intent(self):
        # Runner time is deliberately irrelevant and is not an input.
        for schedule, expected in [('30 9 * * 1-5', ('daily','KR')), ('15 23 * * 1-5', ('daily','US')), ('0 8 * * 6', ('weekly_summary','ALL'))]:
            self.assertEqual(resolve_event('schedule', {'schedule':schedule}), expected)
        self.assertEqual(resolve_event('workflow_dispatch', {'inputs':{'mode':'weekly','target':'US'}}), ('weekly','US'))
        for event, payload in [('schedule',{'schedule':'unexpected'}), ('workflow_dispatch',{'inputs':{'mode':'auto'}}), ('push',{})]:
            with self.assertRaises(ValueError): resolve_event(event,payload)

    def test_stable_ties_and_truthful_fillers(self):
        rows=[row('Z'), row('A')]
        self.assertEqual([s['id'] for s in rank_stocks(rows)], ['A','Z'])
        rows[0]['score']=5
        low=rank_stocks(rows)[1]
        self.assertIn('기준 점수 미달', low['aiReason'])
        self.assertEqual(low['analysisMethod'],'rules_v1')

    def test_failed_scan_preserves_last_good_and_is_not_no_recommendation(self):
        previous={'stocks':[row('old')], 'markets':{'US':{'generatedAt':'2026-10-29T23:00:00Z','quoteAsOf':'2026-10-29','status':'fresh'}}}
        rows, meta=scan_market('US',lambda:[],lambda *_:None,previous,NOW)
        self.assertEqual(rows,previous['stocks']);self.assertEqual(meta['status'],'error')
        self.assertTrue(meta['stale']);self.assertEqual(meta['generatedAt'],'2026-10-29T23:00:00Z')
        notification=daily_notification({'US':(rows,meta)})['body']
        self.assertIn('오류',notification);self.assertNotIn('관망',notification)

    def test_successful_filtering_is_distinct_from_quote_failure(self):
        rows, meta=scan_market('US',lambda:['A'],lambda *_:Observation(None,'2026-10-30'),{},NOW)
        self.assertEqual(rows,[]);self.assertEqual(meta['status'],'empty');self.assertEqual(meta['errorCount'],0)
        self.assertIn('조건',daily_notification({'US':(rows,meta)})['body'])

    def test_partial_scan_retains_previous_full_snapshot(self):
        previous={'stocks':[row('old')]}
        def analyze(ticker,market):
            if ticker=='B': raise RuntimeError('price failure')
            return Observation(row(ticker),'2026-10-30')
        rows,meta=scan_market('US',lambda:['B','A'],analyze,previous,NOW)
        self.assertEqual(rows,previous['stocks']);self.assertEqual(meta['status'],'partial');self.assertEqual(meta['errorCount'],1)

    def test_new_install_partial_scan_reports_incomplete_coverage(self):
        def analyze(ticker,market):
            if ticker=='B': raise RuntimeError('price failure')
            return Observation(row(ticker),'2026-10-30')
        rows,meta=scan_market('US',lambda:['B','A'],analyze,{},NOW)
        self.assertEqual(len(rows),1);self.assertEqual(meta['status'],'partial');self.assertTrue(meta['stale'])

    def test_stale_quotes_are_errors(self):
        rows,meta=scan_market('US',lambda:['A'],lambda *_:Observation(row(),'2026-10-01'),{},NOW)
        self.assertEqual(rows,[]);self.assertEqual(meta['errorCount'],1)

    def test_market_timestamps_are_not_overwritten_by_other_market(self):
        previous={'stocks':[dict(row('K'),market='KR')], 'markets':{'KR':{'quoteAsOf':'2026-10-29','generatedAt':'2026-10-29T09:30:00Z','status':'fresh'}}}
        updated=scan_market('US',lambda:['A'],lambda *_:Observation(row(),'2026-10-30'),previous,NOW)
        merged=merge_daily(previous,{'US':updated},NOW)
        self.assertEqual(merged['markets']['KR'],previous['markets']['KR'])
        self.assertEqual(merged['markets']['US']['quoteAsOf'],'2026-10-30')
        self.assertEqual(len(merged['stocks']),2)

    def test_all_recommendation_events_preserved_carry_forward_deduplicated(self):
        first=row('A');second=dict(row('A'),recommendationId='A-day2')
        snapshots=[('2026-10-23_a_daily.json',{'stocks':[first]}),('2026-10-23_b_daily.json',{'stocks':[first]}),('2026-10-24_daily.json',{'stocks':[second]})]
        self.assertEqual(len(cohort_from_snapshots(snapshots)),2)
        self.assertNotIn('sourceFile',first)

    def test_carried_forward_event_outside_report_window_is_excluded(self):
        old=dict(row('old'),generatedAt='2026-09-01T23:15:00Z')
        new=dict(row('new'),generatedAt='2026-10-23T23:15:00Z')
        snapshots=[('2026-10-30_daily.json',{'stocks':[old,new]})]
        self.assertEqual([s['id'] for s in cohort_from_snapshots(snapshots,'2026-10-16','2026-10-30')],['new'])

    def test_invalid_price_is_failure_not_observed_or_filtered(self):
        invalid=dict(row(),currentPrice=None)
        rows,meta=scan_market('US',lambda:['A'],lambda *_:Observation(invalid,'2026-10-30'),{},NOW)
        self.assertEqual(rows,[])
        self.assertEqual((meta['errorCount'],meta['observedCount'],meta['filteredCount']),(1,0,0))

    def test_invalid_serialization_does_not_create_a_partial_history_file(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            assert Path(folder).resolve().is_relative_to(ROOT.resolve())
            target=Path(folder)/'new.json'
            with self.assertRaises(ValueError):write_json_once(target,{'invalid':float('nan')})
            self.assertFalse(target.exists())

    def test_missing_prices_remain_in_full_denominator(self):
        cohort=[row('good'),row('failed'),dict(row('legacy'),priceBasis=None)]
        def quote(item,_):
            if item['id']=='failed':raise RuntimeError('missing')
            return {'price':110,'quoteAsOf':'2026-10-30'}
        results=settle_cohort(cohort,quote,'2026-10-30')
        self.assertEqual(len(results),3)
        self.assertEqual([s['returnRate'] for s in results],[10,None,None])
        report=build_report(results,'2026-10-30',NOW)
        self.assertEqual(report['performance']['US']['totalCount'],3)
        self.assertEqual(report['performance']['US']['observedCount'],1)
        self.assertEqual(report['performance']['US']['missingCount'],2)
        self.assertFalse(report['performance']['US']['complete'])

    def test_winners_are_separate_from_full_cohort(self):
        rows=[dict(row(f'W{i}'),returnRate=10) for i in range(10)]+[dict(row('loss'),returnRate=-90)]
        report=build_report(rows,'2026-10-30',NOW)
        self.assertEqual(len(report['stocks']),11)
        self.assertAlmostEqual(report['performance']['US']['averageObservedReturn'],10/11)
        self.assertEqual(len(report['winnerRankings']['US']),10)

    def test_split_future_and_stale_legacy_are_never_reported_as_measured_profit(self):
        cohort=[row()]
        split=settle_cohort(cohort,lambda *_:{'price':200,'quoteAsOf':'2026-10-30','hasSplit':True},'2026-10-30')[0]
        self.assertIsNone(split['returnRate']);self.assertEqual(split['returnStatus'],'corporate_action_requires_adjustment')
        future=settle_cohort(cohort,lambda *_:{'price':200,'quoteAsOf':'2026-11-01'},'2026-10-30')[0]
        self.assertIsNone(future['returnRate'])

    def test_existing_history_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            assert Path(folder).resolve().is_relative_to(ROOT.resolve())
            target=Path(folder)/'2026-10-30_weekly.json';write_json_once(target,{'original':True})
            with self.assertRaises(FileExistsError):write_json_once(target,{'original':False})
            self.assertEqual(json.loads(target.read_text()),{'original':True})

    def test_scanner_compiles_without_executing_network_or_main(self):
        ast.parse((ROOT/'daily_stock_scanner.py').read_text('utf-8'))
        workflow=(ROOT/'.github/workflows/daily_scan.yml').read_text('utf-8')
        self.assertIn('python scripts/resolve_scan_event.py',workflow)
        self.assertNotIn('date +%H',workflow)
        self.assertNotIn('git push', (ROOT/'daily_stock_scanner.py').read_text('utf-8'))


def isolated_scanner():
    # Imports only. All optional network packages are inert stubs; tests replace every I/O seam.
    dependencies={name:types.ModuleType(name) for name in ['yfinance','pandas','requests']}
    dependencies['firebase_admin']=None
    def blocked(*args,**kwargs):raise AssertionError('External call blocked by test')
    dependencies['requests'].get=blocked;dependencies['requests'].post=blocked
    dependencies['yfinance'].Ticker=blocked
    spec=importlib.util.spec_from_file_location('scanner_under_test',ROOT/'daily_stock_scanner.py')
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,dependencies):spec.loader.exec_module(module)
    # Pure contract tests use synthetic sessions; real calendars are exercised
    # by the separate offline producer/consumer integration test.
    module.is_market_session = lambda market, day: True
    return module


class ScannerIntegrationTests(unittest.TestCase):
    def test_daily_failure_keeps_last_good_other_market_and_writes_separate_archive(self):
        scanner=isolated_scanner()
        previous={'stocks':[row('old'),dict(row('K'),market='KR')],
                  'markets':{'US':{'generatedAt':'2026-10-29T23:15:00Z','quoteAsOf':'2026-10-29'},
                             'KR':{'generatedAt':'2026-10-29T09:30:00Z','quoteAsOf':'2026-10-29'}}}
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            assert Path(folder).resolve().is_relative_to(ROOT.resolve())
            root=Path(folder);daily=root/'daily';daily.mkdir()
            original=daily/'2026-10-29_daily.json';original.write_text('{"keep":true}')
            (root/'todays_recommendation.json').write_text(json.dumps(previous))
            scanner.DAILY_DATA_DIR=str(daily)
            saved_cwd=os.getcwd()
            try:
                os.chdir(root)
                with patch.object(scanner,'utc_stamp',return_value=NOW), \
                     patch.object(scanner,'market_session_date',return_value='2026-10-30'), \
                     patch.object(scanner,'us_universe',return_value=[]), \
                     patch.object(scanner,'analyze_market_condition',return_value={}), \
                     patch.object(scanner,'process_news_for_list') as news:
                    result=scanner.generate_daily('US')
                    news.assert_not_called()
                self.assertEqual(result['stocks'],previous['stocks'])
                self.assertEqual(result['markets']['KR'],previous['markets']['KR'])
                self.assertEqual(result['markets']['US']['status'],'error')
                self.assertIn('오류',result['notification']['body'])
                self.assertEqual(original.read_text(),'{"keep":true}')
                self.assertEqual(len(list(daily.glob('*_daily.json'))),2)
            finally:
                os.chdir(saved_cwd)

    def test_weekly_writes_new_top10_cohort_and_leaves_legacy_bytes_intact(self):
        scanner=isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            assert Path(folder).resolve().is_relative_to(ROOT.resolve())
            root=Path(folder);daily=root/'daily';weekly=root/'weekly';daily.mkdir();weekly.mkdir()
            legacy=weekly/'2026-10-30_weekly.json';legacy.write_text('{"legacy":true}')
            rows=[dict(row(f'A{i}'),quoteAsOf='2026-11-02') for i in range(11)]
            (daily/'2026-11-03_daily.json').write_text(json.dumps({'stocks':rows}))
            scanner.DAILY_DATA_DIR=str(daily);scanner.WEEKLY_REPORT_DIR=str(weekly)
            with patch.object(scanner,'evaluation_session',side_effect=lambda market,date:date), \
                 patch.object(scanner,'exit_quote',side_effect=lambda item,date:{'price':110 if item['id']!='A10' else 10,'quoteAsOf':date,'hasSplit':False}):
                output=scanner.generate_weekly_report('2026-11-21',now='2026-11-21T08:00:00Z')
                result=json.loads(output.read_text('utf-8'))
                self.assertEqual(len(result['stocks']),11)
                self.assertEqual(len(result['winnerRankings']['US']),10)
                self.assertNotIn('averageObservedReturn',result['performance']['US'])
                original=output.read_bytes()
                self.assertEqual(scanner.generate_weekly_report('2026-11-21',now='2026-11-28T08:00:00Z').read_bytes(),original)
            self.assertEqual(legacy.read_text(),'{"legacy":true}')

    def test_no_legacy_price_fetch_or_return_fabrication(self):
        scanner=isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            assert Path(folder).resolve().is_relative_to(ROOT.resolve())
            root=Path(folder);daily=root/'daily';daily.mkdir()
            (daily/'2026-11-03_daily.json').write_text(json.dumps({'stocks':[dict(row(),priceBasis=None,quoteAsOf='2026-11-02')]}))
            scanner.DAILY_DATA_DIR=str(daily);scanner.WEEKLY_REPORT_DIR=str(root/'weekly')
            with patch.object(scanner,'evaluation_session',side_effect=lambda market,date:date), \
                 patch.object(scanner,'exit_quote',side_effect=AssertionError('must not fetch')) as quote:
                result=json.loads(scanner.generate_weekly_report('2026-11-21',now='2026-11-21T08:00:00Z').read_text('utf-8'))
                quote.assert_not_called();self.assertIsNone(result['stocks'][0]['returnRate'])


class InterceptedWriter:
    def __init__(self, handle, before_write):
        self.handle, self.before_write = handle, before_write
    def __getattr__(self, name): return getattr(self.handle, name)
    def __enter__(self): return self
    def __exit__(self, *args): return self.handle.__exit__(*args)
    def write(self, value):
        self.before_write(self.handle, value)
        return self.handle.write(value)


class JSONPublicationTests(unittest.TestCase):
    def intercept(self, callback):
        real_open = io.open
        def open_file(file, mode='r', *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            return InterceptedWriter(handle, callback) if 'w' in mode or 'x' in mode else handle
        return patch('io.open', side_effect=open_file)

    def test_interrupted_historical_write_never_leaves_a_frozen_partial_file(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            target = Path(folder) / 'report.json'
            def fail(handle, value):
                handle.write(value[:1]); handle.flush()
                raise OSError('synthetic disk failure')
            with self.intercept(fail):
                with self.assertRaises(OSError): write_json_once(target, {'stocks': [1, 2, 3]})
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])
            write_json_once(target, {'complete': True})
            self.assertEqual(json.loads(target.read_text()), {'complete': True})

    def test_final_historical_name_is_invisible_until_all_json_is_written(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            target = Path(folder) / 'report.json'; writes = []
            def check(handle, value):
                writes.append(True); self.assertFalse(target.exists())
            with self.intercept(check): write_json_once(target, {'complete': True})
            self.assertTrue(writes); self.assertTrue(target.exists())

    def test_interleaved_latest_writers_use_distinct_staging_files(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            target = Path(folder) / 'latest.json'; entered = []
            def interleave(handle, value):
                if not entered:
                    entered.append(True); write_latest(target, {'owner': 'inner'})
            with self.intercept(interleave): write_latest(target, {'owner': 'outer'})
            self.assertEqual(json.loads(target.read_text()), {'owner': 'outer'})
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_failed_latest_write_preserves_previous_bytes_and_cleans_staging(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            target = Path(folder) / 'latest.json'; target.write_text('{"last_good": true}')
            before = target.read_bytes()
            def fail(handle, value):
                handle.write(value[:1]); handle.flush(); raise OSError('synthetic disk failure')
            with self.intercept(fail):
                with self.assertRaises(OSError): write_latest(target, {'new': True})
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(Path(folder).iterdir()), [target])


if __name__=='__main__':unittest.main()
