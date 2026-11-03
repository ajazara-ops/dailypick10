"""PR5 lifecycle and PR7 recipients: synthetic documents and SDK facades only."""
from copy import deepcopy
from types import SimpleNamespace
from contextlib import redirect_stdout
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

import test_guarded_daily as guarded_fixtures
import test_spark_runtime as runtime_fixtures
import test_pages_runtime as pages_fixtures
from test_pages_assets import site_fixture
from spark_runtime import SparkBatchRuntime, main as batch_main
from account_deletion import GRACE_SECONDS
from fake_github import GitServer, Response
from publication_runtime import build_daily_publisher, build_weekly_publisher
from test_scan_publication import NOW, facts
from test_weekly_publication import WEEK_NOW, sources

RID = '00000000-0000-4000-8000-000000000001'
UID = 'synthetic-member'
TOKEN = 'ExpoPushToken[synthetic-integration]'


class ReadOnlyDatabase:
    def __init__(self):
        self.rows = {'users/'+UID:{'pushToken':TOKEN}}
        self.reads = [];self.failure = False

    def snapshot(self, path):
        owner = self
        def read():
            owner.reads.append(path)
            if owner.failure:raise OSError('synthetic read unavailable')
            value = deepcopy(owner.rows.get(path))
            return SimpleNamespace(id=path.split('/')[-1], exists=value is not None, to_dict=lambda:value)
        return read()

    def collection(self, name):
        return SimpleNamespace(
            stream=lambda:iter([self.snapshot(p) for p in self.rows if p.startswith(name+'/') and p.count('/')==1]),
            document=lambda uid:SimpleNamespace(get=lambda:self.snapshot(name+'/'+uid)))


class RecipientIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.harness = guarded_fixtures.GuardedScannerTests('test_unconfigured_cli_boundary_has_zero_callbacks')
        self.harness.setUp();self.addCleanup(self.harness.doCleanups)
        self.scanner = self.harness.scanner;self.db = ReadOnlyDatabase()
        self.scanner.FIREBASE_AVAILABLE = True
        self.scanner.firebase_admin = SimpleNamespace(_apps={'synthetic':object()})
        self.scanner.firestore = SimpleNamespace(client=lambda:self.db)

    def test_recreated_profile_cannot_bypass_completed_tombstone(self):
        self.db.rows['accountDeletionState/'+UID] = {'jobId':RID,'blocked':True}
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])

    def test_all_active_or_uncertain_intents_block_even_without_profile_marker(self):
        for status in ('grace_period','cancel_requested','processing','retry_wait','needs_review','completed','unexpected'):
            with self.subTest(status=status):
                self.db.rows['accountDeletionIntents/'+UID] = {'version':2,'requestId':RID,'status':status}
                self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])

    def test_only_matching_confirmed_cancellation_unblocks(self):
        self.db.rows['accountDeletionIntents/'+UID] = {'version':2,'requestId':RID,'status':'cancelled'}
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])
        self.db.rows['accountDeletionState/'+UID] = {'jobId':RID,'blocked':False}
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [TOKEN])
        self.db.rows['accountDeletionState/'+UID]['jobId'] = '00000000-0000-4000-8000-000000000002'
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])

    def test_normal_user_is_retained_and_marker_blocks_before_extra_reads(self):
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [TOKEN])
        self.db.reads.clear();self.db.rows['users/'+UID]['deletionRequest'] = {}
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])
        self.assertEqual(self.db.reads, ['users/'+UID])

    def test_state_lookup_failure_is_not_absence_or_partial_recipient_success(self):
        original = self.db.snapshot
        def read(path):
            if path.startswith('accountDeletionState/'):raise OSError('synthetic secret-like error must not escape')
            return original(path)
        self.db.snapshot = read
        with self.assertRaisesRegex(Exception, '^push_recipient_lookup_failed$'):
            self.scanner.get_all_user_tokens(strict=True)
        self.assertEqual(self.scanner.get_all_user_tokens(), [])

    def test_actual_cancel_batch_then_next_request_changes_eligibility(self):
        life = runtime_fixtures.RuntimeTests();life.setUp();rid = life.seed(UID,1,due=False,cancel=True)
        life.docs.docs['users/'+UID]['pushToken'] = TOKEN
        self.db.rows = life.docs.docs
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])
        with redirect_stdout(io.StringIO()) as out:
            code = batch_main(['--mode','confirm_cancellations'],runtime=life.runtime())
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())['cancelled'],1)
        self.db.rows = life.docs.docs
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [TOKEN])
        life.seed(UID,2,due=False);life.docs.docs['users/'+UID]['pushToken']=TOKEN
        self.db.rows = life.docs.docs
        self.assertEqual(self.scanner.get_all_user_tokens(strict=True), [])
        self.assertEqual(life.auth.calls,[])

    def pages_case(self, mode='daily'):
        # Compose the existing CLI harness, without inheriting/recounting its tests.
        case=pages_fixtures.PagesRuntimeTests('runTest')
        case.setUp();self.addCleanup(case.doCleanups)
        # Real scanner recipient implementation, backed by our read-only synthetic DB.
        case.scanner.get_all_user_tokens=self.scanner.get_all_user_tokens
        if mode=='weekly':
            files,case.policy=site_fixture(sources())
            case.server=GitServer(files);case.env=pages_fixtures.pages_environment(case.server)
            case.now=WEEK_NOW
            case.scanner.is_market_session=lambda *_:True
            case.scanner.evaluation_session=lambda _,day:day
            case.scanner.exit_quote=lambda row,day:{'price':120,'quoteAsOf':day,'hasSplit':False}
            def scan():
                def factory(scanner):
                    return build_weekly_publisher(scanner,environment=case.env,request=case.request,
                        post=case.post,clock=lambda:case.now)
                return case.scanner.main(['--mode','weekly','--publication-result',str(case.result)],weekly_runtime_factory=factory)
            case.scan=scan
        return case

    def test_daily_and_weekly_actual_cli_exclude_lifecycle_without_running_deletion(self):
        for mode in ('daily','weekly'):
            with self.subTest(mode=mode):
                self.db.rows={'users/'+UID:{'pushToken':TOKEN}};self.db.reads.clear()
                case=self.pages_case(mode);case.ready()
                self.assertEqual(self.db.reads,[]);self.assertEqual(case.posts,[])
                # A request accepted after collection but before public verification
                # must still exclude this member from the delayed notification.
                self.db.rows['accountDeletionIntents/'+UID]={'version':2,'requestId':RID,'status':'grace_period'}
                before = deepcopy(self.db.rows)
                result=case.cli('verify')
                self.assertEqual(result['state'],'verified')
                self.assertEqual(list(result['notification'].values()),['no_recipients'])
                self.assertEqual(case.posts,[]);self.assertEqual(self.db.rows,before)
                self.assertFalse(any(k.startswith('accountDeletion') for k in case.server.files))
                reads=len(self.db.reads);counts=deepcopy(case.harness.counts)
                case.new_run();self.assertEqual(case.scan()['state'],'noop')
                self.assertEqual(case.prepare()['action'],'none')
                self.assertEqual(len(self.db.reads),reads);self.assertEqual(case.harness.counts,counts)

    def test_recipient_read_failure_sets_unknown_and_duplicate_does_not_retry(self):
        case=self.pages_case();case.ready();original=self.db.snapshot
        self.assertEqual(self.db.reads,[])
        def read(path):
            if path.startswith('accountDeletionIntents/'):raise OSError('synthetic read failed')
            return original(path)
        self.db.snapshot=read
        first=case.cli('verify')
        self.assertEqual(list(first['notification'].values()),['unknown']);self.assertEqual(case.posts,[])
        before=len(self.db.reads)
        case.cli('verify');self.assertEqual(len(self.db.reads),before)
        case.new_run();second=case.scan()
        self.assertEqual(second['state'],'noop');self.assertEqual(len(self.db.reads),before)

    def test_cancellation_pending_at_delivery_is_excluded_but_confirmed_before_delivery_is_allowed(self):
        for confirmed in (False,True):
            with self.subTest(confirmed=confirmed):
                life=runtime_fixtures.RuntimeTests();life.setUp();life.seed(UID,1,due=False,cancel=True)
                life.docs.docs['users/'+UID]['pushToken']=TOKEN;self.db.rows=life.docs.docs;self.db.reads.clear()
                case=self.pages_case();case.ready()
                self.assertEqual(self.db.reads,[])
                if confirmed:
                    with redirect_stdout(io.StringIO()):code=batch_main(['--mode','confirm_cancellations'],runtime=life.runtime())
                    self.assertEqual(code,0);self.db.rows=life.docs.docs
                before=deepcopy(self.db.rows);result=case.cli('verify')
                self.assertEqual(list(result['notification'].values()),['accepted' if confirmed else 'no_recipients'])
                self.assertEqual(len(case.posts),int(confirmed));self.assertEqual(life.auth.calls,[])
                self.assertEqual(self.db.rows,before)
                self.assertNotIn(TOKEN.encode(),b''.join(case.server.files.values()))


class BatchCLITests(unittest.TestCase):
    def setUp(self):
        self.life=runtime_fixtures.RuntimeTests();self.life.setUp()
    def cli(self, mode='inspect', runtime=True):
        with redirect_stdout(io.StringIO()) as output:
            code=batch_main(['--mode',mode],runtime=self.life.runtime() if runtime else None)
        return code,json.loads(output.getvalue())
    def test_default_no_binding_and_disabled_runtime_never_touch_documents(self):
        self.life.seed(UID,1);before=deepcopy(self.life.docs.docs)
        self.assertEqual(self.cli('process',False)[0],2)
        with redirect_stdout(io.StringIO()):self.assertEqual(batch_main(['--mode','process'],runtime=self.life.runtime(False)),2)
        self.assertEqual(self.life.docs.docs,before);self.assertEqual(self.life.auth.calls,[])
    def test_inspect_is_read_only_and_no_reauthentication_or_delete(self):
        self.life.seed(UID,1);before=deepcopy(self.life.docs.docs)
        code,result=self.cli();self.assertEqual(code,0);self.assertEqual(result['state'],'inspected')
        self.assertEqual(self.life.docs.docs,before);self.assertEqual(self.life.lookups,[])
    def test_seven_day_server_boundary_and_duplicate_keep_request_id(self):
        rid=self.life.seed(UID,1,due=False);created=self.life.now
        self.life.now+=GRACE_SECONDS-1
        self.assertEqual(self.cli('process')[1]['waiting'],1);self.assertEqual(self.life.auth.calls,[])
        self.life.now+=1
        self.assertEqual(self.cli('process')[1]['completed'],1);self.assertEqual(self.life.auth.calls,[UID])
        self.assertEqual(self.cli('process')[1]['examined'],0);self.assertEqual(self.life.auth.calls,[UID])
        self.assertEqual(self.life.worker.store.get(rid)['deleteAfter'],created+GRACE_SECONDS)
    def test_partial_profile_failure_and_retry_do_not_delete_auth_twice(self):
        self.life.seed(UID,1);self.life.docs.fail_delete=True
        _,first=self.cli('process');self.assertEqual(first['completed'],0)
        self.assertEqual(self.life.docs.read('accountDeletionIntents/'+UID)['status'],'retry_wait')
        self.life.docs.fail_delete=False;self.life.now+=2
        self.assertEqual(self.cli('process')[1]['completed'],1);self.assertEqual(self.life.auth.calls,[UID])
    def test_cancel_wins_after_page_read_and_no_delete(self):
        self.life.seed(UID,1);old=self.life.bridge.materialize_due
        def race(uid,clock):
            intent=self.life.docs.docs['accountDeletionIntents/'+uid]
            intent.update(status='cancel_requested',cancelRequestedAt=intent['requestedAt'],cancelAuthTime=intent['consentAuthTime'])
            return old(uid,clock)
        self.life.bridge.materialize_due=race
        self.assertEqual(self.cli('process')[1]['waiting'],1)
        self.assertEqual(self.cli('process')[1]['cancelled'],1);self.assertEqual(self.life.auth.calls,[])
    def test_authentication_failure_is_nonzero_and_keeps_user(self):
        self.life.seed(UID,1);self.life.auth.valid_after[UID]=self.life.now*1000
        code,result=self.cli('process');self.assertEqual(code,1)
        self.assertEqual(result['errors'],{'authentication':1});self.assertEqual(self.life.auth.calls,[])
        self.assertIn(UID,self.life.auth.users);self.assertNotIn(UID,json.dumps(result))
    def test_unknown_cursor_commit_reports_uncertain_and_does_not_replay_effects(self):
        self.life.seed(UID,1);self.life.docs.finish_failure='after'
        code,result=self.cli('process');self.assertEqual(code,1);self.assertEqual(result['state'],'unconfirmed')
        self.life.docs.finish_failure=None
        self.assertEqual(self.cli('process')[1]['examined'],0);self.assertEqual(self.life.auth.calls,[UID])

    def test_export_real_batch_states_for_actual_mobile_parser(self):
        scenarios = {}
        def field(value):
            if isinstance(value, datetime):return {'timestampValue':value.isoformat().replace('+00:00','Z')}
            if isinstance(value, bool):return {'booleanValue':value}
            if type(value) is int:return {'integerValue':str(value)}
            if isinstance(value, str):return {'stringValue':value}
            if value is None:return {'nullValue':None}
            if isinstance(value, list):return {'arrayValue':{'values':[field(v) for v in value]}}
            if isinstance(value, dict):return {'mapValue':{'fields':{k:field(v) for k,v in value.items()}}}
            raise TypeError('Synthetic fixture serialization only')
        def capture(name):
            intent=self.life.docs.read('accountDeletionIntents/'+UID)
            document='projects/demo-dailypick10-deletion/databases/(default)/documents/accountDeletionIntents/'+UID
            scenarios[name]={'documentName':document,'response':[{'readTime':datetime.fromtimestamp(self.life.now,timezone.utc).isoformat().replace('+00:00','Z'),
                'found':{'name':document,'fields':{k:field(v) for k,v in intent.items()}}}],
                'authExists':UID in self.life.auth.users}
        self.life.seed(UID,1,due=False);capture('grace_period')
        self.life.now+=GRACE_SECONDS;capture('queued')
        self.life.docs.fail_delete=True;self.cli('process');capture('retry_wait')
        self.life.docs.fail_delete=False;self.life.now+=2;self.cli('process');capture('completed')
        self.setUp();self.life.seed(UID,1,due=False,cancel=True);self.life.now+=1;capture('cancel_requested')
        self.cli('confirm_cancellations');capture('cancelled')
        destination=Path(__file__).resolve().parents[1]/'synthetic-output'/'deletion-contract.json'
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps({'syntheticOnly':True,'source':'actual SparkBatchRuntime/bridge/worker with synthetic adapters','scenarios':scenarios},indent=2)+'\n',encoding='utf-8')
        self.assertFalse(scenarios['completed']['authExists'])
        self.assertTrue(scenarios['cancelled']['authExists'])
