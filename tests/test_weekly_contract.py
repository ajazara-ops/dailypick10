from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from weekly_contract import weekly_period, latest_publication, assert_publishable, weekly_cohort, settle_weekly, build_weekly_report
from market_calendar import evaluation_session
from test_contract import isolated_scanner, TEST_TEMP

PERIOD=weekly_period('2026-11-21')
NOW='2026-11-21T08:00:00Z'
def row(ticker='A', session='2026-11-02', **extra):
    return dict(dict(id=ticker,symbol=ticker,market='US',quoteAsOf=session,generatedAt=session+'T23:15:00Z',
                     currentPrice=100,score=85,rsi=0,priceBasis='unadjusted_close'),**extra)
def snapshot(*rows,filename='2026-11-07_daily.json'):
    return filename,{'stocks':list(rows)}
def settle(rows,session=lambda market,day:day,quote=lambda item,day:{'price':110,'quoteAsOf':day,'hasSplit':False}):
    cohort,diagnostic=weekly_cohort([snapshot(*rows)],PERIOD)
    measured,sessions=settle_weekly(cohort,PERIOD,session,quote)
    return build_weekly_report(measured,PERIOD,NOW,sessions,diagnostic)

class WeeklyContractTests(unittest.TestCase):
    def test_non_trading_entry_session_is_not_a_recommendation(self):
        cohort, diagnostic = weekly_cohort([snapshot(row('VALID'), row('CLOSED', '2026-11-03'))], PERIOD,
                                          lambda market, day: day != '2026-11-03')
        self.assertEqual([item['id'] for item in cohort], ['VALID'])
        self.assertEqual(diagnostic['nonTradingSessionRecordCount'], 1)

    def test_confirmed_example_and_year_month_boundaries(self):
        self.assertEqual((PERIOD['weekStart'],PERIOD['weekEnd'],PERIOD['evaluationDate'],PERIOD['publishAt']),
                         ('2026-11-02','2026-11-06','2026-11-20','2026-11-21T17:00:00+09:00'))
        self.assertEqual(weekly_period('2027-01-09')['weekStart'],'2026-12-21')
        for day in ['2026-11-20','2026-11-22']:
            with self.assertRaises(ValueError):weekly_period(day)

    def test_saturday_boundary_and_delayed_run_select_same_publication(self):
        for now in ['2026-11-20T23:00:00Z','2026-11-21T07:59:59Z']:
            with self.assertRaises(ValueError):assert_publishable(PERIOD,now)
        assert_publishable(PERIOD,NOW)
        self.assertEqual(latest_publication('2026-11-21T07:59:59Z'),'2026-11-14')
        self.assertEqual(latest_publication(NOW),'2026-11-21')
        self.assertEqual(latest_publication('2026-11-23T04:00:00Z'),'2026-11-21')

    def test_session_week_excludes_adjacent_weeks_and_weekend(self):
        rows=[row('BEFORE','2026-10-30'),row('MON'),row('FRI','2026-11-06'),row('SAT','2026-11-07'),row('AFTER','2026-11-09')]
        cohort,_=weekly_cohort([snapshot(*rows)],PERIOD)
        self.assertEqual({s['id'] for s in cohort},{'MON','FRI'})

    def test_us_friday_is_included_from_kst_saturday_file_and_timestamp(self):
        cohort,_=weekly_cohort([snapshot(row('USFRI','2026-11-06',generatedAt='2026-11-07T08:15:00+09:00'),filename='2026-11-07_daily.json')],PERIOD)
        self.assertEqual(cohort[0]['recommendDate'],'2026-11-06')
        self.assertEqual(cohort[0]['buyPrice'],100)

    def test_first_weekly_recommendation_price_is_fixed_per_market_and_ticker(self):
        snapshots=[snapshot(row('A','2026-11-06',currentPrice=200)),snapshot(row('A',currentPrice=100)),snapshot(row('A',market='KR',currentPrice=50))]
        before=deepcopy(snapshots);cohort,d=weekly_cohort(snapshots,PERIOD)
        self.assertEqual({(s['market'],s['id']):s['buyPrice'] for s in cohort},{('US','A'):100,('KR','A'):50})
        self.assertEqual(d['duplicateRecordCount'],1);self.assertEqual(snapshots,before)

    def test_missing_first_price_is_not_replaced_by_later_better_observation(self):
        report=settle([row(currentPrice=None),row(session='2026-11-06',currentPrice=200)])
        self.assertEqual(report['performance']['US']['totalCount'],1)
        self.assertEqual(report['stocks'][0]['returnStatus'],'missing_entry_price')
        self.assertIsNone(report['stocks'][0]['returnRate'])

    def test_same_day_order_and_unknown_conflicting_prices(self):
        early=row(generatedAt='2026-11-02T22:00:00Z',currentPrice=90)
        later=row(generatedAt='2026-11-02T23:00:00Z',currentPrice=100)
        cohort,_=weekly_cohort([snapshot(later,early)],PERIOD);self.assertEqual(cohort[0]['buyPrice'],90)
        cohort,d=weekly_cohort([snapshot(early,row(generatedAt=None,currentPrice=100))],PERIOD)
        self.assertIsNone(cohort[0]['buyPrice']);self.assertEqual(d['ambiguousEntryCount'],1)

    def test_unknown_session_does_not_borrow_archive_or_global_date(self):
        bad=row(quoteAsOf=None);cohort,d=weekly_cohort([('2026-11-02_daily.json',{'stocks':[bad],'timestamp':'2026-11-02'})],PERIOD)
        self.assertEqual(cohort,[]);self.assertEqual(d['unknownSessionRecordCount'],1)

    def test_later_reanalysis_cannot_backdate_a_recommendation_into_closed_week(self):
        cohort,d=weekly_cohort([snapshot(row('LATE','2026-11-06',generatedAt='2026-11-09T23:15:00Z'),row('ORIGINAL'))],PERIOD)
        self.assertEqual([s['id'] for s in cohort],['ORIGINAL']);self.assertEqual(d['lateRecordCount'],1)

    def test_holiday_uses_market_calendar_not_each_stocks_last_available_price(self):
        calls=[]
        def quote(item,day):calls.append((item['market'],day));return {'price':103,'quoteAsOf':day,'hasSplit':False}
        report=settle([row(),row('K',market='KR')],lambda market,day:'2026-11-19' if market=='KR' else day,quote)
        self.assertEqual(set(calls),{('US','2026-11-20'),('KR','2026-11-19')})
        self.assertEqual(report['performance']['KR']['evaluationQuoteAsOf'],'2026-11-19')
        self.assertEqual(report['stocks'][0]['returnRate'],3)

    def test_ticker_missing_expected_session_is_unevaluated_not_fake_holiday(self):
        for day in ['2026-11-19','2026-11-23']:
            report=settle([row()],quote=lambda item,target:{'price':200,'quoteAsOf':day,'hasSplit':False})
            self.assertIsNone(report['stocks'][0]['returnRate']);self.assertEqual(report['performance']['US']['missingCount'],1)

    def test_current_high_and_future_prices_are_not_used(self):
        report=settle([row()],quote=lambda item,day:{'price':101,'quoteAsOf':day,'hasSplit':False,'currentPrice':200,'High':500})
        self.assertEqual(report['stocks'][0]['returnRate'],1)

    def test_missing_price_real_zero_and_split_status_are_separate(self):
        def quote(item,day):
            return {'price':None if item['id']=='M' else 100,'quoteAsOf':day,'hasSplit':True if item['id']=='S' else None if item['id']=='U' else False}
        report=settle([row('Z'),row('M'),row('S'),row('U')],quote=quote)
        by_id={s['id']:s for s in report['stocks']}
        self.assertEqual(by_id['Z']['returnRate'],0)
        self.assertEqual((report['performance']['US']['totalCount'],report['performance']['US']['observedCount'],report['performance']['US']['missingCount']),(4,1,3))
        self.assertEqual(by_id['S']['returnStatus'],'corporate_action_requires_adjustment')
        self.assertEqual(by_id['U']['returnStatus'],'corporate_action_status_unverified')

    def test_top10_is_per_market_sorted_by_observed_return_not_full_average(self):
        report=settle([row(f'A{i:02}') for i in range(12)]+[row('K',market='KR')],quote=lambda item,day:{'price':100+(int(item['id'][1:]) if item['market']=='US' else -1),'quoteAsOf':day,'hasSplit':False})
        self.assertEqual(len(report['winnerRankings']['US']),10);self.assertEqual(len(report['winnerRankings']['KR']),1)
        self.assertTrue(report['winnerRankings']['US'][0].endswith(':A11'))
        self.assertEqual(report['performance']['US']['totalCount'],12)
        self.assertNotIn('averageObservedReturn',report['performance']['US'])

    def test_calendar_missing_fails_closed_without_price_fetch(self):
        quote=Mock(side_effect=AssertionError('must not fetch'))
        report=settle([row()],lambda *_:(_ for _ in ()).throw(RuntimeError('calendar missing')),quote)
        quote.assert_not_called();self.assertEqual(report['stocks'][0]['returnStatus'],'calendar_unavailable')

    def test_calendar_adapter_uses_previous_session_and_bounds_beyond_holiday(self):
        fake=types.ModuleType('exchange_calendars');calendar=Mock();calendar.date_to_session.return_value=datetime(2026,11,19)
        fake.get_calendar=Mock(return_value=calendar)
        with patch.dict(sys.modules,{'exchange_calendars':fake}):
            self.assertEqual(evaluation_session('KR','2026-11-20'),'2026-11-19')
        calendar.date_to_session.assert_called_once_with('2026-11-20',direction='previous')
        self.assertEqual(fake.get_calendar.call_args.args,('XKRX',))
        self.assertGreater(fake.get_calendar.call_args.kwargs['end'],'2026-11-20')

    def test_native_schedule_remains_saturday_17_kst(self):
        workflow=(ROOT/'.github/workflows/daily_scan.yml').read_text('utf-8')
        self.assertIn("cron: '0 8 * * 6'",workflow)
        self.assertNotIn("cron: '0 8 * * 5'",workflow)


class WeeklyFileTests(unittest.TestCase):
    def test_corrupt_frozen_report_fails_without_overwrite_or_refetch(self):
        scanner = isolated_scanner()
        for content in ['{"stocks":', '[]', json.dumps({'reportKind': 'weekly_top10_v3', 'period': PERIOD})]:
            with self.subTest(content=content), tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
                target = Path(folder) / '2026-11-21_v3_weekly.json'; target.write_text(content)
                scanner.WEEKLY_REPORT_DIR = folder
                with patch.object(scanner, 'exit_quote') as quote:
                    with self.assertRaises(ValueError): scanner.generate_weekly_report('2026-11-21', now=NOW)
                    quote.assert_not_called()
                self.assertEqual(target.read_text(), content)

    def test_history_index_skips_nonobject_and_incomplete_v3_reports(self):
        scanner = isolated_scanner()
        for content in ['[]', json.dumps({'reportKind': 'weekly_top10_v3', 'period': PERIOD})]:
            with self.subTest(content=content), tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
                (Path(folder) / '2026-11-21_v3_weekly.json').write_text(content)
                scanner.WEEKLY_REPORT_DIR = folder
                with patch.object(scanner, 'write_latest') as write:
                    scanner.update_history_index(now=NOW)
                    self.assertEqual(write.call_args.args[1], [])

    def test_entry_calendar_failure_does_not_freeze_empty_result(self):
        scanner = isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            root = Path(folder); daily = root / 'daily'; daily.mkdir()
            (daily / '2026-11-02_daily.json').write_text(json.dumps({'stocks': [row()]}))
            scanner.DAILY_DATA_DIR = str(daily); scanner.WEEKLY_REPORT_DIR = str(root / 'weekly')
            with patch.object(scanner, 'is_market_session', side_effect=RuntimeError('calendar unavailable')), patch.object(scanner, 'exit_quote') as quote:
                with self.assertRaises(RuntimeError): scanner.generate_weekly_report('2026-11-21', now=NOW)
                quote.assert_not_called()
                self.assertFalse((root / 'weekly').exists())

    def test_unmatured_week_does_not_fetch_or_create_report(self):
        scanner=isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder,patch.object(scanner,'exit_quote') as quote:
            scanner.WEEKLY_REPORT_DIR=folder
            with self.assertRaises(ValueError):scanner.generate_weekly_report('2026-11-21',now='2026-11-21T07:59:59Z')
            self.assertEqual(list(Path(folder).iterdir()),[]);quote.assert_not_called()

    def test_rerun_freezes_entries_exits_missing_rows_and_legacy_files(self):
        scanner=isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            root=Path(folder);daily=root/'daily';weekly=root/'weekly';daily.mkdir();weekly.mkdir()
            legacy=weekly/'2026-11-21_v2_weekly.json';legacy.write_text('{"keep":true}')
            source=daily/'2026-11-07_daily.json';source.write_text(json.dumps({'stocks':[row()]}))
            scanner.DAILY_DATA_DIR=str(daily);scanner.WEEKLY_REPORT_DIR=str(weekly)
            with patch.object(scanner,'evaluation_session',side_effect=lambda m,d:d),patch.object(scanner,'exit_quote',return_value={'price':101,'quoteAsOf':'2026-11-20','hasSplit':False}) as quote:
                output=scanner.generate_weekly_report('2026-11-21',now=NOW);before=output.read_bytes()
                source.write_text(json.dumps({'stocks':[row(currentPrice=50)]}));quote.side_effect=AssertionError('No refetch on rerun')
                scanner.generate_weekly_report('2026-11-21',now='2026-11-28T08:00:00Z')
                self.assertEqual(output.read_bytes(),before);self.assertEqual(quote.call_count,1)
                self.assertEqual(json.loads(before)['stocks'][0]['exitPrice'],101)
                self.assertEqual(legacy.read_text(),'{"keep":true}')

    def test_history_index_hides_future_results_and_prefers_v3_only_after_publication(self):
        scanner=isolated_scanner()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as folder:
            root=Path(folder);(root/'2026-11-21_v3_weekly.json').write_text(json.dumps(settle([row()])))
            scanner.WEEKLY_REPORT_DIR=str(root)
            with patch.object(scanner,'write_latest') as write:
                scanner.update_history_index(now='2026-11-21T07:59:59Z');self.assertEqual(write.call_args.args[1],[])
                scanner.update_history_index(now=NOW);self.assertEqual(len(write.call_args.args[1]),1)
                self.assertEqual(write.call_args.args[1][0]['publishAt'],PERIOD['publishAt'])

if __name__=='__main__':unittest.main()
