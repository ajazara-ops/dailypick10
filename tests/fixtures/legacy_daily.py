"""TEST ONLY: legacy daily function extracted verbatim from public baseline.
Origin: ajazara-ops/dailypick10 at 7f1a06d1ee7966b99633916baefd7920e38a75ac
Dependencies are supplied by the synthetic scanner harness; never run live.
"""

def generate_daily(target, requested_date=None):
    generated_at = utc_stamp()
    previous_path = Path('todays_recommendation.json')
    previous = json.loads(previous_path.read_text('utf-8')) if previous_path.exists() else {}
    updates = {}
    for market in (['US', 'KR'] if target == 'ALL' else [target]):
        session_date = market_session_date(market)
        if requested_date and requested_date != session_date:
            raise ValueError('Historical daily regeneration requires point-in-time data and is disabled')
        rows, meta = scan_market(market, us_universe if market == 'US' else get_korea_tickers,
                                lambda ticker, m: analyze_stock(ticker, m, target_date=session_date), previous, generated_at)
        if meta['status'] in {'fresh', 'empty'}:
            process_news_for_list(rows)
        updates[market] = (rows, meta)
    out = merge_daily(previous, updates, generated_at)
    indexes = analyze_market_condition()
    for key, value in indexes.items():
        if key in updates or key == 'VIX':
            out['market_status'][key] = value
    out['dominant_sectors'] = [v for v, _ in Counter(s.get('sector') for s in out['stocks'] if s.get('sector') not in (None, '기타')).most_common(2)]
    out['notification'] = daily_notification(updates)
    stamp = generated_at.replace(':', '').replace('+0000', 'Z')
    filename = f"{generated_at[:10]}_{stamp[11:]}_{target}_daily.json"
    write_json_once(Path(DAILY_DATA_DIR) / filename, out)
    write_latest('todays_recommendation.json', out)
    write_latest('.pending_notification.json', out['notification'])
    return out
