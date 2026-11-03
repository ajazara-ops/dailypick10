import copy
import json
import threading
import unittest
import uuid
from types import SimpleNamespace
from account_deletion import (DeletionService, LocalJobStore, Disabled, Unauthorized,
    IdentityChanged, TemporaryFailure, CancellationClosed, GRACE_SECONDS)
from deletion_adapters import FirebaseAuthAdapter, FirestoreJobStore, FirestoreProfiles
from deletion_boundary import DeletionRequestHandler, run_due_once
from deletion_memory import MemoryAuth,MemoryProfiles


class FakeAdmin:
    synthetic_only=True
    class UserNotFoundError(Exception):pass
    def __init__(self,now):
        self.claims={'sub':'A','uid':'A','aud':'demo-deletion','iss':'https://securetoken.google.com/demo-deletion','auth_time':now,'exp':now+3600}
        self.users={'A':SimpleNamespace(disabled=False,user_metadata=SimpleNamespace(creation_timestamp=1000))}
        self.lookup_error=None;self.revocation_checked=False
    def verify_id_token(self,token,*,check_revoked):
        self.revocation_checked=check_revoked
        if token!='synthetic-token':raise ValueError('invalid')
        return dict(self.claims)
    def get_user(self,uid):
        if self.lookup_error:raise self.lookup_error
        if uid not in self.users:raise self.UserNotFoundError()
        return self.users[uid]
    def delete_user(self,uid):
        if uid not in self.users:raise self.UserNotFoundError()
        del self.users[uid]


class FakeDocuments:
    synthetic_only=True
    def __init__(self):
        self.docs={'users/A':{'favoriteIds':['SYNTHETIC'],'pushToken':'synthetic'},'users/A/unknown/item':{'keep':True}}
        self.lock=threading.RLock();self.fail_commit=False;self.fail_delete=False
    def read(self,path):
        with self.lock:return copy.deepcopy(self.docs.get(path))
    def transaction(self,fn):
        with self.lock:
            stage=copy.deepcopy(self.docs);parent=self
            class Tx:
                wrote=False
                def get(self,path):
                    if self.wrote:raise AssertionError('Firestore forbids reads after writes')
                    return copy.deepcopy(stage.get(path))
                def set(self,path,value):self.wrote=True;stage[path]=copy.deepcopy(value)
                def delete(self,path):
                    if parent.fail_delete:raise TemporaryFailure('synthetic delete failure')
                    self.wrote=True;stage.pop(path,None)
            result=fn(Tx())
            if self.fail_commit:raise TemporaryFailure('synthetic commit failure')
            self.docs=stage;return copy.deepcopy(result)
    def due_jobs(self,now,limit):
        jobs=[v for k,v in self.docs.items() if k.startswith('accountDeletionJobs/') and v['status'] not in ['cancelled','completed','needs_review'] and v['deleteAfter']<=now and v['nextRun']<=now and v['leaseUntil']<=now]
        return [v['id'] for v in sorted(jobs,key=lambda j:(j['nextRun'],j['id']))[:limit]]
    def client_write(self,uid,profile):
        state=self.read('accountDeletionState/'+uid)
        if state and state['blocked']:raise Unauthorized('blocked')
        if 'deletionRequest' in profile:raise Unauthorized('server-owned field')
        self.docs['users/'+uid]=copy.deepcopy(profile)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.now=1800000000;self.sdk=FakeAdmin(self.now);self.docs=FakeDocuments()
        self.auth=FirebaseAuthAdapter(self.sdk,'demo-deletion');self.store=FirestoreJobStore(self.docs)
        self.service=DeletionService(self.store,self.auth,FirestoreProfiles(self.docs),lambda:self.now,mode='mock')
        self.http=DeletionRequestHandler(self.service,mode='mock')
        self.body={'version':1,'requestId':str(uuid.uuid4()),'recoveryKey':'f'*64,'confirmation':'DELETE_MY_ACCOUNT'}
    def request(self):return self.service.submit('synthetic-token',self.body)
    def cancel(self):return self.service.cancel('synthetic-token',{'version':1,'requestId':self.body['requestId'],'confirmation':'CANCEL_ACCOUNT_DELETION'})
    def due(self):self.now+=GRACE_SECONDS
    def worker(self):return self.service.run_once(self.body['requestId'])
    def call(self,path,body=None,method='POST',headers=None):
        return self.http.handle(method,'/__mock__/v1/account-deletion/'+path,headers or {'Authorization':'Bearer synthetic-token','Content-Type':'application/json'},json.dumps(body).encode() if body is not None else b'')

    def test_default_and_production_routes_never_invoke_mock_or_sdk(self):
        self.assertEqual(DeletionRequestHandler().handle('POST','/v1/account-deletion/request')[0],503)
        self.assertEqual(self.http.handle('POST','/v1/account-deletion/request')[0],503)
        self.assertFalse(self.sdk.revocation_checked);self.assertEqual(self.docs.docs.keys(),{'users/A','users/A/unknown/item'})
    def test_real_bindings_and_non_demo_projects_rejected_before_calls(self):
        for make in [lambda:FirebaseAuthAdapter(object(),'demo-deletion'),lambda:FirestoreJobStore(object()),lambda:FirestoreProfiles(object()),lambda:FirebaseAuthAdapter(self.sdk,'unapproved-project')]:
            with self.assertRaises(Disabled):make()
    def test_verifier_revocation_project_issuer_disabled_and_uid_generation(self):
        self.request();self.assertTrue(self.sdk.revocation_checked)
        for key,value in [('aud','wrong'),('iss','wrong'),('uid','B')]:
            previous=self.sdk.claims[key];self.sdk.claims[key]=value
            with self.assertRaises(Unauthorized):self.auth.verify('synthetic-token')
            self.sdk.claims[key]=previous
        self.sdk.users['A'].disabled=True
        with self.assertRaises(Unauthorized):self.auth.verify('synthetic-token')
        self.sdk.users['A'].disabled=False;self.sdk.users['A'].user_metadata.creation_timestamp=9999
        with self.assertRaises(IdentityChanged):self.cancel()
    def test_acceptance_atomically_writes_job_state_and_push_marker(self):
        result=self.request();self.assertEqual(result['deleteAfter']-result['requestedAt'],604800)
        self.assertTrue(self.store.blocked('A'));self.assertEqual(self.docs.read('users/A')['deletionRequest']['requestId'],self.body['requestId'])
        with self.assertRaises(Unauthorized):self.docs.client_write('A',{'late':'write'})
        proof=self.docs.read('accountDeletionReceipts/'+self.body['requestId'])
        self.assertNotEqual(proof['proof'],self.body['recoveryKey'])
    def test_commit_failure_leaves_no_half_request_or_profile_marker(self):
        before=copy.deepcopy(self.docs.docs);self.docs.fail_commit=True
        with self.assertRaises(TemporaryFailure):self.request()
        self.assertEqual(before,self.docs.docs);self.docs.fail_commit=False
        self.request();self.assertTrue(self.store.blocked('A'))
    def test_duplicate_and_cancelled_replay_never_extend_or_restart(self):
        first=self.request();self.now+=1;self.assertEqual(self.request()['deleteAfter'],first['deleteAfter'])
        self.cancel();self.assertEqual(self.request()['status'],'cancelled');self.assertFalse(self.store.blocked('A'))
    def test_cancellation_clears_only_own_marker_and_does_not_cancel_new_job(self):
        self.request();self.cancel();self.assertNotIn('deletionRequest',self.docs.read('users/A'))
        old=copy.deepcopy(self.body);self.body['requestId']=str(uuid.uuid4());self.request();new_id=self.body['requestId']
        self.body=old;self.cancel();self.assertTrue(self.store.blocked('A'))
        self.assertEqual(self.docs.read('users/A')['deletionRequest']['requestId'],new_id)
    def test_exact_deadline_and_claim_close_cancellation(self):
        self.request();self.due();self.sdk.claims.update(auth_time=self.now,exp=self.now+3600)
        with self.assertRaises(CancellationClosed):self.cancel()
        self.assertIsNotNone(self.store.claim(self.body['requestId'],lambda:self.now))
        self.assertIsNone(self.store.claim(self.body['requestId'],lambda:self.now))
    def test_cleanup_retry_after_auth_deleted_and_scope_preserved(self):
        self.request();self.due();self.docs.fail_delete=True
        result=self.worker();self.assertEqual(result['status'],'retry_wait');self.assertNotIn('A',self.sdk.users)
        self.docs.fail_delete=False;self.now=result['nextRun'];self.assertEqual(self.worker()['status'],'completed')
        self.assertIsNone(self.docs.read('users/A'));self.assertIsNotNone(self.docs.read('users/A/unknown/item'));self.assertTrue(self.store.blocked('A'))
        self.assertEqual(self.call('status',{'requestId':self.body['requestId'],'recoveryKey':self.body['recoveryKey']},headers={'Content-Type':'application/json'})[2]['receipt']['status'],'completed')
    def test_lookup_failure_is_not_absence(self):
        self.request();self.due();self.sdk.lookup_error=RuntimeError('transport failed')
        self.assertEqual(self.worker()['status'],'retry_wait');self.assertIn('A',self.sdk.users);self.assertIsNotNone(self.docs.read('users/A'))
    def test_http_contract_auth_fields_content_type_query_and_size(self):
        self.assertEqual(self.call('request',self.body)[0],200)
        self.assertEqual(self.call('request',{**self.body,'uid':'B'})[0],400)
        self.assertEqual(self.call('cancel',{'version':1,'requestId':self.body['requestId'],'confirmation':'CANCEL_ACCOUNT_DELETION'},headers={'Content-Type':'application/json'})[0],401)
        self.assertEqual(self.call('worker',{})[0],404)
        self.assertEqual(self.call('status?recoveryKey=synthetic',{})[0],400)
        self.assertEqual(self.call('request',self.body,headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.http.handle('POST','/__mock__/v1/account-deletion/request',{},b'x'*4097)[0],413)
    def test_current_does_not_cancel_and_stale_recent_auth_rejected(self):
        self.request();self.assertEqual(self.call('current',method='GET')[2]['receipt']['status'],'grace_period')
        self.now+=301;self.assertEqual(self.call('request',self.body)[0],401);self.assertTrue(self.store.blocked('A'))
    def test_bounded_due_callback_requires_explicit_enable_and_is_idempotent(self):
        self.request();self.due();self.assertEqual(run_due_once(self.service)['examined'],0)
        self.assertEqual(run_due_once(self.service,enabled=True,limit=1)['completed'],1)
        self.assertEqual(run_due_once(self.service,enabled=True,limit=1)['examined'],0)
    def test_sqlite_due_callback_reuses_persistent_store(self):
        store=LocalJobStore(':memory:');auth=MemoryAuth();token=auth.seed('A',self.now)
        try:
            service=DeletionService(store,auth,MemoryProfiles(store),lambda:self.now,mode='mock')
            service.submit(token,self.body);self.assertEqual(run_due_once(service,enabled=True)['examined'],0)
            self.due();self.assertEqual(run_due_once(service,enabled=True)['completed'],1)
        finally:store.close()


if __name__=='__main__':unittest.main()
