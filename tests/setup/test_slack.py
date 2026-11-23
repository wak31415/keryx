"""`keryx auth login slack`: the link, then a token on standard input and nothing to ask.

Slack is three fakes here; nothing reaches it.
"""

import json
import tomllib
from dataclasses import dataclass, field

import pytest

from keryx import plugins
from keryx.config.store import ConfigError, ConfigStore
from keryx.integrations.slack import (
    Access,
    Connection,
    Member,
    SlackResult,
    Workspace,
    app_manifest,
    new_app_url,
)
from keryx.plugins.slack import slack_route, workspaces
from keryx.setup import slack
from keryx.setup.slack import (
    CONVERSE_TEST_MESSAGE,
    LISTEN_NOTE,
    MEANING,
    READ_NOTE,
    TEST_MESSAGE,
    SlackApi,
    SlackSetupError,
)

TOKEN = "xoxb-very-secret"


@dataclass
class FakeSlack:
    workspace: Workspace = field(default_factory=lambda: Workspace(team="Acme"))
    member: Member = field(default_factory=lambda: Member(user_id="U0OWNER", name="Sam Doe"))
    sent: SlackResult = field(default_factory=SlackResult)
    #: What `auth.test` says of a user token, and `apps.connections.open` of an app one.
    user: Workspace = field(default_factory=lambda: Workspace(team="Acme", user_id="U0OWNER"))
    opened: Connection = field(default_factory=lambda: Connection(url="wss://x/?ticket=t"))
    calls: list = field(default_factory=list)

    def api(self) -> SlackApi:
        def auth_test(token):
            self.calls.append(("auth", token))
            return self.user if token.startswith("xoxp-") else self.workspace

        return SlackApi(
            auth_test=auth_test,
            lookup_by_email=lambda token, email: self.calls.append(("member", token, email))
            or self.member,
            post_text=lambda token, channel, text: self.calls.append(("post", channel, text))
            or self.sent,
            open_connection=lambda token: self.calls.append(("open", token)) or self.opened,
        )


def connect(settings, fake, *, token=TOKEN, email="sam@example.com", test=True,
            **more) -> list[str]:
    lines: list[str] = []
    slack.connect(settings, ConfigStore(), tokens=token, email=email, test=test,
                  echo=lines.append, api=fake.api(), **more)
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


def said_without_email(settings) -> str:
    lines: list[str] = []
    slack.start(settings, lines.append)
    return lines[-1]


def test_the_next_command_says_what_happens_without_an_address(settings):
    """Each sentence is what `connect` then does: the tests below hold it to them."""
    google = settings.model_copy(update={"user_google_email": "sam@example.com"})

    assert said_without_email(settings) == (
        "Without --email there is no address to look for, and it stops."
    )
    assert said_without_email(google) == "Without --email it looks for sam@example.com."

    plugins.write_config(settings, "send_to_slack", {"channel_id": "U0MEMBER"})
    assert said_without_email(google) == "Without --email it keeps sending to U0MEMBER."

    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    assert said_without_email(google) == (
        "The channel on file (D123) is a direct message with the app its token came from, "
        "so a different token does not keep it: without --email it looks for sam@example.com."
    )


def test_settings_that_do_not_parse_do_not_stop_the_link(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "send_to_slack").write_text("channel_id = [")

    assert said_without_email(settings).startswith("Without --email there is no address")


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


def test_no_test_message_when_it_is_not_wanted_and_it_says_none_was_sent(settings):
    fake = FakeSlack()

    lines = connect(settings, fake, test=False)

    assert not any(call[0] == "post" for call in fake.calls)
    assert lines[-1] == "no test message was sent (--no-test), so nothing has shown it arrives"


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
    [("", "no token on standard input"), ("hunter2-secret", "a bot token starts xoxb-")],
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


def test_a_token_that_could_not_be_checked_is_not_called_refused(settings):
    fake = FakeSlack(workspace=Workspace(error="http_503"))

    with pytest.raises(SlackSetupError) as stopped:
        connect(settings, fake)

    assert str(stopped.value) == (
        "the token could not be checked (Slack answered HTTP 503); nothing was saved"
    )
    assert ConfigStore()._secrets() == {}


def test_an_address_slack_cannot_find_saves_nothing_and_says_what_to_do(settings):
    fake = FakeSlack(member=Member(error="users_not_found"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake)

    message = str(refused.value)
    assert "(users_not_found)" in message and "Nothing was saved" in message
    assert "keryx config set SLACK_BOT_TOKEN --stdin" in message
    assert "keryx plugins install send_to_slack --set channel_id=ID" in message
    assert TOKEN not in message
    assert ConfigStore()._secrets() == {}
    assert not plugins.config_path(settings, "send_to_slack").exists()


def test_a_test_message_that_fails_is_an_error_and_undoes_nothing(settings):
    fake = FakeSlack(sent=SlackResult(error="channel_not_found"))

    with pytest.raises(SlackSetupError) as failed:
        connect(settings, fake)

    assert str(failed.value) == (
        "everything is saved, but the test message to U0OWNER did not go through: "
        "Slack does not know that channel or member (channel_not_found)"
    )
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert plugins.is_on(settings, "send_to_slack") and channel(settings) == "U0OWNER"


# --- a token that is not the one on file brings its destination with it ------------------
#
# A direct message's id belongs to the app that opened it. A new token is presumed to be
# another app's, so that id is never kept beside it; a member's id or a channel's is.


def with_token(settings, token="xoxb-the-old-one", **more):
    return settings.model_copy(update={"slack_bot_token": token, **more})


def test_a_new_token_never_keeps_a_direct_messages_id_and_looks_the_owner_up(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    fake = FakeSlack()

    lines = connect(with_token(settings, user_google_email="sam@example.com"), fake, email=None)

    assert channel(settings) == "U0OWNER"
    assert fake.calls == [
        ("auth", TOKEN), ("member", TOKEN, "sam@example.com"), ("post", "U0OWNER", TEST_MESSAGE)
    ]
    assert "found Sam Doe (U0OWNER): messages go to them" in lines


def test_a_new_token_with_only_a_direct_messages_id_and_no_address_changes_nothing(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-the-old-one"})
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as stopped:
        connect(with_token(settings), fake, email=None)

    assert str(stopped.value) == (
        "the channel on file (D123) is a direct message with the app the old token came "
        "from, and this token cannot post to it: add --email ADDRESS, the one you use in "
        "Slack. Nothing was saved"
    )
    assert fake.calls == []
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-the-old-one"}
    assert channel(settings) == "D123"


def test_an_address_slack_cannot_find_leaves_the_old_token_and_channel_working(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-the-old-one"})
    fake = FakeSlack(member=Member(error="missing_scope"))

    with pytest.raises(SlackSetupError, match="Nothing was saved"):
        connect(with_token(settings), fake)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-the-old-one"}
    assert channel(settings) == "D123"


@pytest.mark.parametrize("on_file", ["U0MEMBER", "W0MEMBER", "C0CHANNEL"])
def test_a_new_token_keeps_a_member_or_a_channel_and_tests_it(settings, on_file):
    plugins.write_config(settings, "send_to_slack", {"channel_id": on_file})
    fake = FakeSlack()

    lines = connect(with_token(settings, user_google_email="sam@example.com"), fake, email=None)

    assert channel(settings) == on_file
    assert fake.calls == [("auth", TOKEN), ("post", on_file, TEST_MESSAGE)]
    assert f"it keeps sending to {on_file}" in lines
    assert lines[-1] == "sent a test message: it is in Slack, under Apps"


def test_the_same_token_again_keeps_even_a_direct_messages_id(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    fake = FakeSlack()

    lines = connect(with_token(settings, TOKEN), fake, email=None)

    assert channel(settings) == "D123"
    assert fake.calls == [("auth", TOKEN), ("post", "D123", TEST_MESSAGE)]
    assert not any("keryx restart" in line for line in lines)


def test_a_kept_destination_that_fails_its_test_says_how_to_change_it(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "C0CHANNEL"})
    fake = FakeSlack(sent=SlackResult(error="not_in_channel"))

    with pytest.raises(SlackSetupError) as failed:
        connect(with_token(settings), fake, email=None)

    assert str(failed.value) == (
        "everything is saved, but the test message to C0CHANNEL did not go through: Slack "
        "said not_in_channel. To send to yourself instead, run it again with --email ADDRESS"
    )


def test_an_address_given_replaces_the_channel_there_was(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "U0MEMBER"})

    connect(with_token(settings, TOKEN), FakeSlack())

    assert channel(settings) == "U0OWNER"


def test_a_token_that_replaces_another_says_a_running_keryx_needs_a_restart(settings):
    plugins.write_config(settings, "send_to_slack", {"channel_id": "U0MEMBER"})

    replaced = connect(with_token(settings), FakeSlack(), email=None)
    first = connect(settings, FakeSlack())

    note = "A Keryx that started with the old token keeps it until `keryx restart`."
    assert note in replaced and note not in first


# --- beside an MCP server that carries Slack ---------------------------------------------


@pytest.fixture
def chat_server(settings, tmp_path, monkeypatch):
    """`send_to_slack` on, with its token and channel both from the `chat` MCP server."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    env = {"SLACK_BOT_TOKEN": "xoxb-the-servers", "SLACK_CHANNEL_ID": "D0SERVER"}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"chat": {"env": env}}}))
    plugins.write_config(settings, "send_to_slack", {"mcp_server": "chat"})
    plugins.install(settings, "send_to_slack")
    return settings


def test_a_token_is_never_paired_with_the_mcp_servers_channel(chat_server):
    """With no address it stops: the server's channel is a direct message with *its* app."""
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as stopped:
        connect(chat_server, fake, email=None)

    assert str(stopped.value) == (
        "a token of its own needs somewhere of its own to send, and the channel so far is "
        "the chat MCP server's: add --email ADDRESS, the one you use in Slack. Nothing was "
        "saved"
    )
    assert fake.calls == [] and ConfigStore()._secrets() == {}
    route = slack_route(chat_server)
    assert (route.token, route.channel) == ("xoxb-the-servers", "D0SERVER")


def test_beside_an_mcp_server_a_token_brings_its_own_destination_and_says_so(chat_server):
    fake = FakeSlack()
    settings = chat_server.model_copy(update={"user_google_email": "sam@example.com"})

    lines = connect(settings, fake, email=None)

    assert ("member", TOKEN, "sam@example.com") in fake.calls
    assert ("post", "U0OWNER", TEST_MESSAGE) in fake.calls
    assert (
        "note: send_to_slack now posts with this token to U0OWNER, not with the chat MCP "
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


# --- the access level comes first ---------------------------------------------------------

APP, USER = "xapp-very-secret", "xoxp-very-secret"


def said(settings, **options) -> str:
    lines: list[str] = []
    slack.start(settings, lines.append, **options)
    return "\n".join(lines)


def saved(settings) -> dict:
    return tomllib.loads(plugins.config_path(settings, "send_to_slack").read_text())


def connected_at(settings, level="announce", **tokens):
    """`settings` with a first workspace already connected at `level`."""
    plugins.write_config(settings, "send_to_slack", {
        "channel_id": "U0OWNER", "owner_id": "U0OWNER", "access": level, "team": "Acme",
    })
    plugins.install(settings, "send_to_slack")
    secrets = {"SLACK_BOT_TOKEN": TOKEN, **tokens}
    ConfigStore().set(secrets)
    return settings.model_copy(update={key.lower(): value for key, value in secrets.items()})


def test_with_nothing_chosen_every_level_is_said_in_plain_words_before_the_link(settings):
    text = said(settings)

    for level in Access:
        assert f"  {level}: {MEANING[level]}" in text
    assert text.index(MEANING[Access.READ]) < text.index(new_app_url())
    assert "What follows is announce; --access converse or --access read chooses another." in text
    assert "reads nothing there" in MEANING[Access.ANNOUNCE]
    assert "direct messages you type to the Keryx app" in MEANING[Access.CONVERSE]
    assert "without the phone PIN" in MEANING[Access.CONVERSE]
    assert "read Slack as you" in MEANING[Access.READ]


def test_a_chosen_level_says_what_it_sees_then_its_own_link_steps_and_command(settings):
    text = said(settings, access=Access.CONVERSE)

    assert text.startswith(f"Access level converse: {MEANING[Access.CONVERSE]}")
    assert new_app_url(Access.CONVERSE) in text.splitlines()
    assert new_app_url() not in text
    assert "4. Under Basic Information, App-Level Tokens, click Generate Token and Scopes" in text
    assert "connections:write" in text and "5." not in text
    assert "hand the tokens over on standard input, never on the command line, one to a line" in (
        text
    )
    assert "keryx auth login slack --stdin --access converse --email ADDRESS" in text
    assert "**" not in text and "`" not in text and "·" not in text


def test_read_asks_for_three_tokens_and_no_address_and_says_nothing_reads_yet(settings):
    text = said(settings, access=Access.READ)

    assert new_app_url(Access.READ) in text.splitlines()
    assert "5. Under OAuth & Permissions, copy the User OAuth Token (xoxp-…)." in text
    assert READ_NOTE in text
    assert "keryx auth login slack --stdin --access read\n" in text
    assert "--email" not in text
    assert text.endswith("The user token says which member you are, so no address is needed.")


def test_raising_the_level_of_an_app_there_is_never_makes_a_second_one(settings):
    settings = connected_at(settings)

    text = said(settings, access=Access.CONVERSE)

    assert "api.slack.com/apps?new_app=1" not in text
    assert "default is connected at the announce level, so the app you have is changed" in text
    assert "pick the Keryx app you already have. Do not create a second one." in text
    assert "2. Open App Manifest, replace what is there with the manifest below" in text
    assert "3. Open Install App and click Reinstall to Workspace" in text
    assert "4. Under Basic Information, App-Level Tokens" in text and "5." not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("The bot token")]
    assert json.loads(manifest) == app_manifest(Access.CONVERSE)
    assert "The bot token stays as it is." in text
    assert "hand the app-level token over on standard input" in text
    assert "keryx auth login slack --stdin --access converse --email ADDRESS" in text


def test_a_connected_workspace_asked_about_again_names_its_own_level(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    text = said(settings)

    assert text.startswith("Access level converse:")
    assert new_app_url(Access.CONVERSE) in text
    assert "keryx auth login slack --stdin --email ADDRESS" in text


def test_converse_takes_two_tokens_in_either_order_and_knows_who_the_owner_is(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"))

    lines = connect(settings, fake, token=f"{APP}\n\n  {TOKEN}\n", access=Access.CONVERSE)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}
    assert saved(settings) == {
        "channel_id": "U0OWNER", "access": "converse", "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "mcp_server": "",
    }
    assert lines[0] == (
        "connected to the Acme workspace at the converse level; SLACK_BOT_TOKEN and "
        "SLACK_APP_TOKEN saved"
    )
    assert LISTEN_NOTE in lines
    assert fake.calls == [
        ("auth", TOKEN), ("open", APP), ("member", TOKEN, "sam@example.com"),
        ("post", "U0OWNER", TEST_MESSAGE + CONVERSE_TEST_MESSAGE),
    ]
    assert not any("secret" in line for line in lines)


def test_a_level_names_the_token_it_still_needs_and_saves_nothing(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, access=Access.READ)

    assert str(refused.value) == (
        "the read level also needs the app-level token (xapp-) and the user token (xoxp-), "
        "one to a line. Nothing was saved"
    )
    assert fake.calls == [] and ConfigStore()._secrets() == {}


def test_a_token_the_level_does_not_use_is_named_and_nothing_is_saved(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{TOKEN}\n{APP}\n{USER}", access=Access.CONVERSE)

    assert str(refused.value) == (
        "the converse level does not use a user token (xoxp-): leave it out, or choose a "
        "higher level with --access. Nothing was saved"
    )
    with pytest.raises(SlackSetupError, match="does not use an app-level token"):
        connect(settings, fake, token=f"{TOKEN}\n{APP}")
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
    assert not plugins.config_path(settings, "send_to_slack").exists()


def test_raising_the_level_needs_only_the_token_that_is_new(settings):
    settings = connected_at(settings)
    fake = FakeSlack()

    lines = connect(settings, fake, token=APP, email=None, access=Access.CONVERSE, test=False)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}
    assert saved(settings)["access"] == "converse" and saved(settings)["owner_id"] == "U0OWNER"
    assert fake.calls == [("auth", TOKEN), ("open", APP)]
    assert "it keeps sending to U0OWNER" in lines
    assert not any("keryx restart`." in line and "old token" in line for line in lines)


def test_with_every_token_on_file_nothing_need_be_typed_again(settings):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)
    fake = FakeSlack()

    connect(settings, fake, token="", email=None, test=False)

    assert fake.calls == [("auth", TOKEN), ("open", APP)]
    assert saved(settings)["access"] == "converse"


def test_read_knows_the_owner_from_the_user_token_and_needs_no_address(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}", email=None,
                    access=Access.READ)

    assert ConfigStore()._secrets() == {
        "SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP, "SLACK_USER_TOKEN": USER,
    }
    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "U0SAM")
    assert "you are U0SAM there: messages go to you" in lines
    assert READ_NOTE in lines and LISTEN_NOTE in lines
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
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"), user=user)

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}", access=Access.READ)

    assert str(refused.value) == f"the user token cannot be used: {why}. Nothing was saved"
    assert ConfigStore()._secrets() == {}


def test_at_read_a_destination_on_file_is_kept_and_the_token_still_says_who(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "send_to_slack", {"channel_id": "C0CHANNEL", "owner_id": ""})
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{APP}", email=None, access=Access.READ,
                    test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "C0CHANNEL")
    assert any(line.startswith("Reports go to C0CHANNEL, which is not your direct messages")
               for line in lines)


def test_converse_refuses_a_destination_that_does_not_say_who_the_owner_is(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "send_to_slack", {"channel_id": "C0CHANNEL", "owner_id": ""})
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=APP, email=None, access=Access.CONVERSE)

    assert str(refused.value) == (
        "at the converse level Keryx acts only on messages from your own member id, and "
        "C0CHANNEL is not one: add --email ADDRESS, the one you use in Slack. Nothing was "
        "saved"
    )
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert saved(settings)["access"] == "announce"


def test_an_owner_on_file_stands_for_the_same_app_and_not_for_another(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "send_to_slack", {"channel_id": "C0CHANNEL"})

    connect(settings, FakeSlack(), token=APP, email=None, access=Access.CONVERSE, test=False)
    assert saved(settings)["owner_id"] == "U0OWNER"

    with pytest.raises(SlackSetupError, match="acts only on messages from your own member id"):
        connect(settings, FakeSlack(), token=f"xoxb-another-app\n{APP}", email=None,
                access=Access.CONVERSE)


def test_a_lower_level_given_with_a_token_forgets_what_it_no_longer_uses(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    lines = connect(settings, FakeSlack(), token=TOKEN, email=None, access=Access.CONVERSE,
                    test=False)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}
    assert saved(settings)["access"] == "converse"
    assert "SLACK_USER_TOKEN is forgotten: the converse level does not use it" in lines


def test_lowering_asks_slack_nothing_and_forgets_the_tokens_at_once(settings):
    settings = connected_at(settings, "read", SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    done = slack.lower(settings, ConfigStore(), workspace="default", access=Access.ANNOUNCE,
                       echo=lines.append)

    assert done and ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert saved(settings)["access"] == "announce"
    assert lines[0] == f"default is now at the announce level: {MEANING[Access.ANNOUNCE]}"
    assert lines[1] == "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten."
    assert "stops acting on your messages there at once" in lines[2]
    assert json.loads(lines[3]) == app_manifest(Access.ANNOUNCE)
    assert [each.access for each in workspaces(settings)] == [Access.ANNOUNCE]


@pytest.mark.parametrize("level", [Access.CONVERSE, Access.READ])
def test_the_same_level_or_a_higher_one_is_not_a_lowering(settings, level):
    settings = connected_at(settings, "converse", SLACK_APP_TOKEN=APP)

    assert not slack.lower(settings, ConfigStore(), workspace="default", access=level,
                           echo=pytest.fail)
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}


def test_a_workspace_that_is_not_connected_has_nothing_to_lower(settings):
    assert not slack.lower(settings, ConfigStore(), workspace="default",
                           access=Access.ANNOUNCE, echo=pytest.fail)


# --- a further workspace ------------------------------------------------------------------


def test_a_further_workspace_has_its_own_app_tokens_owner_and_destination(settings):
    settings = connected_at(settings)
    fake = FakeSlack(workspace=Workspace(team="Lab", team_id="T0LAB"),
                     member=Member(user_id="U0LAB", name="Sam at the lab"))

    lines = connect(settings, fake, token="xoxb-lab-secret\nxapp-lab-secret",
                    email="sam@lab.example", workspace="lab", access=Access.CONVERSE)

    assert ConfigStore()._secrets() == {
        "SLACK_BOT_TOKEN": TOKEN,
        "SLACK_WORKSPACE_TOKENS": {"lab": {"bot": "xoxb-lab-secret", "app": "xapp-lab-secret"}},
    }
    data = saved(settings)
    assert data["workspaces"] == {"lab": {
        "channel_id": "U0LAB", "access": "converse", "owner_id": "U0LAB", "team": "Lab",
        "team_id": "T0LAB",
    }}
    # The first workspace is left exactly as it was.
    assert (data["channel_id"], data["access"], data["team"]) == ("U0OWNER", "announce", "Acme")
    assert lines[0] == (
        "connected to the Lab workspace as lab, at the converse level; its tokens are saved"
    )
    assert ("post", "U0LAB", TEST_MESSAGE + CONVERSE_TEST_MESSAGE) in fake.calls
    assert not any("secret" in line for line in lines)


def test_a_second_further_workspace_keeps_the_first(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-lab-secret", workspace="lab", test=False)
    settings = settings.model_copy(update={
        "slack_workspace_tokens": ConfigStore()._secrets()["SLACK_WORKSPACE_TOKENS"],
    })

    connect(settings, FakeSlack(), token="xoxb-work-secret", workspace="work", test=False)

    assert ConfigStore()._secrets()["SLACK_WORKSPACE_TOKENS"] == {
        "lab": {"bot": "xoxb-lab-secret"}, "work": {"bot": "xoxb-work-secret"},
    }
    assert set(saved(settings)["workspaces"]) == {"lab", "work"}


def test_a_further_workspace_is_lowered_by_its_own_name(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(), token="xoxb-lab-secret\nxapp-lab-secret", workspace="lab",
            access=Access.CONVERSE, test=False)
    settings = settings.model_copy(update={
        "slack_workspace_tokens": ConfigStore()._secrets()["SLACK_WORKSPACE_TOKENS"],
    })
    lines: list[str] = []

    assert slack.lower(settings, ConfigStore(), workspace="lab", access=Access.ANNOUNCE,
                       echo=lines.append)

    assert ConfigStore()._secrets()["SLACK_WORKSPACE_TOKENS"] == {"lab": {"bot": "xoxb-lab-secret"}}
    assert saved(settings)["workspaces"]["lab"]["access"] == "announce"
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

    assert new_app_url(Access.CONVERSE) in text
    assert "keryx auth login slack --stdin --workspace lab --access converse --email ADDRESS" in (
        text
    )


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

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxe.xoxb-1-very-secret"}


def test_the_link_for_a_further_workspace_says_when_the_first_is_not_there_yet(settings):
    text = said(settings, workspace="lab")

    assert text.startswith(
        "lab would be added beside your first workspace, and there is none yet: run this "
        "without --workspace first."
    )
    assert "would be added beside" not in said(connected_at(settings), workspace="lab")


def test_a_user_token_is_never_assumed_to_be_the_bot_tokens_workspace(settings):
    """A bot token Slack named no workspace for cannot vouch for a user token."""
    fake = FakeSlack(workspace=Workspace(team="Acme"),
                     user=Workspace(team="Acme", user_id="U0SAM"))

    with pytest.raises(SlackSetupError, match="is not known, so it cannot be matched"):
        connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}", access=Access.READ)

    assert ConfigStore()._secrets() == {}
