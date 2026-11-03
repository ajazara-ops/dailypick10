"""Read-only eligibility predicate; no account-deletion worker or credentials."""
import re


def push_eligible(profile, blocked=False):
    """Fail closed on any deletion marker; caller must also consult tombstones."""
    return isinstance(profile, dict) and not blocked and 'deletionRequest' not in profile


def confirmed_push_eligible(profile, state, intent):
    """Only no lifecycle, or matching server-confirmed cancellation, can send.

    Both authoritative reads must have succeeded; the caller must never turn a
    read failure into None. A recreated profile cannot erase a durable block.
    This cannot recall a push already accepted before a deletion request.
    """
    if not push_eligible(profile):
        return False
    if state is None and intent is None:
        return True
    if not isinstance(state, dict) or not isinstance(intent, dict):
        return False
    request_id = intent.get('requestId')
    return (type(intent.get('version')) is int and intent['version'] == 2 and
            intent.get('status') == 'cancelled' and isinstance(request_id, str) and
            re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', request_id) is not None and
            state.get('blocked') is False and state.get('jobId') == request_id)
