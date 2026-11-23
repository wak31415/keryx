"""The `slack_report` plugin, and `slack_route`, which everything else asks about Slack."""

import json

import pytest

from keryx import plugins
from keryx.agents.base import render_subagent_suffix
from keryx.agents.claude import CLAUDE_AUTH
from keryx.agents.session import AgentContext
from keryx.integrations.slack import Access, SlackWebApi
from keryx.issues import IssueReporting
from keryx.plugins.slack import (
    FAILED_MESSAGE,
    SlackWorkspace,
    connected,
    report_route,
    slack_report_tool,
    slack_route,
    slack_sender,
    workspace,
    workspaces,
)
from keryx.tasks.models import Task, TaskKind
from keryx.tools.builtin_common import PIN_REQUIRED_MESSAGE
from keryx.tools.custom import ToolUnavailable
from keryx.trust import TrustLevel
from plugins.helpers import call, loaded, loading, names, offered, refusal

NAME = "slack_report"


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
        return slack_report_tool(path, sender=sender)


def description(settings) -> str:
    return " ".join(tool(settings, FakeSlack()).description.split())


# --- the tool ---------------------------------------------------------------------------


async def test_slack_report_sends_the_message(settings):
    slack = FakeSlack()
    registry = offered(tool(settings, slack), settings)

    assert await call(registry, NAME, {"message": "task 3 is done"}) == {"status": "sent"}
    assert slack.sent == ["task 3 is done"]


async def test_slack_report_needs_a_message(settings):
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


async def test_with_one_workspace_there_is_nothing_to_choose(settings):
    one = tool(settings, FakeSlack())

    assert set(one.parameters["properties"]) == {"message"}
    assert "more than one Slack workspace" not in one.description


async def test_with_several_workspaces_one_may_be_named_and_the_usual_one_is_the_default(
    settings,
):
    first, work = FakeSlack(), FakeSlack()
    path = plugins.write_config(settings, NAME, {"channel_id": "D123"})
    with loading(settings):
        several = slack_report_tool(path, senders={"default": first, "work": work})
    registry = offered(several, settings)

    assert several.parameters["properties"]["workspace"]["enum"] == ["default", "work"]
    assert several.parameters["required"] == ["message"]
    assert "more than one Slack workspace" in several.description
    assert await call(registry, NAME, {"message": "to the usual"}) == {"status": "sent"}
    assert await call(registry, NAME, {"message": "to work", "workspace": " Work "}) == {
        "status": "sent"
    }
    assert (first.sent, work.sent) == (["to the usual"], ["to work"])
    refused = await call(registry, NAME, {"message": "x", "workspace": "lab"})
    assert refused == {"error": "there is no Slack workspace called lab: one of default, work"}
    assert (first.sent, work.sent) == (["to the usual"], ["to work"])


# --- installed and loaded ----------------------------------------------------------------


def test_installed_with_a_token_it_loads_from_its_template(slack_settings):
    plugins.write_config(slack_settings, NAME, {"channel_id": "D123"})
    plugins.install(slack_settings, NAME)

    assert NAME in names(slack_settings)


def test_without_a_token_the_file_is_refused_with_the_command_that_fixes_it(settings):
    plugins.write_config(settings, NAME, {"channel_id": "D123"})
    plugins.install(settings, NAME)

    assert "keryx config set SLACK_BOT_TOKEN --stdin" in refusal(settings, NAME)


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
    bad = tmp_path / "slack_report.toml"
    bad.write_text("channel_id = [\n")

    with pytest.raises(ToolUnavailable, match="does not parse"):
        slack_report_tool(bad, sender=FakeSlack())


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


# --- workspaces ---------------------------------------------------------------------------


WORK = {"access": "converse", "owner_id": "U0WORK", "team": "Acme", "team_id": "T0WORK",
        "channel_id": "U0WORK", "bot_user_id": "U0WORKBOT"}
FIRST = {"channel_id": "U0ME", "team": "Home", "team_id": "T0HOME"}


@pytest.fixture
def two_workspaces(slack_settings):
    """The first workspace at announce, and `work` at converse with both its tokens."""
    settings = slack_settings.model_copy(update={
        "slack_workspace_tokens": {"work": {"bot": "xoxb-work", "app": "xapp-work"}},
    })
    plugins.write_config(settings, NAME, {**FIRST, "workspaces": {"work": WORK}})
    plugins.install(settings, NAME)
    return settings


def test_an_install_from_before_workspaces_is_the_default_one_at_announce(slack_settings):
    """One token and one destination, as every install has had: nothing to migrate."""
    slack_settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(slack_settings, NAME).write_text('channel_id = "D123"\n')
    plugins.install(slack_settings, NAME)

    [only] = connected(slack_settings)

    assert (only.name, only.access, only.channel) == ("default", Access.ANNOUNCE, "D123")
    assert only.token("bot") == "xoxb-test-token" and only.token("app") is None
    assert only.sends and not only.listens and only.missing == ()
    assert slack_route(slack_settings).channel == "D123"


def test_each_workspace_has_its_own_level_tokens_owner_and_destination(two_workspaces):
    first, work = workspaces(two_workspaces)

    assert (first.name, work.name) == ("default", "work")
    assert (work.access, work.owner_id, work.channel) == (Access.CONVERSE, "U0WORK", "U0WORK")
    assert (work.token("bot"), work.token("app")) == ("xoxb-work", "xapp-work")
    # What the owner reads: Slack's own name for the first, their word for a further one.
    assert work.label == "work (Acme)" and first.label == "Home"
    assert work.listens and not first.listens
    assert workspace(two_workspaces, "work") == work
    assert workspace(two_workspaces, "nowhere") is None


def test_a_workspaces_tokens_are_never_in_its_repr(two_workspaces):
    assert "xoxb" not in repr(workspaces(two_workspaces)) and "xapp" not in repr(
        workspaces(two_workspaces)
    )


@pytest.mark.parametrize(
    ("access", "tokens", "owner", "team", "bot", "missing", "listens"),
    [
        ("announce", {"bot": "b"}, "", "", "", (), False),
        ("converse", {"bot": "b"}, "U1", "T1", "B1", ("its app token",), False),
        ("converse", {"bot": "b", "app": "a"}, "", "T1", "B1", ("your member id",), False),
        ("converse", {"bot": "b", "app": "a"}, "U1", "", "B1", ("the workspace's id",), False),
        ("converse", {"bot": "b", "app": "a"}, "U1", "T1", "",
         ("the app's own member id",), False),
        ("converse", {"bot": "b", "app": "a"}, "U1", "T1", "B1", (), True),
        ("read", {"bot": "b", "app": "a"}, "U1", "T1", "B1", ("its user token",), True),
        ("read", {"bot": "b", "app": "a", "user": "u"}, "U1", "T1", "B1", (), True),
        ("announce", {}, "", "", "", ("its bot token",), False),
    ],
)
def test_a_workspace_says_what_its_level_needs_and_lacks(
    access, tokens, owner, team, bot, missing, listens
):
    """Above announce a message is held to the owner's id, the workspace's, and a mention
    of the app's own: with any missing there is nothing to hold it to, and nothing is
    heard."""
    one = SlackWorkspace("w", Access(access), channel="U1", owner_id=owner, team_id=team,
                         bot_user_id=bot, tokens=tokens)

    assert one.missing == missing and one.listens is listens
    assert one.reads is (access == "read" and "user" in tokens)


def test_the_first_workspace_with_no_name_from_slack_is_called_what_it_is():
    assert SlackWorkspace("default").label == "your first workspace"
    assert SlackWorkspace("work").label == "work"


def test_a_workspace_with_nowhere_to_post_says_so_and_sends_nothing():
    one = SlackWorkspace("w", tokens={"bot": "b"})

    assert one.missing == ("somewhere to post",) and not one.sends and one.poster() is None


def test_with_the_plugin_off_no_workspace_is_connected(two_workspaces):
    plugins.remove(two_workspaces, NAME)

    assert connected(two_workspaces) == []
    assert workspace(two_workspaces, "work") is None
    # Their settings are still read, for `doctor` to describe.
    assert [each.name for each in workspaces(two_workspaces)] == ["default", "work"]


def test_a_workspace_without_a_bot_token_is_not_connected(two_workspaces):
    settings = two_workspaces.model_copy(update={"slack_workspace_tokens": {}})

    assert [each.name for each in connected(settings)] == ["default"]


def test_the_first_workspaces_other_tokens_are_the_plain_settings(slack_settings):
    settings = slack_settings.model_copy(update={
        "slack_app_token": "xapp-1", "slack_user_token": "xoxp-1",
    })
    plugins.write_config(settings, NAME, {"channel_id": "U0ME", "access": "read",
                                          "owner_id": "U0ME", "team_id": "T0HOME",
                                          "bot_user_id": "U0KERYX"})

    [first] = workspaces(settings)

    assert (first.token("app"), first.token("user")) == ("xapp-1", "xoxp-1")
    assert first.missing == () and first.listens


# --- where a task's report goes -----------------------------------------------------------


def slack_task(**fields) -> Task:
    return Task(id=7, kind=TaskKind.AGENT, description="x", **fields)


def test_a_first_report_is_a_message_of_its_own_in_the_first_workspace(two_workspaces):
    route = report_route(two_workspaces, slack_task())

    assert (route.workspace, route.thread_ts, route.broadcast) == ("default", None, False)
    assert route.poster._channel == "U0ME" and route.poster._token == "xoxb-test-token"


def test_a_task_asked_for_on_slack_reports_in_its_own_thread_in_its_own_workspace(
    two_workspaces,
):
    task = slack_task(origin_channel="slack", slack_workspace="work",
                      slack_team_id="T0WORK", slack_channel="D0WORK",
                      slack_thread_ts="1700000000.000100")

    route = report_route(two_workspaces, task)

    assert (route.workspace, route.thread_ts, route.broadcast) == (
        "work", "1700000000.000100", False
    )
    assert route.poster._channel == "D0WORK" and route.poster._token == "xoxb-work"


def test_a_later_report_of_a_phone_task_is_a_reply_under_its_first_shown_in_the_dm(
    two_workspaces,
):
    task = slack_task(origin_channel="phone", slack_workspace="default",
                      slack_team_id="T0HOME", slack_channel="D0ME",
                      slack_thread_ts="1700000000.000100")

    route = report_route(two_workspaces, task)

    assert (route.workspace, route.thread_ts, route.broadcast) == (
        "default", "1700000000.000100", True
    )
    assert route.poster._channel == "D0ME"


def test_a_task_asked_for_in_a_workspace_that_is_gone_is_reported_nowhere_on_slack(
    two_workspaces,
):
    """Never to another workspace: the first one may be somebody else's employer."""
    task = slack_task(origin_channel="slack", slack_workspace="left", slack_channel="D0GONE",
                      slack_thread_ts="1700000000.000100")

    assert report_route(two_workspaces, task) is None


def test_a_phone_task_whose_thread_is_in_a_workspace_that_is_gone_goes_where_it_always_would(
    two_workspaces,
):
    task = slack_task(origin_channel="phone", slack_workspace="left", slack_channel="D0GONE",
                      slack_thread_ts="1700000000.000100")

    route = report_route(two_workspaces, task)

    assert (route.workspace, route.thread_ts, route.usual) == ("default", None, None)


def test_a_thread_that_is_gone_falls_back_inside_its_own_workspace_only(two_workspaces):
    typed = slack_task(origin_channel="slack", slack_workspace="work",
                      slack_team_id="T0WORK", slack_channel="D0WORK",
                       slack_thread_ts="1700000000.000100")
    spoken = slack_task(origin_channel="phone", slack_workspace="default",
                        slack_team_id="T0HOME", slack_channel="D0ME",
                        slack_thread_ts="1700000000.000100")

    # Typed on Slack: back to the owner's own direct messages with that workspace's app.
    assert report_route(two_workspaces, typed).usual._token == "xoxb-work"
    assert report_route(two_workspaces, typed).usual._channel == "U0WORK"
    # Spoken: to where the first workspace's reports go.
    assert report_route(two_workspaces, spoken).usual._token == "xoxb-test-token"
    assert report_route(two_workspaces, spoken).usual._channel == "U0ME"


def test_with_the_plugin_off_a_report_goes_nowhere(two_workspaces):
    plugins.remove(two_workspaces, NAME)

    assert report_route(two_workspaces, slack_task()) is None


def test_installed_with_two_workspaces_the_tool_offers_both(two_workspaces):
    [(_, loaded_tool)] = loaded(two_workspaces).tools

    assert loaded_tool.parameters["properties"]["workspace"]["enum"] == ["default", "work"]


def test_a_further_workspace_that_cannot_post_is_not_offered(two_workspaces):
    settings = two_workspaces.model_copy(update={"slack_workspace_tokens": {}})

    [(_, loaded_tool)] = loaded(settings).tools

    assert "workspace" not in loaded_tool.parameters["properties"]


def test_the_word_for_a_task_typed_on_slack_is_one_word_everywhere():
    from keryx.agents.base import SLACK_ORIGIN
    from keryx.plugins.slack import SLACK_CHANNEL

    assert SLACK_ORIGIN == SLACK_CHANNEL == "slack"


def test_a_workspace_connected_since_startup_is_found_in_the_store(two_workspaces):
    """As the billing keys are: the running service posts to it with no restart."""
    from keryx.config.store import ConfigStore

    ConfigStore().set({"SLACK_WORKSPACE_TOKENS": {
        "work": {"bot": "xoxb-work-replaced"}, "play": {"bot": "xoxb-play"},
    }})
    plugins.write_config(two_workspaces, NAME, {"workspaces": {
        "work": WORK, "play": {"channel_id": "U0PLAY"},
    }})

    found = {each.name: each for each in connected(two_workspaces)}

    assert set(found) == {"default", "work", "play"}
    assert found["play"].token("bot") == "xoxb-play"
    # What the store holds now wins over what this process started with.
    assert found["work"].token("bot") == "xoxb-work-replaced"


def test_what_is_on_file_is_keryxs_own_and_never_the_mcp_servers(settings, monkeypatch, tmp_path):
    from keryx.plugins.slack import on_file

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    env = {"SLACK_BOT_TOKEN": "xoxb-the-servers", "SLACK_CHANNEL_ID": "D0SERVER"}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"chat": {"env": env}}}))
    monkeypatch.setattr(plugins, "current_settings", lambda: settings)
    plugins.write_config(settings, NAME, {"mcp_server": "chat"})

    own = on_file(settings, "default")
    [sending] = workspaces(settings)

    assert (own.token("bot"), own.channel) == (None, "")
    assert (sending.token("bot"), sending.channel) == ("xoxb-the-servers", "D0SERVER")


# --- answered where it was asked -------------------------------------------------------------


def test_a_task_asked_for_in_a_shared_conversation_is_answered_in_that_thread(two_workspaces):
    """The owner's ruling: the answer goes where the question was, and its members read it."""
    task = slack_task(origin_channel="slack", slack_workspace="work",
                      slack_team_id="T0WORK", slack_channel="C0SHARED",
                      slack_thread_ts="1700000000.000100")

    route = report_route(two_workspaces, task)

    assert (route.workspace, route.thread_ts, route.broadcast) == (
        "work", "1700000000.000100", False
    )
    assert (route.poster._channel, route.poster._token) == ("C0SHARED", "xoxb-work")
    # Only a thread that is gone for good falls back, and then to the owner alone.
    assert route.usual._channel == "U0WORK"


def test_a_slack_task_with_no_thread_is_posted_nowhere(two_workspaces):
    assert report_route(two_workspaces, slack_task(origin_channel="slack",
                                                   slack_workspace="work")) is None


# --- a name is not a workspace: the team is what a thread belongs to ---------------------------


def reconnected(settings):
    """`default` logged out of T0HOME and logged in to another team, as the first workspace."""
    plugins.write_config(settings, NAME, {
        "channel_id": "U0BOWNER", "owner_id": "U0BOWNER", "team": "Team B",
        "team_id": "T0TEAMB", "bot_user_id": "U0BBOT", "access": "read"})
    return settings


def test_a_task_asked_for_in_one_team_is_never_reported_into_another_under_the_same_name(
    two_workspaces,
):
    """Its thread is in team A; what is connected as `default` now is team B. Not into the
    thread with team B's token, and not to team B's owner when Slack says the thread is
    gone: nowhere on Slack."""
    task = slack_task(origin_channel="slack", slack_workspace="default",
                      slack_team_id="T0HOME", slack_channel="C0TEAMA",
                      slack_thread_ts="1700000000.000100")
    assert report_route(two_workspaces, task) is not None

    assert report_route(reconnected(two_workspaces), task) is None


def test_a_phone_tasks_thread_in_a_team_that_is_gone_is_left_and_a_new_one_started(
    two_workspaces,
):
    task = slack_task(origin_channel="phone", slack_workspace="default",
                      slack_team_id="T0HOME", slack_channel="D0ME",
                      slack_thread_ts="1700000000.000100")

    route = report_route(reconnected(two_workspaces), task)

    assert (route.workspace, route.thread_ts, route.usual, route.team_id) == (
        "default", None, None, "T0TEAMB")
    assert route.poster._channel == "U0BOWNER"


def test_a_route_carries_the_team_it_posts_in_for_the_thread_it_may_start(two_workspaces):
    assert report_route(two_workspaces, slack_task()).team_id == "T0HOME"
    work = slack_task(origin_channel="slack", slack_workspace="work", slack_team_id="T0WORK",
                      slack_channel="D0WORK", slack_thread_ts="1700000000.000100")
    assert report_route(two_workspaces, work).team_id == "T0WORK"


def test_a_thread_with_no_team_beside_it_stands_only_while_the_connection_has_none_either(
    slack_settings,
):
    """An install from before levels never wrote its team down, and neither did its
    tasks: one connection, one team. Once the connection knows its team, a thread that
    does not is not posted to."""
    plugins.write_config(slack_settings, NAME, {"channel_id": "U0ME"})
    plugins.install(slack_settings, NAME)
    task = slack_task(origin_channel="phone", slack_workspace="default", slack_channel="D0ME",
                      slack_thread_ts="1700000000.000100")
    assert report_route(slack_settings, task).thread_ts == "1700000000.000100"

    plugins.write_config(slack_settings, NAME, {"team_id": "T0HOME"})

    assert report_route(slack_settings, task).thread_ts is None
    asked = slack_task(origin_channel="slack", slack_workspace="default", slack_channel="D0ME",
                       slack_thread_ts="1700000000.000100")
    assert report_route(slack_settings, asked) is None
