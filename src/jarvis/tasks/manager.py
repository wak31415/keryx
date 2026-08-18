"""Task lifecycle: queue, run, follow up, cancel (spec §3.2 `tasks/manager.py`).

`dispatch()` writes a `queued` row and hands the work to an `asyncio.Task` running
`_run`, which waits on a `max_concurrent_tasks` semaphore before it opens a subagent
session. While it waits the row stays `queued`, so `cancel()` on a queued task simply
cancels the asyncio task before any agent is opened.

State the manager keeps per task id: the asyncio task (`_tasks`), the open
`AgentSession` (`_live`, so follow-ups and cancels can reach it), and an
`asyncio.Event` (`_done_events`) that `wait_for` blocks on. The event is created at
dispatch and stays set after a terminal state, so a late waiter returns immediately.

Progress lines are appended to `data_dir/tasks/<id>.log` and republished as
`TaskProgress`; the agent's final text is written to `data_dir/tasks/<id>.md`, which is
the report the notifier links to. Cancellation deliberately publishes nothing: the user
asked for it, so there is nothing to announce.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from jarvis.config import Settings
from jarvis.events import EventBus, TaskCompleted, TaskFailed, TaskProgress, TaskStarted
from jarvis.tasks.agent_runner import AgentRunner, AgentSession, RunResult, resolve_model
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

log = logging.getLogger("jarvis.tasks.manager")

TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})

#: Bound on the best-effort `interrupt()`/`close()` calls, so a wedged subagent process
#: can never block a cancel or a shutdown.
SESSION_TIMEOUT_S = 5.0
#: Bound on waiting for a cancelled asyncio task to unwind.
CANCEL_TIMEOUT_S = 5.0
#: How many project names an `UnknownProjectError` message spells out.
MAX_LISTED_CANDIDATES = 10

UNKNOWN_ERROR = "unknown error"
FAILURE_SUMMARY = "The task failed: {error}"

_PROMPTS: dict[TaskKind, str] = {
    TaskKind.CHAT: (
        "You are answering a question for the user via a voice assistant. "
        "Answer thoroughly but concisely.\n\n"
        "Question/request:\n{description}"
    ),
    TaskKind.RESEARCH: (
        "Research the following on the web and produce a well-organized report with "
        "sources. Save the report as REPORT.md in the current directory as well.\n\n"
        "Topic:\n{description}"
    ),
    TaskKind.CODING: (
        "You are working in the repository at {cwd} (project '{project}'). "
        "Complete the following task end to end: make the changes, run the relevant "
        "tests/linters if any, and commit with a clear message if the repository is a "
        "git repo. Do not push.\n\n"
        "Task:\n{description}"
    ),
    TaskKind.COWORK: (
        "You have access to the user's Gmail and Google Calendar through the google MCP "
        "tools. Complete the following request. Never send an email or modify calendar "
        "events unless the request explicitly asks for it; otherwise draft/summarize and "
        "report.\n\n"
        "Request:\n{description}"
    ),
}

_NORMALIZE_RE = re.compile(r"[\s_\-]+")


class TaskLimitError(RuntimeError):
    """Raised by `dispatch()` when `daily_task_cap` is already reached."""

    def __init__(self, cap: int, count: int) -> None:
        super().__init__(f"daily task cap reached ({count}/{cap} tasks today)")
        self.cap = cap
        self.count = count


class UnknownProjectError(ValueError):
    """Raised when a spoken project name matches no project, or more than one."""

    def __init__(self, name: str, candidates: list[str] | None = None) -> None:
        self.name = name
        self.candidates = list(candidates or ())
        listed = ", ".join(self.candidates[:MAX_LISTED_CANDIDATES])
        message = f"unknown project {name!r}"
        if listed:
            message += f"; known projects: {listed}"
        super().__init__(message)


def build_prompt(task: Task) -> str:
    """The subagent prompt for `task`: a kind-specific preamble plus its description.

    Only the template is formatted, so braces inside the description are left alone.
    """
    return _PROMPTS[task.kind].format(
        description=task.description,
        cwd=task.cwd or "the current directory",
        project=task.project or "none",
    )


def _normalize(name: str) -> str:
    """A project name with case, spaces, dashes and underscores flattened away."""
    return _NORMALIZE_RE.sub("", name.lower())


class TaskManager:
    """Owns every dispatched task: queueing, lifecycle, follow-ups, cancel and events."""

    def __init__(
        self, store: TaskStore, runner: AgentRunner, bus: EventBus, settings: Settings
    ) -> None:
        self._store = store
        self._runner = runner
        self._bus = bus
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._tasks: dict[int, asyncio.Task] = {}
        self._live: dict[int, AgentSession] = {}
        self._done_events: dict[int, asyncio.Event] = {}
        self._cancelled_before_start: set[int] = set()
        settings.ensure_dirs()

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Nothing to warm up; the data directories are the only prerequisite."""
        self._settings.ensure_dirs()

    async def shutdown(self) -> None:
        """Cancel every queued/running task, wait for them, then close any open session."""
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        for task_id in list(self._live):
            await self._close_session(task_id)

    # --- dispatch --------------------------------------------------------

    async def dispatch(
        self,
        kind: TaskKind | str,
        description: str,
        *,
        project: str | None = None,
        model: str | None = None,
        origin_channel: str,
        origin_caller: str | None,
    ) -> Task:
        """Create a `queued` task and schedule it.

        Raises `ValueError` for an unknown kind or a `coding` task with no project,
        `TaskLimitError` past the daily cap and `UnknownProjectError` for a project
        name that matches nothing (or more than one thing).
        """
        task_kind = TaskKind(kind)
        await self._check_daily_cap()

        project_name: str | None = None
        cwd: str | None = None
        if project:
            project_name, path = self.resolve_project(project)
            cwd = str(path)
        elif task_kind is TaskKind.CODING:
            raise ValueError("coding tasks need a project")

        created = await self._store.create(
            Task(
                id=None,
                kind=task_kind,
                description=description,
                status=TaskStatus.QUEUED,
                project=project_name,
                cwd=cwd,
                model=resolve_model(model, self._settings),
                origin_channel=origin_channel,
                origin_caller=origin_caller,
            )
        )
        self._done_events[created.id] = asyncio.Event()
        self._spawn(created.id)
        log.info(
            "task %s dispatched (%s, project=%s, model=%s, from=%s)",
            created.id,
            created.kind,
            created.project,
            created.model,
            created.origin_channel,
        )
        return created

    async def _check_daily_cap(self) -> None:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        count = await self._store.count_created_since(midnight)
        if count >= self._settings.daily_task_cap:
            raise TaskLimitError(self._settings.daily_task_cap, count)

    def _spawn(self, task_id: int, *, prompt: str | None = None, resume: str | None = None) -> None:
        """Start the asyncio task that will run (or re-run) `task_id`."""
        self._tasks[task_id] = asyncio.create_task(
            self._run(task_id, prompt=prompt, resume=resume), name=f"jarvis-task-{task_id}"
        )

    # --- running ---------------------------------------------------------

    async def _run(self, task_id: int, *, prompt: str | None, resume: str | None) -> None:
        """One task attempt, from the back of the queue to a terminal row."""
        try:
            async with self._semaphore:
                await self._execute(task_id, prompt=prompt, resume=resume)
        except asyncio.CancelledError:
            await self._mark_cancelled(task_id)
            raise
        except Exception as exc:  # any failure becomes a `failed` row, never a lost task
            log.exception("task %s blew up", task_id)
            await self._fail(task_id, f"{type(exc).__name__}: {exc}")
        finally:
            await self._close_session(task_id)
            self._cancelled_before_start.discard(task_id)
            self._done_event(task_id).set()
            if self._tasks.get(task_id) is asyncio.current_task():
                del self._tasks[task_id]

    async def _execute(self, task_id: int, *, prompt: str | None, resume: str | None) -> None:
        """Open a subagent for `task_id`, run one turn and record the outcome."""
        if task_id in self._cancelled_before_start:
            log.info("task %s was cancelled before it started", task_id)
            return
        task = await self._store.get(task_id)
        if task is None:  # pragma: no cover - only if the row is deleted mid-flight
            log.warning("task %s disappeared before it could run", task_id)
            return

        # The session is registered before the row flips to `running`, so anything that
        # sees a running task can rely on finding its live session.
        session = await self._runner.open(task, resume=resume)
        self._live[task_id] = session
        task = await self._store.update(
            task_id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(UTC),
            finished_at=None,
            error=None,
        )
        await self._bus.publish(TaskStarted(task_id))

        result = await session.run(
            prompt if prompt is not None else build_prompt(task),
            on_progress=partial(self._on_progress, task_id),
        )
        await self._finish(task, result)

    async def _finish(self, task: Task, result: RunResult) -> None:
        """Write the report, close the row out as `done`/`failed` and publish the event."""
        fields: dict[str, object] = {
            "summary": result.spoken_summary,
            "report_path": str(self._write_report(task, result)),
            "finished_at": datetime.now(UTC),
        }
        if result.session_id:
            fields["claude_session_id"] = result.session_id
        if result.ok:
            await self._store.update(task.id, status=TaskStatus.DONE, error=None, **fields)
            log.info("task %s done", task.id)
            await self._bus.publish(TaskCompleted(task.id, result.spoken_summary))
        else:
            error = result.error or UNKNOWN_ERROR
            await self._store.update(task.id, status=TaskStatus.FAILED, error=error, **fields)
            log.warning("task %s failed: %s", task.id, error)
            await self._bus.publish(TaskFailed(task.id, error))

    async def _fail(self, task_id: int, error: str) -> None:
        """Mark a task failed after an exception escaped the run."""
        try:
            await self._store.update(
                task_id,
                status=TaskStatus.FAILED,
                error=error,
                summary=FAILURE_SUMMARY.format(error=error),
                finished_at=datetime.now(UTC),
            )
        except Exception:
            log.exception("could not record the failure of task %s", task_id)
        await self._bus.publish(TaskFailed(task_id, error))

    async def _mark_cancelled(self, task_id: int) -> Task | None:
        """Close the row out as `cancelled`. Publishes nothing: the user asked for it.

        A task cancelled in the instant after it finished keeps its terminal state, so a
        row can never say `cancelled` while a `TaskCompleted` event is already out.
        """
        try:
            existing = await self._store.get(task_id)
            if existing is None or existing.status in TERMINAL_STATUSES:
                return existing
            task = await self._store.update(
                task_id, status=TaskStatus.CANCELLED, finished_at=datetime.now(UTC)
            )
        except Exception:
            log.exception("could not record the cancellation of task %s", task_id)
            return None
        log.info("task %s cancelled", task_id)
        return task

    async def _close_session(self, task_id: int) -> None:
        """Close and forget the live session for `task_id`, if there is one."""
        session = self._live.pop(task_id, None)
        if session is None:
            return
        try:
            await asyncio.wait_for(session.close(), SESSION_TIMEOUT_S)
        except Exception:
            log.exception("closing the subagent session of task %s failed", task_id)

    # --- progress, logs and reports --------------------------------------

    def _log_path(self, task_id: int) -> Path:
        return self._settings.data_dir / "tasks" / f"{task_id}.log"

    def _report_path(self, task_id: int) -> Path:
        return self._settings.data_dir / "tasks" / f"{task_id}.md"

    async def _on_progress(self, task_id: int, text: str) -> None:
        self._append_log(task_id, text)
        await self._bus.publish(TaskProgress(task_id, text))

    def _append_log(self, task_id: int, text: str) -> None:
        """Append one timestamped line to the task's log (best effort, never fatal)."""
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            with self._log_path(task_id).open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
        except OSError:
            log.exception("could not append to the log of task %s", task_id)

    def _write_report(self, task: Task, result: RunResult) -> Path:
        """Write `data_dir/tasks/<id>.md`; always created, even for an empty result."""
        path = self._report_path(task.id)
        header = f"# Task {task.id} — {task.kind}\n\n{task.description}\n\n---\n\n"
        try:
            path.write_text(header + (result.final_text or ""), encoding="utf-8")
        except OSError:
            log.exception("could not write the report of task %s", task.id)
        return path

    # --- queries ---------------------------------------------------------

    async def get(self, task_id: int) -> Task | None:
        return await self._store.get(task_id)

    def _done_event(self, task_id: int) -> asyncio.Event:
        """The completion event for `task_id`, created on first use."""
        event = self._done_events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._done_events[task_id] = event
        return event

    async def wait_for(self, task_id: int, timeout: float) -> Task:
        """The task as soon as it reaches a terminal state, or as it is after `timeout`."""
        task = await self._store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        try:
            await asyncio.wait_for(self._done_event(task_id).wait(), timeout)
        except TimeoutError:
            log.info("waiting for task %s timed out after %.1fs", task_id, timeout)
        return await self._store.get(task_id)

    # --- follow-ups and cancel -------------------------------------------

    async def followup(self, task_id: int, text: str) -> Task:
        """Add `text` to a task: into the live turn, as a resumed run, or to its description."""
        task = await self._store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is TaskStatus.CANCELLED:
            raise ValueError("task is cancelled")

        if task.status is TaskStatus.QUEUED:
            updated = await self._store.update(
                task_id, description=f"{task.description}\n\nAdditionally: {text}"
            )
        elif task.status is TaskStatus.RUNNING:
            session = self._live.get(task_id)
            if session is None:
                raise ValueError("task is starting up; try the follow-up again in a moment")
            await session.send(text)
            updated = task
        else:
            updated = await self._restart(task, text)

        self._append_log(task_id, f"[followup] {text}")
        return updated

    async def _restart(self, task: Task, text: str) -> Task:
        """Re-run a finished task, resuming its Claude session when there is one."""
        resume = task.claude_session_id
        if resume:
            prompt = text
        else:
            log.warning("task %s has no Claude session id; starting a fresh run", task.id)
            prompt = f"{build_prompt(task)}\n\nFollow-up: {text}"
        updated = await self._store.update(
            task.id, status=TaskStatus.RUNNING, started_at=datetime.now(UTC), finished_at=None
        )
        self._done_event(task.id).clear()
        self._spawn(task.id, prompt=prompt, resume=resume)
        return updated

    async def cancel(self, task_id: int) -> Task:
        """Stop a queued or running task. Terminal tasks come back unchanged."""
        task = await self._store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in TERMINAL_STATUSES:
            return task

        self._cancelled_before_start.add(task_id)
        session = self._live.get(task_id)
        if session is not None:
            try:
                await asyncio.wait_for(session.interrupt(), SESSION_TIMEOUT_S)
            except Exception:
                log.exception("interrupting the subagent of task %s failed", task_id)

        runner_task = self._tasks.get(task_id)
        if runner_task is not None:
            runner_task.cancel()
            await asyncio.wait({runner_task}, timeout=CANCEL_TIMEOUT_S)

        current = await self._store.get(task_id)
        if current is not None and current.status not in TERMINAL_STATUSES:
            # The asyncio task never got to run its cancellation handler.
            current = await self._mark_cancelled(task_id) or current
        self._cancelled_before_start.discard(task_id)
        self._done_event(task_id).set()
        return current

    # --- projects --------------------------------------------------------

    def _candidates(self) -> dict[str, Path]:
        """Every known project: configured ones first, then `projects_root` subdirectories.

        Names are unique, and so are paths — a configured project that points at a
        `projects_root` subdirectory is listed once, under its configured name.
        """
        candidates: dict[str, Path] = {}
        seen: set[Path] = set()

        def add(name: str, path: Path) -> None:
            key = self._resolved(path)
            if name in candidates or key in seen:
                return
            candidates[name] = path
            seen.add(key)

        for name, raw in self._settings.projects.items():
            add(name, Path(raw).expanduser())

        root = self._settings.projects_root
        try:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            log.exception("could not list the projects root %s", root)
            entries = []
        for entry in entries:
            if not entry.name.startswith(".") and entry.is_dir():
                add(entry.name, entry)
        return candidates

    @staticmethod
    def _resolved(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:  # pragma: no cover - resolve() rarely fails on a plain path
            return path

    def resolve_project(self, name: str) -> tuple[str, Path]:
        """A spoken project name as `(canonical name, path)`.

        Exact name first, then a case/space/dash/underscore-insensitive match, then a
        unique substring match. Anything ambiguous or unmatched raises.
        """
        candidates = self._candidates()
        spoken = (name or "").strip()
        if spoken in candidates:
            return spoken, candidates[spoken]

        normalized = _normalize(spoken)
        if not normalized:
            raise UnknownProjectError(name, sorted(candidates))

        matches = [known for known in candidates if _normalize(known) == normalized]
        if not matches:
            matches = [known for known in candidates if normalized in _normalize(known)]
        if len(matches) == 1:
            return matches[0], candidates[matches[0]]
        raise UnknownProjectError(name, matches or sorted(candidates))

    def list_projects(self) -> list[tuple[str, Path]]:
        """Every known project as `(name, path)`, configured ones first."""
        return list(self._candidates().items())

    # NOTE: this method is named `list` per spec §3.2, so — exactly as in `TaskStore` —
    # it must be defined after every annotation in this class body that uses the builtin
    # `list[...]`, which would otherwise resolve to this method.
    async def list(self, *, status: TaskStatus | None = None, limit: int = 20) -> list[Task]:
        """Tasks newest-first, optionally filtered to one status."""
        return await self._store.list(status=status, limit=limit)
