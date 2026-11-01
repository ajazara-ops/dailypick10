"""Pure synthetic transactions/Auth. No emulator, network, credentials or live CLI."""
import copy
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from account_deletion import DeletionService, GRACE_SECONDS, Unauthorized, PermissionFailure, IdentityChanged, TemporaryFailure, CancellationClosed
from deletion_adapters import FirestoreJobStore, FirestoreProfiles, FirebaseAuthAdapter
from deletion_memory import MemoryAuth
from notification_policy import push_eligible
from spark_intake import SparkIntentBridge, SparkAuthState
from test_deletion_boundary import FakeDocuments, FakeAdmin


def stamp(value): return datetime.fromtimestamp(value, timezone.utc)


class SafeSparkTests(unittest.TestCase):
    def setUp(self):
        self.start = self.now = 1800000000
        self.rid = '00000000-0000-4000-8000-000000000001'
        self.docs = FakeDocuments()
        self.auth = MemoryAuth(); self.auth.seed('A', self.now, created_at=1000)
        self.identity = SparkAuthState(1000, 0)
        self.lookup_error = None
        def lookup(uid):
            if self.lookup_error: raise self.lookup_error
            return self.identity
        self.bridge = SparkIntentBridge(self.docs, lookup, enabled=True)
        self.docs.docs['accountDeletionIntents/A'] = dict(version=2, requestId=self.rid, requestedAt=stamp(self.start), status='grace_period',
            consentAuthTime=self.start, confirmation='DELETE_MY_ACCOUNT', profilePresent=True, cancelledAt=None)
        self.docs.docs['users/A']['deletionRequest'] = {'requestId':self.rid}
        self.store = FirestoreJobStore(self.docs)
        self.worker = DeletionService(self.store,self.auth,FirestoreProfiles(self.docs),lambda:self.now,mode='mock')
    def pending(self, when=None):
        when = self.start + 1 if when is None else when
        self.docs.docs['accountDeletionIntents/A'].update(status='cancel_requested', cancelRequestedAt=stamp(when), cancelAuthTime=when, confirmation='CANCEL_ACCOUNT_DELETION')
    def due(self): self.now=self.start+GRACE_SECONDS
    def materialize(self): return self.bridge.materialize_due('A', lambda:self.now)
    def confirm(self): return self.bridge.confirm_cancellation('A', lambda:self.now)
    def assert_blocked(self):
        self.assertFalse(push_eligible(self.docs.read('users/A')))
        self.assertNotEqual(self.docs.read('accountDeletionIntents/A')['status'],'cancelled')

    def test_pending_blocks_materialization_and_push_until_trusted_confirmation(self):
        self.pending(); self.due(); self.assertIsNone(self.materialize()); self.assert_blocked()
        self.assertEqual(self.confirm()['status'],'cancelled')
        self.assertFalse(self.store.blocked('A')); self.assertTrue(push_eligible(self.docs.read('users/A')))
        self.assertIn('A',self.auth.users); self.assertEqual(self.auth.calls,[])

    def test_cancel_new_request_after_prior_confirmed_cancellation(self):
        self.pending(); self.assertEqual(self.confirm()['status'], 'cancelled')
        old_id = self.rid
        self.rid = '00000000-0000-4000-8000-000000000002'
        self.start += 10; self.now = self.start
        self.docs.docs['accountDeletionIntents/A'] = dict(version=2, requestId=self.rid, requestedAt=stamp(self.start),
            status='grace_period', consentAuthTime=self.start, confirmation='DELETE_MY_ACCOUNT', profilePresent=True, cancelledAt=None)
        self.docs.docs['users/A']['deletionRequest'] = {'requestId': self.rid}
        self.assertEqual(self.docs.read('accountDeletionState/A'), {'jobId': old_id, 'blocked': False})
        self.pending(); self.assertEqual(self.confirm()['status'], 'cancelled')
        self.assertEqual(self.docs.read('accountDeletionState/A'), {'jobId': self.rid, 'blocked': False})
        self.assertTrue(push_eligible(self.docs.read('users/A'))); self.assertEqual(self.auth.calls, [])

    def test_new_cancel_never_overrides_another_active_or_invalid_lifecycle(self):
        self.pending()
        old_id = '00000000-0000-4000-8000-000000000099'
        for blocked, old_job in [(True, None), (None, None), (False, {'uid': 'A', 'status': 'processing'}),
                                 (False, {'uid': 'B', 'status': 'cancelled'}), (False, {'uid': 'A', 'status': 'completed'})]:
            with self.subTest(blocked=blocked, old_job=old_job):
                self.docs.docs['accountDeletionState/A'] = {'jobId': old_id, 'blocked': blocked}
                if old_job is None: self.docs.docs.pop('accountDeletionJobs/' + old_id, None)
                else: self.docs.docs['accountDeletionJobs/' + old_id] = old_job
                before = copy.deepcopy(self.docs.docs)
                with self.assertRaises(CancellationClosed): self.confirm()
                self.assertEqual(self.docs.docs, before)

    def test_new_cancel_accepts_prior_cancelled_job_for_same_uid(self):
        self.pending()
        old_id = '00000000-0000-4000-8000-000000000099'
        old_job = {'uid': 'A', 'status': 'cancelled'}
        self.docs.docs['accountDeletionState/A'] = {'jobId': old_id, 'blocked': False}
        self.docs.docs['accountDeletionJobs/' + old_id] = old_job.copy()
        self.assertEqual(self.confirm()['status'], 'cancelled')
        self.assertEqual(self.docs.read('accountDeletionJobs/' + old_id), old_job)
        self.assertFalse(self.store.blocked('A')); self.assertEqual(self.auth.calls, [])

    def test_predeadline_cancellation_is_honoured_even_if_confirmation_runs_after_deadline(self):
        self.pending(self.start+GRACE_SECONDS-1); self.now=self.start+GRACE_SECONDS+7200
        self.assertEqual(self.confirm()['status'],'cancelled'); self.assertIsNone(self.materialize())

    def test_at_deadline_or_after_cannot_confirm_cancellation(self):
        for offset in [GRACE_SECONDS,GRACE_SECONDS+1]:
            self.pending(self.start+offset)
            with self.assertRaises(CancellationClosed): self.confirm()
            self.assert_blocked()

    def test_nanosecond_cancellation_boundary_uses_exact_server_timestamps(self):
        class Nano:
            microsecond=0
            def __init__(self,second,nano):self.second=second;self.nanosecond=nano
            def utctimetuple(self):return stamp(self.second).utctimetuple()
        self.pending(self.start+GRACE_SECONDS)
        intent=self.docs.docs['accountDeletionIntents/A']
        intent['requestedAt']=Nano(self.start,1)
        intent['cancelRequestedAt']=Nano(self.start+GRACE_SECONDS,1)
        with self.assertRaises(CancellationClosed):self.confirm()
        intent['cancelRequestedAt']=Nano(self.start+GRACE_SECONDS,0)
        self.assertEqual(self.confirm()['status'],'cancelled')

    def test_claim_wins_then_late_cancel_cannot_unblock_or_change_job(self):
        self.due(); job=self.materialize()
        with self.assertRaises(CancellationClosed): self.confirm()
        self.assertEqual(self.store.get(self.rid),job); self.assertTrue(self.store.blocked('A'))
        # Even a malformed admin-written cancel signal must not undo a durable job.
        self.pending(self.start+GRACE_SECONDS-1)
        with self.assertRaises(CancellationClosed): self.confirm()
        self.assertTrue(self.store.blocked('A'))

    def test_duplicate_concurrent_confirmations_do_not_create_or_extend_a_request(self):
        self.pending(); self.now=self.start+10
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.confirm(),range(2)))
        self.assertEqual(results[0],results[1]); self.assertEqual(results[0]['requestedAt'],stamp(self.start))
        self.assertEqual(results[0]['requestId'],self.rid); self.assertIsNone(self.store.get(self.rid))

    def test_missing_profile_cancellation_does_not_recreate_deleted_data(self):
        self.pending(); self.docs.docs.pop('users/A')
        self.assertEqual(self.confirm()['status'],'cancelled'); self.assertIsNone(self.docs.read('users/A'))
        self.assertIn('users/A/unknown/item',self.docs.docs)

    def test_cancel_commit_failure_rolls_back_marker_and_status_then_same_request_retries(self):
        self.pending(); before=copy.deepcopy(self.docs.docs); self.docs.fail_commit=True
        with self.assertRaises(TemporaryFailure): self.confirm()
        self.assertEqual(self.docs.docs,before); self.docs.fail_commit=False
        self.assertEqual(self.confirm()['status'],'cancelled')

    def test_revoked_disabled_missing_and_changed_auth_never_confirm_cancel(self):
        self.pending()
        for identity in [None,SparkAuthState(1000,(self.start+2)*1000),SparkAuthState(1000,0,True),SparkAuthState((self.start+1)*1000,0)]:
            self.identity=identity
            with self.assertRaises((Unauthorized,IdentityChanged)): self.confirm()
            self.assert_blocked()

    def test_auth_lookup_outage_leaves_pending_state_and_no_deletion(self):
        self.pending(); self.lookup_error=TemporaryFailure('synthetic offline')
        with self.assertRaises(TemporaryFailure): self.confirm()
        self.assert_blocked(); self.assertEqual(self.auth.calls,[])

    def test_missing_consent_legacy_or_revoked_request_cannot_materialize(self):
        self.due()
        for changes in [{'version':1},{'consentAuthTime':None},{'consentAuthTime':self.start-300}]:
            old=copy.deepcopy(self.docs.docs['accountDeletionIntents/A']); self.docs.docs['accountDeletionIntents/A'].update(changes)
            with self.assertRaises(Unauthorized): self.materialize()
            self.docs.docs['accountDeletionIntents/A']=old
        self.identity=SparkAuthState(1000,(self.start+1)*1000)
        with self.assertRaises(Unauthorized): self.materialize()
        self.assertIsNone(self.store.get(self.rid)); self.assertEqual(self.auth.calls,[])

    def test_revocation_after_materialization_is_checked_again_before_auth_delete(self):
        self.due(); self.materialize(); self.auth.valid_after['A']=(self.start+1)*1000
        result=self.worker.run_once(self.rid)
        self.assertEqual(result['status'],'needs_review'); self.assertIn('A',self.auth.users)
        self.assertEqual(self.auth.calls,[]); self.assertTrue(self.store.blocked('A'))

    def test_partial_auth_then_profile_failure_stays_incomplete_and_retries_same_job(self):
        self.due(); self.materialize(); self.docs.fail_delete=True
        result=self.worker.run_once(self.rid); self.bridge.reflect(self.rid)
        self.assertEqual(result['status'],'retry_wait'); self.assertNotIn('A',self.auth.users)
        self.assertIsNotNone(self.docs.read('users/A')); self.assertTrue(self.store.blocked('A'))
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'retry_wait')
        self.docs.fail_delete=False; self.now+=4
        result=self.worker.run_once(self.rid); self.bridge.reflect(self.rid)
        self.assertEqual(result['status'],'completed'); self.assertIsNone(self.docs.read('users/A'))
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'completed')
        self.assertTrue(self.store.blocked('A')); self.assertIn('users/A/unknown/item',self.docs.docs)

    def test_absent_consent_verifier_cannot_fall_back_to_generation_only(self):
        self.due(); self.materialize(); self.auth.verify_recorded_consent=None
        self.assertEqual(self.worker.run_once(self.rid)['status'],'needs_review'); self.assertEqual(self.auth.calls,[])

    def test_reflected_completion_requires_both_durable_completed_status_and_phase(self):
        self.due(); self.materialize()
        job = self.docs.docs['accountDeletionJobs/' + self.rid]
        job.update(status='retry_wait', phase='profile')
        reflected = self.bridge.reflect(self.rid)
        self.assertEqual(reflected['phase'], 'profile'); self.assertEqual(reflected['completedScope'], [])
        self.docs.docs['accountDeletionJobs/' + self.rid].update(status='completed', phase='profile')
        self.assertEqual(self.bridge.reflect(self.rid)['completedScope'], [])
        self.docs.docs['accountDeletionJobs/' + self.rid].update(phase='complete')
        self.assertEqual(self.bridge.reflect(self.rid)['completedScope'], ['firebase-auth','users-document'])

    def test_sdk_shaped_auth_adapter_checks_current_revocation_and_disabled_state(self):
        sdk=FakeAdmin(self.start); sdk.users['A'].tokens_valid_after_timestamp=0
        adapter=FirebaseAuthAdapter(sdk,'demo-deletion')
        adapter.verify_recorded_consent('A',1000,self.start)
        sdk.users['A'].tokens_valid_after_timestamp=(self.start+1)*1000
        with self.assertRaises(PermissionFailure): adapter.verify_recorded_consent('A',1000,self.start)
        sdk.users['A']=SimpleNamespace(disabled=True,user_metadata=SimpleNamespace(creation_timestamp=1000),tokens_valid_after_timestamp=0)
        with self.assertRaises(PermissionFailure): adapter.verify_recorded_consent('A',1000,self.start)


if __name__=='__main__':unittest.main()
