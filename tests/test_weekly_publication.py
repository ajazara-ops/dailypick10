"""Synthetic settlement-period concurrency and failure tests; no external I/O."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from fake_repository import Remote, Notifier, Crash
from scan_publication import DailyPublisher, STATE_PATH, LATEST_PATH, encoded
from test_scan_publication import NOW, facts, output
from weekly_contract import weekly_period, weekly_cohort, settle_weekly, build_weekly_report
from weekly_publication import WeeklyPublisher, weekly_key, INDEX_PATH

WEEK_NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
PERIOD = weekly_period('2026-11-21')
KEY = weekly_key(PERIOD)
REPORT_PATH = 'weekly_reports/2026-11-21_v3_weekly.json'


def sources():
    rows = [{'id': 'SYNTH-KR', 'market': 'KR', 'currentPrice': 100, 'priceBasis': 'unadjusted_close',
             'quoteAsOf': '2026-11-02', 'generatedAt': '2026-11-02T09:30:00+00:00'},
            {'id': 'SYNTH-US', 'market': 'US', 'currentPrice': 10, 'priceBasis': 'unadjusted_close',
             'quoteAsOf': '2026-11-06', 'generatedAt': '2026-11-06T23:15:00+00:00'}]
    # US Friday is archived on Saturday KST. The row session must win.
    later = {**rows[0], 'quoteAsOf': '2026-11-04', 'currentPrice': 110,
             'generatedAt': '2026-11-04T09:30:00+00:00'}
    return {'daily_data/2026-11-03_030000_daily.json': encoded({'stocks': [rows[0]]}),
            'daily_data/2026-11-04_KR_daily.json': encoded({'stocks': [later]}),
            'daily_data/2026-11-07_081500_daily.json': encoded({'stocks': [rows[1]]})}


def report(period=PERIOD, stamp=None, snapshots=None, quote=None):
    snapshots = snapshots if snapshots is not None else sources()
    cohort, diagnostics = weekly_cohort([(p.split('/')[-1], json.loads(b)) for p, b in snapshots.items()],
                                        period, lambda market, day: True)
    diagnostics['unreadableSnapshotCount'] = 0
    quote = quote or (lambda item, day: {'price': 120 if item['market'] == 'KR' else 11,
                                        'quoteAsOf': day, 'hasSplit': False})
    rows, sessions = settle_weekly(cohort, period, lambda market, day: day, quote)
    return build_weekly_report(rows, period, stamp or WEEK_NOW.isoformat(timespec='seconds'), sessions, diagnostics)


class WeeklyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote(sources());self.notifier = Notifier(self.remote)
        self.clock = WEEK_NOW;self.calls = []
        self.publisher = WeeklyPublisher(self.remote, self.notifier, clock=lambda: self.clock)

    def collect(self, period, snapshot, stamp):
        self.calls.append(period)
        return report(period, stamp)

    def run_week(self, run='1001', **kwargs):
        return self.publisher.run(run, self.collect, **kwargs)

    def test_sequential_duplicate_no_collection_history_or_notice(self):
        first = self.run_week();before = deepcopy(self.remote.files);commits = self.remote.commit_count
        self.assertEqual(first['state'], 'published');self.assertEqual(first['dataStatus'], 'fresh')
        self.assertEqual(self.run_week('1002')['state'], 'noop')
        self.assertEqual(self.remote.files, before);self.assertEqual(self.remote.commit_count, commits)
        self.assertEqual(len(self.calls), 1);self.assertEqual(len(self.notifier.calls), 1)
        self.assertNotIn(LATEST_PATH, self.remote.files)
        frozen = json.loads(self.remote.files[REPORT_PATH])
        self.assertEqual(frozen['stocks'][0]['buyPrice'], 100)
        self.assertEqual(frozen['stocks'][1]['recommendDate'], '2026-11-06')

    def test_concurrent_calls_have_one_publisher(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.run_week, map(str, range(1001, 1017))))
        self.assertEqual(sum(r['state']=='published' for r in results), 1)
        self.assertEqual(sum(r['state']=='noop' for r in results), 15)
        self.assertEqual(len(self.calls), 1);self.assertEqual(len(self.notifier.calls), 1)

    def test_unknown_and_crash_never_resend(self):
        for outcome in ('unknown', TimeoutError('synthetic'), Crash('synthetic')):
            with self.subTest(outcome=type(outcome).__name__):
                self.setUp();self.notifier.outcome = outcome
                if isinstance(outcome, Crash):
                    with self.assertRaises(Crash):self.run_week()
                else:self.run_week()
                files = deepcopy(self.remote.files)
                self.assertEqual(self.run_week('1002')['state'], 'noop')
                self.assertEqual(self.remote.files, files);self.assertEqual(len(self.notifier.calls), 1)
                self.assertEqual(len(self.calls), 1)

    def test_commit_rejection_keeps_index_and_report_unpublished(self):
        self.remote.files[INDEX_PATH] = encoded([{'date':'2026-11-14','file':'weekly_reports/2026-11-14_v3_weekly.json'}])
        old_index = self.remote.files[INDEX_PATH];self.remote.faults[2] = 'reject'
        self.assertEqual(self.run_week()['state'], 'unconfirmed')
        self.assertNotIn(REPORT_PATH, self.remote.files);self.assertEqual(self.remote.files[INDEX_PATH], old_index)
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.run_week('1002')['reason'], 'owner_active_or_unknown')

    def test_lost_commit_reply_is_confirmed_once_before_notice(self):
        self.remote.faults[2] = 'unknown_after'
        self.assertEqual(self.run_week()['state'], 'published')
        self.assertEqual(len(self.notifier.calls), 1);self.assertEqual(self.remote.commit_count, 4)

    def test_notification_claim_or_ack_failure_does_not_enable_retry(self):
        for step in (3, 4):
            with self.subTest(step=step):
                self.setUp();self.remote.faults[step] = 'reject';self.run_week()
                count = len(self.notifier.calls)
                self.assertEqual(count, 0 if step==3 else 1)
                self.assertEqual(self.run_week('1002')['state'], 'noop')
                self.assertEqual(len(self.notifier.calls), count)

    def test_broken_hash_or_index_blocks_without_repair_or_recollection(self):
        for path in (REPORT_PATH, INDEX_PATH):
            with self.subTest(path=path):
                self.setUp();self.run_week();self.remote.files[path] = encoded([] if path==INDEX_PATH else {})
                files = deepcopy(self.remote.files)
                self.assertEqual(self.run_week('1002')['state'], 'blocked')
                self.assertEqual(self.remote.files, files);self.assertEqual(len(self.calls), 1)

    def test_existing_legacy_report_is_not_overwritten_or_adopted(self):
        for suffix in ('_v3_weekly.json', '_v2_weekly.json', '_weekly.json'):
            with self.subTest(suffix=suffix):
                self.setUp();self.remote.files['weekly_reports/2026-11-21'+suffix] = encoded({'syntheticOnly':True})
                before = deepcopy(self.remote.files)
                self.assertEqual(self.run_week()['reason'], 'legacy_publication_requires_verified_adoption')
                self.assertEqual(self.remote.files, before);self.assertEqual(self.calls, [])

    def test_explicit_future_period_blocked_before_saturday_17_kst(self):
        self.clock = WEEK_NOW - timedelta(seconds=1)
        self.assertEqual(self.run_week(publication_date='2026-11-21')['state'], 'blocked')
        self.assertEqual(self.calls, []);self.assertEqual(self.remote.commit_count, 0)
        self.clock = WEEK_NOW
        self.assertEqual(self.run_week(publication_date='2026-11-21')['state'], 'published')

    def test_delayed_sunday_and_kst_midnight_are_same_period(self):
        self.run_week();self.clock = WEEK_NOW + timedelta(days=2)
        self.assertEqual(self.run_week('1002')['key'], KEY)
        self.assertEqual(len(self.calls), 1)

    def test_transient_failure_is_not_frozen_and_requires_explicit_retry(self):
        def failed(period, snapshot, stamp):
            return report(period, stamp, quote=lambda *_: {})
        result = self.publisher.run('1001', failed)
        self.assertEqual((result['state'], result['dataStatus']), ('failed', 'partial'))
        self.assertNotIn(REPORT_PATH, self.remote.files);self.assertNotIn(INDEX_PATH, self.remote.files)
        self.remote.run_states['1001'] = 'failure'
        self.assertEqual(self.run_week('1002')['reason'], 'explicit_retry_approval_required')
        self.assertEqual(self.calls, []);self.assertEqual(self.notifier.calls, [])

    def test_known_unavailable_rows_are_retained_as_partial_not_zero(self):
        def split(period, snapshot, stamp):
            return report(period, stamp, quote=lambda item, day: {'price':120, 'quoteAsOf':day, 'hasSplit':item['market']=='KR'})
        result = self.publisher.run('1001', split)
        self.assertEqual((result['state'], result['dataStatus']), ('published', 'partial'))
        frozen = json.loads(self.remote.files[REPORT_PATH])
        self.assertIsNone(frozen['stocks'][0]['returnRate']);self.assertEqual(frozen['performance']['KR']['missingCount'], 1)
        self.assertEqual(self.run_week('1002')['state'], 'noop')

    def test_empty_and_corrupt_source_are_not_success(self):
        for kind in ('empty', 'unreadable'):
            with self.subTest(kind=kind):
                self.setUp()
                def bad(period, snapshot, stamp):
                    out = report(period, stamp, snapshots={} if kind=='empty' else None)
                    out['sourceDiagnostics']['unreadableSnapshotCount'] = int(kind=='unreadable')
                    return out
                result = self.publisher.run('1001', bad)
                self.assertEqual(result['state'], 'failed');self.assertNotIn(REPORT_PATH, self.remote.files)
                self.assertEqual(self.notifier.calls, [])

    def test_changed_formula_is_rejected(self):
        def bad(period, snapshot, stamp):
            out = report(period, stamp);out['stocks'][0]['returnRate'] = 999
            return out
        self.assertEqual(self.publisher.run('1001', bad)['reason'], 'invalid_weekly_measurement')
        self.assertNotIn(REPORT_PATH, self.remote.files)

    def test_later_index_rows_preserved_and_old_week_stays_noop(self):
        self.run_week();rows = json.loads(self.remote.files[INDEX_PATH])
        rows.insert(0, {'date':'2026-11-28','file':'weekly_reports/2026-11-28_v3_weekly.json'})
        self.remote.files[INDEX_PATH] = encoded(rows)
        self.assertEqual(self.run_week('1002')['state'], 'noop')
        self.assertEqual(json.loads(self.remote.files[INDEX_PATH]), rows)

    def test_daily_kr_us_and_weekly_share_ledger_without_interference(self):
        daily = DailyPublisher(self.remote, self.notifier, clock=lambda: NOW, calendar=facts)
        for market, run in [('KR','2001'),('US','2002')]:
            self.assertEqual(daily.run(market, run, output)['state'], 'published')
        latest = self.remote.files[LATEST_PATH];archives = {p:b for p,b in self.remote.files.items() if p.startswith('daily_data/')}
        self.assertEqual(self.run_week()['state'], 'published')
        self.assertEqual(self.remote.files[LATEST_PATH], latest)
        self.assertEqual({p:b for p,b in self.remote.files.items() if p.startswith('daily_data/')}, archives)
        self.assertEqual(set(json.loads(self.remote.files[STATE_PATH])['entries']),
                         {'daily:KR:2026-11-02', 'daily:US:2026-11-02', KEY})
        for market in ('KR', 'US'):self.assertEqual(daily.run(market, '2003', output)['state'], 'noop')
        self.assertEqual(self.run_week('1002')['state'], 'noop');self.assertEqual(len(self.notifier.calls), 3)

    def test_stale_checkout_ignored_and_unknown_owner_not_stolen(self):
        self.run_week();self.remote.checkout_files = {}
        self.assertEqual(self.run_week('1002')['state'], 'noop')
        ledger = json.loads(self.remote.files[STATE_PATH]);ledger['entries'][KEY]['phase'] = 'collecting'
        self.remote.files[STATE_PATH] = encoded(ledger)
        self.clock += timedelta(days=100)
        self.assertEqual(self.run_week('1003', publication_date='2026-11-21')['reason'], 'owner_active_or_unknown')


if __name__ == '__main__':
    unittest.main()
