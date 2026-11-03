"""Calendar adapter only. No price requests or weekday/last-price holiday guesses."""
from datetime import date, timedelta, timezone
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


@lru_cache(maxsize=128)
def session_close_utc(market, day):
    """Return the exchange's actual close for this session; holidays return None."""
    import exchange_calendars as calendars
    target = date.fromisoformat(day)
    calendar = calendars.get_calendar(CALENDARS[market],
                                      start=(target - timedelta(days=7)).isoformat(),
                                      end=(target + timedelta(days=7)).isoformat())
    if not calendar.is_session(day):
        return None
    close = calendar.session_close(day).to_pydatetime()
    if close.tzinfo is None or close.utcoffset() is None:
        raise ValueError('exchange_close_timezone_missing')
    return close.astimezone(timezone.utc)
