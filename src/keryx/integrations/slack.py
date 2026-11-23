"""Sending the owner something on Slack, from the voice model itself.

The client behind the `slack_report` plugin (`keryx.plugins.slack`). The subagents can
already have Slack: a Slack MCP server configured user-scope in the Claude CLI is inherited
by every CLI the runner spawns, and the plugin's `mcp_server` names it. The voice model has
no such thing — it only has the tools this process registers — so this module gives it
one, using the very same bot token and DM channel rather than a second Slack app.

`slack_credentials` reads the env pair first (so the process environment can override)
and, when a server is named, falls back to that server's entry in the Claude CLI config,
which keeps one Slack app and one place to rotate it. No server name is built in: unset,
there is no fallback.

The second half is connecting in the first place (`keryx setup`, `keryx auth login slack`).
Each owner has a Slack app of their own, because a bot token cannot be handed to a command
line by an app Keryx publishes without a server holding its client secret. `app_manifest`
is that app, written down once, and `new_app_url` the link that opens Slack's "create an
app" page with it filled in. Three calls follow the token they paste: `auth_test` (is it
one, and whose), `lookup_by_email` (the member to send to, so nobody hunts for a channel
id) and `post_text` (one message, to see it arrive). Each answers with a `SlackResult`,
never an exception. A failure is an error code and a sentence to print, and of what
Slack said about it only the code is kept, so no refusal can carry the token back. A
success carries what was asked for: the workspace's name and address, or the member's id
and name, each cut to one line.

The app comes at one of three **access levels** (`Access`), chosen before it is created
because the level decides the manifest: `announce` posts and reads nothing, `converse`
also hears the direct messages typed to the app itself (Socket Mode, an app-level token),
and `read` also holds a user token that reads Slack as the owner. `app_manifest(access)`
is the only place a level's scopes are written. What listens is `slack_socket`, and what
acts on a message is `keryx.slack_inbox`.
"""

import asyncio
import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from keryx.config.files import claude_user_config

log = logging.getLogger("keryx.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
AUTH_TEST_URL = "https://slack.com/api/auth.test"
LOOKUP_BY_EMAIL_URL = "https://slack.com/api/users.lookupByEmail"
CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"
REACTIONS_ADD_URL = "https://slack.com/api/reactions.add"
CONVERSATIONS_HISTORY_URL = "https://slack.com/api/conversations.history"
#: What Slack answers a bot that asks for the history of a conversation it is not in.
NOT_A_PARTY = frozenset({"channel_not_found", "not_in_channel"})
#: Slack's own page for making an app; `new_app=1&manifest_json=…` opens it filled in.
NEW_APP_URL = "https://api.slack.com/apps"
REQUEST_TIMEOUT_S = 15.0



class Access(StrEnum):
    """How much of Slack the owner's app may see: a ladder, each level holding the last."""

    #: Keryx posts, and reads nothing.
    ANNOUNCE = "announce"
    #: And is sent the direct messages typed to the app itself; one from the owner that
    #: mentions the app may resume or start work.
    CONVERSE = "converse"
    #: And is sent the mentions of the app in conversations it was added to (anyone's;
    #: only the owner's is acted on, and answered there), and reads Slack as the owner,
    #: with a user token, when the owner asks about messages.
    READ = "read"
    #: And may send a message as the owner, with the same user token, to a person, a
    #: channel or a thread, when the owner asks for exactly that message.
    WRITE = "write"

    def reaches(self, level: "Access") -> bool:
        """Whether this level is `level` or above it."""
        return LEVELS.index(self) >= LEVELS.index(level)


#: The ladder, lowest first.
LEVELS = tuple(Access)

#: What the app may do at every level. `chat:write` posts, and with a member id as the
#: channel it posts to that member's conversation with the app, so no `im:write`.
#: `users:read.email` is `users.lookupByEmail`, and Slack grants it only together with
#: `users:read`.
BOT_SCOPES = ("chat:write", "users:read", "users:read.email")
#: Added at `converse`: `im:history` is the `message.im` event (only the app's own direct
#: messages, as a bot scope), and `reactions:write` is the acknowledgement, a reaction,
#: because a reaction notifies nobody and a "got it" message does.
CONVERSE_BOT_SCOPES = ("im:history", "reactions:write")
#: Added at `read`: `app_mentions:read` is the `app_mention` event, a mention of the app
#: in a conversation it has been added to. Nothing else of those conversations is sent
#: to Keryx, and the bot is given no scope that could read them: what is read there is
#: read as the owner, with the user token, and only when they ask.
READ_BOT_SCOPES = ("app_mentions:read",)
#: The events the app subscribes to, as the bot: its own direct messages at `converse`,
#: and at `read` the mentions of it elsewhere. Never `message.channels`, `.groups` or
#: `.mpim`, and never a user-level event: Keryx must not be sent a conversation it was
#: not addressed in, and a user-level event is every direct message the owner has.
BOT_EVENTS = ("message.im",)
READ_BOT_EVENTS = ("app_mention",)
#: Search, as the owner, at `read`. The one constant phase two changes if it changes.
#: `search:read` is the scope `search.messages` takes. Slack calls it legacy and points
#: at the Real-time Search API (`assistant.search.context`, the granular
#: `search:read.public|private|im|mpim`), but that API's terms say its results must not
#: be stored or copied, and Keryx writes down every call's transcript and every task's
#: report. The legacy scope carries no such term, and each owner's app is internal to
#: their own workspace.
SEARCH_SCOPES = ("search:read",)
#: What the user token may do at `read`: the history and the listing of each kind of
#: conversation the owner is in, the member list, and search.
USER_SCOPES = (
    "channels:history", "groups:history", "im:history", "mpim:history",
    "channels:read", "groups:read", "im:read", "mpim:read",
    "users:read",
    *SEARCH_SCOPES,
)
#: Added to the user token at `write`: `chat:write` posts as the owner wherever they could
#: post, and `im:write` opens their direct conversation with a member they have not
#: written to before (`conversations.open`). Nothing that edits, deletes, uploads or
#: joins: `integrations/slack_write.py` can call those two methods and no other.
WRITE_USER_SCOPES = ("chat:write", "im:write")
#: The one scope an app-level token needs for Socket Mode.
APP_TOKEN_SCOPE = "connections:write"
#: The acknowledgement: the reaction put on a message that was acted on.
ACK_REACTION = "eyes"

#: The three tokens, by the name each is stored under, and how each one starts.
BOT, APP, USER = "bot", "app", "user"
TOKEN_PREFIXES = {BOT: "xoxb-", APP: "xapp-", USER: "xoxp-"}


def token_kinds(access: Access) -> tuple[str, ...]:
    """The tokens a level needs, in the order they are asked for."""
    kinds = [BOT]
    if access.reaches(Access.CONVERSE):
        kinds.append(APP)
    if access.reaches(Access.READ):
        kinds.append(USER)
    return tuple(kinds)


def token_kind(token: str) -> str | None:
    """Which of the three `token` is, by its prefix; None when it is none of them."""
    return next((kind for kind, prefix in TOKEN_PREFIXES.items() if token.startswith(prefix)),
                None)


#: In place of Slack's error code when Slack was never reached, or did not answer as Slack.
UNREACHABLE = "unreachable"
UNKNOWN_ERROR = "unknown_error"
#: And when something on the way answered with an HTTP error: `http_429`, `http_503`.
_HTTP = "http_"
#: The shape of an error code. Anything else in `error` is not repeated.
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
_MEMBER_ID = re.compile(r"[A-Z0-9]{2,32}")
#: A message's timestamp, which is also its id within its conversation.
_TS = re.compile(r"\d{1,12}\.\d{1,9}")
#: A conversation's id: a channel's, a group's or a direct message's.
_CONVERSATION = re.compile(r"[CDG][A-Z0-9]{2,31}")

#: The refusals setup meets often enough to say in words; the code always follows.
_EXPLAINED = {
    "invalid_auth": "Slack does not recognise that token",
    "missing_scope": "the app lacks a permission this needs",
    "users_not_found": "nobody in that workspace has that address",
    "channel_not_found": "Slack does not know that channel or member",
    "not_allowed_token_type": "that is not the kind of token this call takes",
}
#: The most Markdown one message carries: Slack's limit on all `markdown` blocks in a payload.
MARKDOWN_LIMIT = 12_000


class SlackSender(Protocol):
    """Anything that can put a line in front of the owner on Slack."""

    async def send(self, text: str, *, fallback: str | None = None) -> bool:
        """Post `text`, which is Markdown; True when Slack accepted it.

        `fallback` is what a notification shows in place of the whole of `text`.
        """
        ...


class SlackPoster(Protocol):
    """A sender that can also post into a thread, and says where the message landed."""

    async def post(
        self,
        text: str,
        *,
        fallback: str | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
    ) -> "Posted":
        """Post `text`, and say where its first piece landed or why Slack did not take it.

        With `thread_ts` it is a reply in that thread, and `broadcast` also shows it in
        the conversation itself. Without one, a text too long for one message is one
        thread: every piece after the first is a reply under the first.
        """
        ...


def is_ts(value: object) -> bool:
    """Whether `value` is a message timestamp as Slack writes one."""
    return isinstance(value, str) and _TS.fullmatch(value) is not None


def is_conversation(value: object) -> bool:
    """Whether `value` is a conversation's id as Slack writes one."""
    return isinstance(value, str) and _CONVERSATION.fullmatch(value) is not None


def ts_key(ts: str) -> tuple[int, int]:
    """A timestamp in an order that can be compared: later messages are greater."""
    seconds, _, fraction = ts.partition(".")
    return int(seconds), int(fraction.ljust(9, "0"))


_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))
#: A link as Slack writes one in a message's text: `<https://x>` or `<https://x|shown>`.
_LINK = re.compile(r"<((?:https?://|mailto:)[^<>|]{1,2000})(?:\|[^<>]{0,2000})?>")


def plain_text(text: str) -> str:
    """A message's text as it was typed: links unwrapped and Slack's three escapes undone."""
    text = _LINK.sub(lambda match: match.group(1), text)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return text.strip()


def markdown_pieces(text: str, limit: int = MARKDOWN_LIMIT) -> list[str]:
    """`text` in pieces of at most `limit`, each ending at a blank line outside a code block.

    Slack shows half a table or half a code block as raw characters, so a piece only ever
    ends between paragraphs, and never right after a heading; a single paragraph longer
    than `limit` is the one thing cut.
    """
    paragraphs: list[str] = []
    for paragraph in text.split("\n\n"):
        last = paragraphs[-1] if paragraphs else ""
        # Still inside a code block, or a heading that goes with what it heads.
        if last.count("```") % 2 or (last.startswith("#") and "\n" not in last):
            paragraphs[-1] += "\n\n" + paragraph
        else:
            paragraphs.append(paragraph)
    pieces: list[str] = []
    for paragraph in paragraphs:
        if pieces and len(pieces[-1]) + 2 + len(paragraph) <= limit:
            pieces[-1] += "\n\n" + paragraph
        else:
            pieces.append(paragraph)
    return [piece[i : i + limit] for piece in pieces for i in range(0, len(piece), limit)]


def mcp_server_config(server: str, config_path: Path | None = None) -> dict | None:
    """The user-scope MCP server `server` from the Claude CLI's config, or None.

    Claude subagents find it there by themselves; this is for everything that does not —
    `slack_report`, and a Codex subagent, which is handed the same server explicitly. The
    config is `claude_user_config()` unless `config_path` names another.
    """
    try:
        path = config_path or claude_user_config()
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    found = config.get("mcpServers", {}).get(server) if isinstance(config, dict) else None
    return found if isinstance(found, dict) else None


def slack_credentials(
    token: str | None = None,
    channel: str | None = None,
    *,
    server: str | None = None,
    config_path: Path | None = None,
) -> tuple[str, str] | None:
    """The bot token and DM channel: the given pair, else the named MCP server's config."""
    if token and channel:
        return token, channel
    if not server:
        return None
    env = (mcp_server_config(server, config_path) or {}).get("env", {})
    resolved_token = token or env.get("SLACK_BOT_TOKEN")
    resolved_channel = channel or env.get("SLACK_CHANNEL_ID")
    if not (resolved_token and resolved_channel):
        return None
    return resolved_token, resolved_channel


def _post(url: str, payload: dict, token: str) -> dict:
    """One blocking JSON POST to the Slack Web API, in a worker thread."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def _post_form(url: str, params: dict, token: str) -> dict:
    """One blocking form-encoded POST: what `apps.connections.open` is documented to take."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def _get(url: str, params: dict, token: str) -> dict:
    """One blocking GET to the Slack Web API. The token is a header, never in the address."""
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


# --- connecting --------------------------------------------------------------------------


def user_scopes(access: Access) -> tuple[str, ...]:
    """What the user token may do at `access`: nothing below `read`."""
    if not access.reaches(Access.READ):
        return ()
    return USER_SCOPES + (WRITE_USER_SCOPES if access.reaches(Access.WRITE) else ())


def bot_scopes(access: Access) -> tuple[str, ...]:
    """What the app's bot may do at `access`."""
    return (
        BOT_SCOPES
        + (CONVERSE_BOT_SCOPES if access.reaches(Access.CONVERSE) else ())
        + (READ_BOT_SCOPES if access.reaches(Access.READ) else ())
    )


def bot_events(access: Access) -> tuple[str, ...]:
    """What the app is sent at `access`; nothing at all below `converse`."""
    if not access.reaches(Access.CONVERSE):
        return ()
    return BOT_EVENTS + (READ_BOT_EVENTS if access.reaches(Access.READ) else ())


def app_manifest(access: Access = Access.ANNOUNCE) -> dict:
    """The Slack app each owner creates, at the level they chose.

    The only place it is written down. At `announce` it is a bot that posts and can find
    the owner by address: nothing listens to Slack — no events, no redirect URL, no socket
    mode — and the Messages tab is on, read-only, because that tab is where a message to
    a member arrives and there is nothing to answer one typed there. At `converse` the
    tab takes messages, the bot subscribes to `message.im` and nothing else, and Socket
    Mode is on, so no address of Keryx's is ever given to Slack. At `read` the bot is
    also sent the mentions of it (`app_mention`), and the install asks the owner for a
    user token, with `USER_SCOPES`; at `write` that token may also post as them
    (`WRITE_USER_SCOPES`).
    """
    converse = access.reaches(Access.CONVERSE)
    manifest: dict = {
        "display_information": {
            "name": "Keryx",
            "description": "Messages from your Keryx phone assistant",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": not converse,
            },
            "bot_user": {"display_name": "Keryx"},
        },
        "oauth_config": {"scopes": {"bot": list(bot_scopes(access))}},
    }
    if access.reaches(Access.READ):
        manifest["oauth_config"]["scopes"]["user"] = list(user_scopes(access))
    if converse:
        manifest["settings"] = {
            "event_subscriptions": {"bot_events": list(bot_events(access))},
            "socket_mode_enabled": True,
        }
    return manifest


def manifest_json(access: Access = Access.ANNOUNCE) -> str:
    """The manifest as JSON to paste into an existing app's App Manifest page."""
    return json.dumps(app_manifest(access), indent=2)


def new_app_url(access: Access = Access.ANNOUNCE) -> str:
    """The link that opens Slack's "create an app" page with `app_manifest` filled in."""
    manifest = json.dumps(app_manifest(access), separators=(",", ":"))
    return f"{NEW_APP_URL}?new_app=1&manifest_json={urllib.parse.quote(manifest, safe='')}"


@dataclass(frozen=True)
class SlackResult:
    """What one of setup's calls came to: `error` is empty when Slack said yes."""

    #: Slack's own code (`invalid_auth`), `http_<status>`, `unreachable` or `unknown_error`.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def refused(self) -> bool:
        """Slack itself said no. False when it worked, and when Slack was never properly
        asked: not reached, an HTTP error on the way, an answer that is not Slack's."""
        return bool(self.error) and not (
            self.error in (UNREACHABLE, UNKNOWN_ERROR) or self.error.startswith(_HTTP)
        )

    @property
    def problem(self) -> str:
        """Why not, in words to print, with Slack's code; empty when it worked."""
        if not self.error:
            return ""
        if self.error == UNREACHABLE:
            return "could not reach Slack"
        if self.error == UNKNOWN_ERROR:
            return "Slack's answer could not be read"
        if self.error.startswith(_HTTP):
            return f"Slack answered HTTP {self.error.removeprefix(_HTTP)}"
        known = _EXPLAINED.get(self.error)
        return f"{known} ({self.error})" if known else f"Slack said {self.error}"


#: The refusals that mean a thread cannot be posted to any more, as opposed to not just
#: now: the conversation or the message is gone, the app is no longer in it, or the token
#: is not the app's that opened it. A timeout or `ratelimited` is not among them.
THREAD_GONE = frozenset({
    "channel_not_found", "thread_not_found", "message_not_found", "not_in_channel",
    "is_archived", "invalid_auth", "account_inactive", "token_revoked",
})


@dataclass(frozen=True)
class Posted(SlackResult):
    """Where a message landed: the conversation Slack put it in, and its timestamp; or,
    with `error`, why it did not land.

    The conversation is Slack's answer, not what was asked for: a message sent to a
    member's id lands in their direct message with the app, whose id (`D…`) is the one
    a reply to it arrives with.
    """

    channel: str = ""
    ts: str = ""

    @property
    def gone(self) -> bool:
        """Slack refused it because the thread or its conversation is gone for good."""
        return self.error in THREAD_GONE


@dataclass(frozen=True)
class Workspace(SlackResult):
    """`auth.test`: the workspace a token belongs to, and whom it acts as.

    `user_id` is the bot's own member id for a bot token, and the member who installed
    the app for a user token: that is how the owner is known at `read`.
    """

    team: str = ""
    team_id: str = ""
    user_id: str = ""
    #: What the token may do, when Slack said (the `x-oauth-scopes` header of its
    #: answer); None when it did not, which is never taken to mean "nothing".
    scopes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Connection(SlackResult):
    """`apps.connections.open`: the address of a Socket Mode connection.

    The address carries a ticket that is the whole of its authentication, so it is a
    secret: never logged, never in an error, and left out of this object's `repr`.
    """

    url: str = field(default="", repr=False)


@dataclass(frozen=True)
class Member(SlackResult):
    """`users.lookupByEmail`: the member an address belongs to."""

    user_id: str = ""
    name: str = ""


def _ask(call, url: str, payload: dict, token: str) -> tuple[dict, str]:
    """Slack's answer and an error code, one of them empty. Nothing raised is repeated."""
    try:
        result = call(url, payload, token)
    except urllib.error.HTTPError as exc:
        return {}, f"{_HTTP}{exc.code}"
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # `OSError` is a timeout or a reset; `HTTPException` a reply cut short or garbled,
        # which urllib lets through as it is.
        return {}, UNREACHABLE
    if not isinstance(result, dict):
        return {}, UNKNOWN_ERROR
    if result.get("ok"):
        return result, ""
    code = str(result.get("error") or "")
    return {}, code if _ERROR_CODE.fullmatch(code) else UNKNOWN_ERROR


def _line(value: object) -> str:
    """A name Slack gave, as one line: it is printed on the owner's terminal."""
    return " ".join(str(value or "").split())


def is_dm(channel: str) -> bool:
    """Whether `channel` is a direct message's id (`D…`).

    A direct message is a conversation between one app and one member, so its id is no use
    to another app's token. A member's id (`U…`, `W…`) and a channel's (`C…`, `G…`) are the
    workspace's, and outlive a change of token.
    """
    return channel.startswith("D")


def _post_scoped(url: str, payload: dict, token: str) -> dict:
    """`_post`, with what Slack says the token may do beside its answer, under `SCOPES`."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        answer = json.load(response)
        if isinstance(answer, dict):
            answer[SCOPES] = response.headers.get("x-oauth-scopes")
        return answer


#: Where `_post_scoped` puts the header: a key no Slack answer has.
SCOPES = "\0scopes"


def auth_test(token: str, *, post=_post_scoped) -> Workspace:
    """Whether Slack takes `token`, the workspace it belongs to, and what it may do when
    Slack says. Needs no scope."""
    result, error = _ask(post, AUTH_TEST_URL, {}, token)
    if error:
        return Workspace(error=error)
    granted = result.get(SCOPES)
    return Workspace(
        scopes=tuple(scope.strip() for scope in granted.split(",") if scope.strip())
        if isinstance(granted, str) else None,
        team=_line(result.get("team")),
        team_id=_id(result.get("team_id")),
        user_id=_id(result.get("user_id")),
    )


def _id(value: object) -> str:
    """An id Slack gave, when it has the shape of one; "" otherwise."""
    return value if isinstance(value, str) and _MEMBER_ID.fullmatch(value) else ""


def open_connection(app_token: str, *, post=_post_form) -> Connection:
    """A Socket Mode address for the app `app_token` belongs to. Needs `connections:write`.

    Also how setup checks an app-level token: Slack answers only for one it takes.
    """
    result, error = _ask(post, CONNECTIONS_OPEN_URL, {}, app_token)
    if error:
        return Connection(error=error)
    url = result.get("url")
    if not (isinstance(url, str) and url.startswith("wss://")):
        return Connection(error=UNKNOWN_ERROR)
    return Connection(url=url)


def in_conversation(token: str, channel: str, *, get=_get) -> SlackResult:
    """Whether the app `token` belongs to is a party to `channel`: one read of its latest
    message, which Slack refuses a bot for any conversation it is not in.

    Needs `im:history` for a direct message, which the app has at `converse` (it is what
    the `message.im` event rests on). Ok means it is; an error in `NOT_A_PARTY` means it
    is not; any other error says nothing either way. The message read is not kept.
    """
    _, error = _ask(get, CONVERSATIONS_HISTORY_URL, {"channel": channel, "limit": 1}, token)
    return SlackResult(error=error)


def add_reaction(
    token: str, channel: str, ts: str, name: str = ACK_REACTION, *, post=_post
) -> SlackResult:
    """React to the message at `ts` in `channel`. One already there is not a failure."""
    payload = {"channel": channel, "timestamp": ts, "name": name}
    _, error = _ask(post, REACTIONS_ADD_URL, payload, token)
    return SlackResult(error="" if error == "already_reacted" else error)


def lookup_by_email(token: str, email: str, *, get=_get) -> Member:
    """The member of the token's workspace whose address is `email`."""
    result, error = _ask(get, LOOKUP_BY_EMAIL_URL, {"email": email.strip()}, token)
    if error:
        return Member(error=error)
    user = result.get("user")
    user_id = str(user.get("id") or "") if isinstance(user, dict) else ""
    if not _MEMBER_ID.fullmatch(user_id):
        return Member(error=UNKNOWN_ERROR)
    return Member(user_id=user_id, name=_line(user.get("real_name") or user.get("name")))


def post_text(token: str, channel: str, text: str, *, post=_post) -> SlackResult:
    """One plain message to `channel`, which may be a member id: their messages from the app."""
    _, error = _ask(post, POST_MESSAGE_URL, {"channel": channel, "text": text}, token)
    return SlackResult(error=error)


class SlackWebApi:
    """`SlackSender` and `SlackPoster` over `chat.postMessage`.

    The text goes in a `markdown` block, which Slack renders as standard Markdown (headings,
    tables, links), where a message's own `text` is Slack's mrkdwn and shows Markdown as
    raw characters. Links are not unfurled: a report's links are references, and a preview
    card for each one buries it.
    """

    def __init__(self, token: str, channel: str, *, post=_post) -> None:
        self._token = token
        self._channel = channel
        self._post = post

    async def send(self, text: str, *, fallback: str | None = None) -> bool:
        return (await self._deliver(text, fallback=fallback, chain=False)).ok

    async def post(
        self,
        text: str,
        *,
        fallback: str | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
    ) -> Posted:
        return await self._deliver(
            text, fallback=fallback, thread_ts=thread_ts, broadcast=broadcast, chain=True
        )

    async def _deliver(
        self,
        text: str,
        *,
        fallback: str | None,
        thread_ts: str | None = None,
        broadcast: bool = False,
        chain: bool,
    ) -> Posted:
        """Post each piece of `text`; the first piece's landing, or the first failure.

        `chain` makes a text of several pieces one thread: with no `thread_ts`, every
        piece after the first is a reply under the first, in the conversation Slack put
        that one in. A reply typed under any of them is then in the one thread.
        """
        first: Posted | None = None
        channel = self._channel
        for piece in markdown_pieces(text):
            plain = {
                "channel": channel,
                "text": piece,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if thread_ts:
                plain["thread_ts"] = thread_ts
                if broadcast and first is None:
                    plain["reply_broadcast"] = True
            payload = {
                **plain,
                "text": fallback or piece,
                "blocks": [{"type": "markdown", "text": piece}],
            }
            try:
                result = await asyncio.to_thread(
                    self._post, POST_MESSAGE_URL, payload, self._token
                )
                if str(result.get("error", "")).startswith("invalid_blocks"):
                    # Slack pitches the markdown block at AI apps; one refused it still gets text.
                    result = await asyncio.to_thread(
                        self._post, POST_MESSAGE_URL, plain, self._token
                    )
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                log.warning("could not post to Slack: %s", exc)
                return self._failed(first, UNREACHABLE)
            if not result.get("ok"):
                # Slack reports failure in the body with a 200, e.g. {"ok": false, "error": …}
                code = str(result.get("error") or "")
                log.warning("Slack refused the message: %s", code)
                return self._failed(first, code if _ERROR_CODE.fullmatch(code) else UNKNOWN_ERROR)
            if first is None:
                landed, ts = result.get("channel"), result.get("ts")
                first = Posted(
                    channel=landed if isinstance(landed, str) and landed else self._channel,
                    ts=ts if is_ts(ts) else "",
                )
                if chain and not thread_ts and first.ts:
                    thread_ts, channel = first.ts, first.channel
        assert first is not None  # `markdown_pieces` always yields one piece
        return first

    @staticmethod
    def _failed(first: Posted | None, error: str) -> Posted:
        """A failure that still says where the first piece landed, when one did: the
        thread it started is there, whatever became of the rest."""
        if first is None:
            return Posted(error=error)
        return Posted(error=error, channel=first.channel, ts=first.ts)
