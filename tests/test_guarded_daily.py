"""Exercise the real scanner entry point, scoring and writer boundary with synthetic feeds."""
import ast
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import pandas as pd

from scan_publication import DailyPublisher, LATEST_PATH, encoded
from fake_repository import Remote, Notifier
from test_scan_publication import facts, NOW, output
from weekly_contract import weekly_cohort, weekly_period

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests/fixtures'


class GuardedScannerTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.counts=dict(price=0,financial=0,naver=0,universe=0,news=0,index=0)
        self.quote_date='2026-11-02';self.bad_ticker=None;self.universe_empty=False
        owner=self
        class Ticker:
            def __init__(self,ticker):self.ticker=ticker
            def history(self,**kwargs):
                owner.counts['price']+=1
                if self.ticker==owner.bad_ticker:raise ValueError('synthetic missing quote')
                dates=pd.bdate_range(end=owner.quote_date,periods=130)
                return pd.DataFrame({'Close':[100+i%2 for i in range(130)],'High':[103]*130,
                                     'Low':[98]*130,'Volume':[1000]*130},index=dates)
            @property
            def info(self):
                owner.counts['financial']+=1
                return {'operatingMargins':.2,'forwardPE':8,'priceToBook':.7,'sector':'SYNTHETIC','shortName':'합성 테스트'}
        yf=types.ModuleType('yfinance');yf.Ticker=Ticker
        requests=types.ModuleType('requests')
        def blocked(*args,**kwargs):raise AssertionError('External HTTP forbidden')
        requests.get=requests.post=blocked
        self.stack.enter_context(patch.dict(sys.modules,{'yfinance':yf,'requests':requests,'firebase_admin':None}))
        self.scanner=self.load(ROOT/'daily_stock_scanner.py','guarded_actual_scanner')
        self.remote=Remote();self.notify=Notifier(self.remote)
        self.publisher=DailyPublisher(self.remote,self.notify,clock=lambda:NOW,calendar=facts)
        temp=ROOT/'.publication-test-temp';temp.mkdir(exist_ok=True)
        self.directory=self.stack.enter_context(tempfile.TemporaryDirectory(dir=temp))
        self.assertTrue(Path(self.directory).resolve().is_relative_to(temp.resolve()))
        old=Path.cwd();os.chdir(self.directory);self.addCleanup(os.chdir,old)

    def load(self,path,name):
        spec=importlib.util.spec_from_file_location(name,path)
        scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)
        tickers=[f'{i:06d}.KS' for i in range(1,51)]+[f'{i:06d}.KQ' for i in range(51,101)]
        def universe():
            self.counts['universe']+=1;return [] if self.universe_empty else tickers
        def financial(_):
            self.counts['naver']+=1;return {'op_margin':.2,'per':8,'pbr':.7}
        def news(rows):self.counts['news']+=1
        def indexes():
            self.counts['index']+=1;return {'KR':{'status':'synthetic'},'US':{'status':'synthetic'},'VIX':{'status':'synthetic'}}
        scanner.get_korea_tickers=universe;scanner.get_kr_fundamental=financial
        scanner.process_news_for_list=news;scanner.analyze_market_condition=indexes
        scanner.send_push_notification=lambda *_:self.fail('legacy push path called')
        scanner.market_session_date=lambda _:'2026-11-02'
        return scanner

    def run_actual(self,run='1001'):
        return self.scanner.generate_daily('KR',publisher=self.publisher,run_id=run)

    def test_real_entrypoint_success_then_duplicate_has_no_provider_news_index_or_write(self):
        first=self.run_actual();counts=deepcopy(self.counts);files=deepcopy(self.remote.files)
        second=self.run_actual('1002')
        self.assertEqual(first['state'],'published');self.assertEqual(second['state'],'noop')
        self.assertEqual(self.counts,counts);self.assertEqual(self.remote.files,files)
        self.assertEqual(counts['price'],100);self.assertEqual(counts['universe'],1)
        self.assertEqual(len(self.notify.calls),1)
        out=json.loads(self.remote.files[LATEST_PATH]);meta=out['markets']['KR']
        self.assertEqual((meta['status'],meta['observedCount'],meta['errorCount']),('fresh',100,0))
        self.assertEqual(len(out['stocks']),10);self.assertEqual(meta['quoteAsOf'],'2026-11-02')
        self.assertFalse(Path('.pending_notification.json').exists());self.assertFalse(Path('daily_data').exists())

    def test_baseline_control_reproduces_two_histories_and_repeated_collection(self):
        # The old public function is an explicit fixture, not a sibling checkout.
        fixture_path=FIXTURES/'legacy_daily.py'
        fixture_source=fixture_path.read_text('utf-8')
        function=next(n for n in ast.parse(fixture_source).body if isinstance(n,ast.FunctionDef))
        contract=json.loads((FIXTURES/'selection_contract.json').read_text('utf-8'))
        self.assertEqual(hashlib.sha256(ast.get_source_segment(fixture_source,function).encode()).hexdigest(),
                         contract['legacyDailyFunctionSha256'])
        baseline=self.load(fixture_path,'baseline_actual_scanner')
        for name,value in vars(self.scanner).items():
            if name!='generate_daily' and not name.startswith('__'):
                setattr(baseline,name,value)
        stamps=iter([NOW.isoformat(),(NOW+timedelta(seconds=1)).isoformat()]);baseline.utc_stamp=lambda:next(stamps)
        baseline.generate_daily('KR');baseline.generate_daily('KR')
        self.assertEqual(self.counts['price'],200);self.assertEqual(self.counts['universe'],2)
        self.assertEqual(len(list(Path('daily_data').glob('*_daily.json'))),2)
        self.assertTrue(Path('.pending_notification.json').exists())

    def test_unconfigured_cli_boundary_has_zero_callbacks(self):
        with self.assertRaisesRegex(RuntimeError,'live execution is disabled'):
            self.scanner.generate_daily('KR')
        self.assertEqual(sum(self.counts.values()),0)

    def test_existing_all_target_uses_separate_market_guards(self):
        calls=[]
        class Publisher:
            def run(self, market, run_id, collect, **kwargs):
                if market not in {'US','KR'}:
                    raise ValueError('explicit market required')
                calls.append((market,run_id,collect,kwargs))
                return {'state':'noop','key':f'daily:{market}:2026-11-02'}
        result=self.scanner.generate_daily('ALL',publisher=Publisher(),run_id='1003')
        self.assertEqual([call[0] for call in calls],['US','KR'])
        self.assertTrue(all(call[1]=='1003' and call[2] is self.scanner.collect_daily_session for call in calls))
        self.assertEqual(set(result['results']),{'US','KR'})
        self.assertEqual(sum(self.counts.values()),0)

    def test_real_missing_quote_is_partial_and_preserves_last_good_without_history(self):
        old=output('KR','2026-10-30',{},'2026-10-30T10:00:00+00:00')
        self.remote.files[LATEST_PATH]=encoded(old);self.bad_ticker='000001.KS'
        result=self.run_actual();view=json.loads(self.remote.files[LATEST_PATH])
        self.assertEqual(result['state'],'failed');self.assertEqual(result['dataStatus'],'partial')
        self.assertEqual(view['stocks'],old['stocks']);self.assertEqual(view['markets']['KR']['observedCount'],99)
        self.assertEqual(view['markets']['KR']['errorCount'],1);self.assertTrue(view['markets']['KR']['stale'])
        self.assertFalse(any(p.startswith('daily_data/') for p in self.remote.files));self.assertEqual(self.notify.calls,[])

    def test_real_universe_zero_is_error_not_success_or_empty(self):
        self.universe_empty=True
        result=self.run_actual()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['dataStatus'],'error')
        self.assertEqual(self.counts['price'],0);self.assertEqual(self.notify.calls,[])

    def test_strict_session_rejects_older_bars_inside_previous_four_day_allowance(self):
        self.quote_date='2026-10-30'
        result=self.run_actual();view=json.loads(self.remote.files[LATEST_PATH])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(view['markets']['KR']['observedCount'],0)
        self.assertEqual(view['markets']['KR']['errorCount'],100)
        self.assertEqual(self.notify.calls,[])

    def test_new_stable_history_is_consumed_by_existing_weekly_contract(self):
        self.run_actual();name='daily_data/2026-11-02_KR_daily.json'
        out=json.loads(self.remote.files[name])
        cohort,diagnostics=weekly_cohort([(Path(name).name,out)],weekly_period('2026-11-21'),lambda m,d:True)
        self.assertEqual(len(cohort),10)
        self.assertTrue(all(item['recommendDate']=='2026-11-02' for item in cohort))

    def test_selection_weekly_calendar_schedule_and_concurrency_are_preserved(self):
        contract=json.loads((FIXTURES/'selection_contract.json').read_text('utf-8'))
        source=(ROOT/'daily_stock_scanner.py').read_text('utf-8');after=ast.parse(source)
        names=set(contract['functionSourceSha256'])
        current={n.name:hashlib.sha256(ast.get_source_segment(source,n).encode()).hexdigest()
                 for n in after.body if isinstance(n,ast.FunctionDef) and n.name in names}
        self.assertEqual(current,contract['functionSourceSha256'])
        new=(ROOT/'.github/workflows/daily_scan.yml').read_text('utf-8')
        self.assertEqual(contract['cron'],re.findall(r"cron: (.+)",new))
        self.assertIn('group: stock-data-publisher\n  cancel-in-progress: false',new)
        self.assertEqual(new.count("if: steps.intent.outputs.mode != 'daily'"),2)


if __name__=='__main__':unittest.main()
