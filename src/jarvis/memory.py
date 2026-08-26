"""Keeping a memory of what has been said, one call at a time (spec §3.3).

The provider keeps no history across sockets, so every call opens blank unless something
writes down what happened in the last one. That is this: on `SessionEnded`, Jarvis
dispatches a subagent to itself whose whole job is to fold the call that just ended into
`data_dir/memory.md`, which `jarvis.briefing` reads back into the next call's prompt.

It is a real subagent rather than a summarising API call because the memory is worth more
when whoever writes it can go and look: open the report of the task that call dispatched,
check whether the thing he was waiting on has landed, read the repo he was asking about.
The cost is that it is a task like any other, so it is dispatched `internal=True` — see
`Task.internal` — which keeps it out of the spoken task lists, out of the daily cap and
out of the notifier. It restricts nothing about what that subagent may do.

Nothing here is allowed to break a call. The session has already ended by the time this
runs, and every failure path ends in a log line.
"""

import logging
from pathlib import Path

from jarvis.briefing import MAX_MEMORY_CHARS, memory_path
from jarvis.config import Settings
from jarvis.events import EventBus, SessionEnded
from jarvis.prompts import render_prompt
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import Task
from jarvis.transcripts import transcript_path

log = logging.getLogger("jarvis.memory")

MEMORY_PROMPT = "memory_update.md"

#: A call has to have been a conversation before it is worth a subagent. Two spoken lines
#: is the greeting and one reply — below that it was a misfire, a wrong number, or a wake
#: word the dog set off, and there is nothing to remember.
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


def describe_tasks(tasks: list[Task]) -> str:
    """The tasks a call dispatched, as one line for the prompt."""
    if not tasks:
        return _NO_TASKS
    return "; ".join(f"task {task.id} ({task.status}) — {task.description}" for task in tasks)


class MemoryWriter:
    """Dispatches the memory update after every call that was worth remembering."""

    def __init__(self, bus: EventBus, manager: TaskManager, settings: Settings) -> None:
        self._bus = bus
        self._manager = manager
        self._settings = settings
        self._remove = None

    def start(self) -> None:
        """Subscribe to `SessionEnded`. Idempotent."""
        if self._remove is None:
            self._remove = self._bus.subscribe(SessionEnded, self._on_session_ended)

    def stop(self) -> None:
        """Unsubscribe. Idempotent."""
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
        log.info("session %s: memory update dispatched as task %s", event.session_id, task.id)
