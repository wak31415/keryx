"""What every builtin tool module shares (spec §3.2, §3.3).

Split out of `builtin.py` (2026-09-02), where it had accumulated at the top of one very
long function. Three kinds of thing live here.

**The wording.** Everything the model is handed is written for something that is
*speaking*: ids are small integers, lists are short by default, long text is cut down
before it can reach a text-to-speech engine, and a failure comes back as a sentence the
assistant can say. Several of those sentences are shared by more than one tool, and prose
that gets read down a phone is worth reviewing in one place.

**The parsing.** `_task_id`, `_clamp_limit` and friends turn whatever a speech model put in
an argument into something a task store can take, without ever raising.

**The gates.** `pin_gate` is the refusal a call gets below `FULL` (`jarvis.trust`), and it
is on most tools. Caller id is spoofable, so what opens a subagent (`dispatch_task`,
`cancel_task`), what takes the phone off the air or runs a command (`restart_service`),
what reads the owner's tasks, calls, projects or world (`list_tasks`, `get_task_status`,
`get_task_result`, `recall`, `list_projects`) and what writes as them (`send_to_slack`)
all need the PIN. Only `check_billing`, `cluster_stats`, `web_search`, `submit_pin` and
`end_session` answer at any level.

`possession_gate` is the second, for the handful a call *Jarvis placed* may use:
`send_followup` and `request_callback`, which are how the owner answers the question
Claude came back with, and the two approval tools. Below `POSSESSION` it is `pin_gate`; at
it, an action driven by speech also wants one keypress on the call, because an answering
machine can be talked at and cannot press a key. `mark_reported` is neither — it is gated
by *which* tasks, not by whether: below `POSSESSION` it may stamp only what this call
actually named (`VoiceSession.reportable_task_ids`).

Both read `ctx.trust` live, so a PIN keyed while the model was thinking is honoured on the
very next call, and the digits themselves never pass through here: `submit_pin` hands what
the caller said straight to the session, which is the only thing that compares it.
`get_task` is the other one — a task number in, a `Task` or the error dict to hand back.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from jarvis.config import Settings
from jarvis.continuity.recall import DEFAULT_LIMIT as DEFAULT_RECALL_LIMIT
from jarvis.continuity.recall import MAX_LIMIT as MAX_RECALL_LIMIT
from jarvis.integrations.billing import BillingReader
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import Task, TaskStatus
from jarvis.tools.registry import ToolContext
from jarvis.trust import TrustLevel

log = logging.getLogger("jarvis.tools.builtin")

#: How `check_billing` gets a reader: a provider name (or None for the configured
#: default) in, a `BillingReader` out, `BillingError` when there is no credential for it.
#: A factory rather than a reader, because the model may name either provider per call.
BillingFactory = Callable[[str | None], BillingReader]

#: What `cluster_stats` answers for when they do not name one: everything it knows.
ALL_CLUSTERS = ("both", "all", "everything")

#: How much of a task description a spoken list may carry.
MAX_DESCRIPTION_CHARS = 120
#: How much of a report `get_task_result` hands back for the model to summarise.
MAX_REPORT_CHARS = 1500
#: Default and maximum number of tasks `list_tasks` returns (short: this is spoken).
DEFAULT_TASK_LIMIT = 5
MAX_TASK_LIMIT = 20

#: The four PIN answers, written to cost one short sentence each. Transcripts of real
#: calls had a single PIN take four spoken turns — "that'll need your PIN", "please say it
#: or key it in", "let me verify that", "okay, you're authorized" — of which one carries
#: any information. Each message therefore says what to say *and* what not to say after it.
PIN_REQUIRED_MESSAGE = (
    "Ask for the PIN in one short sentence and wait. Do not explain why it is needed, do "
    "not say what you are about to do with it, and do not announce that you are checking "
    "it. Call this same tool again once they have given it, spoken or keyed in."
)
PIN_OK_MESSAGE = (
    "Correct, and they are authorized for the rest of this call. Say nothing about the PIN — "
    "not that it worked, not that you are verifying it, not that you are unlocking "
    "anything — and go straight on with what they asked for."
)
PIN_INVALID_MESSAGE = (
    "Not the PIN. One sentence: that it was not right, that they should try again, and how "
    "many attempts are left. Never repeat a digit back, and do not explain why a phone "
    "line mishears digits — they know."
)
PIN_LOCKED_MESSAGE = (
    "Too many wrong attempts and the call is ending. Say one short goodbye and nothing "
    "else; you have already been told this, so do not say it twice."
)
PIN_MISSING_MESSAGE = (
    "This needs a PIN on the phone, but none is configured. Tell them that in one sentence."
)
PIN_NOT_CONFIGURED_MESSAGE = (
    "There is no PIN set on this machine, so there is nothing to check. Tell them that in "
    "one sentence rather than asking again."
)
#: What a call Jarvis placed is asked for before it acts on something said out loud. An
#: answering machine can hold a conversation of sorts; it cannot press a key. Written to
#: cost one short sentence and to be asked once, the first time it is actually needed —
#: not as a greeting on every call-back.
KEYPRESS_REQUIRED_MESSAGE = (
    "Ask them to press any key on the keypad so you know it is really them — one short "
    "sentence, then wait. Do not explain why, do not say what you are about to do with "
    "it, and do not ask a second time. Call this same tool again once they have pressed "
    "one."
)
#: What `request_callback` says to a call that has not given the PIN. Jarvis rang this
#: number because the owner configured it; a number chosen mid-call is a new decision.
CALLBACK_OWNER_ONLY_MESSAGE = (
    "Tell them in one sentence that you can only ring back on this number. Do not offer "
    "to take another one, and do not explain the rule."
)
#: What `request_callback` hands back. The one thing it exists to prevent is the pair
#: "let me set that up for you" / "all set, I'll call you" around a tool that takes
#: milliseconds: arranging it and having arranged it are one fact, not two.
CALLBACK_SET_MESSAGE = (
    "Arranged. Tell them once, in a short clause — \"I'll ring you when it lands\" — and "
    "stop there. Not the task number again, not what that call will contain, and not a "
    "second confirmation if you already said you were setting it up."
)
CALLBACK_ALREADY_DONE_MESSAGE = (
    "That task has already finished, so there is nothing to call back about. Tell them what "
    "came of it now instead, and then call mark_reported."
)
#: What `mark_reported` hands back. It is also registered `silent=True`, so in the normal
#: case nothing is generated over this at all; the wording is here for the model that goes
#: looking at the result anyway.
REPORTED_MESSAGE = (
    "Recorded. This is bookkeeping and they have already heard the result, so say nothing "
    "about it and do not repeat what you just told them."
)
STILL_RUNNING_MESSAGE = (
    "still running. Say the task number once and that you will tell them when it lands. "
    "Nothing about what the answer will contain — you do not know yet."
)
SEARCH_FAILED_MESSAGE = "the search came back empty; say so, or offer to put Claude on it"
SLACK_FAILED_MESSAGE = "Slack would not take the message; tell them it did not go through"
RECALL_EMPTY_MESSAGE = (
    "nothing on record about that; say so plainly and offer to put Claude on it"
)
ENDING_MESSAGE = "The session is ending now; do not say anything else."
#: What `answer_approval` hands back. It never answers anything itself: the most it can do
#: is put the menu in the model's mouth, and the keypad does the rest (jarvis/approvals).
APPROVAL_KEYPAD_MESSAGE = (
    "Read the request back to them once, as written, then read these options out and wait. "
    "The owner answers with the keypad and only with the keypad — if they say yes out loud, ask "
    "them to press the key anyway. Do not call this tool again unless they ask for the menu again."
)
APPROVAL_PHONE_ONLY_MESSAGE = (
    "Approvals are answered on the phone keypad, and this is not a phone call. Tell them it "
    "is still waiting on their screen."
)
APPROVAL_NONE_MESSAGE = "Nothing is waiting for an answer; tell them so."

#: A phone number we are willing to call back: E.164, `+` and 7–15 digits.
_E164_RE = re.compile(r"^\+\d{7,15}$")

#: The task statuses `list_tasks` can filter on, in the words the model uses.
_STATUS_FILTERS: dict[str, tuple[TaskStatus, ...]] = {
    "all": (),
    "running": (TaskStatus.QUEUED, TaskStatus.RUNNING),
    "done": (TaskStatus.DONE,),
    "failed": (TaskStatus.FAILED,),
}

#: The task number every task-scoped tool takes, and the whole parameter schema of a
#: tool that takes nothing else. Read-only once registered, so one copy is shared.
_TASK_ID_PROPERTY = {"type": "integer", "description": "The task number."}
_TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {"task_id": _TASK_ID_PROPERTY},
    "required": ["task_id"],
}

MODEL_DESCRIPTION = (
    "Optional model for the subagent: opus (strongest, the default), sonnet, fable or "
    "haiku (fastest). Leave this out unless the user asks for it."
)
WAIT_DESCRIPTION = (
    "How many seconds to hold the line for the answer, 0 to 25. Use about 20 for quick "
    "questions so you can answer inline; use 0 for long jobs, which are announced later."
)


# --- small helpers ---------------------------------------------------------


def _shorten(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters, ending in an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _small_int(raw: object) -> int | None:
    """`raw` as an int, whether the model sent a number or spelled it as a string."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return None


def _task_id(arguments: dict) -> int | None:
    """The `task_id` argument as an int, or None if the model made one up."""
    try:
        return int(arguments.get("task_id"))
    except (TypeError, ValueError):
        return None


def _text(arguments: dict, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


def _clamp_wait(raw: object, settings: Settings) -> float:
    """The requested inline wait, clamped to `[0, dispatch_wait_max_seconds]`."""
    try:
        wait = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(max(wait, 0.0), float(settings.dispatch_wait_max_seconds))


def _clamp_limit(raw: object) -> int:
    try:
        limit = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TASK_LIMIT
    return min(max(limit, 1), MAX_TASK_LIMIT)


def _clamp_recall_limit(raw: object) -> int:
    try:
        limit = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RECALL_LIMIT
    return min(max(limit, 1), MAX_RECALL_LIMIT)


def _task_ids(raw: object) -> list[int]:
    """The `task_ids` argument as a list of ints, dropping anything that is not one.

    The model sometimes hands over a single number, or a list with a stray string in it;
    neither is worth an error it would have to explain out loud.
    """
    values = raw if isinstance(raw, list | tuple) else [raw]
    ids: list[int] = []
    for value in values:
        try:
            ids.append(int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return ids


def _brief(task: Task) -> dict:
    """One task as a spoken list entry."""
    entry = {
        "id": task.id,
        "status": task.status.value,
        "description": _shorten(task.description, MAX_DESCRIPTION_CHARS),
    }
    if task.summary:
        entry["summary"] = task.summary
    return entry


async def _report_excerpt(task: Task) -> str | None:
    """The head of the task's report file, or None when there is no readable report."""
    if not task.report_path:
        return None
    try:
        text = await asyncio.to_thread(Path(task.report_path).read_text, encoding="utf-8")
    except OSError:
        log.warning("could not read the report of task %s", task.id)
        return None
    return _shorten(text.strip(), MAX_REPORT_CHARS) or None




def pin_gate(ctx: ToolContext, settings: Settings) -> dict | None:
    """The refusal a phone caller gets before the PIN, if any (spec §3.3, §5).

    Called first, before a task number is even looked up, so a refusal says nothing about
    what exists. Reaching into a running task opens the same bypassPermissions subagent
    that dispatching one would; reading a task, a past call or a waiting prompt reads
    something private; and a note, a stamp or a Slack message outlives the call. See the
    module docstring for the five tools that skip it, and why.
    """
    if ctx.trust is TrustLevel.FULL:
        return None
    if not settings.pin:
        return {"status": "refused", "message": PIN_MISSING_MESSAGE}
    log.info("session %s needs a PIN first", ctx.session.session_id)
    return {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}


def possession_gate(
    ctx: ToolContext, settings: Settings, *, keypress: bool = True
) -> dict | None:
    """The refusal for a tool a call Jarvis placed may use, if any (`jarvis.trust`).

    `POSSESSION` is reaching a phone Jarvis dialled at the owner's own number, and it is
    what answering the question Claude came back with is worth — the point of the tier.
    Below it this is `pin_gate` and nothing more.

    `keypress` is the other half, and it is about voicemail. An answering machine takes an
    outbound call, plays a greeting and records; it cannot press a key. So an action
    driven by *speech* at this level asks for one keypress earlier in the same call.
    Answering an approval passes `keypress=False`: the answer is itself a key.
    """
    if ctx.trust is TrustLevel.FULL:
        return None
    if ctx.trust is not TrustLevel.POSSESSION:
        return pin_gate(ctx, settings)
    if not keypress or ctx.session.keypressed:
        return None
    log.info("session %s needs a keypress before it acts", ctx.session.session_id)
    return {"status": "keypress_required", "message": KEYPRESS_REQUIRED_MESSAGE}


async def get_task(manager: TaskManager, arguments: dict) -> Task | dict:
    """The task named by `arguments`, or the error dict to hand back instead."""
    task_id = _task_id(arguments)
    if task_id is None:
        return {"error": "task_id must be a task number, for example 3"}
    task = await manager.get(task_id)
    if task is None:
        return {"error": f"no task {task_id}"}
    return task
