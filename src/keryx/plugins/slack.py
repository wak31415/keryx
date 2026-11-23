"""The `slack_report` plugin, and the one question everything else asks about Slack.

The tool posts to the owner's DM, as their own bot, once they have asked for something in
writing — so it needs the PIN. Its settings are `slack_report.toml` (`channel_id`,
`mcp_server`); the bot token is `SLACK_BOT_TOKEN` in `secrets.toml`, or the named MCP
server's own `env` in `~/.claude.json` (`integrations.slack.slack_credentials`).
`channel_id` is a channel's id or a member's: setup stores the owner's member id, found
from their address, and Slack delivers that to their messages from the app.

Four things besides the tool want Slack, and each asks `slack_route(settings)` rather than
reading a setting: the PIN-lockout alert (`notify.pin_alert`, at alert time, so turning
Slack on needs no restart), every finished task's report (`notify.notifier`, the same way,
through `report_route`), the subagent's Slack paragraph (`agents.base`), and the MCP
server a Codex subagent is handed (`agents.codex`). With the plugin off there is no route,
and none of them reaches Slack.

**Workspaces.** One Slack app per workspace, each a `SlackWorkspace`: a name, an access
level, the owner's member id there, where to post, and its tokens. The first is `default`:
the top of the TOML and the three plain `SLACK_*_TOKEN` settings, which is exactly what a
single-workspace install has always been, so it needs no migration. Each further one is a
`[workspace-NAME]` table and an entry of `SLACK_WORKSPACE_TOKENS`. Everything is sent to
`default` unless a workspace is named, and `workspaces(settings)` is the one place the two
halves are joined (`on_file` under it, for Keryx's own half alone).
"""

import contextlib
import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from keryx import plugins
from keryx.config import Settings
from keryx.config.settings import SLACK_DEFAULT_WORKSPACE
from keryx.integrations.slack import (
    APP,
    BOT,
    USER,
    Access,
    SlackPoster,
    SlackSender,
    SlackWebApi,
    slack_credentials,
    token_kinds,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.tasks.models import Task

TOOL = "slack_report"
#: The first workspace's name: the one the top of the TOML describes.
DEFAULT = SLACK_DEFAULT_WORKSPACE
#: Where a task's request came from when it was typed to the app on Slack.
SLACK_CHANNEL = "slack"

FAILED_MESSAGE = "Slack would not take the message; tell them it did not go through"


@dataclass(frozen=True)
class SlackRoute:
    """Where Slack goes while the plugin is on: a bot token and channel, and a server."""

    token: str | None
    channel: str | None
    mcp_server: str | None

    def sender(self) -> SlackSender | None:
        """A sender, when there is a token and a channel to post with."""
        if self.token and self.channel:
            return SlackWebApi(self.token, self.channel)
        return None


def _route(settings: Settings, values: dict) -> SlackRoute:
    server = values["mcp_server"] or None
    channel = values["channel_id"] or None
    token = plugins.secret(settings, "slack_bot_token")
    credentials = slack_credentials(token, channel, server=server)
    if credentials is not None:
        token, channel = credentials
    return SlackRoute(token=token, channel=channel, mcp_server=server)


def slack_route(settings: Settings) -> SlackRoute | None:
    """Slack as the plugin configures it, or None while the plugin is off."""
    if not plugins.is_on(settings, TOOL):
        return None
    return _route(settings, plugins.read_config(settings, TOOL))


def slack_sender(settings: Settings) -> SlackSender | None:
    """What the notifiers post with: None while the plugin is off or has no token."""
    route = slack_route(settings)
    return route.sender() if route is not None else None


# --- workspaces --------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackWorkspace:
    """One workspace's Slack app as Keryx holds it: its settings and its tokens."""

    name: str
    access: Access = Access.ANNOUNCE
    #: Where it posts: the owner's member id, or a channel's id.
    channel: str = ""
    #: The owner's member id there: the only member whose messages are acted on.
    owner_id: str = ""
    team: str = ""
    team_id: str = ""
    #: The app's own member id there: a message is acted on only when it mentions it.
    bot_user_id: str = ""
    #: The tokens there are, by kind (`bot`, `app`, `user`).
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)

    def token(self, kind: str) -> str | None:
        return self.tokens.get(kind) or None

    @property
    def label(self) -> str:
        """The name the owner reads: Slack's own for the first workspace, and their word
        for a further one with Slack's beside it. `name` stays the key everything is
        stored under and the value `--workspace` takes."""
        if self.name == DEFAULT:
            return self.team or "your first workspace"
        return f"{self.name} ({self.team})" if self.team else self.name

    @property
    def missing(self) -> tuple[str, ...]:
        """What its level needs that is not there, in words to print; empty when whole."""
        gaps = [f"its {kind} token" for kind in token_kinds(self.access)
                if not self.token(kind)]
        if not self.channel:
            gaps.append("somewhere to post")
        if self.access.reaches(Access.CONVERSE):
            if not self.owner_id:
                gaps.append("your member id")
            if not self.team_id:
                gaps.append("the workspace's id")
            if not self.bot_user_id:
                gaps.append("the app's own member id")
        return tuple(gaps)

    @property
    def sends(self) -> bool:
        return bool(self.token(BOT) and self.channel)

    @property
    def listens(self) -> bool:
        """Whether Keryx may hear the owner's direct messages to this app, and can.

        Everything a message is held to has to be on file: without the owner's member id,
        the workspace's id or the app's own member id (the mention that triggers it)
        there is nothing to hold one to, so nothing is heard.
        """
        return bool(
            self.access.reaches(Access.CONVERSE)
            and self.token(BOT) and self.token(APP)
            and self.owner_id and self.team_id and self.bot_user_id
        )

    @property
    def reads(self) -> bool:
        """Whether Keryx may read Slack as the owner here, and holds the token to."""
        return bool(self.access.reaches(Access.READ) and self.token(USER))

    @property
    def writes(self) -> bool:
        """Whether a message may be sent there as the owner: `write`, the user token it
        is sent with, and the member that token belongs to."""
        return bool(self.access.reaches(Access.WRITE) and self.token(USER) and self.owner_id)

    def poster(self, channel: str | None = None) -> SlackWebApi | None:
        """What posts as this workspace's bot, to `channel` or where it usually posts."""
        token, where = self.token(BOT), channel or self.channel
        return SlackWebApi(token, where) if token and where else None


def _workspace(name: str, values: Mapping[str, Any], tokens: Mapping[str, str]) -> SlackWorkspace:
    return SlackWorkspace(
        name=name,
        access=Access(values.get("access") or Access.ANNOUNCE),
        channel=str(values.get("channel_id") or ""),
        owner_id=str(values.get("owner_id") or ""),
        team=str(values.get("team") or ""),
        team_id=str(values.get("team_id") or ""),
        bot_user_id=str(values.get("bot_user_id") or ""),
        tokens={kind: token for kind, token in tokens.items() if token},
    )


def workspace_tokens(settings: Settings) -> dict[str, dict[str, str]]:
    """Every further workspace's tokens: what `settings` started with, under what the
    store holds now.

    Read from the store each time it is asked, as the billing admin keys are, so a
    workspace connected while the service runs can be posted to with no restart.
    """
    held = dict(settings.slack_workspace_tokens)
    with contextlib.suppress(Exception):
        held.update(plugins.current_settings().slack_workspace_tokens)
    return held


def on_file(
    settings: Settings, name: str, values: Mapping[str, Any] | None = None
) -> SlackWorkspace:
    """Workspace `name` as it is saved: its part of the plugin's file, and Keryx's own
    tokens for it. Never a named MCP server's token or channel, which `workspaces` lays
    over the first workspace for sending and which connecting must not mistake for its own.
    """
    if values is None:
        values = plugins.read_config(settings, TOOL)
    if name == DEFAULT:
        return _workspace(name, values, {
            BOT: plugins.secret(settings, "slack_bot_token") or "",
            APP: plugins.secret(settings, "slack_app_token") or "",
            USER: plugins.secret(settings, "slack_user_token") or "",
        })
    return _workspace(
        name, values["workspaces"].get(name) or {}, workspace_tokens(settings).get(name) or {}
    )


def workspaces(settings: Settings, values: Mapping[str, Any] | None = None) -> list[SlackWorkspace]:
    """Every workspace the plugin's settings name, `default` first, on or off.

    `values` is the plugin's settings when the caller has read them already. This and
    `on_file` under it are the only places a workspace's settings and its tokens are
    joined. The first workspace's bot token and channel are the route's, so a named MCP
    server still fills in for either; its other two tokens, and every token of a further
    workspace, are Keryx's own.
    """
    if values is None:
        values = plugins.read_config(settings, TOOL)
    route = _route(settings, values)
    own = on_file(settings, DEFAULT, values)
    first = dataclasses.replace(
        own,
        channel=route.channel or "",
        tokens={**own.tokens, **({BOT: route.token} if route.token else {})},
    )
    return [first] + [
        on_file(settings, name, values) for name in values["workspaces"] if name != DEFAULT
    ]


def connected(settings: Settings) -> list[SlackWorkspace]:
    """The workspaces Keryx can post to now: none while the plugin is off."""
    if not plugins.is_on(settings, TOOL):
        return []
    return [each for each in workspaces(settings) if each.sends]


def can_read(settings: Settings) -> bool:
    """Whether any workspace may be read as the owner now. Never raises: a plugin file
    that does not parse is a machine that cannot, and `doctor` says why."""
    try:
        return any(each.reads and each.owner_id for each in connected(settings))
    except Exception:
        return False


def can_write(settings: Settings) -> bool:
    """Whether a message may be sent as the owner in any workspace now. Never raises."""
    try:
        return any(each.writes for each in connected(settings))
    except Exception:
        return False


def can_report(settings: Settings) -> bool:
    """Whether Keryx's own bot can post to the owner now. Never raises."""
    try:
        return any(each.name == DEFAULT and each.sends for each in connected(settings))
    except Exception:
        return False


class ReportRefused(RuntimeError):
    """`keryx slack report` could not post, in a sentence that holds no token."""


async def report(settings: Settings, text: str, *, task: "Task | None" = None,
                 workspace_name: str | None = None) -> str:
    """Post `text` to the owner, as Keryx, for `keryx slack report`; where it went.

    To where that workspace's reports go, or, for a task that was typed on Slack, into
    the thread it was asked in (`report_route`, so never another workspace). Those are
    the only two places: there is no argument that names a conversation.
    """
    text = text.strip()
    if not text:
        raise ReportRefused("there is nothing on standard input to post")
    if task is not None and task.origin_channel == SLACK_CHANNEL:
        route = report_route(settings, task)
        if route is None:
            raise ReportRefused("the workspace that task was asked in is not connected, "
                                "and a report never leaves its workspace")
        posted = await route.poster.post(text, thread_ts=route.thread_ts)
        if not posted.ok:
            raise ReportRefused(f"Slack did not take it ({posted.error})")
        return "the thread the task was asked in"
    name = (workspace_name or DEFAULT).strip().lower()
    found = workspace(settings, name)
    poster = found.poster() if found is not None and found.sends else None
    if poster is None:
        raise ReportRefused(f"no Slack workspace called {name[:40]} can be posted to: "
                            "`keryx auth login slack` connects one")
    posted = await poster.post(text)
    if not posted.ok:
        raise ReportRefused(f"Slack did not take it ({posted.error})")
    return f"the {name} workspace, where their reports go"


def workspace(settings: Settings, name: str) -> SlackWorkspace | None:
    """The connected workspace called `name`, as its settings stand now."""
    return next((each for each in connected(settings) if each.name == name), None)


@dataclass(frozen=True)
class ReportRoute:
    """Where one task's report goes on Slack, and how."""

    workspace: str
    poster: SlackPoster
    #: The thread it is a reply in. None is a message of its own, whose timestamp then
    #: becomes the task's thread.
    thread_ts: str | None = None
    #: Whether a reply is also shown in the conversation itself.
    broadcast: bool = False
    #: Where the *same* workspace would post a message of its own, for a thread that is
    #: gone for good. Never another workspace's destination.
    usual: SlackPoster | None = None


def report_route(settings: Settings, task: "Task") -> ReportRoute | None:
    """Where `task`'s report goes, or None when it goes nowhere on Slack.

    A task has a thread from the moment it is asked for on Slack, or from its first report.
    Every report is a reply there, so one task stays one thread and a message typed in it
    always finds the task. **A task is answered where it was asked** (the owner's ruling of
    2026-11-23): in their direct messages with the app, or in the thread of a conversation
    the app was mentioned in, where its other members read the report too. A reply under a
    report Keryx posted is also shown in the conversation, as each report always was; one
    under the owner's own message is not, since they are in that thread already.

    **A report never leaves the workspace its task belongs to.** A task asked for on Slack
    is that workspace's: if the workspace is no longer connected, its report is posted
    nowhere (it is still in the task, for the next call's digest and `keryx tasks show`),
    because the first workspace may be somebody else's employer. A task asked for anywhere
    else was always the first workspace's, so with no thread, or with a thread in a
    workspace that is gone, it goes where the first workspace usually posts.
    """
    found = {each.name: each for each in connected(settings)}
    asked_here = task.origin_channel == SLACK_CHANNEL
    home = found.get(task.slack_workspace or "")
    if task.slack_channel and task.slack_thread_ts and home is not None:
        poster = home.poster(task.slack_channel)
        # When its thread is gone for good, a task asked for on Slack falls back to the
        # owner's own direct messages with the app, never to a channel that workspace's
        # reports happen to go to.
        usual = home.poster(home.owner_id) if asked_here and home.owner_id else home.poster()
        if poster is not None:
            return ReportRoute(
                workspace=home.name,
                poster=poster,
                thread_ts=task.slack_thread_ts,
                broadcast=not asked_here,
                usual=usual,
            )
    if asked_here:
        return None
    first = found.get(DEFAULT)
    poster = first.poster() if first is not None else None
    return ReportRoute(workspace=DEFAULT, poster=poster) if poster is not None else None


def description(owner: str) -> str:
    return (
        f"Send {owner} a written message on Slack, in the direct-message channel they "
        "already use for this. Needs the PIN — call it anyway and let it ask. Only call it "
        "when they have explicitly asked "
        'for something in writing — "send me that", "Slack me that", "put it on Slack", '
        '"text me the link", "I want that in writing". Never call it unasked, however '
        "awkward the content is to say out loud, and never volunteer a written copy of "
        "something you have already said; but when they ask for what you just said in "
        "writing, that is exactly what to send, with this tool. If something truly will not "
        'survive being spoken — a long link, a list of ten things — offer it in half a '
        'sentence ("want that on Slack?") and call this only once they say yes. When it '
        "comes back sent, say so in a few words, once. A finished task's whole report is "
        "posted to Slack by itself when the task lands: when they ask for one, say it is "
        "there already — never send it again, and never dispatch work to send it. For a "
        "file or a plot a subagent made, dispatch the sending to Claude: it can attach the "
        "file itself."
    )


PARAMETERS = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The message to send, written to be read rather than heard.",
        }
    },
    "required": ["message"],
}
#: Added to the description when there is more than one workspace to choose from.
SEVERAL = (
    " They have more than one Slack workspace: send to their usual one, and name another "
    "in `workspace` only when they said which."
)


def parameters(names: list[str]) -> dict:
    """What the tool takes: a message, and which workspace only when there are several."""
    if len(names) < 2:
        return PARAMETERS
    return {
        **PARAMETERS,
        "properties": {
            **PARAMETERS["properties"],
            "workspace": {
                "type": "string",
                "enum": names,
                "description": (
                    f"Which Slack workspace to send to. Leave it out for {names[0]}, their "
                    "usual one; name another only when they said which."
                ),
            },
        },
    }


def slack_report_tool(
    config_path: Path,
    *,
    sender: SlackSender | None = None,
    senders: Mapping[str, SlackSender] | None = None,
):
    """The tool `slack_report.py` defines, configured by the TOML at `config_path`: Keryx
    sends something to the owner, as Keryx.

    `to` and `thread` are not arguments, on purpose: this tool only ever posts where the
    owner's reports go. Sending as the owner, to somebody else, is `slack_message`.

    `sender` stands in for the first workspace's, and `senders` for every workspace's by
    name, in the tests.
    """
    from keryx.tools.custom import CustomTool, ToolUnavailable

    settings = plugins.loading_settings()
    try:
        values = plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise ToolUnavailable(str(error)) from None
    if senders is None and sender is not None:
        senders = {DEFAULT: sender}
    if senders is None:
        senders = {each.name: each.poster() for each in workspaces(settings, values)
                   if each.sends}
    if DEFAULT not in senders:
        raise ToolUnavailable(
            "no bot token and channel: `keryx auth login slack` sets both (by hand: "
            "`keryx config set SLACK_BOT_TOKEN --stdin`, and "
            f"channel_id in {config_path.name}, or an mcp_server whose config has both)"
        )
    names = list(senders)

    async def slack_report(ctx, arguments: dict) -> dict:
        message = str(arguments.get("message") or "").strip()
        if not message:
            return {"error": "message is required: say what to send"}
        name = str(arguments.get("workspace") or DEFAULT).strip().lower()
        if name not in senders:
            return {"error": f"there is no Slack workspace called {name}: one of "
                             f"{', '.join(names)}"}
        if not await senders[name].send(message):
            return {"error": FAILED_MESSAGE}
        return {"status": "sent"}

    return CustomTool(
        name=TOOL,
        description=description(settings.owner_label) + (SEVERAL if len(names) > 1 else ""),
        parameters=parameters(names),
        handler=slack_report,
        needs_pin=True,
    )


#: What the tool and its plugin were called until 2026-11-23. A machine that installed it
#: then still has `send_to_slack.py`, whose one line calls this name: it stays an entry
#: point for as long as such a file may be on a machine, and what it builds is the same
#: tool, offered as `slack_report`.
send_to_slack_tool = slack_report_tool
