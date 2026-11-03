"""Actual guarded SDK-shaped adapters, synthetic SDK objects only. No emulator/socket."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace, ModuleType
import sys
import unittest
from unittest.mock import patch

from account_deletion import DeletionService, GRACE_SECONDS, Disabled, TemporaryFailure, Unauthorized
from deletion_adapters import FirebaseAuthAdapter, FirestoreJobStore, FirestoreProfiles
from emulator_only import EmulatorDocuments
import spark_runtime
from spark_batch import ACTIVE
from test_deletion_boundary import FakeDocuments, FakeAdmin

RID='00000000-0000-4000-8000-000000000001'


class Guard:
    def __init__(self):self.calls=0;self.allowed=True
    def assert_environment(self):
        self.calls+=1
        if not self.allowed:raise Disabled('Synthetic guard denied')


class Ref:
    def __init__(self,client,path):self.client,self.path=client,path
    def get(self,transaction=None,timeout=None):
        assert timeout==5
        value=transaction.tx.get(self.path) if transaction else self.client.backend.read(self.path)
        return SimpleNamespace(id=self.path.split('/')[-1],exists=value is not None,to_dict=lambda:deepcopy(value))


class Query:
    def __init__(self,client,name):self.client,self.name=client,name;self.after=None
    def document(self,uid):return self.client.document(self.name+'/'+uid)
    def where(self,*,filter):self.filter=filter;self.client.query_calls.append(('where',filter.field_path,filter.op_string,filter.value));return self
    def order_by(self,field):assert field=='__name__';self.client.query_calls.append(('order',field));return self
    def start_after(self,values):
        assert set(values)=={'__name__'} and isinstance(values['__name__'],Ref)
        self.after=values['__name__'].path.split('/')[-1];self.client.query_calls.append(('after',self.after));return self
    def limit(self,n):self.n=n;self.client.query_calls.append(('limit',n));return self
    def stream(self,*,timeout):
        assert timeout==5 and self.name=='accountDeletionIntents'
        rows=sorted(k.split('/')[1] for k,v in self.client.backend.docs.items()
                    if k.startswith(self.name+'/') and k.count('/')==1 and v.get('status') in self.filter.value
                    and (self.after is None or k.split('/')[1]>self.after))[:self.n]
        for i,uid in enumerate(rows):
            if self.client.query_error and i==1:raise TemporaryFailure('Synthetic partial query')
            yield SimpleNamespace(id=uid)


class Client:
    def __init__(self,backend):self.backend=backend;self.query_calls=[];self.query_error=False
    def document(self,path):return Ref(self,path)
    def collection(self,name):return Query(self,name)
    def transaction(self,*,max_attempts):
        assert max_attempts==5
        class Tx:
            def set(self,ref,value):self.tx.set(ref.path,value)
            def delete(self,ref):self.tx.delete(ref.path)
        value=Tx();value.backend=self.backend;return value


def transactional(fn):
    def run(transaction):
        def apply(tx):transaction.tx=tx;return fn(transaction)
        return transaction.backend.transaction(apply)
    return run


class Admin(FakeAdmin):
    def __init__(self,now):
        super().__init__(now);self.calls=[];self.lookups=[]
        self.users['A'].tokens_valid_after_timestamp=0
    def get_user(self,uid):self.lookups.append(uid);return super().get_user(uid)
    def delete_user(self,uid):self.calls.append(uid);return super().delete_user(uid)


class SDKConnectionTests(unittest.TestCase):
    def setUp(self):
        self.now=1800000000;self.backend=FakeDocuments();self.guard=Guard();self.client=Client(self.backend)
        self.documents=EmulatorDocuments(self.guard,self.client,SimpleNamespace(transactional=transactional))
        self.sdk=Admin(self.now);self.auth=FirebaseAuthAdapter(self.sdk,'demo-deletion')
        self.worker=DeletionService(FirestoreJobStore(self.documents),self.auth,FirestoreProfiles(self.documents),lambda:self.now,mode='mock')
        field_module=ModuleType('google.cloud.firestore_v1.base_query')
        field_module.FieldFilter=lambda field,op,value:SimpleNamespace(field_path=field,op_string=op,value=value)
        self.enterContext(patch.dict(sys.modules,{'google.cloud.firestore_v1.base_query':field_module}))
    def seed(self,uid='A',*,due=True,cancel=False):
        accepted=self.now-GRACE_SECONDS if due else self.now
        row=dict(version=2,requestId=RID,status='cancel_requested' if cancel else 'grace_period',
                 requestedAt=datetime.fromtimestamp(accepted,timezone.utc),consentAuthTime=accepted)
        if cancel:row.update(cancelRequestedAt=datetime.fromtimestamp(accepted+1,timezone.utc),cancelAuthTime=accepted+1)
        self.backend.docs['accountDeletionIntents/'+uid]=row
        self.backend.docs['users/'+uid]={'deletionRequest':{'requestId':RID},'pushToken':'synthetic-only'}
        return row
    def runtime(self,enabled=True):return spark_runtime.build_runtime_from_worker(self.worker,enabled=enabled)

    def test_sdk_query_port_uses_fixed_bounded_document_cursor_without_cursor_read(self):
        self.seed('A');self.seed('B');self.seed('C')
        result=self.documents.pending_intents(statuses=tuple(sorted(ACTIVE)),limit=1,after='B')
        self.assertEqual(result,['C'])
        self.assertEqual(self.client.query_calls,[('where','status','in',sorted(ACTIVE)),('order','__name__'),('after','B'),('limit',1)])
        self.assertEqual(self.sdk.lookups,[])

    def test_missing_port_is_not_a_silent_empty_queue(self):
        self.seed('A');self.seed('B');self.client.query_error=True
        result=self.runtime().run_page(mode='process')
        self.assertEqual(result['state'],'unconfirmed');self.assertEqual(self.sdk.calls,[])
        self.assertNotIn('accountDeletionJobs/'+RID,self.backend.docs)

    def test_default_disabled_composition_performs_no_query_auth_or_write(self):
        self.seed();before=deepcopy(self.backend.docs)
        runtime=spark_runtime.build_runtime_from_worker(self.worker)
        self.assertEqual(runtime.run_page(mode='process')['state'],'disabled')
        self.assertEqual(self.backend.docs,before);self.assertEqual(self.sdk.lookups,[]);self.assertEqual(self.client.query_calls,[])

    def test_actual_adapter_lookup_distinguishes_absent_from_unavailable_auth(self):
        identity=self.auth.intake_identity('A')
        self.assertEqual((identity.created_at,identity.valid_after,identity.disabled),(1000,0,False))
        with self.assertRaises(Unauthorized):self.auth.intake_identity('missing')
        self.sdk.lookup_error=OSError('synthetic unavailable')
        with self.assertRaises(TemporaryFailure):self.auth.intake_identity('A')

    def test_sdk_ports_seven_day_boundary_then_partial_cleanup_and_resume(self):
        self.seed(due=False);runtime=self.runtime();self.now+=GRACE_SECONDS-1
        self.assertEqual(runtime.run_page(mode='process')['waiting'],1);self.assertEqual(self.sdk.calls,[])
        self.now+=1;self.backend.fail_delete=True
        result=runtime.run_page(mode='process');self.assertEqual(result['completed'],0)
        self.assertEqual(self.sdk.calls,['A']);self.assertEqual(self.backend.docs['accountDeletionIntents/A']['status'],'retry_wait')
        self.backend.fail_delete=False;self.now=self.backend.docs['accountDeletionJobs/'+RID]['nextRun']
        self.assertEqual(runtime.run_page(mode='process')['completed'],1)
        self.assertEqual(self.sdk.calls,['A']);self.assertNotIn('users/A',self.backend.docs)
        self.assertIn('users/A/unknown/item',self.backend.docs)
        self.assertEqual(self.backend.docs['accountDeletionIntents/A']['completedScope'],['firebase-auth','users-document'])
        self.assertEqual(runtime.run_page(mode='process')['examined'],0)

    def test_cancel_received_before_deadline_is_confirmed_without_deletion(self):
        self.seed(due=False,cancel=True);self.now+=GRACE_SECONDS+10
        result=self.runtime().run_page(mode='confirm_cancellations')
        self.assertEqual(result['cancelled'],1);self.assertEqual(self.sdk.calls,[])
        self.assertFalse(self.backend.docs['accountDeletionState/A']['blocked'])
        self.assertNotIn('deletionRequest',self.backend.docs['users/A'])

    def test_revoked_consent_or_invalid_sdk_metadata_never_materializes_job(self):
        self.seed();self.sdk.users['A'].tokens_valid_after_timestamp=self.now*1000
        self.assertEqual(self.runtime().run_page(mode='process')['errors'],{'authentication':1})
        self.assertNotIn('accountDeletionJobs/'+RID,self.backend.docs);self.assertEqual(self.sdk.calls,[])
        self.sdk.users['A'].tokens_valid_after_timestamp='invalid'
        with self.assertRaises(Unauthorized):self.auth.intake_identity('A')

    def test_environment_guard_failure_prevents_sdk_query(self):
        self.guard.allowed=False
        with self.assertRaises(Disabled):self.documents.pending_intents(statuses=tuple(sorted(ACTIVE)),limit=1,after=None)
        self.assertEqual(self.client.query_calls,[])


if __name__=='__main__':unittest.main()
