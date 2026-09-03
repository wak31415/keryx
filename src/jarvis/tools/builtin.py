"""The tools the voice model calls to get real work done (spec §3.2, §3.3).

Everything here is written for a model that is *speaking*: descriptions say when to
reach for a tool, ids are small integers, lists are short by default, and long text is
cut down before it ever reaches a text-to-speech engine. Handlers never raise — a
problem comes back as `{"error": ...}` (or a `status` the model is told how to relay),
so a bad task id is a sentence the assistant can say rather than a dropped call.

The PIN gate guards every tool that can put a subagent to work — `dispatch_task`,
`send_followup` and `cancel_task` — and `restart_service`, which can take the phone
channel off the air: on the phone they are refused with
`{"status": "pin_required"}` until the session is authorized. Every task is gated now
that there is one kind of task, and it has the machine and the mailbox. The check reads
`ctx.authorized` live, so a PIN entered on the keypad while the model was thinking is
honoured on the very next call. The digits themselves never pass through here: the
`submit_pin` tool hands whatever the caller said straight to the session, which is the
only thing that ever compares it.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from jarvis.approvals.broker import ApprovalBroker
from jarvis.billing import BillingError, BillingReader
from jarvis.cluster import ClusterError, ClusterQuerier, ClusterReport
from jarvis.config import Settings
from jarvis.inline_waits import InlineWaits
from jarvis.recall import DEFAULT_LIMIT as DEFAULT_RECALL_LIMIT
from jarvis.recall import MAX_LIMIT as MAX_RECALL_LIMIT
from jarvis.recall import Recaller
from jarvis.restart import RestartCoordinator
from jarvis.slack import SlackSender
from jarvis.tasks.manager import TERMINAL_STATUSES, TaskLimitError, TaskManager, UnknownProjectError
from jarvis.tasks.models import Task, TaskStatus
from jarvis.tools.registry import ToolContext, ToolRegistry
from jarvis.web_search import WebSearcher

log = logging.getLogger("jarvis.tools.builtin")

#: How `check_billing` gets a reader: a provider name (or None for the configured
#: default) in, a `BillingReader` out, `BillingError` when there is no credential for it.
#: A factory rather than a reader, because the model may name either provider per call.
BillingFactory = Callable[[str | None], BillingReader]

#: What `cluster_stats` answers for when he does not name one: everything it knows.
ALL_CLUSTERS = ("both", "all", "everything")

#: How much of a task description a spoken list may carry.
MAX_DESCRIPTION_CHARS = 120
#: How much of a report `get_task_result` hands back for the model to summarise.
MAX_REPORT_CHARS = 1500
#: Default and maximum number of tasks `list_tasks` returns (short: this is spoken).
DEFAULT_TASK_LIMIT = 5
MAX_TASK_LIMIT = 20

PIN_REQUIRED_MESSAGE = (
    "Ask the caller to say the PIN or enter it on the keypad, then call the tool again."
)
PIN_MISSING_MESSAGE = "A PIN is required to dispatch work but none is configured."
CALLBACK_NUMBER_MESSAGE = (
    "Without the PIN I can only call back on the number of this call or a number I "
    "already know. Offer that instead."
)
STILL_RUNNING_MESSAGE = "still running; you will be told when it finishes"
SEARCH_FAILED_MESSAGE = "the search came back empty; say so, or offer to put Claude on it"
SLACK_FAILED_MESSAGE = "Slack would not take the message; tell him it did not go through"
RECALL_EMPTY_MESSAGE = (
    "nothing on record about that; say so plainly and offer to put Claude on it"
)
ENDING_MESSAGE = "The session is ending now; do not say anything else."
#: What `answer_approval` hands back. It never answers anything itself: the most it can do
#: is put the menu in the model's mouth, and the keypad does the rest (jarvis/approvals).
APPROVAL_KEYPAD_MESSAGE = (
    "Read the request back to him once, as written, then read these options out and wait. "
    "He answers with the keypad and only with the keypad — if he says yes out loud, ask him "
    "to press the key anyway. Do not call this tool again unless he asks for the menu again."
)
APPROVAL_PHONE_ONLY_MESSAGE = (
    "Approvals are answered on the phone keypad, and this is not a phone call. Tell him it "
    "is still waiting on his screen."
)
APPROVAL_NONE_MESSAGE = "Nothing is waiting for an answer; tell him so."

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


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    manager: TaskManager,
    settings: Settings,
    inline_waits: InlineWaits,
    searcher: WebSearcher | None = None,
    slack: SlackSender | None = None,
    restarter: RestartCoordinator | None = None,
    recaller: Recaller | None = None,
    approvals: ApprovalBroker | None = None,
    billing: BillingFactory | None = None,
    cluster: ClusterQuerier | None = None,
) -> None:
    """Register every tool the voice model has, bound to this process's task manager.

    `web_search`, `send_to_slack`, `restart_service`, `recall`, `check_billing`,
    `cluster_stats` and the two approval tools are registered only when a `searcher` /
    `slack` / `restarter` / `recaller` / `billing` / `cluster` / `approvals` is supplied,
    so a process without one simply does not offer that tool. Registering
    `send_to_slack` only makes it *available*: whether it may be called is the voice
    model's decision, and both its description and the system prompt confine that to the
    turns where William explicitly asked for something on Slack.
    """

    async def _get(arguments: dict) -> Task | dict:
        """The task named by `arguments`, or the error dict to hand back instead."""
        task_id = _task_id(arguments)
        if task_id is None:
            return {"error": "task_id must be a task number, for example 3"}
        task = await manager.get(task_id)
        if task is None:
            return {"error": f"no task {task_id}"}
        return task

    # --- send_to_slack -----------------------------------------------------

    async def send_to_slack(ctx: ToolContext, arguments: dict) -> dict:
        message = _text(arguments, "message")
        if not message:
            return {"error": "message is required: say what to send"}
        assert slack is not None  # only registered when there is one
        if not await slack.send(message):
            return {"error": SLACK_FAILED_MESSAGE}
        return {"status": "sent"}

    if slack is not None:
        registry.register(
            "send_to_slack",
            "Send William a message on Slack, in the direct-message channel he already "
            "uses for this. Only call it when he has explicitly asked for something in "
            'writing — "send me that", "put it on Slack", "text me the link". Never '
            "call it unasked, however awkward the content is to say out loud, and never "
            "to repeat in writing something you have already said; if it truly will not "
            "survive being spoken, offer to send it and call this only once he accepts. "
            "For anything a subagent produced (a file, a plot, a report), dispatch the "
            "sending to Claude instead: it can attach the file itself.",
            {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send, written to be read rather "
                        "than heard.",
                    }
                },
                "required": ["message"],
            },
            send_to_slack,
        )

    # --- web_search --------------------------------------------------------

    async def web_search(ctx: ToolContext, arguments: dict) -> dict:
        query = _text(arguments, "query")
        if not query:
            return {"error": "query is required: say what to look up"}
        assert searcher is not None  # only registered when there is one
        answer = await searcher.search(query)
        if not answer:
            return {"error": SEARCH_FAILED_MESSAGE}
        return {"answer": answer}

    if searcher is not None:
        registry.register(
            "web_search",
            "Look something up on the web and get a short spoken answer. Use it yourself "
            "for small, factual questions — a price, a date, a score, what a company "
            "announced — instead of dispatching a task. Anything that needs his files, "
            "his repositories, his mail, or more than a couple of sentences of work goes "
            "to dispatch_task instead.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up, as a full question.",
                    }
                },
                "required": ["query"],
            },
            web_search,
        )

    # --- check_billing -----------------------------------------------------

    async def check_billing(ctx: ToolContext, arguments: dict) -> dict:
        """This month's spend, read out of the provider's own billing API.

        Read-only end to end, so it is not PIN-gated: it changes nothing, spends a
        fraction of a cent, and "what am I spending" is exactly the sort of small
        question the voice is supposed to answer itself rather than dispatch. Every
        failure comes back as a `status` with a sentence to say, never as a raised
        exception and never with a credential in it.
        """
        assert billing is not None  # only registered when there is one
        provider = _text(arguments, "provider").lower() or None
        try:
            reader = billing(provider)
            report = await reader.month_to_date()
        except BillingError as exc:
            log.warning("billing lookup failed: %s (%s)", exc.code, exc.detail)
            return {"status": exc.code, "message": exc.spoken}
        log.info(
            "billing: %s %.2f %s month to date", report.provider, report.spend, report.currency
        )
        return {"status": "ok", **report.as_dict()}

    if billing is not None:
        registry.register(
            "check_billing",
            "What the API bill is so far this month, and what it is on track to be. Call "
            "it when he asks what he is spending, what the bill looks like, or how much a "
            "provider has cost. It reads the provider's billing API and changes nothing. "
            "Say the figure to the nearest sensible amount rather than every decimal, and "
            "call the month-end number an estimate, because it is a straight-line "
            "projection from the month so far. Default is OpenAI — the account this call "
            "itself runs on; ask for anthropic when he means what Claude has cost.",
            {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["openai", "anthropic"],
                        "description": "Whose bill. Leave it out for the configured "
                        "default, which is OpenAI.",
                    }
                },
                "required": [],
            },
            check_billing,
        )

    # --- cluster_stats -----------------------------------------------------

    async def cluster_stats(ctx: ToolContext, arguments: dict) -> dict:
        """What Ionic and Tiger are doing right now, straight off Slurm.

        Un-PIN-gated for the same reason as `check_billing`: it is three read-only Slurm
        commands behind the ssh guard, it cannot start, stop or change anything, and the
        numbers it carries are counts and his own job ids — never a job name or a path.
        Both clusters are asked at once, and one being unreachable never costs the other:
        a failure comes back beside the report that worked, as a `status` with a sentence
        to say. See `jarvis/cluster.py` for why nothing here ever retries an expired login.
        """
        assert cluster is not None  # only registered when there is one
        wanted = _text(arguments, "cluster").lower()
        names = cluster.known() if wanted in ALL_CLUSTERS or not wanted else [wanted]
        results = await asyncio.gather(
            *(cluster.stats(name) for name in names), return_exceptions=True
        )

        reports: list[ClusterReport] = []
        failures: list[dict] = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, ClusterReport):
                reports.append(result)
            elif isinstance(result, ClusterError):
                log.warning("cluster %s unavailable: %s (%s)", name, result.code, result.detail)
                failures.append(
                    {"cluster": name, "status": result.code, "message": result.spoken}
                )
            elif isinstance(result, BaseException):
                raise result  # the registry turns anything else into {"error": ...}

        if not reports:
            first = failures[0] if failures else {"status": "unavailable", "message": ""}
            return {**first, "unavailable": failures}

        log.info("cluster stats for %s", ", ".join(report.cluster for report in reports))
        payload = {
            "status": "ok",
            "clusters": [report.as_dict() for report in reports],
            "spoken": " ".join(report.spoken() for report in reports),
        }
        if failures:
            payload["unavailable"] = failures
        return payload

    if cluster is not None:
        registry.register(
            "cluster_stats",
            "What the Ionic and Tiger clusters are doing right now: free, busy and down "
            "GPUs, how many jobs of his are running or queued, and how busy the queue is. "
            'Call it for "what\'s free on tiger", "am I still running on ionic", "how '
            'busy is the cluster", "how long until my job finishes". It only reads Slurm '
            "and changes nothing — submitting, cancelling or debugging a job is "
            "dispatch_task instead. Say the numbers roughly and say which cluster each "
            "one is; the free count already leaves out GPUs that are down or held for a "
            "queued job, so do not add them back. If a cluster comes back with a status "
            "other than ok, say the one thing it tells you to say for that cluster and "
            "still report the other.",
            {
                "type": "object",
                "properties": {
                    "cluster": {
                        "type": "string",
                        "enum": ["ionic", "tiger", "both"],
                        "description": "Which cluster. Leave it out for both, which is "
                        "the right answer when he just says \"the cluster\".",
                    }
                },
                "required": [],
            },
            cluster_stats,
        )

    # --- dispatch_task -----------------------------------------------------

    def _pin_gate(ctx: ToolContext) -> dict | None:
        """The refusal to return before putting a subagent to work, if any (spec §3.3).

        Applies to `dispatch_task`, `send_followup` and `cancel_task` alike: reaching into
        a task that is already running opens the very same bypassPermissions subagent that
        dispatching one would.
        """
        if ctx.channel != "phone" or ctx.authorized:
            return None
        if not settings.pin:
            return {"status": "refused", "message": PIN_MISSING_MESSAGE}
        log.info("session %s needs a PIN before dispatching", ctx.session.session_id)
        return {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}

    async def dispatch_task(ctx: ToolContext, arguments: dict) -> dict:
        refusal = _pin_gate(ctx)
        if refusal is not None:
            return refusal

        description = _text(arguments, "description")
        if not description:
            return {"error": "description is required: say what the subagent should do"}

        try:
            task = await manager.dispatch(
                description,
                project=_text(arguments, "project") or None,
                model=_text(arguments, "model") or None,
                origin_channel=ctx.channel,
                origin_caller=ctx.caller,
                origin_session_id=ctx.session.session_id,
            )
        except UnknownProjectError as exc:
            return {"error": str(exc), "candidates": exc.candidates}
        except (TaskLimitError, ValueError) as exc:
            return {"error": str(exc)}

        wait = _clamp_wait(arguments.get("wait_seconds"), settings)
        if wait > 0:
            # Marked for as long as we hold the line, so the notifier does not announce
            # into this session what the tool result below is about to say (spec §3.3).
            with inline_waits.holding(ctx.session.session_id, task.id):
                task = await manager.wait_for(task.id, wait)

        result = {"task_id": task.id, "status": task.status.value}
        if task.status in TERMINAL_STATUSES:
            result["summary"] = task.summary
            if task.status is TaskStatus.FAILED:
                result["error"] = task.error
        else:
            result["message"] = STILL_RUNNING_MESSAGE
        return result

    registry.register(
        "dispatch_task",
        "Hand a piece of work to Claude and get back a task number. Use it for anything you "
        "cannot answer yourself in a sentence or two, and for anything to do with code the "
        "moment you recognise it — do not ask the caller to confirm the request first, and "
        "do not interview him about details Claude can work out for itself. On the phone, "
        "every dispatch comes back as pin_required until the caller has given the PIN.",
        {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the subagent should do, in full sentences. It cannot "
                    "hear the conversation, so include every detail that matters.",
                },
                "project": {
                    "type": "string",
                    "description": "The name of the project to work in. Optional: leave it "
                    "out when he did not name one and a coding task starts in his projects "
                    "folder, where the subagent finds the repo itself. Use list_projects "
                    "only when he asks what exists.",
                },
                "model": {
                    "type": "string",
                    "enum": ["opus", "sonnet", "fable", "haiku"],
                    "description": MODEL_DESCRIPTION,
                },
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": settings.dispatch_wait_max_seconds,
                    "description": WAIT_DESCRIPTION,
                },
            },
            "required": ["description"],
        },
        dispatch_task,
    )

    # --- list_tasks --------------------------------------------------------

    async def list_tasks(ctx: ToolContext, arguments: dict) -> dict:
        status = (_text(arguments, "status") or "all").lower()
        if status not in _STATUS_FILTERS:
            return {"error": f"unknown status {status!r}; use running, done, failed or all"}
        limit = _clamp_limit(arguments.get("limit"))

        wanted = _STATUS_FILTERS[status]
        if not wanted:
            tasks = await manager.list(limit=limit)
        else:
            found: list[Task] = []
            for one in wanted:
                found.extend(await manager.list(status=one, limit=limit))
            tasks = sorted(found, key=lambda task: task.id, reverse=True)[:limit]
        return {"tasks": [_brief(task) for task in tasks]}

    registry.register(
        "list_tasks",
        "The tasks the subagents are working on or have finished, newest first. Use it when "
        "the user asks what is running or what has come back.",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(_STATUS_FILTERS),
                    "description": "Which tasks to list. 'running' also covers tasks still "
                    "waiting for their turn. Defaults to all.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TASK_LIMIT,
                    "description": f"How many to return, at most {MAX_TASK_LIMIT}. Defaults "
                    f"to {DEFAULT_TASK_LIMIT}, which is about as many as anyone wants read "
                    "out loud.",
                },
            },
            "required": [],
        },
        list_tasks,
    )

    # --- get_task_status / get_task_result ---------------------------------

    async def get_task_status(ctx: ToolContext, arguments: dict) -> dict:
        task = await _get(arguments)
        if isinstance(task, dict):
            return task
        return {
            "task_id": task.id,
            "status": task.status.value,

            "description": task.description,
            "summary": task.summary,
            "error": task.error,
            "line": task.short_status_line(),
        }

    registry.register(
        "get_task_status",
        "How one task is doing, by its task number. Use it when the user asks about a "
        "specific task rather than about everything at once.",
        _TASK_ID_SCHEMA,
        get_task_status,
    )

    async def get_task_result(ctx: ToolContext, arguments: dict) -> dict:
        task = await _get(arguments)
        if isinstance(task, dict):
            return task
        result = {
            "task_id": task.id,
            "status": task.status.value,
            "summary": task.summary,
        }
        excerpt = await _report_excerpt(task)
        if excerpt is not None:
            result["report_excerpt"] = excerpt
        return result

    registry.register(
        "get_task_result",
        "What a finished task actually found, including the start of its written report. "
        "Use it when the user wants more than the one-line summary. Summarise it in a "
        "sentence or two; never read the report out.",
        _TASK_ID_SCHEMA,
        get_task_result,
    )

    # --- mark_reported -----------------------------------------------------

    async def mark_reported(ctx: ToolContext, arguments: dict) -> dict:
        raw = arguments.get("task_ids")
        ids = _task_ids(raw)
        if not ids:
            return {"error": "task_ids must be a list of task numbers, for example [3, 4]"}
        reported = await manager.mark_reported(ids)
        # Ids that were already reported (or never existed) come back missing rather than
        # as an error: the model is working from a spoken conversation, and there is
        # nothing useful it could say to him about either case.
        return {"reported": reported}

    registry.register(
        "mark_reported",
        "Record that you have now told him about tasks that finished. Call it immediately "
        "after you say a result out loud — whether it came from the list of things he had "
        "not heard, from a '[system]' note during the call, or inline from dispatch_task. "
        "Until you call it, those tasks are still waiting to be told and he will hear them "
        "again at the start of the next call. Only pass ids you actually mentioned to him.",
        {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The task numbers you just told him about.",
                }
            },
            "required": ["task_ids"],
        },
        mark_reported,
    )

    # --- recall ------------------------------------------------------------

    async def recall(ctx: ToolContext, arguments: dict) -> dict:
        query = _text(arguments, "query")
        if not query:
            return {"error": "query is required: say what to look for"}
        assert recaller is not None  # only registered when there is one
        hits = await recaller.recall(query, limit=_clamp_recall_limit(arguments.get("limit")))
        if not hits:
            return {"hits": [], "message": RECALL_EMPTY_MESSAGE}
        return {"hits": [hit.as_dict() for hit in hits]}

    if recaller is not None:
        registry.register(
            "recall",
            "Search what was said in earlier calls and what past tasks returned. Use it "
            "whenever he refers to something that already happened — 'what did we decide "
            "about', 'what did I ask you to do with', 'remind me what came of' — before "
            "you either guess or dispatch a task. It searches records, so it finds only "
            "words that were actually said or written: if it comes back with nothing, say "
            "you have nothing on it and offer to put Claude on it.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The distinctive words to look for — a project, a "
                        "person, a thing. Not a full sentence: common words are ignored.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RECALL_LIMIT,
                        "description": f"How many results, at most {MAX_RECALL_LIMIT}. "
                        f"Defaults to {DEFAULT_RECALL_LIMIT}.",
                    },
                },
                "required": ["query"],
            },
            recall,
        )

    # --- send_followup / cancel_task ---------------------------------------

    async def send_followup(ctx: ToolContext, arguments: dict) -> dict:
        task = await _get(arguments)
        if isinstance(task, dict):
            return task
        refusal = _pin_gate(ctx)
        if refusal is not None:
            return refusal
        message = _text(arguments, "message")
        if not message:
            return {"error": "message is required: say what to add to the task"}
        try:
            updated = await manager.followup(task.id, message)
        except KeyError:
            return {"error": f"no task {task.id}"}
        except ValueError as exc:
            return {"error": str(exc)}
        return {"task_id": updated.id, "status": updated.status.value}

    registry.register(
        "send_followup",
        "Add something to a task that is already under way, or ask a finished task for more. "
        "Use it for 'also…' and 'actually, make that…' instead of dispatching a second task. "
        "On the phone, this needs the PIN too.",
        {
            "type": "object",
            "properties": {
                "task_id": _TASK_ID_PROPERTY,
                "message": {
                    "type": "string",
                    "description": "What to tell the subagent, in full sentences.",
                },
            },
            "required": ["task_id", "message"],
        },
        send_followup,
    )

    async def cancel_task(ctx: ToolContext, arguments: dict) -> dict:
        task = await _get(arguments)
        if isinstance(task, dict):
            return task
        refusal = _pin_gate(ctx)
        if refusal is not None:
            return refusal
        try:
            cancelled = await manager.cancel(task.id)
        except KeyError:
            return {"error": f"no task {task.id}"}
        return {"task_id": cancelled.id, "status": cancelled.status.value}

    registry.register(
        "cancel_task",
        "Stop a task that is queued or running. A task that has already finished comes back "
        "unchanged, so say so rather than claiming you stopped it. On the phone, this needs "
        "the PIN too.",
        _TASK_ID_SCHEMA,
        cancel_task,
    )

    # --- list_projects -----------------------------------------------------

    async def list_projects(ctx: ToolContext, arguments: dict) -> dict:
        return {"projects": [name for name, _path in manager.list_projects()]}

    registry.register(
        "list_projects",
        "The names of the projects a coding task can run in. Use it when the user names a "
        "project you do not recognise, and offer the closest match instead of guessing.",
        {"type": "object", "properties": {}, "required": []},
        list_projects,
    )

    # --- request_callback --------------------------------------------------

    async def request_callback(ctx: ToolContext, arguments: dict) -> dict:
        task = await _get(arguments)
        if isinstance(task, dict):
            return task
        if task.status in TERMINAL_STATUSES:
            return {
                "task_id": task.id,
                "status": "already_finished",
                "summary": task.summary,
            }

        number = _text(arguments, "number") or ctx.caller or settings.owner_number or ""
        if not number:
            return {"error": "no number to call back on; ask the user for one"}
        if not _E164_RE.match(number):
            return {"error": f"{number!r} is not a phone number I can call back"}
        # An outbound call is the one thing an unauthorized caller could aim at a stranger,
        # so without the PIN it may only go back to a number we already trust (spec §5).
        if ctx.channel == "phone" and not ctx.authorized:
            known = {ctx.caller, settings.owner_number, *settings.allowed_callers}
            if number not in known:
                log.warning("session %s asked to call an unknown number", ctx.session.session_id)
                return {"status": "refused", "message": CALLBACK_NUMBER_MESSAGE}

        await manager.request_callback(task.id, number, _text(arguments, "note") or None)
        return {"task_id": task.id, "status": "callback_requested"}

    registry.register(
        "request_callback",
        "Arrange for Jarvis to phone the user back when a task finishes, instead of them "
        "waiting on the line. Offer this yourself whenever a task is still running and the "
        "conversation is winding down — do not wait to be asked. Without a number it uses "
        "the number they are calling from, which is the only number an unauthorized caller "
        "may name.",
        {
            "type": "object",
            "properties": {
                "task_id": _TASK_ID_PROPERTY,
                "number": {
                    "type": "string",
                    "description": "The number to call, in full international form such as "
                    "+491701234567. Leave it out to use the number of this call.",
                },
                "note": {
                    "type": "string",
                    "description": "One line of where you left off, for the you that makes "
                    "that call: what he asked for in his own words, anything he decided or "
                    "ruled out, and what he said he wanted next. The call-back is a new "
                    "call and remembers nothing else of this one.",
                },
            },
            "required": ["task_id"],
        },
        request_callback,
    )

    # --- restart_service ---------------------------------------------------

    if restarter is not None:

        async def restart_service(ctx: ToolContext, arguments: dict) -> dict:
            """Restart the service, and let it phone back when it is up (spec §3.3)."""
            refusal = _pin_gate(ctx)
            if refusal is not None:
                return refusal
            # Only ever a number we already trust: the one calling, or the owner's. A
            # restart is not a way to make Jarvis dial a stranger.
            return await restarter.request(
                reason=_text(arguments, "reason"),
                number=ctx.caller if ctx.channel == "phone" else None,
                origin_channel=ctx.channel,
                origin_session_id=ctx.session.session_id,
                task_id=_task_id(arguments),
            )

        registry.register(
            "restart_service",
            "Restart Jarvis itself — the service behind this call — when he asks for one, "
            "or when work he asked for has changed Jarvis's own code and only a restart "
            "loads it. The restart drops this call, so it waits until the call has ended "
            "and then rings him back by itself to say whether it worked; the answer tells "
            "you what to say. Never reach for it to fix something you were not asked to fix.",
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why it is being restarted, in a few words — he "
                        "hears this back on the confirmation call.",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "The task whose change this restart is loading, if "
                        "it is loading one. Pass it: the confirmation then checks that the "
                        "change is actually running rather than only that Jarvis came back.",
                    },
                },
                "required": [],
            },
            restart_service,
        )

    # --- submit_pin / end_session ------------------------------------------

    # --- approvals ---------------------------------------------------------

    async def list_pending_approvals(ctx: ToolContext, arguments: dict) -> dict:
        assert approvals is not None  # only registered when there is one
        waiting = approvals.pending_requests()
        if not waiting:
            return {"status": "none", "message": APPROVAL_NONE_MESSAGE}
        return {"status": "waiting", "requests": waiting}

    async def answer_approval(ctx: ToolContext, arguments: dict) -> dict:
        """Offer the keypad menu for one pending prompt. It answers nothing by itself.

        Two gates before the menu is even read out: the PIN, exactly as for dispatching
        work — this is a strictly larger capability, so it gets at least the same gate —
        and the phone, because the keypad is where the answer has to come from.
        """
        assert approvals is not None  # only registered when there is one
        refusal = _pin_gate(ctx)
        if refusal is not None:
            return refusal
        if ctx.channel != "phone":
            return {"status": "phone_only", "message": APPROVAL_PHONE_ONLY_MESSAGE}
        request_id = _small_int(arguments.get("request_id"))
        if request_id is None:
            return {"error": "request_id must be a request number, for example 1"}
        answer = approvals.arm(request_id, ctx.session.session_id)
        if answer.get("status") == "awaiting_keypad":
            answer["message"] = APPROVAL_KEYPAD_MESSAGE
        return answer

    if approvals is not None:
        registry.register(
            "list_pending_approvals",
            "The prompts Claude Code is waiting on, on his screen. Call it when he asks "
            "what is waiting, or when a call opened because something was.",
            {"type": "object", "properties": {}},
            list_pending_approvals,
        )
        registry.register(
            "answer_approval",
            "Start answering one prompt Claude Code is waiting on. It does not answer "
            "anything: it hands you back the keypad menu for that request, which you read "
            "out, and he decides by pressing a key. Never tell him it is done until the "
            "machine says so — a spoken yes is not an answer, and you must never choose "
            "for him. Needs the PIN and a phone call.",
            {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "integer",
                        "description": "The request number, from the call's opening "
                        "context or list_pending_approvals.",
                    }
                },
                "required": ["request_id"],
            },
            answer_approval,
        )

    # --- submit_pin --------------------------------------------------------

    async def submit_pin(ctx: ToolContext, arguments: dict) -> dict:
        """Hand a spoken PIN to the session; only it ever sees the digits."""
        return await ctx.session.submit_pin(str(arguments.get("pin") or ""))

    registry.register(
        "submit_pin",
        "Check the PIN the caller just said, to unlock dispatching work on the phone. "
        "Pass the digits exactly as you heard them, with nothing else. Never say them back "
        "out loud. The answer is authorized, invalid (with the attempts left) or locked.",
        {
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "The digits the caller said."},
            },
            "required": ["pin"],
        },
        submit_pin,
    )

    async def end_session(ctx: ToolContext, arguments: dict) -> dict:
        ctx.session.request_end("user")
        return {"status": "ending", "message": ENDING_MESSAGE}

    registry.register(
        "end_session",
        "Hang up. Say your goodbye first and call this straight afterwards, once the user "
        "has said goodbye or has nothing more to ask: nothing you say after this call is "
        "heard. Never call it while a question is still open.",
        {"type": "object", "properties": {}, "required": []},
        end_session,
    )
