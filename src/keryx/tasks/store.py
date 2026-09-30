"""SQLite-backed persistence for `Task`.

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
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from keryx.config import secure_file
from keryx.tasks.models import ProjectUsage, Task, TaskStatus, to_utc_iso

log = logging.getLogger("keryx.tasks.store")

_SCHEMA_VERSION = 6

#: How many unreported tasks a call opens with. The owner is on a phone: past a handful, the
#: digest stops being a briefing and becomes a recital, and the rest keep until next time.
MAX_UNREPORTED = 5


def _escape_like(term: str) -> str:
    """`term` with the `LIKE` wildcards neutralised, for use with `ESCAPE '\\'`.

    Search terms come off a speech transcript, so an underscore or a percent sign in one
    is a literal character they said, never a pattern.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

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
    agent TEXT NOT NULL DEFAULT 'claude',
    claude_session_id TEXT,
    summary TEXT,
    report_path TEXT,
    error TEXT,
    origin_channel TEXT NOT NULL,
    origin_caller TEXT,
    origin_session_id TEXT,
    callback_requested INTEGER NOT NULL DEFAULT 0,
    callback_number TEXT,
    callback_note TEXT,
    announced INTEGER NOT NULL DEFAULT 0,
    sms_sent INTEGER NOT NULL DEFAULT 0,
    reported_at TEXT,
    internal INTEGER NOT NULL DEFAULT 0,
    needs_restart INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at)"

#: The digest at the top of every call asks exactly this question — finished, mine, not
#: yet said out loud — so it gets an index rather than a scan of every task ever run.
_CREATE_UNREPORTED_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_unreported ON tasks (reported_at, internal, status)"
)

#: v1 -> v2 (2026-08-24): a task remembers the call it came from, and what the call-back
#: should remind them of. Both are nullable, so old rows need nothing but the columns.
_V2_COLUMNS = ("origin_session_id TEXT", "callback_note TEXT")

#: v2 -> v3 (2026-08-25): a task remembers whether Keryx has actually told them about it,
#: and whether Keryx asked for it of its own accord. Rows written before this are treated
#: as already reported — see `_migrate`.
_V3_COLUMNS = ("reported_at TEXT", "internal INTEGER NOT NULL DEFAULT 0")
#: How long a statement waits for a lock another connection is holding, rather than
#: failing with "database is locked". Explicit because the contention here is *between
#: processes*, not between threads: `keryx serve` holds this database open for the life of
#: the service while `keryx tasks`, `keryx restart` and `keryx forget` open it from the
#: terminal — which the "the database runs ahead of the code" working agreement takes as
#: normal. Python's implicit default is 5 seconds; this says so out loud and raises it,
#: because a `keryx tasks list` that waits two extra seconds is fine and one that raises
#: mid-call is not.
BUSY_TIMEOUT_S = 15.0

#: v3 -> v4 (2026-08-26): a task can say it changed Keryx's own code and needs a restart
#: to take effect. Rows written before this never asked for one, which the default says.
_V4_COLUMNS = ("needs_restart INTEGER NOT NULL DEFAULT 0",)

#: v4 -> v5 (2026-09-24): a task remembers which coding agent ran it. Every row before this
#: ran on Claude, which is exactly what the default says.
_V5_COLUMNS = ("agent TEXT NOT NULL DEFAULT 'claude'",)

#: v5 -> v6 (2026-09-26): what a task spent, in tokens and (where the agent prices a call)
#: dollars. Nullable: nothing before this recorded it, and "unknown" is not "free".
_V6_COLUMNS = ("input_tokens INTEGER", "output_tokens INTEGER", "cost_usd REAL")


class TaskStore:
    """Task persistence. `":memory:"` is accepted for tests."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_S,
        )
        conn.row_factory = sqlite3.Row
        self._conn: sqlite3.Connection | None = conn
        if self._path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            # `timeout=` covers the connect handshake; the pragma covers every statement
            # afterwards, including one a *different* process is blocking. Both, because
            # they are not the same lock and neither is the other's fallback.
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        self._migrate()
        if self._path != ":memory:":
            # The row text is what was asked for out loud and what came back. WAL leaves
            # two sidecars next to the database holding the same content, so all three.
            for suffix in ("", "-wal", "-shm"):
                secure_file(Path(self._path + suffix))

    def _migrate(self) -> None:
        """Create the schema, or bring an older one up to `_SCHEMA_VERSION`.

        Runs synchronously in `__init__`. Each step is guarded by the version it upgrades
        *from*, so a database two versions behind walks through both.
        """
        with self._lock:
            conn = self._conn
            conn.execute(_CREATE_SCHEMA_VERSION_SQL)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute(_CREATE_TASKS_SQL)
                conn.execute(_CREATE_INDEX_SQL)
                conn.execute(_CREATE_UNREPORTED_INDEX_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                log.info("created tasks schema v%d at %s", _SCHEMA_VERSION, self._path)
                return

            version = row["version"]
            if version >= _SCHEMA_VERSION:
                return
            if version < 2:
                for column in _V2_COLUMNS:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            if version < 3:
                for column in _V3_COLUMNS:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
                conn.execute(_CREATE_UNREPORTED_INDEX_SQL)
                # Everything that had already finished before this column existed counts as
                # told: they have lived through those calls, and the alternative is a first
                # call after the upgrade that opens by reading out months of history.
                conn.execute(
                    "UPDATE tasks SET reported_at = COALESCE(finished_at, created_at) "
                    "WHERE reported_at IS NULL AND status IN (?, ?, ?)",
                    (TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value),
                )
            if version < 4:
                for column in _V4_COLUMNS:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            if version < 5:
                for column in _V5_COLUMNS:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            if version < 6:
                for column in _V6_COLUMNS:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
            log.info(
                "migrated tasks schema v%d -> v%d at %s", version, _SCHEMA_VERSION, self._path
            )

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

        # Read and write under one lock acquisition: an update rewrites *every* column, so
        # two overlapping patches that each read first would each write back their own
        # stale snapshot and the loser's fields would silently vanish (a call-back request
        # placed just as the task flips to `running`, say).
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            existing = Task.from_row(row)
            if not patch:
                return existing

            updated = dataclasses.replace(existing, **patch)
            values = updated.to_row()
            values.pop("id")
            set_clause = ", ".join(f"{name} = ?" for name in values)
            self._conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?", [*values.values(), task_id]
            )
        return updated

    async def list_unreported(self, *, limit: int = MAX_UNREPORTED) -> list[Task]:
        """Finished tasks Keryx has not told them about yet, oldest first.

        Oldest first because this is read out as "since we last spoke": the order things
        happened in is the order they make sense in. Internal (housekeeping) tasks and
        cancelled ones are excluded — they asked for neither an announcement nor, in the
        cancelled case, the work.
        """
        return await asyncio.to_thread(self._list_unreported_sync, limit)

    def _list_unreported_sync(self, limit: int) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE reported_at IS NULL AND internal = 0 "
                "AND status IN (?, ?) ORDER BY id ASC LIMIT ?",
                (TaskStatus.DONE.value, TaskStatus.FAILED.value, limit),
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    async def delete_finished_before(self, cutoff: datetime) -> list[int]:
        """Delete terminal task rows older than `cutoff`; returns the ids that went.

        **Never deletes a task the caller has not been told about.** `reported_at` is the
        only record that Keryx said a result out loud, so a `done`/`failed` row still
        waiting to be reported is kept however old it is — the alternative is a result they
        will never hear, and hearing something late beats not hearing it. `internal` rows
        (housekeeping they never asked about) and `cancelled` ones (work they stopped) are owed
        to nobody and go on schedule.

        `finished_at` can be NULL on a row cancelled before it ever ran, so the age of a
        task falls back to when it was created.
        """
        return await asyncio.to_thread(self._delete_finished_before_sync, cutoff)

    def _delete_finished_before_sync(self, cutoff: datetime) -> list[int]:
        stamp = to_utc_iso(cutoff)
        terminal = (TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?, ?) "
                "AND COALESCE(finished_at, created_at) < ? "
                "AND NOT (reported_at IS NULL AND internal = 0 AND status IN (?, ?)) "
                "ORDER BY id ASC",
                (*terminal, stamp, TaskStatus.DONE.value, TaskStatus.FAILED.value),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
        return ids

    async def count_unreported(self) -> int:
        """How many finished tasks are still waiting to be told, in total."""
        return await asyncio.to_thread(self._count_unreported_sync)

    def _count_unreported_sync(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE reported_at IS NULL AND internal = 0 "
                "AND status IN (?, ?)",
                (TaskStatus.DONE.value, TaskStatus.FAILED.value),
            ).fetchone()
        return row["n"]

    async def mark_reported(self, task_ids: Iterable[int], *, when: datetime) -> list[int]:
        """Stamp `reported_at` on each id that exists and had none. Returns the ids stamped.

        Already-reported tasks are left alone rather than re-stamped: the first time Keryx
        said it is the honest answer, and a second call must not move the timestamp.
        """
        return await asyncio.to_thread(self._mark_reported_sync, list(task_ids), when)

    def _mark_reported_sync(self, task_ids: list[int], when: datetime) -> list[int]:
        if not task_ids:
            return []
        stamp = to_utc_iso(when)
        stamped: list[int] = []
        with self._lock:
            for task_id in task_ids:
                cursor = self._conn.execute(
                    "UPDATE tasks SET reported_at = ? WHERE id = ? AND reported_at IS NULL",
                    (stamp, task_id),
                )
                if cursor.rowcount:
                    stamped.append(task_id)
        return stamped

    async def list_for_session(self, session_id: str, *, limit: int = 20) -> list[Task]:
        """The tasks one voice session dispatched, oldest first. Housekeeping excluded."""
        return await asyncio.to_thread(self._list_for_session_sync, session_id, limit)

    def _list_for_session_sync(self, session_id: str, limit: int) -> list[Task]:
        if not session_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE origin_session_id = ? AND internal = 0 "
                "ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    async def search(self, terms: Sequence[str], *, limit: int = 5) -> list[Task]:
        """Tasks whose description or summary contains every term in `terms`, newest first.

        A plain `LIKE` conjunction rather than FTS: the table is small, the query comes
        from a speech transcript, and a second virtual table would be one more thing that
        has to survive a migration.
        """
        return await asyncio.to_thread(self._search_sync, list(terms), limit)

    def _search_sync(self, terms: list[str], limit: int) -> list[Task]:
        if not terms:
            return []
        clauses = " AND ".join(
            "(description LIKE ? ESCAPE '\\' OR COALESCE(summary, '') LIKE ? ESCAPE '\\')"
            for _ in terms
        )
        params: list[object] = []
        for term in terms:
            pattern = f"%{_escape_like(term)}%"
            params += [pattern, pattern]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE internal = 0 AND {clauses} ORDER BY id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    async def usage_by_project(self, *, since: datetime | None = None) -> list[ProjectUsage]:
        """What tasks created since `since` (all of them, when None) spent, per project.

        One row per dispatched project name, one for tasks dispatched with none, and one for
        Keryx's housekeeping, whatever project it named. Most expensive first; a group
        nothing in it priced sorts after every group that was. Every status counts: a
        cancelled or failed run spent what it spent.
        """
        return await asyncio.to_thread(self._usage_by_project_sync, since)

    def _usage_by_project_sync(self, since: datetime | None) -> list[ProjectUsage]:
        clause, params = ("WHERE created_at >= ? ", [to_utc_iso(since)]) if since else ("", [])
        with self._lock:
            rows = self._conn.execute(
                "SELECT CASE WHEN internal THEN NULL ELSE project END AS grouped, "
                "internal, COUNT(*) AS tasks, COUNT(input_tokens) AS measured, "
                "COUNT(cost_usd) AS priced, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, SUM(cost_usd) AS cost_usd "
                f"FROM tasks {clause}GROUP BY grouped, internal "
                "ORDER BY cost_usd IS NULL, cost_usd DESC, tasks DESC, grouped",
                params,
            ).fetchall()
        return [
            ProjectUsage(
                project=row["grouped"],
                internal=bool(row["internal"]),
                tasks=row["tasks"],
                measured=row["measured"],
                priced=row["priced"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cost_usd=row["cost_usd"],
            )
            for row in rows
        ]

    def _list_sync(
        self, status: TaskStatus | None, limit: int, include_internal: bool
    ) -> list[Task]:
        where = [] if include_internal else ["internal = 0"]
        params: list[object] = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks {clause}ORDER BY id DESC LIMIT ?", [*params, limit]
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    # NOTE: this method is named `list` (a stable interface name), so it must be defined *after*
    # every other annotation in this class that uses the builtin `list[...]` — once
    # `list` is bound as a class attribute, later annotations evaluated in the class
    # body would resolve to this method instead of the builtin.
    async def list(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 20,
        include_internal: bool = False,
    ) -> list[Task]:
        """Tasks newest-first (`id DESC`), optionally filtered to one status.

        Housekeeping tasks are left out unless `include_internal` asks for them: the voice
        model reads this list out, and "what's running" must mean their work.
        """
        return await asyncio.to_thread(self._list_sync, status, limit, include_internal)

    async def count_created_since(self, since: datetime) -> int:
        """Number of tasks with `created_at >= since` (UTC ISO-8601 string comparison).

        Internal tasks do not count: the daily cap is there to bound what they can spend on
        a runaway conversation, and Keryx's own memory upkeep is not that.
        """
        return await asyncio.to_thread(self._count_created_since_sync, since)

    def _count_created_since_sync(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ? AND internal = 0",
                (to_utc_iso(since),),
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
