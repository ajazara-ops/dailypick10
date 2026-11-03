"""Explicit LOCAL emulator binding. No production fallback or credential discovery.

The demo project, exact loopback ports and live Emulator Hub are mandatory.
Never import/call this module from the app, workflow or production service.
"""
import json
import os
import urllib.request
import uuid
from account_deletion import DeletionService, Disabled, PermissionFailure
from deletion_adapters import FirebaseAuthAdapter, FirestoreJobStore, FirestoreProfiles

PROJECT='demo-dailypick10-deletion'
AUTH_HOST='127.0.0.1:19099'
FIRESTORE_HOST='127.0.0.1:18080'
HUB_HOST='127.0.0.1:14400'


class EmulatorGuard:
    def assert_environment(self):
        expected={'GCLOUD_PROJECT':PROJECT,'GOOGLE_CLOUD_PROJECT':PROJECT,
                  'FIREBASE_AUTH_EMULATOR_HOST':AUTH_HOST,'FIRESTORE_EMULATOR_HOST':FIRESTORE_HOST,
                  'FIREBASE_EMULATOR_HUB':HUB_HOST,'DAILYPICK10_LOCAL_EMULATOR_TEST':'1'}
        if any(os.environ.get(k)!=v for k,v in expected.items()):
            raise Disabled('Exact demo/loopback emulator environment required')
        if any(os.environ.get(k) for k in ['GOOGLE_APPLICATION_CREDENTIALS','FIREBASE_TOKEN','CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE']):
            raise Disabled('Credential/config environment forbidden in local emulator tests')
        # CLI emulators:exec creates FIREBASE_CONFIG if absent. Supply and accept
        # only the exact demo project, without database/storage URLs or credentials.
        try: config=json.loads(os.environ.get('FIREBASE_CONFIG','{}'))
        except ValueError: raise Disabled('Invalid local emulator config')
        if config!={'projectId':PROJECT}:raise Disabled('Exact demo-only Firebase config required')

    def preflight(self):
        self.assert_environment()
        # No proxy or redirects: only the exact locally configured Emulator Hub.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):raise Disabled('Emulator redirect refused')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        try:
            with opener.open('http://'+HUB_HOST+'/emulators',timeout=3) as response: services=json.load(response)
            for name,port in [('auth',19099),('firestore',18080)]:
                if services[name]['host']!='127.0.0.1' or services[name]['port']!=port:raise Disabled('Unexpected emulator endpoint')
        except Exception as error:raise Disabled('Required local emulators are unavailable; no fallback') from error


class EmulatorAuthSDK:
    synthetic_only=True
    emulator_only=True
    def __init__(self, guard, sdk, app):
        self.guard,self.sdk,self.app=guard,sdk,app
        self.UserNotFoundError=sdk.UserNotFoundError
    def verify_id_token(self,token,*,check_revoked):
        self.guard.assert_environment()
        return self.sdk.verify_id_token(token,check_revoked=check_revoked,app=self.app)
    def get_user(self,uid):self.guard.assert_environment();return self.sdk.get_user(uid,app=self.app)
    def delete_user(self,uid):self.guard.assert_environment();return self.sdk.delete_user(uid,app=self.app)


class EmulatorDocuments:
    synthetic_only=True
    emulator_only=True
    def __init__(self,guard,client,firestore):self.guard,self.client,self.firestore=guard,client,firestore
    def read(self,path):
        self.guard.assert_environment()
        snapshot=self.client.document(path).get(timeout=5)
        return snapshot.to_dict() if snapshot.exists else None
    def transaction(self,fn):
        self.guard.assert_environment();client=self.client;guard=self.guard
        class Tx:
            def __init__(self,transaction):self.transaction=transaction;self.wrote=False
            def get(self,path):
                if self.wrote:raise ValueError('Read after write forbidden')
                guard.assert_environment();snapshot=client.document(path).get(transaction=self.transaction,timeout=5)
                return snapshot.to_dict() if snapshot.exists else None
            def set(self,path,value):guard.assert_environment();self.wrote=True;self.transaction.set(client.document(path),value)
            def delete(self,path):guard.assert_environment();self.wrote=True;self.transaction.delete(client.document(path))
        @self.firestore.transactional
        def run(transaction):return fn(Tx(transaction))
        return run(client.transaction(max_attempts=5))
    def due_jobs(self,now,limit):
        from google.cloud.firestore_v1.base_query import FieldFilter
        self.guard.assert_environment()
        query=self.client.collection('accountDeletionJobs').where(filter=FieldFilter('status','in',['grace_period','processing','retry_wait']))
        for field in ['deleteAfter','nextRun','leaseUntil']:query=query.where(filter=FieldFilter(field,'<=',now))
        # Firestore implicitly orders remaining inequality fields. Put every range
        # field before the document key; otherwise SDK appends fields after __name__.
        for field in ['nextRun','deleteAfter','leaseUntil','__name__']:query=query.order_by(field)
        query=query.limit(limit)
        return [snapshot.id for snapshot in query.stream(timeout=5)]

    def pending_intents(self, *, statuses, limit, after):
        """Only the fixed active-intent query, bounded by document ID on localhost.

        start_after uses a document reference, not a read of the cursor document:
        a processed/deleted or changed intent must not break the next page.
        """
        self.guard.assert_environment()
        from account_deletion import InvalidRequest
        from spark_batch import ACTIVE
        from spark_runtime import validate_cursor
        from google.cloud.firestore_v1.base_query import FieldFilter
        if tuple(statuses) != tuple(sorted(ACTIVE)) or type(limit) is not int or not 1 <= limit <= 25:
            raise InvalidRequest('Only the bounded active-intent query is allowed')
        validate_cursor(after)
        collection = self.client.collection('accountDeletionIntents')
        query = collection.where(filter=FieldFilter('status', 'in', list(statuses))).order_by('__name__')
        if after is not None:
            query = query.start_after({'__name__': collection.document(after)})
        return [snapshot.id for snapshot in query.limit(limit).stream(timeout=5)]


class LocalEmulatorService(DeletionService):
    def __init__(self,guard,*args,**kwargs):self.emulator_guard=guard;super().__init__(*args,mode='emulator',**kwargs)
    def _guard(self):
        self.emulator_guard.assert_environment()
        if self.mode!='emulator' or not all(getattr(x,'emulator_only',False) for x in (self.store,self.auth,self.profiles)):
            raise Disabled('Only explicit local emulator adapters are permitted')


class EmulatorRuntime:
    def __init__(self,clock):
        self.guard=EmulatorGuard();self.guard.preflight()
        # Imports happen only after the exact localhost/demo guard passes.
        import firebase_admin
        from firebase_admin import auth,credentials
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        class AnonymousFirebaseCredential(credentials.Base):
            def get_credential(self):return AnonymousCredentials()
        self.sdk=firebase_admin
        self.app=firebase_admin.initialize_app(AnonymousFirebaseCredential(),{'projectId':PROJECT,'httpTimeout':5},name='local-emulator-'+uuid.uuid4().hex)
        self.client=firestore.Client(project=PROJECT,credentials=AnonymousCredentials(),client_options={'api_endpoint':FIRESTORE_HOST})
        self.documents=EmulatorDocuments(self.guard,self.client,firestore)
        self.auth=FirebaseAuthAdapter(EmulatorAuthSDK(self.guard,auth,self.app),PROJECT)
        self.store=FirestoreJobStore(self.documents);self.profiles=FirestoreProfiles(self.documents)
        for port in [self.auth,self.store,self.profiles]:port.emulator_only=True
        self.service=LocalEmulatorService(self.guard,self.store,self.auth,self.profiles,clock)
    def close(self):self.client.close();self.sdk.delete_app(self.app)
