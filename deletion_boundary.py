"""Framework-neutral request/worker boundary. No socket, CLI, SDK or credential loading.

Production routes always remain disabled. Explicit /__mock__/ and /__emulator__/
routes reach their corresponding local adapters only.
"""
import json
from account_deletion import (Disabled, InvalidRequest, Unauthorized, IdentityChanged,
                              CancellationClosed)


class DeletionRequestHandler:
    def __init__(self, service=None, *, mode='disabled'):
        self.service, self.mode = service, mode

    def handle(self, method, path, headers=None, raw_body=b''):
        response_headers = {'Cache-Control':'no-store', 'Content-Type':'application/json'}
        def response(status, body): return status, response_headers, body
        prefix = '/__mock__' if self.mode == 'mock' else '/__emulator__' if self.mode == 'emulator' else None
        if prefix is None or self.service is None or not path.startswith(prefix+'/'):
            return response(503, {'error':'deletion-service-disabled'})
        try:
            self.service._guard()
            if self.mode == 'emulator' and self.service.mode != 'emulator':
                raise Disabled('Actual local emulator adapters required')
            if '?' in path or '#' in path: raise InvalidRequest('No URL credentials')
            if not isinstance(raw_body, bytes) or len(raw_body)>4096: return response(413, {'error':'invalid-body-size'})
            headers = {str(k).lower():v for k,v in (headers or {}).items()}
            if method=='POST' and headers.get('content-type','').split(';')[0]!='application/json':
                return response(415, {'error':'json-required'})
            body=json.loads(raw_body) if raw_body else {}
            if not isinstance(body,dict): raise InvalidRequest('Object required')
            route=path.removeprefix(prefix)
            # Recovery lookup is read-only and never accepts a UID or an Auth command.
            if method=='POST' and route=='/v1/account-deletion/status':
                if set(body)!={'requestId','recoveryKey'}: raise InvalidRequest('Invalid receipt request')
                return response(200, {'receipt':self.service.status(body['requestId'],body['recoveryKey'])})
            if (method,route) not in {('POST','/v1/account-deletion/request'),('POST','/v1/account-deletion/cancel'),('GET','/v1/account-deletion/current')}:
                return response(404, {'error':'not-found'})
            authorization=headers.get('authorization','')
            if not isinstance(authorization,str) or not authorization.startswith('Bearer ') or not authorization[7:] or len(authorization)>8192:
                raise Unauthorized('Token required')
            token=authorization[7:]
            if method=='GET':
                if raw_body: raise InvalidRequest('GET body forbidden')
                result=self.service.current(token)
            elif route.endswith('/request'): result=self.service.submit(token,body)
            else: result=self.service.cancel(token,body)
            return response(200, {'receipt':result})
        except Disabled: return response(503, {'error':'deletion-service-disabled'})
        except (InvalidRequest,ValueError,UnicodeError): return response(400, {'error':'invalid-request'})
        except Unauthorized: return response(401, {'error':'authentication-required'})
        except (CancellationClosed,IdentityChanged): return response(409, {'error':'transition-rejected'})
        except Exception: return response(503, {'error':'temporarily-unavailable'})


def run_due_once(service, *, enabled=False, limit=25):
    """Injected local scheduler callback, not an HTTP route. Never enables itself."""
    if not enabled: return {'enabled':False,'examined':0,'completed':0}
    service._guard()
    if type(limit) is not int or not 1<=limit<=100: raise InvalidRequest('Invalid batch limit')
    ids=service.store.due_ids(service.clock(),limit)
    completed=0
    for job_id in ids:
        job=service.run_once(job_id)
        completed+=bool(job and job['status']=='completed')
    return {'enabled':True,'examined':len(ids),'completed':completed}
