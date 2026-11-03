from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scan_publication import DailyPublisher, STATE_PATH, LATEST_PATH, encoded, session_key
from fake_repository import Remote, Notifier, Crash

NOW = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)


def facts(market, now, requested=None):
    day = '2026-11-02'
    return dict(session=requested or day, latestCompleted=day, localDate=day,
                localIsSession=True, completed=True)


def output(market, session, previous, stamp, *, empty=False):
    out = deepcopy(previous)
    out.setdefault('markets', {})
    rows = [] if empty else [dict(id=f'SYNTH-{market}', market=market, rank=1,
                                  currentPrice=100, score=20, rsi=45, quoteAsOf=session)]
    out['stocks'] = [s for s in previous.get('stocks', []) if s['market'] != market] + rows
    out['markets'][market] = dict(status='empty' if empty else 'fresh', stale=False,
        attemptedCount=100, observedCount=100, filteredCount=100 if empty else 99,
        errorCount=0, universeError=None, quoteAsOf=session, generatedAt=stamp)
    out.update(schemaVersion=2, generatedAt=stamp, notification={'title':'SYNTHETIC ONLY', 'body':'Not a real recommendation'})
    return out


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote()
        self.notify = Notifier(self.remote)
        self.clock = lambda: NOW
        self.publisher = DailyPublisher(self.remote, self.notify, clock=lambda:self.clock(), calendar=facts)
        self.collections = []

    def collect(self, *args):
        self.collections.append(args[:2])
        return output(*args)

    def run_once(self, run='1001', market='KR', collect=None, **kwargs):
        return self.publisher.run(market, run, collect or self.collect, **kwargs)

    def entry(self, market='KR'):
        return json.loads(self.remote.files[STATE_PATH])['entries'][f'daily:{market}:2026-11-02']

    def test_serial_duplicate_has_zero_new_collection_history_commit_and_send(self):
        first = self.run_once()
        files, commits, events = deepcopy(self.remote.files), self.remote.commit_count, len(self.remote.events)
        second = self.run_once('1002')
        self.assertEqual(first['state'], 'published'); self.assertEqual(second['state'], 'noop')
        self.assertEqual(len(self.collections), 1); self.assertEqual(len(self.notify.calls), 1)
        self.assertEqual(self.remote.files, files); self.assertEqual(self.remote.commit_count, commits)
        self.assertEqual([e[0] for e in self.remote.events[events:]], ['lock', 'refresh_main', 'unlock'])
        self.assertEqual([p for p in files if p.startswith('daily_data/')], ['daily_data/2026-11-02_KR_daily.json'])

    def test_two_concurrent_workers_one_collection_and_notification(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set(); self.assertTrue(release.wait(5)); return self.collect(*args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.run_once, '1001', 'KR', slow)
            self.assertTrue(entered.wait(5))
            b=pool.submit(self.run_once, '1002')
            release.set()
            self.assertEqual({a.result()['state'], b.result()['state']}, {'published', 'noop'})
        self.assertEqual(len(self.collections),1);self.assertEqual(len(self.notify.calls),1)

    def test_stale_checkout_is_ignored_after_lock(self):
        self.run_once(); self.remote.checkout_files = {}
        self.assertEqual(self.run_once('1002')['state'],'noop')
        self.assertEqual(len(self.collections),1)
        for i,e in enumerate(self.remote.events):
            if e[0]=='lock': self.assertEqual(self.remote.events[i+1][0],'refresh_main')

    def test_published_data_survives_other_market_latest_update(self):
        kr=self.run_once();us=self.run_once('1002','US')
        self.assertEqual(kr['state'],'published');self.assertEqual(us['state'],'published')
        self.assertEqual(self.run_once('1003')['state'],'noop')
        self.assertNotEqual(self.entry('KR')['notification']['key'],self.entry('US')['notification']['key'])
        self.assertEqual({s['market'] for s in json.loads(self.remote.files[LATEST_PATH])['stocks']},{'KR','US'})

    def test_corrupt_frozen_output_blocks_without_rescan(self):
        self.run_once(); self.remote.files[self.entry()['outputPath']]=b'{}'
        before=self.remote.commit_count
        result=self.run_once('1002')
        self.assertEqual(result['reason'],'published_hash_unverified')
        self.assertEqual(len(self.collections),1);self.assertEqual(self.remote.commit_count,before)

    def test_empty_is_success_only_after_full_universe_observation(self):
        result=self.run_once(collect=lambda *a:output(*a,empty=True))
        self.assertEqual(result['dataStatus'],'empty');self.assertEqual(self.run_once('1002')['state'],'noop')

    def test_missing_universe_not_empty_success(self):
        def broken(*a):
            o=output(*a,empty=True);o['markets']['KR'].update(attemptedCount=0,observedCount=0,filteredCount=0)
            return o
        self.assertEqual(self.run_once(collect=broken)['state'],'failed')
        self.assertEqual(self.notify.calls,[])
        self.assertFalse(any(p.startswith('daily_data/') for p in self.remote.files))

    def test_partial_preserves_last_good_rows_and_dates_and_marks_failure(self):
        old=output('KR','2026-10-30',{},'2026-10-30T10:00:00+00:00')
        self.remote.files[LATEST_PATH]=encoded(old)
        def partial(*a):
            o=output(*a);o['markets']['KR'].update(status='partial',stale=True,errorCount=1,observedCount=99)
            return o
        result=self.run_once(collect=partial);view=json.loads(self.remote.files[LATEST_PATH])
        self.assertEqual(result['dataStatus'],'partial');self.assertEqual(self.entry()['phase'],'failed')
        self.assertEqual(view['stocks'],old['stocks']);self.assertEqual(view['markets']['KR']['quoteAsOf'],'2026-10-30')
        self.assertTrue(view['markets']['KR']['stale']);self.assertEqual(self.notify.calls,[])

    def test_stale_quote_never_published_as_fresh(self):
        def stale(*a):
            o=output(*a);o['markets']['KR']['quoteAsOf']='2026-10-30';return o
        self.assertEqual(self.run_once(collect=stale)['dataStatus'],'stale')
        self.assertEqual(self.notify.calls,[])

    def test_failed_collection_requires_explicit_retry_and_known_finished_run(self):
        self.run_once(collect=lambda *a: (_ for _ in ()).throw(ValueError('synthetic')))
        key='daily:KR:2026-11-02'
        self.remote.retry_grants.add((key,'1002'))
        self.assertEqual(self.run_once('1002')['reason'],'owner_active_or_unknown')
        self.remote.run_states['1001']='in_progress'
        self.assertEqual(self.run_once('1002')['reason'],'owner_active_or_unknown')
        self.remote.run_states['1001']='failure'
        self.assertEqual(self.run_once('1003')['reason'],'explicit_retry_approval_required')
        self.assertEqual(self.run_once('1002')['state'],'published')

    def test_crash_during_collection_does_not_allow_timeout_steal(self):
        with self.assertRaises(Crash): self.run_once(collect=lambda *a:(_ for _ in ()).throw(Crash()))
        self.remote.retry_grants.add(('daily:KR:2026-11-02','1002'))
        self.clock=lambda:datetime(2026,11,20,10,tzinfo=timezone.utc)
        self.assertEqual(self.run_once('1002')['reason'],'owner_active_or_unknown')
        self.assertEqual(self.notify.calls,[])

    def test_claim_commit_failure_prevents_all_collection(self):
        self.remote.faults[1]='reject'
        self.assertEqual(self.run_once()['reason'],'collection_claim_unconfirmed')
        self.assertEqual(self.collections,[]);self.assertEqual(self.notify.calls,[])

    def test_publication_commit_failure_keeps_claim_and_does_not_notify(self):
        self.remote.faults[2]='reject'
        self.assertEqual(self.run_once()['reason'],'publication_unconfirmed')
        self.assertEqual(self.entry()['phase'],'collecting');self.assertEqual(self.notify.calls,[])
        self.assertFalse(any(p.startswith('daily_data/') for p in self.remote.files))

    def test_commit_ack_lost_is_read_back_without_second_publish(self):
        self.remote.faults[2]='unknown_after'
        self.assertEqual(self.run_once()['state'],'published')
        publication_commits=[e for e in self.remote.events if e[0]=='commit' and any(p.startswith('daily_data/') for p in e[2])]
        self.assertEqual(len(publication_commits),1);self.assertEqual(len(self.notify.calls),1)

    def test_unknown_publication_not_visible_never_sends(self):
        self.remote.faults[2]='unknown_before'
        self.assertEqual(self.run_once()['state'],'unconfirmed');self.assertEqual(self.notify.calls,[])

    def test_readback_failure_after_published_does_not_recollect_later(self):
        self.remote.fail_reads={3}
        self.assertEqual(self.run_once()['state'],'unconfirmed')
        self.assertEqual(self.run_once('1002')['state'],'noop')
        self.assertEqual(len(self.collections),1);self.assertEqual(self.notify.calls,[])

    def test_notification_claim_failure_does_not_send_or_recollect(self):
        self.remote.faults[3]='reject'
        self.assertEqual(self.run_once()['notification'],'claim_unconfirmed')
        self.assertEqual(self.run_once('1002')['state'],'noop');self.assertEqual(self.notify.calls,[])

    def test_lost_notification_response_never_automatically_resends(self):
        self.notify.outcome=TimeoutError('synthetic')
        self.assertEqual(self.run_once()['notification'],'unknown')
        self.assertEqual(self.run_once('1002')['state'],'noop')
        self.assertEqual(len(self.notify.calls),1);self.assertEqual(len(self.collections),1)

    def test_crash_after_notification_claim_has_no_automatic_send(self):
        self.remote.faults[3]='crash_after'
        with self.assertRaises(Crash):self.run_once()
        self.assertEqual(self.entry()['notification']['state'],'claimed')
        self.assertEqual(self.run_once('1002')['state'],'noop');self.assertEqual(self.notify.calls,[])

    def test_crash_after_send_leaves_claimed_and_does_not_replay(self):
        self.notify.outcome=Crash()
        with self.assertRaises(Crash):self.run_once()
        self.assertEqual(self.run_once('1002')['state'],'noop');self.assertEqual(len(self.notify.calls),1)

    def test_notification_ack_commit_failure_does_not_replay(self):
        self.remote.faults[4]='reject'
        self.assertEqual(self.run_once()['notification'],'unconfirmed')
        self.assertEqual(self.run_once('1002')['state'],'noop');self.assertEqual(len(self.notify.calls),1)

    def test_unknown_calendar_fails_closed(self):
        self.publisher.calendar=lambda *a:(_ for _ in ()).throw(RuntimeError('missing calendar'))
        self.assertEqual(self.run_once()['state'],'blocked')
        self.assertEqual(self.remote.commit_count,0);self.assertEqual(self.collections,[])

    def test_holiday_is_not_success_and_has_no_effects(self):
        self.publisher.calendar=lambda *a:{**facts(*a),'localIsSession':False,'localDate':'2026-11-01','session':'2026-10-30','latestCompleted':'2026-10-30'}
        self.assertEqual(self.run_once()['state'],'holiday');self.assertEqual(self.remote.commit_count,0)

    def test_past_request_is_not_backfilled_with_current_financials(self):
        self.assertEqual(self.run_once(requested_session='2026-10-30')['reason'],'historical_current_inputs_forbidden')
        self.assertEqual(self.collections,[])

    def test_midnight_rollover_during_collection_prevents_publish(self):
        calls=[]
        def crossing(*a):
            calls.append(1);return {**facts(*a),'localDate':'2026-11-03' if len(calls)>1 else '2026-11-02'}
        self.publisher.calendar=crossing
        self.assertEqual(self.run_once()['reason'],'collection_crossed_session_boundary')
        self.assertEqual(self.notify.calls,[])

    def test_legacy_success_without_record_requires_verified_adoption(self):
        self.remote.files[LATEST_PATH]=encoded(output('KR','2026-11-02',{},NOW.isoformat()))
        self.assertEqual(self.run_once()['reason'],'legacy_publication_requires_verified_adoption')
        self.assertEqual(self.collections,[]);self.assertEqual(self.remote.commit_count,0)

    def test_weekly_key_is_separate_and_weekly_not_intercepted(self):
        self.assertNotEqual(session_key('weekly_summary','ALL','2026-11-02'),session_key('daily','KR','2026-11-02'))
        with self.assertRaises(ValueError):self.run_once(mode='weekly_summary')
        self.assertEqual(self.remote.commit_count,0)

    def test_synthetic_durable_remote_survives_process_recreation(self):
        temp=Path(__file__).resolve().parents[1]/'.publication-test-temp';temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp) as d:
            self.assertTrue(Path(d).resolve().is_relative_to(temp.resolve()))
            remote=Remote(path=Path(d)/'main.json');notify=Notifier(remote)
            publisher=DailyPublisher(remote,notify,clock=lambda:NOW,calendar=facts)
            self.assertEqual(publisher.run('KR','1001',output)['state'],'published')
            reopened=Remote(path=Path(d)/'main.json');fresh_notify=Notifier(reopened)
            fresh=DailyPublisher(reopened,fresh_notify,clock=lambda:NOW,calendar=facts)
            self.assertEqual(fresh.run('KR','1002',lambda *a:self.fail('rescan'))['state'],'noop')
            self.assertEqual(fresh_notify.calls,[])

    def test_main_changed_after_claim_rejects_publish_without_overwrite(self):
        def concurrent_change(*args):
            self.remote.files['unrelated.txt']=b'synthetic concurrent change\n'
            return self.collect(*args)
        result=self.run_once(collect=concurrent_change)
        self.assertEqual(result['reason'],'publication_unconfirmed')
        self.assertEqual(self.remote.files['unrelated.txt'],b'synthetic concurrent change\n')
        self.assertEqual(self.notify.calls,[])
        self.assertFalse(any(p.startswith('daily_data/') for p in self.remote.files))

    def test_public_state_does_not_store_raw_error_or_notification_body(self):
        secret_marker='SYNTHETIC_PRIVATE_MARKER_DO_NOT_PUBLISH'
        self.run_once(collect=lambda *a:(_ for _ in ()).throw(RuntimeError(secret_marker)))
        self.assertNotIn(secret_marker,self.remote.files[STATE_PATH].decode())
        self.remote=Remote();self.notify=Notifier(self.remote)
        self.publisher=DailyPublisher(self.remote,self.notify,clock=lambda:NOW,calendar=facts)
        self.run_once()
        state=self.remote.files[STATE_PATH].decode()
        self.assertNotIn('SYNTHETIC ONLY',state);self.assertNotIn('Not a real recommendation',state)

    def test_rejected_notification_does_not_recollect_or_automatically_retry(self):
        self.notify.outcome='rejected'
        self.assertEqual(self.run_once()['notification'],'rejected')
        self.assertEqual(self.run_once('1002')['state'],'noop')
        self.assertEqual(len(self.collections),1);self.assertEqual(len(self.notify.calls),1)

    def test_published_commit_crash_is_recovered_read_only(self):
        self.remote.faults[2]='crash_after'
        with self.assertRaises(Crash):self.run_once()
        self.assertEqual(self.run_once('1002')['state'],'noop')
        self.assertEqual(len(self.collections),1);self.assertEqual(self.notify.calls,[])

    def test_uncompleted_session_record_is_not_trusted_as_completed_success(self):
        self.run_once()
        self.publisher.calendar=lambda *a:{**facts(*a),'completed':False}
        result=self.run_once('1002')
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(result['reason'],'session_not_completed')
        self.assertEqual(len(self.collections),1);self.assertEqual(len(self.notify.calls),1)


if __name__=='__main__':unittest.main()
