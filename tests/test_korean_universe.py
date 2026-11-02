"""Synthetic source contracts only: no network, scan, Firebase or push."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korean_universe import parse_market_cap_list, load_korean_universe
from scan_contract import SourceDataError, Observation, scan_market


def fixture(market='KOSPI'):
    exchange, base = ('KS', 0) if market == 'KOSPI' else ('KQ', 100)
    return dict(stockListSortType='MARKET_VALUE', stockListCategoryType=market,
                page=1, pageSize=50, totalCount=1000,
                stocks=[dict(itemCode=f'{base+i+1:06}', marketValue=f'{5000-i:,}',
                             stockExchangeType={'code': exchange}) for i in range(50)])


class Response:
    status_code = 200
    def __init__(self, data): self.data = data
    def json(self): return self.data


class KoreanUniverseTests(unittest.TestCase):
    def test_same_top50_each_market_and_stable100_universe(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return Response(fixture('KOSDAQ' if '/KOSDAQ?' in url else 'KOSPI'))
        result = load_korean_universe(fetch)
        self.assertEqual(len(result), 100)
        self.assertEqual(result, sorted(set(result)))
        self.assertIn('000001.KS', result)
        self.assertIn('000150.KQ', result)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('page=1&pageSize=50' in url for url in calls))

    def test_legacy_html_redirect_is_error_not_empty_success(self):
        class HtmlResponse:
            status_code = 200
            def json(self): raise json.JSONDecodeError('html', '<html>', 0)
        with self.assertRaisesRegex(SourceDataError, 'kr_universe_invalid_json'):
            load_korean_universe(lambda _: HtmlResponse())

    def test_incomplete_duplicate_wrong_market_and_sort_are_rejected(self):
        variants = []
        for field, value in [('stocks', fixture()['stocks'][:49]), ('stockListCategoryType', 'KOSDAQ'),
                             ('stockListSortType', 'VOLUME'), ('page', 2), ('pageSize', 100)]:
            data = fixture(); data[field] = value; variants.append(data)
        data = fixture(); data['stocks'][1]['itemCode'] = data['stocks'][0]['itemCode']; variants.append(data)
        data = fixture(); data['stocks'][1]['stockExchangeType']['code'] = 'KQ'; variants.append(data)
        data = fixture(); data['stocks'][1]['marketValue'] = '0'; variants.append(data)
        data = fixture(); data['stocks'][0]['marketValue'] = 'N/A'; variants.append(data)
        data = fixture(); data['stocks'][0]['itemCode'] = '../invalid'; variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                with self.assertRaises(SourceDataError): parse_market_cap_list(data, 'KOSPI')

    def test_one_market_failure_does_not_return_half_universe(self):
        with self.assertRaisesRegex(SourceDataError, 'kr_universe_fetch_failed'):
            load_korean_universe(lambda url: None if '/KOSDAQ?' in url else Response(fixture()))

    def test_declared_market_cap_order_is_not_reranked_by_asynchronous_quote_values(self):
        data = fixture('KOSDAQ')
        data['stocks'][45]['marketValue'] = '16,384'
        data['stocks'][46]['marketValue'] = '16,421'
        # The provider still declares MARKET_VALUE ordering. Live quote fields
        # were observed with different timestamps; preserve membership/order.
        result = parse_market_cap_list(data, 'KOSDAQ')
        self.assertEqual(result, [f'{101+i:06}.KQ' for i in range(50)])

    def test_provider_alphanumeric_codes_are_preserved_without_expanding_the_universe(self):
        for market, code, suffix in [('KOSPI','0126Z0','KS'),('KOSDAQ','0009K0','KQ')]:
            data = fixture(market)
            data['stocks'][0]['itemCode'] = code
            result = parse_market_cap_list(data, market)
            self.assertEqual(len(result), 50)
            self.assertEqual(result[0], f'{code}.{suffix}')

    def test_failed_source_preserves_previous_rows_and_timestamps(self):
        old = {'stocks':[{'id':'OLD.KS','market':'KR','currentPrice':100}],
               'markets':{'KR':{'generatedAt':'2026-10-29T09:30:00Z','quoteAsOf':'2026-10-29'}}}
        before = deepcopy(old)
        def fail(): raise SourceDataError('kr_universe_invalid_json')
        rows, meta = scan_market('KR', fail, lambda *_: self.fail('must not analyze'), old, '2026-10-30T09:30:00Z')
        self.assertEqual(rows, old['stocks']); self.assertEqual(old, before)
        self.assertEqual(meta['generatedAt'], old['markets']['KR']['generatedAt'])
        self.assertEqual(meta['status'], 'error'); self.assertTrue(meta['stale'])
        self.assertEqual(meta['universeError'], 'kr_universe_invalid_json')

    def test_unknown_exception_details_are_never_published(self):
        def fail(): raise RuntimeError('sensitive-unexpected-response')
        _, meta = scan_market('KR', fail, lambda *_: None, {}, '2026-10-30T09:30:00Z')
        self.assertEqual(meta['universeError'], 'universe_unavailable')
        self.assertNotIn('sensitive', json.dumps(meta))

    def test_success_clears_source_error_without_changing_empty_meaning(self):
        _, meta = scan_market('KR', lambda:['000001.KS'], lambda *_: Observation(None,'2026-10-30'), {}, '2026-10-30T09:30:00Z')
        self.assertEqual(meta['status'], 'empty')
        self.assertIsNone(meta['universeError'])

    def test_actual_scanner_entry_preserves_failure_for_the_market_contract(self):
        source = Path(__file__).resolve().parents[1] / 'daily_stock_scanner.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'get_korea_tickers')
        messages = []
        scope = {'load_korean_universe':load_korean_universe, 'fetch_with_retry':lambda _: None,
                 'print':messages.append}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'actual_scanner_entry', 'exec'), scope)
        _, meta = scan_market('KR', scope['get_korea_tickers'], lambda *_: self.fail('no universe'), {}, '2026-10-30T09:30:00Z')
        self.assertEqual(meta['universeError'], 'kr_universe_fetch_failed')
        self.assertEqual(meta['status'], 'error')
        self.assertTrue(any('::warning' in message for message in messages))


if __name__ == '__main__': unittest.main()
