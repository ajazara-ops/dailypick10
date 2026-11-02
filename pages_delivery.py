"""Pages handoff after Git publication; no deployment API or import-time I/O.

The workflow owns the concurrency slot. All mutable state lives in scan_state.json.
An uncertain deployment is verification-only. A notice claim is never retried.
"""
from copy import deepcopy
from pathlib import Path
import json
import re
from urllib.parse import urlsplit
import uuid

from scan_publication import DailyPublisher, STATE_PATH, _ledger, encoded, digest
from pages_assets import load_policy, validate_policy, asset_plan, write_archive, verify_archive

MANIFEST = 'publication-manifest.json'
DEPLOY_STEP = 'Deploy verified Pages artifact'


def configuration(e):
    if e.get('DAILYPICK_PAGES_SOURCE') != 'workflow-confirmed-v1':
        raise RuntimeError('Pages Source is unverified; collection and delivery are disabled')
    if e.get('DAILYPICK_PAGES_ROUTE') != 'verified-v1':
        raise RuntimeError('App JSON redirect/canonical route is unverified; runtime disabled')
    base = e.get('DAILYPICK_PAGES_URL', '').rstrip('/')
    url = urlsplit(base)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.port not in (None, 443) or
            '..' in url.path or '\\' in base):
        raise RuntimeError('Approved HTTPS Pages URL is required')
    attempt = e.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[1-9][0-9]*', attempt):
        raise RuntimeError('Explicit Actions run attempt is required')
    return {'baseUrl': base, 'runId': str(e['GITHUB_RUN_ID']),
            'runAttempt': attempt, 'workflowSha': e['GITHUB_SHA']}


def manifest(row):
    return {k: deepcopy(row[k]) for k in ('id', 'publicationSha', 'contentFingerprint', 'files', 'assetFiles', 'keys', 'attempt')}


class PublicJSON:
    """Single GET per exact file; never forwards an authenticated GitHub header.

    Cache headers are diagnostic, not proof. Every response must match raw bytes.
    The embedded attempt/commit manifest differentiates an older site build.
    """
    def __init__(self, base_url, get):
        self.base_url, self.get = base_url, get

    def inspect(self, row):
        expected = {MANIFEST: digest(encoded(manifest(row))), **row['files']}
        for path, wanted in expected.items():
            try:
                response = self.get(self.base_url + '/' + path,
                    params={'publication': row['id'], 'attempt': row['attempt']['id']},
                    headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'},
                    timeout=30, allow_redirects=False)
                if response.status_code != 200:
                    return 'public_file_unavailable'
                body = response.content
                if not isinstance(body, bytes) or len(body) > 16 * 1024 * 1024:
                    return 'public_file_invalid'
                json.loads(body)
                if digest(body) != wanted:
                    if path == MANIFEST:
                        return 'old_or_different_publication'
                    age = response.headers.get('Age', '')
                    return 'cached_hash_mismatch' if str(age).isdigit() and int(age) > 0 else 'partial_or_changed_file'
            except Exception:
                return 'public_response_unknown'
        return 'verified'


class PagesDelivery:
    def __init__(self, publisher, config, public, asset_policy=None):
        self.publisher, self.repository = publisher, publisher.repository
        self.config, self.public = dict(config), public
        self.run_id = self.config['runId']
        self.asset_policy = validate_policy(asset_policy) if asset_policy is not None else load_policy()

    def _save(self, snapshot, ledger):
        result = self.publisher._commit(snapshot, {STATE_PATH: encoded(ledger)})
        if result is None:
            raise RuntimeError('delivery_state_write_unconfirmed')
        return result

    def _load(self, delivery_id):
        snapshot = self.repository.refresh_main()
        ledger = _ledger(snapshot)
        row = ledger.get('deliveries', {}).get(delivery_id)
        if not isinstance(row, dict):
            raise RuntimeError('unknown_delivery')
        return snapshot, ledger, row

    def _unchanged(self, snapshot, row):
        return self.repository.content_fingerprint(snapshot) == row['contentFingerprint'] and all(
                   isinstance(snapshot.files.get(p), bytes) and digest(snapshot.files[p]) == h
                   for p, h in row['files'].items())

    def prepare(self, keys):
        """No collection and no deploy here; capture one immutable Git snapshot."""
        with self.repository.publisher_lock(self.run_id):
            snapshot = self.repository.refresh_main()
            ledger = _ledger(snapshot)
            entries = ledger['entries']
            keys = sorted(set(keys))
            if any(k not in entries or entries[k].get('phase') != 'published' for k in keys):
                raise RuntimeError('unconfirmed_publication')
            keys = [k for k in keys if entries[k]['notification']['state'] == 'ready']
            if not keys:
                return {'action': 'none'}
            links = {entries[k].get('deliveryId') for k in keys} - {None}
            if links:
                if len(links) != 1 or any(entries[k].get('deliveryId') not in links for k in keys):
                    raise RuntimeError('mixed_delivery_requires_separate_resume')
                row = ledger['deliveries'][links.pop()]
                if not self._unchanged(snapshot, row):
                    raise RuntimeError('published_files_superseded_no_rollback')
                if row['deployment']['state'] != 'prepared':
                    return {'action': 'verify', 'handoff': manifest(row)}
                old = row['attempt']
                if old['runId'] == self.run_id and old['runAttempt'] == self.config['runAttempt']:
                    return {'action': 'build', 'handoff': manifest(row)}
                if self.repository.run_state(old['runId']) not in {'success','failure','cancelled','timed_out','skipped'}:
                    raise RuntimeError('prior_delivery_owner_active_or_unknown')
                # Safe retry only before the durable deploy claim; artifact not submitted.
                row['attempt'] = self._attempt()
                row['artifact'] = None
                row.pop('preparedArchive',None)
                self._save(snapshot, ledger)
                return {'action': 'build', 'handoff': manifest(row)}
            files = {}
            for key in keys:
                entry = entries[key]
                path = entry['outputPath']
                if digest(snapshot.files[path]) != entry['outputHash']:
                    raise RuntimeError('immutable_publication_hash_mismatch')
                files[path] = entry['outputHash']
                companion = 'history_index.json' if entry['mode'] == 'weekly' else 'todays_recommendation.json'
                files[companion] = digest(snapshot.files[companion])
            # Publication SHA points to data, not to this subsequent ledger-only commit.
            site_snapshot = self.repository.site_snapshot_at(snapshot.head,self.asset_policy)
            bodies = asset_plan(site_snapshot,ledger,self.asset_policy)
            identity = {'publicationSha': snapshot.head, 'files': files, 'keys': keys,
                        'assetFiles': {p:digest(b) for p,b in bodies.items()},
                        'contentFingerprint': self.repository.content_fingerprint(snapshot)}
            delivery_id = digest(encoded(identity))
            row = {**identity, 'id': delivery_id, 'attempt': self._attempt(), 'artifact': None,
                   'deployment': {'state': 'prepared'}, 'public': {'state': 'pending'}}
            ledger.setdefault('deliveries', {})[delivery_id] = row
            for key in keys:
                entries[key]['deliveryId'] = delivery_id
            self._save(snapshot, ledger)
            return {'action': 'build', 'handoff': manifest(row)}

    def _attempt(self):
        return {'id': uuid.uuid4().hex, **{k: self.config[k] for k in ('runId','runAttempt','workflowSha')}}

    def stage(self, handoff, archive):
        """Build a positive-only tar from Git blobs, then inspect that actual tar."""
        with self.repository.publisher_lock(self.run_id):
            snapshot, ledger, row = self._load(handoff['id'])
            if (manifest(row) != handoff or row['deployment']['state'] != 'prepared' or
                    row['attempt']['runId'] != self.run_id or
                    row['attempt']['runAttempt'] != self.config['runAttempt'] or
                    not self._unchanged(snapshot, row)):
                raise RuntimeError('stale_artifact_handoff')
            frozen = self.repository.site_snapshot_at(row['publicationSha'],self.asset_policy)
            if self.repository.content_fingerprint(frozen) != row['contentFingerprint']:
                raise RuntimeError('artifact_source_commit_mismatch')
            bodies=asset_plan(frozen,ledger,self.asset_policy)
            if {p:digest(b) for p,b in bodies.items()} != row['assetFiles']:
                raise RuntimeError('artifact_git_json_mismatch')
            bodies[MANIFEST]=encoded(handoff)
            proof=write_archive(archive,bodies)
            row['preparedArchive']={**proof,'attemptId':row['attempt']['id']}
            self._save(snapshot,ledger)
            return {'state':'artifact_validated',**proof}

    def check_archive(self, handoff, archive):
        with self.repository.publisher_lock(self.run_id):
            snapshot, _, row=self._load(handoff['id'])
            if manifest(row)!=handoff or not self._unchanged(snapshot,row):
                raise RuntimeError('stale_artifact_handoff')
            proof=verify_archive(archive,{**row['assetFiles'],MANIFEST:digest(encoded(handoff))})
            if {**proof,'attemptId':row['attempt']['id']}!=row.get('preparedArchive'):
                raise RuntimeError('prepared_archive_changed')
            return proof

    def _artifact(self, row, artifact_id):
        if not re.fullmatch(r'[1-9][0-9]*', str(artifact_id)):
            raise RuntimeError('invalid_artifact_id')
        info = self.repository._get('/actions/artifacts/' + str(artifact_id))
        attempt = row['attempt']
        if (info.get('id') != int(artifact_id) or info.get('expired') is not False or
                info.get('name') != 'pages-' + attempt['id'] or
                info.get('workflow_run', {}).get('id') != int(attempt['runId']) or
                info['workflow_run'].get('head_sha') != attempt['workflowSha'] or
                not re.fullmatch(r'sha256:[0-9a-f]{64}', str(info.get('digest')))):
            raise RuntimeError('artifact_identity_unverified')
        return {'id': str(artifact_id), 'digest': info['digest'], 'name': info['name']}

    def claim(self, handoff, artifact_id, archive):
        """Durable claim immediately before official deploy; only one permission."""
        with self.repository.publisher_lock(self.run_id):
            self.check_archive(handoff,archive)
            snapshot, ledger, row = self._load(handoff['id'])
            if (manifest(row) != handoff or row['deployment']['state'] != 'prepared' or
                    row['attempt']['runId'] != self.run_id or
                    row['attempt']['runAttempt'] != self.config['runAttempt'] or
                    not self._unchanged(snapshot, row)):
                raise RuntimeError('deployment_already_claimed_or_stale')
            artifact = self._artifact(row, artifact_id)
            row['artifact'] = artifact
            row['deployment'] = {'state': 'claimed'}
            self._save(snapshot, ledger)
            return {'state': 'claimed', 'artifactName': artifact['name']}

    def _deployment_evidence(self, row):
        """Read the exact run attempt's named official step, not CLI assertions."""
        attempt = row['attempt']
        jobs = self.repository._get('/actions/runs/' + attempt['runId'] +
                                    '/attempts/' + attempt['runAttempt'] + '/jobs')
        rows = jobs.get('jobs', [])
        if not isinstance(rows, list) or jobs.get('total_count') != len(rows):
            return 'jobs_incomplete'
        matches = [j for j in rows if j.get('name') == 'run-scanner' and
                   j.get('run_id') == int(attempt['runId']) and j.get('head_sha') == attempt['workflowSha']]
        if len(matches) != 1:
            return 'job_identity_unverified'
        steps = [s for s in matches[0].get('steps', []) if s.get('name') == DEPLOY_STEP]
        if len(steps) != 1 or steps[0].get('status') != 'completed':
            return 'deployment_pending'
        if steps[0].get('conclusion') != 'success':
            # Even a timeout/failure can follow an accepted API call. No blind resubmit.
            return 'deployment_failure_or_unknown'
        if self._artifact(row, row['artifact']['id']) != row['artifact']:
            return 'artifact_changed'
        return 'success'

    def verify(self, delivery_id):
        with self.repository.publisher_lock(self.run_id):
            snapshot, ledger, row = self._load(delivery_id)
            if row['deployment']['state'] not in {'claimed', 'success'} or not row.get('artifact'):
                raise RuntimeError('deployment_not_claimed')
            if not self._unchanged(snapshot, row):
                return {'state': 'pending', 'reason': 'published_files_superseded_no_rollback'}
            try:
                evidence = self._deployment_evidence(row)
            except Exception:
                evidence = 'deployment_response_unknown'
            row['deployment']['observation'] = evidence
            if evidence == 'success':
                row['deployment']['state'] = 'success'
                public = self.public.inspect(row)
            else:
                public = 'not_checked'
            row['public'] = {'state': public}
            snapshot = self._save(snapshot, ledger)
            if public != 'verified':
                return {'state': 'pending', 'reason': evidence if evidence != 'success' else public}
            results = {}
            for key in row['keys']:
                entry = ledger['entries'][key]
                notice = entry['notification']
                if notice['state'] != 'ready':
                    results[key] = notice['state']  # Includes unknown/claimed: never resend.
                    continue
                payload = entry.get('noticePayload')
                if not isinstance(payload, dict) or set(payload) != {'title','body'}:
                    raise RuntimeError('notification_payload_unavailable')
                notice.update(state='claimed', runId=self.run_id, deliveryId=delivery_id)
                snapshot = self._save(snapshot, ledger)
                try:
                    ack = self.publisher.notifier.send(notice['key'], payload['title'], payload['body'])
                    ack = ack if ack in {'accepted','rejected','no_recipients'} else 'unknown'
                except Exception:
                    ack = 'unknown'
                notice['state'] = ack
                snapshot = self._save(snapshot, ledger)
                results[key] = ack
            return {'state': 'verified', 'notification': results}
