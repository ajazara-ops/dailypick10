"""Synthetic GitHub Git-data HTTP server, in memory only. Never opens a socket."""
from copy import deepcopy
import base64
import hashlib
import json

from github_publication import git_blob, WORKFLOW, REPOSITORY


def object_id(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return deepcopy(self.body)


class GitServer:
    def __init__(self, files=None):
        self.blobs, self.trees, self.commits = {}, {}, {}
        files = files or {'unrelated.txt': b'keep', '.github/workflows/identity-check.yml': b'preserve'}
        tree = self.make_tree(files)
        self.head = self.add_commit(tree, [])
        self.initial_head = self.head
        self.calls, self.runs, self.patch_faults = [], {}, {}
        self.patch_count = 0
        self.before_patch = None
        self.fail_object_creation = False
        self.corrupt_blob = self.truncated = False

    def make_tree(self, files):
        refs = {p: git_blob(b) for p, b in files.items()}
        self.blobs.update({git_blob(b): b for b in files.values()})
        tree = object_id(refs)
        self.trees[tree] = dict(files)
        return tree

    def add_commit(self, tree, parents):
        ident = object_id({'tree': tree, 'parents': parents})
        self.commits[ident] = {'sha': ident, 'tree': {'sha': tree}, 'parents': parents}
        return ident

    @property
    def files(self):
        return self.trees[self.commits[self.head]['tree']['sha']]

    def advance(self, changes):
        self.head = self.add_commit(self.make_tree({**self.files, **changes}), [self.head])

    def environment(self, run='1001', event='workflow_dispatch'):
        self.runs[str(run)] = {'id': int(run), 'status': 'in_progress', 'event': event,
                              'head_branch': 'main', 'head_sha': self.head, 'path': WORKFLOW}
        return {'DAILYPICK_GUARDED_RUNTIME': 'enabled-v1',
                'DAILYPICK_PAGES_SOURCE':'workflow-confirmed-v1',
                'DAILYPICK_PAGES_ROUTE':'verified-v1',
                'DAILYPICK_PAGES_URL':'https://synthetic.invalid/dailypick10',
                'GITHUB_RUN_ATTEMPT':'1', 'GITHUB_ACTIONS': 'true',
                'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
                'GITHUB_JOB': 'run-scanner', 'GITHUB_WORKFLOW_REF': f'{REPOSITORY}/{WORKFLOW}@refs/heads/main',
                'GITHUB_EVENT_NAME': event, 'GITHUB_RUN_ID': str(run), 'GITHUB_SHA': self.head,
                'GITHUB_TOKEN': 'synthetic-only-token'}

    def tree_body(self, ident):
        files = self.trees[ident];rows = []
        directories = set()
        for path, data in sorted(files.items()):
            if '/' not in path:
                rows.append({'path': path, 'type': 'blob', 'mode': '100644', 'sha': git_blob(data)})
            else:
                directories.add(path.split('/')[0])
        for directory in sorted(directories):
            subtree = self.make_tree({p[len(directory)+1:]: b for p,b in files.items() if p.startswith(directory+'/')})
            rows.append({'path': directory, 'type': 'tree', 'mode': '040000', 'sha': subtree})
        return {'sha': ident, 'truncated': self.truncated, 'tree': rows}

    def is_ancestor(self, ancestor, descendant):
        if ancestor == descendant:
            return True
        return any(self.is_ancestor(ancestor, p) for p in self.commits[descendant]['parents'])

    def request(self, method, url, **kwargs):
        prefix = f'https://api.github.com/repos/{REPOSITORY}'
        assert url.startswith(prefix + '/')
        assert kwargs['allow_redirects'] is False
        path = url[len(prefix):];data = kwargs.get('json')
        self.calls.append((method, path, deepcopy(data)))  # No auth header retained.
        if method == 'GET':
            if path == '/git/ref/heads/main':
                return Response(200, {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.head}})
            if path.startswith('/git/commits/'):
                return Response(200, self.commits[path.rsplit('/',1)[1]])
            if path.startswith('/git/trees/'):
                return Response(200, self.tree_body(path.rsplit('/',1)[1]))
            if path.startswith('/git/blobs/'):
                ident = path.rsplit('/',1)[1];body = b'corrupt' if self.corrupt_blob else self.blobs[ident]
                return Response(200, {'sha': ident, 'encoding': 'base64', 'content': base64.b64encode(body).decode()})
            if path.startswith('/actions/runs/'):
                run = self.runs.get(path.rsplit('/',1)[1])
                return Response(200, run) if run else Response(404)
        if method == 'POST':
            if self.fail_object_creation:
                raise TimeoutError('synthetic object response lost')
            if path == '/git/trees':
                files = dict(self.trees[data['base_tree']])
                for row in data['tree']:
                    assert row['mode'] == '100644' and row['type'] == 'blob'
                    files[row['path']] = row['content'].encode()
                return Response(201, {'sha': self.make_tree(files)})
            if path == '/git/commits':
                return Response(201, {'sha': self.add_commit(data['tree'], data['parents'])})
        if method == 'PATCH' and path == '/git/refs/heads/main':
            self.patch_count += 1
            if self.before_patch:
                callback,self.before_patch=self.before_patch,None;callback()
            assert data['force'] is False
            if not self.is_ancestor(self.head, data['sha']):
                return Response(422)
            fault = self.patch_faults.get(self.patch_count)
            if fault == 'unknown_before':
                raise TimeoutError('synthetic write acknowledgement unknown')
            if fault == 'reject':
                return Response(403)
            self.head = data['sha']
            if fault == 'unknown_after':
                raise TimeoutError('synthetic response lost after update')
            return Response(200, {'ref': 'refs/heads/main', 'object': {'sha': self.head}})
        raise AssertionError('unexpected synthetic endpoint')
