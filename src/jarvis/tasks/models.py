"""Task state and persistence value objects (spec §3.2 `tasks/models.py`).

`Task.to_row()` / `Task.from_row()` convert between the dataclass and the flat
string-keyed representation `TaskStore` reads/writes to SQLite: enums <-> their string
value, datetimes <-> ISO-8601 UTC strings, bools <-> 0/1.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class TaskKind(StrEnum):
    """What a task is. There is one kind, on purpose.

    Splitting work into chat/research/coding/cowork (2026-08-18 to 2026-08-24) meant the
    voice model had to classify a request before it could hand it over — a decision it is
    badly placed to make, and one that put mail and code in separate boxes a single
    request often has to reach across. Claude works out what a request needs on its own,
    so the only routing left is "answer it myself" versus "hand it to Claude".
    """

    AGENT = "agent"

    @classmethod
    def _missing_(cls, value: object) -> "TaskKind | None":
        """Rows written before the collapse carry chat/research/coding/cowork."""
        return cls.AGENT if isinstance(value, str) else None


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ENUM_FIELDS: dict[str, type[StrEnum]] = {"kind": TaskKind, "status": TaskStatus}
_BOOL_FIELDS = frozenset({"callback_requested", "announced", "sms_sent"})
_DATETIME_FIELDS = frozenset({"created_at", "started_at", "finished_at"})


def to_utc_iso(value: datetime) -> str:
    """`value` as an ISO-8601 string, converted to UTC first (naive values are assumed UTC).

    Also how `TaskStore` builds the bounds it compares `created_at` against, so the
    stored strings and the query strings are produced by the same code.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat()


def _parse_datetime(value: str) -> datetime:
    """The inverse of `to_utc_iso`: always returns a tz-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _serialize_field(name: str, value: Any) -> Any:
    """One `Task` field's Python value, as its SQLite row representation."""
    if value is None:
        return None
    if name in _ENUM_FIELDS:
        return value.value if isinstance(value, StrEnum) else value
    if name in _BOOL_FIELDS:
        return int(value)
    if name in _DATETIME_FIELDS:
        return to_utc_iso(value)
    return value


def _deserialize_field(name: str, value: Any) -> Any:
    """The inverse of `_serialize_field`."""
    if value is None:
        return None
    if name in _ENUM_FIELDS:
        return _ENUM_FIELDS[name](value)
    if name in _BOOL_FIELDS:
        return bool(value)
    if name in _DATETIME_FIELDS:
        return _parse_datetime(value)
    return value


@dataclass
class Task:
    """One dispatched unit of subagent work (spec §3.2 `tasks/models.py`).

    Required fields come first (`id`, `kind`, `description`) so `Task(id=None,
    kind=TaskKind.AGENT, description="...")` works positionally; every remaining field
    is defaulted, in the order the spec lists them. `kind` survives the collapse to a
    single kind because the column does: old rows still carry the old words.
    """

    id: int | None
    kind: TaskKind
    description: str
    status: TaskStatus = TaskStatus.QUEUED
    project: str | None = None
    cwd: str | None = None
    model: str = "claude-opus-5"
    claude_session_id: str | None = None
    summary: str | None = None
    report_path: str | None = None
    error: str | None = None
    origin_channel: str = "local"
    origin_caller: str | None = None
    #: The voice session that dispatched this task. A call-back opens a *new* session, so
    #: this is how a task can be traced back to the conversation that started it.
    origin_session_id: str | None = None
    callback_requested: bool = False
    callback_number: str | None = None
    #: One line of "where we left off", written by the voice model when the call-back is
    #: arranged, and read out to it when the call-back opens.
    callback_note: str | None = None
    announced: bool = False
    sms_sent: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        """This task as a flat dict of SQLite column values."""
        return {f.name: _serialize_field(f.name, getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        """Build a `Task` back from a SQLite row (`sqlite3.Row` or any string-keyed mapping)."""
        data = dict(row)
        return cls(**{name: _deserialize_field(name, value) for name, value in data.items()})

    def short_status_line(self) -> str:
        """A speakable one-liner, e.g. 'task 3 (running): add README to ...'."""
        description = self.description
        if len(description) > 80:
            description = description[:80] + "…"
        return f"task {self.id} ({self.status}): {description}"
