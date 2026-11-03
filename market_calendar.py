"""Calendar adapter only. No price requests or weekday/last-price holiday guesses."""
from datetime import date, timedelta
from functools import lru_cache

CALENDARS = {'US': 'XNYS', 'KR': 'XKRX'}


def evaluation_session(market, friday):
    import exchange_calendars as calendars
    target = date.fromisoformat(friday)
    calendar = calendars.get_calendar(CALENDARS[market],
                                      start=(target - timedelta(days=30)).isoformat(),
                                      end=(target + timedelta(days=7)).isoformat())
    return calendar.date_to_session(friday, direction='previous').strftime('%Y-%m-%d')


@lru_cache(maxsize=128)
def is_market_session(market, day):
    """Only actual sessions may supply a recommendation entry date."""
    return evaluation_session(market, day) == day


def daily_session_facts(market, now, requested_session=None):
    """Use installed exchange calendars and an aware server clock, never weekday guesses."""
    import exchange_calendars as calendars
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from scan_contract import TIMEZONES
    if market not in CALENDARS or now.tzinfo is None:
        raise ValueError('verified_market_and_aware_server_clock_required')
    local_day = now.astimezone(ZoneInfo(TIMEZONES[market])).date()
    requested = date.fromisoformat(requested_session) if requested_session else local_day
    start = min(requested, local_day) - timedelta(days=40)
    calendar = calendars.get_calendar(CALENDARS[market], start=start.isoformat(),
                                      end=(max(requested, local_day)+timedelta(days=7)).isoformat())
    latest = calendar.date_to_session(local_day.isoformat(), direction='previous')
    if calendar.session_close(latest).to_pydatetime() > now.astimezone(timezone.utc):
        latest = calendar.previous_session(latest)
    session = requested_session or latest.strftime('%Y-%m-%d')
    # Invalid requested holiday is not silently moved to a different date.
    if not calendar.is_session(session):
        raise ValueError('requested_date_is_not_session')
    close = calendar.session_close(session).to_pydatetime()
    return {'session': session, 'latestCompleted': latest.strftime('%Y-%m-%d'),
            'localDate': local_day.isoformat(), 'localIsSession': bool(calendar.is_session(local_day.isoformat())),
            'completed': close <= now.astimezone(timezone.utc), 'closeAt': close.isoformat()}
