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
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from keryx.config.files import claude_user_config

log = logging.getLogger("keryx.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT_S = 15.0
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
