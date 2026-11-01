import ast
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from account_deletion import (DeletionService, LocalJobStore, Disabled, InvalidRequest, Unauthorized,
    CancellationClosed, IdentityChanged, PermissionFailure, LeaseLost, GRACE_SECONDS)
from deletion_memory import MemoryAuth, MemoryProfiles


def body():
    return dict(version=1, requestId=str(uuid.uuid4()), recoveryKey=uuid.uuid4().hex+uuid.uuid4().hex, confirmation='DELETE_MY_ACCOUNT')


class DeletionTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.test-tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp')
        self.path = str(Path(self.temp.name)/'synthetic.sqlite')
        self.store = LocalJobStore(self.path)
        self.now = 1800000000
        self.auth = MemoryAuth(); self.token = self.auth.seed('A', self.now)
        self.auth.seed('B', self.now)
        self.profiles = MemoryProfiles(self.store)
        self.profiles.documents = {'A':{'favoriteIds':['SYNTHETIC'],'pushToken':'synthetic'},'B':{'pushToken':'synthetic'}}
        self.profiles.unscoped = {'A/subcollection':{'keep':'unknown scope'}}
        self.service = DeletionService(self.store, self.auth, self.profiles, lambda:self.now, mode='mock')
        self.request = body()

    def tearDown(self): self.store.close(); self.temp.cleanup()
    def submit(self): return self.service.submit(self.token, self.request)
    def cancel(self): return self.service.cancel(self.token, dict(version=1,requestId=self.request['requestId'],confirmation='CANCEL_ACCOUNT_DELETION'))
    def refresh_auth(self): self.token = self.auth.seed('A', self.now)
    def due(self): self.now += GRACE_SECONDS
    def worker(self): return self.service.run_once(self.request['requestId'])

    def test_disabled_before_any_auth_or_store_action(self):
        disabled = DeletionService(self.store,self.auth,self.profiles,lambda:self.now)
        with patch.object(self.auth,'verify',side_effect=AssertionError('must not verify')):
            with self.assertRaises(Disabled): disabled.submit(self.token,self.request)
        self.assertIsNone(self.store.current('A'))

    def test_rejects_client_uid_deadline_status_and_missing_explicit_consent(self):
        for key in ['uid','deleteAfter','status','auth_time','paths']:
            with self.assertRaises(InvalidRequest): self.service.submit(self.token,{**self.request,key:'forged'})
        with self.assertRaises(InvalidRequest): self.service.submit(self.token,{**self.request,'confirmation':False})
        with self.assertRaises(Unauthorized): self.service.submit('forged',self.request)
        self.assertFalse(self.store.blocked('A'))

    def test_recent_auth_required_for_submit_and_cancel(self):
        self.now += 301
        with self.assertRaises(Unauthorized): self.submit()
        self.refresh_auth(); self.submit(); self.now += 301
        with self.assertRaises(Unauthorized): self.cancel()
        self.assertTrue(self.store.blocked('A'))

    def test_server_time_exact_seven_days_and_immediate_access_push_block(self):
        result=self.submit()
        self.assertEqual(result['deleteAfter']-result['requestedAt'],604800)
        self.assertEqual(result['status'],'grace_period'); self.assertTrue(result['cancelAllowed'])
        self.assertEqual(self.profiles.recipients(),['B'])
        with self.assertRaises(Unauthorized): self.profiles.write_as_client('A',{'late':'write'})
        self.assertIn('A',self.auth.users); self.assertIn('A',self.profiles.documents)
        self.assertIsNone(self.worker())

    def test_duplicate_and_different_nonce_cannot_extend_deadline(self):
        first=self.submit();self.now+=100
        self.assertEqual(self.submit()['deleteAfter'],first['deleteAfter'])
        second=self.service.submit(self.token,body())
        self.assertEqual(second['deleteAfter'],first['deleteAfter'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_relogin_does_not_cancel(self):
        self.submit();self.refresh_auth()
        self.assertEqual(self.service.current(self.token)['status'],'grace_period')
        self.assertTrue(self.store.blocked('A'))

    def test_explicit_cancel_before_deadline_restores_access_without_deleting(self):
        self.submit();self.now+=GRACE_SECONDS-1;self.refresh_auth()
        self.assertEqual(self.cancel()['status'],'cancelled')
        self.assertFalse(self.store.blocked('A'));self.profiles.write_as_client('A',{'kept':True})
        self.now+=2;self.assertIsNone(self.worker());self.assertIn('A',self.auth.users)

    def test_cancel_lost_response_and_replay_are_idempotent(self):
        self.submit();self.cancel()
        self.assertEqual(self.cancel()['status'],'cancelled')
        self.assertEqual(self.service.status(self.request['requestId'],self.request['recoveryKey'])['status'],'cancelled')
        self.now+=20;self.assertEqual(self.submit()['status'],'cancelled')
        new=self.service.submit(self.token,body());self.assertEqual(new['status'],'grace_period')
        self.assertEqual(new['requestedAt'],self.now)
        self.cancel()  # old cancelled receipt cannot unblock the new request
        self.assertTrue(self.store.blocked('A'))

    def test_exact_deadline_closes_cancel_and_only_queues_deletion(self):
        self.submit();self.due();self.refresh_auth()
        with self.assertRaises(CancellationClosed):self.cancel()
        result=self.service.status(self.request['requestId'],self.request['recoveryKey'])
        self.assertEqual(result['status'],'queued');self.assertFalse(result['cancelAllowed']);self.assertEqual(result['completedScope'],[])
        self.assertIn('A',self.auth.users)

    def test_started_deletion_cannot_be_cancelled_even_if_clock_moves_back(self):
        self.submit();self.due();claimed=self.store.claim(self.request['requestId'],self.now)
        self.assertIsNotNone(claimed);self.now-=1;self.refresh_auth()
        with self.assertRaises(CancellationClosed):self.cancel()

    def test_cancel_waiting_for_transaction_uses_time_after_acquiring_lock(self):
        self.submit();self.now+=GRACE_SECONDS-1;self.refresh_auth()
        original=self.store.transaction
        @contextmanager
        def delayed():
            with original():
                self.now+=1
                yield
        with patch.object(self.store,'transaction',delayed):
            with self.assertRaises(CancellationClosed):self.cancel()
        self.assertTrue(self.store.blocked('A'))

    def test_other_uid_cannot_cancel_or_reuse_request_identifier(self):
        self.submit()
        with self.assertRaises(Unauthorized):self.service.cancel('synthetic-token:B',dict(version=1,requestId=self.request['requestId'],confirmation='CANCEL_ACCOUNT_DELETION'))
        with self.assertRaises(Unauthorized):self.service.submit('synthetic-token:B',self.request)
        with self.assertRaises(Unauthorized):self.service.status(self.request['requestId'],'0'*64)

    def test_reused_uid_cannot_read_cancel_or_replay_the_previous_identity_request(self):
        self.submit();self.token=self.auth.seed('A',self.now,created_at=999)
        for action in [self.submit,self.cancel,lambda:self.service.current(self.token)]:
            with self.assertRaises(IdentityChanged):action()
        self.assertTrue(self.store.blocked('A'))

    def test_acceptance_and_receipt_survive_lost_response_and_store_reopen(self):
        self.submit();self.store.close();self.store=LocalJobStore(self.path)
        self.profiles.store=self.store;self.service.store=self.store
        r=self.service.status(self.request['requestId'],self.request['recoveryKey'])
        self.assertEqual(r['status'],'grace_period');self.assertTrue(self.store.blocked('A'))

    def test_receipt_stores_hash_not_recovery_secret(self):
        self.submit();stored=self.store.db.execute('SELECT proof FROM receipts').fetchone()[0]
        self.assertNotEqual(stored,self.request['recoveryKey'])
        self.assertNotIn(self.request['recoveryKey'],json.dumps(self.store.current('A')))

    def test_auth_failure_retry_preserves_profile_and_final_scope_is_narrow(self):
        self.submit();self.due();self.auth.failures['A']='before'
        result=self.worker();self.assertEqual(result['status'],'retry_wait');self.assertIn('A',self.profiles.documents)
        self.auth.failures.clear();self.now=result['nextRun'];result=self.worker()
        self.assertEqual(result['status'],'completed');self.assertNotIn('A',self.auth.users);self.assertNotIn('A',self.profiles.documents)
        self.assertIn('B',self.profiles.documents);self.assertIn('A/subcollection',self.profiles.unscoped)
        receipt=self.service.status(self.request['requestId'],self.request['recoveryKey'])
        self.assertFalse(receipt['subscriptionCancelled']);self.assertEqual(receipt['completedScope'],['firebase-auth','users-document'])
        self.assertTrue(self.store.blocked('A'));self.assertIsNone(self.worker())

    def test_profile_failure_after_auth_deletion_recovers_without_user_login(self):
        self.submit();self.due();self.profiles.failures['A']='before'
        result=self.worker();self.assertEqual(result['phase'],'profile');self.assertNotIn('A',self.auth.users)
        self.assertEqual(self.service.status(self.request['requestId'],self.request['recoveryKey'])['status'],'retry_wait')
        self.profiles.failures.clear();self.now=result['nextRun'];self.assertEqual(self.worker()['status'],'completed')

    def test_lost_auth_and_profile_responses_retry_already_deleted_resources(self):
        for component in [self.auth,self.profiles]:
            # Reset only synthetic test state, never real data.
            if component is self.auth:self.submit();self.due()
            component.failures['A']='after';r=self.worker();self.assertEqual(r['status'],'retry_wait')
            component.failures.clear();self.now=r['nextRun']
        self.assertEqual(self.worker()['status'],'completed')

    def test_no_deletion_on_auth_lookup_error_or_uid_generation_reuse(self):
        self.submit();self.due();self.auth.failures['A']='lookup';r=self.worker()
        self.assertEqual(r['status'],'retry_wait');self.assertIn('A',self.profiles.documents)
        self.auth.failures.clear();self.auth.users['A']=999;self.now=r['nextRun'];r=self.worker()
        self.assertEqual(r['status'],'needs_review');self.assertIn('A',self.profiles.documents)

    def test_permission_failure_stops_for_review_and_retries_are_bounded(self):
        self.submit();self.due()
        with patch.object(self.profiles,'quiesce',side_effect=PermissionFailure('synthetic')):
            self.assertEqual(self.worker()['status'],'needs_review')
        self.assertIsNone(self.worker());self.assertTrue(self.store.blocked('A'))

    def test_lease_recovery_rejects_old_worker_checkpoint(self):
        self.submit();self.due();old=self.store.claim(self.request['requestId'],self.now)
        other=LocalJobStore(self.path)
        try:
            self.assertIsNone(other.claim(self.request['requestId'],self.now))
            self.now+=61;new=other.claim(self.request['requestId'],self.now);self.assertIsNotNone(new)
            with self.assertRaises(LeaseLost):self.store.checkpoint(old,self.now,status='completed')
        finally:other.close()

    def test_five_transient_failures_stop_for_review_without_reporting_complete(self):
        self.submit();self.due();self.profiles.failures['A']='quiesce'
        for attempt in range(1,6):
            result=self.worker();self.assertEqual(result['attempts'],attempt)
            self.assertEqual(result['status'],'needs_review' if attempt==5 else 'retry_wait')
            self.now=result['nextRun']
        self.assertIsNone(self.worker());self.assertIn('A',self.auth.users);self.assertTrue(self.store.blocked('A'))

    def test_existing_push_reader_excludes_pending_markers_without_loading_credentials(self):
        from account_deletion import push_eligible
        tree=ast.parse((ROOT/'daily_stock_scanner.py').read_text(encoding='utf-8-sig'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_all_user_tokens')
        class Doc:
            def __init__(self,data):self.value=data
            def to_dict(self):return self.value
        class Collection:
            def stream(self):return [Doc({'pushToken':'ExponentPushToken[synthetic-A]','deletionRequest':{'status':'pending'}}),Doc({'pushToken':'ExponentPushToken[synthetic-B]'}),Doc(None)]
        class DB:
            def collection(self,name):self_test.assertEqual(name,'users');return Collection()
        class Firestore:
            def client(self):return DB()
        self_test=self
        namespace={'FIREBASE_AVAILABLE':True,'firebase_admin':type('Admin',(),{'_apps':[True]}),'firestore':Firestore(),'push_eligible':push_eligible,'print':lambda *a:None}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<push-reader-only>','exec'),namespace)
        self.assertEqual(namespace['get_all_user_tokens'](),['ExponentPushToken[synthetic-B]'])

    def test_fixed_push_recipient_cannot_bypass_db_exclusion_or_lookup_failure(self):
        tree=ast.parse((ROOT/'daily_stock_scanner.py').read_text(encoding='utf-8-sig'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='send_push_notification')
        # Replace the existing fixed recipient in the AST; never send or expose its value.
        for node in ast.walk(function):
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='admin_tokens' for t in node.targets):
                node.value=ast.List(elts=[ast.Constant('ExponentPushToken[synthetic-A]')],ctx=ast.Load())
        module=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
        sent=[]
        class Requests:
            @staticmethod
            def post(url,**kwargs):sent.append(json.loads(kwargs['data'])['to'])
        namespace={'get_all_user_tokens':lambda:['ExponentPushToken[synthetic-B]'],'requests':Requests,'json':json,'print':lambda *a:None}
        exec(compile(module,'<push-dispatch-mock-only>','exec'),namespace)
        namespace['send_push_notification']('synthetic','synthetic')
        self.assertEqual(sent,['ExponentPushToken[synthetic-B]'])
        sent.clear();namespace['get_all_user_tokens']=lambda:[]
        namespace['send_push_notification']('synthetic','synthetic');self.assertEqual(sent,[])


if __name__=='__main__':unittest.main()
