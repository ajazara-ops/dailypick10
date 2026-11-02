"""Synthetic-only durable Git-like port. No subprocess, HTTP, tokens or credentials."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import json
from pathlib import Path
import threading

from scan_publication import Snapshot, CommitRejected, CommitUnknown, encoded, digest


class Crash(BaseException):
    pass


class Remote:
    def __init__(self, files=None, path=None):
        self.files = dict(files or {})
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.events, self.faults, self.run_states, self.retry_grants = [], {}, {}, set()
        self.commit_count = self.refresh_count = 0
        self.fail_reads = set()
        self.checkout_files = {}  # Deliberately stale; the coordinator must never use it.
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text('utf-8'))
            assert saved['syntheticOnly'] is True
            self.files = {k: base64.b64decode(v) for k, v in saved['files'].items()}

    @property
    def head(self):
        return digest(encoded({k: digest(v) for k, v in sorted(self.files.items())}))

    @contextmanager
    def publisher_lock(self, run_id):
        with self.lock:
            self.events.append(('lock', run_id))
            try:
                yield
            finally:
                self.events.append(('unlock', run_id))

    def refresh_main(self):
        self.refresh_count += 1
        self.events.append(('refresh_main', self.head))
        if self.refresh_count in self.fail_reads:
            raise OSError('synthetic read unavailable')
        return Snapshot(self.head, deepcopy(self.files))

    def commit(self, expected_head, changes):
        self.commit_count += 1
        number = self.commit_count
        self.events.append(('commit', number, tuple(sorted(changes))))
        if self.head != expected_head:
            raise CommitRejected('synthetic non-fast-forward')
        fault = self.faults.get(number)
        if fault == 'reject':
            raise CommitRejected('synthetic confirmed rejection')
        if fault == 'unknown_before':
            raise CommitUnknown('synthetic unknown acknowledgement')
        for key, value in changes.items():
            if key.startswith('daily_data/') and key in self.files and self.files[key] != value:
                raise CommitRejected('history is immutable')
        next_files = {**self.files, **deepcopy(changes)}
        if self.path:
            payload = {'syntheticOnly': True, 'files': {k: base64.b64encode(v).decode() for k, v in next_files.items()}}
            temporary = self.path.with_suffix('.tmp')
            temporary.write_bytes(encoded(payload))
            temporary.replace(self.path)
        self.files = next_files
        if fault == 'unknown_after':
            raise CommitUnknown('synthetic acknowledgement lost after atomic commit')
        if fault == 'crash_after':
            raise Crash('synthetic process exit')
        return self.head

    def run_state(self, run_id):
        return self.run_states.get(run_id, 'unknown')

    def retry_authorized(self, key, run_id):
        return (key, run_id) in self.retry_grants


class Notifier:
    def __init__(self, remote, outcome='accepted'):
        self.remote, self.outcome = remote, outcome
        self.calls = []

    def send(self, key, title, body):
        self.calls.append((key, title, body))
        self.remote.events.append(('send', key))
        entries = json.loads(self.remote.files['scan_state.json'])['entries']
        assert any(e['phase'] == 'published' and e['notification']['state'] == 'claimed'
                   and e['notification']['key'] == key for e in entries.values())
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome
