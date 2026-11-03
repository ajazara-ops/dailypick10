"""Daily publication protocol. No Git, HTTP, credentials, DB or live notifier.

The injected repository must hold the existing publisher lock, refresh main
after acquiring it, and atomically CAS a whole commit. The only supplied adapter
is synthetic (tests). No TTL permits stealing an unknown/running owner's claim.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from market_calendar import daily_session_facts
from scan_contract import number, score_inputs_available

STATE_PATH = 'scan_state.json'
LATEST_PATH = 'todays_recommendation.json'
TERMINAL_RUNS = frozenset({'success', 'failure', 'cancelled', 'timed_out', 'skipped'})


class CommitRejected(RuntimeError):
    """Confirmed no write. Do not blindly retry a stale expected head."""


class CommitUnknown(RuntimeError):
    """The commit may have reached main. Read back; never reissue it blindly."""


class InvalidOutput(ValueError):
    def __init__(self, code, status='error'):
        self.code, self.status = code, status


@dataclass(frozen=True)
class Snapshot:
    head: str
    files: dict


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def session_key(mode, market, session):
    if mode not in {'daily', 'weekly', 'weekly_summary'} or market not in {'KR', 'US', 'ALL'}:
        raise ValueError('invalid_key_scope')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', session):
        raise ValueError('invalid_session')
    datetime.strptime(session, '%Y-%m-%d')
    return f'{mode}:{market}:{session}'


def _json(snapshot, path, default):
    if path not in snapshot.files:
        return deepcopy(default)
    value = json.loads(snapshot.files[path])
    if not isinstance(value, dict):
        raise ValueError('invalid_repository_json')
    return value


def _ledger(snapshot):
    value = _json(snapshot, STATE_PATH, {'schemaVersion': 1, 'entries': {}})
    if value.get('schemaVersion') != 1 or not isinstance(value.get('entries'), dict):
        raise ValueError('invalid_state_schema')
    return value


def validate_daily(out, market, session, generated_at):
    if not isinstance(out, dict) or not isinstance(out.get('stocks'), list):
        raise InvalidOutput('invalid_output')
    meta = out.get('markets', {}).get(market, {})
    status = meta.get('status')
    if status in {'partial', 'error'}:
        raise InvalidOutput('incomplete_observation', status)
    if meta.get('stale') is not False:
        raise InvalidOutput('stale_output', 'stale')
    if status not in {'fresh', 'empty'}:
        raise InvalidOutput('invalid_status')
    counts = [meta.get(k) for k in ('attemptedCount', 'observedCount', 'errorCount', 'filteredCount')]
    if any(type(n) is not int or n < 0 for n in counts):
        raise InvalidOutput('invalid_counts')
    attempted, observed, errors, filtered = counts
    if attempted == 0 or observed != attempted or errors or meta.get('universeError'):
        raise InvalidOutput('incomplete_universe')
    if market == 'KR' and attempted != 100:
        raise InvalidOutput('incomplete_korean_universe')
    if meta.get('quoteAsOf') != session:
        raise InvalidOutput('wrong_quote_session', 'stale')
    if meta.get('generatedAt') != generated_at or out.get('generatedAt') != generated_at:
        raise InvalidOutput('wrong_generation_time')
    rows = [s for s in out['stocks'] if isinstance(s, dict) and s.get('market') == market]
    if len(rows) > 10 or (status == 'fresh') != bool(rows):
        raise InvalidOutput('invalid_recommendation_count')
    if status == 'empty' and filtered != observed:
        raise InvalidOutput('empty_without_complete_filtering')
    ids = []
    for rank, row in enumerate(rows, 1):
        if (not isinstance(row.get('id'), str) or not row['id'] or
                row.get('rank') != rank or row.get('quoteAsOf') != session or
                number(row.get('currentPrice')) is None or number(row['currentPrice']) <= 0 or
                number(row.get('score')) is None or number(row['score']) < 0 or
                not score_inputs_available(row)):
            raise InvalidOutput('invalid_recommendation')
        ids.append(row['id'])
    if len(set(ids)) != len(ids):
        raise InvalidOutput('duplicate_ticker')
    notice = out.get('notification')
    if not isinstance(notice, dict) or any(not isinstance(notice.get(k), str) for k in ('title', 'body')):
        raise InvalidOutput('invalid_notification')
    encoded(out)  # Also reject NaN/unserializable data before any publication.
    return status


def _failure_view(previous, market, out, stamp, status, code):
    """Keep last-good rows/quote dates; expose bounded failure diagnostics only."""
    view = deepcopy(previous)
    view.setdefault('stocks', [])
    view.setdefault('markets', {})
    view.setdefault('schemaVersion', 2)
    view.setdefault('market_status', {})
    view.setdefault('dominant_sectors', [])
    old = deepcopy(view['markets'].get(market, {}))
    incoming = out.get('markets', {}).get(market, {}) if isinstance(out, dict) else {}
    for key in ('attemptedCount', 'observedCount', 'filteredCount', 'errorCount'):
        n = incoming.get(key)
        if type(n) is int and n >= 0:
            old[key] = n
    old.update(status='partial' if status == 'partial' else 'error', stale=True,
               attemptedAt=stamp, publicationStatus='failed', publicationErrorCode=code,
               generatedAt=old.get('generatedAt'), quoteAsOf=old.get('quoteAsOf'))
    view['markets'][market] = old
    return view


class DailyPublisher:
    def __init__(self, repository, notifier, *, clock, calendar=daily_session_facts, defer_delivery=False):
        self.repository, self.notifier = repository, notifier
        self.clock, self.calendar = clock, calendar
        self.defer_delivery = defer_delivery

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('aware_server_clock_required')
        return now.astimezone(timezone.utc)

    def _commit(self, snapshot, changes):
        try:
            self.repository.commit(snapshot.head, changes)
        except CommitRejected:
            return None
        except CommitUnknown:
            pass  # Read-only confirmation below; no second write attempt.
        try:
            current = self.repository.refresh_main()
        except Exception:
            return None
        if all(current.files.get(p) == data for p, data in changes.items()):
            return current
        return None

    def _published_valid(self, snapshot, entry, market, session):
        expected_path = f'daily_data/{session}_{market}_daily.json'
        if entry.get('outputPath') != expected_path:
            return False
        data = snapshot.files.get(expected_path)
        if not isinstance(data, bytes) or digest(data) != entry.get('outputHash'):
            return False
        try:
            validate_daily(json.loads(data), market, session, entry['generatedAt'])
        except Exception:
            return False
        return True

    def run(self, market, run_id, collect, *, requested_session=None, mode='daily'):
        # Weekly is deliberately untouched and cannot consume a daily key/claim.
        if mode != 'daily' or market not in {'KR', 'US'} or not re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id)):
            raise ValueError('explicit_daily_market_and_run_id_required')
        run_id = str(run_id)
        with self.repository.publisher_lock(run_id):
            # NEVER use the checkout's pre-lock state, even with workflow concurrency.
            snapshot = self.repository.refresh_main()
            now = self._now()
            try:
                facts = self.calendar(market, now, requested_session)
                key = session_key('daily', market, facts['session'])
                ledger = _ledger(snapshot)
                previous = _json(snapshot, LATEST_PATH, {})
            except Exception:
                return {'state': 'blocked', 'reason': 'unverified_calendar_or_repository'}
            # An existing record cannot override authoritative close/session evidence.
            if facts.get('completed') is not True:
                return {'state': 'blocked', 'key': key, 'reason': 'session_not_completed'}
            entry = ledger['entries'].get(key)
            if entry:
                if not isinstance(entry, dict):
                    return {'state': 'blocked', 'key': key, 'reason': 'invalid_state_entry'}
                if entry.get('phase') == 'published':
                    valid = self._published_valid(snapshot, entry, market, facts['session'])
                    return {'state': 'noop' if valid else 'blocked', 'key': key,
                            'reason': 'already_published' if valid else 'published_hash_unverified',
                            'notification': entry.get('notification', {}).get('state', 'unconfirmed')}
                if entry.get('phase') not in {'collecting', 'failed'}:
                    return {'state': 'blocked', 'key': key, 'reason': 'unknown_phase'}
                owner = entry.get('runId')
                if self.repository.run_state(owner) not in TERMINAL_RUNS:
                    return {'state': 'blocked', 'key': key, 'reason': 'owner_active_or_unknown'}
                if not self.repository.retry_authorized(key, run_id):
                    return {'state': 'blocked', 'key': key, 'reason': 'explicit_retry_approval_required'}
            path = f"daily_data/{facts['session']}_{market}_daily.json"
            old_meta = previous.get('markets', {}).get(market, {})
            if not entry and (path in snapshot.files or
                              (old_meta.get('status') in {'fresh', 'empty'} and
                               old_meta.get('stale') is False and old_meta.get('quoteAsOf') == facts['session'])):
                return {'state': 'blocked', 'key': key, 'reason': 'legacy_publication_requires_verified_adoption'}
            if not facts['localIsSession'] and requested_session is None:
                return {'state': 'holiday', 'key': key, 'reason': 'no_session_today'}
            # Current financials/news cannot be backdated, even before today's open.
            if facts['session'] != facts['localDate'] or facts['session'] != facts['latestCompleted']:
                return {'state': 'blocked', 'key': key, 'reason': 'historical_current_inputs_forbidden'}
            stamp = now.isoformat(timespec='seconds')
            entry = {'phase': 'collecting', 'runId': run_id, 'session': facts['session'],
                     'market': market, 'mode': 'daily', 'attemptedAt': stamp,
                     'dataStatus': 'pending', 'notification': {'state': 'not_ready'}}
            ledger['entries'][key] = entry
            claimed = self._commit(snapshot, {STATE_PATH: encoded(ledger)})
            if claimed is None:
                return {'state': 'blocked', 'key': key, 'reason': 'collection_claim_unconfirmed'}
            snapshot = claimed
            out = None
            try:
                out = collect(market, facts['session'], deepcopy(previous), stamp)
                after = self.calendar(market, self._now(), facts['session'])
                if after['localDate'] != facts['localDate'] or after['latestCompleted'] != facts['session']:
                    raise InvalidOutput('collection_crossed_session_boundary', 'stale')
                status = validate_daily(out, market, facts['session'], stamp)
                other = 'US' if market == 'KR' else 'KR'
                if ([s for s in out['stocks'] if s.get('market') == other] !=
                        [s for s in previous.get('stocks', []) if s.get('market') == other]):
                    raise InvalidOutput('other_market_changed')
            except Exception as exc:
                code = exc.code if isinstance(exc, InvalidOutput) else 'collection_or_validation_failed'
                status = exc.status if isinstance(exc, InvalidOutput) else 'error'
                entry.update(phase='failed', dataStatus=status, errorCode=code)
                changes = {STATE_PATH: encoded(ledger), LATEST_PATH: encoded(_failure_view(previous, market, out, stamp, status, code))}
                saved = self._commit(snapshot, changes)
                return {'state': 'failed' if saved else 'unconfirmed', 'key': key, 'reason': code, 'dataStatus': status}
            body = encoded(out)
            notice_key = 'daily-' + digest(key.encode('utf-8'))
            entry.update(phase='published', dataStatus=status, outputPath=path,
                         outputHash=digest(body), generatedAt=stamp,
                         notification={'state': 'ready', 'key': notice_key})
            if self.defer_delivery:
                entry['noticePayload'] = deepcopy(out['notification'])
            published = self._commit(snapshot, {path: body, LATEST_PATH: body, STATE_PATH: encoded(ledger)})
            if published is None:
                return {'state': 'unconfirmed', 'key': key, 'reason': 'publication_unconfirmed'}
            if self.defer_delivery:
                return {'state': 'published', 'key': key, 'dataStatus': status,
                        'notification': 'awaiting_pages'}
            # Claim is durable and confirmed BEFORE a notification callback is allowed.
            entry['notification'].update(state='claimed', runId=run_id)
            notification_claim = self._commit(published, {STATE_PATH: encoded(ledger)})
            if notification_claim is None:
                return {'state': 'published', 'key': key, 'notification': 'claim_unconfirmed'}
            try:
                ack = self.notifier.send(notice_key, out['notification']['title'], out['notification']['body'])
                ack = ack if ack in {'accepted', 'rejected', 'no_recipients'} else 'unknown'
            except Exception:
                ack = 'unknown'
            # BaseException/crash deliberately leaves claimed. A later run is a no-op.
            entry['notification']['state'] = ack
            saved = self._commit(notification_claim, {STATE_PATH: encoded(ledger)})
            return {'state': 'published', 'key': key, 'dataStatus': status,
                    'outputHash': digest(body), 'notification': ack if saved else 'unconfirmed'}
