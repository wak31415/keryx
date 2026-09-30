"""The `send_to_slack` plugin, and the one question everything else asks about Slack.

The tool posts to the owner's DM, as their own bot, once they have asked for something in
writing — so it needs the PIN. Its settings are `send_to_slack.toml` (`channel_id`,
`mcp_server`); the bot token is `SLACK_BOT_TOKEN` in `secrets.toml`, or the named MCP
server's own `env` in `~/.claude.json` (`integrations.slack.slack_credentials`).

Three things besides the tool want Slack, and each asks `slack_route(settings)` rather than
reading a setting: the PIN-lockout alert (`notify.pin_alert`, at alert time, so turning
Slack on needs no restart), the subagent's Slack paragraph (`agents.base`), and the MCP
server a Codex subagent is handed (`agents.codex`). With the plugin off there is no route,
and none of them reaches Slack.
"""

from dataclasses import dataclass
from pathlib import Path

from keryx import plugins
from keryx.config import Settings
from keryx.integrations.slack import SlackSender, SlackWebApi, slack_credentials

TOOL = "send_to_slack"

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
    """What the PIN-lockout alert posts with: None while the plugin is off or has no token."""
    route = slack_route(settings)
    return route.sender() if route is not None else None


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
        "comes back sent, say so in a few words, once. For anything a subagent produced "
        "(a file, a plot, a report), dispatch the sending to Claude instead: it can attach "
        "the file itself."
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


def send_to_slack_tool(config_path: Path, *, sender: SlackSender | None = None):
    """The tool `send_to_slack.py` defines, configured by the TOML at `config_path`."""
    from keryx.tools.custom import CustomTool, ToolUnavailable

    settings = plugins.loading_settings()
    try:
        values = plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise ToolUnavailable(str(error)) from None
    if sender is None:
        sender = _route(settings, values).sender()
    if sender is None:
        raise ToolUnavailable(
            "no bot token and channel: `keryx config set SLACK_BOT_TOKEN --stdin`, and "
            f"channel_id in {config_path.name} (or an mcp_server whose config has both)"
        )

    async def send_to_slack(ctx, arguments: dict) -> dict:
        message = str(arguments.get("message") or "").strip()
        if not message:
            return {"error": "message is required: say what to send"}
        if not await sender.send(message):
            return {"error": FAILED_MESSAGE}
        return {"status": "sent"}

    return CustomTool(
        name=TOOL,
        description=description(settings.owner_label),
        parameters=PARAMETERS,
        handler=send_to_slack,
        needs_pin=True,
    )
