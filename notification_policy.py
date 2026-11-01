"""Read-only eligibility predicate; no account-deletion worker or credentials."""


def push_eligible(profile, blocked=False):
    """Fail closed on any deletion marker; caller must also consult tombstones."""
    return isinstance(profile, dict) and not blocked and 'deletionRequest' not in profile
