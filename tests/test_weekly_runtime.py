"""Actual weekly CLI and collector through synthetic GitHub/Expo transports."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest

from fake_github import GitServer, Response
from github_publication import GitHubHTTP, GitHubRepository
from publication_runtime import build_weekly_publisher
from scan_publication import CommitRejected, encoded
import test_guarded_daily as guarded_fixtures
from test_weekly_publication import WEEK_NOW, PERIOD, KEY, REPORT_PATH, sources, report
from weekly_publication import INDEX_PATH


class WeeklyRuntimeTests(unittest.TestCase):
    def setUp(self):
        # Do not subclass/import a TestCase under its real name into discovery.
        self.harness = guarded_fixtures.GuardedScannerTests('test_unconfigured_cli_boundary_has_zero_callbacks')
        self.harness.setUp();self.addCleanup(self.harness.doCleanups)
        self.scanner = self.harness.scanner
        self.server = GitServer({**sources(), 'unrelated.txt':b'preserve'})
        self.env = self.server.environment();self.now = WEEK_NOW
        self.quote_calls = [];self.posts = [];self.token_reads = 0
        self.scanner.is_market_session = lambda market, day: True
        self.scanner.evaluation_session = lambda market, day: day
        self.scanner.exit_quote = self.quote
        self.scanner.get_all_user_tokens = self.tokens

    def quote(self, item, day):
        self.quote_calls.append((item['market'], item['id'], day))
        return {'price':120 if item['market']=='KR' else 11, 'quoteAsOf':day, 'hasSplit':False}

    def tokens(self, strict=False):
        self.assertTrue(strict);self.token_reads += 1
        entry = json.loads(self.server.files['scan_state.json'])['entries'][KEY]
        self.assertEqual(entry['notification']['state'], 'claimed')
        self.assertIn(REPORT_PATH, self.server.files);self.assertIn(INDEX_PATH, self.server.files)
        return ['ExpoPushToken[synthetic-weekly]']

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response(200, {'data':{'status':'ok','id':'synthetic-ticket'}})

    def factory(self, scanner):
        return build_weekly_publisher(scanner, environment=self.env, request=self.server.request,
                                      post=self.post, clock=lambda:self.now)

    def cli(self, mode='weekly', extra=None):
        return self.scanner.main(['--mode',mode,'--target','ALL']+(extra or []), weekly_runtime_factory=self.factory)

    def test_actual_cli_reads_legacy_archives_and_alias_is_complete_noop(self):
        result = self.cli();self.assertEqual(result['state'],'published')
        self.assertEqual(result['notification'],'accepted');self.assertEqual(len(self.quote_calls),2)
        self.assertEqual(json.loads(self.server.files[REPORT_PATH]), report())
        before = deepcopy(self.server.files);patches = self.server.patch_count
        self.env = self.server.environment('1002','schedule')
        self.assertEqual(self.cli('weekly_summary')['state'],'noop')
        self.assertEqual(self.server.files,before);self.assertEqual(self.server.patch_count,patches)
        self.assertEqual(self.token_reads,1);self.assertEqual(len(self.posts),1);self.assertEqual(len(self.quote_calls),2)
        self.assertNotIn(b'ExpoPushToken',b'\n'.join(self.server.files.values()))
        self.assertFalse(Path('.pending_notification.json').exists())
        self.assertFalse(Path('weekly_reports').exists())
        self.assertEqual(sum(self.harness.counts.values()),0)
        self.assertTrue(self.posts[0][1]['json']['data']['notificationKey'].startswith('weekly-'))

    def test_new_collector_matches_legacy_frozen_report_and_index(self):
        for path, body in sources().items():
            dest = Path(path);dest.parent.mkdir(exist_ok=True);dest.write_bytes(body)
        old = self.scanner.generate_weekly_report('2026-11-21',now=WEEK_NOW.isoformat(timespec='seconds'))
        expected = json.loads(Path(old).read_text('utf-8'))
        self.scanner.update_history_index(now=WEEK_NOW.isoformat(timespec='seconds'))
        expected_index = json.loads(Path(INDEX_PATH).read_text('utf-8'))
        self.cli()
        self.assertEqual(json.loads(self.server.files[REPORT_PATH]),expected)
        self.assertEqual(json.loads(self.server.files[INDEX_PATH]),expected_index)

    def test_missing_exact_friday_quote_leaves_old_results_and_no_notice(self):
        self.server.advance({INDEX_PATH:encoded([{'date':'2026-11-14','file':'weekly_reports/2026-11-14_v3_weekly.json'}])})
        before = self.server.files[INDEX_PATH]
        self.scanner.exit_quote = lambda *_: {'price':100,'quoteAsOf':'2026-11-19','hasSplit':False}
        with self.assertRaisesRegex(Exception,'weekly_publication_not_complete'):self.cli()
        self.assertNotIn(REPORT_PATH,self.server.files);self.assertEqual(self.server.files[INDEX_PATH],before)
        self.assertEqual(self.token_reads,0);self.assertEqual(self.posts,[])

    def test_market_holiday_uses_previous_session_without_current_prices(self):
        self.scanner.evaluation_session = lambda market, day: '2026-11-19' if market=='KR' else day
        self.cli();data = json.loads(self.server.files[REPORT_PATH])
        self.assertEqual(data['stocks'][0]['exitQuoteAsOf'],'2026-11-19')
        self.assertEqual(data['stocks'][1]['exitQuoteAsOf'],'2026-11-20')
        self.assertEqual(self.quote_calls,[('KR','SYNTH-KR','2026-11-19'),('US','SYNTH-US','2026-11-20')])

    def test_ambiguous_main_update_is_confirmed_without_second_patch_or_notice(self):
        self.server.patch_faults[2] = 'unknown_after'
        self.assertEqual(self.cli()['state'],'published')
        self.assertEqual(self.server.patch_count,4);self.assertEqual(len(self.posts),1)

    def test_concurrent_main_update_is_preserved_and_no_notice(self):
        def request(method,url,**kwargs):
            if method=='PATCH' and self.server.patch_count==1:
                self.server.before_patch = lambda:self.server.advance({'unrelated.txt':b'concurrent-user-change'})
            return self.server.request(method,url,**kwargs)
        def factory(scanner):
            return build_weekly_publisher(scanner,environment=self.env,request=request,post=self.post,clock=lambda:self.now)
        self.factory = factory
        with self.assertRaisesRegex(Exception,'weekly_publication_not_complete'):self.cli()
        self.assertEqual(self.server.files['unrelated.txt'],b'concurrent-user-change')
        self.assertNotIn(REPORT_PATH,self.server.files);self.assertEqual(self.posts,[])

    def test_unknown_expo_reply_is_not_resent_or_repriced(self):
        def timeout(url,**kwargs):self.posts.append((url,kwargs));raise TimeoutError('synthetic')
        self.post = timeout
        self.assertEqual(self.cli()['notification'],'unknown')
        self.env = self.server.environment('1002')
        self.assertEqual(self.cli('weekly_summary')['state'],'noop')
        self.assertEqual(len(self.posts),1);self.assertEqual(len(self.quote_calls),2)

    def test_disabled_weekly_factory_reads_no_secret_or_provider(self):
        class NoSecrets(dict):
            def get(self,key,default=None):
                if key in {'GITHUB_TOKEN','FIREBASE_CREDENTIALS'}:raise AssertionError('secret read')
                return super().get(key,default)
        self.env = NoSecrets()
        with self.assertRaisesRegex(RuntimeError,'disabled'):self.cli()
        self.assertEqual(self.server.calls,[]);self.assertEqual(self.quote_calls,[]);self.assertEqual(self.posts,[])

    def test_corrupt_archive_never_freezes_a_partial_source_set(self):
        self.server.advance({'daily_data/2026-11-05_170000_daily.json':b'not-json'})
        with self.assertRaisesRegex(Exception,'weekly_publication_not_complete'):self.cli()
        self.assertNotIn(REPORT_PATH,self.server.files);self.assertEqual(self.posts,[])

    def test_legacy_daily_read_only_and_weekly_immutable_at_git_port(self):
        repo = GitHubRepository(GitHubHTTP('synthetic-only-token',self.server.request))
        snapshot = repo.refresh_main()
        old = next(p for p in sources() if '_030000_' in p)
        self.assertEqual(snapshot.files[old],sources()[old])
        with self.assertRaises(CommitRejected):repo.commit(snapshot.head,{old:encoded({'stocks':[]})})
        self.cli();snapshot = repo.refresh_main()
        with self.assertRaises(CommitRejected):repo.commit(snapshot.head,{REPORT_PATH:encoded({'changed':True})})
        with self.assertRaises(CommitRejected):repo.commit(snapshot.head,{INDEX_PATH:encoded({'not':'list'})})
        with self.assertRaises(CommitRejected):repo.commit(snapshot.head,{'unrelated.txt':encoded({})})


