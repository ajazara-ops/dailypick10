"""Weekly first-recommendation price rankings. Pure functions, no external I/O."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from scan_contract import TIMEZONES, SourceDataError, number, price

KST = timezone(timedelta(hours=9))
REPORT_KIND = 'weekly_top10_v3'


def aware_time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError('An explicit timezone is required')
    return stamp


def latest_publication(now):
    local = aware_time(now).astimezone(KST)
    saturday = local.date() - timedelta(days=(local.weekday() - 5) % 7)
    if local < datetime.combine(saturday, time(17), KST):
        saturday -= timedelta(days=7)
    return saturday.isoformat()


def weekly_period(publication_date):
    publication = date.fromisoformat(publication_date)
    if publication.weekday() != 5:
        raise ValueError('Weekly publication must be a Saturday (KST)')
    week_end = publication - timedelta(days=15)
    return {'weekStart': (week_end - timedelta(days=4)).isoformat(), 'weekEnd': week_end.isoformat(),
            'evaluationDate': (publication - timedelta(days=1)).isoformat(),
            'publicationDate': publication.isoformat(),
            'publishAt': datetime.combine(publication, time(17), KST).isoformat(),
            'timezone': 'Asia/Seoul', 'entryPolicy': 'first_recommendation_in_market_session_week',
            'exitPolicy': 'evaluation_friday_or_previous_market_session_close'}


def assert_publishable(period, now):
    if aware_time(now) < aware_time(period['publishAt']):
        raise ValueError('Weekly cohort is not publishable before Saturday 17:00 KST')


def session_date(stock):
    # A KST archive filename is not a US session date. Neither is a refreshed
    # envelope timestamp. Older rows without explicit session evidence stay unknown.
    value = stock.get('recommendationSessionDate') or stock.get('quoteAsOf')
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def weekly_cohort(snapshots, period, is_market_session=None):
    grouped = {}
    diagnostics = {'unknownSessionRecordCount': 0, 'invalidIdentityCount': 0,
                   'ambiguousEntryCount': 0, 'duplicateRecordCount': 0, 'lateRecordCount': 0,
                   'nonTradingSessionRecordCount': 0}
    collection_cutoff = datetime.combine(date.fromisoformat(period['weekEnd']) + timedelta(days=1), time(17), KST).timestamp()
    for filename, payload in snapshots:
        for raw in payload.get('stocks', []):
            if not isinstance(raw, dict) or raw.get('market') not in TIMEZONES or not str(raw.get('id') or '').strip():
                diagnostics['invalidIdentityCount'] += 1
                continue
            session = session_date(raw)
            if session is None:
                diagnostics['unknownSessionRecordCount'] += 1
                continue
            if not period['weekStart'] <= session <= period['weekEnd'] or date.fromisoformat(session).weekday() > 4:
                continue
            # Production supplies the exchange-calendar adapter. Calendar errors
            # propagate: do not freeze an empty report after a failed validation.
            if is_market_session is not None and not is_market_session(raw['market'], session):
                diagnostics['nonTradingSessionRecordCount'] += 1
                diagnostics['unknownSessionRecordCount'] += 1  # Existing app warns that this row cannot be assigned.
                continue
            key = (raw['market'], str(raw['id']).strip().upper())
            try:
                stamp = aware_time(raw.get('scoreAsOf') or raw.get('generatedAt')).timestamp()
            except (ValueError, TypeError, AttributeError):
                stamp = None
            if stamp is not None and stamp > collection_cutoff:
                diagnostics['lateRecordCount'] += 1
                continue
            grouped.setdefault(key, []).append((session, stamp, filename, raw))
    rows = []
    for (market, ticker), observations in sorted(grouped.items()):
        first_session = min(item[0] for item in observations)
        first = [item for item in observations if item[0] == first_session]
        first.sort(key=lambda item: (item[1] is None, item[1] or 0, item[2]))
        chosen = first[0]
        # If the order cannot be known, never silently substitute a better price.
        first_stamp = chosen[1]
        ties = [item for item in first if item[1] is None or item[1] == first_stamp]
        values = {(number(item[3].get('currentPrice')), item[3].get('priceBasis')) for item in ties}
        ambiguous = len(values) > 1
        item = deepcopy(chosen[3])
        item.update(id=ticker, market=market, recommendationId=f"{period['weekStart']}:{market}:{ticker}",
                    recommendDate=first_session, recommendationSessionDate=first_session,
                    buyPrice=number(item.get('currentPrice')), sourceFile=chosen[2],
                    returnRate=None, exitPrice=None, exitQuoteAsOf=None,
                    returnStatus='ambiguous_entry_price' if ambiguous else 'unavailable')
        if ambiguous:
            item['buyPrice'] = None
            diagnostics['ambiguousEntryCount'] += 1
        rows.append(item)
        diagnostics['duplicateRecordCount'] += len(observations) - 1
    return rows, diagnostics


def settle_weekly(cohort, period, get_market_session, get_exit_quote):
    sessions = {}
    for market in TIMEZONES:
        try:
            session = get_market_session(market, period['evaluationDate'])
            if not period['weekEnd'] < session <= period['evaluationDate']:
                raise SourceDataError('invalid_calendar_session')
            date.fromisoformat(session)
            sessions[market] = {'evaluationQuoteAsOf': session, 'calendarStatus': 'resolved',
                                'calendarSource': 'exchange_calendars: XNYS / XKRX'}
        except Exception:
            sessions[market] = {'evaluationQuoteAsOf': None, 'calendarStatus': 'unavailable'}
    results = []
    for source in cohort:
        item = deepcopy(source)
        item.update(returnRate=None, exitPrice=None, exitQuoteAsOf=None)
        entry = number(item.get('buyPrice'))
        target = sessions[item['market']]['evaluationQuoteAsOf']
        if item.get('returnStatus') == 'ambiguous_entry_price':
            pass
        elif entry is None or entry <= 0:
            item['returnStatus'] = 'missing_entry_price'
        elif item.get('priceBasis') != 'unadjusted_close':
            item['returnStatus'] = 'unverified_legacy_basis'
        elif target is None:
            item['returnStatus'] = 'calendar_unavailable'
        else:
            try:
                quote = get_exit_quote(item, target)
                exit_price = number(quote.get('price'))
                # A missing Friday bar in one ticker is not a market holiday.
                if quote.get('quoteAsOf') != target or exit_price is None or exit_price <= 0:
                    raise SourceDataError('missing_exact_session_close')
                if quote.get('hasSplit') is True:
                    item['returnStatus'] = 'corporate_action_requires_adjustment'
                elif quote.get('hasSplit') is not False:
                    item['returnStatus'] = 'corporate_action_status_unverified'
                else:
                    item.update(exitPrice=exit_price, currentPrice=price(exit_price, item['market']),
                                exitQuoteAsOf=target, returnRate=round((exit_price - entry) / entry * 100, 4),
                                returnStatus='observed', holdingCalendarDays=(date.fromisoformat(target) - date.fromisoformat(item['recommendDate'])).days)
            except Exception:
                item['returnStatus'] = 'price_fetch_failed'
        results.append(item)
    return results, sessions


def build_weekly_report(rows, period, generated_at, sessions, diagnostics):
    assert_publishable(period, generated_at)
    performance, rankings = {}, {}
    for market in TIMEZONES:
        all_rows = [row for row in rows if row['market'] == market]
        measured = [row for row in all_rows if row.get('returnStatus') == 'observed' and number(row.get('returnRate')) is not None]
        ordered = sorted(measured, key=lambda row: (-row['returnRate'], row['id']))[:10]
        performance[market] = {**sessions[market], 'totalCount': len(all_rows), 'observedCount': len(measured),
                               'missingCount': len(all_rows) - len(measured), 'complete': len(all_rows) == len(measured)}
        rankings[market] = [row['recommendationId'] for row in ordered]
    return {'schemaVersion': 3, 'reportKind': REPORT_KIND, 'reportDate': period['publicationDate'],
            'generatedAt': generated_at, 'timestamp': generated_at, 'period': period,
            'stocks': rows, 'performance': performance, 'winnerRankings': rankings,
            'sourceDiagnostics': diagnostics, 'market_status': {}, 'dominant_sectors': [],
            'methodology': '시장별 월~금 추천 중복 종목의 첫 추천 기록 가격 대비, 2주 뒤 금요일(휴장 시 직전 거래일) 종가 상승률 상위 10개. 전체 평균 수익률이 아닙니다. 가격 누락·분할 확인 필요 건은 미평가로 유지합니다. 배당·수수료·슬리피지·실제 체결 미반영.',
            'priceAdjustmentPolicy': 'recorded_unadjusted_close; split events remain unevaluated',
            'evaluationFrozen': True, 'legacyHistoryRecalculated': False}
