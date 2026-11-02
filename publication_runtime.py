"""Reviewed runtime wiring, disabled by default; no import-time credential access."""
from datetime import datetime, timezone
import os

from expo_publication import ExpoNotifier
from github_publication import GitHubHTTP, GitHubRepository, WorkflowPublisherLock, REPOSITORY, WORKFLOW
from scan_publication import DailyPublisher
from weekly_publication import WeeklyPublisher


def _build_publisher(scanner, publisher_type, *, environment=None, request=None, post=None, clock=None, calendar=None):
    e = os.environ if environment is None else environment
    # Validate the explicit gate/context before accessing any credential or provider.
    if e.get('DAILYPICK_GUARDED_RUNTIME') != 'enabled-v1':
        raise RuntimeError('Guarded publication is disabled; operating approval is required')
    if (e.get('GITHUB_ACTIONS') != 'true' or e.get('GITHUB_REPOSITORY') != REPOSITORY or
            e.get('GITHUB_REF') != 'refs/heads/main' or e.get('GITHUB_JOB') != 'run-scanner' or
            e.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main' or
            e.get('GITHUB_EVENT_NAME') not in {'schedule', 'workflow_dispatch'}):
        raise RuntimeError('Approved publisher workflow context is required')
    if request is None or post is None:
        import requests
        request = request or requests.request
        post = post or requests.post
    repository = GitHubRepository(GitHubHTTP(e.get('GITHUB_TOKEN'), request))
    # Copy only public context. Do not retain token/Firebase JSON in the lock.
    fields = ('GITHUB_ACTIONS', 'GITHUB_REPOSITORY', 'GITHUB_REF', 'GITHUB_JOB',
              'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME', 'GITHUB_RUN_ID', 'GITHUB_SHA')
    repository._lock = WorkflowPublisherLock({k: e.get(k) for k in fields}, repository.get_run)
    notifier = ExpoNotifier(lambda: scanner.get_all_user_tokens(strict=True), post)
    kwargs = {'clock': clock or (lambda: datetime.now(timezone.utc))}
    if calendar is not None:
        kwargs['calendar'] = calendar
    return publisher_type(repository, notifier, **kwargs), e.get('GITHUB_RUN_ID')


def build_daily_publisher(scanner, **kwargs):
    return _build_publisher(scanner, DailyPublisher, **kwargs)


def build_weekly_publisher(scanner, **kwargs):
    return _build_publisher(scanner, WeeklyPublisher, **kwargs)
