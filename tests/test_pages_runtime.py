"""Actual scanner + Git-data + continuation CLI + Expo; synthetic ports only."""
from copy import deepcopy
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from fake_github import GitServer, Response
import test_guarded_daily as fixtures
from test_scan_publication import NOW, facts
from test_weekly_publication import WEEK_NOW, sources
from publication_runtime import build_daily_publisher, build_weekly_publisher
from github_publication import RepositoryUnavailable
from pages_cli import main as pages_main, build_delivery
from pages_delivery import MANIFEST, DEPLOY_STEP
from scan_publication import encoded
from scripts.resolve_scan_event import resolve_event
from test_pages_assets import site_fixture


def pages_environment(server, run='1001'):
    return {**server.environment(run), 'GITHUB_RUN_ATTEMPT':'1',
            'DAILYPICK_PAGES_SOURCE':'workflow-confirmed-v1',
            'DAILYPICK_PAGES_URL':'https://synthetic.invalid/dailypick10'}


class PagesRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.harness=fixtures.GuardedScannerTests('test_unconfigured_cli_boundary_has_zero_callbacks')
        self.harness.setUp(); self.addCleanup(self.harness.doCleanups)
        self.scanner=self.harness.scanner
        files,self.policy=site_fixture()
        self.server=GitServer(files); self.env=pages_environment(self.server)
        self.artifacts={}; self.jobs={}; self.web={}; self.http_status=200; self.age='0'
        self.posts=[]; self.token_reads=[]; self.gets=[]; self.now=NOW
        self.post_error=False; self.job_error=False
        self.scanner.get_all_user_tokens=self.tokens
        self.result=Path('result.json');self.handoff=Path('handoff.json');self.archive=Path('artifact.tar')

    def tokens(self,strict=False):
        self.token_reads.append(strict)
        ledger=json.loads(self.server.files['scan_state.json'])
        self.assertTrue(any(r['public']['state']=='verified' for r in ledger['deliveries'].values()))
        self.assertTrue(any(e['notification']['state']=='claimed' for e in ledger['entries'].values()))
        return ['ExpoPushToken[synthetic-pages-only]']

    def post(self,url,**kwargs):
        self.posts.append(kwargs['json'])
        if self.post_error: raise TimeoutError('synthetic')
        return Response(200,{'data':{'status':'ok','id':'synthetic-ticket'}})

    def request(self,method,url,**kwargs):
        if '/actions/artifacts/' in url:
            self.assertEqual(method,'GET')
            return Response(200,self.artifacts[int(url.rsplit('/',1)[1])])
        if '/attempts/' in url and url.endswith('/jobs'):
            self.assertEqual(method,'GET')
            if self.job_error:raise TimeoutError('synthetic')
            return Response(200,self.jobs[url.split('/runs/')[1]])
        return self.server.request(method,url,**kwargs)

    def get(self,url,**kwargs):
        self.gets.append((url,kwargs))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('Authorization',kwargs['headers'])
        path=url.split('/dailypick10/')[1]
        return SimpleNamespace(status_code=self.http_status,content=self.web.get(path,b'{}'),headers={'Age':self.age})

    def factory(self,scanner):
        return build_daily_publisher(scanner,environment=self.env,request=self.request,
            post=self.post,clock=lambda:self.now,calendar=facts)

    def delivery(self):
        return build_delivery(environment=self.env,request=self.request,post=self.post,get=self.get,scanner=self.scanner,asset_policy=self.policy)

    def scan(self,target='KR'):
        return self.scanner.main(['--mode','daily','--target',target,'--date','2026-11-02','--publication-result',str(self.result)],runtime_factory=self.factory)

    def cli(self,command,*args):
        return pages_main([command,'--handoff',str(self.handoff),*args],factory=self.delivery)

    def prepare(self):
        result=self.cli('prepare','--result',str(self.result))
        return result

    def row(self):
        handoff=json.loads(self.handoff.read_text())['handoff']
        return json.loads(self.server.files['scan_state.json'])['deliveries'][handoff['id']]

    def build(self):
        row=self.row()
        self.cli('stage','--archive',str(self.archive))
        with tarfile.open(self.archive,'r:') as t:
            self.web={path:t.extractfile(path).read() for path in [MANIFEST,*row['files']]}
        self.artifacts[71]={'id':71,'name':'pages-'+row['attempt']['id'],'expired':False,
            'digest':'sha256:'+'a'*64,'workflow_run':{'id':int(row['attempt']['runId']),'head_sha':row['attempt']['workflowSha']}}
        return row

    def claim(self):
        self.cli('claim','--artifact-id','71','--archive',str(self.archive))

    def deployed(self,conclusion='success',status='completed'):
        row=self.row();attempt=row['attempt']
        self.jobs[attempt['runId']+'/attempts/'+attempt['runAttempt']+'/jobs']={
            'total_count':1,'jobs':[{'name':'run-scanner','run_id':int(attempt['runId']),
            'head_sha':attempt['workflowSha'],'steps':[{'name':DEPLOY_STEP,'status':status,'conclusion':conclusion}]}]}

    def ready(self):
        self.scan();self.prepare();self.build();self.claim();self.deployed()

    def new_run(self):
        self.server.runs[self.env['GITHUB_RUN_ID']].update(status='completed',conclusion='failure')
        self.env=pages_environment(self.server,'1002')

    def test_actual_cli_defers_tokens_then_exact_public_build_notifies_once(self):
        self.ready();counts=deepcopy(self.harness.counts)
        self.assertEqual(self.posts,[]);self.assertEqual(self.token_reads,[])
        row=self.row()
        self.assertNotEqual(row['publicationSha'],row['attempt']['workflowSha'])
        self.assertEqual(self.cli('verify')['state'],'verified')
        self.assertEqual(len(self.posts),1);self.assertEqual(self.token_reads,[True])
        self.new_run();self.assertEqual(self.scan()['state'],'noop')
        self.assertEqual(self.prepare()['action'],'none')
        self.assertEqual(self.harness.counts,counts);self.assertEqual(len(self.posts),1)
        self.assertNotIn(b'ExpoPushToken',b''.join(self.server.files.values()))

    def test_unreviewed_legacy_history_stops_pages_before_notification(self):
        path='daily_data/2026-11-04_035611Z_US_daily.json'
        self.server.advance({path:encoded({'synthetic':True})})
        self.scan()
        with self.assertRaisesRegex(RepositoryUnavailable,'unreviewed_legacy_history_requires_review'):
            self.prepare()
        self.assertEqual(self.posts,[])
        self.assertEqual(self.token_reads,[])
        self.assertFalse(self.archive.exists())

    def test_source_unknown_blocks_actual_cli_before_credentials_and_quotes(self):
        class Guard(dict):
            def get(self,k,default=None):
                if k in {'GITHUB_TOKEN','FIREBASE_CREDENTIALS'}:raise AssertionError('secret accessed')
                return super().get(k,default)
        self.env=Guard({**self.env,'DAILYPICK_PAGES_SOURCE':''})
        with self.assertRaisesRegex(RuntimeError,'Source is unverified'):self.scan()
        self.assertEqual(self.server.calls,[]);self.assertEqual(sum(self.harness.counts.values()),0)

    def test_unverified_route_blocks_collection_before_transport(self):
        self.env.pop('DAILYPICK_PAGES_ROUTE')
        with self.assertRaisesRegex(RuntimeError,'route is unverified'):self.scan()
        self.assertEqual(self.server.calls,[]);self.assertEqual(sum(self.harness.counts.values()),0)

    def test_actual_archive_change_is_rejected_before_artifact_claim(self):
        self.scan();self.prepare();self.build()
        with self.archive.open('ab') as f:f.write(b'synthetic extra')
        with self.assertRaisesRegex(ValueError,'trailer'):self.claim()
        self.assertEqual(self.row()['deployment']['state'],'prepared');self.assertEqual(self.posts,[])

    def test_wrong_artifact_commit_or_run_cannot_claim(self):
        self.scan();self.prepare();self.build()
        self.artifacts[71]['workflow_run']['head_sha']='f'*40
        with self.assertRaisesRegex(RuntimeError,'artifact_identity'):self.claim()
        self.assertEqual(self.row()['deployment']['state'],'prepared')

    def test_ambiguous_claim_commit_readback_allows_only_one_deploy_permission(self):
        self.scan();self.prepare();self.build()
        self.server.patch_faults[self.server.patch_count+1]='unknown_after'
        self.claim()
        with self.assertRaisesRegex(RuntimeError,'already_claimed'):self.claim()

    def test_claim_commit_not_landed_has_no_deploy_permission(self):
        self.scan();self.prepare();self.build()
        self.server.patch_faults[self.server.patch_count+1]='unknown_before'
        with self.assertRaisesRegex(RuntimeError,'write_unconfirmed'):self.claim()
        self.assertEqual(self.row()['deployment']['state'],'prepared');self.assertEqual(self.posts,[])

    def test_pending_deploy_resumes_read_only_proof_without_scan_or_resubmit(self):
        self.ready();self.deployed(None,'in_progress');counts=deepcopy(self.harness.counts)
        with self.assertRaisesRegex(RuntimeError,'deployment_pending'):self.cli('verify')
        self.new_run();ident=self.row()['id']
        self.assertEqual(self.cli('resume','--delivery-id',ident)['action'],'verify')
        self.deployed();self.cli('verify')
        self.assertEqual(self.harness.counts,counts);self.assertEqual(len(self.posts),1)

    def test_failed_or_unknown_deploy_never_redeploys_or_notifies(self):
        self.ready();self.deployed('failure')
        with self.assertRaisesRegex(RuntimeError,'failure_or_unknown'):self.cli('verify')
        self.job_error=True
        with self.assertRaisesRegex(RuntimeError,'response_unknown'):self.cli('verify')
        self.new_run();self.assertEqual(self.cli('resume','--delivery-id',self.row()['id'])['action'],'verify')
        self.assertEqual(self.posts,[])

    def test_prior_manifest_cache_and_partial_file_are_distinct_pending_results(self):
        self.ready();saved=deepcopy(self.web)
        self.web[MANIFEST]=encoded({'publicationSha':'0'*40})
        with self.assertRaisesRegex(RuntimeError,'old_or_different'):self.cli('verify')
        self.web=saved;self.web['todays_recommendation.json']=b'{}';self.age='90'
        with self.assertRaisesRegex(RuntimeError,'cached_hash_mismatch'):self.cli('verify')
        self.age='0'
        with self.assertRaisesRegex(RuntimeError,'partial_or_changed_file'):self.cli('verify')
        self.assertEqual(self.posts,[])

    def test_redirect_or_missing_file_is_not_success(self):
        self.ready()
        for status in (302,404,503):
            self.http_status=status
            with self.assertRaisesRegex(RuntimeError,'public_file_unavailable'):self.cli('verify')
        self.assertEqual(self.posts,[])

    def test_public_cache_lag_can_later_verify_without_data_recollection(self):
        self.ready();saved=deepcopy(self.web);counts=deepcopy(self.harness.counts)
        self.web['todays_recommendation.json']=b'{}'
        with self.assertRaises(RuntimeError):self.cli('verify')
        self.web=saved;self.new_run();self.cli('resume','--delivery-id',self.row()['id']);self.cli('verify')
        self.assertEqual(self.harness.counts,counts);self.assertEqual(len(self.posts),1)

    def test_unknown_notification_ack_never_resends_after_successful_pages(self):
        self.ready();self.post_error=True;result=self.cli('verify')
        self.assertEqual(list(result['notification'].values()),['unknown'])
        self.cli('verify');self.assertEqual(len(self.posts),1)

    def test_notice_claim_write_failure_reads_no_recipients(self):
        self.ready();self.server.patch_faults[self.server.patch_count+2]='unknown_before'
        with self.assertRaisesRegex(RuntimeError,'write_unconfirmed'):self.cli('verify')
        self.assertEqual(self.posts,[]);self.assertEqual(self.token_reads,[])

    def test_crash_after_notice_claim_is_not_resent(self):
        self.ready()
        def crash(*a,**k):raise KeyboardInterrupt('synthetic crash')
        self.post=crash
        with self.assertRaises(KeyboardInterrupt):self.cli('verify')
        self.post_error=False
        result=self.cli('verify')
        self.assertEqual(list(result['notification'].values()),['claimed'])
        self.assertEqual(self.token_reads,[True])

    def test_build_failure_can_resume_before_deploy_claim_but_unknown_owner_cannot(self):
        self.scan();self.prepare();ident=self.row()['id'];old=self.row()['attempt']['id']
        self.env=pages_environment(self.server,'1002')
        with self.assertRaisesRegex(RuntimeError,'owner_active_or_unknown'):self.cli('resume','--delivery-id',ident)
        self.server.runs['1001'].update(status='completed',conclusion='failure')
        self.assertEqual(self.cli('resume','--delivery-id',ident)['action'],'build')
        self.assertNotEqual(old,self.row()['attempt']['id'])
        self.assertEqual(self.harness.counts['price'],100)

    def test_superseded_latest_cannot_be_rolled_back_by_old_delivery(self):
        self.ready();self.server.advance({'todays_recommendation.json':encoded({'newer':True})})
        with self.assertRaisesRegex(RuntimeError,'superseded_no_rollback'):self.cli('verify')
        with self.assertRaisesRegex(RuntimeError,'superseded_no_rollback'):self.cli('resume','--delivery-id',self.row()['id'])
        self.assertEqual(self.posts,[])

    def test_unrelated_new_site_asset_blocks_old_frozen_commit_deployment(self):
        self.scan();self.prepare();self.build()
        self.server.advance({'index.html':b'newer user website'})
        with self.assertRaisesRegex(RuntimeError,'stale_artifact_handoff'):self.claim()
        self.assertEqual(self.posts,[])

    def test_all_markets_without_two_explicit_session_keys_fails_before_scan(self):
        with self.assertRaisesRegex(ValueError,'one_explicit_daily_market_required'):self.scan('ALL')
        self.assertEqual(self.posts,[]);self.assertEqual(self.harness.counts['price'],0)

    def test_weekly_actual_collector_and_index_go_through_same_delivery_cli(self):
        files,self.policy=site_fixture(sources())
        self.server=GitServer(files);self.env=pages_environment(self.server);self.now=WEEK_NOW
        self.scanner.is_market_session=lambda *_:True
        self.scanner.evaluation_session=lambda market,day:day
        quotes=[]
        def quote(item,day):
            quotes.append((item['market'],day));return {'price':120 if item['market']=='KR' else 11,'quoteAsOf':day,'hasSplit':False}
        self.scanner.exit_quote=quote
        def factory(scanner):
            return build_weekly_publisher(scanner,environment=self.env,request=self.request,post=self.post,clock=lambda:self.now)
        args=['--mode','weekly','--date','2026-11-21','--publication-result',str(self.result)]
        self.scanner.main(args,weekly_runtime_factory=factory)
        self.assertEqual(self.posts,[])
        self.prepare();self.build();self.claim();self.deployed();self.cli('verify')
        self.assertIn('history_index.json',self.row()['files']);self.assertEqual(len(self.posts),1)
        self.new_run();self.scanner.main(args,weekly_runtime_factory=factory)
        self.assertEqual(self.prepare()['action'],'none');self.assertEqual(len(quotes),2)

    def test_resume_intent_never_falls_back_to_daily_or_changes_crons(self):
        self.assertEqual(resolve_event('workflow_dispatch',{'inputs':{'mode':'pages_resume','delivery_id':'a'*64}}),('pages_resume','ALL',''))
        with self.assertRaises(ValueError):resolve_event('workflow_dispatch',{'inputs':{'mode':'pages_resume'}})
        with self.assertRaisesRegex(ValueError,'session_unverified'):resolve_event('schedule',{'schedule':'30 9 * * 1-5'})


if __name__=='__main__':unittest.main()
