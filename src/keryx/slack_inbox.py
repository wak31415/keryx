"""What the owner asks of Keryx on Slack, acted on.

The other direction from `notify`: there Keryx carries a result out, here the owner sends
something in. One `SocketModeListener` runs per workspace at the `converse` level or above
(`integrations.slack_socket`), started by `keryx serve` and by nothing else, and every
event it hears comes to `SlackInbox.handle`.

**Keryx acts only when the owner mentions it** (the owner's ruling of 2026-11-23). A
message is a `Request` when it is from the one member id the workspace's settings name as
the owner *and* its text mentions the app's own bot user (`<@…>`, the id `auth.test` gave
at connect). That holds everywhere: in the owner's direct messages with the app, in a
reply under a task's report, and at `read` in any conversation the app has been added to.
No mention, no action: no reaction, no sentence. Anyone else's mention is dropped the same.

**Where a request may be typed, by level.** At `converse`, only the direct-message
conversation with the app (`message.im`). At `read`, also a conversation the app was added
to, through `app_mention`: the one message that mentions it, and nothing else of that
conversation, which Keryx is never sent.

**Two things a request may do, both without the phone PIN** (SECURITY.md has what that
costs). In a thread that is a task's, it is a follow-up to that task: what `send_followup`
does on a call, counted against the daily cap like a dispatch, because no PIN and no
keypress stand in front of it here. Anywhere else it starts new work: an ordinary task —
no task kind, the same cap, the same agent — dispatched with the request's text and
nothing more.

**A request is the owner's words; everything else on Slack is context.** This module
never reads context: not the rest of the thread, not the message a reply is under. A task
is handed the request and a pointer to where it was typed (`SlackThread`), and a subagent
that needs the thread reads it itself, fenced, with `keryx slack` (`keryx.slack_context`).

**Asked in one place, answered in another.** A request typed in a shared conversation is
acknowledged there with a reaction and nothing else. Its report goes to the owner's direct
messages (`plugins.slack.report_route`), and so does a refusal: a report can hold anything
from their machine, and a shared conversation is not theirs alone.

**What is acted on is decided in one function, `owner_request`, and it fails closed.**
Everything it needs is on file or it refuses: the workspace's id, the owner's member id,
the app's own. The event must say whose authorization delivered it. Because the real
payload has only ever been read in Slack's documentation, every event that is not acted on
writes one log line with a reason and nothing else — never the text, the member or the
conversation — so a live test shows at once which check refused. The settings are read
again for every message, so lowering a workspace to `announce`, or turning the plugin off,
stops Keryx acting at once.

**Each message is acted on once.** Slack sends an event again when its acknowledgement was
lost, and promises no order. A message is a repeat when its task remembers acting on it
(`Task.slack_seen`, the last several, which survives a restart) or this process has seen
it; never merely because a later one arrived first.

On Slack Keryx is brief: a reaction on the message it acted on (a reaction notifies
nobody), then the report when the work is done. A refusal is one sentence. The service
speaks as Keryx here, never by the assistant's name. What a message says is never logged.
"""

import asyncio
import contextlib
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keryx.config import Settings
from keryx.integrations import slack
from keryx.integrations.slack import Access, SlackPoster, SlackResult, SlackWebApi
from keryx.integrations.slack_socket import SocketModeListener
from keryx.plugins import slack as plugin
from keryx.plugins.slack import SLACK_CHANNEL, SlackWorkspace
from keryx.tasks.manager import TaskLimitError, TaskManager
from keryx.tasks.models import SlackThread, Task, TaskStatus

log = logging.getLogger("keryx.slack.inbox")

#: How many messages this process remembers having seen, to drop one delivered twice.
MAX_SEEN = 512

NOTHING_TO_RESUME = "Keryx has no task in this thread to resume; mention it again to start one."
CANCELLED = "Task {task_id} was cancelled, so there is nothing here to resume."
STARTING = "Task {task_id} is only just starting, so send that again in a moment."
NOT_PASSED_ON = "Keryx could not confirm that reached task {task_id}."
CAP_REACHED = "Keryx has reached today's task limit ({cap}), so this was not started."
NOT_STARTED = "Keryx could not start that."
TEXT_ONLY = "Keryx reads text only: send that again as a message without the file."
NOTHING_ASKED = "Keryx was mentioned with nothing to do: say what you want after the mention."
#: Added to a sentence that has to be said somewhere other than where the request was typed.
ELSEWHERE = " (about {link})"

#: The events a request may arrive as, and the subtypes of a message that may be one: an
#: ordinary message has none, a reply "also sent to the channel" is `thread_broadcast`, and
#: one with a file is `file_share`, which is answered and not acted on. Every other
#: subtype (an edit, a deletion, a join) is nothing the owner asked.
MESSAGE, MENTION = "message", "app_mention"
_SPACES = re.compile(r"[ \t]{2,}")
SUBTYPES = ("", "thread_broadcast", "file_share")


@dataclass(frozen=True)
class Request:
    """What the owner asked, in their own words, and where they typed it.

    The only text from Slack that is ever an instruction. `text` is the message with the
    mention of the app taken out; nothing of the conversation around it is in here.
    """

    text: str
    channel: str
    ts: str
    #: The message its thread hangs from, when it is a reply; None for one of its own.
    thread_ts: str | None = None
    #: Typed in a conversation that is not the owner's direct messages with the app.
    shared: bool = False
    #: It came with a file, which Keryx does not read.
    files: bool = False

    @property
    def root(self) -> str:
        """The message the thread it belongs to hangs from: its own, when it starts one."""
        return self.thread_ts or self.ts


@dataclass(frozen=True)
class Dropped:
    """An event that is nobody's request, and the one word for why."""

    reason: str


def envelope_problem(payload: Any) -> str | None:
    """Why `payload` cannot be a request, from its shape alone; None when it might be.

    Asked before any setting is read: the app's own reports come back as events too, and
    each of those should cost nothing.
    """
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return "not_an_event"
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") not in (MESSAGE, MENTION):
        return "not_a_message"
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return "bot_message"
    if (event.get("subtype") or "") not in SUBTYPES:
        return "subtype"
    return None


def owner_request(payload: Any, workspace: SlackWorkspace) -> Request | Dropped:
    """The owner's request in `payload`, or why there is none. The one place it is decided.

    Fails closed: a workspace with no id, owner or bot member on file accepts nothing, and
    so does an event that does not say whose authorization delivered it.
    """
    if problem := envelope_problem(payload):
        return Dropped(problem)
    if not (workspace.team_id and workspace.owner_id and workspace.bot_user_id):
        return Dropped("not_on_file")
    if payload.get("team_id") != workspace.team_id:
        return Dropped("other_team")
    # Delivered to the app as its bot. A user-level subscription, which the manifest
    # never asks for, would deliver every direct message the owner has with anybody.
    authorizations = payload.get("authorizations")
    if not (isinstance(authorizations, list) and authorizations):
        return Dropped("no_authorizations")
    if not any(
        isinstance(each, dict)
        and (each.get("is_bot") is True or each.get("user_id") == workspace.bot_user_id)
        for each in authorizations
    ):
        return Dropped("not_for_the_bot")
    event = payload["event"]
    if event.get("user") != workspace.owner_id:
        return Dropped("not_the_owner")
    shared = event["type"] == MENTION
    if shared and not workspace.access.reaches(Access.READ):
        return Dropped("mention_below_read")
    if not shared and event.get("channel_type") != "im":
        return Dropped("not_a_direct_message")
    channel, ts, thread_ts = event.get("channel"), event.get("ts"), event.get("thread_ts")
    if not (isinstance(channel, str) and channel and slack.is_ts(ts)):
        return Dropped("malformed")
    if thread_ts is not None and not slack.is_ts(thread_ts):
        return Dropped("malformed")
    text = event.get("text")
    text = text if isinstance(text, str) else ""
    mention = slack.mention(workspace.bot_user_id)
    if not mention.search(text):
        return Dropped("no_mention")
    return Request(
        # The mention is not part of what was asked; the gap it leaves is closed.
        text=_SPACES.sub(" ", slack.plain_text(mention.sub(" ", text))),
        channel=channel,
        ts=ts,
        thread_ts=thread_ts if thread_ts and thread_ts != ts else None,
        shared=shared,
        files=event.get("subtype") == "file_share" or bool(event.get("files")),
    )


@dataclass(frozen=True)
class InboxApi:
    """What the inbox does on Slack beside listening, replaced in the tests."""

    react: Callable[[str, str, str], SlackResult] = slack.add_reaction
    poster: Callable[[str, str], SlackPoster] = SlackWebApi
    listener: Callable[..., Any] = SocketModeListener


class SlackInbox:
    """Listens to each workspace that may be heard, and acts on what the owner asks."""

    def __init__(
        self, settings: Settings, manager: TaskManager, *, api: InboxApi | None = None
    ) -> None:
        self._settings = settings
        self._manager = manager
        self._api = api or InboxApi()
        self._listeners: dict[str, asyncio.Task] = {}
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def start(self) -> list[str]:
        """Start one listener for each workspace at `converse` or above; their names.

        Idempotent, and never raises: a plugin file that does not parse, or a workspace
        missing a token, is a workspace not listened to, and `doctor` says which.
        """
        try:
            found = [each for each in plugin.connected(self._settings) if each.listens]
        except Exception:
            log.exception("could not read the Slack workspaces; none is listened to")
            return []
        for each in found:
            if each.name in self._listeners:
                continue
            listener = self._api.listener(
                each.name, each.token(slack.APP), lambda payload, name=each.name:
                self.handle(name, payload)
            )
            self._listeners[each.name] = asyncio.create_task(
                listener.run(), name=f"slack-listener-{each.name}"
            )
            log.info("Slack: listening for the owner's messages in %s", each.name)
        return [each.name for each in found]

    async def stop(self) -> None:
        """Stop every listener. Idempotent."""
        listeners, self._listeners = list(self._listeners.values()), {}
        for listener in listeners:
            listener.cancel()
        for listener in listeners:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener

    # --- one event -----------------------------------------------------------------------

    async def handle(self, name: str, payload: Any) -> None:
        """Act on one event from workspace `name`'s app, if it is the owner's request."""
        if problem := envelope_problem(payload):
            return _dropped(name, problem)
        # As the settings stand now: a level lowered since startup stops this at once.
        workspace = plugin.workspace(self._settings, name)
        if workspace is None or not workspace.listens:
            return _dropped(name, "not_listening")
        request = owner_request(payload, workspace)
        if isinstance(request, Dropped):
            return _dropped(name, request.reason)
        if not self._first_time(name, request):
            return _dropped(name, "seen")
        thread = SlackThread(name, request.channel, request.root, shared=request.shared,
                             asked_at=request.ts)
        task = await self._manager.task_in_slack_thread(thread)
        if task is not None and request.ts in (task.slack_seen or "").split():
            return _dropped(name, "already_acted_on")
        if request.files:
            await self._say(workspace, request, TEXT_ONLY)
        elif not request.text:
            await self._say(workspace, request, NOTHING_ASKED)
        elif task is None:
            # No task lives in this thread, or it is no thread at all: new work, whose
            # thread is the one the request was typed in.
            await self._start(workspace, request, thread)
        else:
            await self._resume(workspace, request, task)

    def _first_time(self, name: str, request: Request) -> bool:
        key = (name, request.channel, request.ts)
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)
        return True

    async def _start(
        self, workspace: SlackWorkspace, request: Request, thread: SlackThread
    ) -> None:
        """Dispatch the request as a task: its text, and where it was typed, nothing else."""
        try:
            task = await self._manager.dispatch(
                request.text, origin_channel=SLACK_CHANNEL, origin_caller=None, slack=thread
            )
        except TaskLimitError as limit:
            await self._say(workspace, request, CAP_REACHED.format(cap=limit.cap))
            return
        except Exception:
            log.exception("could not start a task from a Slack message in %s", workspace.name)
            await self._say(workspace, request, NOT_STARTED)
            return
        log.info("Slack: task %s started from a message in %s", task.id, workspace.name)
        await self._acknowledge(workspace, request)

    async def _resume(self, workspace: SlackWorkspace, request: Request, task: Task) -> None:
        """Hand the request to the task its thread belongs to, as a follow-up."""
        if task.status is TaskStatus.CANCELLED:
            await self._say(workspace, request, CANCELLED.format(task_id=task.id))
            return
        try:
            await self._manager.followup_from_slack(task.id, request.text, request.ts)
        except TaskLimitError as limit:
            await self._say(workspace, request, CAP_REACHED.format(cap=limit.cap))
            return
        except KeyError:  # deleted since it was looked up
            await self._say(workspace, request, NOTHING_TO_RESUME)
            return
        except ValueError:
            # Not taken: its subagent is not open yet, or it was cancelled this instant.
            now = await self._manager.get(task.id)
            gone = now is not None and now.status is TaskStatus.CANCELLED
            sentence = CANCELLED if gone else STARTING
            await self._say(workspace, request, sentence.format(task_id=task.id))
            return
        except Exception:
            # A steer that failed for real may have landed, so it is not sent a second way.
            log.exception("a follow-up from Slack to task %s failed", task.id)
            await self._say(workspace, request, NOT_PASSED_ON.format(task_id=task.id))
            return
        log.info("Slack: a reply in %s went to task %s as a follow-up", workspace.name, task.id)
        await self._acknowledge(workspace, request)

    # --- what Keryx says back ------------------------------------------------------------

    async def _acknowledge(self, workspace: SlackWorkspace, request: Request) -> None:
        """A reaction on the message that was acted on. Its failure costs nothing."""
        try:
            result = await asyncio.to_thread(
                self._api.react, workspace.token(slack.BOT), request.channel, request.ts
            )
            if not result.ok:
                log.warning("could not acknowledge a Slack message in %s: %s", workspace.name,
                            result.problem)
        except Exception:
            log.exception("could not acknowledge a Slack message in %s", workspace.name)

    async def _say(self, workspace: SlackWorkspace, request: Request, sentence: str) -> None:
        """One sentence: in the request's thread, or for one typed in a shared
        conversation, in the owner's direct messages. Never raises."""
        try:
            if request.shared:
                link = slack.permalink(workspace.url, request.channel, request.ts)
                poster = self._api.poster(workspace.token(slack.BOT), workspace.owner_id)
                await poster.post(sentence + (ELSEWHERE.format(link=link) if link else ""))
            else:
                poster = self._api.poster(workspace.token(slack.BOT), request.channel)
                await poster.post(sentence, thread_ts=request.root)
        except Exception:
            log.exception("could not answer a Slack message in %s", workspace.name)


def _dropped(name: str, reason: str) -> None:
    """The one line an event that was not acted on leaves: where, and a reason. Never
    what it said, who sent it or which conversation it was in."""
    log.info("Slack: an event in %s was not acted on (%s)", name, reason)
