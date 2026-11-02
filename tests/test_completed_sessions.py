from datetime import datetime, timezone
import unittest

from market_calendar import daily_session_facts


class CalendarTests(unittest.TestCase):
    def facts(self,market,iso,session=None):
        return daily_session_facts(market,datetime.fromisoformat(iso),session)

    def test_kr_before_and_after_actual_close(self):
        before=self.facts('KR','2026-11-02T06:29:59+00:00')
        after=self.facts('KR','2026-11-02T06:30:00+00:00')
        self.assertEqual(before['session'],'2026-10-30');self.assertEqual(after['session'],'2026-11-02')

    def test_us_local_session_survives_utc_midnight(self):
        f=self.facts('US','2026-11-03T00:15:00+00:00')
        self.assertEqual(f['session'],'2026-11-02');self.assertEqual(f['localDate'],'2026-11-02')

    def test_kst_midnight_does_not_invent_new_completed_session(self):
        f=self.facts('KR','2026-11-02T15:00:00+00:00')
        self.assertEqual(f['session'],'2026-11-02');self.assertEqual(f['localDate'],'2026-11-03')

    def test_weekend_and_exchange_holidays_are_calendar_based(self):
        for market,iso,session in [('KR','2026-11-01T10:00:00+00:00','2026-10-30'),
                                    ('KR','2026-01-01T10:00:00+00:00','2025-12-30'),
                                    ('US','2026-12-25T23:00:00+00:00','2026-12-24')]:
            with self.subTest(market=market,iso=iso):
                f=self.facts(market,iso);self.assertEqual(f['session'],session);self.assertFalse(f['localIsSession'])

    def test_us_half_day_close_is_not_assumed_to_be_1600(self):
        f=self.facts('US','2026-11-27T18:00:00+00:00')
        self.assertEqual(f['session'],'2026-11-27');self.assertIn('18:00:00',f['closeAt'])

    def test_explicit_future_open_and_holiday_dates_fail_closed(self):
        f=self.facts('KR','2026-11-02T05:00:00+00:00','2026-11-02')
        self.assertFalse(f['completed'])
        with self.assertRaises(ValueError):self.facts('US','2026-12-25T23:00:00+00:00','2026-12-25')
        with self.assertRaises(ValueError):daily_session_facts('KR',datetime(2026,11,2))


if __name__=='__main__':unittest.main()
