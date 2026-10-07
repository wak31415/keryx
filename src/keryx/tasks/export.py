"""Tasks as the JSON `keryx tasks list|show --json` print: what the Mac app reads.

An allowlist, not `dataclasses.asdict`: a field added to `Task` reaches the app only when
someone decides it should, and the caller's number never does.
"""

from datetime import datetime
from typing import Any

from keryx.tasks.models import Task, TaskStatus

FIELDS = (
    "id", "status", "description", "project", "agent", "model", "cwd", "created_at",
    "started_at", "finished_at", "reported_at", "internal", "needs_restart", "summary",
    "error", "report_path", "input_tokens", "output_tokens", "cost_usd", "origin_channel",
    "callback_requested",
)


def told(task: Task) -> bool | None:
    """Whether Keryx has told the owner: None while unfinished, and for housekeeping."""
    if task.internal or task.status not in {TaskStatus.DONE, TaskStatus.FAILED}:
        return None
    return task.reported_at is not None


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if hasattr(value, "value") else value  # StrEnum -> its value


def task_json(task: Task) -> dict[str, Any]:
    """`task` as the dict the Mac app's JSON decoder is written against (see `FIELDS`)."""
    data = {name: _plain(getattr(task, name)) for name in FIELDS}
    data["told"] = told(task)
    return data
