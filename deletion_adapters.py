"""Firebase-shaped adapters exercised ONLY with injected synthetic ports.

No Admin SDK import, initialize_app, ADC, URL, environment lookup or live CLI.
Constructors reject non-synthetic bindings. Real binding and guard changes need
separate review/approval and emulator verification before any deployment.
"""
import hashlib
import hmac
import secrets
from account_deletion import (Principal, Disabled, InvalidRequest, Unauthorized, IdentityChanged,
    TemporaryFailure, PermissionFailure, CancellationClosed, LeaseLost, GRACE_SECONDS, validate_proof)


def synthetic(port):
    if getattr(port,'synthetic_only',False) is not True:
        raise Disabled('Only injected synthetic bindings are enabled')


class FirebaseAuthAdapter:
    synthetic_only=True
    def __init__(self, sdk, project_id):
        synthetic(sdk)
        if not project_id.startswith('demo-'): raise Disabled('Synthetic demo project required')
        self.sdk,self.project_id=sdk,project_id

    def verify(self, token):
        try:
            claims=self.sdk.verify_id_token(token,check_revoked=True)
            uid=claims['sub']
            if claims.get('uid',uid)!=uid or claims['aud']!=self.project_id or claims['iss']!='https://securetoken.google.com/'+self.project_id:
                raise Unauthorized('Wrong identity scope')
            user=self.sdk.get_user(uid)
            if user.disabled: raise Unauthorized('Disabled user')
            return Principal(uid,claims['auth_time'],claims['exp'],user.user_metadata.creation_timestamp)
        except Exception as error:
            raise Unauthorized('Token verification failed') from error

    def _user(self, uid):
        try: return self.sdk.get_user(uid)
        except self.sdk.UserNotFoundError: return None
        except PermissionError as error: raise PermissionFailure('Auth access denied') from error
        except Exception as error: raise TemporaryFailure('Auth lookup failed') from error

    def intake_identity(self, uid):
        """Trusted SDK metadata for the existing Spark bridge; no client claims.

        This retains the synthetic/demo constructor guard. Absence and lookup
        failure must not both become a fabricated valid identity.
        """
        from spark_intake import SparkAuthState
        if not isinstance(uid,str) or not uid or '/' in uid or len(uid)>128:
            raise Unauthorized('Invalid scoped identity')
        user=self._user(uid)
        if user is None:raise Unauthorized('Authoritative identity absent')
        created=getattr(getattr(user,'user_metadata',None),'creation_timestamp',None)
        valid_after=getattr(user,'tokens_valid_after_timestamp',None)
        disabled=getattr(user,'disabled',None)
        if (type(created) is not int or created<0 or type(valid_after) is not int or
                valid_after<0 or type(disabled) is not bool):
            raise Unauthorized('Authoritative identity metadata unavailable')
        return SparkAuthState(created,valid_after,disabled)

    def ensure_same_or_absent(self, uid, generation):
        user=self._user(uid)
        if user and user.user_metadata.creation_timestamp!=generation: raise IdentityChanged('UID generation changed')

    def verify_recorded_consent(self, uid, generation, auth_time):
        user=self._user(uid)
        if user is None: return  # Only a previously validated durable job may reach this phase.
        if user.user_metadata.creation_timestamp != generation: raise IdentityChanged('UID generation changed')
        valid_after=getattr(user, 'tokens_valid_after_timestamp', None)
        if type(auth_time) is not int or type(valid_after) is not int or auth_time < 0 or valid_after < 0:
            raise PermissionFailure('Authoritative consent timestamps required')
        if user.disabled or auth_time * 1000 < valid_after:
            raise PermissionFailure('Recorded consent revoked or identity disabled')

    def delete_if_same(self, uid, generation):
        self.ensure_same_or_absent(uid,generation)
        try: self.sdk.delete_user(uid)
        except self.sdk.UserNotFoundError: pass
        except PermissionError as error: raise PermissionFailure('Auth deletion denied') from error
        except Exception as error: raise TemporaryFailure('Auth deletion not confirmed') from error


class FirestoreJobStore:
    """Port: read(path), transaction(fn(tx)), tx.get/set/delete, due_jobs(now,limit).
    The transaction callback reads every document before any write, including on retry.
    """
    synthetic_only=True
    def __init__(self, documents): synthetic(documents);self.documents=documents
    def get(self, job_id): return self.documents.read('accountDeletionJobs/'+job_id)
    def current(self, uid):
        def read(tx):
            state=tx.get('accountDeletionState/'+uid)
            return tx.get('accountDeletionJobs/'+state['jobId']) if state else None
        return self.documents.transaction(read)
    def blocked(self, uid):
        state=self.documents.read('accountDeletionState/'+uid)
        return bool(state and state.get('blocked'))
    def due_ids(self, now, limit): return self.documents.due_jobs(now,limit)

    def accept(self, principal, request_id, key, now):
        digest=hashlib.sha256(key.encode()).hexdigest()
        def transact(tx):
            proof=tx.get('accountDeletionReceipts/'+request_id)
            if proof:
                job=tx.get('accountDeletionJobs/'+proof['jobId'])
                if job['uid']!=principal.uid or not hmac.compare_digest(proof['proof'],digest): raise Unauthorized('Receipt collision')
                if job['authCreatedAt']!=principal.created_at: raise IdentityChanged('UID reused')
                return job
            state=tx.get('accountDeletionState/'+principal.uid)
            job=tx.get('accountDeletionJobs/'+state['jobId']) if state else None
            profile=tx.get('users/'+principal.uid)
            timestamp=now() if callable(now) else now
            if job and job['authCreatedAt']!=principal.created_at: raise IdentityChanged('UID reused')
            if not job or job['status']=='cancelled':
                job=dict(id=request_id,uid=principal.uid,authCreatedAt=principal.created_at,status='grace_period',phase='quiesce',
                    attempts=0,nextRun=timestamp+GRACE_SECONDS,createdAt=timestamp,deleteAfter=timestamp+GRACE_SECONDS,
                    consentAuthTime=principal.auth_time,startedAt=None,lease=None,leaseUntil=0,error=None,receiptCount=0)
            if job['receiptCount']>=8: raise InvalidRequest('Receipt limit')
            job['receiptCount']+=1
            tx.set('accountDeletionJobs/'+job['id'],job)
            tx.set('accountDeletionState/'+principal.uid,{'jobId':job['id'],'blocked':True})
            tx.set('accountDeletionReceipts/'+request_id,{'jobId':job['id'],'proof':digest})
            if profile is not None:
                profile['deletionRequest']={'requestId':job['id'],'requestedAt':job['createdAt'],'deleteAfter':job['deleteAfter']}
                tx.set('users/'+principal.uid,profile)
            return job
        return self.documents.transaction(transact)

    def lookup(self, request_id, key):
        validate_proof(request_id,key)
        proof=self.documents.read('accountDeletionReceipts/'+request_id)
        if not proof: return None
        if not hmac.compare_digest(proof['proof'],hashlib.sha256(key.encode()).hexdigest()): raise Unauthorized('Wrong receipt proof')
        return self.get(proof['jobId'])

    def cancel(self, principal, request_id, now):
        def transact(tx):
            proof=tx.get('accountDeletionReceipts/'+request_id)
            job=tx.get('accountDeletionJobs/'+(proof['jobId'] if proof else request_id))
            state=tx.get('accountDeletionState/'+principal.uid)
            profile=tx.get('users/'+principal.uid)
            timestamp=now() if callable(now) else now
            if not job or job['uid']!=principal.uid: raise Unauthorized('Not your request')
            if job['authCreatedAt']!=principal.created_at: raise IdentityChanged('UID reused')
            if job['status']=='cancelled': return job
            if timestamp>=job['deleteAfter'] or job['startedAt'] is not None or job['status']!='grace_period': raise CancellationClosed('Cancellation ended')
            job.update(status='cancelled',error=None);tx.set('accountDeletionJobs/'+job['id'],job)
            if state and state['jobId']==job['id']:
                tx.set('accountDeletionState/'+principal.uid,{'jobId':job['id'],'blocked':False})
                if profile is not None and profile.get('deletionRequest',{}).get('requestId')==job['id']:
                    profile.pop('deletionRequest');tx.set('users/'+principal.uid,profile)
            return job
        return self.documents.transaction(transact)

    def claim(self, job_id, now):
        def transact(tx):
            job=tx.get('accountDeletionJobs/'+job_id);timestamp=now() if callable(now) else now
            if not job or job['status'] in {'completed','needs_review','cancelled'} or timestamp<job['deleteAfter'] or job['nextRun']>timestamp or job['leaseUntil']>timestamp:return None
            job.update(status='processing',startedAt=job['startedAt'] if job['startedAt'] is not None else timestamp,attempts=job['attempts']+1,lease=secrets.token_hex(16),leaseUntil=timestamp+60)
            tx.set('accountDeletionJobs/'+job_id,job);return job
        return self.documents.transaction(transact)

    def checkpoint(self, job, now, **updates):
        def transact(tx):
            current=tx.get('accountDeletionJobs/'+job['id']);timestamp=now() if callable(now) else now
            if not current or current['lease']!=job['lease'] or current['leaseUntil']<=timestamp:raise LeaseLost('Lease expired')
            current.update(updates);tx.set('accountDeletionJobs/'+job['id'],current);return current
        job.update(self.documents.transaction(transact))


class FirestoreProfiles:
    synthetic_only=True
    def __init__(self, documents):synthetic(documents);self.documents=documents
    def quiesce(self, uid):
        def transact(tx):
            state=tx.get('accountDeletionState/'+uid);profile=tx.get('users/'+uid)
            if not state or not state.get('blocked'):raise PermissionFailure('Missing authoritative block')
            if profile is not None:
                profile['deletionRequest']={'requestId':state['jobId']};tx.set('users/'+uid,profile)
        self.documents.transaction(transact)
    def delete_user_document(self, uid):
        def transact(tx):
            state=tx.get('accountDeletionState/'+uid)
            if not state or not state.get('blocked'):raise PermissionFailure('Missing authoritative block')
            tx.delete('users/'+uid)
        self.documents.transaction(transact)
