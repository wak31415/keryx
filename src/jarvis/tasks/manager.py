"""Task lifecycle: queue, run, follow up, cancel (spec §3.2 `tasks/manager.py`).

`dispatch()` writes a `queued` row and hands the work to an `asyncio.Task` running
`_run`, which waits on a `max_concurrent_tasks` semaphore before it opens a subagent
session. While it waits the row stays `queued`, so `cancel()` on a queued task simply
cancels the asyncio task before any agent is opened.

State the manager keeps per task id: the asyncio task (`_tasks`), the open
`AgentSession` (`_live`, so cancels can reach it), an `asyncio.Event` (`_done_events`)
that `wait_for` blocks on, a cancel flag (`_cancel_requested`), the prompt a queued
re-run will send (`_resume_pending`) and the follow-ups that arrived mid-turn
(`_live_followups`). The event is created at dispatch and stays set after a terminal
state, so a late waiter returns immediately.

A follow-up to a *running* task is never pushed into the open turn: `run()` has already
stopped at the first result and the SDK's mid-turn `query()` semantics are unverified
(spec §3.3 ruling), so the text is queued and becomes the prompt of an immediate resumed
run inside the same semaphore slot. Only the last run of that chain is published, so one
request stays one announcement.

`cancel()` cannot rely on cancelling the asyncio task alone: interrupting a live session
is itself what ends the agent's turn, so `run()` typically returns a (useless) result
first. Hence the flag — `_execute` checks it after `run()` returns and takes the cancel
path instead of writing a terminal row or publishing an event.

Progress lines are appended to `data_dir/tasks/<id>.log` and republished as
`TaskProgress`; the agent's final text is written to `data_dir/tasks/<id>.md`, which is
the report the notifier links to. Cancellation deliberately publishes nothing: the user
asked for it, so there is nothing to announce.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from jarvis.agents.base import AgentRunner, AgentSession, RunResult
from jarvis.agents.registry import resolve_model
from jarvis.config import Settings, secure_file
from jarvis.events import EventBus, TaskCompleted, TaskFailed, TaskProgress, TaskStarted
from jarvis.projects import discover_projects
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import MAX_UNREPORTED, TaskStore

log = logging.getLogger("jarvis.tasks.manager")

TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})

#: Bound on the best-effort `interrupt()` call, so a wedged subagent process can never
#: block a cancel.
SESSION_TIMEOUT_S = 5.0
#: Bound on `close()` alone. Longer than the SDK's own bounded close (up to 5 s for the
#: write lock, then 5 s each for a clean exit, SIGTERM and SIGKILL), so a CLI still
#: mid-turn gets terminated rather than abandoned.
CLOSE_TIMEOUT_S = 25.0
#: Bound on waiting for a cancelled asyncio task to unwind.
CANCEL_TIMEOUT_S = 5.0
#: How much "where we left off" a call-back carries; it is spoken from, not read out.
MAX_CALLBACK_NOTE_CHARS = 300
#: How many project names an `UnknownProjectError` message spells out.
MAX_LISTED_CANDIDATES = 10
#: How many abandoned `queued` rows one startup will pick back up. A bound, not a policy:
#: more than this waiting means something is wrong that resuming will not fix.
MAX_RESUMED = 20

UNKNOWN_ERROR = "unknown error"
#: What is said about a run the wall-clock cap stopped.
TIMEOUT_SUMMARY = "The task ran for {duration} without finishing, so I stopped it."
FAILURE_SUMMARY = "The task failed: {error}"
#: Heads the prompt of a re-run carrying the follow-ups that arrived mid-turn.
LIVE_FOLLOWUP_PREAMBLE = "Follow-up from the user:"

#: One prompt for one kind of task. What the work needs — a repo, the web, the mailbox, a
#: skill, a fleet of subagents — is the subagent's call, not something the voice model
#: classified in advance.
_PROMPT = (
    "Complete this request end to end. You are working on the user's own machine, with "
    "their repositories, their Gmail and Calendar (through the google MCP tools), the skills "
    "installed for you, and subagents of your own. Use whatever the work "
    "actually needs.\n\n"
    "Working directory: {cwd}{project_clause}\n\n"
    "Where the request touches a repository, finish the job properly: make the change, "
    "run the tests and linters that exist, and commit with a clear message if it is a git "
    "repo — but never push. Never send mail or change a calendar entry unless the request "
    "asks for it; otherwise draft it and say so in the report.\n\n"
    "Request:\n{description}"
)

_NORMALIZE_RE = re.compile(r"[\s_\-]+")


class TaskLimitError(RuntimeError):
    """Raised by `dispatch()` when `daily_task_cap` is already reached."""

    def __init__(self, cap: int, count: int) -> None:
        super().__init__(f"daily task cap reached ({count}/{cap} tasks today)")
        self.cap = cap
        self.count = count


class AgentUnavailableError(ValueError):
    """Raised by `dispatch()` for an agent this process has not enabled."""

    def __init__(self, agent: str, enabled: Sequence[str]) -> None:
        self.agent = agent
        self.enabled = list(enabled)
        super().__init__(f"the {agent} agent is not enabled; enabled: {', '.join(enabled)}")


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
    """The subagent prompt for `task`: the standing preamble plus its description.

    Only the template is formatted, so braces inside the description are left alone.
    """
    return _PROMPT.format(
        description=task.description,
        cwd=task.cwd or "the current directory",
        project_clause=f" (project '{task.project}')" if task.project else "",
    )


def executor_workers(settings: Settings) -> int:
    """How many threads the event loop's default executor needs under this manager.

    A running Codex turn holds two of them for as long as it runs — the SDK reads its
    stream, and its turn-less warnings, one blocking `asyncio.to_thread` at a time — and
    steering, interrupting and closing it need more, as does every SQLite call. Python's
    default (`min(32, cpus + 4)`) can be fewer than a full slate of tasks holds, which would
    leave a cancel waiting on a thread that only the turn it is cancelling can free.
    """
    return max(32, 4 * settings.max_concurrent_tasks + 16)


def install_default_executor(settings: Settings) -> ThreadPoolExecutor:
    """Give the running loop a default executor sized by `executor_workers`."""
    executor = ThreadPoolExecutor(executor_workers(settings), thread_name_prefix="jarvis")
    asyncio.get_running_loop().set_default_executor(executor)
    return executor


def _duration(seconds: float) -> str:
    """`seconds` as a speakable length: "3 hours", "90 minutes", "45 seconds"."""
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds >= 60:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:.0f} seconds"


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
        self._cancel_requested: set[int] = set()
        self._resume_pending: dict[int, str] = {}
        self._live_followups: dict[int, list[str]] = {}
        settings.ensure_dirs()

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Nothing to warm up; the data directories are the only prerequisite."""
        self._settings.ensure_dirs()

    async def resume_queued(self) -> list[int]:
        """Start the tasks a previous process was killed before it could ever run.

        `queued` is not a waiting room. A task is *born* queued and flips to `running`
        about a second later, when the coroutine `dispatch` created gets its first slice
        and takes the semaphore; under the concurrency cap nothing normally waits there at
        all. So a row still `queued` in a fresh process is one that never executed a single
        instruction — the process was killed inside that second — and starting it here is
        running it for the first time, not running it twice. Nothing is half-done, because
        nothing was done.

        A `running` row is the opposite and is deliberately left alone: that subagent had
        opened, and what it got through before the machine went down is unknowable from
        here. `RestartCoordinator.status_summary` reports those as lost, which they are.
        """
        try:
            waiting = await self._store.list(status=TaskStatus.QUEUED, limit=MAX_RESUMED)
        except Exception:
            log.exception("could not look for tasks to pick back up")
            return []
        # Oldest first: the owner asked for them in that order, so they run in it.
        found = sorted(
            (task for task in waiting if task.id is not None and task.id not in self._tasks),
            key=lambda task: task.created_at,
        )
        for task in found:
            log.info("picking task %s back up, queued since %s", task.id, task.created_at)
            self._done_events.setdefault(task.id, asyncio.Event())
            self._spawn(task.id)
        return [task.id for task in found]

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
        description: str,
        *,
        project: str | None = None,
        model: str | None = None,
        origin_channel: str,
        origin_caller: str | None,
        origin_session_id: str | None = None,
        cwd: str | None = None,
        internal: bool = False,
        agent: str | None = None,
    ) -> Task:
        """Create a `queued` task and schedule it.

        Raises `TaskLimitError` past the daily cap and `UnknownProjectError` for a project
        name that matches nothing (or more than one thing). With no project the task
        starts in `projects_root`, and the subagent finds its way from there; an explicit
        `cwd` overrides both, for work that is not in a project at all.

        A `projects_root` that is not a directory is not a place to start: the task gets no
        `cwd` at all, and the runner starts it in its own workspace under `data_dir`.

        `internal` marks work Jarvis asked for itself (the per-call memory update): it is
        exempt from the daily cap, hidden from the spoken task lists, and never announced.
        It restricts nothing about the subagent — see `Task.internal`.

        `agent` is the coding agent to run it on; None is `AGENT_BACKEND`. It is fixed for
        the life of the task, and one that is not enabled raises `AgentUnavailableError`.
        """
        agent = agent or self._settings.agent_backend
        if agent not in self._settings.enabled_agents:
            raise AgentUnavailableError(agent, self._settings.enabled_agents)
        if not internal:
            await self._check_daily_cap()

        project_name: str | None = None
        if project:
            project_name, path = self.resolve_project(project)
            cwd = cwd or str(path)
        elif cwd is None and self._settings.projects_root.is_dir():
            # Work is handed over the moment it is recognised, so the project is often
            # still unsaid. Start in the projects root and let the subagent find its way —
            # asking first only pushes the question back onto the voice.
            cwd = str(self._settings.projects_root)

        created = await self._store.create(
            Task(
                id=None,
                kind=TaskKind.AGENT,
                description=description,
                status=TaskStatus.QUEUED,
                project=project_name,
                cwd=cwd,
                model=resolve_model(agent, model, self._settings),
                agent=agent,
                origin_channel=origin_channel,
                origin_caller=origin_caller,
                origin_session_id=origin_session_id,
                internal=internal,
            )
        )
        self._done_events[created.id] = asyncio.Event()
        self._spawn(created.id)
        log.info(
            "task %s dispatched (project=%s, agent=%s, model=%s, from=%s%s)",
            created.id,
            created.project,
            created.agent,
            created.model,
            created.origin_channel,
            ", internal" if internal else "",
        )
        return created

    async def _check_daily_cap(self) -> None:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        count = await self._store.count_created_since(midnight)
        if count >= self._settings.daily_task_cap:
            raise TaskLimitError(self._settings.daily_task_cap, count)

    def _spawn(self, task_id: int, *, resume: str | None = None) -> None:
        """Start the asyncio task that will run (or re-run) `task_id`."""
        self._tasks[task_id] = asyncio.create_task(
            self._run(task_id, resume=resume), name=f"jarvis-task-{task_id}"
        )

    # --- running ---------------------------------------------------------

    async def _run(self, task_id: int, *, resume: str | None) -> None:
        """One task attempt, from the back of the queue to a terminal row."""
        try:
            async with self._semaphore:
                await self._execute(task_id, resume=resume)
        except asyncio.CancelledError:
            await self._mark_cancelled(task_id)
            raise
        except Exception as exc:  # any failure becomes a `failed` row, never a lost task
            log.exception("task %s blew up", task_id)
            await self._fail(task_id, f"{type(exc).__name__}: {exc}")
        finally:
            await self._close_session(task_id)
            self._release(task_id)
            if self._tasks.get(task_id) is asyncio.current_task():
                del self._tasks[task_id]

    def _release(self, task_id: int) -> None:
        """Drop the per-task bookkeeping and wake anything blocked in `wait_for`."""
        self._cancel_requested.discard(task_id)
        self._resume_pending.pop(task_id, None)
        self._live_followups.pop(task_id, None)
        self._done_event(task_id).set()

    async def _execute(self, task_id: int, *, resume: str | None) -> None:
        """Open a subagent for `task_id`, run one turn and record the outcome."""
        if task_id in self._cancel_requested:
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

        # A pending follow-up prompt (a resumed run) wins over the task's own prompt; it is
        # read here, not at spawn time, so follow-ups arriving while the run was still
        # queued are all included.
        prompt = self._resume_pending.pop(task_id, None) or build_prompt(task)
        result = await self._run_turn(task_id, session, prompt)
        result = await self._run_live_followups(task, result)

        if task_id in self._cancel_requested:
            # `interrupt()` ended the turn: the result is the wreckage of a cancel, not an
            # outcome. Record the cancel and publish nothing, exactly as if the asyncio
            # task's own cancellation had landed first.
            log.info("task %s was cancelled while running; discarding the result", task_id)
            await self._mark_cancelled(task_id)
            return
        await self._finish(task, result)

    async def _run_live_followups(self, task: Task, result: RunResult) -> RunResult:
        """Re-run `task` for the follow-ups that arrived while it was running (spec §3.3).

        Each round resumes the session the last run left behind — on the task's own agent,
        which is the only one that can — so the agent keeps its context, and stays inside
        the semaphore slot the task already holds. Only the result of the final round is
        returned — the ones in between are steps in the same
        piece of work, not outcomes to announce. A run that came back without a session id
        cannot be resumed, so its follow-ups go to a fresh run of the whole task instead.
        """
        while task.id not in self._cancel_requested:
            followups = self._live_followups.pop(task.id, None)
            if not followups:
                break
            prompt = LIVE_FOLLOWUP_PREAMBLE + "".join(f"\n- {text}" for text in followups)
            resume = result.session_id
            if not resume:
                log.warning("task %s has no session id; re-running from the top", task.id)
                prompt = f"{build_prompt(task)}\n\n{prompt}"
            log.info("task %s re-runs with %d follow-up(s)", task.id, len(followups))
            await self._close_session(task.id)
            session = await self._runner.open(task, resume=resume)
            self._live[task.id] = session
            result = await self._run_turn(task.id, session, prompt)
        return result

    async def _run_turn(self, task_id: int, session: AgentSession, prompt: str) -> RunResult:
        """One `session.run`, cut off at `SUBAGENT_TIMEOUT_S` whichever agent it is on.

        The cap lives here rather than in a runner so that it is one rule for every agent:
        Claude also has a turn and a dollar cap, but Codex has neither, and a run nobody
        bounds is a run that can hold a semaphore slot for ever.

        Cutting off `run()` only stops *reading* the agent: its turn goes on, changing files,
        in a process nobody is listening to. So a timed-out session is interrupted and closed
        here, before anything records or announces that it was stopped.
        """
        run = session.run(prompt, on_progress=partial(self._on_progress, task_id))
        limit = self._settings.subagent_timeout_s
        if not limit:
            return await run
        try:
            return await asyncio.wait_for(run, limit)
        except TimeoutError:
            log.warning("task %s ran past its %.0fs limit; stopping it", task_id, limit)
            await self._interrupt(task_id, session)
            await self._close_session(task_id)
            return RunResult(
                ok=False,
                spoken_summary=TIMEOUT_SUMMARY.format(duration=_duration(limit)),
                error=f"timed out after {limit:.0f}s (SUBAGENT_TIMEOUT_S)",
            )

    async def _finish(self, task: Task, result: RunResult) -> None:
        """Write the report, close the row out as `done`/`failed` and publish the event."""
        fields: dict[str, object] = {
            "summary": result.spoken_summary,
            "report_path": str(self._write_report(task, result)),
            "finished_at": datetime.now(UTC),
        }
        if result.session_id:
            fields["claude_session_id"] = result.session_id
        if result.ok and result.restart_reason is not None and not task.internal:
            # The subagent says it changed Jarvis's own code. Recorded, not acted on: the
            # Notifier decides when a restart is safe, because it is the thing that knows
            # whether they are mid-call. An internal task never asks — nothing Jarvis
            # dispatches to itself has any business taking Jarvis off the air.
            log.info("task %s asks for a restart: %s", task.id, result.restart_reason or "no why")
            fields["needs_restart"] = True
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

    async def _interrupt(self, task_id: int, session: AgentSession) -> None:
        """Ask `session` to stop its turn, for at most `SESSION_TIMEOUT_S`."""
        try:
            await asyncio.wait_for(session.interrupt(), SESSION_TIMEOUT_S)
        except Exception:
            log.exception("interrupting the subagent of task %s failed", task_id)

    async def _close_session(self, task_id: int) -> None:
        """Close and forget the live session for `task_id`, if there is one."""
        session = self._live.pop(task_id, None)
        if session is None:
            return
        try:
            await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_S)
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
        path = self._log_path(task_id)
        try:
            existed = path.exists()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
            if not existed:
                secure_file(path)
        except OSError:
            log.exception("could not append to the log of task %s", task_id)

    def _write_report(self, task: Task, result: RunResult) -> Path:
        """Write `data_dir/tasks/<id>.md`; always created, even for an empty result."""
        path = self._report_path(task.id)
        header = f"# Task {task.id}\n\n{task.description}\n\n---\n\n"
        try:
            path.write_text(header + (result.final_text or ""), encoding="utf-8")
            secure_file(path)
        except OSError:
            log.exception("could not write the report of task %s", task.id)
        return path

    # --- queries ---------------------------------------------------------

    async def get(self, task_id: int) -> Task | None:
        return await self._store.get(task_id)

    async def unreported(self, *, limit: int = MAX_UNREPORTED) -> list[Task]:
        """Finished tasks Jarvis still owes them a word about, oldest first (spec §3.3)."""
        return await self._store.list_unreported(limit=limit)

    async def count_unreported(self) -> int:
        """How many finished tasks are still waiting to be told, in total."""
        return await self._store.count_unreported()

    async def mark_reported(self, task_ids: Iterable[int]) -> list[int]:
        """Record that Jarvis has now told them about these tasks. Returns the ids stamped.

        Ids that do not exist, or that were already stamped, are simply not returned — the
        voice model is guessing at ids from a spoken conversation, and a wrong one must be
        a shrug rather than an error it has to explain out loud.
        """
        stamped = await self._store.mark_reported(task_ids, when=datetime.now(UTC))
        if stamped:
            log.info("reported tasks %s to the user", ", ".join(str(one) for one in stamped))
        return stamped

    async def search(self, terms: Sequence[str], *, limit: int = 5) -> list[Task]:
        """Tasks whose description or summary contains every term, newest first."""
        return await self._store.search(terms, limit=limit)

    async def tasks_for_session(self, session_id: str) -> list[Task]:
        """The tasks one voice session dispatched, oldest first."""
        return await self._store.list_for_session(session_id)

    def _done_event(self, task_id: int) -> asyncio.Event:
        """The completion event for `task_id`, created on first use."""
        event = self._done_events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._done_events[task_id] = event
        return event

    async def request_callback(self, task_id: int, number: str, note: str | None = None) -> Task:
        """Ask for an outbound call to `number` when `task_id` finishes.

        `note` is what the call-back should remind them of — the call it was arranged on is
        long over by then. Only records the wish; the notifier places the call (spec §3.3).
        Raises `KeyError` if the task does not exist.
        """
        fields: dict[str, object] = {"callback_requested": True, "callback_number": number}
        if note:
            fields["callback_note"] = note[:MAX_CALLBACK_NOTE_CHARS]
        task = await self._store.update(task_id, **fields)
        log.info("task %s will be called back when it finishes", task_id)
        return task

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
        """Add `text` to a task: queued for its next run, or folded into its description."""
        task = await self._store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is TaskStatus.CANCELLED:
            raise ValueError("task is cancelled")

        if task.status is TaskStatus.QUEUED:
            updated = await self._queue_followup(task, text)
        elif task.status is TaskStatus.RUNNING:
            if task_id not in self._live:
                raise ValueError("task is starting up; try the follow-up again in a moment")
            self._live_followups.setdefault(task_id, []).append(text)
            log.info("[followup queued] task %s re-runs when this turn ends", task_id)
            updated = task
        else:
            updated = await self._restart(task, text)

        self._append_log(task_id, f"[followup] {text}")
        return updated

    async def _queue_followup(self, task: Task, text: str) -> Task:
        """Fold `text` into a queued task: into a pending resume prompt, else its description.

        A task queued for a resumed run has already been described to the agent, so more
        follow-up text belongs in the prompt that run will send, not in the description.
        """
        pending = self._resume_pending.get(task.id)
        if pending is not None:
            self._resume_pending[task.id] = f"{pending}\n\nAdditionally: {text}"
            return task
        return await self._store.update(
            task.id, description=f"{task.description}\n\nAdditionally: {text}"
        )

    async def _restart(self, task: Task, text: str) -> Task:
        """Queue a finished task for a re-run, resuming its agent's session if it has one.

        The row goes back to `queued`, not `running`: the re-run still has to wait for the
        semaphore, and a row may only claim to be running once a subagent is actually on it.
        `summary` is left in place until the new run overwrites it.
        """
        resume = task.claude_session_id
        if resume:
            prompt = text
        else:
            log.warning("task %s has no session id; starting a fresh run", task.id)
            prompt = f"{build_prompt(task)}\n\nFollow-up: {text}"
        updated = await self._store.update(
            task.id, status=TaskStatus.QUEUED, started_at=None, finished_at=None
        )
        self._done_event(task.id).clear()
        self._resume_pending[task.id] = prompt
        self._spawn(task.id, resume=resume)
        return updated

    async def cancel(self, task_id: int) -> Task:
        """Stop a queued or running task. Terminal tasks come back unchanged."""
        task = await self._store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in TERMINAL_STATUSES:
            return task

        # The flag goes up *before* the interrupt: interrupting is what usually ends the
        # turn, so `run()` can return (and `_execute` reach its post-run check) long before
        # the asyncio cancellation below lands.
        self._cancel_requested.add(task_id)
        session = self._live.get(task_id)
        if session is not None:
            await self._interrupt(task_id, session)

        runner_task = self._tasks.get(task_id)
        if runner_task is not None:
            runner_task.cancel()
            await asyncio.wait({runner_task}, timeout=CANCEL_TIMEOUT_S)

        current = await self._store.get(task_id)
        if current is not None and current.status not in TERMINAL_STATUSES:
            # The asyncio task never got to run its cancellation handler.
            current = await self._mark_cancelled(task_id) or current
        self._release(task_id)
        return current

    # --- projects --------------------------------------------------------

    def _candidates(self) -> dict[str, Path]:
        """Every known project; shared with the voice prompt, which lists the same names."""
        return discover_projects(self._settings)

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
    async def list(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 20,
        include_internal: bool = False,
    ) -> list[Task]:
        """Tasks newest-first, optionally filtered to one status. Housekeeping is hidden."""
        return await self._store.list(
            status=status, limit=limit, include_internal=include_internal
        )
