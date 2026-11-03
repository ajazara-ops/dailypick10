"""GitHub Git-data port; transport/lock are injected and have no import-time I/O.

The workflow owns the distributed publisher slot. Branch writes are non-forced
fast-forwards of a commit whose sole parent is the expected main HEAD. Read-back
is still required by DailyPublisher, including after an ambiguous PATCH response.
"""
import base64
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import re
import threading

from scan_publication import Snapshot, CommitRejected, CommitUnknown

REPOSITORY = 'ajazara-ops/dailypick10'
WORKFLOW = '.github/workflows/daily_scan.yml'
SHA = re.compile(r'[0-9a-f]{40}')
ARCHIVE = re.compile(r'daily_data/\d{4}-\d{2}-\d{2}_(?:KR|US)_daily\.json')
WEEKLY = re.compile(r'weekly_reports/\d{4}-\d{2}-\d{2}_v3_weekly\.json')
READ_HISTORY = re.compile(r'(?:daily_data/[A-Za-z0-9_.+-]+_daily\.json|weekly_reports/\d{4}-\d{2}-\d{2}(?:_v[23])?_weekly\.json)')


class RepositoryUnavailable(RuntimeError):
    pass


def permitted(path):
    return path in {'scan_state.json', 'todays_recommendation.json', 'history_index.json'} or bool(ARCHIVE.fullmatch(path) or WEEKLY.fullmatch(path))


def readable(path):
    return permitted(path) or bool(READ_HISTORY.fullmatch(path))


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise RepositoryUnavailable('invalid_git_object')
    return value


class GitHubHTTP:
    """Single-attempt HTTPS, fixed origin, no redirects or raw response logging."""
    def __init__(self, token, request):
        if not isinstance(token, str) or not token:
            raise RepositoryUnavailable('existing_workflow_token_required')
        self._token, self._request = token, request

    def __repr__(self):
        return '<GitHubHTTP credentials=redacted>'

    def request(self, method, path, payload=None):
        if (method not in {'GET', 'POST', 'PATCH'} or
                not path.startswith(f'/repos/{REPOSITORY}/') or
                any(c in path for c in ('?', '#', '..', '\\'))):
            raise RepositoryUnavailable('invalid_github_request')
        try:
            response = self._request(method, 'https://api.github.com' + path,
                headers={'Authorization': 'Bearer ' + self._token,
                         'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'},
                json=payload, timeout=30, allow_redirects=False)
            body = response.json() if 200 <= response.status_code < 300 else None
            return response.status_code, body
        except Exception:
            raise RepositoryUnavailable('github_response_unavailable') from None


class GitFiles(Mapping):
    """Lazy immutable blob reads at the captured commit, not mutable Contents URLs."""
    def __init__(self, repository, entries):
        self.repository, self.entries, self.cache = repository, entries, {}

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, path):
        blob = self.entries[path]
        if path not in self.cache:
            result = self.repository._get('/git/blobs/' + blob)
            if result.get('encoding') != 'base64' or result.get('sha') != blob:
                raise RepositoryUnavailable('invalid_blob_metadata')
            try:
                data = base64.b64decode(''.join(result['content'].split()), validate=True)
            except Exception:
                raise RepositoryUnavailable('invalid_blob_encoding') from None
            if git_blob(data) != blob:
                raise RepositoryUnavailable('blob_integrity_failure')
            self.cache[path] = data
        return self.cache[path]


class WorkflowPublisherLock:
    """Assert the existing Actions concurrency slot; never a TTL/lease-stealing lock.

Only this workflow can use the live port. External schedulers must dispatch it,
not run this code in an unrelated job. The thread lock covers local callers only.
"""
    def __init__(self, environment, get_run):
        self.environment, self.get_run = dict(environment), get_run
        self.local = threading.RLock()

    @contextmanager
    def __call__(self, run_id):
        e = self.environment
        if (e.get('GITHUB_ACTIONS') != 'true' or e.get('GITHUB_REPOSITORY') != REPOSITORY or
                e.get('GITHUB_REF') != 'refs/heads/main' or e.get('GITHUB_JOB') != 'run-scanner' or
                e.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main' or
                e.get('GITHUB_EVENT_NAME') not in {'schedule', 'workflow_dispatch'} or
                e.get('GITHUB_RUN_ID') != str(run_id) or not SHA.fullmatch(str(e.get('GITHUB_SHA')))):
            raise RepositoryUnavailable('workflow_publisher_slot_unverified')
        with self.local:
            info = self.get_run(run_id)
            if (info.get('id') != int(run_id) or info.get('status') != 'in_progress' or
                    info.get('event') != e['GITHUB_EVENT_NAME'] or info.get('head_branch') != 'main' or
                    info.get('path') != WORKFLOW or info.get('head_sha') != e.get('GITHUB_SHA')):
                raise RepositoryUnavailable('active_workflow_run_unverified')
            yield


class GitHubRepository:
    def __init__(self, transport, lock=None):
        self.transport, self._lock = transport, lock
        self.prefix = f'/repos/{REPOSITORY}'
        self._trees = {}

    def _get(self, suffix):
        status, result = self.transport.request('GET', self.prefix + suffix)
        if status != 200 or not isinstance(result, dict):
            raise RepositoryUnavailable('github_read_unavailable')
        return result

    def _head(self):
        ref = self._get('/git/ref/heads/main')
        if ref.get('ref') != 'refs/heads/main' or ref.get('object', {}).get('type') != 'commit':
            raise RepositoryUnavailable('invalid_main_reference')
        return sha(ref['object'].get('sha'))

    def publisher_lock(self, run_id):
        if self._lock is None:
            raise RepositoryUnavailable('publisher_lock_required')
        return self._lock(run_id)

    def refresh_main(self):
        return self.snapshot_at(self._head())

    def content_fingerprint(self, snapshot):
        """Entire repository root except the coordinator ledger (not just JSON).

        Prevent an old frozen Pages checkout from rolling back newer assets,
        another market, an index or code while retrying a delivery.
        """
        tree_sha = self._trees[snapshot.head]
        root = self._get('/git/trees/' + tree_sha)
        if root.get('sha') != tree_sha or root.get('truncated') is not False:
            raise RepositoryUnavailable('incomplete_delivery_tree')
        rows = sorted((r['path'], r['mode'], r['type'], sha(r['sha']))
                      for r in root['tree'] if r['path'] != 'scan_state.json')
        return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()

    def snapshot_at(self, head):
        head = sha(head)
        commit = self._get('/git/commits/' + head)
        if commit.get('sha') != head:
            raise RepositoryUnavailable('commit_identity_mismatch')
        tree_sha = sha(commit.get('tree', {}).get('sha'))
        # Only publication data trees; legacy snapshots are read-only inputs.
        entries = {}
        root = self._get('/git/trees/' + tree_sha)
        if (root.get('sha') != tree_sha or root.get('truncated') is not False or
                not isinstance(root.get('tree'), list)):
            raise RepositoryUnavailable('incomplete_root_tree')
        for row in root.get('tree', []):
            path = row.get('path', '')
            if path in {'daily_data', 'weekly_reports'}:
                if row.get('type') != 'tree':
                    raise RepositoryUnavailable('invalid_history_tree')
                tree = self._get('/git/trees/' + sha(row.get('sha')))
                if (tree.get('sha') != row['sha'] or tree.get('truncated') is not False or
                        not isinstance(tree.get('tree'), list)):
                    raise RepositoryUnavailable('incomplete_history_tree')
                for child in tree.get('tree', []):
                    full = path + '/' + child.get('path', '')
                    if readable(full):
                        self._entry(entries, full, child)
            elif readable(path):
                self._entry(entries, path, row)
        self._trees[head] = tree_sha
        return Snapshot(head, GitFiles(self, entries))

    def site_snapshot_at(self, head, policy):
        """Only traverse allowlisted site directories; never read excluded blobs."""
        from pages_assets import candidate_path, safe_path, validate_policy, unreviewed_legacy_history
        policy = validate_policy(policy)
        head = sha(head)
        commit = self._get('/git/commits/' + head)
        if commit.get('sha') != head:
            raise RepositoryUnavailable('commit_identity_mismatch')
        root_sha = sha(commit.get('tree', {}).get('sha'))
        known = {r['path'] for r in policy['files']}
        entries = {}
        def walk(tree_sha, prefix=''):
            tree = self._get('/git/trees/' + tree_sha)
            if tree.get('sha') != tree_sha or tree.get('truncated') is not False:
                raise RepositoryUnavailable('incomplete_site_tree')
            seen=set()
            for row in tree['tree']:
                name=row.get('path')
                if (not isinstance(name,str) or name in ('','.','..') or '/' in name or
                        '\\' in name or name.casefold() in seen):
                    raise RepositoryUnavailable('invalid_or_duplicate_site_tree_path')
                seen.add(name.casefold())
                if name.startswith('.'):
                    continue  # Hidden content is neither traversed nor downloaded.
                full=prefix+name
                try:safe_path(full)
                except ValueError:raise RepositoryUnavailable('unsafe_site_tree_path') from None
                if unreviewed_legacy_history(full,known):
                    # Do not download an unreviewed blob or silently omit a public history URL.
                    raise RepositoryUnavailable('unreviewed_legacy_history_requires_review')
                needed = candidate_path(full,policy)
                directory = full in {'daily_data','weekly_reports'} or any(p.startswith(full+'/') for p in known)
                if needed:
                    self._entry(entries,full,row)
                elif directory:
                    if row.get('type')!='tree' or row.get('mode')!='040000':
                        raise RepositoryUnavailable('unsafe_site_directory')
                    walk(sha(row.get('sha')),full+'/')
                # Any other file/directory is excluded without reading its contents.
        walk(root_sha)
        self._trees[head]=root_sha
        return Snapshot(head,GitFiles(self,entries))

    @staticmethod
    def _entry(entries, path, row):
        if row.get('type') != 'blob' or row.get('mode') != '100644' or path in entries:
            raise RepositoryUnavailable('unsafe_data_entry')
        entries[path] = sha(row.get('sha'))

    def _create(self, suffix, payload):
        status, result = self.transport.request('POST', self.prefix + suffix, payload)
        if status != 201 or not isinstance(result, dict):
            raise RepositoryUnavailable('git_object_creation_unconfirmed')
        return sha(result.get('sha'))

    def commit(self, expected_head, changes):
        # No ref write occurs before all validation and a second HEAD check.
        try:
            sha(expected_head)
            if not changes or any(not permitted(p) for p in changes):
                raise ValueError('unexpected_publication_path')
            current = self.refresh_main()
            if current.head != expected_head:
                raise ValueError('main_changed')
            rows = []
            for path, data in sorted(changes.items()):
                expected_type = list if path == 'history_index.json' else dict
                if not isinstance(data, bytes) or not isinstance(json.loads(data), expected_type):
                    raise ValueError('invalid_json_write')
                if (ARCHIVE.fullmatch(path) or WEEKLY.fullmatch(path)) and path in current.files and current.files[path] != data:
                    raise ValueError('immutable_history')
                rows.append({'path': path, 'type': 'blob', 'mode': '100644', 'content': data.decode('utf-8')})
            tree = self._create('/git/trees', {'base_tree': self._trees[current.head], 'tree': rows})
            commit = self._create('/git/commits', {'message': 'Record guarded stock publication',
                                  'tree': tree, 'parents': [expected_head]})
            if self._head() != expected_head:
                raise ValueError('main_changed_before_update')
        except Exception:
            # May leave unreachable Git objects, but this call has not updated main.
            raise CommitRejected('conditional_publication_not_attempted') from None
        try:
            status, result = self.transport.request('PATCH', self.prefix + '/git/refs/heads/main',
                                                    {'sha': commit, 'force': False})
        except Exception:
            raise CommitUnknown('reference_update_response_unknown') from None
        if status in {401, 403, 404, 409, 422}:
            raise CommitRejected('reference_update_rejected')
        if status != 200 or not isinstance(result, dict) or result.get('object', {}).get('sha') != commit:
            raise CommitUnknown('reference_update_unconfirmed')
        return commit

    def get_run(self, run_id):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id)):
            raise RepositoryUnavailable('invalid_run_id')
        return self._get('/actions/runs/' + str(run_id))

    def run_state(self, run_id):
        try:
            result = self.get_run(run_id)
            if result.get('id') != int(run_id) or result.get('path') != WORKFLOW:
                return 'unknown'
            if result.get('status') == 'completed':
                return result.get('conclusion', 'unknown')
            return 'running' if result.get('status') == 'in_progress' else 'unknown'
        except Exception:
            return 'unknown'

    def retry_authorized(self, key, run_id):
        # No implicit force/retry setting or age-based takeover in this runtime.
        return False
