"""Bounded LOCAL/SYNTHETIC intent coordinator; no SDK, credential, CLI or schedule.

Queue port: pending_page(limit=..., after=...) -> (UID list, next cursor).
The trusted query must include cancel_requested, due grace_period, processing and
retry_wait intents. Processing intents stay discoverable after a job completes,
so a failed reflection is retried without requiring the now-deleted Auth user.
spark_runtime persists bounded query position in the same guarded demo store.
Production SDK binding and scheduling are intentionally not implemented.
"""
from account_deletion import (Disabled, InvalidRequest, PermissionFailure,
                              Unauthorized, IdentityChanged, CancellationClosed,
                              TemporaryFailure, validate_proof)
from deletion_adapters import synthetic
from spark_intake import deadline_parts


ACTIVE = frozenset({'grace_period', 'cancel_requested', 'processing', 'retry_wait'})


def run_intent_batch(queue, bridge, worker, *, enabled=False, mode='inspect',
                     limit=25, after=None, before_item=None):
    """Inspect by default; writes/deletions require explicit synthetic process mode.

    This is not a client endpoint. IDs come only from a guarded query port, never
    from an HTTP body. No raw exceptions or per-user details leave the boundary.
    """
    result = dict(enabled=enabled, mode=mode, examined=0, cancelled=0, processed=0,
                  completed=0, reflected=0, waiting=0, failed=0, errors={},
                  statuses={}, nextCursor=None)
    if enabled is not True:
        return result
    if mode not in {'inspect', 'confirm_cancellations', 'process'}:
        raise InvalidRequest('Unknown batch mode')
    if type(limit) is not int or not 1 <= limit <= 25:
        raise InvalidRequest('Batch limit must be 1..25')
    synthetic(queue)
    synthetic(bridge)
    worker._guard()
    if (not bridge.enabled or queue.documents is not bridge.documents or
            getattr(worker.store, 'documents', None) is not bridge.documents or
            getattr(worker.profiles, 'documents', None) is not bridge.documents):
        raise Disabled('One guarded synthetic database is required')
    uids, cursor = queue.pending_page(limit=limit, after=after)
    if (not isinstance(uids, list) or len(uids) > limit or
            any(not isinstance(uid, str) or not uid or '/' in uid or len(uid) > 128 for uid in uids) or
            (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256))):
        raise InvalidRequest('Invalid bounded query result')
    result['nextCursor'] = cursor
    # Read the bounded page once and handle explicit cancellations before due jobs.
    rows = []
    for uid in dict.fromkeys(uids):
        try:
            rows.append((uid, bridge.documents.read('accountDeletionIntents/' + uid), None))
        except Exception as error:
            rows.append((uid, None, error))
    rows.sort(key=lambda pair: pair[1].get('status') != 'cancel_requested' if isinstance(pair[1], dict) else True)
    for uid, intent, read_error in rows:
        if before_item is not None:
            before_item()  # Trusted runner lease check; fail the page before more effects.
        result['examined'] += 1
        try:
            if read_error is not None:
                raise read_error
            if intent is None:
                continue  # Gone/changed after the query. Never create a request.
            if not isinstance(intent, dict) or intent.get('version') != 2:
                raise InvalidRequest('Unverified intent')
            request_id = intent.get('requestId')
            validate_proof(request_id, '0' * 64)  # Reuse UUID validation; no proof is issued.
            state = intent.get('status')
            if state not in ACTIVE | {'cancelled', 'completed', 'needs_review'}:
                raise InvalidRequest('Unknown intent state')
            result['statuses'][state] = result['statuses'].get(state, 0) + 1
            if mode == 'inspect':
                continue
            if state == 'cancel_requested':
                confirmed = bridge.confirm_cancellation(uid, worker.clock)
                result['cancelled'] += bool(confirmed and confirmed['status'] == 'cancelled')
                continue
            if mode == 'confirm_cancellations' or state not in ACTIVE:
                continue
            # Look for an existing job BEFORE looking up Auth. After Auth deletion
            # lookup may fail; durable completion/profile retry must still progress.
            job = worker.store.get(request_id)
            if job is None:
                if state != 'grace_period':
                    raise PermissionFailure('Missing durable job; manual review required')
                if worker.clock() < deadline_parts(intent['requestedAt'])[1]:
                    result['waiting'] += 1
                    continue
                job = bridge.materialize_due(uid, worker.clock)
            if job is None:
                result['waiting'] += 1  # Cancellation may have won the transaction.
                continue
            if job.get('id') != request_id or job.get('uid') != uid:
                raise PermissionFailure('Job identity mismatch')
            if job['status'] not in {'completed', 'needs_review', 'cancelled'}:
                processed = worker.run_once(request_id)
                result['processed'] += processed is not None
                if processed is None:
                    result['waiting'] += 1
            # Always reread inside reflect; never publish a stale worker return.
            reflected = bridge.reflect(request_id)
            result['reflected'] += reflected is not None
            result['completed'] += bool(reflected and reflected.get('status') == 'completed'
                                        and reflected.get('phase') == 'complete')
        except Exception as error:
            category = ('authentication' if isinstance(error, Unauthorized) else
                        'identity_changed' if isinstance(error, IdentityChanged) else
                        'transition_closed' if isinstance(error, CancellationClosed) else
                        'manual_review' if isinstance(error, PermissionFailure) else
                        'invalid_intent' if isinstance(error, (InvalidRequest, KeyError, TypeError, ValueError)) else
                        'temporary' if isinstance(error, TemporaryFailure) else 'unconfirmed')
            result['failed'] += 1
            result['errors'][category] = result['errors'].get(category, 0) + 1
    return result
