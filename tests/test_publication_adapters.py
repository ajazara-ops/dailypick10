from contextlib import nullcontext
import json
import unittest

from github_publication import GitHubHTTP, GitHubRepository, RepositoryUnavailable, WorkflowPublisherLock, git_blob
from scan_publication import DailyPublisher, CommitRejected, encoded
from expo_publication import ExpoNotifier, EXPO_URL
from fake_github import GitServer, Response
from fake_repository import Notifier
from test_scan_publication import NOW, facts, output


class GitAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = GitServer()
        self.repo = GitHubRepository(GitHubHTTP('synthetic-only-token', self.server.request), lambda _: nullcontext())

    def test_reads_only_allowed_blobs_and_verifies_hash(self):
        self.server.advance({'scan_state.json': encoded({'schemaVersion':1,'entries':{}}),
                             'private-unrelated.txt': b'synthetic-do-not-read'})
        snap=self.repo.refresh_main();self.assertEqual(json.loads(snap.files['scan_state.json'])['entries'],{})
        blobs=[p.rsplit('/',1)[1] for m,p,_ in self.server.calls if p.startswith('/git/blobs/')]
        self.assertEqual(blobs,[git_blob(self.server.files['scan_state.json'])])
        self.assertNotIn('private-unrelated.txt',snap.files)

    def test_commit_preserves_base_and_uses_expected_parent_nonforced_reference(self):
        old=self.repo.refresh_main();self.repo.commit(old.head,{'scan_state.json':encoded({'entries':{}})})
        self.assertEqual(self.server.files['unrelated.txt'],b'keep')
        self.assertEqual(self.server.files['.github/workflows/identity-check.yml'],b'preserve')
        commits=[data for method,path,data in self.server.calls if method=='POST' and path=='/git/commits']
        self.assertEqual(commits[0]['parents'],[old.head])
        self.assertEqual(self.server.patch_count,1)
        self.assertEqual(json.loads(self.repo.refresh_main().files['scan_state.json']),{'entries':{}})

    def test_stale_head_has_no_object_or_reference_write(self):
        head=self.server.head;self.server.advance({'other.txt':b'new'})
        with self.assertRaises(CommitRejected):self.repo.commit(head,{'scan_state.json':encoded({})})
        self.assertTrue(all(method=='GET' for method,_,_ in self.server.calls))

    def test_race_at_reference_update_is_rejected_without_overwrite(self):
        head=self.server.head;self.server.before_patch=lambda:self.server.advance({'other.txt':b'concurrent'})
        with self.assertRaises(CommitRejected):self.repo.commit(head,{'scan_state.json':encoded({})})
        self.assertEqual(self.server.files['other.txt'],b'concurrent');self.assertNotIn('scan_state.json',self.server.files)

    def test_object_response_loss_never_attempts_reference_update(self):
        self.server.fail_object_creation=True;head=self.server.head
        with self.assertRaises(CommitRejected):self.repo.commit(head,{'scan_state.json':encoded({})})
        self.assertEqual(self.server.head,head);self.assertEqual(self.server.patch_count,0)

    def test_unknown_update_is_read_back_by_coordinator_without_repush(self):
        self.server.patch_faults[2]='unknown_after'
        sends=[]
        class Notice:
            def send(_, *args):sends.append(args);return 'accepted'
        publisher=DailyPublisher(self.repo,Notice(),clock=lambda:NOW,calendar=facts)
        result=publisher.run('KR','1001',output)
        self.assertEqual(result['state'],'published');self.assertEqual(len(sends),1)
        self.assertEqual(self.server.patch_count,4)
        before=len(self.server.calls)
        self.assertEqual(publisher.run('KR','1002',lambda *_:self.fail('duplicate collect'))['state'],'noop')
        self.assertTrue(all(m=='GET' for m,_,_ in self.server.calls[before:]));self.assertEqual(len(sends),1)

    def test_unknown_unapplied_publication_never_notifies_or_steals(self):
        self.server.patch_faults[2]='unknown_before'
        class Notice:
            def send(_, *args):raise AssertionError('unconfirmed publication notified')
        publisher=DailyPublisher(self.repo,Notice(),clock=lambda:NOW,calendar=facts)
        self.assertEqual(publisher.run('KR','1001',output)['state'],'unconfirmed')
        result=publisher.run('KR','1002',lambda *_:self.fail('must not recollect'))
        self.assertEqual(result['reason'],'owner_active_or_unknown');self.assertEqual(self.server.patch_count,2)

    def test_history_rewrite_or_unrelated_path_never_updates_main(self):
        path='daily_data/2026-11-02_KR_daily.json';self.server.advance({path:encoded({'old':True})})
        for changes in [{path:encoded({'new':True})},{'../secrets.json':encoded({})},{'.github/workflows/daily_scan.yml':b'{}'}]:
            with self.subTest(paths=list(changes)),self.assertRaises(CommitRejected):
                self.repo.commit(self.server.head,changes)
        self.assertEqual(self.server.patch_count,0)

    def test_corrupt_blob_and_truncated_tree_fail_closed(self):
        self.server.advance({'scan_state.json':encoded({})});self.server.corrupt_blob=True
        with self.assertRaises(RepositoryUnavailable):self.repo.refresh_main().files['scan_state.json']
        self.server.corrupt_blob=False;self.server.truncated=True
        with self.assertRaises(RepositoryUnavailable):self.repo.refresh_main()

    def test_missing_lock_and_unknown_owner_do_not_grant_retry(self):
        repo=GitHubRepository(self.repo.transport)
        with self.assertRaises(RepositoryUnavailable):repo.publisher_lock('1001')
        self.assertEqual(repo.run_state('1001'),'unknown');self.assertFalse(repo.retry_authorized('daily:KR:2026-11-02','1002'))

    def test_workflow_lock_verifies_active_identity_and_preserves_unknown(self):
        env=self.server.environment();lock=WorkflowPublisherLock(env,self.repo.get_run)
        with lock('1001'):pass
        self.server.runs['1001']['status']='completed'
        with self.assertRaises(RepositoryUnavailable),lock('1001'):pass
        env['GITHUB_JOB']='other-job'
        with self.assertRaises(RepositoryUnavailable),WorkflowPublisherLock(env,self.repo.get_run)('1001'):pass

    def test_http_errors_hide_secrets_and_never_follow_redirects(self):
        calls=[]
        def request(*args,**kwargs):
            calls.append((args,kwargs));raise TimeoutError('synthetic-secret-value')
        client=GitHubHTTP('synthetic-secret-value',request)
        with self.assertRaises(RepositoryUnavailable) as error:client.request('GET','/repos/ajazara-ops/dailypick10/git/ref/heads/main')
        self.assertNotIn('synthetic-secret-value',str(error.exception));self.assertNotIn('synthetic-secret-value',repr(client))
        self.assertIs(calls[0][1]['allow_redirects'],False)
        with self.assertRaises(RepositoryUnavailable):client.request('GET','https://other.invalid')
        self.assertEqual(len(calls),1)


class ExpoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tokens=['ExpoPushToken[synthetic-a]','ExpoPushToken[synthetic-b]']
        self.key='daily-'+'1'*64;self.calls=[];self.responses=[]

    def post(self,url,**kwargs):
        self.assertEqual(url,EXPO_URL);self.assertIs(kwargs['allow_redirects'],False)
        self.calls.append(kwargs)
        response=self.responses.pop(0) if self.responses else Response(200,{'data':{'status':'ok','id':'synthetic-ticket'}})
        if isinstance(response,Exception):raise response
        return response

    def test_deduplicates_recipients_and_preserves_payload_and_stable_key(self):
        notifier=ExpoNotifier(lambda:self.tokens+self.tokens,self.post)
        self.assertEqual(notifier.send(self.key,'title','body'),'accepted');self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0]['json']['data'],{'title':'title','message':'body','notificationKey':self.key})
        self.assertEqual(self.calls[0]['json']['channelId'],'default')

    def test_empty_recipients_and_lookup_failure_are_distinct(self):
        self.assertEqual(ExpoNotifier(lambda:[],self.post).send(self.key,'t','b'),'no_recipients')
        def fail():raise ValueError('synthetic recipient failure')
        self.assertEqual(ExpoNotifier(fail,self.post).send(self.key,'t','b'),'unknown');self.assertEqual(self.calls,[])

    def test_unknown_response_stops_remaining_recipients_without_retry(self):
        self.responses=[TimeoutError('synthetic response lost')]
        self.assertEqual(ExpoNotifier(lambda:self.tokens,self.post).send(self.key,'t','b'),'unknown')
        self.assertEqual(len(self.calls),1)

    def test_explicit_ticket_rejection_continues_other_recipients_once(self):
        self.responses=[Response(200,{'data':{'status':'error','details':{'error':'DeviceNotRegistered'}}})]
        self.assertEqual(ExpoNotifier(lambda:self.tokens,self.post).send(self.key,'t','b'),'rejected')
        self.assertEqual(len(self.calls),2)

    def test_rate_limit_server_error_redirect_or_malformed_ticket_stops(self):
        for response in [Response(429),Response(500),Response(302),Response(200,{'data':{'status':'ok'}})]:
            with self.subTest(status=response.status_code):
                self.calls=[];self.responses=[response]
                self.assertEqual(ExpoNotifier(lambda:self.tokens,self.post).send(self.key,'t','b'),'unknown')
                self.assertEqual(len(self.calls),1)

    def test_invalid_key_or_token_never_sends(self):
        with self.assertRaises(ValueError):ExpoNotifier(lambda:self.tokens,self.post).send('invalid','t','b')
        self.assertEqual(ExpoNotifier(lambda:['invalid'],self.post).send(self.key,'t','b'),'unknown')
        self.assertEqual(self.calls,[])
