"""Real tar files, synthetic blobs only. No live Git/Pages/quote calls."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from fake_github import GitServer
from github_publication import GitHubHTTP, GitHubRepository
from pages_assets import (load_policy, validate_policy, asset_plan, write_archive,
    verify_archive, git_blob, safe_path, STATIC, RESERVED)
from scan_publication import encoded, digest
from test_scan_publication import output, NOW


def site_fixture(extra=None):
    """Same 329 path layout, explicitly synthetic contents and hashes."""
    policy=deepcopy(load_policy());files={}
    for row in policy['files']:
        p=row['path']
        if p=='CNAME':body=b'ajazara.xyz'
        elif row['kind']=='static':body=('Synthetic site fixture: '+p+'\n').encode()
        else:body=encoded({'stocks':[]})
        files[p]=body
    weekly=[r['path'] for r in policy['files'] if r['kind']=='weekly']
    files['history_index.json']=encoded([{'date':p.split('/')[1][:10],'file':p} for p in sorted(weekly,reverse=True)])
    files.update(extra or {})
    rows={r['path']:r for r in policy['files']}
    for p in (extra or {}):
        if p.startswith('daily_data/') and p not in rows:
            rows[p]={'path':p,'kind':'daily','gitBlobSha':git_blob(files[p])}
    for p,row in rows.items():row['gitBlobSha']=git_blob(files[p])
    policy['files']=list(rows.values())
    return files,policy


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.files,self.policy=site_fixture()
        self.server=GitServer(self.files)
        self.repo=GitHubRepository(GitHubHTTP('synthetic-only-token',self.server.request))
        self.ledger={'schemaVersion':1,'entries':{}}
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]);self.addCleanup(self.temp.cleanup)
        self.tar=Path(self.temp.name)/'artifact.tar'

    def plan(self):
        return asset_plan(self.repo.site_snapshot_at(self.server.head,self.policy),self.ledger,self.policy)

    def add_daily(self,change=None):
        day='2026-11-02';stamp=NOW.isoformat(timespec='seconds');p=f'daily_data/{day}_KR_daily.json'
        body=output('KR',day,{},stamp)
        if change:change(body)
        raw=encoded(body);key='daily:KR:'+day
        self.ledger['entries'][key]={'phase':'published','mode':'daily','market':'KR','session':day,
            'outputPath':p,'outputHash':digest(raw),'generatedAt':stamp}
        self.server.advance({p:raw,'todays_recommendation.json':raw})
        return p

    def custom_tar(self,members):
        with tarfile.open(self.tar,'w',format=tarfile.USTAR_FORMAT) as out:
            for name,body,kind,link in members:
                info=tarfile.TarInfo(name);info.mode=0o644;info.type=kind;info.linkname=link
                info.size=len(body) if kind==tarfile.REGTYPE else 0
                out.addfile(info,io.BytesIO(body) if kind==tarfile.REGTYPE else None)

    def test_production_manifest_matches_reviewed_329_layout_and_static_domain(self):
        policy=load_policy()
        self.assertEqual(policy['reviewedMain'],'c796be6ed00e0e4e7d0d589e4711a579999cb7e6')
        counts={k:sum(r['kind']==k for r in policy['files']) for k in ('static','latest','index','daily','weekly')}
        self.assertEqual(counts,{'static':12,'latest':1,'index':1,'daily':283,'weekly':32})
        rows={r['path']:r for r in policy['files']}
        self.assertEqual(rows['daily_data/2026-11-02_193919Z_KR_daily.json']['gitBlobSha'],'829a90cef78c8476c2837a2128c673ddc89d9056')
        self.assertEqual(rows['todays_recommendation.json']['gitBlobSha'],'829a90cef78c8476c2837a2128c673ddc89d9056')
        self.assertEqual(set(r['path'] for r in policy['files'] if r['kind']=='static'),STATIC)
        self.assertEqual(next(r['gitBlobSha'] for r in policy['files'] if r['path']=='CNAME'),git_blob(b'ajazara.xyz'))

    def test_actual_tar_preserves_all_329_paths_and_bytes_and_only_generated_manifest(self):
        bodies=self.plan();self.assertEqual(len(bodies),329)
        bodies[RESERVED]=encoded({'synthetic':True})
        proof=write_archive(self.tar,bodies)
        self.assertEqual(proof['fileCount'],330)
        self.assertEqual(verify_archive(self.tar,{p:digest(b) for p,b in bodies.items()}),proof)
        with tarfile.open(self.tar,'r:') as t:
            self.assertEqual(set(t.getnames()),set(bodies))
            for p,b in bodies.items():self.assertEqual(t.extractfile(p).read(),b)

    def test_unknown_code_private_hidden_files_are_not_read_or_packaged(self):
        excluded={'scan_state.json':b'synthetic state','.env':b'synthetic private',
                  'credentials.json':b'synthetic credential','tests/fake.json':b'synthetic test',
                  'unknown.html':b'unknown','daily_data/credentials.json':b'synthetic'}
        self.server.advance(excluded)
        bodies=self.plan()
        self.assertEqual(set(bodies),set(self.files))
        read={path.rsplit('/',1)[1] for method,path,_ in self.server.calls if path.startswith('/git/blobs/')}
        self.assertTrue(read.isdisjoint({git_blob(b) for b in excluded.values()}))

    def test_required_static_and_domain_changes_fail_closed(self):
        for path in ('CNAME','app-ads.txt','about/index.html'):
            with self.subTest(path=path):
                self.server=GitServer({**self.files,path:b'changed'})
                self.repo=GitHubRepository(GitHubHTTP('synthetic-only-token',self.server.request))
                with self.assertRaisesRegex(ValueError,'reviewed_asset_changed'):self.plan()

    def test_missing_legacy_or_required_asset_fails_instead_of_removing_url(self):
        files=dict(self.files);files.pop('assets/site.css')
        self.server=GitServer(files);self.repo=GitHubRepository(GitHubHTTP('synthetic-only-token',self.server.request))
        with self.assertRaisesRegex(ValueError,'required_asset_missing'):self.plan()

    def test_new_exact_daily_requires_matching_ledger_and_schema(self):
        p=self.add_daily();self.assertIn(p,self.plan())
        self.ledger['entries'].clear()
        with self.assertRaisesRegex(ValueError,'provenance_unverified'):self.plan()

    def test_new_daily_invalid_price_is_not_published_even_with_matching_hash(self):
        self.add_daily(lambda d:d['stocks'][0].update(currentPrice=None))
        with self.assertRaises(Exception):self.plan()

    def test_new_daily_extra_credential_or_user_fields_are_rejected(self):
        self.add_daily(lambda d:d['stocks'][0].update(pushToken='synthetic-private'))
        with self.assertRaisesRegex(ValueError,'private_field'):self.plan()

    def test_json_external_reference_is_rejected_without_resolving_it(self):
        self.add_daily(lambda d:d['stocks'][0].update({'$ref':'https://outside.invalid/private'}))
        with self.assertRaisesRegex(ValueError,'external_json_reference'):self.plan()

    def test_index_must_retain_reviewed_reports_and_have_new_publication(self):
        rows=json.loads(self.files['history_index.json'])
        self.server.advance({'history_index.json':encoded(rows[1:])})
        with self.assertRaisesRegex(ValueError,'history_index_entry_missing'):self.plan()
        self.server.advance({'history_index.json':encoded(list(reversed(rows)))})
        with self.assertRaisesRegex(ValueError,'without_new_publication'):self.plan()

    def test_new_index_cannot_point_outside_site_or_duplicate_report(self):
        path=next(p for p in self.files if p.startswith('weekly_reports/'))
        for rows in ([{'date':'2026-01-05','file':'https://outside.invalid/data.json'}],
                     [{'date':'2026-01-05','file':'../credentials.json'}],
                     [{'date':path.split('/')[1][:10],'file':path}]*2):
            self.server.advance({'history_index.json':encoded(rows)})
            with self.assertRaises(ValueError):self.plan()

    def test_policy_path_traversal_urls_case_aliases_and_duplicates_rejected(self):
        for path in ('../private','/etc/private','https://outside.invalid/x','a\\b','a//b','a/%2e%2e/x','.env','a/./b'):
            with self.subTest(path=path),self.assertRaises(ValueError):safe_path(path)
        for row in (deepcopy(self.policy['files'][0]),{**self.policy['files'][0],'path':self.policy['files'][0]['path'].upper()}):
            policy=deepcopy(self.policy);policy['files'].append(row)
            with self.assertRaises(ValueError):validate_policy(policy)

    def test_git_symlink_directory_or_duplicate_entry_is_rejected_without_blob_read(self):
        original=self.server.tree_body
        for scenario in ('link','duplicate','path'):
            def tree(ident):
                body=original(ident)
                if any(r['path']=='assets' for r in body['tree']):
                    if scenario=='link':
                        row=next(r for r in body['tree'] if r['path']=='assets');row.update(type='blob',mode='120000')
                    elif scenario=='duplicate':body['tree'].append(deepcopy(body['tree'][0]))
                    else:body['tree'].append({'path':'../private','mode':'100644','type':'blob','sha':'a'*40})
                return body
            self.server.tree_body=tree
            with self.subTest(scenario=scenario),self.assertRaises(Exception):self.plan()
        self.server.tree_body=original

    def test_tar_rejects_symlink_hardlink_absolute_and_traversal_without_extraction(self):
        for name,kind,link in [('a',tarfile.SYMTYPE,'/private'),('a',tarfile.LNKTYPE,'/private'),
                               ('/a',tarfile.REGTYPE,''),('../a',tarfile.REGTYPE,'')]:
            self.custom_tar([(name,b'x',kind,link)])
            with self.subTest(name=name,kind=kind),self.assertRaises(ValueError):verify_archive(self.tar,{'a':digest(b'x')})

    def test_tar_extra_hidden_duplicate_missing_and_wrong_bytes_rejected(self):
        cases=[ [('a',b'x',tarfile.REGTYPE,''),('.env',b'x',tarfile.REGTYPE,'')],
                [('a',b'x',tarfile.REGTYPE,'')]*2,
                [('b',b'x',tarfile.REGTYPE,'')], [('a',b'y',tarfile.REGTYPE,'')], [] ]
        for members in cases:
            self.custom_tar(members)
            with self.assertRaises(ValueError):verify_archive(self.tar,{'a':digest(b'x')})

    def test_tar_appended_payload_is_not_ignored_after_end_marker(self):
        write_archive(self.tar,{'a':b'x'})
        with self.tar.open('ab') as f:f.write(b'synthetic extra')
        with self.assertRaisesRegex(ValueError,'trailer'):verify_archive(self.tar,{'a':digest(b'x')})

    def test_archive_creation_cannot_overwrite_existing_file(self):
        self.tar.write_bytes(b'preserve existing')
        with self.assertRaises(FileExistsError):write_archive(self.tar,{'a':b'x'})
        self.assertEqual(self.tar.read_bytes(),b'preserve existing')


if __name__=='__main__':unittest.main()
