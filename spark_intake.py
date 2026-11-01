"""Local-only bridge: rules-validated Firestore intent -> existing deletion worker.

No live binding, Admin initialization, credentials, network entry or runnable CLI.
The caller supplies a guarded demo store and a synthetic generation lookup.
"""
import calendar
from dataclasses import dataclass
from account_deletion import GRACE_SECONDS, Disabled, IdentityChanged, PermissionFailure, Unauthorized, CancellationClosed
from deletion_adapters import synthetic


def deadline_parts(timestamp):
    # Firestore preserves nanoseconds; a float can round a 1ns fraction down.
    # The worker's whole-second clock must never delete early because of that.
    seconds = calendar.timegm(timestamp.utctimetuple())
    nanos = getattr(timestamp, 'nanosecond', timestamp.microsecond * 1000)
    accepted = seconds + nanos / 1_000_000_000
    return accepted, seconds + GRACE_SECONDS + (1 if nanos else 0)


def timestamp_ns(timestamp):
    return calendar.timegm(timestamp.utctimetuple()) * 1_000_000_000 + getattr(timestamp, 'nanosecond', timestamp.microsecond * 1000)


@dataclass(frozen=True)
class SparkAuthState:
    """Trusted synthetic Auth snapshot, never accepted from a client document."""
    created_at: int  # milliseconds
    valid_after: int  # milliseconds, Admin SDK tokens_valid_after_timestamp
    disabled: bool = False


def verify_consent(identity, auth_time, accepted_ns):
    if not isinstance(identity, SparkAuthState) or type(identity.disabled) is not bool:
        raise Unauthorized('Authoritative Auth state required')
    if any(type(x) is not int or x < 0 for x in (identity.created_at, identity.valid_after, auth_time)):
        raise Unauthorized('Invalid consent timestamps')
    if identity.created_at * 1_000_000 > accepted_ns:
        raise IdentityChanged('UID created after consent')
    if identity.disabled or auth_time * 1000 < identity.valid_after or not 0 <= accepted_ns - auth_time * 1_000_000_000 < 300_000_000_000:
        raise Unauthorized('Consent revoked, disabled or not recent at intake')
    return identity.created_at


class SparkIntentBridge:
    synthetic_only = True

    def __init__(self, documents, auth_state_lookup, *, enabled=False):
        synthetic(documents)
        self.documents, self.auth_state_lookup, self.enabled = documents, auth_state_lookup, enabled

    def _guard(self, uid):
        if not self.enabled: raise Disabled('Spark intent bridge is disabled by default')
        if not isinstance(uid, str) or not uid or '/' in uid or len(uid) > 128: raise ValueError('Invalid scoped UID')

    def confirm_cancellation(self, uid, clock):
        """Trusted confirmation; client acknowledgement alone never unblocks."""
        self._guard(uid)
        identity = self.auth_state_lookup(uid)
        def transact(tx):
            path = 'accountDeletionIntents/' + uid
            intent = tx.get(path)
            if not intent or intent['status'] == 'cancelled': return intent
            if intent['status'] != 'cancel_requested': raise CancellationClosed('No pending cancellation')
            job_id = intent['requestId']
            job = tx.get('accountDeletionJobs/' + job_id)
            state = tx.get('accountDeletionState/' + uid)
            profile = tx.get('users/' + uid)
            accepted = timestamp_ns(intent['requestedAt'])
            cancelled = timestamp_ns(intent['cancelRequestedAt'])
            if intent.get('version') != 2 or not accepted <= cancelled < accepted + GRACE_SECONDS * 1_000_000_000:
                raise CancellationClosed('Cancellation was not received within grace period')
            if job is not None:
                raise CancellationClosed('Deletion claimed or lifecycle changed')
            if state and state.get('jobId') != job_id:
                # A confirmed cancellation intentionally leaves an unblocked
                # state record. A later explicit request has a new requestId.
                # That old record must not prevent cancelling the new request,
                # but an active, completed or ambiguous lifecycle must remain blocked.
                prior_id = state.get('jobId')
                if state.get('blocked') is not False or not isinstance(prior_id, str) or not prior_id or '/' in prior_id:
                    raise CancellationClosed('Deletion lifecycle is still blocked')
                prior_job = tx.get('accountDeletionJobs/' + prior_id)
                if prior_job is not None and (prior_job.get('uid') != uid or prior_job.get('status') != 'cancelled'):
                    raise CancellationClosed('Previous lifecycle is not confirmed cancelled')
            verify_consent(identity, intent.get('cancelAuthTime'), cancelled)
            if identity.created_at * 1_000_000 > accepted: raise IdentityChanged('UID generation changed')
            if profile is not None and profile.get('deletionRequest') != {'requestId':job_id}:
                raise PermissionFailure('Unexpected profile marker')
            # All reads precede writes; a failed commit retains the pending block.
            intent.update(status='cancelled', cancelledAt=clock())
            tx.set(path, intent)
            tx.set('accountDeletionState/' + uid, {'jobId':job_id, 'blocked':False})
            if profile is not None:
                profile.pop('deletionRequest', None); tx.set('users/' + uid, profile)
            return intent
        return self.documents.transaction(transact)

    def materialize_due(self, uid, clock):
        self._guard(uid)
        # This read must distinguish absent Auth from lookup failure. No stale token is used.
        identity = self.auth_state_lookup(uid)
        def transact(tx):
            intent = tx.get('accountDeletionIntents/' + uid)
            if not intent or intent['status'] in {'cancelled', 'cancel_requested'}: return None
            job_id = intent['requestId']
            existing = tx.get('accountDeletionJobs/' + job_id)
            if existing:
                if existing['uid'] != uid: raise PermissionFailure('Request collision')
                return existing
            now = clock()
            accepted, not_before = deadline_parts(intent['requestedAt'])
            if intent['status'] != 'grace_period' or now < not_before: return None
            if intent.get('version') != 2: raise Unauthorized('Legacy unverified intent requires review')
            generation = verify_consent(identity, intent.get('consentAuthTime'), timestamp_ns(intent['requestedAt']))
            profile = tx.get('users/' + uid)
            job = dict(id=job_id, uid=uid, authCreatedAt=generation,
                       status='grace_period', phase='quiesce', attempts=0, nextRun=not_before,
                       createdAt=accepted, deleteAfter=accepted+GRACE_SECONDS, consentAuthTime=intent['consentAuthTime'],
                       startedAt=None, lease=None, leaseUntil=0, error=None, receiptCount=0,
                       consentSource='rules-validated-spark-intent')
            # Claim/cancel conflict on the same intent. Clients cannot change this status.
            intent.update(status='processing')
            tx.set('accountDeletionIntents/' + uid, intent)
            tx.set('accountDeletionJobs/' + job_id, job)
            tx.set('accountDeletionState/' + uid, {'jobId':job_id, 'blocked':True})
            if profile is not None:
                profile['deletionRequest']={'requestId':job_id}; tx.set('users/'+uid,profile)
            return job
        return self.documents.transaction(transact)

    def reflect(self, job_id):
        if not self.enabled: raise Disabled('Spark intent bridge is disabled by default')
        def transact(tx):
            job=tx.get('accountDeletionJobs/'+job_id)
            if not job: return None
            path='accountDeletionIntents/'+job['uid']; intent=tx.get(path)
            if not intent or intent['requestId']!=job_id: raise PermissionFailure('Intent generation mismatch')
            intent.update(status=job['status'] if job['status']!='grace_period' else 'processing')
            tx.set(path,intent);return intent
        return self.documents.transaction(transact)
