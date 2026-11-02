"""Explicit, self-contained synthetic test entry point. No live scanner CLI.

From any working directory: python -I path/to/scripts/test_session_publication.py
Only the candidate repo and installed Python packages are required. No sibling
baseline checkout, hidden runner, credentials, or network service is used.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ('test_scan_publication.py', 'test_completed_sessions.py', 'test_guarded_daily.py',
            'test_publication_adapters.py', 'test_publication_runtime.py')


def main(names=None):
    os.chdir(ROOT)
    sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append('blocked_external_call')
        raise AssertionError('Network/subprocess disabled during synthetic verification')

    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden), \
         patch.object(urllib.request, 'urlopen', forbidden), patch.object(subprocess, 'Popen', forbidden), \
         patch('platform.machine', return_value='AMD64'):
        # Avoid pandas' Windows version-query subprocess during import; no allowlist hole.
        suite = unittest.TestSuite()
        if names:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromNames(names))
        else:
            for pattern in PATTERNS:
                suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern=pattern))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {'testsRun': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'blockedExternalAttempts': len(attempts),
               'syntheticOnly': True, 'isolatedPython': bool(sys.flags.isolated),
               'success': result.wasSuccessful() and not attempts}
    (ROOT/'publication-test-results.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary))
    return 0 if summary['success'] else 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main(sys.argv[1:]))
