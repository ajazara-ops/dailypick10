"""Actual scanner CLI -> factory -> Git HTTP port -> durable claim -> Expo port.
All provider, HTTP and Firestore boundaries are synthetic; no credentials loaded.
"""
from copy import deepcopy
import json
import types
import unittest
from unittest.mock import patch

from publication_runtime import build_daily_publisher
from fake_github import GitServer, Response
import test_guarded_daily as guarded_fixtures
from test_scan_publication import NOW, facts


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.harness=guarded_fixtures.GuardedScannerTests('test_unconfigured_cli_boundary_has_zero_callbacks')
        self.harness.setUp();self.addCleanup(self.harness.doCleanups)
        self.scanner=self.harness.scanner;self.server=GitServer();self.calls=[];self.token_reads=[]
        self.original_tokens=self.scanner.get_all_user_tokens
        self.scanner.get_all_user_tokens=self.tokens
        self.env=self.server.environment()

    def tokens(self,strict=False):
        self.token_reads.append(strict)
        ledger=json.loads(self.server.files['scan_state.json'])
        self.assertEqual(ledger['entries']['daily:KR:2026-11-02']['notification']['state'],'claimed')
        return ['ExpoPushToken[synthetic-runtime]']

    def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        return Response(200,{'data':{'status':'ok','id':'synthetic-ticket'}})

    def factory(self,scanner):
        return build_daily_publisher(scanner,environment=self.env,request=self.server.request,
                                     post=self.post,clock=lambda:NOW,calendar=facts)

    def run_cli(self):
        return self.scanner.main(['--mode','daily','--target','KR','--date','2026-11-02'],runtime_factory=self.factory)

    def test_actual_cli_publishes_once_defers_all_notices_then_noop(self):
        result=self.run_cli();counts=deepcopy(self.harness.counts);files=deepcopy(self.server.files)
        self.assertEqual(result['state'],'published');self.assertEqual(result['notification'],'awaiting_pages')
        self.assertEqual(counts['price'],100);self.assertEqual(self.token_reads,[]);self.assertEqual(len(self.calls),0)
        self.env=self.server.environment('1002','schedule')
        self.assertEqual(self.run_cli()['state'],'noop')
        self.assertEqual(self.server.files,files);self.assertEqual(self.harness.counts,counts)
        self.assertEqual(self.token_reads,[]);self.assertEqual(len(self.calls),0)
        public_bytes=b'\n'.join(self.server.files.values())
        self.assertNotIn(b'ExpoPushToken',public_bytes);self.assertNotIn(b'synthetic-only-token',public_bytes)

    def test_lost_publication_response_confirmed_with_recipients_still_deferred(self):
        self.server.patch_faults[2]='unknown_after'
        self.assertEqual(self.run_cli()['state'],'published')
        self.assertEqual(self.token_reads,[]);self.assertEqual(self.server.patch_count,2)

    def test_us_actual_scanner_uses_its_own_session_and_preserves_kr(self):
        self.scanner.get_all_user_tokens=lambda strict=False:[]
        self.run_cli()
        kr=json.loads(self.server.files['todays_recommendation.json'])['stocks']
        self.scanner.us_universe=lambda:['SYNTHETIC-A','SYNTHETIC-B']
        self.env=self.server.environment('1002')
        result=self.scanner.main(['--mode','daily','--target','US','--date','2026-11-02'],runtime_factory=self.factory)
        self.assertEqual(result['state'],'published');self.assertEqual(result['notification'],'awaiting_pages')
        combined=json.loads(self.server.files['todays_recommendation.json'])
        self.assertEqual([row for row in combined['stocks'] if row['market']=='KR'],kr)
        self.assertEqual(combined['markets']['US']['observedCount'],2)
        self.assertEqual(self.harness.counts['price'],102)
        self.assertEqual(set(json.loads(self.server.files['scan_state.json'])['entries']),
                         {'daily:KR:2026-11-02','daily:US:2026-11-02'})
        self.env=self.server.environment('1003')
        self.assertEqual(self.scanner.main(['--mode','daily','--target','US','--date','2026-11-02'],runtime_factory=self.factory)['state'],'noop')
        self.assertEqual(self.harness.counts['price'],102)

    def test_unconfirmed_publication_returns_failure_and_never_reads_recipients(self):
        self.server.patch_faults[2]='unknown_before'
        with self.assertRaisesRegex(Exception,'daily_publication_not_complete'):self.run_cli()
        self.assertEqual(self.token_reads,[]);self.assertEqual(self.calls,[])

    def test_unavailable_expo_not_contacted_until_pages_and_duplicate_does_not_collect(self):
        def ambiguous(url,**kwargs):self.calls.append((url,kwargs));raise TimeoutError('synthetic lost response')
        self.post=ambiguous
        self.assertEqual(self.run_cli()['notification'],'awaiting_pages');counts=deepcopy(self.harness.counts)
        self.env=self.server.environment('1002')
        self.assertEqual(self.run_cli()['state'],'noop');self.assertEqual(len(self.calls),0)
        self.assertEqual(self.harness.counts,counts);self.assertEqual(self.token_reads,[])

    def test_token_lookup_is_not_attempted_in_collection_stage(self):
        def unavailable(strict=False):raise ValueError('synthetic database unavailable')
        self.scanner.get_all_user_tokens=unavailable
        self.assertEqual(self.run_cli()['notification'],'awaiting_pages');self.assertEqual(self.calls,[])
        self.env=self.server.environment('1002')
        self.assertEqual(self.run_cli()['state'],'noop')

    def test_disabled_factory_does_not_read_credentials_or_construct_http(self):
        class NoSecrets(dict):
            def get(_,key,default=None):
                if key in {'GITHUB_TOKEN','FIREBASE_CREDENTIALS'}:raise AssertionError('credential read while disabled')
                return super().get(key,default)
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            build_daily_publisher(self.scanner,environment=NoSecrets(),request=self.server.request,post=self.post)
        self.assertEqual(self.server.calls,[]);self.assertEqual(sum(self.harness.counts.values()),0)

    def test_wrong_workflow_context_never_uses_live_transport(self):
        for field,value in [('GITHUB_REF','refs/heads/unapproved'),('GITHUB_EVENT_NAME','push'),('GITHUB_JOB','other')]:
            with self.subTest(field=field),self.assertRaises(RuntimeError):
                build_daily_publisher(self.scanner,environment={**self.env,field:value},request=self.server.request,post=self.post)
        self.assertEqual(self.server.calls,[])

    def test_missing_runtime_token_stops_before_any_http_or_provider(self):
        self.env.pop('GITHUB_TOKEN')
        with self.assertRaisesRegex(Exception,'existing_workflow_token_required'):self.run_cli()
        self.assertEqual(self.server.calls,[]);self.assertEqual(sum(self.harness.counts.values()),0)

    def test_existing_recipient_filter_strict_lookup_and_legacy_default_are_preserved(self):
        token='ExpoPushToken[synthetic-eligible]'
        docs=[types.SimpleNamespace(to_dict=lambda:{'pushToken':token}),
              types.SimpleNamespace(to_dict=lambda:{'pushToken':'ExpoPushToken[synthetic-excluded]','deletionRequest':{}})]
        collection=types.SimpleNamespace(stream=lambda:iter(docs))
        def select(name):self.assertEqual(name,'users');return collection
        self.scanner.FIREBASE_AVAILABLE=True
        self.scanner.firebase_admin=types.SimpleNamespace(_apps={'synthetic':object()})
        self.scanner.firestore=types.SimpleNamespace(client=lambda:types.SimpleNamespace(collection=select))
        self.assertEqual(self.original_tokens(strict=True),[token])
        def failure():raise ValueError('synthetic Firestore failure')
        collection.stream=failure
        with self.assertRaisesRegex(Exception,'push_recipient_lookup_failed'):self.original_tokens(strict=True)
        self.assertEqual(self.original_tokens(),[])

    def test_weekly_cli_does_not_construct_daily_runtime(self):
        calls=[]
        def run(run_id, collect, *, publication_date):
            calls.append((run_id, collect, publication_date));return {'state':'noop'}
        def weekly_factory(_):return types.SimpleNamespace(run=run),'1001'
        self.scanner.generate_weekly_report=lambda *_:self.fail('legacy writer called')
        self.scanner.update_history_index=lambda:self.fail('legacy index called')
        self.scanner.write_latest=lambda *_:self.fail('legacy pending notice called')
        def forbidden(_):self.fail('weekly must not use daily runtime')
        self.scanner.main(['--mode','weekly_summary','--target','ALL','--date','2026-11-21'],runtime_factory=forbidden,
                          weekly_runtime_factory=weekly_factory)
        self.assertEqual(calls,[('1001',self.scanner.collect_weekly_period,'2026-11-21')])
