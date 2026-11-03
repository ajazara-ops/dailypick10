"""Disabled-by-default deletion contract. No Firebase/network/credential adapter.

SQLite is a LOCAL SYNTHETIC reference job store, not the deployed Firestore store.
Jobs/tombstones survive worker restarts and are never stored inside users/{uid}.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from notification_policy import push_eligible


class Disabled(RuntimeError): pass
class InvalidRequest(ValueError): pass
class Unauthorized(RuntimeError): pass
class TemporaryFailure(RuntimeError): pass
class PermissionFailure(RuntimeError): pass
class IdentityChanged(RuntimeError): pass
class LeaseLost(RuntimeError): pass
class CancellationClosed(RuntimeError): pass

GRACE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Principal:
    uid: str
    auth_time: int
    expires_at: int
    created_at: int


def validate_body(body):
    if not isinstance(body, dict) or set(body) != {'version', 'requestId', 'recoveryKey', 'confirmation'}:
        raise InvalidRequest('Unexpected request fields')
    if type(body['version']) is not int or body['version'] != 1 or body['confirmation'] != 'DELETE_MY_ACCOUNT':
        raise InvalidRequest('Explicit confirmation required')
    validate_proof(body['requestId'], body['recoveryKey'])


def validate_proof(request_id, key):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', request_id):
        raise InvalidRequest('Invalid request identifier')
    if uuid.UUID(request_id).version != 4 or not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        raise InvalidRequest('Invalid recovery proof')


class LocalJobStore:
    synthetic_only = True

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, uid TEXT NOT NULL, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tombstones(uid TEXT PRIMARY KEY, job_id TEXT NOT NULL, blocked INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, proof TEXT NOT NULL)')

    def close(self): self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, job_id):
        row = self.db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def current(self, uid):
        row = self.db.execute('SELECT job_id FROM tombstones WHERE uid=?', (uid,)).fetchone()
        return self.get(row[0]) if row else None

    def _put(self, job):
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?)', (job['id'], job['uid'], json.dumps(job)))

    def blocked(self, uid):
        row = self.db.execute('SELECT blocked FROM tombstones WHERE uid=?', (uid,)).fetchone()
        return bool(row and row[0])

    def due_ids(self, now, limit):
        rows=self.db.execute("SELECT id FROM jobs WHERE json_extract(data,'$.status') NOT IN ('completed','needs_review','cancelled') AND json_extract(data,'$.deleteAfter')<=? AND json_extract(data,'$.nextRun')<=? AND json_extract(data,'$.leaseUntil')<=? ORDER BY json_extract(data,'$.nextRun'),id LIMIT ?",(now,now,now,limit)).fetchall()
        return [row[0] for row in rows]

    def accept(self, principal, request_id, key, now):
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self.transaction():
            now = now() if callable(now) else now
            old = self.db.execute('SELECT job_id,proof FROM receipts WHERE id=?', (request_id,)).fetchone()
            if old:
                job = self.get(old[0])
                if job['uid'] != principal.uid or not hmac.compare_digest(old[1], digest):
                    raise Unauthorized('Request identifier collision')
                if job['authCreatedAt'] != principal.created_at:
                    raise IdentityChanged('UID was reused')
                # Replaying an old cancelled request MUST NOT create another request.
                return job
            job = self.current(principal.uid)
            if job and job['authCreatedAt'] != principal.created_at:
                raise IdentityChanged('UID was reused')
            if not job or job['status'] == 'cancelled':
                job = dict(id=request_id, uid=principal.uid, authCreatedAt=principal.created_at, status='grace_period', phase='quiesce',
                           attempts=0, nextRun=now+GRACE_SECONDS, createdAt=now, deleteAfter=now+GRACE_SECONDS,
                           consentAuthTime=principal.auth_time, startedAt=None, lease=None, leaseUntil=0, error=None)
                self._put(job)
                self.db.execute('INSERT OR REPLACE INTO tombstones VALUES(?,?,1)', (principal.uid, job['id']))
            count = self.db.execute('SELECT count(*) FROM receipts WHERE job_id=?', (job['id'],)).fetchone()[0]
            if count >= 8: raise InvalidRequest('Recovery receipt limit reached')
            self.db.execute('INSERT INTO receipts VALUES(?,?,?)', (request_id, job['id'], digest))
        return job

    def lookup(self, request_id, key):
        validate_proof(request_id, key)
        row = self.db.execute('SELECT job_id,proof FROM receipts WHERE id=?', (request_id,)).fetchone()
        if not row: return None
        if not hmac.compare_digest(row[1], hashlib.sha256(key.encode()).hexdigest()):
            raise Unauthorized('Invalid recovery proof')
        return self.get(row[0])

    def cancel(self, principal, request_id, now):
        with self.transaction():
            now = now() if callable(now) else now
            row = self.db.execute('SELECT job_id FROM receipts WHERE id=?', (request_id,)).fetchone()
            job = self.get(row[0]) if row else self.get(request_id)
            if not job or job['uid'] != principal.uid: raise Unauthorized('Not your request')
            if job['authCreatedAt'] != principal.created_at: raise IdentityChanged('UID was reused')
            if job['status'] == 'cancelled': return job
            if now >= job['deleteAfter'] or job['startedAt'] is not None or job['status'] != 'grace_period':
                raise CancellationClosed('Grace period ended or deletion already started')
            job.update(status='cancelled', error=None)
            self._put(job)
            self.db.execute('UPDATE tombstones SET blocked=0 WHERE uid=? AND job_id=?', (principal.uid, job['id']))
            return job

    def claim(self, job_id, now):
        with self.transaction():
            now = now() if callable(now) else now
            job = self.get(job_id)
            if not job or job['status'] in {'completed', 'needs_review', 'cancelled'} or now < job['deleteAfter'] or job['nextRun'] > now or job['leaseUntil'] > now:
                return None
            job.update(status='processing', startedAt=job['startedAt'] if job['startedAt'] is not None else now,
                       attempts=job['attempts']+1, lease=secrets.token_hex(16), leaseUntil=now+60)
            self._put(job)
            return job

    def checkpoint(self, job, now, **updates):
        with self.transaction():
            now = now() if callable(now) else now
            current = self.get(job['id'])
            if not current or current['lease'] != job['lease'] or current['leaseUntil'] <= now:
                raise LeaseLost('Worker lease expired')
            current.update(updates)
            self._put(current)
            job.update(current)


def receipt(job, request_id, now):
    status = 'queued' if job['status'] == 'grace_period' and now >= job['deleteAfter'] else job['status']
    return dict(version=1, requestId=request_id, status=status, phase=job['phase'],
                requestedAt=job['createdAt'], deleteAfter=job['deleteAfter'], serverNow=now,
                cancelAllowed=job['status'] == 'grace_period' and job['startedAt'] is None and now < job['deleteAfter'],
                completedScope=['firebase-auth', 'users-document'] if job['status'] == 'completed' else [],
                subscriptionCancelled=False)


class DeletionService:
    def __init__(self, store, auth, profiles, clock, *, mode='disabled'):
        self.store, self.auth, self.profiles, self.clock, self.mode = store, auth, profiles, clock, mode

    def _guard(self):
        if self.mode != 'mock' or not all(getattr(x, 'synthetic_only', False) for x in (self.store, self.auth, self.profiles)):
            raise Disabled('Only local synthetic adapters are available; production deletion is disabled')

    def _principal(self, token, recent=True):
        # verify() is the TRUSTED adapter boundary: signature/project/issuer/revocation
        # and user existence must be checked by a future production implementation.
        principal = self.auth.verify(token)
        now = self.clock()
        if not isinstance(principal, Principal) or not principal.uid or '/' in principal.uid or len(principal.uid) > 128:
            raise Unauthorized('Invalid principal')
        if any(type(v) is not int for v in (principal.auth_time, principal.expires_at, principal.created_at)):
            raise Unauthorized('Invalid token timestamps')
        if principal.expires_at <= now or (recent and not 0 <= now-principal.auth_time <= 300):
            raise Unauthorized('Recent authentication required')
        return principal

    def submit(self, token, body):
        self._guard()
        validate_body(body)
        principal = self._principal(token)
        job = self.store.accept(principal, body['requestId'], body['recoveryKey'], self.clock)
        return receipt(job, body['requestId'], self.clock())

    def current(self, token):
        self._guard()
        principal = self._principal(token, recent=False)
        job = self.store.current(principal.uid)
        if job and job['authCreatedAt'] != principal.created_at: raise IdentityChanged('UID was reused')
        return receipt(job, job['id'], self.clock()) if job else None

    def cancel(self, token, body):
        self._guard()
        if not isinstance(body, dict) or set(body) != {'version', 'requestId', 'confirmation'} or type(body['version']) is not int or body['version'] != 1 or body['confirmation'] != 'CANCEL_ACCOUNT_DELETION':
            raise InvalidRequest('Explicit cancellation required')
        if not isinstance(body['requestId'], str): raise InvalidRequest('Invalid request identifier')
        principal = self._principal(token)
        job = self.store.cancel(principal, body['requestId'], self.clock)
        return receipt(job, body['requestId'], self.clock())

    def status(self, request_id, recovery_key):
        self._guard()
        job = self.store.lookup(request_id, recovery_key)
        return receipt(job, request_id, self.clock()) if job else None

    def run_once(self, job_id):
        """Trusted worker entry, never a client endpoint. All side effects idempotent."""
        self._guard()
        job = self.store.claim(job_id, self.clock)
        if job is None: return None
        uid = job['uid']
        try:
            self.auth.ensure_same_or_absent(uid, job['authCreatedAt'])
            def check_recorded_consent():
                if job.get('consentSource') == 'rules-validated-spark-intent':
                    verify = getattr(self.auth, 'verify_recorded_consent', None)
                    if not callable(verify): raise PermissionFailure('Consent verifier unavailable')
                    verify(uid, job['authCreatedAt'], job.get('consentAuthTime'))
            check_recorded_consent()
            if job['phase'] == 'quiesce':
                self.profiles.quiesce(uid)
                self.store.checkpoint(job, self.clock, phase='auth')
            if job['phase'] == 'auth':
                check_recorded_consent()
                # An absent user is success; a transport/permission error is NOT absence.
                self.auth.delete_if_same(uid, job['authCreatedAt'])
                self.store.checkpoint(job, self.clock, phase='profile')
            if job['phase'] == 'profile':
                # Scope is exactly users/{uid}; no guessed subcollections/RC/store deletion.
                self.profiles.delete_user_document(uid)
                self.store.checkpoint(job, self.clock, phase='complete', status='completed', error=None, lease=None, leaseUntil=0)
        except LeaseLost:
            return None  # Another worker may own it; do not overwrite its result.
        except Exception as error:
            review = isinstance(error, (PermissionFailure, IdentityChanged)) or job['attempts'] >= 5
            try:
                self.store.checkpoint(job, self.clock, status='needs_review' if review else 'retry_wait',
                    error='manual-review' if review else 'temporary', nextRun=self.clock()+min(3600, 2**job['attempts']), lease=None, leaseUntil=0)
            except LeaseLost:
                return None
        return self.store.get(job_id)
