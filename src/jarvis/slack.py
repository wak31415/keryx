"""Sending William something on Slack, from the voice model itself.

The subagents already have Slack: the `slack-research` MCP server from the
`auto-research` skill is configured user-scope, so every Claude CLI the runner spawns
inherits it. The voice model has no such thing — it only has the tools this process
registers — so this module gives it one, using the very same bot token and DM channel
rather than a second Slack app.

Those credentials live in the Claude CLI config, next to the MCP server that uses them.
`slack_credentials` reads the env pair first (so `.env` can override) and falls back to
that config, which keeps one Slack app and one place to rotate it.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

log = logging.getLogger("jarvis.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT_S = 15.0
#: Where the Claude CLI keeps its user-scope MCP servers, `slack-research` among them.
CLAUDE_CONFIG = Path.home() / ".claude.json"
MCP_SERVER_NAME = "slack-research"


class SlackSender(Protocol):
    """Anything that can put a line in front of William on Slack."""

    async def send(self, text: str) -> bool:
        """True when Slack accepted the message."""
        ...


def slack_credentials(
    token: str | None = None,
    channel: str | None = None,
    *,
    config_path: Path = CLAUDE_CONFIG,
) -> tuple[str, str] | None:
    """The bot token and DM channel: the given pair, else the skill's MCP server config."""
    if token and channel:
        return token, channel
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    env = config.get("mcpServers", {}).get(MCP_SERVER_NAME, {}).get("env", {})
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
    """`SlackSender` over `chat.postMessage`."""

    def __init__(self, token: str, channel: str, *, post=_post) -> None:
        self._token = token
        self._channel = channel
        self._post = post

    async def send(self, text: str) -> bool:
        try:
            result = await asyncio.to_thread(
                self._post, POST_MESSAGE_URL, {"channel": self._channel, "text": text}, self._token
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("could not post to Slack: %s", exc)
            return False
        if not result.get("ok"):
            # Slack reports failure in the body with a 200, e.g. {"ok": false, "error": …}
            log.warning("Slack refused the message: %s", result.get("error"))
            return False
        return True
