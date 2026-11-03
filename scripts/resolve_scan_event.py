"""Resolve scheduled intent from the event, never from the runner's wall clock."""
import json
import os
import re
from pathlib import Path

SCHEDULES = {
    "30 9 * * 1-5": ("daily", "KR"),
    "15 23 * * 1-5": ("daily", "US"),
    "0 8 * * 6": ("weekly_summary", "ALL"),
}


def resolve_event(event_name, event):
    if event_name == "schedule":
        if event.get("schedule") not in SCHEDULES:
            raise ValueError("Unknown scheduled event; refusing an ALL-market fallback")
        return SCHEDULES[event["schedule"]]
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        mode, target = inputs.get("mode"), inputs.get("target", "ALL")
        if mode == 'pages_resume':
            if not re.fullmatch(r'[0-9a-f]{64}', str(inputs.get('delivery_id', ''))):
                raise ValueError('Existing delivery ID is required; no collection on resume')
            return mode, 'ALL'
        if mode not in {"daily", "weekly", "weekly_summary"} or target not in {"US", "KR", "ALL"}:
            raise ValueError("Explicit valid mode and target are required")
        return mode, target
    raise ValueError("Unsupported workflow event")


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text("utf-8"))
    mode, target = resolve_event(os.environ["GITHUB_EVENT_NAME"], event)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"mode={mode}\ntarget={target}\n")
