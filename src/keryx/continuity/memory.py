"""Keeping a memory of what has been said, one call at a time.

The provider keeps no history across sockets, so every call opens blank unless something
writes down what happened in the last one. That is this: on `SessionEnded`, Keryx
dispatches a subagent to itself whose whole job is to fold the call that just ended into
`data_dir/memory.md`, which `keryx.continuity.briefing` reads back into the next call's
prompt. The only other writer is `keryx setup` (and `keryx memory seed`), which
`seed_memory`s a first draft before any call has happened and `add_standing_facts` to it;
`memory_skeleton` is the structure they share.

It is a real subagent rather than a summarising API call because the memory is worth more
when whoever writes it can go and look: open the report of the task that call dispatched,
check whether the thing they were waiting on has landed, read the repo they were asking about.
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
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from keryx.config import Settings, secure_dir, secure_file, write_private
from keryx.continuity.transcripts import transcript_path
from keryx.events import EventBus, SessionEnded, TaskCompleted, TaskFailed
from keryx.prompts import render_prompt

if TYPE_CHECKING:  # pragma: no cover - typing only
    #: Type-only, and it has to stay that way. `briefing` imports `read_memory` from here,
    #: `session` imports `briefing`, and `tasks.manager` imports the Claude Agent SDK at
    #: module scope — so a real import here would put the SDK behind reading a filename.
    from keryx.tasks.manager import TaskManager
    from keryx.tasks.models import Task

log = logging.getLogger("keryx.memory")

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

#: The memory's sections, in the order they are written: what stays true first, what
#: happened lately last — which is why both trims cut from the end. `memory_skeleton` is the
#: only thing that reads this.
_SECTIONS = ("Standing facts", "Ongoing threads", "Recent calls")
#: A bullet somebody typed in front of a fact, which `compose_memory` supplies itself.
_BULLET = re.compile(r"^(?:[-*•]\s+)+")


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


def memory_skeleton(owner: str, assistant: str) -> str:
    """The memory document's structure, and the one place it is written down.

    The update prompt shows it to the subagent that keeps the file, and `compose_memory`
    fills it in for a first memory typed at `keryx setup`: a section renamed here is renamed
    for both.
    """
    headings = "\n".join(f"## {section}" for section in _SECTIONS)
    return f"# What {assistant} knows about {owner}\n\n{headings}"


def compose_memory(owner: str, facts: Iterable[str], *, assistant: str) -> str:
    """A first memory document: the skeleton, with `facts` as its standing facts.

    Each fact becomes one bullet on one line, whatever bullet or line breaks it arrived
    with, and a blank one is dropped.
    """
    cleaned = (_BULLET.sub("", " ".join(fact.split())) for fact in facts)
    bullets = "\n".join(f"- {fact}" for fact in cleaned if fact)
    standing = f"## {_SECTIONS[0]}"
    blocks: list[str] = []
    for line in memory_skeleton(owner, assistant).splitlines():
        if line.strip():
            blocks.append(line)
        if line == standing and bullets:
            blocks.append(bullets)
    return "\n\n".join(blocks) + "\n"


def seed_memory(
    data_dir: Path,
    *,
    owner: str,
    assistant: str,
    facts: Iterable[str],
    force: bool = False,
) -> bool:
    """Write a first `memory.md` from `facts`. True when written.

    False, and nothing touched, when there is already a memory with anything in it and
    `force` is not set: what calls have written down is worth more than a first draft.
    Raises `ValueError` when the document is longer than `MAX_MEMORY_CHARS`, which is what a
    call reads — somebody is at the keyboard to shorten it, where a trim would quietly lose
    the end of it on every call.
    """
    if not force and read_memory(data_dir):
        return False
    text = compose_memory(owner, facts, assistant=assistant)
    if len(text) > MAX_MEMORY_CHARS:
        raise ValueError(
            f"that memory is {len(text)} characters, and a call reads {MAX_MEMORY_CHARS}"
        )
    secure_dir(data_dir)
    path = memory_path(data_dir)
    path.write_text(text, encoding="utf-8")
    secure_file(path)
    log.info("seeded %s with %d characters", path, len(text))
    return True


def add_standing_facts(
    data_dir: Path, *, owner: str, assistant: str, facts: Iterable[str]
) -> None:
    """Add `facts` to the memory's standing facts; a first memory when there is none.

    The bullets go at the end of the "Standing facts" section, whatever calls have written
    since, and the section is added when a hand edit took it out. Raises `ValueError` past
    `MAX_MEMORY_CHARS`, for the same reason `seed_memory` does.
    """
    path = memory_path(data_dir)
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if not existing.strip():
        seed_memory(data_dir, owner=owner, assistant=assistant, facts=facts, force=True)
        return
    cleaned = (_BULLET.sub("", " ".join(fact.split())) for fact in facts)
    bullets = [f"- {fact}" for fact in cleaned if fact]
    lines = existing.rstrip("\n").splitlines()
    heading = f"## {_SECTIONS[0]}"
    if heading in lines:
        end = lines.index(heading) + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        while end > 0 and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = bullets
    else:
        lines += ["", heading, "", *bullets]
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_MEMORY_CHARS:
        raise ValueError(
            f"that memory would be {len(text)} characters, and a call reads {MAX_MEMORY_CHARS}"
        )
    write_private(path, text)


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
            assistant=self._settings.assistant_name,
            structure=memory_skeleton(
                self._settings.owner_label, self._settings.assistant_name
            ),
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
