"""`keryx auth login slack`: the link, then a token on standard input and nothing to ask.

Slack is three fakes here; nothing reaches it.
"""

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from keryx import plugins
from keryx.config.store import ConfigError, ConfigStore
from keryx.integrations.slack import (
    USER_SCOPES,
    WRITE_USER_SCOPES,
    Access,
    Connection,
    SlackResult,
    Workspace,
    app_manifest,
    new_app_url,
)
from keryx.plugins.slack import connected, slack_route, workspaces
from keryx.setup import slack
from keryx.setup.slack import (
    CONVERSE_TEST_MESSAGE,
    LISTEN_NOTE,
    OWNER_LINE,
    READ_NOTE,
    SlackApi,
    SlackSetupError,
    meaning,
)
from keryx.setup.slack import test_message as said_to_test

TOKEN = "xoxb-very-secret"
#: What the app is called when nobody has named the assistant: Lyra, the default.
LYRA = "Lyra"
#: The one message that shows a connection arriving, at `announce`.
TEST_MESSAGE = said_to_test(Access.ANNOUNCE, LYRA)
#: What Slack says of the bot token of an app in a second workspace.
LAB = Workspace(team="Lab", team_id="T0LAB", user_id="U0KERYX")


@dataclass
class FakeSlack:
    workspace: Workspace = field(
        default_factory=lambda: Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX")
    )
    sent: SlackResult = field(default_factory=SlackResult)
    #: What `auth.test` says of a user token (None: the owner, in the bot token's own
    #: workspace), and `apps.connections.open` of an app one.
    user: Workspace | None = None
    opened: Connection = field(default_factory=lambda: Connection(url="wss://x/?ticket=t"))
    calls: list = field(default_factory=list)

    def api(self) -> SlackApi:
        def auth_test(token):
            self.calls.append(("auth", token))
            if not token.startswith("xoxp-"):
                return self.workspace
            return self.user or Workspace(team=self.workspace.team,
                                          team_id=self.workspace.team_id, user_id="U0OWNER")

        return SlackApi(
            auth_test=auth_test,
            post_text=lambda token, channel, text: self.calls.append(("post", channel, text))
            or self.sent,
            open_connection=lambda token: self.calls.append(("open", token)) or self.opened,
        )


def connect(settings, fake, *, token=TOKEN, user=True, test=True, **more) -> list[str]:
    """`keryx auth login slack --stdin` with `token` on standard input, and (unless
    `user` is False, or one is already among them) the user token beside it."""
    lines: list[str] = []
    if user and "xoxp-" not in token:
        token = f"{token}\n{USER}".strip()
    slack.connect(settings, ConfigStore(), tokens=token, test=test,
                  echo=lines.append, api=fake.api(), **more)
    return lines


def channel(settings) -> str:
    return tomllib.loads(plugins.config_path(settings, "slack_report").read_text())["channel_id"]


def test_with_no_token_it_prints_the_link_the_steps_and_the_next_command(settings):
    lines: list[str] = []

    slack.start(settings, lines.append)

    text = "\n".join(lines)
    assert new_app_url(assistant="Lyra") in lines  # on a line of its own, to be relayed whole
    assert "1. Open the link" in text and "3. Under OAuth & Permissions" in text
    assert ("4. Under OAuth & Permissions, the same page as the bot token, copy the User "
            "OAuth Token (xoxp-…). It says which member you are.") in text
    assert ("Then hand the tokens over on standard input, never on the command line, one "
            "to a line in any order:\n  keryx auth login slack --stdin") in text
    assert lines[-1] == ("The user token says which member you are: reports go to you, and "
                         "nothing else is asked.")
    assert "--email" not in text and "address" not in text.lower()
    assert "**" not in text and "`" not in text


def test_settings_that_do_not_parse_do_not_stop_the_link(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text("channel_id = [")
    lines: list[str] = []

    slack.start(settings, lines.append)

    assert new_app_url(assistant="Lyra") in lines


def test_no_test_message_when_it_is_not_wanted_and_it_says_none_was_sent(settings):
    fake = FakeSlack()

    lines = connect(settings, fake, test=False)

    assert not any(call[0] == "post" for call in fake.calls)
    assert lines[-1] == "no test message was sent (--no-test), so nothing has shown it arrives"


@pytest.mark.parametrize(
    ("token", "message"),
    [("", "no token on standard input"), ("hunter2-secret", "a bot token starts xoxb-")],
)
def test_what_is_not_a_token_is_refused_before_slack_and_never_repeated(settings, token, message):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match=message) as refused:
        connect(settings, fake, token=token, user=False)

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
    assert not plugins.config_path(settings, "slack_report").exists()


def test_a_token_that_could_not_be_checked_is_not_called_refused(settings):
    fake = FakeSlack(workspace=Workspace(error="http_503"))

    with pytest.raises(SlackSetupError) as stopped:
        connect(settings, fake)

    assert str(stopped.value) == (
        "the token could not be checked (Slack answered HTTP 503); nothing was saved"
    )
    assert ConfigStore()._secrets() == {}


def test_a_test_message_that_fails_is_an_error_and_undoes_nothing(settings):
    fake = FakeSlack(sent=SlackResult(error="channel_not_found"))

    with pytest.raises(SlackSetupError) as failed:
        connect(settings, fake)

    assert str(failed.value) == (
        "everything is saved, but the test message to U0OWNER did not go through: "
        "Slack does not know that channel or member (channel_not_found)"
    )
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_USER_TOKEN": USER}
    assert plugins.is_on(settings, "slack_report") and channel(settings) == "U0OWNER"


# --- a token that is not the one on file brings its destination with it ------------------
#
# A direct message's id belongs to the app that opened it. A new token is presumed to be
# another app's, so that id is never kept beside it; a member's id or a channel's is.


def with_token(settings, token="xoxb-the-old-one", **more):
    return settings.model_copy(update={"slack_bot_token": token, **more})


def test_a_new_token_never_keeps_a_direct_messages_id_and_reports_go_to_the_owner(settings):
    plugins.write_config(settings, "slack_report", {"channel_id": "D123"})
    fake = FakeSlack()

    lines = connect(with_token(settings), fake)

    assert channel(settings) == "U0OWNER"
    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("post", "U0OWNER", TEST_MESSAGE)]
    assert "you are U0OWNER there: reports go to you" in lines
    assert slack.ELSEWHERE in lines and "--set channel_id=ID" in slack.ELSEWHERE


@pytest.mark.parametrize("on_file", ["U0MEMBER", "W0MEMBER", "C0CHANNEL"])
def test_a_new_token_keeps_a_member_or_a_channel_and_tests_it(settings, on_file):
    plugins.write_config(settings, "slack_report", {"channel_id": on_file})
    fake = FakeSlack()

    lines = connect(with_token(settings), fake)

    assert channel(settings) == on_file and saved(settings)["owner_id"] == "U0OWNER"
    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("post", on_file, TEST_MESSAGE)]
    assert f"it keeps sending to {on_file}" in lines
    assert lines[-1] == "sent a test message: it is in Slack, under Apps"


def test_the_same_token_again_keeps_even_a_direct_messages_id(settings):
    plugins.write_config(settings, "slack_report", {"channel_id": "D123"})
    fake = FakeSlack()

    lines = connect(with_token(settings, TOKEN), fake)

    assert channel(settings) == "D123"
    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("post", "D123", TEST_MESSAGE)]
    assert not any("keryx restart" in line for line in lines)


def test_a_token_that_replaces_another_says_a_running_keryx_needs_a_restart(settings):
    plugins.write_config(settings, "slack_report", {"channel_id": "U0MEMBER"})

    replaced = connect(with_token(settings), FakeSlack())
    first = connect(settings, FakeSlack())

    note = "A Keryx that started with the old token keeps it until `keryx restart`."
    assert note in replaced and note not in first


# --- beside an MCP server that carries Slack ---------------------------------------------


@pytest.fixture
def chat_server(settings, tmp_path, monkeypatch):
    """`slack_report` on, with its token and channel both from the `chat` MCP server."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    env = {"SLACK_BOT_TOKEN": "xoxb-the-servers", "SLACK_CHANNEL_ID": "D0SERVER"}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"chat": {"env": env}}}))
    plugins.write_config(settings, "slack_report", {"mcp_server": "chat"})
    plugins.install(settings, "slack_report")
    return settings


def test_a_token_is_never_paired_with_the_mcp_servers_channel(chat_server):
    """The server's channel is a direct message with *its* app: a token of Keryx's own
    posts to the owner, who the user token says they are."""
    fake = FakeSlack()

    connect(chat_server, fake)

    assert ("post", "U0OWNER", TEST_MESSAGE) in fake.calls
    assert not any(call[0] == "post" and call[1] == "D0SERVER" for call in fake.calls)
    assert channel(chat_server) == "U0OWNER"


def test_beside_an_mcp_server_a_token_brings_its_own_destination_and_says_so(chat_server):
    fake = FakeSlack()
    settings = chat_server

    lines = connect(settings, fake)

    assert ("post", "U0OWNER", TEST_MESSAGE) in fake.calls
    assert (
        "note: slack_report now posts with this token to U0OWNER, not with the chat MCP "
        "server's token and channel; subagents still use that server"
    ) in lines
    route = slack_route(settings.model_copy(update={"slack_bot_token": TOKEN}))
    assert (route.token, route.channel, route.mcp_server) == (TOKEN, "U0OWNER", "chat")


def test_a_token_the_environment_also_sets_is_said(settings, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    lines = connect(settings, FakeSlack())

    assert "note: SLACK_BOT_TOKEN is also set in the environment, which wins" in lines


def test_settings_that_do_not_parse_stop_it_before_anything(settings):
    settings.ensure_dirs()
    plugins.config_path(settings, "slack_report").parent.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text("channel_id = [")
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match="does not parse"):
        connect(settings, fake)

    assert fake.calls == []


def test_a_plugin_that_will_not_install_says_the_token_is_saved(settings, monkeypatch):
    def refuse(settings, name):
        raise plugins.PluginConfigError("slack_report was not turned on: no")

    monkeypatch.setattr(plugins, "install", refuse)

    with pytest.raises(SlackSetupError, match="the token is saved, but slack_report was not"):
        connect(settings, FakeSlack())


def test_a_plugin_file_the_loader_refuses_is_an_error(settings):
    settings.ensure_dirs()
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    tool = plugins.tool_path(settings, "slack_report")
    tool.write_text("raise RuntimeError('broken')\n")
    tool.chmod(0o600)

    with pytest.raises(SlackSetupError, match="slack_report is installed but refused"):
        connect(settings, FakeSlack())


def test_a_setting_the_store_refuses_is_an_error(settings, monkeypatch):
    def refuse(self, values, **kwargs):
        raise ConfigError("the store is read-only")

    monkeypatch.setattr(ConfigStore, "set", refuse)

    with pytest.raises(SlackSetupError, match="read-only"):
        connect(settings, FakeSlack())


# --- the access level comes first ---------------------------------------------------------

APP, USER = "xapp-very-secret", "xoxp-very-secret"


def said(settings, **options) -> str:
    lines: list[str] = []
    slack.start(settings, lines.append, **options)
    return "\n".join(lines)


def saved(settings) -> dict:
    return tomllib.loads(plugins.config_path(settings, "slack_report").read_text())


def workspace_tokens() -> dict:
    """Every further workspace's tokens as the store holds them: one line of JSON."""
    return json.loads(ConfigStore()._secrets().get("SLACK_WORKSPACE_TOKENS", "{}"))


def connected_at(settings, level="announce", user=True, **tokens):
    """`settings` with a first workspace already connected at `level`, its user token on
    file unless `user` is False (a workspace connected before user tokens were asked for)."""
    plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "owner_id": "U0OWNER", "access": level, "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    plugins.install(settings, "slack_report")
    secrets = {"SLACK_BOT_TOKEN": TOKEN, **({"SLACK_USER_TOKEN": USER} if user else {}),
               **tokens}
    ConfigStore().set(secrets)
    return settings.model_copy(update={key.lower(): value for key, value in secrets.items()})


def test_with_nothing_chosen_every_level_is_said_in_plain_words_before_the_link(settings):
    text = said(settings)

    for level in Access:
        assert f"  {level}: {meaning(level, LYRA)}" in text
    assert text.index(meaning(Access.READ, LYRA)) < text.index(new_app_url(assistant="Lyra"))
    assert ("What follows is announce; --access converse or --access read or --access write "
            "chooses another.") in text
    assert "reads nothing there" in meaning(Access.ANNOUNCE, LYRA)
    assert "direct messages you type to the Lyra app" in meaning(Access.CONVERSE, LYRA)
    assert "without the phone PIN" in meaning(Access.CONVERSE, LYRA)
    assert "read Slack as you" in meaning(Access.READ, LYRA)


def test_a_chosen_level_says_what_it_sees_then_its_own_link_steps_and_command(settings):
    text = said(settings, access=Access.CONVERSE)

    assert text.startswith(f"Access level converse: {meaning(Access.CONVERSE, LYRA)}")
    assert new_app_url(Access.CONVERSE, assistant="Lyra") in text.splitlines()
    assert new_app_url(assistant="Lyra") not in text
    assert "4. Under Basic Information, App-Level Tokens, click Generate Token and Scopes" in text
    assert "connections:write" in text
    assert "5. Under OAuth & Permissions, the same page as the bot token" in text
    assert "6. Optional:" in text and "7." not in text
    assert "hand the tokens over on standard input, never on the command line, one to a line" in (
        text
    )
    assert "keryx auth login slack --stdin --access converse\n" in text
    assert "--email" not in text
    assert "**" not in text and "`" not in text and "·" not in text


def test_read_asks_for_three_tokens_and_no_address_and_says_nothing_reads_yet(settings):
    text = said(settings, access=Access.READ)

    assert new_app_url(Access.READ, assistant="Lyra") in text.splitlines()
    assert "5. Under OAuth & Permissions, the same page as the bot token, copy the User" in text
    assert READ_NOTE in text
    assert "keryx auth login slack --stdin --access read\n" in text
    assert "--email" not in text
    assert text.endswith("The user token says which member you are: reports go to you, and "
                         "nothing else is asked.")


def test_raising_the_level_of_an_app_there_is_never_makes_a_second_one(settings):
    settings = connected_at(settings)

    text = said(settings, access=Access.CONVERSE)

    assert "api.slack.com/apps?new_app=1" not in text
    assert "Acme is connected at the announce level, so the app you have is changed" in text
    assert "pick the app you already have for Keryx. Do not create a second one." in text
    assert "2. Open App Manifest, replace what is there with the manifest below" in text
    assert "3. Open Install App and click Reinstall to Workspace" in text
    assert "4. Under Basic Information, App-Level Tokens" in text and "6." not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(Access.CONVERSE, assistant="Lyra")
    assert "The bot token stays as it is." in text
    assert "hand the app-level token over on standard input" in text
    assert "keryx auth login slack --stdin --access converse\n" in text


def test_a_workspace_connected_at_that_level_is_told_so_and_never_shown_the_link(settings):
    """Asking again must not offer the link that makes a second app."""
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    for text in (said(settings), said(settings, access=Access.CONVERSE)):
        assert text.startswith(f"Acme is connected at the converse level: "
                               f"{meaning(Access.CONVERSE, LYRA)}")
        assert "api.slack.com/apps?new_app=1" not in text and "Create your Slack app" not in text
        assert "Only messages from U0OWNER are acted on there: every one typed to the" in text
        assert "and elsewhere only one that mentions it (@Lyra)" in text
        assert "keryx auth login slack --access announce or --access read" in text
        assert "To replace a token: keryx auth login slack --stdin, with the new one" in text
        assert "To disconnect it: keryx auth logout slack" in text
        assert "**" not in text and "`" not in text


def test_a_connected_workspace_is_always_shown_the_manifest_that_renames_its_app(settings):
    """Slack learns the app's name only from a manifest, and is never asked what it is
    called: so after `ASSISTANT_NAME` changes, the next login shows the manifest with the
    new name and how to save it, and says the old name stands until then."""
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    settings = settings.model_copy(update={"assistant_name": "Jarvis"})

    text = said(settings)

    assert ("The app is called Jarvis in Slack, after your assistant, once its manifest "
            "says so: one made under another name keeps it until then.") in text
    assert "open https://api.slack.com/apps, pick the app, open App Manifest" in text
    manifest = json.loads(text[text.index("click Save Changes:") + len("click Save Changes:"):
                               text.index("\nOptional:")])
    assert manifest == app_manifest(Access.CONVERSE, assistant="Jarvis")
    assert manifest["display_information"]["name"] == "Jarvis"
    assert "every one typed to the Jarvis app itself" in text and "@Jarvis" in text
    assert "api.slack.com/apps?new_app=1" not in text


def test_a_connected_workspace_that_lacks_something_is_told_what(settings):
    settings = connected_at(settings, "converse")

    assert "It is without its app token." in said(settings)


def test_a_further_workspace_that_is_connected_is_told_so_by_its_own_name(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(workspace=LAB),
            token="xoxb-lab-secret", workspace="lab", test=False)

    text = said(settings, workspace="lab")

    assert text.startswith("lab (Lab) is connected at the announce level")
    assert "keryx auth logout slack --workspace lab" in text
    assert "keryx auth login slack --workspace lab --access converse or --access read" in text


def test_when_every_token_is_saved_there_is_nothing_to_hand_over(settings):
    """Lowered from read and raised again: the tokens never left the file at converse."""
    settings = connected_at(settings, "announce", SLACK_APP_TOKEN=APP)

    text = said(settings, access=Access.CONVERSE)

    assert "hand nothing over" not in text
    assert "Every token this level needs is saved already, so there is nothing to hand over." in (
        text
    )
    assert "keryx auth login slack --stdin --access converse < /dev/null" in text


def test_converse_takes_three_tokens_in_any_order_and_the_user_token_says_who_is_the_owner(
    settings
):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"))

    lines = connect(settings, fake, token=f"{APP}\n\n  {TOKEN}\n", access=Access.CONVERSE)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    assert saved(settings) == {
        "channel_id": "U0OWNER", "access": "converse", "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX", "mcp_server": "",
    }
    assert lines[0] == (
        "connected to the Acme workspace at the converse level; SLACK_BOT_TOKEN, "
        "SLACK_APP_TOKEN and SLACK_USER_TOKEN saved"
    )
    assert LISTEN_NOTE.format(app=LYRA) in lines
    assert fake.calls == [
        ("auth", TOKEN), ("auth", USER), ("open", APP),
        ("post", "U0OWNER", said_to_test(Access.CONVERSE, LYRA)),
    ]
    assert not any("secret" in line for line in lines)


def test_a_level_names_the_token_it_still_needs_and_saves_nothing(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, access=Access.READ, user=False)

    assert str(refused.value) == (
        "the read level also needs the app-level token (xapp-) and the user token (xoxp-), "
        "one to a line. Nothing was saved"
    )
    assert fake.calls == [] and ConfigStore()._secrets() == {}


def test_a_token_the_level_does_not_use_is_named_and_nothing_is_saved(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{TOKEN}\n{APP}\n{USER}")

    assert str(refused.value) == (
        "the announce level does not use an app-level token (xapp-): leave it out, or "
        "choose a higher level with --access. Nothing was saved"
    )
    assert fake.calls == [] and ConfigStore()._secrets() == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"{TOKEN}\nxoxb-another", "two bot tokens on standard input; one of each"),
        (f"{TOKEN}\nhunter2-secret", "that is not a Slack token"),
        (f"{APP}\nxoxe-refresh", "that is not a Slack token"),
    ],
)
def test_lines_that_are_not_one_of_each_token_are_refused_and_never_repeated(
    settings, text, message
):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError, match=message) as refused:
        connect(settings, fake, token=text, access=Access.CONVERSE)

    assert fake.calls == [] and ConfigStore()._secrets() == {}
    assert "hunter2" not in str(refused.value) and "another" not in str(refused.value)


def test_an_app_token_slack_does_not_take_saves_nothing_and_says_what_it_needs(settings):
    fake = FakeSlack(opened=Connection(error="not_allowed_token_type"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{TOKEN}\n{APP}", access=Access.CONVERSE)

    assert str(refused.value) == (
        "the app-level token could not be used (that is not the kind of token this call "
        "takes (not_allowed_token_type)): it needs the connections:write scope, and Socket "
        "Mode on in the app. Nothing was saved"
    )
    assert ConfigStore()._secrets() == {}
    assert not plugins.config_path(settings, "slack_report").exists()


def test_raising_the_level_needs_only_the_token_that_is_new(settings):
    settings = connected_at(settings)
    fake = FakeSlack()

    lines = connect(settings, fake, token=APP, access=Access.CONVERSE, test=False)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    assert saved(settings)["access"] == "converse" and saved(settings)["owner_id"] == "U0OWNER"
    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("open", APP)]
    assert "it keeps sending to U0OWNER" in lines
    assert not any("keryx restart`." in line and "old token" in line for line in lines)


def test_with_every_token_on_file_nothing_need_be_typed_again(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    fake = FakeSlack()

    connect(settings, fake, token="", user=False, test=False)

    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("open", APP)]
    assert saved(settings)["access"] == "converse"


def test_read_knows_the_owner_from_the_user_token_and_needs_no_address(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}",
                    access=Access.READ)

    assert ConfigStore()._secrets() == {
        "SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP, "SLACK_USER_TOKEN": USER,
    }
    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "U0SAM")
    assert "you are U0SAM there: reports go to you" in lines
    assert READ_NOTE in lines and LISTEN_NOTE.format(app=LYRA) in lines
    assert lines[0].endswith("SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_USER_TOKEN saved")
    assert not any(call[0] == "member" for call in fake.calls)


@pytest.mark.parametrize(
    ("user", "why"),
    [
        (Workspace(error="invalid_auth"), "Slack does not recognise that token (invalid_auth)"),
        (Workspace(error="http_503"), "it could not be checked (Slack answered HTTP 503)"),
        (Workspace(team="Acme", team_id="T0ACME"), "Slack did not say which member it belongs to"),
        (Workspace(team="Other", team_id="T0OTHER", user_id="U0SAM"),
         "it belongs to another workspace than the bot token (Other)"),
        (Workspace(team="Acme", user_id="U0SAM"),
         "it belongs to another workspace than the bot token (Acme)"),
    ],
)
def test_a_user_token_that_is_not_this_workspaces_owner_saves_nothing(settings, user, why):
    fake = FakeSlack(user=user)

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}", access=Access.READ)

    assert str(refused.value) == f"the user token cannot be used: {why}. Nothing was saved"
    assert ConfigStore()._secrets() == {}


def test_at_read_a_destination_on_file_is_kept_and_the_token_still_says_who(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "slack_report", {"channel_id": "C0CHANNEL", "owner_id": ""})
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{APP}", access=Access.READ,
                    test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "C0CHANNEL")
    assert any(line.startswith("Reports go to C0CHANNEL, which is not your direct messages")
               for line in lines)


def test_an_owner_on_file_stands_whatever_the_destination(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "slack_report", {"channel_id": "C0CHANNEL"})

    connect(settings, FakeSlack(), token=APP, access=Access.CONVERSE, test=False)

    assert saved(settings)["owner_id"] == "U0OWNER"


def test_a_bot_token_from_another_workspace_is_refused_while_one_is_connected(settings):
    """It would bring another owner with it, and the owner is fixed until logout."""
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    elsewhere = FakeSlack(workspace=Workspace(team="Other", team_id="T0OTHER", user_id="U0BOT2"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, elsewhere, token="xoxb-another-app")

    assert str(refused.value) == (
        "that bot token is for Other, and Acme is connected here: `keryx auth logout "
        "slack` first, then log in again. Nothing was saved"
    )
    assert saved(settings)["owner_id"] == "U0OWNER"
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == TOKEN


# --- where reports go is never whose messages are acted on -------------------------------


def older_install(settings, channel):
    """An install from before levels: a bot token and where reports go, nothing else."""
    settings = settings.model_copy(update={"slack_bot_token": TOKEN})
    ConfigStore().set({"SLACK_BOT_TOKEN": TOKEN})
    plugins.config_path(settings, "slack_report").parent.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(f'channel_id = "{channel}"\n')
    plugins.install(settings, "slack_report")
    return settings


@pytest.mark.parametrize("channel", ["U0COLLEAGUE", "D0OLDDM", "C0REPORTS"])
def test_where_reports_go_never_makes_anybody_the_owner(settings, channel):
    """The owner is whoever the user token belongs to, and nothing else says who: at
    announce reports may go to a colleague's member id, which once made the colleague
    the one whose messages start work when the level was raised."""
    settings = older_install(settings, channel)

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, FakeSlack(), token=APP, user=False, access=Access.CONVERSE)
    assert "the converse level also needs the user token (xoxp-)" in str(refused.value)
    assert saved(settings).get("owner_id", "") == ""

    lines = connect(settings, FakeSlack(), token=APP, access=Access.CONVERSE, test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", channel)
    assert f"it keeps sending to {channel}" in lines
    assert OWNER_LINE.format(who="U0OWNER", app=LYRA) in lines


def test_the_owner_is_recorded_at_announce_too_and_nothing_says_messages_are_acted_on(settings):
    """So raising the level later changes nothing about who the owner is."""
    lines = connect(settings, FakeSlack(), test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", "U0OWNER")
    assert not any("are acted on there" in line for line in lines)
    assert slack.OWNER_NOTED.format(who="U0OWNER") in lines

    raised = connected_like(settings)
    connect(raised, FakeSlack(), token=APP, access=Access.CONVERSE, test=False)
    assert saved(settings)["owner_id"] == "U0OWNER"


def connected_like(settings):
    """`settings` holding what the store now does, as the next command would load them."""
    secrets = ConfigStore()._secrets()
    return settings.model_copy(update={
        "slack_bot_token": secrets.get("SLACK_BOT_TOKEN"),
        "slack_app_token": secrets.get("SLACK_APP_TOKEN"),
        "slack_user_token": secrets.get("SLACK_USER_TOKEN"),
    })


def test_an_owner_on_file_is_kept_through_announce_for_the_day_it_is_raised_again(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    connect(settings, FakeSlack(), token=TOKEN, access=Access.ANNOUNCE, test=False)

    assert (saved(settings)["access"], saved(settings)["owner_id"]) == ("announce", "U0OWNER")


def test_there_is_no_way_to_change_the_owner_in_place(settings):
    """One owner, fixed at connect: no option, no argument, no address, nothing but
    logout and login. Somebody else's user token is refused, whoever hands it over."""
    import inspect

    from keryx.setup.auth import LoginOptions

    parameters = inspect.signature(slack.connect).parameters
    assert "owner" not in parameters and "email" not in parameters
    assert not hasattr(LoginOptions(), "owner") and not hasattr(LoginOptions(), "email")
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    theirs = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0COLLEAGUE"))
    for level in (Access.ANNOUNCE, Access.CONVERSE, Access.READ):
        with pytest.raises(SlackSetupError, match="the user token belongs to U0COLLEAGUE"):
            connect(settings, theirs, token="xoxp-theirs", access=level, test=False)
        assert saved(settings)["owner_id"] == "U0OWNER"
        assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == USER


def test_logging_out_and_in_again_is_how_somebody_else_connects(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    slack.logout(settings, ConfigStore(), workspace="default", echo=lambda line: None)
    cleared = settings.model_copy(update={"slack_bot_token": None, "slack_app_token": None,
                                          "slack_user_token": None})

    connect(cleared, FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0NEWME")),
            token=f"{TOKEN}\n{APP}", access=Access.CONVERSE, test=False)

    assert saved(settings)["owner_id"] == "U0NEWME"


def test_at_read_a_user_token_that_is_not_the_owners_on_file_is_refused(settings):
    """Somebody else's token would otherwise make them the owner without a word."""
    settings = connected_at(settings, "converse", user=False, SLACK_APP_TOKEN=APP)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0ELSE"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=USER, access=Access.READ)

    assert str(refused.value) == (
        "the user token belongs to U0ELSE, and the member whose messages Keryx acts on "
        "there is U0OWNER: paste your own user token. To connect as somebody else, `keryx "
        "auth logout slack` first. Nothing was saved"
    )
    assert saved(settings)["access"] == "converse"
    assert "SLACK_USER_TOKEN" not in ConfigStore()._secrets()


def test_at_read_the_first_owner_is_whom_the_user_token_belongs_to(settings):
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}",
            access=Access.READ, test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "U0SAM")


def test_above_announce_a_bot_token_slack_names_no_workspace_for_is_refused(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme"))

    with pytest.raises(SlackSetupError, match="did not say which workspace the bot token"):
        connect(settings, fake, token=f"{TOKEN}\n{APP}", access=Access.CONVERSE)

    assert ConfigStore()._secrets() == {}


def test_a_lower_level_given_with_a_token_forgets_what_it_no_longer_uses(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    fake = FakeSlack()
    lines = connect(settings, fake, token=TOKEN, user=False, access=Access.CONVERSE,
                    test=False)

    # The token that could read Slack as them is not kept below read, and Slack is not
    # asked about it: the owner on file stands.
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}
    assert (saved(settings)["access"], saved(settings)["owner_id"]) == ("converse", "U0OWNER")
    assert ("SLACK_USER_TOKEN is forgotten: the converse level does not use it, and until "
            "the app is reinstalled it could still read Slack as you") in lines
    assert ("auth", USER) not in fake.calls


def test_a_lower_level_given_with_the_reinstalled_apps_user_token_keeps_that_one(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    connect(settings, FakeSlack(), token="xoxp-after-reinstall", access=Access.CONVERSE,
            test=False)

    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-after-reinstall"


def test_lowering_asks_slack_nothing_and_forgets_the_tokens_at_once(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    done = slack.lower(settings, ConfigStore(), workspace="default", access=Access.ANNOUNCE,
                       echo=lines.append)

    assert done and ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert saved(settings)["access"] == "announce"
    assert lines[0] == f"Acme is now at the announce level: {meaning(Access.ANNOUNCE, LYRA)}"
    assert lines[1] == "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten."
    assert "A running Keryx stops acting on your messages there at once." in lines[2]
    assert json.loads(lines[3]) == app_manifest(Access.ANNOUNCE, assistant="Lyra")
    assert [each.access for each in workspaces(settings)] == [Access.ANNOUNCE]


@pytest.mark.parametrize("level", [Access.CONVERSE, Access.READ])
def test_the_same_level_or_a_higher_one_is_not_a_lowering(settings, level):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    assert not slack.lower(settings, ConfigStore(), workspace="default", access=level,
                           echo=pytest.fail)
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}


def test_a_workspace_that_is_not_connected_has_nothing_to_lower(settings):
    assert not slack.lower(settings, ConfigStore(), workspace="default",
                           access=Access.ANNOUNCE, echo=pytest.fail)


# --- a further workspace ------------------------------------------------------------------


def test_a_further_workspace_has_its_own_app_tokens_owner_and_destination(settings):
    settings = connected_at(settings)
    fake = FakeSlack(workspace=LAB,
                     user=Workspace(team="Lab", team_id="T0LAB", user_id="U0LAB"))

    lines = connect(settings, fake, token="xoxb-lab-secret\nxapp-lab-secret\nxoxp-lab-secret",
                    workspace="lab", access=Access.CONVERSE)

    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == TOKEN
    assert workspace_tokens() == {"lab": {"bot": "xoxb-lab-secret", "app": "xapp-lab-secret",
                                          "user": "xoxp-lab-secret"}}
    assert ("To send reports somewhere else: set channel_id under [workspace-lab] in "
            "slack_report.toml.") in lines
    data = saved(settings)
    assert {"lab": data["workspace-lab"]} == {"lab": {
        "channel_id": "U0LAB", "access": "converse", "owner_id": "U0LAB", "team": "Lab",
        "team_id": "T0LAB", "bot_user_id": "U0KERYX",
    }}
    # The first workspace is left exactly as it was.
    assert (data["channel_id"], data["access"], data["team"]) == ("U0OWNER", "announce", "Acme")
    assert lines[0] == (
        "connected to the Lab workspace as lab, at the converse level; its tokens are saved"
    )
    assert ("post", "U0LAB", said_to_test(Access.CONVERSE, LYRA)) in fake.calls
    assert not any("secret" in line for line in lines)


def test_a_second_further_workspace_keeps_the_first(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-lab-secret", workspace="lab", test=False)

    lines = connect(settings, FakeSlack(), token="xoxb-work-secret", workspace="work",
                    test=False)

    assert workspace_tokens() == {
        "lab": {"bot": "xoxb-lab-secret", "user": USER},
        "work": {"bot": "xoxb-work-secret", "user": USER},
    }
    assert {"workspace-lab", "workspace-work"} <= set(saved(settings))
    # A process that started before either is told nothing of a restart, and needs none:
    # a further workspace's tokens are read from the store whenever they are asked for.
    assert not any("restart" in line for line in lines)
    assert [each.name for each in connected(settings)] == ["default", "lab", "work"]


def test_a_further_workspace_is_lowered_by_its_own_name(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(workspace=LAB),
            token="xoxb-lab-secret\nxapp-lab-secret", workspace="lab",
            access=Access.CONVERSE, test=False)
    lines: list[str] = []

    assert slack.lower(settings, ConfigStore(), workspace="lab", access=Access.ANNOUNCE,
                       echo=lines.append)

    # Its user token could only ever say who they are, so it is kept.
    assert workspace_tokens() == {"lab": {"bot": "xoxb-lab-secret", "user": USER}}
    assert saved(settings)["workspace-lab"]["access"] == "announce"
    assert lines[0].startswith("lab (Lab) is now at the announce level")
    assert lines[1] == "lab's app-level token is forgotten."


def test_a_further_workspace_wants_the_first_one_there_first(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, workspace="lab")

    assert "lab is added beside your first workspace, and there is none yet" in str(refused.value)
    assert "run it without --workspace first. Nothing was saved" in str(refused.value)
    assert fake.calls == [] and ConfigStore()._secrets() == {}


def test_the_link_for_a_further_workspace_names_it_on_the_next_command(settings):
    settings = connected_at(settings)

    text = said(settings, workspace="lab", access=Access.CONVERSE)

    assert new_app_url(Access.CONVERSE, assistant="Lyra") in text
    assert "keryx auth login slack --stdin --workspace lab --access converse\n" in text


def test_a_workspaces_name_is_a_short_word_and_a_level_is_one_of_three():
    assert slack.workspace_name(None) == "default" and slack.workspace_name(" Work ") == "work"
    with pytest.raises(SlackSetupError, match="a short word of your own"):
        slack.workspace_name("my work")
    assert slack.parse_access(None) is None and slack.parse_access(" READ ") is Access.READ
    with pytest.raises(SlackSetupError, match="one of announce, converse, read"):
        slack.parse_access("everything")


def test_each_token_has_a_prefix_that_is_said_when_it_is_wrong():
    assert slack.token_problem("xoxb-1") is None and slack.token_problem("xoxp-old-bot") is None
    assert slack.token_problem("xapp-1", "app") is None
    assert slack.token_problem("xoxb-1", "app") == "An app-level token starts xapp-"
    assert slack.token_problem("xoxb-1", "user") == "A user token starts xoxp-"
    assert slack.token_problem("nope") == "A bot token starts xoxb-"


def test_one_line_that_starts_xox_is_still_taken_as_the_bot_token(settings):
    """As it always was: a rotating bot token does not start xoxb-."""
    connect(settings, FakeSlack(), token="xoxe.xoxb-1-very-secret", test=False)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxe.xoxb-1-very-secret",
                                        "SLACK_USER_TOKEN": USER}
    with pytest.raises(SlackSetupError, match="that is not a Slack token"):
        slack.read_tokens("hunter2")
    with pytest.raises(SlackSetupError, match="two bot tokens"):
        slack.read_tokens("xoxb-1\nxoxe.xoxb-2")


def test_the_link_for_a_further_workspace_says_when_the_first_is_not_there_yet(settings):
    text = said(settings, workspace="lab")

    assert text.startswith(
        "lab would be added beside your first workspace, and there is none yet: run this "
        "without --workspace first."
    )
    assert "would be added beside" not in said(connected_at(settings), workspace="lab")


# --- lowering says what really stops ------------------------------------------------------


def test_lowering_read_to_converse_says_keryx_still_acts_on_messages(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    assert slack.lower(settings, ConfigStore(), workspace="default", access=Access.CONVERSE,
                       echo=lines.append)

    text = "\n".join(lines)
    assert "stops acting on your messages" not in text
    assert ("Keryx no longer holds your user token, so nothing reads Slack as you; it still "
            "acts on the messages you type to the app.") in text
    assert lines[1] == "SLACK_USER_TOKEN is forgotten."
    assert workspaces(settings)[0].listens


# --- disconnecting --------------------------------------------------------------------------


def logged_out(settings, workspace="default") -> list[str]:
    lines: list[str] = []
    slack.logout(settings, ConfigStore(), workspace=workspace, echo=lines.append)
    return lines


def test_logging_out_a_further_workspace_removes_its_tokens_and_its_table(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(workspace=LAB),
            token="xoxb-lab-secret", workspace="lab", test=False)
    connect(settings, FakeSlack(), token="xoxb-work-secret", workspace="work", test=False)

    lines = logged_out(settings, "lab")

    assert workspace_tokens() == {"work": {"bot": "xoxb-work-secret", "user": USER}}
    assert "workspace-lab" not in saved(settings) and "workspace-work" in saved(settings)
    assert lines[0] == "lab (Lab) is disconnected: its tokens and its settings are removed."
    assert "api.slack.com/apps" in lines[-1] and "at once" in lines[-1]
    assert [each.name for each in connected(settings)] == ["default", "work"]
    assert plugins.is_on(settings, "slack_report")
    assert not any("secret" in line for line in lines)


def test_logging_out_the_last_further_workspace_leaves_no_line_of_tokens(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-lab-secret", workspace="lab", test=False)

    logged_out(settings, "lab")

    assert "SLACK_WORKSPACE_TOKENS" not in ConfigStore()._secrets()


def test_logging_out_the_first_workspace_forgets_its_tokens_and_turns_the_plugin_off(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    lines = logged_out(settings)

    assert ConfigStore()._secrets() == {}
    assert not plugins.is_on(settings, "slack_report")
    kept = plugins.read_config(settings, "slack_report")
    assert (kept["channel_id"], kept["access"], kept["owner_id"], kept["team_id"]) == (
        "", "announce", "", ""
    )
    assert kept["bot_user_id"] == ""
    assert lines[0] == (
        "Acme is disconnected: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_USER_TOKEN and what "
        "Slack said of it are removed."
    )
    assert lines[1] == "slack_report is off."


def test_the_first_workspace_is_not_logged_out_from_under_the_others(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-lab-secret", workspace="lab", test=False)

    with pytest.raises(SlackSetupError, match="lab would be left with none: disconnect it "
                                              "first, with --workspace NAME"):
        logged_out(settings)

    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == TOKEN


def test_logging_out_beside_an_mcp_server_leaves_the_servers_route(chat_server):
    settings = chat_server.model_copy(update={"user_google_email": "sam@example.com"})
    connect(settings, FakeSlack(), test=False)

    lines = logged_out(settings.model_copy(update={"slack_bot_token": TOKEN}))

    assert ConfigStore()._secrets() == {}
    assert "slack_report stays on with the chat MCP server's token and channel." in lines
    route = slack_route(chat_server)
    assert (route.token, route.channel) == ("xoxb-the-servers", "D0SERVER")


def test_logging_out_what_is_not_there_says_so(settings):
    assert logged_out(settings) == ["Slack is not connected: there is nothing to remove."]
    with pytest.raises(SlackSetupError, match="there is no workspace called lab"):
        logged_out(settings, "lab")
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-work-secret", workspace="work", test=False)
    with pytest.raises(SlackSetupError, match=r"no workspace called lab \(there is work\)"):
        logged_out(settings, "lab")


def test_the_apps_own_member_id_is_stored_because_a_mention_of_it_is_the_trigger(settings):
    connect(settings, FakeSlack(), token=f"{TOKEN}\n{APP}", access=Access.CONVERSE, test=False)

    assert saved(settings)["bot_user_id"] == "U0KERYX"
    assert workspaces(settings)[0].missing == ()


def test_above_announce_a_bot_token_slack_names_no_member_for_is_refused(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"))

    with pytest.raises(SlackSetupError, match="or which member the app is there"):
        connect(settings, fake, token=f"{TOKEN}\n{APP}", access=Access.CONVERSE)

    assert ConfigStore()._secrets() == {}


def test_each_level_says_where_keryx_can_be_set_to_work_from_and_what_it_sees():
    assert "reads nothing" in meaning(Access.ANNOUNCE, LYRA)
    converse, read = meaning(Access.CONVERSE, LYRA), meaning(Access.READ, LYRA)
    assert "direct messages you type to the Lyra app itself: nothing else" in converse
    assert "Every message you type there is a request, with no @Lyra needed" in converse
    assert "Only your own member id is checked" in converse
    assert "without the phone PIN" in converse
    assert "any conversation it has been added to" in read
    # What is really delivered, and that the answer is read by the others there.
    assert "every message that mentions it there is sent to Keryx" in read
    assert "anyone's but yours is dropped" in read
    assert "answered in that thread, where the others in the conversation read the answer" in read
    assert "only when you ask it about messages" in read


# --- write: sending as the owner ----------------------------------------------------------------


def test_write_says_plainly_before_the_link_that_keryx_can_then_send_as_you(settings):
    text = said(settings, access=Access.WRITE)

    assert "Keryx can also send messages as you, in your name, to other people and to" in (
        meaning(Access.WRITE, LYRA))
    assert "you have said yes" in meaning(Access.WRITE, LYRA)
    assert text.index(meaning(Access.WRITE, LYRA)) < text.index(
        new_app_url(Access.WRITE, assistant="Lyra"))
    assert slack.WRITE_NOTE in text and slack.READ_NOTE in text
    assert "other people read it as written by you" in slack.WRITE_NOTE
    assert "The user token says which member you are: reports go to you" in text


def test_raising_read_to_write_changes_the_app_there_is_and_asks_for_no_new_kind_of_token(
    settings,
):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    text = said(settings, access=Access.WRITE)

    assert "is connected at the read level, so the app you have is changed" in text
    assert '"chat:write"' in text.split("The manifest:")[1]
    assert "Every token this level needs is saved already" in text
    assert "keryx auth login slack --stdin --access write < /dev/null" in text
    assert "If Slack shows a different User OAuth Token after the reinstall" in text
    assert "api.slack.com/apps?new_app=1" not in text


def test_write_is_refused_while_the_user_token_may_not_post_as_the_owner_yet(settings):
    """The app was not reinstalled: Slack says what the token may do, and it is not this."""
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=USER_SCOPES))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token="", access=Access.WRITE)

    assert str(refused.value) == (
        "the user token cannot be used: it may not yet do what the write level needs (it "
        "lacks chat:write, im:write): save the manifest in the app, reinstall it to the "
        "workspace, and hand over the User OAuth Token shown after that. Nothing was saved"
    )
    assert saved(settings)["access"] == "read"


@pytest.mark.parametrize(
    "scopes", [None, (*USER_SCOPES, *WRITE_USER_SCOPES)]
)
def test_write_is_saved_once_the_token_may_or_when_slack_does_not_say(settings, scopes):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=scopes))

    lines = connect(settings, fake, token="", access=Access.WRITE, test=False)

    assert saved(settings)["access"] == "write" and saved(settings)["owner_id"] == "U0OWNER"
    assert slack.WRITE_NOTE in lines
    assert [each.writes for each in workspaces(settings)] == [True]


def test_lowering_write_to_read_keeps_the_token_and_takes_sending_away(settings):
    settings = connected_at(settings, "write", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    slack.lower(settings, ConfigStore(), workspace="default", access=Access.READ,
                echo=lambda line: None)

    [one] = workspaces(settings)
    assert (one.access, one.reads, one.writes) == (Access.READ, True, False)
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == USER


def test_lowering_write_to_read_says_what_stops_and_never_that_the_user_token_is_gone(
    settings,
):
    settings = connected_at(settings, "write", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    slack.lower(settings, ConfigStore(), workspace="default", access=Access.READ,
                echo=lines.append)

    said = "\n".join(lines)
    assert "Keryx no longer sends messages as you, from this moment" in said
    assert "it still reads Slack as you when you ask, with the user token it keeps" in said
    assert "no longer holds your user token" not in said and "forgotten" not in said


def test_what_is_said_at_connect_states_the_direct_message_rule(settings):
    """A direct message to the app needs no mention: the test message, the note about
    listening and the owner line all say so, since they are what the owner reads."""
    assert "with no @Lyra needed" in said_to_test(Access.CONVERSE, LYRA)
    assert "a reply under a report resumes that task" in CONVERSE_TEST_MESSAGE
    assert "every message you type to the Lyra app is a request" in LISTEN_NOTE.format(app=LYRA)
    said = OWNER_LINE.format(who="U0OWNER", app=LYRA)
    assert "every one typed to the Lyra app itself" in said
    assert "elsewhere only one that mentions it (@Lyra)" in said
    assert "also acts on every message you send the Lyra app itself" in slack.short(
        Access.CONVERSE, LYRA)


# --- the app carries the assistant's name -------------------------------------------------------


def test_everything_that_says_what_to_type_in_slack_follows_the_assistants_name(settings):
    """The level descriptions, the steps, the link's manifest, the owner line, the note
    about listening and the test message: an owner whose assistant is Jarvis is told to
    type to the Jarvis app and to mention @Jarvis, and never @Keryx."""
    jarvis = settings.model_copy(update={"assistant_name": "Jarvis"})

    shown = said(jarvis, access=Access.READ)

    assert "the direct messages you type to the Jarvis app itself" in said(
        jarvis, access=Access.CONVERSE)
    assert "You can also mention the app (@Jarvis)" in shown
    assert "with yours already filled in and named Jarvis, after your assistant" in shown
    assert new_app_url(Access.READ, assistant="Jarvis") in shown.splitlines()
    assert "@Keryx" not in shown and "Keryx app" not in shown

    fake = FakeSlack()
    lines = connect(jarvis, fake, token=f"{TOKEN}\n{APP}", access=Access.CONVERSE)

    assert OWNER_LINE.format(who="U0OWNER", app="Jarvis") in lines
    assert LISTEN_NOTE.format(app="Jarvis") in lines
    assert ("post", "U0OWNER", said_to_test(Access.CONVERSE, "Jarvis")) in fake.calls
    assert not any("@Keryx" in line or "Keryx app" in line for line in lines)


def test_what_the_app_posts_to_show_it_arrives_does_not_call_itself_keryx():
    """The app is the assistant in Slack: under the name Lyra, "Keryx is connected" would
    read as somebody else's message. It names nothing but what the owner would type."""
    for access in Access:
        text = said_to_test(access, "Lyra")
        assert "Keryx" not in text and text.startswith("Connected. ")
        assert "{" not in text
    assert "@Lyra" not in said_to_test(Access.ANNOUNCE, "Lyra")
    assert "with no @Lyra needed" in said_to_test(Access.WRITE, "Lyra")


def test_the_steps_that_change_an_app_say_that_saving_the_manifest_renames_it(settings):
    settings = connected_at(settings)
    settings = settings.model_copy(update={"assistant_name": "Jarvis"})

    text = said(settings, access=Access.CONVERSE)

    assert "pick the app you already have for Keryx. Do not create a second one." in text
    assert ("Saving it also names the app Jarvis, after your assistant: Slack shows the "
            "name it was made with until then.") in text
    assert text.index("The manifest:") < text.index("Saving it also names the app Jarvis")


# --- an install from before user tokens ---------------------------------------------------------


def test_an_older_install_is_shown_how_to_update_the_app_it_has(settings):
    """Bot token and a destination, no user token: the next login takes one, so the
    steps are the ones that change the app there is, one to a line, and never the link
    that would make a second app."""
    settings = older_install(settings, "D0OLDDM")

    text = said(settings)

    assert "api.slack.com/apps?new_app=1" not in text
    assert "from before Keryx asked for a user token. Nothing about it stops working." in text
    lines = text.splitlines()
    for step in (
        "1. Open https://api.slack.com/apps and pick the app you already have for Keryx. Do "
        "not create a second one.",
        "2. Open App Manifest, replace what is there with the manifest below, and click "
        "Save Changes.",
        "3. Open Install App and click Reinstall to Workspace, then Allow: the new "
        "permissions count only after that.",
        "4. Under OAuth & Permissions, the same page as the bot token, copy the User OAuth "
        "Token (xoxp-…). It says which member you are.",
    ):
        assert step in lines
    assert "5. Optional:" in text and "6." not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(Access.ANNOUNCE, assistant="Lyra")
    assert "hand the user token over on standard input" in text
    assert "keryx auth login slack --stdin\n" in text
    assert text.endswith("The user token says which member you are: reports keep going to "
                         "D0OLDDM, and nothing else is asked.")


def test_an_older_install_keeps_posting_untouched_and_is_asked_only_at_the_next_login(
    settings,
):
    """No user token, no owner on file: it sends, it lacks nothing it runs on, and the
    login is where the user token is asked for, in the sentence every missing token gets."""
    settings = older_install(settings, "D0OLDDM")
    [one] = workspaces(settings)

    assert one.sends and one.missing == () and one.owner_id == ""
    with pytest.raises(SlackSetupError) as asked:
        connect(settings, FakeSlack(), token=TOKEN, user=False)
    assert str(asked.value) == (
        "the announce level also needs the user token (xoxp-), one to a line. Nothing was "
        "saved")
    assert saved(settings) == {"channel_id": "D0OLDDM"} or channel(settings) == "D0OLDDM"


def test_that_login_establishes_the_owner_and_keeps_the_destination_it_had(settings):
    settings = older_install(settings, "D0OLDDM")

    lines = connect(settings, FakeSlack(), token="", test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", "D0OLDDM")
    assert "it keeps sending to D0OLDDM" in lines and slack.ELSEWHERE in lines
    assert slack.OWNER_NOTED.format(who="U0OWNER") in lines


def test_a_user_token_from_another_workspace_is_refused_at_every_level(settings):
    fake = FakeSlack(user=Workspace(team="Other", team_id="T0OTHER", user_id="U0ELSE"))

    for level in Access:
        with pytest.raises(SlackSetupError, match="belongs to another workspace than the bot"):
            connect(settings, fake, token=f"{TOKEN}\n{APP}" if level.reaches(
                Access.CONVERSE) else TOKEN, access=level)
    assert ConfigStore()._secrets() == {}


def test_below_read_the_user_token_is_asked_who_it_is_and_for_no_scope(settings):
    """One that carries only the least scope there is, or more than the level asks."""
    for scopes in (("emoji:read",), USER_SCOPES, None):
        fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                        scopes=scopes))
        connect(settings, fake, token=f"{TOKEN}\n{APP}", access=Access.CONVERSE, test=False)
        assert saved(settings)["owner_id"] == "U0OWNER"


def test_lowering_converse_to_announce_keeps_the_user_token_that_only_says_who_they_are(
    settings,
):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    lines: list[str] = []

    slack.lower(settings, ConfigStore(), workspace="default", access=Access.ANNOUNCE,
                echo=lines.append)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_USER_TOKEN": USER}
    assert lines[1] == "SLACK_APP_TOKEN is forgotten."


# --- the app's icon ------------------------------------------------------------------------------


def test_the_packaged_icon_is_a_square_png_slack_takes():
    """Slack takes an app icon between 512 and 2000 pixels square (`apps.icon.set`'s
    `invalid_icon_size`). Read from the file's own header: its signature, then the
    width and height its first chunk states."""
    data = slack.ICON.read_bytes()

    assert slack.ICON.is_absolute() and slack.ICON.name == "slack_app_icon.png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = (int.from_bytes(data[at:at + 4], "big") for at in (16, 20))
    assert (width, height) == (1024, 1024)
    assert len(data) < 100_000, "small enough to ship in the wheel without a thought"


def test_a_new_apps_steps_end_with_the_optional_icon_and_the_file_on_its_own_line(settings):
    lines = said(settings, access=Access.CONVERSE).splitlines()

    step = next(line for line in lines if "Display Information" in line)
    assert step.startswith("6. Optional: under Basic Information, Display Information,")
    assert "Skipping it changes nothing else" in step
    path = lines[lines.index(step) + 1].strip()
    assert path == str(slack.ICON) and Path(path).is_file()
    assert "7." not in "\n".join(lines)


@pytest.mark.parametrize("older", [False, True])
def test_updating_an_app_there_is_offers_the_icon_too(settings, older):
    """Raising a level, and an install from before user tokens: the same optional step,
    after the tokens."""
    text = (said(older_install(settings, "D0OLDDM")) if older
            else said(connected_at(settings), access=Access.CONVERSE))

    lines = text.splitlines()
    step = next(line for line in lines if "Display Information" in line)
    assert step.startswith("5. Optional:") and "Skipping it changes nothing else" in step
    assert lines[lines.index(step) + 1].strip() == str(slack.ICON)


def test_a_connected_workspace_is_told_where_the_icon_is(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    lines = said(settings).splitlines()

    told = next(line for line in lines if "Display Information" in line)
    assert told.startswith("Optional:") and lines[lines.index(told) + 1].strip() == str(slack.ICON)


def test_the_wizards_guide_has_the_same_optional_icon_step():
    guide = slack.guide(Access.ANNOUNCE, app="Lyra")
    raised = slack.guide(Access.CONVERSE, app="Lyra", raising=True, have=("bot",))

    for text, number in ((guide, 5), (raised, 6)):
        assert f"{number}. Optional: under **Basic Information**, **Display Information**" in text
        assert f"`{slack.ICON}`" in text
        assert f"{number + 1}." not in text
