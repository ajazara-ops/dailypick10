"""Disabled-by-default, injected SYNTHETIC batch runtime and durable page cursor.

No SDK initialization, credentials, environment toggle, HTTP or schedule.
The CLI has no runtime factory: without an injected synthetic runtime it is OFF.
The document port supplies read/transaction/pending_intents; its ONLY SDK binding
is EmulatorDocuments, which enforces the existing demo/loopback guard.
"""
import secrets

from account_deletion import Disabled, InvalidRequest, LeaseLost
from deletion_adapters import synthetic
from spark_batch import ACTIVE, run_intent_batch


LEASE_SECONDS = 120


def validate_cursor(after):
    if after is not None and (not isinstance(after, str) or not after or
                              '/' in after or len(after) > 128 or after in {'.', '..'}):
        raise InvalidRequest('Invalid batch cursor')


def seconds(clock):
    value = clock()
    if type(value) is not int or value < 0:
        raise InvalidRequest('Trusted whole-second clock required')
    return value


class FirestoreIntentQueue:
    """Bounded, stable document-ID pagination; active intents are never deleted.

    Include processing/retry_wait, even when Auth or the durable job has already
    completed: reflection may still need repair. A full page returns its last ID
    without an extra lookahead read; an empty final page wraps the next run.
    """
    synthetic_only = True

    def __init__(self, documents):
        synthetic(documents)
        self.documents = documents

    def pending_page(self, *, limit, after):
        if type(limit) is not int or not 1 <= limit <= 25:
            raise InvalidRequest('Batch limit must be 1..25')
        validate_cursor(after)
        rows = self.documents.pending_intents(statuses=tuple(sorted(ACTIVE)), limit=limit, after=after)
        if not isinstance(rows, list) or len(rows) > limit:
            raise InvalidRequest('Invalid bounded query result')
        for uid in rows:
            validate_cursor(uid)
            if uid is None or (after is not None and uid <= after):
                raise InvalidRequest('Non-advancing query result')
        if rows != sorted(set(rows)):
            raise InvalidRequest('Unstable query order')
        return rows, rows[-1] if len(rows) == limit else None


class FirestoreBatchCursor:
    """Server-owned cursor/lease separate from users and deletion-intent records.

    The lease fences cursor commits, not the Auth API. Existing per-job leases and
    transaction-based consent checks remain authoritative for lifecycle effects.
    """
    synthetic_only = True

    def __init__(self, documents, mode):
        synthetic(documents)
        if mode not in {'process', 'confirm_cancellations'}:
            raise InvalidRequest('Invalid cursor mode')
        self.documents = documents
        self.path = 'accountDeletionBatchState/spark-' + mode + '-v1'

    @staticmethod
    def validate(row):
        if row is None:
            return dict(version=1, after=None, cycle=0, revision=0, lease=None, leaseUntil=0)
        if not isinstance(row, dict) or set(row) != {'version', 'after', 'cycle', 'revision', 'lease', 'leaseUntil'}:
            raise InvalidRequest('Invalid durable cursor state')
        if type(row['version']) is not int or row['version'] != 1:
            raise InvalidRequest('Unsupported cursor version')
        validate_cursor(row['after'])
        if any(type(row[k]) is not int or row[k] < 0 for k in ('cycle', 'revision', 'leaseUntil')):
            raise InvalidRequest('Invalid cursor counters')
        lease = row['lease']
        if lease is not None and (not isinstance(lease, str) or len(lease) != 32 or any(c not in '0123456789abcdef' for c in lease)):
            raise InvalidRequest('Invalid cursor lease')
        if (lease is None) != (row['leaseUntil'] == 0):
            raise InvalidRequest('Inconsistent cursor lease')
        return dict(row)

    def read(self):
        return self.validate(self.documents.read(self.path))

    def claim(self, clock):
        nonce = secrets.token_hex(16)
        def transact(tx):
            row = self.validate(tx.get(self.path)); now = seconds(clock)
            if row['leaseUntil'] > now:
                return None
            row.update(lease=nonce, leaseUntil=now + LEASE_SECONDS)
            tx.set(self.path, row)
            return row
        return self.documents.transaction(transact)

    @staticmethod
    def owned(row, claim, now):
        if (row['lease'] != claim['lease'] or row['revision'] != claim['revision'] or
                row['leaseUntil'] <= now):
            raise LeaseLost('Batch page lease expired or replaced')

    def assert_owned(self, claim, clock):
        self.owned(self.read(), claim, seconds(clock))

    def finish(self, claim, after, clock):
        validate_cursor(after)
        def transact(tx):
            row = self.validate(tx.get(self.path))
            self.owned(row, claim, seconds(clock))
            row.update(after=after, revision=row['revision'] + 1,
                       cycle=row['cycle'] + (after is None), lease=None, leaseUntil=0)
            tx.set(self.path, row)
            return row
        return self.documents.transaction(transact)


class SparkBatchRuntime:
    """One bounded page, never a loop or scheduler. Default does no reads/writes.

    Partial row failures still advance a confirmed page; they are rediscovered
    after wrap. Query/lease/commit uncertainty leaves the run unconfirmed. A new
    invocation reads authoritative cursor state rather than assuming rollback.
    """
    def __init__(self, documents, bridge, worker, *, enabled=False):
        synthetic(documents)
        self.documents, self.bridge, self.worker = documents, bridge, worker
        self.enabled = enabled
        self.queue = FirestoreIntentQueue(documents)

    def run_page(self, *, mode='inspect', limit=25):
        if self.enabled is not True:
            return {'state': 'disabled', 'enabled': False, 'examined': 0}
        if mode not in {'inspect', 'process', 'confirm_cancellations'}:
            raise InvalidRequest('Unknown batch mode')
        if type(limit) is not int or not 1 <= limit <= 25:
            raise InvalidRequest('Batch limit must be 1..25')
        synthetic(self.bridge); self.worker._guard()
        if (self.bridge.enabled is not True or self.bridge.documents is not self.documents or
                getattr(self.worker.store, 'documents', None) is not self.documents or
                getattr(self.worker.profiles, 'documents', None) is not self.documents):
            raise Disabled('One guarded synthetic database is required')
        cursor = FirestoreBatchCursor(self.documents, 'process' if mode == 'inspect' else mode)
        claim = None
        try:
            claim = cursor.read() if mode == 'inspect' else cursor.claim(self.worker.clock)
            if claim is None:
                return {'state': 'busy', 'enabled': True, 'examined': 0}
            result = run_intent_batch(self.queue, self.bridge, self.worker, enabled=True,
                mode=mode, limit=limit, after=claim['after'],
                before_item=None if mode == 'inspect' else lambda: cursor.assert_owned(claim, self.worker.clock))
            after = result.pop('nextCursor')  # Raw UIDs/cursors never leave the aggregate result.
            if mode == 'inspect':
                return dict(result, state='inspected', pageCommitted=False)
            saved = cursor.finish(claim, after, self.worker.clock)
            return dict(result, state='processed', pageCommitted=True, cycle=saved['cycle'])
        except Exception:
            # No raw exception, credential, UID or false completion claim. A commit
            # may have succeeded before its response was lost: do NOT reset state.
            return {'state': 'unconfirmed', 'enabled': True, 'pageCommitted': None,
                    'effectsMayHaveOccurred': mode != 'inspect'}


def build_runtime_from_worker(worker, *, enabled=False):
    """Compose existing guarded ports without initializing an SDK or a live CLI.

    No credential/environment lookup or operation occurs during construction.
    Production mode and non-demo/non-synthetic SDK bindings stay forbidden.
    """
    from account_deletion import DeletionService
    from deletion_adapters import FirebaseAuthAdapter
    from spark_intake import SparkIntentBridge
    if (not isinstance(worker,DeletionService) or worker.mode not in {'mock','emulator'}
            or not isinstance(worker.auth,FirebaseAuthAdapter)):
        raise Disabled('Explicit guarded worker and Auth adapter required')
    documents=getattr(worker.store,'documents',None)
    synthetic(documents);synthetic(worker.auth);synthetic(worker.profiles)
    if getattr(worker.profiles,'documents',None) is not documents:
        raise Disabled('One guarded database is required')
    bridge=SparkIntentBridge(documents,worker.auth.intake_identity,enabled=enabled is True)
    return SparkBatchRuntime(documents,bridge,worker,enabled=enabled is True)


def main(argv=None, *, runtime=None):
    """Bounded injected CLI boundary, not an operational deletion launcher.

    There is deliberately no --enable, credential, UID, date, environment gate,
    live adapter or factory. Running this file alone cannot touch any account.
    """
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Disabled-by-default synthetic deletion batch boundary')
    parser.add_argument('--mode', choices=['inspect', 'confirm_cancellations', 'process'], default='inspect')
    parser.add_argument('--limit', type=int, choices=range(1, 26), default=25)
    args = parser.parse_args(argv)
    try:
        if runtime is None:
            result = {'state': 'disabled', 'enabled': False, 'examined': 0, 'reason': 'no_runtime_binding'}
        elif type(runtime) is not SparkBatchRuntime:
            raise Disabled('Explicit synthetic runtime required')
        else:
            result = runtime.run_page(mode=args.mode, limit=args.limit)
    except Exception:
        result = {'state': 'unconfirmed', 'enabled': False, 'reason': 'runtime_unavailable'}
    print(json.dumps(result, sort_keys=True))  # Aggregate counters only; no UID/credential/exception.
    if result['state'] == 'disabled':
        return 2
    if result['state'] == 'busy':
        return 3
    return 0 if result['state'] in {'inspected', 'processed'} and not result.get('failed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
