"""`keryx auth login slack`: the link, then a token on standard input and nothing to ask.

Slack is three fakes here; nothing reaches it.
"""

import json
import tomllib
from dataclasses import dataclass, field

import pytest

from keryx import plugins
from keryx.config.store import ConfigError, ConfigStore
from keryx.integrations.slack import Member, SlackResult, Workspace, new_app_url
from keryx.setup import slack
from keryx.setup.slack import TEST_MESSAGE, SlackApi, SlackSetupError

TOKEN = "xoxb-very-secret"


@dataclass
class FakeSlack:
    workspace: Workspace = field(default_factory=lambda: Workspace(team="Acme"))
    member: Member = field(default_factory=lambda: Member(user_id="U0OWNER", name="Sam Doe"))
    sent: SlackResult = field(default_factory=SlackResult)
    calls: list = field(default_factory=list)

    def api(self) -> SlackApi:
        return SlackApi(
            auth_test=lambda token: self.calls.append(("auth", token)) or self.workspace,
            lookup_by_email=lambda token, email: self.calls.append(("member", token, email))
            or self.member,
            post_text=lambda token, channel, text: self.calls.append(("post", channel, text))
            or self.sent,
        )


def connect(settings, fake, *, token=TOKEN, email="sam@example.com", test=True) -> list[str]:
    lines: list[str] = []
    slack.connect(settings, ConfigStore(), token=token, email=email, test=test,
                  echo=lines.append, api=fake.api())
    return lines


def channel(settings) -> str:
    return tomllib.loads(plugins.config_path(settings, "send_to_slack").read_text())["channel_id"]


def test_with_no_token_it_prints_the_link_the_steps_and_the_next_command(settings):
    lines: list[str] = []

    slack.start(settings, lines.append)

    text = "\n".join(lines)
    assert new_app_url() in lines  # on a line of its own, to be relayed whole
    assert "1. Open the link" in text and "3. Under OAuth & Permissions" in text
    assert "keryx auth login slack --stdin --email ADDRESS" in text
    assert "**" not in text and "`" not in text


def test_the_next_command_says_whose_address_it_would_use(settings):
    lines: list[str] = []

    slack.start(settings.model_copy(update={"user_google_email": "sam@example.com"}),
                lines.append)

    assert "Without --email it looks for sam@example.com." in lines


def test_a_token_and_an_address_are_everything(settings):
    fake = FakeSlack()

    lines = connect(settings, fake, token=f"  {TOKEN}\n")

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert channel(settings) == "U0OWNER"
    assert plugins.is_on(settings, "send_to_slack")
    assert lines == [
        "connected to the Acme workspace; SLACK_BOT_TOKEN saved",
        "found Sam Doe (U0OWNER): messages go to them",
        "send_to_slack is on, from the next call",
        "sent a test message: it is in Slack, under Apps",
    ]
    assert fake.calls == [
        ("auth", TOKEN), ("member", TOKEN, "sam@example.com"), ("post", "U0OWNER", TEST_MESSAGE)
    ]


def test_no_test_message_when_it_is_not_wanted(settings):
    fake = FakeSlack()

    connect(settings, fake, test=False)

    assert not any(call[0] == "post" for call in fake.calls)


def test_without_an_address_the_google_one_is_looked_up(settings):
    fake = FakeSlack()
    settings = settings.model_copy(update={"user_google_email": "sam@example.com"})

    connect(settings, fake, email=None)

    assert ("member", TOKEN, "sam@example.com") in fake.calls


def test_with_nowhere_to_send_nothing_is_saved_and_slack_is_not_asked(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match="add --email ADDRESS"):
        connect(settings, fake, email=None)

    assert fake.calls == [] and ConfigStore()._secrets() == {}


@pytest.mark.parametrize(
    ("token", "message"),
    [("", "no token on standard input"), ("hunter2-secret", "A Slack token starts xox")],
)
def test_what_is_not_a_token_is_refused_before_slack_and_never_repeated(settings, token, message):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match=message) as refused:
        connect(settings, fake, token=token)

    assert fake.calls == [] and ConfigStore()._secrets() == {}
    assert "hunter2" not in str(refused.value)


def test_a_token_slack_refuses_saves_nothing_and_says_only_the_code(settings):
    fake = FakeSlack(workspace=Workspace(error="invalid_auth"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake)

    assert str(refused.value) == (
        "Slack does not recognise that token (invalid_auth); nothing was saved"
    )
    assert ConfigStore()._secrets() == {}
    assert not plugins.config_path(settings, "send_to_slack").exists()


def test_an_address_slack_cannot_find_keeps_the_token_and_says_what_to_do(settings):
    fake = FakeSlack(member=Member(error="users_not_found"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake)

    message = str(refused.value)
    assert "the token is saved" in message and "(users_not_found)" in message
    assert "keryx plugins install send_to_slack --set channel_id=ID" in message
    assert TOKEN not in message
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert not plugins.is_on(settings, "send_to_slack")


def test_a_test_message_that_fails_is_an_error_and_undoes_nothing(settings):
    fake = FakeSlack(sent=SlackResult(error="channel_not_found"))

    with pytest.raises(SlackSetupError, match=r"everything is saved.*\(channel_not_found\)"):
        connect(settings, fake)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert plugins.is_on(settings, "send_to_slack") and channel(settings) == "U0OWNER"


def test_a_new_token_for_an_install_that_has_a_channel_keeps_the_channel(settings):
    """Rotating the token: no address is needed, and none is looked up."""
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    fake = FakeSlack()
    settings = settings.model_copy(update={"user_google_email": "sam@example.com"})

    connect(settings, fake, email=None)

    assert channel(settings) == "D123"
    assert [call[0] for call in fake.calls] == ["auth", "post"]


def test_an_address_given_replaces_the_channel_there_was(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})

    connect(settings, FakeSlack())

    assert channel(settings) == "U0OWNER"


def test_an_mcp_server_that_carries_the_channel_needs_no_address(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"chat": {"env": {"SLACK_CHANNEL_ID": "D0CHAT"}}}})
    )
    plugins.write_config(settings, "send_to_slack", {"mcp_server": "chat"})
    fake = FakeSlack()

    lines = connect(settings, fake, email=None)

    assert [call[0] for call in fake.calls] == ["auth"]  # no channel here to test with
    assert lines[-1] == "send_to_slack is on, from the next call"


def test_a_token_the_environment_also_sets_is_said(settings, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    lines = connect(settings, FakeSlack())

    assert "note: SLACK_BOT_TOKEN is also set in the environment, which wins" in lines


def test_settings_that_do_not_parse_stop_it_before_anything(settings):
    settings.ensure_dirs()
    plugins.config_path(settings, "send_to_slack").parent.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "send_to_slack").write_text("channel_id = [")
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match="does not parse"):
        connect(settings, fake)

    assert fake.calls == []


def test_a_plugin_that_will_not_install_says_the_token_is_saved(settings, monkeypatch):
    def refuse(settings, name):
        raise plugins.PluginConfigError("send_to_slack was not turned on: no")

    monkeypatch.setattr(plugins, "install", refuse)

    with pytest.raises(SlackSetupError, match="the token is saved, but send_to_slack was not"):
        connect(settings, FakeSlack())


def test_a_plugin_file_the_loader_refuses_is_an_error(settings):
    settings.ensure_dirs()
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    tool = plugins.tool_path(settings, "send_to_slack")
    tool.write_text("raise RuntimeError('broken')\n")
    tool.chmod(0o600)

    with pytest.raises(SlackSetupError, match="send_to_slack is installed but refused"):
        connect(settings, FakeSlack())


def test_a_setting_the_store_refuses_is_an_error(settings, monkeypatch):
    def refuse(self, values, **kwargs):
        raise ConfigError("the store is read-only")

    monkeypatch.setattr(ConfigStore, "set", refuse)

    with pytest.raises(SlackSetupError, match="read-only"):
        connect(settings, FakeSlack())
