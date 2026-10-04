"""Everything about tasks: dispatching one, watching it, and reaching back into it.

Ten tools, and what each needs on the phone is reading versus acting (`builtin_common`).
`dispatch_task`, `send_followup` and `cancel_task` need the PIN because reaching into a
task opens the very same `bypassPermissions` subagent that dispatching one would;
`request_callback` because it leaves something behind that outlives the call; `recall`
because it reads every raw transcript there is, which is a different thing from reading
today's news. `list_tasks`, `get_task_status`, `get_task_result` and `list_projects` are
the caller asking out loud for a piece of the briefing the prompt already carries, so they
follow it (`read_gate`, `BRIEFING_BEFORE_PIN`).

`mark_reported` is the load-bearing one and the easiest to mistake for bookkeeping. It is
the *only* thing that stamps `Task.reported_at`, and `reported_at` is the only record that
Keryx actually said a result out loud: `announced` and `sms_sent` say a delivery was
attempted, and neither survives a call they missed. Until it is stamped, the task rides the
top of the next call. Nothing but the voice model, having spoken, may stamp it.
"""

from collections.abc import Sequence

from keryx.agents.registry import agent_for_model
from keryx.config import Settings
from keryx.continuity.recall import DEFAULT_LIMIT as DEFAULT_RECALL_LIMIT
from keryx.continuity.recall import MAX_LIMIT as MAX_RECALL_LIMIT
from keryx.continuity.recall import Recaller
from keryx.inline_waits import InlineWaits
from keryx.tasks.manager import (
    TERMINAL_STATUSES,
    TaskLimitError,
    TaskManager,
    UnknownProjectError,
)
from keryx.tasks.models import Task, TaskStatus
from keryx.tools.builtin_common import (
    _E164_RE,
    _STATUS_FILTERS,
    _TASK_ID_PROPERTY,
    _TASK_ID_SCHEMA,
    AGENT_UNAVAILABLE_MESSAGE,
    BACKENDS,
    CALLBACK_ALREADY_DONE_MESSAGE,
    CALLBACK_OWNER_ONLY_MESSAGE,
    CALLBACK_SET_MESSAGE,
    CONTINUE_REPORTING_MESSAGE,
    DEFAULT_TASK_LIMIT,
    MAX_TASK_LIMIT,
    MODEL_AGENT_CONFLICT_MESSAGE,
    RECALL_EMPTY_MESSAGE,
    REPORTED_MESSAGE,
    STILL_RUNNING_MESSAGE,
    WAIT_DESCRIPTION,
    _brief,
    _clamp_limit,
    _clamp_recall_limit,
    _clamp_wait,
    _report_excerpt,
    _task_ids,
    _text,
    agent_description,
    get_task,
    model_description,
    pin_gate,
    possession_gate,
    read_gate,
)
from keryx.tools.registry import ToolContext, ToolRegistry
from keryx.trust import TrustLevel


def register_task_tools(
    registry: ToolRegistry,
    *,
    manager: TaskManager,
    settings: Settings,
    inline_waits: InlineWaits,
    recaller: Recaller | None = None,
    agents: Sequence[str] | None = None,
) -> None:
    """Register the task tools. `recall` needs a `Recaller`; the rest are unconditional.

    `agents` is what `dispatch_task` may name, the default first
    (`keryx.agents.registry.offered_agents`); None is the default alone. With one, the
    tool has no `agent` parameter at all, and reads exactly as it did before there was a
    choice.
    """
    offered = list(agents or [settings.agent_backend])
    default_agent = offered[0]

    # --- dispatch_task -----------------------------------------------------

    def choose_agent(arguments: dict) -> tuple[str | None, dict | None]:
        """The agent to run on, or the refusal to hand back: `(agent, None)` / `(None, error)`.

        A model name picks its own agent ("opus" is Claude's), so the model rarely says
        both; when it does and they disagree, nothing is guessed.
        """
        named = (_text(arguments, "agent") or "").lower() or None
        model = _text(arguments, "model")
        implied = agent_for_model(model)
        if named and implied and named != implied:
            return None, {
                "error": MODEL_AGENT_CONFLICT_MESSAGE.format(
                    model=model,
                    model_agent=BACKENDS[implied].spoken_name,
                    agent=BACKENDS[named].spoken_name if named in BACKENDS else named,
                )
            }
        agent = named or implied or default_agent
        if agent not in offered:
            label = BACKENDS[agent].spoken_name if agent in BACKENDS else agent
            return None, {
                "error": AGENT_UNAVAILABLE_MESSAGE.format(
                    agent=label, default=BACKENDS[default_agent].spoken_name
                )
            }
        return agent, None


    async def dispatch_task(ctx: ToolContext, arguments: dict) -> dict:
        refusal = pin_gate(ctx, settings)
        if refusal is not None:
            return refusal

        description = _text(arguments, "description")
        if not description:
            return {"error": "description is required: say what the subagent should do"}
        agent, refusal = choose_agent(arguments)
        if refusal is not None:
            return refusal

        try:
            task = await manager.dispatch(
                description,
                project=_text(arguments, "project") or None,
                model=_text(arguments, "model") or None,
                agent=agent,
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
            # into this session what the tool result below is about to say.
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

    worker = BACKENDS[default_agent].spoken_name
    dispatch_properties: dict = {
        "description": {
            "type": "string",
            "description": "What the subagent should do, in full sentences. It cannot "
            "hear the conversation, so include every detail that matters.",
        },
        "project": {
            "type": "string",
            "description": "The name of the project to work in. Optional: leave it "
            "out when they did not name one and the task starts in their projects "
            "folder, where the subagent finds the repo itself. Use list_projects "
            "only when they ask what exists.",
        },
        "wait_seconds": {
            "type": "number",
            "minimum": 0,
            "maximum": settings.dispatch_wait_max_seconds,
            "description": WAIT_DESCRIPTION,
        },
    }
    # Only agents with spoken model names offer a choice; the local model is the one model
    # its server was set up with, so naming one is not something to ask the caller for.
    named = [name for name in offered if BACKENDS[name].models]
    if named:
        dispatch_properties["model"] = {
            "type": "string",
            "enum": [alias for name in named for alias in BACKENDS[name].models],
            "description": model_description(named),
        }
    if len(offered) > 1:
        dispatch_properties["agent"] = {
            "type": "string",
            "enum": offered,
            "description": agent_description(offered),
        }
    registry.register(
        "dispatch_task",
        f"Hand a piece of work to {worker} and get back a task number. Use it for anything you "
        "cannot answer yourself in a sentence or two, and for anything to do with code the "
        "moment you recognise it — do not ask the caller to confirm the request first, and "
        f"do not interview them about details {worker} can work out for itself. On the phone, "
        "every dispatch comes back as pin_required until the caller has given the PIN.",
        {"type": "object", "properties": dispatch_properties, "required": ["description"]},
        dispatch_task,
    )

    # --- list_tasks --------------------------------------------------------

    async def list_tasks(ctx: ToolContext, arguments: dict) -> dict:
        # A read over what the briefing already named (`read_gate`).
        if (refusal := read_gate(ctx, settings)) is not None:
            return refusal
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
        if (refusal := read_gate(ctx, settings)) is not None:
            return refusal
        task = await get_task(manager, arguments)
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
        if (refusal := read_gate(ctx, settings)) is not None:
            return refusal
        task = await get_task(manager, arguments)
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

    def _still_to_say(arguments: dict) -> bool:
        # Missing or not a boolean is "still to say": a turn too many is a sentence said
        # twice, where a turn too few is the call going quiet on news they never heard.
        return arguments.get("still_to_say") is not False

    async def mark_reported(ctx: ToolContext, arguments: dict) -> dict:
        ids = _task_ids(arguments.get("task_ids"))
        # Stamping a task takes it out of the next call's digest, so a call that has proved
        # nothing may stamp only what it actually said out loud: the digest it opened with,
        # or the result a call Keryx placed opened by saying. At POSSESSION, anything —
        # whoever answered is holding the owner's own phone.
        refusal = pin_gate(ctx, settings)
        if refusal is not None and ctx.trust < TrustLevel.POSSESSION:
            ids = [task_id for task_id in ids if task_id in ctx.session.reportable_task_ids]
            if not ids:
                return refusal
        if not ids:
            return {"error": "task_ids must be a list of task numbers, for example [3, 4]"}
        reported = await manager.mark_reported(ids)
        # Ids that were already reported (or never existed) come back missing rather than
        # as an error: the model is working from a spoken conversation, and there is
        # nothing useful it could say to them about either case.
        still_to_say = _still_to_say(arguments)
        message = CONTINUE_REPORTING_MESSAGE if still_to_say else REPORTED_MESSAGE
        return {"reported": reported, "message": message}

    registry.register(
        "mark_reported",
        "Record that you are telling them about tasks that finished — whether from the list of "
        "things they had not heard, from a '[system]' note during the call, or inline from "
        "dispatch_task. Until you call it, those tasks are still waiting to be told and the "
        "owner will hear them again at the start of the next call. Only pass ids you have "
        "told them about, or are telling them about in this turn. Set still_to_say to true "
        "if you have not finished — the result itself, or other news, is still to come — and "
        "the turn comes straight back to you to say it. Set it to false only once everything "
        "has been said: then nothing more is generated, and it is their turn.",
        {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The task numbers you are telling them about.",
                },
                "still_to_say": {
                    "type": "boolean",
                    "description": "True if you have not finished yet: the result itself, "
                    "or other news, is still to come in this turn. False once you have said "
                    "everything.",
                },
            },
            "required": ["task_ids", "still_to_say"],
        },
        mark_reported,
        # Silent only once everything has been said: a turn generated over its answer then
        # is the result said a second time. Called before the result — the habit on almost
        # every call-back, 2026-09-29 — silence left the call quiet with the news half told
        # until the owner asked for it, so that call gets its turn back.
        silent=lambda arguments: not _still_to_say(arguments),
    )

    # --- recall ------------------------------------------------------------

    async def recall(ctx: ToolContext, arguments: dict) -> dict:
        # `pin_gate`, not `read_gate`, and the difference is the point. The briefing is a
        # bounded, curated page the owner can read with `keryx memory` and prune, and it
        # is the same whatever the caller says. This is an unbounded query the *caller*
        # steers over every raw transcript Keryx has ever written — a different quantity
        # of exposure, and the one thing on the phone a spoofer could actually mine.
        if (refusal := pin_gate(ctx, settings)) is not None:
            return refusal
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
            "whenever they refer to something that already happened — 'what did we decide "
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
        # The one tool a call Keryx placed exists for: Claude came back with a question,
        # this is the answer going the other way. One keypress stands in for the PIN, and
        # rules out the answering machine that picked up (`possession_gate`).
        if (refusal := possession_gate(ctx, settings)) is not None:
            return refusal
        task = await get_task(manager, arguments)
        if isinstance(task, dict):
            return task
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
        if (refusal := pin_gate(ctx, settings)) is not None:
            return refusal
        task = await get_task(manager, arguments)
        if isinstance(task, dict):
            return task
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
        if (refusal := read_gate(ctx, settings)) is not None:
            return refusal
        return {"projects": [name for name, _path in manager.list_projects()]}

    registry.register(
        "list_projects",
        "The names of the projects a task can run in. Use it when the user names a "
        "project you do not recognise, and offer the closest match instead of guessing.",
        {"type": "object", "properties": {}, "required": []},
        list_projects,
    )

    # --- request_callback --------------------------------------------------

    async def request_callback(ctx: ToolContext, arguments: dict) -> dict:
        # Its note opens their real call-back and the call goes out on their account, so
        # below FULL it takes possession plus a keypress — and may only ever ring the
        # number Keryx already dialled.
        if (refusal := possession_gate(ctx, settings)) is not None:
            return refusal
        task = await get_task(manager, arguments)
        if isinstance(task, dict):
            return task
        if task.status in TERMINAL_STATUSES:
            return {
                "task_id": task.id,
                "status": "already_finished",
                "summary": task.summary,
                "message": CALLBACK_ALREADY_DONE_MESSAGE,
            }

        number = _text(arguments, "number") or ctx.caller or settings.owner_number or ""
        if not number:
            return {"error": "no number to call back on; ask the user for one"}
        if not _E164_RE.match(number):
            return {"error": f"{number!r} is not a phone number I can call back"}
        if ctx.trust is not TrustLevel.FULL and number not in settings.owner_numbers:
            # Possession is a fact about the owner's own phones, all of which they
            # configured. A number chosen on the call is a new decision, and the PIN is
            # what makes one.
            return {"status": "refused", "message": CALLBACK_OWNER_ONLY_MESSAGE}

        await manager.request_callback(task.id, number, _text(arguments, "note") or None)
        return {
            "task_id": task.id,
            "status": "callback_requested",
            "message": CALLBACK_SET_MESSAGE,
        }

    registry.register(
        "request_callback",
        "Arrange to phone the user back yourself when a task finishes, instead of them "
        "waiting on the line. Offer this yourself whenever a task is still running and the "
        "conversation is winding down — do not wait to be asked. It returns at once, so do "
        "not say you are setting it up first: once they say yes, call it and then tell them "
        "in one clause that you will ring them. Without a number it uses the number they are "
        "calling from. On the phone, this needs the PIN too.",
        {
            "type": "object",
            "properties": {
                "task_id": _TASK_ID_PROPERTY,
                "number": {
                    "type": "string",
                    "description": "The number to call, in full international form such as "
                    "+15551234567. Leave it out to use the number of this call.",
                },
                "note": {
                    "type": "string",
                    "description": "One line of where you left off, for the you that makes "
                    "that call: what they asked for in their own words, anything they decided or "
                    "ruled out, and what they said they wanted next. The call-back is a new "
                    "call and remembers nothing else of this one.",
                },
            },
            "required": ["task_id"],
        },
        request_callback,
    )
