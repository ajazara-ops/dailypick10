"""Offline integration: actual writers/quote adapter, synthetic data, real calendars.

Run with the existing calendar-test Python environment and an EMPTY output path.
No scanner CLI, credentials, Firebase, notification, or external quote calls.
"""
import argparse
from contextlib import ExitStack
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import isolated_scanner
from scan_contract import Observation
from market_calendar import is_market_session


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')


def stock(ticker, market, session, price=100):
    return dict(id=ticker, symbol=ticker, name=ticker, market=market, currentPrice=price,
                score=85, rsi=0, rvol=0, financials={}, priceBasis='unadjusted_close',
                quoteAsOf=session, generatedAt=session+'T23:15:00Z')


def run_case(root, publication, rows=None):
    import pandas as pd  # Local installed calendar dependency, no download.
    root.mkdir()
    saved_cwd = Path.cwd()
    scanner = isolated_scanner()
    scanner.is_market_session = is_market_session
    quote_calls = []

    def ticker(symbol):
        class SyntheticTicker:
            def history(self, **args):
                assert args['auto_adjust'] is False and args['actions'] is True
                target = (date.fromisoformat(args['end']) - timedelta(days=1)).isoformat()
                quote_calls.append((symbol, target))
                if symbol in {'A12', 'KMISS'}:
                    return pd.DataFrame()
                value = 100 + int(symbol[1:]) if symbol.startswith('A') else 103
                # The adapter must ignore future/high values and use only exact Close.
                index = pd.to_datetime([args['start'], target, (date.fromisoformat(target)+timedelta(days=3)).isoformat()])
                return pd.DataFrame({'Close':[100,value,9999], 'High':[100,9999,9999],
                                     'Stock Splits':[0,2 if symbol=='A13' else 0,0]}, index=index)
        return SyntheticTicker()

    try:
        os.chdir(root)
        if rows is None:
            for session, market, tickers in [
                ('2026-11-02','US',[f'A{i:02}' for i in range(10)]),
                ('2026-11-02','KR',['KGAIN','KMISS','KZERO']),
                ('2026-11-06','US',[f'A{i:02}' for i in range(4,14)]),
            ]:
                stamp = '2026-11-07T00:15:00Z' if session.endswith('06') else session+('T09:30:00Z' if market=='KR' else 'T23:15:00Z')
                def analyze(symbol, selected_market, target_date):
                    price = 200 if session.endswith('06') and int(symbol[1:]) < 10 else 100
                    return Observation(stock(symbol, selected_market, session, price), session)
                with ExitStack() as mocks:
                    mocks.enter_context(patch.object(scanner,'utc_stamp',return_value=stamp))
                    mocks.enter_context(patch.object(scanner,'market_session_date',return_value=session))
                    mocks.enter_context(patch.object(scanner,'us_universe',return_value=tickers))
                    mocks.enter_context(patch.object(scanner,'get_korea_tickers',return_value=tickers))
                    mocks.enter_context(patch.object(scanner,'analyze_stock',side_effect=analyze))
                    mocks.enter_context(patch.object(scanner,'process_news_for_list'))
                    mocks.enter_context(patch.object(scanner,'analyze_market_condition',return_value={}))
                    scanner.generate_daily(market)
        else:
            save(Path('daily_data/synthetic_daily.json'), {'stocks':rows})
        now = publication+'T08:00:00Z'
        with patch.object(scanner.yf,'Ticker',side_effect=ticker):
            output = scanner.generate_weekly_report(publication, now=now)
            scanner.update_history_index(now=now)
            before = output.read_bytes()
            report = json.loads(before)
            # Rerun does not fetch quotes, change entry prices, or fill missing prices.
            with patch.object(scanner,'exit_quote',side_effect=AssertionError('rerun must not fetch')):
                assert scanner.generate_weekly_report(publication, now=now).read_bytes()==before
        return {'publication':publication,'now':now,'file':output.as_posix(),
                'sha256':hashlib.sha256(before).hexdigest(), 'quoteCalls':len(quote_calls),
                'performance':report['performance'],'sourceDiagnostics':report['sourceDiagnostics']}
    finally:
        os.chdir(saved_cwd)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must not contain existing data')
    output.mkdir(parents=True, exist_ok=True)
    def denied(*args,**kwargs): raise AssertionError('Network prohibited in this test')
    with patch.object(socket.socket,'connect',denied), patch.object(socket,'create_connection',denied):
        cases={
            'normal':run_case(output/'normal','2026-11-21'),
            'holiday_exit':run_case(output/'holiday_exit','2027-01-02',[
                stock('USENTRY','US','2026-12-14'), stock('KRENTRY','KR','2026-12-14')]),
            'holiday_entry':run_case(output/'holiday_entry','2026-10-24',[
                stock('KRVALID','KR','2026-10-08'), stock('KRCLOSED','KR','2026-10-09'),
                stock('USVALID','US','2026-10-09')])}
    assert cases['normal']['performance']['US']['totalCount']==14
    assert cases['normal']['performance']['US']['observedCount']==12
    assert cases['holiday_exit']['performance']['US']['evaluationQuoteAsOf']=='2026-12-31'
    assert cases['holiday_exit']['performance']['KR']['evaluationQuoteAsOf']=='2026-12-30'
    assert cases['holiday_entry']['sourceDiagnostics']['nonTradingSessionRecordCount']==1
    save(output/'cases.json',cases)
    print(json.dumps({'cases':len(cases),'result':'passed','network':'blocked','output':str(output)}))


if __name__=='__main__': main()
