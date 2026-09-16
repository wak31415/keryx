"""Keeping a memory of what has been said, one call at a time (spec §3.3).

The provider keeps no history across sockets, so every call opens blank unless something
writes down what happened in the last one. That is this: on `SessionEnded`, Jarvis
dispatches a subagent to itself whose whole job is to fold the call that just ended into
`data_dir/memory.md`, which `jarvis.continuity.briefing` reads back into the next call's
prompt.

It is a real subagent rather than a summarising API call because the memory is worth more
when whoever writes it can go and look: open the report of the task that call dispatched,
check whether the thing he was waiting on has landed, read the repo he was asking about.
The cost is that it is a task like any other, so it is dispatched `internal=True` — see
`Task.internal` — which keeps it out of the spoken task lists, out of the daily cap and
out of the notifier. It restricts nothing about what that subagent may do.

Which is why it only ever runs for a call that was **authorized**: a local session, or a
phone call that gave the PIN. Caller id is spoofable, and this subagent reads the
transcript with a shell at its disposal, so the words of a caller who never gave the PIN
would be instructions to that shell — and anything it wrote into `memory.md` would ride
into the prompt of every call after. The PIN is the control, not a narrower tool set.

Nothing here is allowed to break a call. The session has already ended by the time this
runs, and every failure path ends in a log line.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.config import Settings, secure_file
from jarvis.continuity.transcripts import transcript_path
from jarvis.events import EventBus, SessionEnded, TaskCompleted, TaskFailed
from jarvis.prompts import render_prompt

if TYPE_CHECKING:  # pragma: no cover - typing only
    #: Type-only, and it has to stay that way. `briefing` imports `read_memory` from here,
    #: `session` imports `briefing`, and `tasks.manager` imports the Claude Agent SDK at
    #: module scope — so a real import here would put the SDK behind reading a filename.
    from jarvis.tasks.manager import TaskManager
    from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.memory")

MEMORY_PROMPT = "memory_update.md"

MEMORY_FILE = "memory.md"

#: How much of the memory reaches the system prompt. It is spoken from, not read out, and
#: a realtime session pays for every token of instructions on every turn.
MAX_MEMORY_CHARS = 4000
#: How much of it may sit on disk. Looser than the read limit on purpose: this module's
#: own prompt is what keeps the document short, and this is only the backstop that stops a
#: runaway rewrite growing the file without limit. See `trim_memory`.
MAX_MEMORY_FILE_CHARS = 3 * MAX_MEMORY_CHARS

_TRIMMED_NOTE = "\n\n(older memory trimmed)"

#: A call has to have been a conversation before it is worth a subagent. Two spoken lines
#: is the greeting and one reply — below that it was a misfire, a wrong number, or a wake
#: word the television set off, and there is nothing to remember.
MIN_SPOKEN_LINES = 2

_NO_TASKS = "none"
_SPOKEN_PREFIXES = ("user:", "assistant:")


def count_spoken_lines(path: Path) -> int:
    """How many `user:`/`assistant:` lines a transcript has. 0 when it cannot be read."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    spoken = 0
    for line in raw.splitlines():
        body = line.split("] ", 1)[-1].strip() if line.startswith("[") else line.strip()
        if body.startswith(_SPOKEN_PREFIXES):
            spoken += 1
    return spoken


def describe_tasks(tasks: list["Task"]) -> str:
    """The tasks a call dispatched, as one line for the prompt."""
    if not tasks:
        return _NO_TASKS
    return "; ".join(f"task {task.id} ({task.status}) — {task.description}" for task in tasks)


def memory_path(data_dir: Path) -> Path:
    """Where the rolling memory lives."""
    return data_dir / MEMORY_FILE


def trim_memory(data_dir: Path, *, max_chars: int = MAX_MEMORY_FILE_CHARS) -> bool:
    """Bound `memory.md` on disk. True when it was actually shortened.

    `read_memory` bounds what reaches a *prompt*; this bounds the file, which nothing else
    did — it is written by a subagent, under a prompt that asks it to stay within
    `MAX_MEMORY_CHARS`, and a prompt is not a bound. The limit here is deliberately looser
    than the read limit so that the subagent's own discipline stays the primary mechanism
    and this stays a backstop against a runaway rewrite.

    Trimmed from the *end*, for the same reason `read_memory` is: the document is written
    standing-facts-first and recent-calls-last.
    """
    path = memory_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if len(text) <= max_chars:
        secure_file(path)
        return False
    try:
        path.write_text(text[:max_chars].rstrip() + "\n", encoding="utf-8")
    except OSError:
        log.warning("could not trim %s", path)
        return False
    secure_file(path)
    log.info("trimmed %s from %d to %d characters", path.name, len(text), max_chars)
    return True


def read_memory(data_dir: Path, *, max_chars: int = MAX_MEMORY_CHARS) -> str:
    """The memory document, trimmed to `max_chars`. Empty when there is none yet.

    Trimmed from the *end*: the document is written standing-facts-first and
    recent-calls-last, so what survives a trim is the part that is true for longest.
    """
    try:
        text = memory_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + _TRIMMED_NOTE


class MemoryWriter:
    """Dispatches the memory update after every call that was worth remembering."""

    def __init__(self, bus: EventBus, manager: "TaskManager", settings: Settings) -> None:
        self._bus = bus
        self._manager = manager
        self._settings = settings
        self._remove = None
        self._remove_finished: list = []
        #: Memory-update tasks this writer dispatched and is still waiting on, so the trim
        #: below fires for our subagent's write and not for every task that finishes.
        self._writing: set[int] = set()

    def start(self) -> None:
        """Subscribe to `SessionEnded`, and to the end of our own update tasks. Idempotent."""
        if self._remove is None:
            self._remove = self._bus.subscribe(SessionEnded, self._on_session_ended)
        if not self._remove_finished:
            self._remove_finished = [
                self._bus.subscribe(event, self._on_task_finished)
                for event in (TaskCompleted, TaskFailed)
            ]

    def stop(self) -> None:
        """Unsubscribe. Idempotent."""
        for remove in self._remove_finished:
            remove()
        self._remove_finished = []
        self._writing.clear()
        if self._remove is not None:
            self._remove()
            self._remove = None

    async def _on_session_ended(self, event: SessionEnded) -> None:
        """Dispatch the memory update for the call that just ended. Never raises."""
        try:
            await self._update(event)
        except Exception:
            log.exception("could not dispatch the memory update for session %s", event.session_id)

    async def _update(self, event: SessionEnded) -> None:
        if not event.authorized:
            log.info("session %s never gave the PIN; nothing of it is kept", event.session_id)
            return
        path = transcript_path(self._settings.data_dir, event.session_id)
        spoken = count_spoken_lines(path)
        if spoken < MIN_SPOKEN_LINES:
            log.info(
                "session %s had %d spoken line(s); nothing worth remembering",
                event.session_id,
                spoken,
            )
            return

        dispatched = await self._manager.tasks_for_session(event.session_id)
        prompt = render_prompt(
            MEMORY_PROMPT,
            owner=self._settings.owner_label,
            transcript_path=str(path),
            memory_path=str(memory_path(self._settings.data_dir)),
            max_chars=str(MAX_MEMORY_CHARS),
            session_id=event.session_id,
            channel=event.channel,
            reason=event.reason,
            tasks=describe_tasks(dispatched),
        )
        task = await self._manager.dispatch(
            prompt,
            origin_channel="internal",
            origin_caller=None,
            origin_session_id=event.session_id,
            cwd=str(self._settings.data_dir),
            internal=True,
        )
        if task.id is not None:
            self._writing.add(task.id)
        log.info("session %s: memory update dispatched as task %s", event.session_id, task.id)

    async def _on_task_finished(self, event: TaskCompleted | TaskFailed) -> None:
        """Bound `memory.md` as soon as the subagent that rewrote it has stopped.

        This is the "on write" half of the memory's size limit. `read_memory` trims what
        reaches a prompt; nothing trimmed the file, which is written by a subagent under a
        prompt asking it to stay short — good discipline, not a bound. Doing it here rather
        than on the next start means a runaway rewrite is corrected before anything reads it.
        """
        if event.task_id not in self._writing:
            return
        self._writing.discard(event.task_id)
        try:
            trim_memory(self._settings.data_dir)
        except Exception:  # pragma: no cover - trim_memory swallows its own OSErrors
            log.exception("could not trim the memory after task %s", event.task_id)
