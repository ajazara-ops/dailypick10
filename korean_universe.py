"""Naver public market-cap list adapter; selection remains 50 per KR market.

The old finance.naver.com HTML now redirects to a client-rendered stock page.
Do not interpret missing legacy links, partial pages, or changed sorting as a
valid smaller universe. Prices in this payload are NOT recommendation prices.
"""
import re
from scan_contract import SourceDataError

MARKETS = {'KOSPI': 'KS', 'KOSDAQ': 'KQ'}
PER_MARKET = 50


def parse_market_cap_list(payload, market):
    if market not in MARKETS:
        raise ValueError('Unsupported Korean market')
    if not isinstance(payload, dict) or (
        payload.get('stockListSortType') != 'MARKET_VALUE' or
        payload.get('stockListCategoryType') != market or
        payload.get('page') != 1 or payload.get('pageSize') != PER_MARKET
    ):
        raise SourceDataError('kr_universe_invalid_contract')
    rows = payload.get('stocks')
    if not isinstance(rows, list) or len(rows) != PER_MARKET:
        raise SourceDataError('kr_universe_incomplete')
    codes, caps = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceDataError('kr_universe_invalid_contract')
        code, cap = row.get('itemCode'), row.get('marketValue')
        exchange = row.get('stockExchangeType')
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9A-Z]{6}', code) or
            not isinstance(exchange, dict) or exchange.get('code') != MARKETS[market] or
            not isinstance(cap, str) or not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)', cap)):
            raise SourceDataError('kr_universe_invalid_contract')
        codes.append(code)
        caps.append(int(cap.replace(',', '')))
    if len(set(codes)) != PER_MARKET:
        raise SourceDataError('kr_universe_incomplete')
    if any(cap <= 0 for cap in caps):
        raise SourceDataError('kr_universe_invalid_order')
    # Keep the provider's explicitly declared MARKET_VALUE page membership.
    # Live marketValue fields can update at different times and briefly invert
    # adjacent values. Re-sorting a partial page cannot reconstruct the universe;
    # rejecting that page would incorrectly stop an otherwise valid KR scan.
    return [f'{code}.{MARKETS[market]}' for code in codes]


def load_korean_universe(fetch):
    """Use the caller's bounded HTTP adapter; no hidden fallback or effects."""
    tickers = []
    for market in MARKETS:
        url = f'https://m.stock.naver.com/api/stocks/marketValue/{market}?page=1&pageSize={PER_MARKET}'
        response = fetch(url)
        if response is None or response.status_code != 200:
            raise SourceDataError('kr_universe_fetch_failed')
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceDataError('kr_universe_invalid_json') from exc
        tickers.extend(parse_market_cap_list(payload, market))
    return sorted(tickers)
