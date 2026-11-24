"""Sending the owner something on Slack, from the voice model itself.

The client behind the `slack_report` plugin (`keryx.plugins.slack`). The subagents can
already have Slack: a Slack MCP server configured user-scope in the Claude CLI is inherited
by every CLI the runner spawns, and the plugin's `mcp_server` names it. The voice model has
no such thing — it only has the tools this process registers — so this module gives it
one, using the very same bot token and DM channel rather than a second Slack app. That is
the install from before Keryx's own app: once a bot token is saved, the app outranks the
server for everything (`keryx.plugins.slack._route`).

`slack_credentials` reads the env pair first (so the process environment can override)
and, when a server is named, falls back to that server's entry in the Claude CLI config,
which keeps one Slack app and one place to rotate it. No server name is built in: unset,
there is no fallback.

The second half is connecting in the first place (`keryx setup`, `keryx auth login slack`).
Each owner has a Slack app of their own, because a bot token cannot be handed to a command
line by an app Keryx publishes without a server holding its client secret. `app_manifest`
is that app, written down once, and `new_app_url` the link that opens Slack's "create an
app" page with it filled in. Two calls follow the tokens they paste: `auth_test` (is it
one, and whose: for the user token, the member who installed the app, which is who the
owner is) and `post_text` (one message, to see it arrive). Each answers with a `SlackResult`,
never an exception. A failure is an error code and a sentence to print, and of what
Slack said about it only the code is kept, so no refusal can carry the token back. A
success carries what was asked for: the workspace's name and address, or the member's id
and name, each cut to one line.

What the app may do is a **set of permissions the owner ticks** (`Permission`), chosen
before it is created because the set decides the manifest. Sending them reports and files
is the base and always there. `requests` also has Keryx hear the direct messages typed to
the app itself (Socket Mode, an app-level token); `read` has the user token read Slack as
the owner; `send` has it post as the owner. Any combination is one app, and
`app_manifest(allowed)` with the `*_SCOPES` constants is the only place a permission's
scopes are written. What listens is `slack_socket`, and what acts on a message is
`keryx.slack_inbox`.

The third part is a file (`SlackFiles`, behind `keryx slack report --file`). Slack takes
one in three steps, and each is its own request: `files.getUploadURLExternal` answers
with an address and a file id, the bytes are posted to that address, and
`files.completeUploadExternal` shares the file into a conversation. The single-call
`files.upload` was retired on 2025-11-12. Both methods take the bot scope `files:write`
(`FILE_BOT_SCOPES`), which every manifest has; an app made before it lacks the
scope, and Slack then refuses the first step with `missing_scope`, before anything is
sent. The address is not logged or kept, and the token is never sent to it.
"""

import asyncio
import http.client
import json
import logging
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from keryx.config.files import claude_user_config

log = logging.getLogger("keryx.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
AUTH_TEST_URL = "https://slack.com/api/auth.test"
CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"
REACTIONS_ADD_URL = "https://slack.com/api/reactions.add"
CONVERSATIONS_HISTORY_URL = "https://slack.com/api/conversations.history"
GET_UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"
COMPLETE_UPLOAD_URL = "https://slack.com/api/files.completeUploadExternal"
#: What Slack answers a bot that asks for the history of a conversation it is not in.
NOT_A_PARTY = frozenset({"channel_not_found", "not_in_channel"})
#: Slack's own page for making an app; `new_app=1&manifest_json=…` opens it filled in.
NEW_APP_URL = "https://api.slack.com/apps"
REQUEST_TIMEOUT_S = 15.0
#: For each read or write of a file's bytes on their way to Slack, not for the whole of it.
UPLOAD_TIMEOUT_S = 60.0
#: The most Slack takes in one file, and the most it puts in one message. Neither is in
#: its API reference: both are its help centre's ("files up to 1GB", "up to 10 files"),
#: read as the smaller gigabyte so nothing it would refuse is sent.
MAX_FILE_BYTES = 1_000_000_000
MAX_FILES = 10



class Permission(StrEnum):
    """One thing the owner may tick for their Slack app, beyond what it always does
    (send them reports and files). Independent: any set of them is one manifest."""

    #: Keryx is sent the direct messages typed to the app itself, and acts on the
    #: owner's: a new message starts work, a reply under a report resumes its task.
    REQUESTS = "requests"
    #: Keryx reads Slack as the owner, with their user token, when they ask about
    #: messages. With `REQUESTS` as well, the owner's mention of the app in a
    #: conversation it was added to is a request too (`mentions`).
    READ = "read"
    #: Keryx may send a message as the owner, with the same user token, to a person, a
    #: channel or a thread, when the owner asks for exactly that message.
    SEND = "send"


#: Every permission, in the order they are shown and written down.
PERMISSIONS = tuple(Permission)
#: What a workspace's app may do beyond the base: a set of `Permission`.
Allowed = frozenset[Permission]
#: Nothing ticked: the app sends the owner reports and files, and that is all.
BASE: Allowed = frozenset()
EVERYTHING: Allowed = frozenset(PERMISSIONS)


def permissions(names: Iterable[object]) -> Allowed:
    """`names` as a set of permissions. `ValueError` for one that is not a permission,
    carrying that name."""
    found = set()
    for name in names:
        try:
            found.add(Permission(str(name).strip().lower()))
        except ValueError:
            raise ValueError(str(name)) from None
    return frozenset(found)


def ordered(chosen: Iterable[Permission]) -> tuple[Permission, ...]:
    """`chosen` in the one order permissions are shown and stored in."""
    chosen = set(chosen)
    return tuple(each for each in PERMISSIONS if each in chosen)


def mentions(chosen: Allowed) -> bool:
    """Whether the owner's mention of the app in a conversation it was added to is a
    request: with `REQUESTS` (something has to listen) and `READ` (the thread around a
    mention is somebody else's words, which only the owner's own token may read, so
    without it the work would be handed one line of a conversation it cannot see)."""
    return Permission.REQUESTS in chosen and Permission.READ in chosen


def as_owner(chosen: Allowed) -> bool:
    """Whether the user token is used for anything after it has said who the owner is."""
    return Permission.READ in chosen or Permission.SEND in chosen


#: What sending a file takes: `files.getUploadURLExternal` and
#: `files.completeUploadExternal` both ask for this one scope and no other. It also lets
#: the app delete a file it uploaded, which nothing here does.
FILE_BOT_SCOPES = ("files:write",)
#: What the app may do whatever is ticked. `chat:write` posts, and with a member id as
#: the channel it posts to that member's conversation with the app, so no `im:write`.
#: The bot reads nothing about members: who the owner is comes from their own user token,
#: so the `users:read` and `users:read.email` an address lookup needed are gone.
BOT_SCOPES = ("chat:write", *FILE_BOT_SCOPES)
#: Added with `requests`: `im:history` is the `message.im` event (only the app's own
#: direct messages, as a bot scope), and `reactions:write` is the acknowledgement, a
#: reaction, because a reaction notifies nobody and a "got it" message does.
REQUESTS_BOT_SCOPES = ("im:history", "reactions:write")
#: Added with `requests` and `read` together (`mentions`): `app_mentions:read` is the
#: `app_mention` event, a mention of the app in a conversation it has been added to.
#: Nothing else of those conversations is sent to Keryx, and the bot is given no scope
#: that could read them: what is read there is read as the owner, with the user token,
#: and only when they ask.
MENTION_BOT_SCOPES = ("app_mentions:read",)
#: The events the app subscribes to, as the bot: its own direct messages with
#: `requests`, and with `read` as well the mentions of it elsewhere. Never
#: `message.channels`, `.groups` or `.mpim`, and never a user-level event: Keryx must not
#: be sent a conversation it was not addressed in, and a user-level event is every
#: direct message the owner has.
BOT_EVENTS = ("message.im",)
MENTION_BOT_EVENTS = ("app_mention",)
#: Search, as the owner, with `read`. The one constant phase two changes if it changes.
#: `search:read` is the scope `search.messages` takes. Slack calls it legacy and points
#: at the Real-time Search API (`assistant.search.context`, the granular
#: `search:read.public|private|im|mpim`), but that API's terms say its results must not
#: be stored or copied, and Keryx writes down every call's transcript and every task's
#: report. The legacy scope carries no such term, and each owner's app is internal to
#: their own workspace.
SEARCH_SCOPES = ("search:read",)
#: What the user token needs to find who or where is meant, with `read` or `send`: the
#: listing of each kind of conversation the owner is in (`users.conversations`) and the
#: member list (`users.list`, `users.info`). Names and ids, and no message.
LOOKUP_USER_SCOPES = (
    "channels:read", "groups:read", "im:read", "mpim:read",
    "users:read",
)
#: Added to the user token with `read`: the history of each kind of conversation the
#: owner is in, and search. `send` alone never gets these.
READ_USER_SCOPES = (
    "channels:history", "groups:history", "im:history", "mpim:history",
    *SEARCH_SCOPES,
)
#: What the user token may do with neither `read` nor `send`: one scope, the least
#: revealing Slack grants a user token, because an install issues a user token only when
#: it asks for a user scope, and the token is what says who installed the app
#: (`auth.test`, which needs no scope itself). `emoji:read` is "View custom emoji in a
#: workspace": its one method is `emoji.list`, it is granted to user tokens, it is not a
#: legacy scope, and nothing it shows is anybody's. (`team:read` also shows the
#: workspace's email domain and lists external teams and contacts; `identify` is
#: legacy.) Keryx never calls `emoji.list`.
IDENTITY_USER_SCOPES = ("emoji:read",)
#: Added to the user token with `send`: `chat:write` posts as the owner wherever they
#: could post, and `im:write` opens their direct conversation with a member they have
#: not written to before (`conversations.open`). Nothing that edits, deletes, uploads or
#: joins: `integrations/slack_write.py` can call those two methods and no other.
SEND_USER_SCOPES = ("chat:write", "im:write")
#: The one scope an app-level token needs for Socket Mode.
APP_TOKEN_SCOPE = "connections:write"
#: The acknowledgement: the reaction put on a message that was acted on.
ACK_REACTION = "eyes"

#: The three tokens, by the name each is stored under, and how each one starts.
BOT, APP, USER = "bot", "app", "user"
TOKEN_PREFIXES = {BOT: "xoxb-", APP: "xapp-", USER: "xoxp-"}


def token_kinds(chosen: Allowed = BASE) -> tuple[str, ...]:
    """The tokens connecting with `chosen` takes, in the order they are asked for.

    The user token whatever is ticked (the owner's ruling of 2026-11-24, "let's always
    require a user token"): a bot token names the bot, not whoever installed it, and
    `auth.test` on the user token is how Slack itself says who the owner is.

    **The order is the order Slack's console shows them in**, as the owner found walking
    it: the app-level token is on a page of its own, met before the app is installed;
    then one section of one page lists the User OAuth Token and, below it, the Bot User
    OAuth Token. The steps are written in this order and every prompt asks in it, so
    nobody is sent back to a page they have left. Nothing else may read an order out of
    this: standard input is told apart by prefix.
    """
    return (APP, USER, BOT) if Permission.REQUESTS in chosen else (USER, BOT)


def used_kinds(chosen: Allowed = BASE) -> tuple[str, ...]:
    """The tokens a connection works with once it is made: the bot's to post, the app's
    to listen with `requests`, the user's to read or send as the owner. With neither of
    those ticked the user token is used to connect and for nothing after, so a workspace
    an older build connected, which has none, lacks nothing it runs on."""
    kinds = [BOT]
    if Permission.REQUESTS in chosen:
        kinds.append(APP)
    if as_owner(chosen):
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
#: A file's id, as `files.getUploadURLExternal` answers with one.
_FILE_ID = re.compile(r"F[A-Z0-9]{2,31}")

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


def user_scopes(chosen: Allowed = BASE) -> tuple[str, ...]:
    """What the user token may do with `chosen`: with neither `read` nor `send`, the one
    scope that makes Slack issue it at all. `send` without `read` gets the lookups that
    find a recipient and the two scopes that post, and no history or search."""
    if not as_owner(chosen):
        return IDENTITY_USER_SCOPES
    return (
        LOOKUP_USER_SCOPES
        + (READ_USER_SCOPES if Permission.READ in chosen else ())
        + (SEND_USER_SCOPES if Permission.SEND in chosen else ())
    )


def bot_scopes(chosen: Allowed = BASE) -> tuple[str, ...]:
    """What the app's bot may do with `chosen`."""
    return (
        BOT_SCOPES
        + (REQUESTS_BOT_SCOPES if Permission.REQUESTS in chosen else ())
        + (MENTION_BOT_SCOPES if mentions(chosen) else ())
    )


def bot_events(chosen: Allowed = BASE) -> tuple[str, ...]:
    """What the app is sent with `chosen`; nothing at all without `requests`."""
    if Permission.REQUESTS not in chosen:
        return ()
    return BOT_EVENTS + (MENTION_BOT_EVENTS if mentions(chosen) else ())


#: What the app is called when the assistant's name leaves nothing Slack would take.
SERVICE_NAME = "Keryx"
#: `display_information.name` takes 35 characters and `bot_user.display_name` 80 (Slack's
#: manifest reference): one name is used for both, so the smaller limit is the limit.
APP_NAME_MAX = 35
#: Punctuation a name keeps, beside letters, digits and spaces. Slack makes the bot's
#: username from the display name by keeping `a-z`, `0-9`, `-`, `_` and `.`; the
#: apostrophe is a name's own. Nothing Slack reads as markup is among them.
_NAME_MARKS = "-'’._"
#: Usernames Slack keeps for itself (it refuses a display name whose username is
#: reserved). The reference does not list them: these are Slackbot and the words a
#: message uses to mention everybody, compared with every mark taken out.
_RESERVED_USERNAMES = frozenset({"slackbot", "slack", "here", "channel", "everyone", "group"})


#: The colour Slack puts behind the app's name and description: the violet of the mark
#: (`docs/assets/keryx-mark.svg`). Slack takes a hex value with its hash, three or six
#: digits, and says nothing of contrast; it sets white text on it, and of the mark's three
#: colours this one stands furthest from white (5.7 to 1; the pink is 4.6, the orange
#: 2.8). Once a manifest has set one, Slack lets it be changed and never removed.
BACKGROUND_COLOR = "#7c3aed"


def app_name(assistant: str | None) -> str:
    """What the owner's Slack app and its bot are called: the assistant's name, fitted.

    The app is the assistant's presence in Slack, so it carries `ASSISTANT_NAME`, and the
    same name is what the owner is told to mention and what the app says of itself. The
    fitting, in order: only letters and digits of any script, spaces and `- ' . _` are
    kept, because the name is written into messages and nothing in it may be markup;
    runs of spaces become one; it is cut to `APP_NAME_MAX`. A name with no letter or
    digit left, or whose username Slack keeps for itself, is `SERVICE_NAME`.
    """
    kept = "".join(char if char.isalnum() or char in _NAME_MARKS else " "
                   if char.isspace() else "" for char in assistant or "")
    name = " ".join(kept.split())[:APP_NAME_MAX].rstrip()
    username = "".join(char for char in name.lower() if char.isalnum())
    if not username or username in _RESERVED_USERNAMES:
        return SERVICE_NAME
    return name


def app_manifest(chosen: Allowed = BASE, *, assistant: str = "") -> dict:
    """The Slack app each owner creates, for what they ticked, called after their
    assistant (`app_name`; with none given, `SERVICE_NAME`).

    The only place it is written down. Whatever is ticked, the install also issues the
    owner a user token (`user_scopes`): it is how Slack says who installed the app, and
    with neither `read` nor `send` its one scope lets it see nothing of theirs. With
    nothing ticked it is a bot that posts: nothing listens to Slack (no events, no
    redirect URL, no socket mode), and the Messages tab is on, read-only, because that
    tab is where a message to a member arrives and there is nothing to answer one typed
    there. With `requests` the tab takes messages, the bot subscribes to `message.im`,
    and Socket Mode is on, so no address of Keryx's is ever given to Slack; with `read`
    as well it is also sent the mentions of it (`app_mention`). `read` gives the user
    token the histories and search, `send` lets it post as them, and either gives it
    the lookups that find who is meant (`LOOKUP_USER_SCOPES`).
    """
    requests = Permission.REQUESTS in chosen
    name = app_name(assistant)
    manifest: dict = {
        "display_information": {
            "name": name,
            "description": "Messages from your Keryx phone assistant",
            "background_color": BACKGROUND_COLOR,
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": not requests,
            },
            "bot_user": {"display_name": name},
        },
        "oauth_config": {"scopes": {"bot": list(bot_scopes(chosen)),
                                    "user": list(user_scopes(chosen))}},
    }
    if requests:
        manifest["settings"] = {
            "event_subscriptions": {"bot_events": list(bot_events(chosen))},
            "socket_mode_enabled": True,
        }
    return manifest


def manifest_json(chosen: Allowed = BASE, *, assistant: str = "") -> str:
    """The manifest as JSON to paste into an existing app's App Manifest page."""
    return json.dumps(app_manifest(chosen, assistant=assistant), indent=2, ensure_ascii=False)


def new_app_url(chosen: Allowed = BASE, *, assistant: str = "") -> str:
    """The link that opens Slack's "create an app" page with `app_manifest` filled in."""
    manifest = json.dumps(app_manifest(chosen, assistant=assistant), separators=(",", ":"))
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
    the app for a user token: that is how the owner is known.
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

    Needs `im:history` for a direct message, which the app has with `requests` (it is
    what the `message.im` event rests on). Ok means it is; an error in `NOT_A_PARTY` means it
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


# --- files -------------------------------------------------------------------------------


class FileRefused(ValueError):
    """A path that will not be sent as a file, in one sentence. Slack was not asked."""


@dataclass(frozen=True)
class LocalFile:
    """A file on this machine that may be sent: where it is, the name Slack shows it
    under, and how many bytes it is."""

    path: Path
    name: str
    size: int


def local_file(path: str | Path, *, private: Iterable[Path] = ()) -> LocalFile:
    """`path` as a file that may be sent, or `FileRefused` saying why not.

    A regular file (a link to one counts), readable, not empty, and no larger than Slack
    takes. Slack refuses a length of 0 (`missing_argument`, by its reference), so an empty
    file is refused here with a sentence instead. Nothing inside a directory in `private`
    is sent, wherever a link to it is: that is how Keryx's own configuration, with its
    tokens and the PIN, is kept out of Slack by mistake. It stops a mistake, not a shell.
    """
    shown = str(path)[:200]
    try:
        found = Path(path).expanduser().resolve()
        status = found.stat()
    except (OSError, RuntimeError, ValueError):
        raise FileRefused(f"there is no file at {shown}") from None
    if not stat.S_ISREG(status.st_mode):
        raise FileRefused(f"{shown} is not a regular file")
    for directory in private:
        if found.is_relative_to(Path(directory).resolve()):
            raise FileRefused(f"{shown} is in Keryx's own configuration directory, and "
                              "nothing in it is ever sent")
    if not os.access(found, os.R_OK):
        raise FileRefused(f"{shown} cannot be read")
    if status.st_size == 0:
        raise FileRefused(f"{shown} is empty, and Slack takes no empty file")
    if status.st_size > MAX_FILE_BYTES:
        raise FileRefused(f"{shown} is larger than the 1 GB Slack takes in one file")
    return LocalFile(path=found, name=found.name, size=status.st_size)


def local_files(paths: Sequence[str | Path], *, private: Iterable[Path] = ()) -> list[LocalFile]:
    """Every path as a file that may be sent; `FileRefused` for the first that may not,
    and for more of them than Slack puts in one message."""
    if len(paths) > MAX_FILES:
        raise FileRefused(f"that is {len(paths)} files, and Slack takes {MAX_FILES} in one "
                          "message: send them in several")
    private = tuple(private)
    return [local_file(path, private=private) for path in paths]


def _send_file(url: str, path: Path, size: int) -> None:
    """The file's bytes, to the address Slack gave for them, read as they are sent.

    Raw bytes, which the reference allows beside a multipart form, and no token: the
    address is its own authorization. Slack answers 200 when it has them, and anything
    else is a failure.
    """
    with path.open("rb") as handle:
        request = urllib.request.Request(
            url,
            data=handle,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
        )
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_S) as response:
            if response.status != 200:
                # Without the address: it is never in anything that could be printed.
                raise urllib.error.HTTPError("", response.status, "", response.headers, None)


@dataclass(frozen=True)
class Uploaded(SlackResult):
    """A file Slack holds and has not shared yet: its id, and the title it will carry.
    With `error`, why it never got there. The address it was sent to is not kept."""

    file_id: str = ""
    title: str = ""


class SlackFiles:
    """Files as the app's bot, in Slack's three steps: `upload` is the first two
    (`files.getUploadURLExternal`, then the bytes to the address it answers with) and
    `share` the third (`files.completeUploadExternal`).

    Two calls, so that a file Slack will not take is found out before the words that go
    with it are posted. A file that is uploaded and never shared is discarded by Slack.
    `files.getUploadURLExternal` takes a form and ignores a JSON body, by its reference.
    Every failure is a code: nothing Slack or the network said is repeated or logged, and
    neither is the address, which is all the authorization the bytes need.
    """

    def __init__(self, token: str, *, form=_post_form, post=_post, send=_send_file) -> None:
        self._token = token
        self._form = form
        self._post = post
        self._send = send

    async def upload(self, file: LocalFile) -> Uploaded:
        """Hand `file` to Slack, which then holds it unshared."""
        result, error = await asyncio.to_thread(
            _ask, self._form, GET_UPLOAD_URL,
            {"filename": file.name, "length": file.size}, self._token,
        )
        if error:
            return Uploaded(error=error)
        url, file_id = result.get("upload_url"), result.get("file_id")
        if not (isinstance(url, str) and url.startswith("https://")
                and isinstance(file_id, str) and _FILE_ID.fullmatch(file_id)):
            return Uploaded(error=UNKNOWN_ERROR)
        try:
            await asyncio.to_thread(self._send, url, file.path, file.size)
        except urllib.error.HTTPError as exc:
            return Uploaded(error=f"{_HTTP}{exc.code}")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            return Uploaded(error=UNREACHABLE)
        return Uploaded(file_id=file_id, title=file.name)

    async def share(
        self, uploaded: Sequence[Uploaded], *, channel: str, thread_ts: str | None = None
    ) -> SlackResult:
        """Share what was uploaded into `channel`, a conversation's id, as one message;
        with `thread_ts`, as a reply in that thread (its first message's timestamp)."""
        payload: dict = {
            "files": [{"id": each.file_id, "title": each.title} for each in uploaded],
            "channel_id": channel,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        _, error = await asyncio.to_thread(
            _ask, self._post, COMPLETE_UPLOAD_URL, payload, self._token)
        return SlackResult(error=error)
