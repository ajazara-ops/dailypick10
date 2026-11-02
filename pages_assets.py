"""Positive-only Pages packaging from authenticated immutable Git blobs.

Known historical bytes are pinned, not reselected/recalculated. New reports
need exact paths, existing validators and a matching confirmed ledger record.
No external asset URL is fetched and no checkout directory is recursively copied.
"""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

from scan_publication import encoded, digest, validate_daily, session_key
from weekly_publication import validate_weekly, weekly_key

DAILY = re.compile(r'daily_data/(\d{4}-\d{2}-\d{2})_(KR|US)_daily\.json')
WEEKLY = re.compile(r'weekly_reports/(\d{4}-\d{2}-\d{2})_v3_weekly\.json')
STATIC = frozenset({'CNAME', 'app-ads.txt', 'google37c103c6e1de47ae.html',
    'index.html', 'about/index.html', 'assets/site.css', 'assets/language.js',
    'blogger/privacy/index.html', 'dailypick10/index.html',
    'dailypick10/privacy/index.html', 'dailypick10/terms/index.html',
    'dailypick10/delete-account/index.html'})
COMPANIONS = frozenset({'todays_recommendation.json', 'history_index.json'})
RESERVED = 'publication-manifest.json'
SENSITIVE = frozenset({'privatekey','credentials','firebasecredentials','pushtoken',
    'expopushtoken','accesstoken','refreshtoken','password','userid','uid','email',
    'clientemail','serviceaccount','recipient','recipients','users'})
DAILY_FIELDS = frozenset({'schemaVersion','stocks','markets','generatedAt','timestamp',
    'analysisMethod','methodology','market_status','dominant_sectors','notification'})
WEEKLY_FIELDS = frozenset({'schemaVersion','reportKind','reportDate','generatedAt',
    'timestamp','period','stocks','performance','winnerRankings','sourceDiagnostics',
    'market_status','dominant_sectors','methodology','priceAdjustmentPolicy',
    'evaluationFrozen','legacyHistoryRecalculated'})
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 220 or
            any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-' for c in value) or
            value.startswith('/') or any(p in ('','.','..') or p.startswith('.') for p in value.split('/')) or
            str(PurePosixPath(value)) != value):
        raise ValueError('unsafe_asset_path')
    return value


def unique_paths(values):
    found=set()
    for value in values:
        path=safe_path(value)
        if path.casefold() in found:
            raise ValueError('duplicate_or_case_alias_path')
        found.add(path.casefold())


def git_blob(body):
    return hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()


def load_policy(path=None):
    value=json.loads(Path(path or Path(__file__).with_name('pages_site_manifest.json')).read_text('utf-8'))
    return validate_policy(value)


def validate_policy(value):
    if value.get('version') != 1 or value.get('customDomain') != 'ajazara.xyz':
        raise ValueError('unreviewed_site_policy')
    rows=value.get('files')
    if not isinstance(rows,list):raise ValueError('invalid_site_policy')
    unique_paths(r['path'] for r in rows)
    static=set()
    for row in rows:
        p=row['path'];kind=row.get('kind')
        if not re.fullmatch(r'[0-9a-f]{40}',str(row.get('gitBlobSha'))):raise ValueError('invalid_asset_git_hash')
        valid=(kind=='static' and p in STATIC or
               kind=='daily' and re.fullmatch(r'daily_data/\d{4}-\d{2}-\d{2}(?:_[A-Za-z0-9+-]+)*_daily\.json',p) or
               kind=='weekly' and re.fullmatch(r'weekly_reports/\d{4}-\d{2}-\d{2}(?:_v[23])?_weekly\.json',p) or
               kind=='latest' and p=='todays_recommendation.json' or
               kind=='index' and p=='history_index.json')
        if not valid:raise ValueError('unreviewed_asset_policy_path')
        if kind=='static':static.add(p)
    if static!=STATIC or not COMPANIONS <= {r['path'] for r in rows}:
        raise ValueError('missing_required_site_asset')
    return value


def candidate_path(path, policy):
    # Unknown/hidden/code/credential paths are excluded without reading their bytes.
    return path in {r['path'] for r in policy['files']} or bool(DAILY.fullmatch(path) or WEEKLY.fullmatch(path))


def _sensitive(value):
    if isinstance(value,dict):
        for k,v in value.items():
            if k in {'$ref','$schema','$include'}:raise ValueError('external_json_reference_not_allowed')
            if re.sub(r'[^a-z]','',k.lower()) in SENSITIVE:raise ValueError('private_field_in_public_data')
            _sensitive(v)
    elif isinstance(value,list):
        for v in value:_sensitive(v)
    elif isinstance(value,str):
        if ('PRIVATE KEY-----' in value or 'ExpoPushToken[' in value or 'ExponentPushToken[' in value):
            raise ValueError('credential_in_public_data')


def parse_json(body):
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate_json_key')
            out[k]=v
        return out
    def invalid(_):raise ValueError('nonfinite_json')
    return json.loads(body,object_pairs_hook=pairs,parse_constant=invalid)


def _new_report(path, body, ledger):
    data=parse_json(body);_sensitive(data)
    entries=[(key,e) for key,e in ledger['entries'].items() if
             e.get('phase')=='published' and e.get('outputPath')==path and e.get('outputHash')==digest(body)]
    if len(entries)!=1:raise ValueError('asset_publication_provenance_unverified')
    key,e=entries[0]
    if DAILY.fullmatch(path):
        day,market=DAILY.fullmatch(path).groups()
        if (e.get('mode')!='daily' or e.get('market')!=market or e.get('session')!=day or
                key!=session_key('daily',market,day) or not isinstance(data,dict) or set(data)-DAILY_FIELDS):
            raise ValueError('daily_asset_schema_or_identity')
        validate_daily(data,market,day,e['generatedAt'])
    elif WEEKLY.fullmatch(path):
        period=e.get('period',{})
        if (e.get('mode')!='weekly' or key!=weekly_key(period) or
                period.get('publicationDate')!=WEEKLY.fullmatch(path).group(1) or
                not isinstance(data,dict) or set(data)-WEEKLY_FIELDS):
            raise ValueError('weekly_asset_schema_or_identity')
        validate_weekly(data,period,e['generatedAt'])
    else:raise ValueError('new_report_path_not_allowed')


def asset_plan(snapshot, ledger, policy):
    """Return only allowed bytes; known site/history changes require a new review."""
    policy=validate_policy(policy)
    pinned={r['path']:r for r in policy['files']}
    paths=sorted(p for p in snapshot.files if candidate_path(p,policy))
    unique_paths(paths)
    if not set(pinned)<=set(paths):raise ValueError('required_asset_missing')
    bodies={};total=0
    for path in paths:
        body=snapshot.files[path]
        if not isinstance(body,bytes) or len(body)>MAX_FILE:raise ValueError('asset_size_or_type')
        total+=len(body)
        if total>MAX_TOTAL:raise ValueError('artifact_size_limit')
        known=pinned.get(path)
        unchanged=known is not None and git_blob(body)==known['gitBlobSha']
        if known and path not in COMPANIONS and not unchanged:
            raise ValueError('reviewed_asset_changed')
        if path=='CNAME' and body.strip()!=b'ajazara.xyz':raise ValueError('custom_domain_changed')
        if path not in COMPANIONS and not unchanged:_new_report(path,body,ledger)
        bodies[path]=body
    latest='todays_recommendation.json'
    if git_blob(bodies[latest])!=pinned[latest]['gitBlobSha']:
        match=[p for p,b in bodies.items() if DAILY.fullmatch(p) and b==bodies[latest]]
        if len(match)!=1:raise ValueError('latest_not_bound_to_published_archive')
        _new_report(match[0],bodies[latest],ledger)
    index=parse_json(bodies['history_index.json'])
    if not isinstance(index,list):raise ValueError('invalid_public_index')
    seen=set()
    new_rows=0
    for row in index:
        if (not isinstance(row,dict) or set(row)-{'date','file','publishAt','reportKind'} or
                not isinstance(row.get('date'),str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',row['date'])):
            raise ValueError('invalid_public_index_row')
        path=safe_path(row.get('file'))
        if (path not in bodies or not path.startswith('weekly_reports/'+row['date']+'_') or path in seen):
            raise ValueError('index_reference_not_local_unique_report')
        seen.add(path)
        if path not in pinned:
            matches=[e for e in ledger['entries'].values() if e.get('outputPath')==path and e.get('phase')=='published']
            if len(matches)!=1 or matches[0].get('indexEntry')!=row:raise ValueError('index_provenance_unverified')
            new_rows+=1
    if not {p for p,r in pinned.items() if r['kind']=='weekly'} <= seen:
        raise ValueError('reviewed_history_index_entry_missing')
    if git_blob(bodies['history_index.json'])!=pinned['history_index.json']['gitBlobSha'] and not new_rows:
        raise ValueError('changed_index_without_new_publication')
    return bodies


def verify_archive(path, expected):
    """Inspect the actual uploaded tar, never extract it. Reject extra members."""
    unique_paths(expected)
    metadata=Path(path).lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
        raise ValueError('archive_must_be_single_regular_file')
    seen=set();total=0;offset=0
    with tarfile.open(path,mode='r:') as archive:
        for member in archive:
            name=safe_path(member.name)
            if (name in seen or name not in expected or not member.isfile() or member.linkname or
                    member.pax_headers or member.size>MAX_FILE or member.size<0 or
                    member.offset!=offset or member.offset_data!=offset+512 or
                    member.uid!=0 or member.gid!=0 or member.mode!=0o644 or member.mtime!=0):
                raise ValueError('unexpected_archive_member')
            seen.add(name);total+=member.size
            if total>MAX_TOTAL:raise ValueError('artifact_size_limit')
            stream=archive.extractfile(member)
            body=stream.read(MAX_FILE+1) if stream else None
            if body is None or len(body)!=member.size or digest(body)!=expected[name]:
                raise ValueError('archive_file_hash_mismatch')
            offset+=512+((member.size+511)//512)*512
    if seen!=set(expected):raise ValueError('archive_file_set_mismatch')
    expected_size=((offset+1024+10239)//10240)*10240
    with Path(path).open('rb') as raw:
        raw.seek(offset)
        tail=raw.read()
    if metadata.st_size!=expected_size or any(tail):
        raise ValueError('unexpected_archive_trailer')
    return {'sha256':digest(Path(path).read_bytes()),'fileCount':len(seen)}


def write_archive(path,bodies):
    unique_paths(bodies)
    dest=Path(path)
    # Exclusive creation: do not overwrite an existing file or follow its link.
    with dest.open('xb') as output:
        with tarfile.open(fileobj=output,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name,body in sorted(bodies.items()):
                member=tarfile.TarInfo(name);member.size=len(body);member.mode=0o644
                member.uid=member.gid=member.mtime=0
                archive.addfile(member,io.BytesIO(body))
    return verify_archive(dest,{p:digest(b) for p,b in bodies.items()})
