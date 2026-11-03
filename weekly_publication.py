"""Weekly frozen-report publication. Injected ports only; no import-time I/O.

Both weekly CLI aliases share a settlement-period key. Daily keys and latest
recommendations are never modified. No automatic retry of failed/unknown claims.
"""
from copy import deepcopy
from datetime import date
import json
import re

from scan_contract import number
from scan_publication import (DailyPublisher, InvalidOutput, STATE_PATH, TERMINAL_RUNS,
                              _ledger, encoded, digest)
from weekly_contract import (REPORT_KIND, latest_publication, weekly_period,
                             assert_publishable, validate_frozen_report)

INDEX_PATH = 'history_index.json'
MISSING = frozenset({'ambiguous_entry_price', 'missing_entry_price', 'unverified_legacy_basis',
                     'corporate_action_requires_adjustment', 'corporate_action_status_unverified'})
TRANSIENT = frozenset({'price_fetch_failed', 'calendar_unavailable'})


def weekly_key(period):
    return f"weekly:ALL:{period['weekStart']}:{period['weekEnd']}:{period['evaluationDate']}"


def index_rows(snapshot):
    rows = json.loads(snapshot.files[INDEX_PATH]) if INDEX_PATH in snapshot.files else []
    if not isinstance(rows, list):
        raise ValueError('invalid_history_index')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('date'), str) or
                row['date'] in seen or not isinstance(row.get('file'), str) or
                not re.fullmatch(r'weekly_reports/\d{4}-\d{2}-\d{2}(?:_v[23])?_weekly\.json', row['file']) or
                not row['file'].startswith('weekly_reports/' + row['date'] + '_')):
            raise ValueError('invalid_history_index_entry')
        date.fromisoformat(row['date'])
        seen.add(row['date'])
    return rows


def validate_weekly(report, period, stamp):
    validate_frozen_report(report, period)
    if report.get('generatedAt') != stamp or report.get('timestamp') != stamp:
        raise InvalidOutput('wrong_generation_time')
    encoded(report)
    diagnostics = report.get('sourceDiagnostics')
    if not isinstance(diagnostics, dict) or any(type(v) is not int or v < 0 for v in diagnostics.values()):
        raise InvalidOutput('invalid_source_diagnostics')
    if diagnostics.get('unreadableSnapshotCount', 0):
        raise InvalidOutput('unreadable_source_history', 'partial')
    # Do not freeze an unverified empty universe as a successful weekly result.
    if not report['stocks']:
        raise InvalidOutput('empty_cohort_unverified', 'empty')
    partial = any(diagnostics.get(k, 0) for k in ('unknownSessionRecordCount', 'invalidIdentityCount',
                   'ambiguousEntryCount', 'lateRecordCount', 'nonTradingSessionRecordCount'))
    ids = set()
    for market in ('KR', 'US'):
        meta = report['performance'][market]
        target = meta.get('evaluationQuoteAsOf')
        if (meta.get('calendarStatus') != 'resolved' or not isinstance(target, str) or
                not period['weekEnd'] < target <= period['evaluationDate']):
            raise InvalidOutput('calendar_unavailable', 'partial')
        date.fromisoformat(target)
        partial = partial or meta['missingCount'] > 0 or meta['totalCount'] == 0
    for row in report['stocks']:
        identity = (row['market'], row['id'])
        if identity in ids:
            raise InvalidOutput('duplicate_weekly_ticker')
        ids.add(identity)
        status = row.get('returnStatus')
        if status in TRANSIENT:
            raise InvalidOutput('weekly_quote_or_calendar_failed', 'partial')
        if status == 'observed':
            buy, exit_price, change = (number(row.get(k)) for k in ('buyPrice', 'exitPrice', 'returnRate'))
            target = report['performance'][row['market']]['evaluationQuoteAsOf']
            if (buy is None or buy <= 0 or exit_price is None or exit_price <= 0 or change is None or
                    row.get('exitQuoteAsOf') != target or row.get('priceBasis') != 'unadjusted_close' or
                    abs(change - round((exit_price - buy) / buy * 100, 4)) > .000001):
                raise InvalidOutput('invalid_weekly_measurement')
        elif status not in MISSING or row.get('returnRate') is not None or row.get('exitPrice') is not None:
            raise InvalidOutput('invalid_missing_measurement')
    return 'partial' if partial else 'fresh'


class WeeklyPublisher(DailyPublisher):
    """Reuses confirmed CAS and clock, not daily calendar/collection semantics."""
    def _valid(self, snapshot, entry, period, rows, path):
        data = snapshot.files.get(path)
        if entry.get('outputPath') != path or not isinstance(data, bytes) or digest(data) != entry.get('outputHash'):
            return False
        try:
            status = validate_weekly(json.loads(data), period, entry['generatedAt'])
            expected = {'date': period['publicationDate'], 'file': path,
                        'publishAt': period['publishAt'], 'reportKind': REPORT_KIND}
            return status == entry.get('dataStatus') and expected == entry.get('indexEntry') and expected in rows
        except Exception:
            return False

    def run(self, run_id, collect, *, publication_date=None):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id)):
            raise ValueError('explicit_weekly_run_id_required')
        run_id = str(run_id)
        with self.repository.publisher_lock(run_id):
            snapshot = self.repository.refresh_main()
            now = self._now()
            stamp = now.isoformat(timespec='seconds')
            try:
                period = weekly_period(publication_date or latest_publication(stamp))
                assert_publishable(period, stamp)
                ledger, rows = _ledger(snapshot), index_rows(snapshot)
            except Exception:
                return {'state': 'blocked', 'reason': 'unverified_weekly_period_or_repository'}
            key = weekly_key(period)
            path = f"weekly_reports/{period['publicationDate']}_v3_weekly.json"
            entry = ledger['entries'].get(key)
            if entry:
                if not isinstance(entry, dict):
                    return {'state': 'blocked', 'key': key, 'reason': 'invalid_state_entry'}
                if entry.get('phase') == 'published':
                    valid = self._valid(snapshot, entry, period, rows, path)
                    return {'state': 'noop' if valid else 'blocked', 'key': key,
                            'reason': 'already_published' if valid else 'published_hash_or_index_unverified',
                            'notification': entry.get('notification', {}).get('state', 'unconfirmed')}
                if entry.get('phase') not in {'collecting', 'failed'}:
                    return {'state': 'blocked', 'key': key, 'reason': 'unknown_phase'}
                if self.repository.run_state(entry.get('runId')) not in TERMINAL_RUNS:
                    return {'state': 'blocked', 'key': key, 'reason': 'owner_active_or_unknown'}
                if not self.repository.retry_authorized(key, run_id):
                    return {'state': 'blocked', 'key': key, 'reason': 'explicit_retry_approval_required'}
            # Never silently replace/adopt historical publication or notify it again.
            if any(p.startswith(f"weekly_reports/{period['publicationDate']}_") for p in snapshot.files) or any(
                    r['date'] == period['publicationDate'] for r in rows):
                return {'state': 'blocked', 'key': key, 'reason': 'legacy_publication_requires_verified_adoption'}
            entry = {'phase': 'collecting', 'runId': run_id, 'mode': 'weekly', 'market': 'ALL',
                     'period': period, 'attemptedAt': stamp, 'dataStatus': 'pending',
                     'notification': {'state': 'not_ready'}}
            ledger['entries'][key] = entry
            claimed = self._commit(snapshot, {STATE_PATH: encoded(ledger)})
            if claimed is None:
                return {'state': 'blocked', 'key': key, 'reason': 'collection_claim_unconfirmed'}
            try:
                report = collect(deepcopy(period), claimed, stamp)
                status = validate_weekly(report, period, stamp)
            except Exception as exc:
                code = exc.code if isinstance(exc, InvalidOutput) else 'weekly_collection_or_validation_failed'
                status = exc.status if isinstance(exc, InvalidOutput) else 'error'
                entry.update(phase='failed', dataStatus=status, errorCode=code)
                saved = self._commit(claimed, {STATE_PATH: encoded(ledger)})
                return {'state': 'failed' if saved else 'unconfirmed', 'key': key, 'reason': code, 'dataStatus': status}
            body = encoded(report)
            index_entry = {'date': period['publicationDate'], 'file': path,
                           'publishAt': period['publishAt'], 'reportKind': REPORT_KIND}
            index = sorted(rows + [index_entry], key=lambda r: r['date'], reverse=True)
            notice_key = 'weekly-' + digest(key.encode('utf-8'))
            entry.update(phase='published', dataStatus=status, outputPath=path, outputHash=digest(body),
                         generatedAt=stamp, indexEntry=index_entry,
                         counts={m: {k: report['performance'][m][k] for k in ('totalCount', 'observedCount', 'missingCount')}
                                 for m in ('KR', 'US')}, notification={'state': 'ready', 'key': notice_key})
            title = '주간 상승률 TOP 10'
            message = '한 주 추천을 중복 통합해 공통 2주 뒤 금요일 종가로 비교했습니다. 시장별 상승률 상위 10개와 전체 평가 범위를 확인하세요.'
            if status == 'partial':
                message = '일부 기록이 미평가이거나 확인이 필요합니다. 주간 상승률 TOP 10의 시장별 평가 수·미평가 수와 기간을 확인하세요.'
            if self.defer_delivery:
                entry['noticePayload'] = {'title': title, 'body': message}
            published = self._commit(claimed, {path: body, INDEX_PATH: encoded(index), STATE_PATH: encoded(ledger)})
            if published is None:
                return {'state': 'unconfirmed', 'key': key, 'reason': 'publication_unconfirmed'}
            if self.defer_delivery:
                return {'state': 'published', 'key': key, 'dataStatus': status, 'notification': 'awaiting_pages'}
            entry['notification'].update(state='claimed', runId=run_id)
            notification_claim = self._commit(published, {STATE_PATH: encoded(ledger)})
            if notification_claim is None:
                return {'state': 'published', 'key': key, 'dataStatus': status, 'notification': 'claim_unconfirmed'}
            try:
                ack = self.notifier.send(notice_key, title, message)
                ack = ack if ack in {'accepted', 'rejected', 'no_recipients'} else 'unknown'
            except Exception:
                ack = 'unknown'
            entry['notification']['state'] = ack
            saved = self._commit(notification_claim, {STATE_PATH: encoded(ledger)})
            return {'state': 'published', 'key': key, 'dataStatus': status,
                    'outputHash': digest(body), 'notification': ack if saved else 'unconfirmed'}
