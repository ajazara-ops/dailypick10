"""Connected cursor -> actual bridge/worker, using synthetic documents/Auth only."""
import copy
import json
import sqlite3
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest

from account_deletion import (DeletionService, GRACE_SECONDS, Disabled,
    InvalidRequest, TemporaryFailure, LeaseLost, Unauthorized)
from deletion_adapters import FirestoreJobStore, FirestoreProfiles
from deletion_memory import MemoryAuth
from notification_policy import push_eligible
from spark_batch import ACTIVE
from spark_intake import SparkIntentBridge, SparkAuthState
from spark_runtime import (SparkBatchRuntime, FirestoreBatchCursor, FirestoreIntentQueue,
                           LEASE_SECONDS)
from test_deletion_boundary import FakeDocuments


class Documents(FakeDocuments):
    def __init__(self):
        super().__init__()
        self.queries = []; self.query_failure = False; self.query_override = None
        self.finish_failure = None

    def pending_intents(self, *, statuses, limit, after):
        self.queries.append((statuses, limit, after))
        if self.query_failure: raise TemporaryFailure('synthetic query failed')
        if self.query_override is not None: return self.query_override
        with self.lock:
            return sorted(k.split('/')[1] for k, v in self.docs.items()
                          if k.startswith('accountDeletionIntents/') and k.count('/') == 1
                          and v.get('status') in statuses and (after is None or k.split('/')[1] > after))[:limit]

    def transaction(self, fn):
        is_finish = False
        def run(tx):
            nonlocal is_finish
            value = fn(tx)
            is_finish = (isinstance(value, dict) and set(value) == {'version','after','cycle','revision','lease','leaseUntil'}
                         and value['lease'] is None and value['revision'] > 0)
            if is_finish and self.finish_failure == 'before': raise TemporaryFailure('synthetic commit lost before')
            return value
        result = super().transaction(run)
        if is_finish and self.finish_failure == 'after': raise TemporaryFailure('synthetic commit response lost')
        return result


class DiskDocuments(Documents):
    """Synthetic test fixture backed by one SQLite row; not a runtime adapter."""
    def __init__(self, path):
        super().__init__()
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS fixture(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        if self.db.execute('SELECT 1 FROM fixture WHERE id=1').fetchone():self.reload()
        else:self.persist()
    def persist(self):
        def encode(value):
            if isinstance(value,datetime):return {'__synthetic_datetime':value.isoformat()}
            raise TypeError('Unsupported synthetic fixture')
        body=json.dumps(self.docs,default=encode)
        self.db.execute('INSERT OR REPLACE INTO fixture VALUES(1,?)',(body,))
    def reload(self):
        def decode(value):
            return datetime.fromisoformat(value['__synthetic_datetime']) if set(value)=={'__synthetic_datetime'} else value
        self.docs=json.loads(self.db.execute('SELECT body FROM fixture WHERE id=1').fetchone()[0],object_hook=decode)
    def read(self,path):self.reload();return super().read(path)
    def pending_intents(self,**kwargs):self.reload();return super().pending_intents(**kwargs)
    def transaction(self,fn):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.reload();result=super().transaction(fn);self.persist();self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK');self.reload();raise
    def close(self):self.db.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.docs = Documents(); self.auth = MemoryAuth(); self.lookups = []
        def lookup(uid):
            self.lookups.append(uid)
            if uid not in self.auth.users: raise Unauthorized('Synthetic Auth absent')
            return SparkAuthState(self.auth.users[uid], self.auth.valid_after.get(uid, 0), uid in self.auth.disabled)
        self.bridge = SparkIntentBridge(self.docs, lookup, enabled=True)
        self.worker = DeletionService(FirestoreJobStore(self.docs), self.auth,
            FirestoreProfiles(self.docs), lambda: self.now, mode='mock')
        self.cursor = FirestoreBatchCursor(self.docs, 'process')

    def runtime(self, enabled=True): return SparkBatchRuntime(self.docs, self.bridge, self.worker, enabled=enabled)
    def run_page(self, **kw): return self.runtime().run_page(mode='process', **kw)
    def seed(self, uid, n, *, due=True, cancel=False):
        accepted = self.now - GRACE_SECONDS if due else self.now
        rid = f'00000000-0000-4000-8000-{n:012}'
        self.auth.seed(uid, accepted, created_at=1000)
        intent = dict(version=2, requestId=rid, status='cancel_requested' if cancel else 'grace_period',
            requestedAt=datetime.fromtimestamp(accepted, timezone.utc), consentAuthTime=accepted)
        if cancel: intent.update(cancelRequestedAt=datetime.fromtimestamp(accepted+1, timezone.utc),cancelAuthTime=accepted+1)
        self.docs.docs['accountDeletionIntents/'+uid] = intent
        self.docs.docs['users/'+uid] = {'pushToken':'synthetic-unusable','deletionRequest':{'requestId':rid}}
        return rid

    def test_default_disabled_and_inspection_never_create_cursor_or_touch_auth(self):
        self.seed('A',1); before=copy.deepcopy(self.docs.docs)
        self.assertEqual(self.runtime(False).run_page()['state'], 'disabled')
        self.assertEqual(self.docs.queries, [])
        result=self.runtime().run_page()
        self.assertEqual(result['state'],'inspected');self.assertEqual(result['examined'],1)
        self.assertEqual(before,self.docs.docs);self.assertEqual(self.auth.calls,[]);self.assertEqual(self.lookups,[])
        self.assertNotIn('nextCursor',result)

    def test_restart_resumes_after_future_item_and_wrap_revisits_newly_due_item(self):
        first=self.seed('A',1,due=False); self.seed('B',2)
        self.assertEqual(self.run_page(limit=1)['waiting'],1)
        self.assertEqual(self.cursor.read()['after'],'A')
        self.assertIsNone(self.worker.store.get(first))
        self.assertEqual(self.run_page(limit=1)['completed'],1)
        self.assertEqual(self.cursor.read()['after'],'B')
        self.assertEqual(self.run_page(limit=1)['examined'],0)  # Short final page wraps.
        self.now += GRACE_SECONDS
        self.assertEqual(self.run_page(limit=1)['completed'],1)
        self.assertEqual(self.auth.calls,['B','A'])
        self.assertEqual(self.worker.store.get(first)['deleteAfter'],self.now)

    def test_query_failure_is_not_an_empty_success_or_cursor_reset(self):
        self.seed('A',1);self.docs.query_failure=True
        self.assertEqual(self.run_page()['state'],'unconfirmed')
        self.assertEqual(self.cursor.read()['revision'],0)
        self.docs.query_failure=False
        self.assertEqual(self.run_page()['state'],'busy')
        self.assertEqual(self.auth.calls,[])
        self.now += LEASE_SECONDS
        self.assertEqual(self.run_page()['completed'],1)

    def test_cursor_commit_failure_and_lost_response_are_recovered_without_reset(self):
        for when in ['before','after']:
            with self.subTest(when=when):
                self.setUp();self.seed('A',1);self.docs.finish_failure=when
                self.assertEqual(self.run_page(limit=1)['state'],'unconfirmed')
                self.assertEqual(self.auth.calls,['A'])
                state=self.cursor.read()
                self.assertEqual(state['revision'],0 if when=='before' else 1)
                self.docs.finish_failure=None;self.now += LEASE_SECONDS
                self.assertEqual(self.run_page(limit=1)['state'],'processed')
                self.assertEqual(self.auth.calls,['A'])
                self.assertEqual(self.docs.read('accountDeletionIntents/A')['phase'],'complete')

    def test_partial_profile_failure_and_lost_reflection_survive_new_runtime(self):
        rid=self.seed('A',1);self.docs.fail_delete=True
        original=self.bridge.reflect
        self.bridge.reflect=lambda _: (_ for _ in ()).throw(TemporaryFailure('synthetic response loss'))
        result=self.run_page();self.assertEqual(result['failed'],1)
        self.assertEqual(self.worker.store.get(rid)['status'],'retry_wait')
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'processing')
        self.assertFalse(push_eligible(self.docs.read('users/A')))
        self.bridge.reflect=original;self.docs.fail_delete=False
        self.assertEqual(self.run_page()['waiting'],1)
        self.now += 2
        self.assertEqual(self.run_page()['completed'],1)
        self.assertEqual(self.lookups,['A']);self.assertEqual(self.auth.calls,['A'])
        with self.assertRaises(Unauthorized):self.docs.client_write('A',{'late':'write'})
        self.assertIn('users/A/unknown/item',self.docs.docs)

    def test_explicit_cancellation_precedes_due_work_and_confirm_only_never_deletes(self):
        self.seed('A',1);self.seed('B',2,cancel=True)
        result=self.runtime().run_page(mode='confirm_cancellations')
        self.assertEqual(result['cancelled'],1);self.assertEqual(self.auth.calls,[])
        self.assertEqual(self.lookups,['B']);self.assertTrue(push_eligible(self.docs.read('users/B')))
        self.assertIsNone(self.docs.read(self.cursor.path))  # Separate cursor, no skipped process page.
        self.assertEqual(self.run_page()['completed'],1)

    def test_bad_row_advances_page_without_starving_other_users_and_reappears_on_wrap(self):
        self.seed('A',1);self.seed('B',2)
        self.docs.docs['accountDeletionIntents/A']['requestId']='bad'
        first=self.run_page(limit=1);self.assertEqual(first['failed'],1)
        self.assertEqual(self.run_page(limit=1)['completed'],1)
        self.run_page(limit=1)
        self.assertEqual(self.run_page(limit=1)['failed'],1)
        self.assertEqual(self.auth.calls,['B'])

    def test_concurrent_claim_only_one_owner_stale_finish_cannot_move_new_cursor(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(lambda _:self.cursor.claim(lambda:self.now),range(2)))
        self.assertEqual(sum(x is not None for x in claims),1)
        old=next(x for x in claims if x is not None)
        self.now += LEASE_SECONDS
        new=self.cursor.claim(lambda:self.now)
        with self.assertRaises(LeaseLost):self.cursor.finish(old,'A',lambda:self.now)
        self.cursor.finish(new,'B',lambda:self.now)
        self.assertEqual(self.cursor.read()['after'],'B')

    def test_expired_page_owner_stops_before_next_user(self):
        self.seed('A',1);self.seed('B',2)
        original=self.bridge.reflect
        def reflect(rid):
            out=original(rid);self.now += LEASE_SECONDS;return out
        self.bridge.reflect=reflect
        self.assertEqual(self.run_page()['state'],'unconfirmed')
        self.assertEqual(self.auth.calls,['A']);self.assertEqual(self.cursor.read()['revision'],0)
        self.bridge.reflect=original
        self.assertEqual(self.run_page()['completed'],1);self.assertEqual(self.auth.calls,['A','B'])

    def test_corrupt_cursor_and_query_results_fail_closed_without_reset(self):
        self.seed('A',1)
        for bad in [['A','A'], ['B','A'], ['../other'], [None], ['A']*26]:
            with self.subTest(bad=bad):
                self.docs.query_override=bad
                with self.assertRaises(InvalidRequest):FirestoreIntentQueue(self.docs).pending_page(limit=25,after=None)
        self.docs.query_override=None
        self.docs.docs[self.cursor.path]={'version':99,'after':'A'}
        before=copy.deepcopy(self.docs.docs)
        self.assertEqual(self.run_page()['state'],'unconfirmed')
        self.assertEqual(self.docs.docs,before);self.assertEqual(self.auth.calls,[])

    def test_non_synthetic_or_mixed_bindings_are_rejected_before_any_cursor_write(self):
        self.docs.synthetic_only=False
        with self.assertRaises(Disabled):self.runtime()
        self.docs.synthetic_only=True;self.bridge.documents=Documents()
        with self.assertRaises(Disabled):self.run_page()
        self.assertNotIn(self.cursor.path,self.docs.docs);self.assertEqual(self.docs.queries,[])

    def test_completed_job_reflection_is_discoverable_without_existing_auth(self):
        rid=self.seed('A',1);self.bridge.materialize_due('A',lambda:self.now)
        self.worker.run_once(rid)
        self.assertNotIn('A',self.auth.users)
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['status'],'processing')
        self.assertEqual(self.run_page()['completed'],1)
        self.assertEqual(self.lookups,['A']);self.assertEqual(self.auth.calls,['A'])
        self.assertEqual(self.docs.read('accountDeletionIntents/A')['completedScope'],['firebase-auth','users-document'])

    def test_durable_cursor_and_partial_job_resume_after_disk_store_reopen(self):
        parent=Path(__file__).resolve().parents[1]/'.synthetic-test-temp'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='synthetic-spark-pages-',dir=parent) as folder:
            assert Path(folder).resolve().parent == parent.resolve()  # Verify cleanup stays in test workspace.
            path=folder+'/fixture.sqlite'
            def bind(documents):
                self.docs=documents;self.bridge.documents=documents
                self.worker.store=FirestoreJobStore(documents);self.worker.profiles=FirestoreProfiles(documents)
                self.cursor=FirestoreBatchCursor(documents,'process')
            bind(DiskDocuments(path))
            try:
                self.seed('A',1,due=False);rid=self.seed('B',2);self.docs.persist()
                self.assertEqual(self.run_page(limit=1)['waiting'],1)
                self.docs.close();bind(DiskDocuments(path))
                self.assertEqual(self.cursor.read()['after'],'A')
                self.docs.fail_delete=True
                self.assertEqual(self.run_page(limit=1)['completed'],0)
                self.assertEqual(self.worker.store.get(rid)['phase'],'profile')
                self.assertEqual(self.auth.calls,['B'])
                self.docs.close();bind(DiskDocuments(path))
                self.assertEqual(self.cursor.read()['after'],'B')
                self.assertEqual(self.run_page(limit=1)['examined'],0)  # Wrap.
                self.now+=2
                self.assertEqual(self.run_page(limit=1)['waiting'],1)  # A still immature.
                self.assertEqual(self.run_page(limit=1)['completed'],1)
                self.assertEqual(self.auth.calls,['B'])
                self.assertEqual(self.docs.read('accountDeletionIntents/B')['phase'],'complete')
            finally:self.docs.close()


if __name__ == '__main__':unittest.main()
