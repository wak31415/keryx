"""Sending the owner something on Slack, from the voice model itself.

The client behind the `send_to_slack` plugin (`keryx.plugins.slack`). The subagents can
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
"""

import asyncio
import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from keryx.config.files import claude_user_config

log = logging.getLogger("keryx.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
AUTH_TEST_URL = "https://slack.com/api/auth.test"
LOOKUP_BY_EMAIL_URL = "https://slack.com/api/users.lookupByEmail"
#: Slack's own page for making an app; `new_app=1&manifest_json=…` opens it filled in.
NEW_APP_URL = "https://api.slack.com/apps"
REQUEST_TIMEOUT_S = 15.0

#: What the app may do. `chat:write` posts, and with a member id as the channel it posts
#: to that member's conversation with the app, so no `im:write`. `users:read.email` is
#: `users.lookupByEmail`, and Slack grants it only together with `users:read`.
BOT_SCOPES = ("chat:write", "users:read", "users:read.email")

#: In place of Slack's error code when Slack was never reached, or did not answer as Slack.
UNREACHABLE = "unreachable"
UNKNOWN_ERROR = "unknown_error"
#: And when something on the way answered with an HTTP error: `http_429`, `http_503`.
_HTTP = "http_"
#: The shape of an error code. Anything else in `error` is not repeated.
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
_MEMBER_ID = re.compile(r"[A-Z0-9]{2,32}")

#: The refusals setup meets often enough to say in words; the code always follows.
_EXPLAINED = {
    "invalid_auth": "Slack does not recognise that token",
    "missing_scope": "the app lacks a permission this needs",
    "users_not_found": "nobody in that workspace has that address",
    "channel_not_found": "Slack does not know that channel or member",
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
    `send_to_slack`, and a Codex subagent, which is handed the same server explicitly. The
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


def _get(url: str, params: dict, token: str) -> dict:
    """One blocking GET to the Slack Web API. The token is a header, never in the address."""
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


# --- connecting --------------------------------------------------------------------------


def app_manifest() -> dict:
    """The Slack app each owner creates: a bot that posts, and can find them by address.

    The only place it is written down. Nothing here listens to Slack — no events, no
    redirect URL, no socket mode — and the Messages tab is on, read-only, because that tab
    is where a message to a member arrives and there is nothing to answer one typed there.
    """
    return {
        "display_information": {
            "name": "Keryx",
            "description": "Messages from your Keryx phone assistant",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": True,
            },
            "bot_user": {"display_name": "Keryx"},
        },
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
    }


def new_app_url() -> str:
    """The link that opens Slack's "create an app" page with `app_manifest` filled in."""
    manifest = json.dumps(app_manifest(), separators=(",", ":"))
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


@dataclass(frozen=True)
class Workspace(SlackResult):
    """`auth.test`: the workspace a token belongs to."""

    team: str = ""
    url: str = ""


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


def auth_test(token: str, *, post=_post) -> Workspace:
    """Whether Slack takes `token`, and the workspace it belongs to. Needs no scope."""
    result, error = _ask(post, AUTH_TEST_URL, {}, token)
    if error:
        return Workspace(error=error)
    return Workspace(team=_line(result.get("team")), url=_line(result.get("url")))


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
    """`SlackSender` over `chat.postMessage`.

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
        for piece in markdown_pieces(text):
            plain = {
                "channel": self._channel,
                "text": piece,
                "unfurl_links": False,
                "unfurl_media": False,
            }
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
                return False
            if not result.get("ok"):
                # Slack reports failure in the body with a 200, e.g. {"ok": false, "error": …}
                log.warning("Slack refused the message: %s", result.get("error"))
                return False
        return True
