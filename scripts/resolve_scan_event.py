"""Resolve scheduled intent from the event, never from the runner's wall clock."""
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

SCHEDULES = {
    "30 9 * * 1-5": ("daily", "KR"),
    "15 23 * * 1-5": ("daily", "US"),
    "0 8 * * 6": ("weekly_summary", "ALL"),
}


def resolve_event(event_name, event):
    if event_name == "schedule":
        # Cron identifies the rule, not a trustworthy occurrence/session date.
        # Never infer an intended historical session from the current clock.
        raise ValueError("scheduled_occurrence_session_unverified")
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        mode, target = inputs.get("mode"), inputs.get("target", "ALL")
        if mode == 'pages_resume':
            if not re.fullmatch(r'[0-9a-f]{64}', str(inputs.get('delivery_id', ''))):
                raise ValueError('Existing delivery ID is required; no collection on resume')
            return mode, 'ALL', ''
        if mode not in {"daily", "weekly", "weekly_summary"} or target not in {"US", "KR", "ALL"}:
            raise ValueError("Explicit valid mode and target are required")
        key = inputs.get('session_key')
        if not isinstance(key, str):
            raise ValueError('explicit_session_key_required')
        if mode == 'daily':
            match = re.fullmatch(r'daily:(KR|US):(\d{4}-\d{2}-\d{2})', key)
            if not match or target != match.group(1):
                raise ValueError('daily_key_target_mismatch')
            day = date.fromisoformat(match.group(2))
            if day.isoformat() != match.group(2):
                raise ValueError('noncanonical_session_date')
            return mode, target, day.isoformat()
        match = re.fullmatch(r'weekly:ALL:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})', key)
        if target != 'ALL' or not match:
            raise ValueError('weekly_key_target_mismatch')
        start, end, evaluation = (date.fromisoformat(value) for value in match.groups())
        if (start.weekday() != 0 or end != start + timedelta(days=4) or
                evaluation != end + timedelta(days=14)):
            raise ValueError('invalid_weekly_period_key')
        return mode, 'ALL', (evaluation + timedelta(days=1)).isoformat()
    raise ValueError("Unsupported workflow event")


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text("utf-8"))
    mode, target, session_date = resolve_event(os.environ["GITHUB_EVENT_NAME"], event)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"mode={mode}\ntarget={target}\nsession_date={session_date}\n")
