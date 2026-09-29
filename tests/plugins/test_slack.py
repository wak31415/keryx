"""The `send_to_slack` plugin, and `slack_route`, which everything else asks about Slack."""

import json

import pytest

from jarvis import plugins
from jarvis.agents.base import render_subagent_suffix
from jarvis.agents.claude import CLAUDE_AUTH
from jarvis.agents.session import AgentContext
from jarvis.integrations.slack import SlackWebApi
from jarvis.issues import IssueReporting
from jarvis.plugins.slack import (
    FAILED_MESSAGE,
    send_to_slack_tool,
    slack_route,
    slack_sender,
)
from jarvis.tasks.models import Task, TaskKind
from jarvis.tools.builtin_common import PIN_REQUIRED_MESSAGE
from jarvis.tools.custom import ToolUnavailable
from jarvis.trust import TrustLevel
from plugins.helpers import call, loaded, loading, names, offered, refusal

NAME = "send_to_slack"


class FakeSlack:
    """A `SlackSender` that records what it was asked to send."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


@pytest.fixture
def slack_settings(settings):
    return settings.model_copy(update={"slack_bot_token": "xoxb-test-token"})


def tool(settings, sender=None, **values):
    path = plugins.write_config(settings, NAME, {"channel_id": "D123", **values})
    with loading(settings):
        return send_to_slack_tool(path, sender=sender)


def description(settings) -> str:
    return " ".join(tool(settings, FakeSlack()).description.split())


# --- the tool ---------------------------------------------------------------------------


async def test_send_to_slack_sends_the_message(settings):
    slack = FakeSlack()
    registry = offered(tool(settings, slack), settings)

    assert await call(registry, NAME, {"message": "task 3 is done"}) == {"status": "sent"}
    assert slack.sent == ["task 3 is done"]


async def test_send_to_slack_needs_a_message(settings):
    registry = offered(tool(settings, FakeSlack()), settings)

    assert "message is required" in (await call(registry, NAME, {}))["error"]


async def test_a_refused_slack_message_comes_back_as_an_error(settings):
    registry = offered(tool(settings, FakeSlack(ok=False)), settings)

    assert (await call(registry, NAME, {"message": "x"})) == {"error": FAILED_MESSAGE}


async def test_it_needs_the_pin_because_it_writes_as_them(settings):
    slack = FakeSlack()
    registry = offered(tool(settings, slack), settings)

    result = await call(registry, NAME, {"message": "x"}, trust=TrustLevel.NONE)

    assert result == {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}
    assert slack.sent == []


def test_the_description_tells_the_model_to_wait_to_be_asked(settings):
    text = description(settings)

    assert "Only call it when they have explicitly asked" in text
    assert "Never call it unasked" in text
    assert "Needs the PIN" in text


def test_the_description_names_whom_it_sends_to(settings):
    assert description(settings).startswith("Send the owner a written message on Slack")
    ada = settings.model_copy(update={"owner_name": "Ada"})
    assert description(ada).startswith("Send Ada a written message on Slack")


def test_the_description_does_not_invite_a_written_copy_of_the_answer(settings):
    """Repeating in writing what was just said out loud is the commonest unasked send.

    But only unasked: a flat ban on it once sent "Slack me that" to a subagent instead.
    """
    text = description(settings)

    assert "never volunteer a written copy of something you have already said" in text
    assert "when they ask for what you just said in writing, that is exactly what" in text
    assert '"Slack me that"' in text


# --- installed and loaded ----------------------------------------------------------------


def test_installed_with_a_token_it_loads_from_its_template(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123"})
    plugins.install(slack_settings, NAME)

    assert NAME in names(slack_settings)


def test_without_a_token_the_file_is_refused_with_the_command_that_fixes_it(settings):
    plugins.write_config(settings, NAME, {"channel_id": "D123"})
    plugins.install(settings, NAME)

    assert "jarvis config set SLACK_BOT_TOKEN --stdin" in refusal(settings, NAME)


def test_a_token_saved_after_startup_is_found_in_the_store(settings, monkeypatch):
    """The service's settings are from startup; the store is read for a secret it lacks."""
    monkeypatch.setattr(plugins, "current_settings",
                        lambda: settings.model_copy(update={"slack_bot_token": "xoxb-later"}))
    plugins.write_config(settings, NAME, {"channel_id": "D123"})
    plugins.install(settings, NAME)

    assert slack_route(settings).token == "xoxb-later"


def test_the_token_can_come_from_the_named_mcp_server(settings, monkeypatch, tmp_path):
    """Read from the Claude CLI's own config, wherever `CLAUDE_CONFIG_DIR` puts it."""
    config = tmp_path / "claude" / ".claude.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"chat": {"env": {
        "SLACK_BOT_TOKEN": "xoxb-server", "SLACK_CHANNEL_ID": "D999"}}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config.parent))
    monkeypatch.setattr(plugins, "current_settings", lambda: settings)
    plugins.write_config(settings, NAME, {"mcp_server": "chat"})
    plugins.install(settings, NAME)

    route = slack_route(settings)

    assert (route.token, route.channel, route.mcp_server) == ("xoxb-server", "D999", "chat")
    assert NAME in names(settings)


def test_a_toml_edit_is_seen_on_the_next_load(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123"})
    plugins.install(slack_settings, NAME)
    plugins.config_path(slack_settings, NAME).write_text('channel_id = "has space"\n')

    assert "channel_id must be a bare word" in refusal(slack_settings, NAME)


def test_a_toml_that_does_not_parse_is_refused_with_why(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123"})
    plugins.install(slack_settings, NAME)
    plugins.config_path(slack_settings, NAME).write_text("channel_id = \n")

    assert "does not parse" in refusal(slack_settings, NAME)


def test_the_tool_itself_refuses_a_file_it_cannot_read(settings, tmp_path):
    bad = tmp_path / "send_to_slack.toml"
    bad.write_text("channel_id = [\n")

    with pytest.raises(ToolUnavailable, match="does not parse"):
        send_to_slack_tool(bad, sender=FakeSlack())


# --- the route: what the alert, the subagent and Codex ask ------------------------------


def test_with_the_plugin_off_there_is_no_route_whatever_the_token(slack_settings):
    assert slack_route(slack_settings) is None
    assert slack_sender(slack_settings) is None


def test_with_the_plugin_on_the_route_carries_the_token_channel_and_server(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123", "mcp_server": "chat"})
    plugins.install(slack_settings, NAME)

    route = slack_route(slack_settings)

    assert (route.token, route.channel, route.mcp_server) == ("xoxb-test-token", "D123", "chat")
    assert isinstance(slack_sender(slack_settings), SlackWebApi)


def test_turned_off_the_route_is_gone_at_once(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123"})
    plugins.install(slack_settings, NAME)
    plugins.remove(slack_settings, NAME)

    assert slack_route(slack_settings) is None


def test_a_route_with_no_token_has_no_sender(settings, monkeypatch):
    monkeypatch.setattr(plugins, "current_settings", lambda: settings)
    plugins.write_config(settings, NAME, {"channel_id": "D123", "mcp_server": "chat"})
    plugins.install(settings, NAME)

    assert slack_route(settings).mcp_server == "chat"
    assert slack_sender(settings) is None


def test_the_subagent_hears_about_slack_only_through_the_route(slack_settings):
    task = Task(id=7, kind=TaskKind.AGENT, description="review the diff")
    off = AgentContext.build(task, slack_settings, auth=CLAUDE_AUTH, model=None)
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123", "mcp_server": "chat"})
    plugins.install(slack_settings, NAME)
    on = AgentContext.build(task, slack_settings, auth=CLAUDE_AUTH, model=None)

    assert "`chat` MCP server" not in off.instructions
    assert "`chat` MCP server" in on.instructions
    assert on.instructions == render_subagent_suffix(
        task, slack_mcp_server="chat", owner=slack_settings.owner_label,
        tools_dir=slack_settings.custom_tools_dir,
        issues=IssueReporting.from_settings(slack_settings),
    )


def test_every_template_loads_nothing_it_should_not(slack_settings):
    """Only the plugins that are on are in the directory a call loads."""
    assert loaded(slack_settings).tools == []
