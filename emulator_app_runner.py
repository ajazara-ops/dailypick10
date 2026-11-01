"""LOCAL test orchestration only; no deployment/server entry for production.

Starts a loopback HTTP boundary while the installed CLI owns demo emulators.
Test controls require a per-process secret capability and are never app routes.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from account_deletion import Unauthorized, TemporaryFailure
from deletion_boundary import DeletionRequestHandler
from emulator_only import EmulatorGuard, EmulatorRuntime

HTTP_HOST='127.0.0.1'
HTTP_PORT=18781


class TestClock:
    def __init__(self): self.now=int(time.time())
    def __call__(self): return self.now


class TestClockAuth:
    synthetic_only=True
    emulator_only=True
    def __init__(self, actual, clock): self.actual,self.clock,self.sessions=actual,clock,{}
    def register_actual_signin(self, token):
        self.actual.verify(token)
        self.sessions[token]=self.clock()
    def verify(self, token):
        principal=self.actual.verify(token)
        if token not in self.sessions: raise Unauthorized('Test sign-in must be registered')
        logical_time=self.sessions[token]
        return replace(principal,auth_time=logical_time,expires_at=logical_time+3600)
    def ensure_same_or_absent(self,*args): return self.actual.ensure_same_or_absent(*args)
    def delete_if_same(self,*args): return self.actual.delete_if_same(*args)


def main():
    if os.environ.get('DAILYPICK10_APP_EMULATOR_TEST')!='1':
        raise RuntimeError('Explicit local app test launch required')
    EmulatorGuard().preflight()
    import requests
    original_http=requests.sessions.Session.request
    def local_http(session,method,url,*args,**kwargs):
        endpoint=urlsplit(url)
        if endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1' or endpoint.port not in {19099,18080,14400}:
            raise RuntimeError('External request blocked')
        kwargs['allow_redirects']=False
        return original_http(session,method,url,*args,**kwargs)
    requests.sessions.Session.request=local_http
    clock=TestClock()
    runtime=EmulatorRuntime(clock)
    auth=TestClockAuth(runtime.auth,clock)
    runtime.service.auth=auth
    boundary=DeletionRequestHandler(runtime.service,mode='emulator')
    capability=secrets.token_hex(32)  # Memory-only test control, never an operational credential.
    faults={'lose_response':False,'profile_fail_once':False}
    delete_profile=runtime.profiles.delete_user_document
    def maybe_fail_profile(uid):
        if faults['profile_fail_once']:
            faults['profile_fail_once']=False
            raise TemporaryFailure('Injected local profile failure')
        return delete_profile(uid)
    runtime.profiles.delete_user_document=maybe_fail_profile

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass  # Never log tokens, proofs, synthetic passwords.
        def reply(self,status,value,headers=None):
            data=json.dumps(value).encode()
            self.send_response(status)
            for key,value in (headers or {'Content-Type':'application/json','Cache-Control':'no-store'}).items():self.send_header(key,value)
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
        def dispatch(self):
            if self.client_address[0]!=HTTP_HOST or self.headers.get('Host')!=f'{HTTP_HOST}:{HTTP_PORT}' or self.headers.get('Origin'):
                return self.reply(403,{'error':'local-only'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=4096: return self.reply(413,{'error':'body-limit'})
                body=self.rfile.read(length)
                if self.path=='/__test__/control':
                    if self.command!='POST' or not hmac.compare_digest(self.headers.get('X-Local-Test-Key',''),capability):
                        return self.reply(403,{'error':'test-control-denied'})
                    value=json.loads(body);operation=value.get('operation')
                    if operation=='reset' and set(value)=={'operation'}:
                        clock.now=int(time.time());faults.update(lose_response=False,profile_fail_once=False)
                        return self.reply(200,{'now':clock()})
                    if operation=='signin' and set(value)=={'operation','token'}:
                        auth.register_actual_signin(value['token']);return self.reply(200,{'registered':True})
                    if operation=='advance' and set(value)=={'operation','seconds'} and type(value['seconds']) is int and 0<=value['seconds']<=1209600:
                        clock.now+=value['seconds'];return self.reply(200,{'now':clock()})
                    if operation=='fault' and set(value)=={'operation','name'} and value['name'] in faults:
                        faults[value['name']]=True;return self.reply(200,{'armed':True})
                    if operation in {'work','claim'} and set(value)=={'operation','requestId'} and uuid.UUID(value['requestId']).version==4:
                        result=runtime.service.run_once(value['requestId']) if operation=='work' else runtime.store.claim(value['requestId'],clock)
                        return self.reply(200,{'status':result['status'] if result else None})
                    return self.reply(400,{'error':'invalid-test-control'})
                status,headers,result=boundary.handle(self.command,self.path,dict(self.headers),body)
                if self.path.endswith('/request') and status==200 and faults['lose_response']:
                    faults['lose_response']=False;self.close_connection=True
                    return  # Actual server commit succeeded; intentionally lose this HTTP response.
                self.reply(status,result,headers)
            except Exception:self.reply(500,{'error':'local-test-error'})

    server=None
    try:
        server=ThreadingHTTPServer((HTTP_HOST,HTTP_PORT),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        env=dict(os.environ);env['DAILYPICK10_LOCAL_TEST_KEY']=capability
        mobile=Path(__file__).resolve().parents[1]/'mobile'
        result=subprocess.run(['node','--test','--test-concurrency=1','emulator-tests/app-deletion.test.cjs'],cwd=mobile,env=env)
        return result.returncode
    finally:
        if server is not None:server.shutdown();server.server_close()
        runtime.close();requests.sessions.Session.request=original_http


if __name__=='__main__':raise SystemExit(main())
