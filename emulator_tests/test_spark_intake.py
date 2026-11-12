"""New local bridge only; no production credentials or worker binding."""
import time, uuid, unittest
from datetime import datetime, timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from account_deletion import Disabled, IdentityChanged, TemporaryFailure
from emulator_only import EmulatorRuntime
from spark_intake import SparkIntentBridge, SparkAuthState
from notification_policy import push_eligible

class SparkBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now=int(time.time());self.runtime=EmulatorRuntime(lambda:self.now);self.addCleanup(self.runtime.close)
        self.uid='synthetic-spark-'+uuid.uuid4().hex;self.rid=str(uuid.uuid4())
        # Synthetic Auth side effects only: reuse existing mock, real Firestore transactions.
        from deletion_memory import MemoryAuth
        self.auth=MemoryAuth();self.auth.seed(self.uid,self.now,created_at=(self.now-1000000)*1000)
        self.auth.emulator_only=True;self.runtime.service.auth=self.auth
        self.bridge=SparkIntentBridge(self.runtime.documents,lambda uid:SparkAuthState(self.auth.users[uid],self.auth.valid_after.get(uid,0),uid in self.auth.disabled) if uid in self.auth.users else None,enabled=True)
        self.ref=self.runtime.client.document('accountDeletionIntents/'+self.uid)
        self.ref.set(dict(version=2,requestId=self.rid,requestedAt=datetime.fromtimestamp(self.now,timezone.utc),status='grace_period',confirmation='DELETE_MY_ACCOUNT',consentAuthTime=self.now,profilePresent=True,cancelledAt=None))
        self.runtime.client.document('users/'+self.uid).set({'pushToken':'synthetic-unusable','deletionRequest':{'requestId':self.rid}})
    def test_disabled_and_not_due_never_create_job(self):
        with self.assertRaises(Disabled):SparkIntentBridge(self.runtime.documents,lambda _:None).materialize_due(self.uid,lambda:self.now)
        self.assertIsNone(self.bridge.materialize_due(self.uid,lambda:self.now))
        self.now+=604799;self.assertIsNone(self.bridge.materialize_due(self.uid,lambda:self.now))
        self.assertIsNone(self.runtime.store.get(self.rid))
    def test_due_materialization_concurrent_idempotency_and_existing_worker_completion(self):
        self.now+=604800
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(lambda _:self.bridge.materialize_due(self.uid,lambda:self.now),range(2)))
        self.assertEqual(jobs[0]['id'],jobs[1]['id']);self.assertEqual(jobs[0]['deleteAfter'],self.now)
        self.assertFalse(push_eligible(self.runtime.documents.read('users/'+self.uid)))
        self.assertEqual(self.runtime.service.run_once(self.rid)['status'],'completed')
        self.bridge.reflect(self.rid);self.assertEqual(self.ref.get().to_dict()['status'],'completed')
        self.assertIsNone(self.runtime.documents.read('users/'+self.uid));self.assertTrue(self.runtime.store.blocked(self.uid))
        self.assertIsNone(self.runtime.service.run_once(self.rid))
    def test_cancelled_intent_and_reused_uid_cannot_materialize(self):
        self.now+=604800;self.ref.update({'status':'cancelled'})
        self.assertIsNone(self.bridge.materialize_due(self.uid,lambda:self.now))
        self.ref.update({'status':'grace_period'});self.auth.users[self.uid]=self.now*1000
        with self.assertRaises(IdentityChanged):self.bridge.materialize_due(self.uid,lambda:self.now)
        self.assertIsNone(self.runtime.store.get(self.rid))
    def test_failure_after_auth_keeps_tombstone_and_retries_same_job(self):
        self.now+=604800;self.bridge.materialize_due(self.uid,lambda:self.now)
        with patch.object(self.runtime.profiles,'delete_user_document',side_effect=TemporaryFailure('synthetic fault')):
            job=self.runtime.service.run_once(self.rid)
        self.assertEqual(job['status'],'retry_wait');self.assertNotIn(self.uid,self.auth.users)
        self.bridge.reflect(self.rid);self.assertEqual(self.ref.get().to_dict()['status'],'retry_wait')
        self.now+=4;self.assertEqual(self.runtime.service.run_once(self.rid)['status'],'completed')
        self.bridge.reflect(self.rid);self.assertEqual(self.ref.get().to_dict()['status'],'completed')

    def test_pending_cancellation_remains_blocked_until_atomic_server_confirmation(self):
        accepted=self.now
        self.ref.update({'status':'cancel_requested','cancelRequestedAt':datetime.fromtimestamp(accepted+1,timezone.utc),'cancelAuthTime':accepted+1})
        self.now+=604800
        self.assertIsNone(self.bridge.materialize_due(self.uid,lambda:self.now))
        self.assertFalse(push_eligible(self.runtime.documents.read('users/'+self.uid)))
        result=self.bridge.confirm_cancellation(self.uid,lambda:self.now)
        self.assertEqual(result['status'],'cancelled');self.assertFalse(self.runtime.store.blocked(self.uid))
        self.assertTrue(push_eligible(self.runtime.documents.read('users/'+self.uid)))

    def test_new_request_can_cancel_after_previous_confirmed_cancellation(self):
        first = self.rid
        self.ref.update({'status':'cancel_requested','cancelRequestedAt':datetime.fromtimestamp(self.now+1,timezone.utc),'cancelAuthTime':self.now+1})
        self.assertEqual(self.bridge.confirm_cancellation(self.uid,lambda:self.now+2)['status'],'cancelled')
        self.now += 10; self.rid = str(uuid.uuid4())
        self.ref.set(dict(version=2,requestId=self.rid,requestedAt=datetime.fromtimestamp(self.now,timezone.utc),status='cancel_requested',
            confirmation='CANCEL_ACCOUNT_DELETION',consentAuthTime=self.now,profilePresent=True,cancelledAt=None,
            cancelRequestedAt=datetime.fromtimestamp(self.now+1,timezone.utc),cancelAuthTime=self.now+1))
        self.runtime.client.document('users/'+self.uid).update({'deletionRequest':{'requestId':self.rid}})
        self.assertEqual(self.runtime.documents.read('accountDeletionState/'+self.uid)['jobId'],first)
        self.assertEqual(self.bridge.confirm_cancellation(self.uid,lambda:self.now+2)['status'],'cancelled')
        self.assertEqual(self.runtime.documents.read('accountDeletionState/'+self.uid),{'jobId':self.rid,'blocked':False})
        self.assertTrue(push_eligible(self.runtime.documents.read('users/'+self.uid)))
        self.assertIsNone(self.runtime.store.get(self.rid)); self.assertEqual(self.auth.calls,[])

if __name__=='__main__':unittest.main()
