"""What Jarvis knows before the first word of a call (spec §3.3).

A realtime session starts with no history: the provider keeps nothing across sockets, so
without help every call opens as if it were the first one ever. Two things fix that, and
both are assembled here, once, at session start:

1. **The digest.** Tasks that finished while nobody was talking to Jarvis. `announced`
   and `sms_sent` record that a *delivery* was attempted; neither survives a call they
   missed or a text they never read. `reported_at` records that Jarvis actually said it,
   and until it is stamped the task comes back at the top of the next call. The voice
   model stamps it with `mark_reported` once it has told them (spec §3.3 ruling).
2. **The memory.** `data_dir/memory.md`, rewritten after every call by the subagent
   `jarvis.continuity.memory` dispatches, and read back through its `read_memory`.
   Standing facts and what recent calls were about, so "the thing we talked about
   yesterday" resolves to something.

The two are not equally private, and since 2026-09-19 they are not gated alike. The
**memory** waits for `FULL` (`jarvis.trust`) along with the project briefs and the skills:
it is the map of the owner's whole world and the injection surface, and none of it is
news. The **digest** does not. The owner's ruling is that unheard results are what they
ring in for, and a digest that waits for the PIN is one they often never get; the price,
accepted, is that a caller who has spoofed an allowed number hears those summaries.
`DIGEST_BEFORE_PIN` (default on) is the switch, and off restores the older silence. A call
Jarvis placed to the owner's own number hears the digest whatever the setting — the
setting is about strangers, and possession is not one.

`VoiceSession` asks for a briefing at session start whenever the digest is allowed, drops
the memory from it below `FULL`, and asks again the moment the PIN goes in — updating the
prompt, and handing over `after_pin_nudge()` unless the digest has already been given.

Nothing here may fail a call. Every read is guarded and the worst case is a briefing with
empty parts, which renders as a prompt with those sections left out entirely.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from jarvis.config import Settings
from jarvis.continuity.memory import read_memory
from jarvis.tasks.models import Task, TaskStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.tasks.manager import TaskManager

log = logging.getLogger("jarvis.briefing")

#: How much of one task's summary the digest carries. Enough to say a sentence about it.
MAX_DIGEST_SUMMARY_CHARS = 200
#: How much of one task's description the digest carries, to remind them what they asked for.
MAX_DIGEST_REQUEST_CHARS = 100

_DIGEST_HEADING = (
    "These finished while you were not talking to the owner, and the owner has not heard about "
    "them yet. Lead with them: say what landed in a sentence or two — not a recital — and then "
    "call mark_reported with the ids you actually mentioned."
)
_DIGEST_MORE = "\n\n(and {count} more waiting; these are the oldest.)"
#: Appended to the message that opens the session, because a realtime model leads with
#: what it was just told far more reliably than with a section of its system prompt.
OPENING_NUDGE = (
    " [system] {count} finished while you were away and the owner has not heard yet — see "
    '"What the owner has not heard yet" and lead with it, briefly, after your greeting.'
)
#: The same nudge for a phone call, which is briefed only once the PIN is accepted — by
#: which time they have been greeted and have usually asked for something.
AFTER_PIN_NUDGE = (
    "[system] {count} finished while you were away and the owner has not heard yet — see "
    '"What the owner has not heard yet". Tell them briefly, once what they just asked for is in '
    "hand (straight away if they asked for nothing), and do not greet them again."
)


@dataclass(frozen=True)
class Briefing:
    """The two blocks a session opens with. Empty strings mean "leave the section out"."""

    memory: str = ""
    pending: str = ""
    #: How many finished tasks are waiting, in total — not just the ones in `pending`.
    pending_count: int = 0
    #: The tasks `pending` actually names, which is what `mark_reported` may stamp on a
    #: call that has not given the PIN: the digest named them, so saying them is what the
    #: call is for. Never the `pending_count` remainder — those were not read out.
    task_ids: tuple[int, ...] = ()

    def without_memory(self) -> "Briefing":
        """The same briefing with the memory dropped: the digest is news, the memory is not.

        A call below `FULL` may hear what it has not been told about (`DIGEST_BEFORE_PIN`)
        and may not hear what Jarvis knows about the owner — the second is the map of their
        whole world and the injection surface, and no part of it is today's news.
        """
        return replace(self, memory="")

    def opening_nudge(self) -> str:
        """The clause to append to the message that opens the session, if any."""
        return OPENING_NUDGE.format(count=self._count()) if self.pending_count else ""

    def after_pin_nudge(self) -> str:
        """The note to hand a phone call once the PIN has let its briefing in, if any."""
        return AFTER_PIN_NUDGE.format(count=self._count()) if self.pending_count else ""

    def _count(self) -> str:
        noun = "task" if self.pending_count == 1 else "tasks"
        return f"{self.pending_count} {noun}"


def _shorten(text: str, limit: int) -> str:
    """`text` collapsed onto one line and cut to `limit` characters."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


def _digest_line(task: Task) -> str:
    """One unreported task, as a line the model can speak from."""
    verb = "failed" if task.status is TaskStatus.FAILED else "finished"
    detail = task.error if task.status is TaskStatus.FAILED else task.summary
    request = _shorten(task.description, MAX_DIGEST_REQUEST_CHARS)
    line = f"- task {task.id} ({verb}) — they asked for: {request}"
    if detail:
        line += f"\n  Result: {_shorten(detail, MAX_DIGEST_SUMMARY_CHARS)}"
    return line


def format_digest(tasks: list[Task], total: int) -> str:
    """The unreported tasks as a prompt block, or "" when there is nothing to say."""
    if not tasks:
        return ""
    body = "\n".join(_digest_line(task) for task in tasks)
    digest = f"{_DIGEST_HEADING}\n\n{body}"
    if total > len(tasks):
        digest += _DIGEST_MORE.format(count=total - len(tasks))
    return digest


class BriefingSource(Protocol):
    """Anything a `VoiceSession` can ask for its opening context.

    A Protocol rather than the concrete `Briefer` so a session never has to reach for a
    `TaskManager`, and so a test can hand one a fixed `Briefing` without a store.
    """

    async def build(self) -> Briefing:
        """The digest and the memory for a session about to open."""
        ...


class Briefer:
    """Builds one `Briefing` per session. Injected into `VoiceSession` so tests can fake it."""

    def __init__(self, settings: Settings, manager: "TaskManager") -> None:
        self._settings = settings
        self._manager = manager

    async def build(self) -> Briefing:
        """The digest and the memory for a session about to open. Never raises."""
        pending, total, task_ids = await self._pending()
        return Briefing(
            memory=await self._memory(),
            pending=pending,
            pending_count=total,
            task_ids=task_ids,
        )

    async def _pending(self) -> tuple[str, int, tuple[int, ...]]:
        try:
            tasks = await self._manager.unreported()
            total = await self._manager.count_unreported()
        except Exception:
            log.exception("could not read the unreported tasks; opening without a digest")
            return "", 0, ()
        return format_digest(tasks, total), total, tuple(task.id for task in tasks)

    async def _memory(self) -> str:
        try:
            return await asyncio.to_thread(read_memory, self._settings.data_dir)
        except Exception:
            log.exception("could not read the memory; opening without it")
            return ""
