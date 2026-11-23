"""What the owner types to their Keryx app on Slack, acted on.

The other direction from `notify`: there Keryx carries a result out, here the owner sends
something in. One `SocketModeListener` runs per workspace at the `converse` level or above
(`integrations.slack_socket`), started by `keryx serve` and by nothing else, and every
event it hears comes to `SlackInbox.handle`.

**Two things a message may do, and both without the phone PIN** (the owner's ruling of
2026-11-23; SECURITY.md has what it costs):

- A reply in a task's thread is a follow-up to that task: the same thing `send_followup`
  does on a call. A task's thread is the message it was asked for in, or the first report
  Keryx posted for it (`Task.slack_thread_ts`).
- A message outside any thread starts new work: it is dispatched as a task like any
  other — no task kind, the same daily cap, the same agent — whose thread is that message,
  and the report is posted there.

**What is acted on is narrow, and everything else is dropped without a word.** A message
event, with no subtype at all (an edit, a deletion, a file, a join are all subtypes), in
the direct-message conversation with the app (`channel_type == "im"`), not posted by a
bot, from the one member id the workspace's settings name as the owner, delivered to the
app as its bot. The settings are read again for every message, so lowering a workspace to
`announce`, or turning the plugin off, stops Keryx acting at once.

**Each message is acted on once.** Slack sends an event again when its acknowledgement was
lost. A message at or before the latest one its task has acted on (`Task.slack_last_ts`,
which survives a restart) is that, and so is one this process has already seen.

On Slack Keryx is brief: a reaction on the message it acted on (a reaction notifies
nobody), then the report in the thread when the work is done. A refusal is one sentence
in the thread. The service speaks as Keryx here, never by the assistant's name. What a
message says is never logged.
"""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keryx.config import Settings
from keryx.integrations import slack
from keryx.integrations.slack import SlackPoster, SlackResult, SlackWebApi
from keryx.integrations.slack_socket import SocketModeListener
from keryx.plugins import slack as plugin
from keryx.plugins.slack import SLACK_CHANNEL, SlackWorkspace
from keryx.tasks.manager import TaskLimitError, TaskManager
from keryx.tasks.models import SlackThread, Task, TaskStatus

log = logging.getLogger("keryx.slack.inbox")

#: How many messages this process remembers having seen, to drop one delivered twice.
MAX_SEEN = 512

NOTHING_TO_RESUME = (
    "Keryx has no task in this thread to resume; a message outside a thread starts new work."
)
CANCELLED = "Task {task_id} was cancelled, so there is nothing here to resume."
STARTING = "Task {task_id} is only just starting, so send that again in a moment."
NOT_PASSED_ON = "Keryx could not confirm that reached task {task_id}."
CAP_REACHED = "Keryx has reached today's task limit ({cap}), so this was not started."
NOT_STARTED = "Keryx could not start that."


@dataclass(frozen=True)
class Message:
    """One message the owner typed to the app: where, when, and what it says."""

    channel: str
    ts: str
    #: The message its thread hangs from, when it is a reply; None for one of its own.
    thread_ts: str | None
    text: str


def owner_message(payload: Any, workspace: SlackWorkspace) -> Message | None:
    """The message in `payload`, when it is one the owner typed to the app; else None.

    Everything the module's docstring lists is checked here and nowhere else.
    """
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return None
    if workspace.team_id and payload.get("team_id") != workspace.team_id:
        return None
    # Delivered to the app as its bot. A user-level subscription, which the manifest
    # never asks for, would deliver every direct message the owner has with anybody.
    authorizations = payload.get("authorizations")
    if isinstance(authorizations, list) and authorizations and not any(
        isinstance(each, dict) and each.get("is_bot") is True for each in authorizations
    ):
        return None
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("subtype") or event.get("bot_id") or event.get("channel_type") != "im":
        return None
    if not workspace.owner_id or event.get("user") != workspace.owner_id:
        return None
    channel, ts, thread_ts = event.get("channel"), event.get("ts"), event.get("thread_ts")
    if not (isinstance(channel, str) and channel and slack.is_ts(ts)):
        return None
    if thread_ts is not None and not slack.is_ts(thread_ts):
        return None
    text = event.get("text")
    text = slack.plain_text(text) if isinstance(text, str) else ""
    if not text:
        return None
    return Message(channel, ts, thread_ts if thread_ts and thread_ts != ts else None, text)


@dataclass(frozen=True)
class InboxApi:
    """What the inbox does on Slack beside listening, replaced in the tests."""

    react: Callable[[str, str, str], SlackResult] = slack.add_reaction
    poster: Callable[[str, str], SlackPoster] = SlackWebApi
    listener: Callable[..., Any] = SocketModeListener


class SlackInbox:
    """Listens to each workspace that may be heard, and acts on what the owner types."""

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
        """Act on one event from workspace `name`'s app, if it is the owner's message."""
        # As the settings stand now: a level lowered since startup stops this at once.
        workspace = plugin.workspace(self._settings, name)
        if workspace is None or not workspace.listens:
            return
        message = owner_message(payload, workspace)
        if message is None or not self._first_time(name, message):
            return
        thread = SlackThread(name, message.channel, message.thread_ts or message.ts)
        task = await self._manager.task_in_slack_thread(thread)
        if task is not None and _heard_already(task, message):
            return
        if message.thread_ts is None:
            # A message of its own. A task already hanging from it is the same message
            # delivered again after a restart, which `_heard_already` has just caught.
            await self._start(workspace, message, thread)
        elif task is None:
            await self._say(workspace, message, NOTHING_TO_RESUME)
        else:
            await self._resume(workspace, message, task)

    def _first_time(self, name: str, message: Message) -> bool:
        key = (name, message.channel, message.ts)
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)
        return True

    async def _start(
        self, workspace: SlackWorkspace, message: Message, thread: SlackThread
    ) -> None:
        """Dispatch the message as a task whose thread is the message itself."""
        try:
            task = await self._manager.dispatch(
                message.text, origin_channel=SLACK_CHANNEL, origin_caller=None, slack=thread
            )
        except TaskLimitError as limit:
            await self._say(workspace, message, CAP_REACHED.format(cap=limit.cap))
            return
        except Exception:
            log.exception("could not start a task from a Slack message in %s", workspace.name)
            await self._say(workspace, message, NOT_STARTED)
            return
        log.info("Slack: task %s started from a message in %s", task.id, workspace.name)
        await self._acknowledge(workspace, message)

    async def _resume(self, workspace: SlackWorkspace, message: Message, task: Task) -> None:
        """Hand the reply to the task its thread belongs to, as a follow-up."""
        if task.status is TaskStatus.CANCELLED:
            await self._say(workspace, message, CANCELLED.format(task_id=task.id))
            return
        try:
            await self._manager.followup(task.id, message.text)
        except KeyError:  # deleted since it was looked up
            await self._say(workspace, message, NOTHING_TO_RESUME)
            return
        except ValueError:
            # Its subagent is not open yet (or it was cancelled this instant): not taken.
            await self._say(workspace, message, STARTING.format(task_id=task.id))
            return
        except Exception:
            # A steer that failed for real may have landed, so it is not sent a second way.
            log.exception("a follow-up from Slack to task %s failed", task.id)
            await self._say(workspace, message, NOT_PASSED_ON.format(task_id=task.id))
            return
        log.info("Slack: a reply in %s went to task %s as a follow-up", workspace.name, task.id)
        try:
            await self._manager.heard_on_slack(task.id, message.ts)
        except Exception:
            log.exception("could not record the Slack reply task %s acted on", task.id)
        await self._acknowledge(workspace, message)

    # --- what Keryx says back ------------------------------------------------------------

    async def _acknowledge(self, workspace: SlackWorkspace, message: Message) -> None:
        """A reaction on the message that was acted on. Its failure costs nothing."""
        try:
            result = await asyncio.to_thread(
                self._api.react, workspace.token(slack.BOT), message.channel, message.ts
            )
            if not result.ok:
                log.warning("could not acknowledge a Slack message in %s: %s", workspace.name,
                            result.problem)
        except Exception:
            log.exception("could not acknowledge a Slack message in %s", workspace.name)

    async def _say(self, workspace: SlackWorkspace, message: Message, sentence: str) -> None:
        """One sentence in the message's thread. Never raises."""
        try:
            poster = self._api.poster(workspace.token(slack.BOT), message.channel)
            await poster.post(sentence, thread_ts=message.thread_ts or message.ts)
        except Exception:
            log.exception("could not answer a Slack message in %s", workspace.name)


def _heard_already(task: Task, message: Message) -> bool:
    """Whether `message` is at or before the latest one `task` has acted on."""
    last = task.slack_last_ts
    return bool(last) and slack.is_ts(last) and slack.ts_key(message.ts) <= slack.ts_key(last)
