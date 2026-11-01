"""Real Auth/Firestore emulators; synthetic accounts only, no production fallback.

The logical clock maps ONLY tokens obtained from successful local sign-in to test
time after real SDK verification. It never changes the computer clock or claims.
This exercises seven-day policy, not production JWT cryptography (emulator JWTs
are unsigned). Request handlers run in-process; Firestore/Auth calls are real.
"""
import json
import base64
import os
import secrets
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from account_deletion import (Disabled, Unauthorized, TemporaryFailure,
                              CancellationClosed, GRACE_SECONDS, push_eligible)
from deletion_boundary import DeletionRequestHandler, run_due_once
from emulator_only import EmulatorRuntime, EmulatorGuard, PROJECT, AUTH_HOST, FIRESTORE_HOST


class Clock:
    def __init__(self): self.now = int(time.time())
    def __call__(self): return self.now


class ClockMappedAuth:
    synthetic_only = True
    emulator_only = True
    def __init__(self, actual, clock): self.actual, self.clock, self.sessions = actual, clock, {}
    def fresh(self, token):
        self.actual.verify(token)
        self.sessions[token] = self.clock()
    def verify(self, token):
        principal = self.actual.verify(token)  # Actual SDK scope/user/revocation checks first.
        if token not in self.sessions: raise Unauthorized('Not a locally authenticated test session')
        signed_in = self.sessions[token]
        return replace(principal, auth_time=signed_in, expires_at=signed_in+3600)
    def ensure_same_or_absent(self, *args): return self.actual.ensure_same_or_absent(*args)
    def delete_if_same(self, *args): return self.actual.delete_if_same(*args)


class EmulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EmulatorGuard().preflight()
        actual_request = requests.sessions.Session.request
        def loopback_only(session, method, url, *args, **kwargs):
            target = urlsplit(url)
            if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.port not in {19099,18080,14400}:
                raise AssertionError('External HTTP forbidden in emulator tests')
            kwargs['allow_redirects'] = False
            kwargs.setdefault('timeout', 8)
            return actual_request(session, method, url, *args, **kwargs)
        cls.http_patch = patch.object(requests.sessions.Session, 'request', loopback_only)
        cls.http_patch.start()

    @classmethod
    def tearDownClass(cls): cls.http_patch.stop()

    def setUp(self):
        self.clock = Clock()
        self.runtime = EmulatorRuntime(self.clock)
        self.addCleanup(self.runtime.close)
        self.auth = ClockMappedAuth(self.runtime.auth, self.clock)
        self.service = self.runtime.service
        self.service.auth = self.auth
        self.handler = DeletionRequestHandler(self.service, mode='mock')
        self.uid, self.token, self.email = self.signup()
        self.body = self.new_body()
        self.assertEqual(self.client('PATCH', 'users/'+self.uid, {'fields': {'favoriteIds': {'arrayValue': {'values': [{'stringValue': 'SYNTHETIC'}]}}}}).status_code, 200)

    def new_body(self):
        return dict(version=1,requestId=str(uuid.uuid4()),recoveryKey=secrets.token_hex(32),confirmation='DELETE_MY_ACCOUNT')

    def signup(self):
        email = 'synthetic-'+uuid.uuid4().hex+'@example.invalid'
        response = requests.post('http://'+AUTH_HOST+'/identitytoolkit.googleapis.com/v1/accounts:signUp?key=synthetic-only',json={'email':email,'password':'Local-Only-Example-123!','returnSecureToken':True})
        self.assertEqual(response.status_code,200)
        data = response.json(); self.auth.fresh(data['idToken'])
        return data['localId'],data['idToken'],email

    def signin(self):
        response = requests.post('http://'+AUTH_HOST+'/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=synthetic-only',json={'email':self.email,'password':'Local-Only-Example-123!','returnSecureToken':True})
        self.assertEqual(response.status_code,200)
        self.token = response.json()['idToken']; self.auth.fresh(self.token)

    def client(self, method, path, body=None, token='self'):
        token = self.token if token=='self' else token
        headers = {'Authorization':'Bearer '+token} if token else {}
        return requests.request(method,'http://'+FIRESTORE_HOST+'/v1/projects/'+PROJECT+'/databases/(default)/documents/'+path,headers=headers,json=body)

    def call(self, action, body=None, token='self', method='POST'):
        token = self.token if token=='self' else token
        headers = {'Content-Type':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        return self.handler.handle(method,'/__mock__/v1/account-deletion/'+action,headers,json.dumps(body).encode() if body is not None else b'')

    def submit(self):
        response=self.call('request',self.body);self.assertEqual(response[0],200);return response[2]['receipt']

    def cancel_body(self):
        return dict(version=1,requestId=self.body['requestId'],confirmation='CANCEL_ACCOUNT_DELETION')

    def test_01_real_sdk_identity_and_recent_auth(self):
        principal=self.runtime.auth.verify(self.token)
        self.assertEqual(principal.uid,self.uid)
        self.assertGreater(principal.created_at,0)
        self.assertEqual(self.call('request',self.body,token='invalid')[0],401)
        self.clock.now+=301
        self.assertEqual(self.call('request',self.body)[0],401)
        self.signin();self.submit()

    def test_02_security_rules_ownership_server_fields_and_paths(self):
        other,token,_=self.signup()
        self.assertEqual(self.client('GET','users/'+self.uid,token=token).status_code,403)
        self.assertEqual(self.client('GET','users/'+self.uid,token=None).status_code,403)
        self.assertEqual(self.client('PATCH','users/'+other,{'fields':{}}).status_code,403)
        self.assertEqual(self.client('DELETE','users/'+self.uid).status_code,403)
        forged={'fields':{'deletionRequest':{'mapValue':{'fields':{'status':{'stringValue':'completed'}}}}}}
        self.assertEqual(self.client('PATCH','users/'+self.uid,forged).status_code,403)
        for collection in ['accountDeletionJobs','accountDeletionState','accountDeletionReceipts']:
            self.assertEqual(self.client('PATCH',collection+'/'+self.uid,{'fields':{}}).status_code,403)
            self.assertEqual(self.client('GET',collection+'/'+self.uid).status_code,403)

    def test_03_request_contract_and_other_user_cancellation(self):
        for extra in [{'uid':'someone-else'},{'deleteAfter':0},{'status':'completed'}]:
            self.assertEqual(self.call('request',{**self.body,**extra})[0],400)
        self.submit();_,other,_=self.signup()
        self.assertEqual(self.call('cancel',self.cancel_body(),token=other)[0],401)
        self.assertTrue(self.runtime.store.blocked(self.uid))
        self.assertEqual(self.handler.handle('POST','/v1/account-deletion/request')[0],503)
        self.assertEqual(DeletionRequestHandler().handle('POST','/__mock__/v1/account-deletion/request')[0],503)

    def test_04_atomic_marker_blocks_service_push_and_late_autosave(self):
        receipt=self.submit()
        self.assertEqual(receipt['deleteAfter']-receipt['requestedAt'],604800)
        self.assertFalse(receipt['subscriptionCancelled'])
        profile=self.runtime.documents.read('users/'+self.uid)
        self.assertEqual(profile['deletionRequest']['requestId'],self.body['requestId'])
        self.assertFalse(push_eligible(profile,self.runtime.store.blocked(self.uid)))
        self.assertEqual(self.client('GET','users/'+self.uid).status_code,403)
        self.assertEqual(self.client('PATCH','users/'+self.uid,{'fields':{}}).status_code,403)
        proof=self.runtime.documents.read('accountDeletionReceipts/'+self.body['requestId'])
        self.assertNotEqual(proof['proof'],self.body['recoveryKey'])

    def test_05_duplicate_relogin_and_durable_receipt_do_not_extend(self):
        first=self.submit();self.clock.now+=2
        self.assertEqual(self.submit()['deleteAfter'],first['deleteAfter'])
        self.signin()
        self.assertEqual(self.call('current',method='GET')[2]['receipt']['status'],'grace_period')
        old=dict(self.body);self.body=self.new_body()
        self.assertEqual(self.submit()['deleteAfter'],first['deleteAfter'])
        second=EmulatorRuntime(self.clock)
        try:
            result=second.service.status(old['requestId'],old['recoveryKey'])
            self.assertEqual(result['deleteAfter'],first['deleteAfter'])
            self.assertEqual(result['status'],'grace_period')
        finally:second.close()

    def test_06_cancel_just_before_deadline_and_old_replays(self):
        first=self.submit();self.clock.now=first['deleteAfter']-1;self.signin()
        self.assertEqual(self.call('cancel',self.cancel_body())[2]['receipt']['status'],'cancelled')
        self.assertFalse(self.runtime.store.blocked(self.uid))
        self.assertEqual(self.client('GET','users/'+self.uid).status_code,200)
        self.assertTrue(push_eligible(self.runtime.documents.read('users/'+self.uid)))
        old=dict(self.body)
        self.assertEqual(self.submit()['status'],'cancelled')
        self.body=self.new_body();self.submit();new_id=self.body['requestId']
        self.body=old;self.assertEqual(self.call('cancel',self.cancel_body())[0],200)
        self.assertTrue(self.runtime.store.blocked(self.uid))
        self.assertEqual(self.runtime.documents.read('users/'+self.uid)['deletionRequest']['requestId'],new_id)

    def test_07_deadline_is_queued_not_completed_and_cancel_rejected(self):
        first=self.submit();self.clock.now=first['deleteAfter'];self.signin()
        status=self.call('current',method='GET')[2]['receipt']
        self.assertEqual(status['status'],'queued');self.assertEqual(status['completedScope'],[])
        self.assertFalse(status['cancelAllowed'])
        self.assertEqual(self.call('cancel',self.cancel_body())[0],409)
        self.assertIsNotNone(self.runtime.documents.read('users/'+self.uid))

    def test_08_concurrent_cancel_and_worker_claim_use_atomic_server_state(self):
        first=self.submit();self.clock.now=first['deleteAfter']-1;self.signin()
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel=pool.submit(self.service.cancel,self.token,self.cancel_body())
            claim=pool.submit(self.runtime.store.claim,self.body['requestId'],self.clock)
            self.assertEqual(cancel.result()['status'],'cancelled');self.assertIsNone(claim.result())
        self.body=self.new_body();second=self.submit();self.clock.now=second['deleteAfter'];self.signin()
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel=pool.submit(self.service.cancel,self.token,self.cancel_body())
            claim=pool.submit(self.runtime.store.claim,self.body['requestId'],self.clock)
            with self.assertRaises(CancellationClosed):cancel.result()
            self.assertEqual(claim.result()['status'],'processing')
        self.assertIsNone(self.runtime.store.claim(self.body['requestId'],self.clock))

    def test_09_actual_auth_delete_partial_failure_restart_and_recreation_block(self):
        self.runtime.client.document('users/'+self.uid+'/unknown/item').set({'preserve':True})
        first=self.submit();self.clock.now=first['deleteAfter']
        with patch.object(self.runtime.profiles,'delete_user_document',side_effect=TemporaryFailure('injected local failure')):
            result=self.service.run_once(self.body['requestId'])
        self.assertEqual(result['status'],'retry_wait');self.assertEqual(result['phase'],'profile')
        self.assertIsNone(self.runtime.auth._user(self.uid))
        self.assertIsNotNone(self.runtime.documents.read('users/'+self.uid))
        self.assertEqual(self.call('status',{'requestId':self.body['requestId'],'recoveryKey':self.body['recoveryKey']},token=None)[2]['receipt']['status'],'retry_wait')
        self.clock.now=result['nextRun']
        second=EmulatorRuntime(self.clock)
        try:
            self.assertEqual(second.service.run_once(self.body['requestId'])['status'],'completed')
            self.assertIsNone(second.service.run_once(self.body['requestId']))
            self.assertIsNone(second.documents.read('users/'+self.uid))
            self.assertTrue(second.documents.read('users/'+self.uid+'/unknown/item')['preserve'])
            self.assertTrue(second.store.blocked(self.uid))
            self.assertEqual(second.service.status(self.body['requestId'],self.body['recoveryKey'])['completedScope'],['firebase-auth','users-document'])
        finally:second.close()
        self.assertEqual(self.client('PATCH','users/'+self.uid,{'fields':{}}).status_code,403)
        with self.assertRaises(Unauthorized):self.runtime.auth.verify(self.token)

    def test_10_scheduler_query_and_bounded_callback(self):
        first=self.submit();self.assertNotIn(self.body['requestId'],self.runtime.store.due_ids(self.clock(),100))
        self.clock.now=first['deleteAfter']
        self.assertIn(self.body['requestId'],self.runtime.store.due_ids(self.clock(),100))
        self.assertEqual(run_due_once(self.service)['examined'],0)
        result=run_due_once(self.service,enabled=True,limit=100)
        self.assertGreaterEqual(result['completed'],1)
        self.assertEqual(self.runtime.store.get(self.body['requestId'])['status'],'completed')
        self.assertNotIn(self.body['requestId'],self.runtime.store.due_ids(self.clock(),100))

    def test_11_endpoint_guard_prevents_fallback_before_sdk_calls(self):
        for field,value in [('GCLOUD_PROJECT','real-project'),('FIRESTORE_EMULATOR_HOST','example.com:443'),('FIREBASE_AUTH_EMULATOR_HOST',''),('GOOGLE_APPLICATION_CREDENTIALS','forbidden'),('FIREBASE_CONFIG','{"projectId":"real-project"}')]:
            with patch.dict(os.environ,{field:value}):
                with self.assertRaises(Disabled):EmulatorRuntime(self.clock)
                with self.assertRaises(Disabled):self.service.current(self.token)
        with patch('urllib.request.OpenerDirector.open',side_effect=OSError('local emulator absent')):
            with self.assertRaises(Disabled):EmulatorRuntime(self.clock)

    def test_12_committed_request_response_loss_is_recovered_without_new_deadline(self):
        actual_accept=self.runtime.store.accept
        def lose_response(*args):
            actual_accept(*args)
            raise TemporaryFailure('Injected response loss AFTER actual Firestore commit')
        with patch.object(self.runtime.store,'accept',side_effect=lose_response):
            self.assertEqual(self.call('request',self.body)[0],503)
        persisted=self.runtime.store.get(self.body['requestId'])
        self.assertTrue(self.runtime.store.blocked(self.uid))
        self.clock.now+=1
        self.assertEqual(self.submit()['deleteAfter'],persisted['deleteAfter'])
        self.assertEqual(self.runtime.store.get(self.body['requestId'])['receiptCount'],1)

    def test_13_actual_sdk_rejects_wrong_project_issuer_and_disabled_user(self):
        # Emulator JWTs are intentionally unsigned. This checks scope, NOT signatures.
        header,payload,signature=self.token.split('.')
        original=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        for field,value in [('aud','demo-another-project'),('iss','https://securetoken.google.com/demo-another-project')]:
            encoded=base64.urlsafe_b64encode(json.dumps({**original,field:value}).encode()).decode().rstrip('=')
            with self.assertRaises(Unauthorized):self.runtime.auth.verify(header+'.'+encoded+'.'+signature)
        from firebase_admin import auth
        auth.update_user(self.uid,disabled=True,app=self.runtime.app)
        with self.assertRaises(Unauthorized):self.runtime.auth.verify(self.token)


if __name__=='__main__':unittest.main(verbosity=2)
