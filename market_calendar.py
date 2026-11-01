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
