"""SQLite-backed persistence for `Task` (spec §3.2 `tasks/store.py`).

One `sqlite3.Connection`, opened eagerly in `__init__` with `check_same_thread=False`
and autocommit (`isolation_level=None`), guarded by a `threading.Lock` since a single
sqlite3 connection is not safe for concurrent use even with `check_same_thread=False`.
Every public method is `async` but does its actual work synchronously in a worker
thread via `asyncio.to_thread`, so callers on the event loop never block on file I/O.
"""

import asyncio
import dataclasses
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from jarvis.tasks.models import Task, TaskStatus

log = logging.getLogger("jarvis.tasks.store")

_SCHEMA_VERSION = 1

# "id" is assigned by SQLite (AUTOINCREMENT) and is never a patchable field.
_UPDATABLE_FIELD_NAMES = {f.name for f in dataclasses.fields(Task)} - {"id"}

_CREATE_SCHEMA_VERSION_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
)

_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    project TEXT,
    cwd TEXT,
    model TEXT NOT NULL,
    claude_session_id TEXT,
    summary TEXT,
    report_path TEXT,
    error TEXT,
    origin_channel TEXT NOT NULL,
    origin_caller TEXT,
    callback_requested INTEGER NOT NULL DEFAULT 0,
    callback_number TEXT,
    announced INTEGER NOT NULL DEFAULT 0,
    sms_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at)"


class TaskStore:
    """Task persistence (spec §3.2 `tasks/store.py`). `":memory:"` is accepted for tests."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._conn: sqlite3.Connection | None = conn
        if self._path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        """Create the v1 schema if `schema_version` is empty. Runs synchronously in `__init__`."""
        with self._lock:
            conn = self._conn
            conn.execute(_CREATE_SCHEMA_VERSION_SQL)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute(_CREATE_TASKS_SQL)
                conn.execute(_CREATE_INDEX_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                log.info("created tasks schema v%d at %s", _SCHEMA_VERSION, self._path)

    async def create(self, task: Task) -> Task:
        """Insert `task` and return a copy with `id` assigned; `task` itself is untouched."""
        return await asyncio.to_thread(self._create_sync, task)

    def _create_sync(self, task: Task) -> Task:
        row = task.to_row()
        row.pop("id", None)
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})"
        with self._lock:
            cursor = self._conn.execute(sql, [row[name] for name in columns])
        return dataclasses.replace(task, id=cursor.lastrowid)

    async def get(self, task_id: int) -> Task | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(row) if row is not None else None

    async def update(self, task_id: int, **fields: object) -> Task:
        """Patch the given fields and return the fresh `Task`.

        Raises `ValueError` for an unknown field name, `KeyError` if `task_id` doesn't exist.
        """
        return await asyncio.to_thread(self._update_sync, task_id, fields)

    def _update_sync(self, task_id: int, patch: dict[str, object]) -> Task:
        unknown = set(patch) - _UPDATABLE_FIELD_NAMES
        if unknown:
            raise ValueError(f"unknown Task field(s): {', '.join(sorted(unknown))}")

        existing = self._get_sync(task_id)
        if existing is None:
            raise KeyError(task_id)
        if not patch:
            return existing

        updated = dataclasses.replace(existing, **patch)
        row = updated.to_row()
        row.pop("id")
        set_clause = ", ".join(f"{name} = ?" for name in row)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?", [*row.values(), task_id]
            )
        return updated

    def _list_sync(self, status: TaskStatus | None, limit: int) -> list[Task]:
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [Task.from_row(row) for row in rows]

    # NOTE: this method is named `list` per spec §3.2, so it must be defined *after*
    # every other annotation in this class that uses the builtin `list[...]` — once
    # `list` is bound as a class attribute, later annotations evaluated in the class
    # body would resolve to this method instead of the builtin.
    async def list(self, *, status: TaskStatus | None = None, limit: int = 20) -> list[Task]:
        """Tasks newest-first (`id DESC`), optionally filtered to one status."""
        return await asyncio.to_thread(self._list_sync, status, limit)

    async def count_created_since(self, since: datetime) -> int:
        """Number of tasks with `created_at >= since` (UTC ISO-8601 string comparison)."""
        return await asyncio.to_thread(self._count_created_since_sync, since)

    def _count_created_since_sync(self, since: datetime) -> int:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        else:
            since = since.astimezone(UTC)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ?", (since.isoformat(),)
            ).fetchone()
        return row["n"]

    async def close(self) -> None:
        """Close the connection. Idempotent: safe to call more than once."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
