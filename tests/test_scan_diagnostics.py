"""Synthetic actual-contract/CLI tests; no live providers, keys or artifacts."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scan_contract import Observation, SourceDataError, scan_market
from scan_diagnostics import emit_scan_failure, failure_record
import test_guarded_daily as harness
from scan_publication import LATEST_PATH, encoded
from test_scan_publication import output

STAMP='2026-11-02T19:39:19+00:00'
CONTEXT=dict(market='KR',ticker='000001.KS',stage='stock_analysis',generated_at=STAMP,source_error_type=SourceDataError)


class ScanDiagnosticTests(unittest.TestCase):
    def record(self,error,**kwargs):
        return failure_record(error,**{**CONTEXT,**kwargs})

    def test_run_identity_and_retry_scope_are_explicit(self):
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'12345','GITHUB_RUN_ATTEMPT':'2'}):
            row=self.record(RuntimeError('untrusted body'))
        self.assertEqual((row['runId'],row['runAttempt'],row['ticker']),('12345','2','000001.KS'))
        self.assertEqual((row['retryCount'],row['retryScope'],row['downstreamRetryCount']),(0,'scan_market',None))
        self.assertEqual(row['attemptedAt'],STAMP)

    def test_nested_source_reason_is_preserved_without_exception_text(self):
        cause=SourceDataError('missing_quote_history')
        error=SourceDataError('stock_observation_failed');error.__cause__=cause
        row=self.record(error)
        self.assertEqual((row['errorCode'],row['stage'],row['exceptionType']),('missing_quote_history','quote_validation','SourceDataError'))

    def test_429_in_an_unknown_message_is_not_rate_limit_evidence(self):
        row=self.record(RuntimeError('429 rate limit PRIVATE_TOKEN=user@example.test'))
        self.assertEqual(row['category'],'unknown');self.assertIsNone(row['httpStatus'])
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(row));self.assertNotIn('user@example',json.dumps(row))

    def test_actual_http_status_distinguishes_limit_from_server_failure(self):
        for status,category in [(429,'rate_limit'),(503,'http')]:
            with self.subTest(status=status):
                error=HTTPError('https://secret.invalid/?token=PRIVATE_TOKEN',status,'PRIVATE_BODY',{'Authorization':'SECRET'},None)
                row=self.record(error)
                self.assertEqual((row['category'],row['httpStatus']),(category,status))
                self.assertNotIn('PRIVATE',json.dumps(row));self.assertNotIn('secret.invalid',json.dumps(row))

    def test_timeout_chain_reports_quote_stage_without_guessing_http_status(self):
        timeout=TimeoutError('PRIVATE_COOKIE')
        quote=SourceDataError('quote_fetch_failed');quote.__cause__=timeout
        outer=SourceDataError('stock_observation_failed');outer.__cause__=quote
        row=self.record(outer)
        self.assertEqual((row['category'],row['stage'],row['exceptionType']),('timeout','quote_fetch','TimeoutError'))
        self.assertIsNone(row['httpStatus'])

    def test_typed_provider_limit_does_not_invent_a_429_status(self):
        error_type=type('YFRateLimitError',(Exception,),{'__module__':'yfinance.exceptions'})
        row=self.record(error_type('not logged'))
        self.assertEqual(row['category'],'rate_limit');self.assertIsNone(row['httpStatus'])
        fake=type('YFRateLimitError',(Exception,),{'__module__':'untrusted_payload'})
        self.assertEqual(self.record(fake('429'))['category'],'unknown')

    def test_dynamic_exception_names_fields_messages_and_context_are_not_exported(self):
        custom=type('PRIVATE_TOKEN_Exception',(Exception,),{})
        error=custom({'password':'PRIVATE_TOKEN','email':'user@example.test'})
        error.__context__=TimeoutError('suppressed PRIVATE_TOKEN')
        error.__suppress_context__=True
        row=self.record(error);wire=json.dumps(row)
        self.assertEqual(row['exceptionType'],'OtherException');self.assertEqual(row['category'],'unknown')
        for secret in ['PRIVATE_TOKEN','user@example.test','password','__context__']:
            self.assertNotIn(secret,wire)

    def test_untrusted_context_fields_are_rejected_not_truncated_into_identifiers(self):
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'SECRET\n123','GITHUB_RUN_ATTEMPT':'token=1'}):
            row=self.record(ValueError('secret'),ticker='user@example.test',stage='cookie=SECRET',generated_at='SECRET')
        self.assertIsNone(row['ticker']);self.assertIsNone(row['runId']);self.assertIsNone(row['runAttempt'])
        self.assertIsNone(row['attemptedAt']);self.assertEqual(row['stage'],'unknown')
        self.assertNotIn('SECRET',json.dumps(row));self.assertLess(len(json.dumps(row)),1000)

    def test_cyclic_exception_causes_are_bounded(self):
        error=SourceDataError('stock_observation_failed');error.__cause__=error
        row=self.record(error)
        self.assertEqual(row['category'],'unknown');self.assertLess(len(json.dumps(row)),1000)

    def test_partial_scan_logs_one_failure_and_preserves_all_last_good_fields(self):
        tickers=[f'{i:06d}.KS' for i in range(1,101)]
        old={'stocks':[{'id':'999999.KS','market':'KR','currentPrice':200,'score':50}],
             'markets':{'KR':{'generatedAt':'2026-11-02T10:58:00+00:00','quoteAsOf':'2026-11-02'}}}
        untouched=deepcopy(old)
        def analyze(ticker,market):
            if ticker==tickers[-1]:raise SourceDataError('missing_quote_history')
            if ticker in tickers[:9]:return Observation(None,'2026-11-02')
            return Observation({'id':ticker,'market':market,'currentPrice':100,'score':0,'rsi':0},'2026-11-02')
        stream=io.StringIO()
        with redirect_stderr(stream):rows,meta=scan_market('KR',lambda:tickers,analyze,old,STAMP)
        records=[json.loads(s) for s in stream.getvalue().splitlines()]
        self.assertEqual(len(records),1);self.assertEqual(records[0]['ticker'],tickers[-1])
        self.assertEqual(rows,old['stocks']);self.assertEqual(old,untouched)
        self.assertEqual((meta['observedCount'],meta['errorCount'],meta['filteredCount']),(99,1,9))
        self.assertEqual((meta['status'],meta['stale'],meta['generatedAt']),('partial',True,old['markets']['KR']['generatedAt']))
        self.assertNotIn('errorCode',meta);self.assertNotIn('safeSummary',json.dumps(meta))

    def test_freshness_failure_keeps_existing_policy_and_identifies_stage(self):
        stream=io.StringIO()
        with redirect_stderr(stream):
            rows,meta=scan_market('KR',lambda:['000001.KS'],lambda *_:Observation(None,'2026-10-20'),{},STAMP)
        record=json.loads(stream.getvalue())
        self.assertEqual((meta['errorCount'],meta['status'],meta['freshnessLimitCalendarDays']),(1,'error',4))
        self.assertEqual((record['errorCode'],record['stage']),('stale_quote','quote_freshness'))

    def test_success_and_eligibility_filter_do_not_emit_failures(self):
        stream=io.StringIO()
        with redirect_stderr(stream):
            rows,meta=scan_market('US',lambda:['TEST'],lambda *_:Observation(None,'2026-11-02'),{},STAMP)
        self.assertEqual(stream.getvalue(),'');self.assertEqual((meta['status'],meta['filteredCount'],meta['errorCount']),('empty',1,0))

    def test_exact_session_failure_does_not_fall_back_to_calendar_day_allowance(self):
        stream=io.StringIO()
        with redirect_stderr(stream):
            rows,meta=scan_market('KR',lambda:['000001.KS'],lambda *_:Observation(None,'2026-10-30'),{},STAMP,
                                  expected_session='2026-11-02')
        row=json.loads(stream.getvalue())
        self.assertEqual((row['errorCode'],row['stage']),('wrong_quote_session','quote_date_validation'))
        self.assertEqual((meta['errorCount'],meta['observedCount'],meta['status']),(1,0,'error'))

    def test_logging_output_failure_cannot_hide_the_scan_failure(self):
        with patch('scan_diagnostics.print',side_effect=OSError('SECRET')):
            self.assertFalse(emit_scan_failure(RuntimeError('SECRET'),**CONTEXT))
            rows,meta=scan_market('KR',lambda:['000001.KS'],lambda *_:(_ for _ in ()).throw(ValueError('SECRET')),{},STAMP)
        self.assertEqual((meta['errorCount'],meta['status']),(1,'error'))

    def test_universe_failure_has_safe_stage_and_no_fake_ticker(self):
        stream=io.StringIO()
        with redirect_stderr(stream):
            rows,meta=scan_market('KR',lambda:[],lambda *_:self.fail('must not analyze'),{},STAMP)
        row=json.loads(stream.getvalue())
        self.assertEqual((row['stage'],row['ticker'],row['errorCode']),('universe',None,'universe_unavailable'))
        self.assertEqual((meta['attemptedCount'],meta['errorCount']),(0,1))

    def test_actual_scanner_publication_path_emits_log_but_no_diagnostic_json_or_notice(self):
        case=harness.GuardedScannerTests(methodName='runTest');case.setUp();self.addCleanup(case.doCleanups)
        old=output('KR','2026-10-30',{},'2026-10-30T10:00:00+00:00')
        case.remote.files[LATEST_PATH]=encoded(old);case.bad_ticker='000001.KS'
        stream=io.StringIO()
        with redirect_stderr(stream),patch.dict(os.environ,{'GITHUB_RUN_ID':'555','GITHUB_RUN_ATTEMPT':'1'}):
            result=case.run_actual()
        records=[json.loads(s) for s in stream.getvalue().splitlines() if s.startswith('{')]
        self.assertEqual(len(records),1);self.assertEqual(records[0]['runId'],'555')
        self.assertEqual((records[0]['ticker'],records[0]['stage']),('000001.KS','quote_fetch'))
        self.assertEqual(result['state'],'failed');self.assertEqual(case.notify.calls,[])
        view=json.loads(case.remote.files[LATEST_PATH])
        self.assertEqual(view['stocks'],old['stocks']);self.assertEqual(view['markets']['KR']['errorCount'],1)
        self.assertNotIn('stock_scan_failure',case.remote.files[LATEST_PATH].decode())
        self.assertFalse(any(p.startswith('daily_data/') for p in case.remote.files))


if __name__=='__main__':unittest.main()
