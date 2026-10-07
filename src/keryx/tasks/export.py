"""Tasks as the JSON `keryx tasks list|show --json` print: what the Mac app reads.

An allowlist, not `dataclasses.asdict`: a field added to `Task` reaches the app only when
someone decides it should, and the caller's number never does.
"""

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from keryx.tasks.models import Task, TaskStatus

FIELDS = (
    "id", "status", "description", "project", "agent", "model", "cwd", "created_at",
    "started_at", "finished_at", "reported_at", "internal", "needs_restart", "summary",
    "error", "report_path", "input_tokens", "output_tokens", "cost_usd", "origin_channel",
    "callback_requested",
)

#: How many of a task's log entries `tasks show --json` carries, oldest-first, counted from
#: the end: a session can run for a long time, and the app wants the recent timeline, not
#: the whole history.
MAX_LOG_ENTRIES = 500

#: `TaskManager._append_log` writes `f"[{stamp}] {text}\n"` with `stamp = "%H:%M:%S"`.
_STAMP_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] (.*)$")
#: `TaskManager.followup` prepends this before logging a follow-up (`_append_log(task_id,
#: f"[followup] {text}")`); stripped from the text, kept as `"kind"`.
_FOLLOWUP_PREFIX = "[followup] "


def told(task: Task) -> bool | None:
    """Whether Keryx has told the owner: None while unfinished, and for housekeeping."""
    if task.internal or task.status not in {TaskStatus.DONE, TaskStatus.FAILED}:
        return None
    return task.reported_at is not None


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if isinstance(value, StrEnum) else value


def task_json(task: Task) -> dict[str, Any]:
    """`task` as the dict the Mac app's JSON decoder is written against (see `FIELDS`)."""
    data = {name: _plain(getattr(task, name)) for name in FIELDS}
    data["told"] = told(task)
    return data


def _kind_and_text(text: str) -> tuple[str, str]:
    """A log entry's `(kind, text)`: `followup` with the prefix stripped, else `progress`."""
    if text.startswith(_FOLLOWUP_PREFIX):
        return "followup", text[len(_FOLLOWUP_PREFIX):]
    return "progress", text


def parse_log(lines: list[str]) -> list[dict[str, Any]]:
    """`lines` (one task's `.log` file, oldest first) as `{"time", "kind", "text"}` entries.

    Each entry `_append_log` wrote starts with its `[HH:MM:SS] ` stamp, but the text is
    written with no escaping, so a `text` that itself contains a newline spills onto
    further lines that do *not* start with a stamp. Such a line is folded into the entry
    before it (joined with `"\\n"`); if there is no entry yet to fold it into, it becomes
    one of its own with `time: None` — still parsed for the `[followup] ` prefix, since
    that check does not depend on there having been a timestamp.
    """
    entries: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.rstrip("\n")
        match = _STAMP_RE.match(line)
        if match:
            kind, text = _kind_and_text(match.group(2))
            entries.append({"time": match.group(1), "kind": kind, "text": text})
        elif entries:
            entries[-1]["text"] += "\n" + line
        else:
            kind, text = _kind_and_text(line)
            entries.append({"time": None, "kind": kind, "text": text})
    return entries


def read_log(path: Path, *, limit: int = MAX_LOG_ENTRIES) -> tuple[list[dict[str, Any]], bool]:
    """`path`'s log entries, oldest first, kept to the last `limit`; `(entries, truncated)`.

    `([], False)` for a log that is missing or cannot be read — a task's log is best-effort
    from the moment it is written (`TaskManager._append_log` never raises either), so a
    reader that raised here would turn a quiet gap in one file into a broken `tasks show`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    entries = parse_log(text.splitlines())
    if len(entries) > limit:
        return entries[-limit:], True
    return entries, False
