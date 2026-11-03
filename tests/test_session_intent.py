"""Synthetic tests: no GitHub dispatch, network, credential or live scan."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolve_scan_event import resolve_event
from market_calendar import daily_session_facts
from scan_publication import DailyPublisher
from fake_repository import Remote, Notifier
from test_scan_publication import output
from test_weekly_publication import report, sources, WEEK_NOW
from weekly_publication import WeeklyPublisher
import test_guarded_daily as guarded_fixtures


def dispatch(mode, target, key):
    return resolve_event('workflow_dispatch', {'inputs': {'mode':mode,'target':target,'session_key':key}})


class IntentBoundaryTests(unittest.TestCase):
    def test_scheduled_cron_never_invents_session_even_with_extra_payload(self):
        for cron in ('30 9 * * 1-5', '15 23 * * 1-5', '0 8 * * 6', 'unknown'):
            with self.subTest(cron=cron), self.assertRaisesRegex(ValueError, 'session_unverified'):
                resolve_event('schedule', {'schedule':cron,'session_key':'daily:KR:2026-11-02'})

    def test_explicit_daily_key_and_market_are_canonical(self):
        self.assertEqual(dispatch('daily','KR','daily:KR:2026-11-02'),('daily','KR','2026-11-02'))
        self.assertEqual(dispatch('daily','US','daily:US:2026-11-02'),('daily','US','2026-11-02'))
        for target,key in [('ALL','daily:KR:2026-11-02'),('KR','daily:US:2026-11-02'),
                           ('KR','daily:KR:2026-02-30'),('KR','daily:KR:2026-11-02\nx=1'),('KR','')]:
            with self.subTest(target=target,key=key), self.assertRaises(ValueError): dispatch('daily',target,key)

    def test_explicit_weekly_key_maps_to_the_fixed_saturday(self):
        key='weekly:ALL:2026-11-02:2026-11-06:2026-11-20'
        self.assertEqual(dispatch('weekly_summary','ALL',key),('weekly_summary','ALL','2026-11-21'))
        self.assertEqual(dispatch('weekly','ALL',key),('weekly','ALL','2026-11-21'))
        for target,bad in [('KR',key),('ALL','weekly:ALL:2026-11-03:2026-11-06:2026-11-20'),
                           ('ALL','weekly:ALL:2026-11-02:2026-11-06:2026-11-19')]:
            with self.subTest(target=target,key=bad), self.assertRaises(ValueError): dispatch('weekly',target,bad)

    def test_resume_keeps_existing_delivery_contract_and_never_collects_a_session(self):
        self.assertEqual(resolve_event('workflow_dispatch',{'inputs':{'mode':'pages_resume','delivery_id':'a'*64}}),
                         ('pages_resume','ALL',''))
        with self.assertRaises(ValueError): resolve_event('workflow_dispatch',{'inputs':{'mode':'pages_resume','delivery_id':'x'}})

    def test_session_calendar_handles_dst_close_and_holidays(self):
        # NY close moves from 21:00 UTC to 20:00 UTC over US DST spring transition.
        for stamp,day in [('2026-03-06T21:00:00+00:00','2026-03-06'),
                          ('2026-03-09T20:00:00+00:00','2026-03-09'),
                          ('2026-11-03T00:15:00+00:00','2026-11-02')]:
            facts=daily_session_facts('US',datetime.fromisoformat(stamp),day)
            self.assertTrue(facts['completed']);self.assertEqual(facts['latestCompleted'],day)
        for market,stamp,day in [('US','2026-12-25T23:00:00+00:00','2026-12-25'),
                                 ('KR','2026-01-01T10:00:00+00:00','2026-01-01')]:
            with self.subTest(market=market), self.assertRaises(ValueError):
                daily_session_facts(market,datetime.fromisoformat(stamp),day)

    def test_daily_late_and_restarted_duplicate_do_not_recollect(self):
        for market in ('KR','US'):
            with self.subTest(market=market):
                remote=Remote();notifier=Notifier(remote);calls=[]
                def collect(market,session,previous,stamp):
                    calls.append((market,session));return output(market,session,previous,stamp)
                clock=[datetime(2026,11,2,23,tzinfo=timezone.utc)]
                def facts(market,now,requested=None):
                    day='2026-11-02' if now.day==2 else '2026-11-03'
                    return {'session':requested or day,'latestCompleted':day,'localDate':day,
                            'localIsSession':True,'completed':True}
                first=DailyPublisher(remote,notifier,clock=lambda:clock[0],calendar=facts)
                self.assertEqual(first.run(market,'1001',collect,requested_session='2026-11-02')['state'],'published')
                files=deepcopy(remote.files);commits=remote.commit_count
                clock[0]=datetime(2026,11,3,23,tzinfo=timezone.utc)
                restarted=DailyPublisher(remote,notifier,clock=lambda:clock[0],calendar=facts)
                self.assertEqual(restarted.run(market,'1002',collect,requested_session='2026-11-02')['state'],'noop')
                self.assertEqual(remote.files,files);self.assertEqual(remote.commit_count,commits)
                self.assertEqual(calls,[(market,'2026-11-02')]);self.assertEqual(len(notifier.calls),1)
                fresh=Remote();n=Notifier(fresh)
                blocked=DailyPublisher(fresh,n,clock=lambda:clock[0],calendar=facts)
                self.assertEqual(blocked.run(market,'1003',collect,requested_session='2026-11-02')['reason'],
                                 'historical_current_inputs_forbidden')
                self.assertEqual(calls,[(market,'2026-11-02')]);self.assertEqual(n.calls,[])

    def test_kr_midnight_blocks_previous_session_without_backdating(self):
        remote=Remote();notifier=Notifier(remote);calls=[]
        publisher=DailyPublisher(remote,notifier,clock=lambda:datetime(2026,11,3,15,1,tzinfo=timezone.utc))
        result=publisher.run('KR','1001',lambda *args:calls.append(args),requested_session='2026-11-03')
        self.assertEqual(result['reason'],'historical_current_inputs_forbidden')
        self.assertEqual(calls,[]);self.assertEqual(notifier.calls,[]);self.assertEqual(remote.commit_count,0)

    def test_weekly_explicit_period_before_release_and_after_restart(self):
        remote=Remote(sources());notifier=Notifier(remote);calls=[]
        def collect(period,snapshot,stamp):calls.append(period);return report(period,stamp)
        clock=[datetime(2026,11,21,7,59,tzinfo=timezone.utc)]
        p=WeeklyPublisher(remote,notifier,clock=lambda:clock[0])
        self.assertEqual(p.run('1101',collect,publication_date='2026-11-21')['state'],'blocked')
        self.assertEqual(calls,[])
        clock[0]=WEEK_NOW
        self.assertEqual(p.run('1102',collect,publication_date='2026-11-21')['state'],'published')
        files=deepcopy(remote.files);commits=remote.commit_count
        restarted=WeeklyPublisher(remote,notifier,clock=lambda:clock[0])
        self.assertEqual(restarted.run('1103',collect,publication_date='2026-11-21')['state'],'noop')
        self.assertEqual(remote.files,files);self.assertEqual(remote.commit_count,commits)
        self.assertEqual(len(calls),1);self.assertEqual(len(notifier.calls),1)

    def test_actual_scanner_cli_enforces_date_before_factory_and_forwards_it(self):
        harness=guarded_fixtures.GuardedScannerTests(methodName='runTest');harness.setUp();self.addCleanup(harness.doCleanups)
        scanner=harness.scanner;calls=[]
        def factory(_):calls.append('factory');return harness.publisher,'1001'
        for args in (['--mode','daily','--target','KR'],['--mode','daily','--target','ALL','--date','2026-11-02'],
                     ['--mode','weekly','--target','ALL','--date','2026-11-20']):
            with self.subTest(args=args), self.assertRaises(ValueError):scanner.main(args,runtime_factory=factory)
        self.assertEqual(calls,[])
        scanner.collect_daily_session=lambda market,session,previous,stamp:output(market,session,previous,stamp)
        mode,target,day=dispatch('daily','KR','daily:KR:2026-11-02')
        result=scanner.main(['--mode',mode,'--target',target,'--date',day],runtime_factory=factory)
        self.assertEqual(result['key'],'daily:KR:2026-11-02')
        self.assertEqual(result['state'],'published')
        before=deepcopy(harness.remote.files); commits=harness.remote.commit_count
        mode,target,day=dispatch('daily','KR','daily:KR:2026-11-02')
        result=scanner.main(['--mode',mode,'--target',target,'--date',day],runtime_factory=factory)
        self.assertEqual(result['state'],'noop')
        self.assertEqual(harness.remote.files,before);self.assertEqual(harness.remote.commit_count,commits)
        self.assertEqual(len(harness.notify.calls),1)
        class WeeklyProbe:
            def run(self,run_id,collect,*,publication_date=None):
                calls.append(('weekly',run_id,publication_date));return {'state':'noop'}
        mode,target,day=dispatch('weekly_summary','ALL','weekly:ALL:2026-11-02:2026-11-06:2026-11-20')
        scanner.main(['--mode',mode,'--target',target,'--date',day],
                     weekly_runtime_factory=lambda _:(WeeklyProbe(),'2001'))
        self.assertIn(('weekly','2001','2026-11-21'),calls)


if __name__ == '__main__': unittest.main(verbosity=2)
