"""Actual bridge/worker coordinator with synthetic documents/Auth only."""
import copy
from datetime import datetime, timezone
import unittest
from account_deletion import DeletionService, GRACE_SECONDS, Disabled, InvalidRequest, TemporaryFailure
from deletion_adapters import FirestoreJobStore, FirestoreProfiles
from deletion_memory import MemoryAuth
from spark_intake import SparkIntentBridge, SparkAuthState
from spark_batch import run_intent_batch
from notification_policy import push_eligible
from test_deletion_boundary import FakeDocuments


class Queue:
    synthetic_only = True
    def __init__(self, documents): self.documents=documents; self.calls=[]; self.override=None
    def pending_page(self, *, limit, after):
        self.calls.append((limit, after))
        if self.override is not None: return self.override, None
        # Reference query only; a real bounded Firestore query is not bound here.
        uids=sorted(k.split('/')[1] for k,v in self.documents.docs.items()
                    if k.startswith('accountDeletionIntents/') and k.count('/')==1 and
                    v.get('status') in {'grace_period','cancel_requested','processing','retry_wait'})
        uids=[u for u in uids if after is None or u>after]
        return uids[:limit], uids[limit-1] if len(uids)>limit else None


class SparkBatchTests(unittest.TestCase):
    def setUp(self):
        self.start=self.now=1800000000; self.docs=FakeDocuments(); self.auth=MemoryAuth()
        self.lookups=[]; self.fail_lookup=False
        def lookup(uid):
            self.lookups.append(uid)
            if self.fail_lookup: raise TemporaryFailure('synthetic identity unavailable')
            return SparkAuthState(self.auth.users[uid], self.auth.valid_after.get(uid,0), uid in self.auth.disabled)
        self.bridge=SparkIntentBridge(self.docs,lookup,enabled=True)
        self.worker=DeletionService(FirestoreJobStore(self.docs),self.auth,FirestoreProfiles(self.docs),lambda:self.now,mode='mock')
        self.queue=Queue(self.docs); self.seed('A',1)
    def seed(self, uid, n):
        self.auth.seed(uid,self.start,created_at=1000)
        rid=f'00000000-0000-4000-8000-{n:012}'
        self.docs.docs['users/'+uid]={'deletionRequest':{'requestId':rid},'pushToken':'synthetic'}
        self.docs.docs['accountDeletionIntents/'+uid]=dict(version=2,requestId=rid,status='grace_period',
             requestedAt=datetime.fromtimestamp(self.start,timezone.utc),consentAuthTime=self.start)
        return rid
    def run_batch(self, **kw): return run_intent_batch(self.queue,self.bridge,self.worker,enabled=True,mode='process',**kw)
    def cancel(self, uid='A'):
        self.docs.docs['accountDeletionIntents/'+uid].update(status='cancel_requested',
            cancelRequestedAt=datetime.fromtimestamp(self.start+1,timezone.utc),cancelAuthTime=self.start+1)
    def due(self):self.now=self.start+GRACE_SECONDS

    def test_default_disabled_and_inspection_never_write_or_lookup_auth(self):
        self.due(); before=copy.deepcopy(self.docs.docs)
        self.assertFalse(run_intent_batch(self.queue,self.bridge,self.worker)['enabled'])
        self.assertEqual(self.queue.calls,[])
        result=run_intent_batch(self.queue,self.bridge,self.worker,enabled=True)
        self.assertEqual(result['examined'],1);self.assertEqual(self.docs.docs,before)
        self.assertEqual(self.lookups,[]);self.assertEqual(self.auth.calls,[])

    def test_live_mixed_databases_invalid_mode_and_unbounded_pages_fail_closed(self):
        self.queue.synthetic_only=False
        with self.assertRaises(Disabled):self.run_batch()
        self.queue.synthetic_only=True; self.queue.documents=FakeDocuments()
        with self.assertRaises(Disabled):self.run_batch()
        self.queue.documents=self.docs
        for limit in [True,0,26]:
            with self.assertRaises(InvalidRequest):self.run_batch(limit=limit)
        self.queue.override=['A']*26
        with self.assertRaises(InvalidRequest):self.run_batch()
        self.assertEqual(self.auth.calls,[])

    def test_due_complete_duplicate_input_and_rerun_do_not_delete_again(self):
        self.due();self.queue.override=['A','A']
        result=self.run_batch();self.assertEqual(result['examined'],1);self.assertEqual(result['completed'],1)
        self.assertEqual(self.auth.calls,['A']);self.assertNotIn('users/A',self.docs.docs)
        self.assertIn('users/A/unknown/item',self.docs.docs)
        self.run_batch();self.assertEqual(self.auth.calls,['A'])
        self.assertTrue(self.worker.store.blocked('A'))

    def test_unmatured_request_is_not_extended_or_materialized(self):
        before=copy.deepcopy(self.docs.docs)
        self.assertEqual(self.run_batch()['waiting'],1)
        self.assertEqual(self.docs.docs,before);self.assertEqual(self.lookups,[])

    def test_explicit_cancel_before_due_runs_first_and_cancel_only_never_deletes(self):
        self.seed('B',2);self.cancel('B');self.due()
        result=run_intent_batch(self.queue,self.bridge,self.worker,enabled=True,mode='confirm_cancellations')
        self.assertEqual(result['cancelled'],1);self.assertEqual(self.lookups,['B'])
        self.assertEqual(self.auth.calls,[]);self.assertTrue(push_eligible(self.docs.read('users/B')))
        self.assertFalse(push_eligible(self.docs.read('users/A')))

    def test_failed_cancel_commit_keeps_block_until_next_batch(self):
        self.cancel();self.docs.fail_commit=True
        self.assertEqual(self.run_batch()['failed'],1)
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'cancel_requested')
        self.assertFalse(push_eligible(self.docs.read('users/A')))
        self.docs.fail_commit=False;self.assertEqual(self.run_batch()['cancelled'],1)
        self.assertEqual(self.auth.calls,[])

    def test_lost_completion_reflection_recovered_without_auth_lookup_or_delete(self):
        self.due(); original=self.bridge.reflect
        self.bridge.reflect=lambda _: (_ for _ in ()).throw(TemporaryFailure('synthetic lost reflection'))
        self.assertEqual(self.run_batch()['failed'],1)
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'processing')
        self.assertEqual(self.auth.calls,['A'])
        self.bridge.reflect=original;self.fail_lookup=True
        result=self.run_batch();self.assertEqual(result['completed'],1)
        self.assertEqual(self.auth.calls,['A']);self.assertEqual(self.lookups,['A'])

    def test_partial_profile_failure_respects_backoff_and_retries_without_auth_lookup(self):
        self.due();self.docs.fail_delete=True
        self.assertEqual(self.run_batch()['completed'],0)
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'retry_wait')
        self.assertNotIn('A',self.auth.users);self.assertIn('users/A',self.docs.docs)
        self.fail_lookup=True;self.docs.fail_delete=False
        self.assertEqual(self.run_batch()['waiting'],1)
        self.now+=2;self.assertEqual(self.run_batch()['completed'],1)
        self.assertEqual(self.auth.calls,['A'])

    def test_cancel_winning_after_query_prevents_materialization(self):
        self.due();original=self.bridge.materialize_due
        def race(uid,clock):self.cancel(uid);return original(uid,clock)
        self.bridge.materialize_due=race
        self.assertEqual(self.run_batch()['waiting'],1);self.assertEqual(self.auth.calls,[])
        self.assertEqual(self.run_batch()['cancelled'],1)

    def test_revocation_and_corrupt_job_do_not_erase_user(self):
        self.due();self.auth.valid_after['A']=(self.start+1)*1000
        self.assertEqual(self.run_batch()['errors'],{'authentication':1})
        self.assertEqual(self.auth.calls,[])
        self.auth.valid_after.clear();rid=self.docs.read('accountDeletionIntents/A')['requestId']
        self.docs.docs['accountDeletionJobs/'+rid]={'id':rid,'uid':'OTHER','status':'completed'}
        self.assertEqual(self.run_batch()['errors'],{'manual_review':1});self.assertEqual(self.auth.calls,[])

    def test_one_failure_does_not_drop_other_request_and_next_page_is_bounded(self):
        self.seed('B',2);self.seed('C',3);self.due()
        self.docs.docs['accountDeletionIntents/A']['requestId']='invalid'
        first=self.run_batch(limit=2);self.assertEqual(first['failed'],1);self.assertEqual(first['completed'],1)
        self.assertEqual(first['nextCursor'],'B')
        second=self.run_batch(limit=2,after=first['nextCursor']);self.assertEqual(second['completed'],1)
        self.assertIn('A',self.auth.users);self.assertEqual(self.auth.calls,['B','C'])

    def test_one_intent_read_failure_does_not_discard_other_rows(self):
        self.seed('B',2);self.due();original=self.docs.read
        def read(path):
            if path=='accountDeletionIntents/A':raise TemporaryFailure('synthetic read unavailable')
            return original(path)
        self.docs.read=read
        result=self.run_batch()
        self.assertEqual(result['examined'],2);self.assertEqual(result['errors'],{'temporary':1})
        self.assertEqual(result['completed'],1);self.assertEqual(self.auth.calls,['B'])
        self.docs.read=original
        self.assertEqual(self.run_batch()['completed'],1);self.assertEqual(self.auth.calls,['B','A'])


if __name__=='__main__':unittest.main()
