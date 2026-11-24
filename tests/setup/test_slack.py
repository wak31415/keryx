"""`keryx auth login slack`: the link, then a token on standard input and nothing to ask.

Slack is three fakes here; nothing reaches it.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from slack_sets import BASE, EVERYTHING, READ, READS, REQUESTS, SELECTIONS, SEND, named, stored

from keryx import plugins
from keryx.config.store import ConfigError, ConfigStore
from keryx.integrations import slack as slack_api
from keryx.integrations.slack import (
    Connection,
    Permission,
    SlackResult,
    Workspace,
    app_manifest,
    new_app_url,
)
from keryx.plugins.slack import connected, slack_route, workspaces
from keryx.setup import slack
from keryx.setup.slack import (
    LISTEN_NOTE,
    REQUESTS_TEST_MESSAGE,
    SlackApi,
    SlackSetupError,
    owner_line,
)
from keryx.setup.slack import test_message as said_to_test

TOKEN = "xoxb-very-secret"
#: What the app is called when nobody has named the assistant: Lyra, the default.
LYRA = "Lyra"
#: The one message that shows a connection arriving, at `announce`.
TEST_MESSAGE = said_to_test(BASE, LYRA)
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

    slack.start(settings, lines.append, allowed=BASE)

    text = "\n".join(lines)
    assert new_app_url(assistant="Lyra") in lines  # on a line of its own, to be relayed whole
    assert "1. Open the link" in text and "2. Install the app to your workspace" in text
    assert ("3. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…), "
            "then the Bot User OAuth Token (xoxb-…) below it.") in text
    assert ("Then hand the tokens over on standard input, never on the command line, one "
            "to a line in any order:\n  keryx auth login slack --stdin --allow none") in text
    assert lines[-1] == ("The user token says which member you are: reports go to you, and "
                         "nothing else is asked.")
    assert "--email" not in text and "address" not in text.lower()
    assert "**" not in text and "`" not in text


def test_whether_an_app_is_connected_is_whether_keryx_holds_its_bot_token(settings):
    assert not slack.is_connected(settings)
    assert slack.is_connected(settings.model_copy(update={"slack_bot_token": TOKEN}))
    assert not slack.is_connected(settings.model_copy(update={"slack_bot_token": TOKEN}),
                                  "lab")
    # Settings that do not parse are not a connection, and are not raised from here.
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text("channel_id = [")
    assert not slack.is_connected(settings.model_copy(update={"slack_bot_token": TOKEN}))


def test_settings_that_do_not_parse_do_not_stop_the_link(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text("channel_id = [")
    lines: list[str] = []

    slack.start(settings, lines.append, allowed=BASE)

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
        "server's token and channel, and subagents are told to send with `keryx slack "
        "report`, no longer through that server"
    ) in lines
    route = slack_route(settings.model_copy(update={"slack_bot_token": TOKEN}))
    assert (route.token, route.channel, route.mcp_server) == (TOKEN, "U0OWNER", None)
    # The server's name is left in the file: logging out is what makes it count again.
    assert plugins.read_config(settings, "slack_report")["mcp_server"] == "chat"


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


# --- what is ticked comes first ----------------------------------------------------------

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


def connected_at(settings, allowed=BASE, user=True, **tokens):
    """`settings` with a first workspace already connected with `allowed` ticked, its user
    token on file unless `user` is False (a workspace connected before user tokens were
    asked for)."""
    plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "owner_id": "U0OWNER", "allow": stored(allowed), "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    plugins.install(settings, "slack_report")
    secrets = {"SLACK_BOT_TOKEN": TOKEN, **({"SLACK_USER_TOKEN": USER} if user else {}),
               **tokens}
    ConfigStore().set(secrets)
    return settings.model_copy(update={key.lower(): value for key, value in secrets.items()})


def test_with_nothing_connected_and_nothing_said_the_choices_are_printed_and_none_is_guessed(
    settings,
):
    """No terminal to tick at and no `--allow`: every choice in full, by the name the
    option takes, and the shapes of the command. No link, because no app was chosen."""
    lines: list[str] = []

    assert slack.start(settings, lines.append) is None

    text = "\n".join(lines)
    assert lines[0] == ("Always on: Keryx sends you reports and files on Slack, as the Lyra "
                        "app. That alone reads nothing.")
    for permission in Permission:
        assert f"  {permission:<8}  {slack.MEANING[permission].format(app=LYRA)}" in text
    assert "api.slack.com/apps" not in text and "Create your Slack app" not in text
    assert "  keryx auth login slack --allow none\n" in text
    assert "  keryx auth login slack --allow requests --allow read\n" in text
    assert ("  requests and read together: You can also mention the app (@Lyra) in any "
            "conversation it has been added to") in text
    assert "  send without read: To find who you mean it sees the names" in text
    assert lines[-1].endswith("Nothing was chosen for you, and nothing was saved.")
    assert not plugins.config_path(settings, "slack_report").exists()
    assert "**" not in text and "·" not in text
    for level in ("announce", "converse", "--access", "level"):
        assert level not in text


def test_each_box_says_what_keryx_may_do_and_what_it_sees_in_plain_words():
    boxes = slack.choices(LYRA, ticked=REQUESTS)

    assert [box.value for box in boxes] == ["requests", "read", "send"]
    assert [box.checked for box in boxes] == [True, False, False]
    assert [(box.label, box.hint) for box in boxes] == [
        ("Act on what you type to the Lyra app",
         "each message starts or resumes work, with no PIN; it sees only those messages"),
        ("Read your Slack as you, when you ask",
         "your conversations and search; with the first box, it also answers your @Lyra "
         "in a channel, where others read it"),
        ("Send messages as you, after a yes",
         "read back to you first; it sees the names of your conversations and of members, "
         "and no messages"),
    ]
    shown = " ".join(f"{box.label} {box.hint}" for box in boxes) + slack.QUESTION + slack.ALWAYS
    for word in ("announce", "converse", "level", "requests", "slack_", "scope", "·"):
        assert word not in shown


def test_what_was_ticked_is_said_in_full_before_the_link():
    assert slack.meanings(BASE, app=LYRA) == []
    requests, = slack.meanings(REQUESTS, app=LYRA)
    assert "every message of yours in its direct messages is a request" in requests
    assert "without the phone PIN" in requests and "@Lyra" in requests
    # The pair unlocks the mention, and says who else reads the answer.
    both = slack.meanings(READS, app=LYRA)
    assert len(both) == 3 and "mention the app (@Lyra)" in both[2]
    assert "the others in the conversation read the answer too" in both[2]
    assert "every channel, private channel, direct message" in both[1]
    # Sending without reading says what that alone lets Keryx see.
    alone = slack.meanings(SEND, app=LYRA)
    assert alone[-1] == slack.SEND_ALONE and "no message" in alone[-1]
    assert "A coding agent working on a request of yours can send one too." in alone[0]
    assert slack.SEND_ALONE not in slack.meanings(READ | SEND, app=LYRA)
    # Adding one box to another says what the pair now unlocks.
    assert slack.meanings(READ, app=LYRA, whole=READS)[-1].startswith(
        "With acting on what you type and reading both ticked, you can also mention")
    assert len(slack.meanings(SEND, app=LYRA, whole=EVERYTHING)) == 1


def test_what_is_ticked_says_what_it_sees_then_its_own_link_steps_and_command(settings):
    text = said(settings, allowed=REQUESTS)

    assert text.startswith(
        "Always on: Keryx sends you reports and files on Slack, as the Lyra app. That "
        "alone reads nothing.\nYou ticked: acting on what you type to the app.\n  "
        + slack.MEANING[Permission.REQUESTS].format(app=LYRA))
    assert new_app_url(REQUESTS, assistant="Lyra") in text.splitlines()
    assert new_app_url(assistant="Lyra") not in text
    assert "2. Basic Information > App-Level Tokens > Generate Token and Scopes: " in text
    assert "connections:write" in text
    assert "4. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token" in text
    assert "5. Optional:" in text and "6." not in text
    assert "hand the tokens over on standard input, never on the command line, one to a line" in (
        text
    )
    assert "keryx auth login slack --stdin --allow requests\n" in text
    assert "--email" not in text
    assert "**" not in text and "`" not in text and "·" not in text


def test_nothing_ticked_says_that_is_all_it_does(settings):
    text = said(settings, allowed=BASE)

    assert "You ticked nothing else, so that is all it does." in text
    assert "keryx auth login slack --stdin --allow none\n" in text
    assert "App-Level Tokens" not in text


def test_requests_and_read_ask_for_three_tokens_and_no_address(settings):
    text = said(settings, allowed=READS)

    assert new_app_url(READS, assistant="Lyra") in text.splitlines()
    assert "You ticked: acting on what you type to the app and reading Slack as you." in text
    assert "4. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…)" in text
    assert "keryx auth login slack --stdin --allow requests --allow read\n" in text
    assert "--email" not in text
    assert text.endswith("The user token says which member you are: reports go to you, and "
                         "nothing else is asked.")


@pytest.mark.parametrize("allowed", [READ, SEND, READ | SEND], ids=named)
def test_reading_or_sending_alone_has_no_app_level_token_and_no_step_for_one(settings, allowed):
    """Only what is ticked varies the steps: with nothing to listen with, the page that
    makes an app-level token is never visited."""
    text = said(settings, allowed=allowed)

    assert new_app_url(allowed, assistant="Lyra") in text.splitlines()
    assert "App-Level Tokens" not in text and "xapp-" not in text
    assert "2. Install the app to your workspace" in text
    assert "3. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…)" in text
    assert f"keryx auth login slack --stdin{slack.allow_flags(allowed)}\n" in text


def test_ticking_more_for_an_app_there_is_never_makes_a_second_one(settings):
    settings = connected_at(settings)

    text = said(settings, allowed=REQUESTS)

    assert "api.slack.com/apps?new_app=1" not in text
    assert text.startswith("Acme is connected. Adding: acting on what you type to the app.\n  "
                           + slack.MEANING[Permission.REQUESTS].format(app=LYRA))
    assert "The app you have is changed, not made again:" in text
    assert "pick the app you already have for Keryx. Do not create a second one." in text
    assert "2. App Manifest: replace what is there with the manifest below" in text
    assert "3. Basic Information > App-Level Tokens > Generate Token and Scopes: " in text
    assert "4. Install App: click Reinstall to Workspace, then Allow." in text
    # The user token and the bot token are on file, so their page is not visited.
    assert "OAuth Tokens" not in text and "5. Optional:" in text and "6." not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(REQUESTS, assistant="Lyra")
    assert "The bot token stays as it is." in text
    assert "hand the app-level token over on standard input" in text
    assert "keryx auth login slack --stdin --allow requests\n" in text


def test_adding_reading_to_requests_says_what_the_pair_unlocks_and_shows_the_manifest(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    text = said(settings, allowed=READS)

    assert text.startswith("Acme is connected. Adding: reading Slack as you.\n")
    assert slack.MEANING[Permission.READ] in text
    assert ("With acting on what you type and reading both ticked, you can also mention "
            "the app (@Lyra)") in text
    assert slack.MEANING[Permission.REQUESTS].format(app=LYRA) not in text  # not new
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(READS, assistant="Lyra")
    assert "api.slack.com/apps?new_app=1" not in text
    assert ("Every token this needs is saved already, so there is nothing to hand over. "
            "Once the app is reinstalled:\n  keryx auth login slack --stdin --allow requests "
            "--allow read < /dev/null") in text
    assert "If Slack shows a different User OAuth Token after the reinstall" in text


def test_a_workspace_connected_with_exactly_that_is_told_so_and_never_shown_the_link(settings):
    """Asking again must not offer the link that makes a second app."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    for text in (said(settings), said(settings, allowed=REQUESTS)):
        assert text.startswith(
            "Acme is connected, and nothing was changed. On: sending you reports and "
            "files, acting on what you type to the app; off: reading Slack as you, "
            "sending messages as you.")
        assert "api.slack.com/apps?new_app=1" not in text and "Create your Slack app" not in text
        assert ("Only messages from U0OWNER are acted on there: every one typed to the "
                "Lyra app itself. That member id") in text
        assert "mentions it" not in text  # a mention is a request only with reading too
        assert ("To change what is on: keryx auth login slack at a terminal, and tick or "
                "untick it (with no terminal: --allow none, or --allow once for each of "
                "requests, read, send that is to be on)") in text
        assert "To replace a token: keryx auth login slack --stdin, which asks for each" in text
        assert "To disconnect it: keryx auth logout slack" in text
        assert "**" not in text and "`" not in text
        assert "level" not in text and "--access" not in text


def test_with_requests_and_reading_the_owner_line_says_a_mention_counts_elsewhere(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP)

    text = said(settings)

    assert owner_line("U0OWNER", READS, LYRA) in text
    assert "and elsewhere only one that mentions it (@Lyra)" in text


def test_a_connected_workspace_is_always_shown_the_manifest_that_renames_its_app(settings):
    """Slack learns the app's name only from a manifest, and is never asked what it is
    called: so after `ASSISTANT_NAME` changes, the next login shows the manifest with the
    new name and how to save it, and says the old name stands until then."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    settings = settings.model_copy(update={"assistant_name": "Jarvis"})

    text = said(settings)

    assert ("The app is called Jarvis in Slack, after your assistant, once its manifest "
            "says so: one made under another name keeps it until then.") in text
    assert "open https://api.slack.com/apps, pick the app, open App Manifest" in text
    manifest = json.loads(text[text.index("click Save Changes:") + len("click Save Changes:"):
                               text.index("\nOptional:")])
    assert manifest == app_manifest(REQUESTS, assistant="Jarvis")
    assert manifest["display_information"]["name"] == "Jarvis"
    assert "every one typed to the Jarvis app itself" in text
    assert "api.slack.com/apps?new_app=1" not in text


def test_a_connected_workspace_that_lacks_something_is_told_what(settings):
    settings = connected_at(settings, REQUESTS)

    assert "It is without its app token." in said(settings)


def test_a_further_workspace_that_is_connected_is_told_so_by_its_own_name(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(workspace=LAB),
            token="xoxb-lab-secret", workspace="lab", test=False)

    text = said(settings, workspace="lab")

    assert text.startswith("lab (Lab) is connected, and nothing was changed. On: sending")
    assert "keryx auth logout slack --workspace lab" in text
    assert "To change what is on: keryx auth login slack --workspace lab at a terminal" in text


def test_when_every_token_is_saved_there_is_nothing_to_hand_over(settings):
    """An app-level token left on file by hand, and requests ticked again."""
    settings = connected_at(settings, BASE, SLACK_APP_TOKEN=APP)

    text = said(settings, allowed=REQUESTS)

    assert "hand nothing over" not in text
    assert "Every token this needs is saved already, so there is nothing to hand over." in text
    assert "keryx auth login slack --stdin --allow requests < /dev/null" in text


def test_requests_takes_three_tokens_in_any_order_and_the_user_token_says_who_is_the_owner(
    settings
):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"))

    lines = connect(settings, fake, token=f"{APP}\n\n  {TOKEN}\n", allowed=REQUESTS)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    assert saved(settings) == {
        "channel_id": "U0OWNER", "allow": ["requests"], "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX", "mcp_server": "",
    }
    assert lines[:2] == [
        "connected to the Acme workspace; SLACK_BOT_TOKEN, SLACK_APP_TOKEN and "
        "SLACK_USER_TOKEN saved",
        "On: sending you reports and files, acting on what you type to the app; off: "
        "reading Slack as you, sending messages as you.",
    ]
    assert LISTEN_NOTE.format(app=LYRA) in lines
    assert fake.calls == [
        ("auth", TOKEN), ("auth", USER), ("open", APP),
        ("post", "U0OWNER", said_to_test(REQUESTS, LYRA)),
    ]
    assert not any("secret" in line for line in lines)


def test_the_token_still_needed_is_named_and_nothing_is_saved(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, allowed=READS, user=False)

    assert str(refused.value) == (
        "this also needs the app-level token (xapp-) and the user token (xoxp-), one to a "
        "line. Nothing was saved"
    )
    assert fake.calls == [] and ConfigStore()._secrets() == {}


def test_a_token_that_what_is_ticked_does_not_use_is_named_and_nothing_is_saved(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{TOKEN}\n{APP}\n{USER}")

    assert str(refused.value) == (
        "what is ticked does not use an app-level token (xapp-): leave it out, or add "
        "--allow requests. Nothing was saved"
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
        connect(settings, fake, token=text, allowed=REQUESTS)

    assert fake.calls == [] and ConfigStore()._secrets() == {}
    assert "hunter2" not in str(refused.value) and "another" not in str(refused.value)


def test_an_app_token_slack_does_not_take_saves_nothing_and_says_what_it_needs(settings):
    fake = FakeSlack(opened=Connection(error="not_allowed_token_type"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=f"{TOKEN}\n{APP}", allowed=REQUESTS)

    assert str(refused.value) == (
        "the app-level token could not be used (that is not the kind of token this call "
        "takes (not_allowed_token_type)): it needs the connections:write scope, and Socket "
        "Mode on in the app. Nothing was saved"
    )
    assert ConfigStore()._secrets() == {}
    assert not plugins.config_path(settings, "slack_report").exists()


def test_ticking_more_needs_only_the_token_that_is_new(settings):
    settings = connected_at(settings)
    fake = FakeSlack()

    lines = connect(settings, fake, token=APP, allowed=REQUESTS, test=False)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    assert saved(settings)["allow"] == ["requests"] and saved(settings)["owner_id"] == "U0OWNER"
    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("open", APP)]
    assert "it keeps sending to U0OWNER" in lines
    assert not any("keryx restart`." in line and "old token" in line for line in lines)


def test_with_every_token_on_file_nothing_need_be_typed_again(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake = FakeSlack()

    connect(settings, fake, token="", user=False, test=False)

    assert fake.calls == [("auth", TOKEN), ("auth", USER), ("open", APP)]
    assert saved(settings)["allow"] == ["requests"]


def test_the_owner_is_known_from_the_user_token_and_needs_no_address(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}",
                    allowed=READS)

    assert ConfigStore()._secrets() == {
        "SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP, "SLACK_USER_TOKEN": USER,
    }
    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "U0SAM")
    assert "you are U0SAM there: reports go to you" in lines
    assert LISTEN_NOTE.format(app=LYRA) in lines
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
        connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}", allowed=READS)

    assert str(refused.value) == f"the user token cannot be used: {why}. Nothing was saved"
    assert ConfigStore()._secrets() == {}


def test_a_destination_on_file_is_kept_and_the_token_still_says_who(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "slack_report", {"channel_id": "C0CHANNEL", "owner_id": ""})
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                     user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    lines = connect(settings, fake, token=f"{USER}\n{APP}", allowed=READS,
                    test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "C0CHANNEL")
    assert any(line.startswith("Reports go to C0CHANNEL, which is not your direct messages")
               for line in lines)


def test_an_owner_on_file_stands_whatever_the_destination(settings):
    settings = connected_at(settings)
    plugins.write_config(settings, "slack_report", {"channel_id": "C0CHANNEL"})

    connect(settings, FakeSlack(), token=APP, allowed=REQUESTS, test=False)

    assert saved(settings)["owner_id"] == "U0OWNER"


def test_a_bot_token_from_another_workspace_is_refused_while_one_is_connected(settings):
    """It would bring another owner with it, and the owner is fixed until logout."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
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
    """An install from before requests: a bot token and where reports go, nothing else."""
    settings = settings.model_copy(update={"slack_bot_token": TOKEN})
    ConfigStore().set({"SLACK_BOT_TOKEN": TOKEN})
    plugins.config_path(settings, "slack_report").parent.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(f'channel_id = "{channel}"\n')
    plugins.install(settings, "slack_report")
    return settings


@pytest.mark.parametrize("channel", ["U0COLLEAGUE", "D0OLDDM", "C0REPORTS"])
def test_where_reports_go_never_makes_anybody_the_owner(settings, channel):
    """The owner is whoever the user token belongs to, and nothing else says who: with
    nothing ticked reports may go to a colleague's member id, which once made the
    colleague the one whose messages start work when requests were switched on."""
    settings = older_install(settings, channel)

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, FakeSlack(), token=APP, user=False, allowed=REQUESTS)
    assert "this also needs the user token (xoxp-)" in str(refused.value)
    assert saved(settings).get("owner_id", "") == ""

    lines = connect(settings, FakeSlack(), token=APP, allowed=REQUESTS, test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", channel)
    assert f"it keeps sending to {channel}" in lines
    assert owner_line("U0OWNER", REQUESTS, LYRA) in lines
    assert "mentions it" not in owner_line("U0OWNER", REQUESTS, LYRA)
    assert "only one that mentions it (@Lyra)" in owner_line("U0OWNER", READS, LYRA)


def test_the_owner_is_recorded_with_nothing_ticked_and_nothing_says_messages_are_acted_on(
    settings,
):
    """So ticking more later changes nothing about who the owner is."""
    lines = connect(settings, FakeSlack(), test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", "U0OWNER")
    assert not any("are acted on there" in line for line in lines)
    assert slack.OWNER_NOTED.format(who="U0OWNER") in lines

    raised = connected_like(settings)
    connect(raised, FakeSlack(), token=APP, allowed=REQUESTS, test=False)
    assert saved(settings)["owner_id"] == "U0OWNER"


def connected_like(settings):
    """`settings` holding what the store now does, as the next command would load them."""
    secrets = ConfigStore()._secrets()
    return settings.model_copy(update={
        "slack_bot_token": secrets.get("SLACK_BOT_TOKEN"),
        "slack_app_token": secrets.get("SLACK_APP_TOKEN"),
        "slack_user_token": secrets.get("SLACK_USER_TOKEN"),
    })


def test_an_owner_on_file_is_kept_with_everything_unticked_for_the_day_it_is_ticked_again(
    settings,
):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    connect(settings, FakeSlack(), token=TOKEN, allowed=BASE, test=False)

    assert (saved(settings)["allow"], saved(settings)["owner_id"]) == ([], "U0OWNER")


def test_there_is_no_way_to_change_the_owner_in_place(settings):
    """One owner, fixed at connect: no option, no argument, no address, nothing but
    logout and login. Somebody else's user token is refused, whoever hands it over."""
    import inspect

    from keryx.setup.auth import LoginOptions

    parameters = inspect.signature(slack.connect).parameters
    assert "owner" not in parameters and "email" not in parameters
    assert not hasattr(LoginOptions(), "owner") and not hasattr(LoginOptions(), "email")
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    theirs = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0COLLEAGUE"))
    for allowed in (BASE, REQUESTS, READS, SEND):
        with pytest.raises(SlackSetupError, match="the user token belongs to U0COLLEAGUE"):
            connect(settings, theirs, token="xoxp-theirs", allowed=allowed, test=False)
        assert saved(settings)["owner_id"] == "U0OWNER"
        assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == USER


def test_logging_out_and_in_again_is_how_somebody_else_connects(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    slack.logout(settings, ConfigStore(), workspace="default", echo=lambda line: None)
    cleared = settings.model_copy(update={"slack_bot_token": None, "slack_app_token": None,
                                          "slack_user_token": None})

    connect(cleared, FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0NEWME")),
            token=f"{TOKEN}\n{APP}", allowed=REQUESTS, test=False)

    assert saved(settings)["owner_id"] == "U0NEWME"


def test_a_user_token_that_is_not_the_owners_on_file_is_refused(settings):
    """Somebody else's token would otherwise make them the owner without a word."""
    settings = connected_at(settings, REQUESTS, user=False, SLACK_APP_TOKEN=APP)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0ELSE"))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token=USER, allowed=READS)

    assert str(refused.value) == (
        "the user token belongs to U0ELSE, and the member whose messages Keryx acts on "
        "there is U0OWNER: paste your own user token. To connect as somebody else, `keryx "
        "auth logout slack` first. Nothing was saved"
    )
    assert saved(settings)["allow"] == ["requests"]
    assert "SLACK_USER_TOKEN" not in ConfigStore()._secrets()


def test_the_first_owner_is_whom_the_user_token_belongs_to(settings):
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"))

    connect(settings, fake, token=f"{USER}\n{TOKEN}\n{APP}",
            allowed=READS, test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0SAM", "U0SAM")


def test_with_requests_a_bot_token_slack_names_no_workspace_for_is_refused(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme"))

    with pytest.raises(SlackSetupError, match="did not say which workspace the bot token"):
        connect(settings, fake, token=f"{TOKEN}\n{APP}", allowed=REQUESTS)

    assert ConfigStore()._secrets() == {}


def test_reading_unticked_with_a_token_given_forgets_what_is_no_longer_used(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    fake = FakeSlack()
    lines = connect(settings, fake, token=TOKEN, user=False, allowed=REQUESTS,
                    test=False)

    # The token that could read Slack as them is not kept once nothing is ticked for it,
    # and Slack is not asked about it: the owner on file stands.
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP}
    assert (saved(settings)["allow"], saved(settings)["owner_id"]) == (["requests"], "U0OWNER")
    assert ("SLACK_USER_TOKEN is forgotten: what is ticked does not use it, and until the "
            "app is reinstalled it could still act as you in Slack") in lines
    assert ("auth", USER) not in fake.calls


def test_reading_unticked_with_the_reinstalled_apps_user_token_keeps_that_one(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    connect(settings, FakeSlack(), token="xoxp-after-reinstall", allowed=REQUESTS,
            test=False)

    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-after-reinstall"


def test_unticking_asks_slack_nothing_and_takes_effect_at_once(settings):
    """Everything unticked: saved, the tokens it no longer uses forgotten, and the
    manifest that takes it away in Slack too printed, whether or not it is ever pasted."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    done = slack.untick(settings, ConfigStore(), workspace="default", allowed=BASE,
                        echo=lines.append)

    assert done.done and (done.removed, done.added) == (READS, BASE)
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN}
    assert saved(settings)["allow"] == []
    assert lines[0] == ("Acme: acting on what you type to the app and reading Slack as you "
                        "are off, from this moment. A running Keryx stops at once.")
    assert lines[1] == "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten."
    assert lines[2] == (
        "The app in Slack still holds the permissions it was made with until you paste "
        "this manifest into its App Manifest page (https://api.slack.com/apps) and "
        "reinstall it:\n")
    assert json.loads(lines[3]) == app_manifest(BASE, assistant="Lyra")
    assert [each.allowed for each in workspaces(settings)] == [BASE]
    # The settings to go on with no longer hold what was forgotten.
    assert (done.settings.slack_app_token, done.settings.slack_user_token) == (None, None)
    assert done.settings.slack_bot_token == TOKEN


@pytest.mark.parametrize(("was", "now", "kept"), [
    # Sending off, reading still on: the user token still reads.
    (EVERYTHING, READS, {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN"}),
    # Requests off, reading on: nothing to listen with, and the user token still reads.
    (READS, READ, {"SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"}),
    # Reading off, requests on: the token that could read as them is not kept.
    (READS, REQUESTS, {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}),
    # Requests off, where the user token could only ever say who they are: it is kept.
    (REQUESTS, BASE, {"SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"}),
    (SEND, BASE, {"SLACK_BOT_TOKEN"}),
])
def test_unticking_forgets_exactly_the_tokens_nothing_ticked_uses_any_more(
    settings, was, now, kept
):
    settings = connected_at(settings, was, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    slack.untick(settings, ConfigStore(), workspace="default", allowed=now,
                 echo=lambda line: None)

    assert set(ConfigStore()._secrets()) == kept
    assert saved(settings)["allow"] == stored(now)
    assert saved(settings)["owner_id"] == "U0OWNER"  # who the owner is never changes


@pytest.mark.parametrize("allowed", [REQUESTS, READS, EVERYTHING], ids=named)
def test_the_same_boxes_or_more_take_nothing_away(settings, allowed):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    done = slack.untick(settings, ConfigStore(), workspace="default", allowed=allowed,
                        echo=pytest.fail)

    assert not done.done and done.removed == BASE and done.added == allowed - REQUESTS
    assert done.settings is settings
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}


def test_one_box_swapped_for_another_takes_the_one_away_now_and_leaves_the_other_to_the_login(
    settings,
):
    """Unticking counts on the machine at once; what was ticked instead still needs the
    app changed and its tokens checked."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    lines: list[str] = []

    done = slack.untick(settings, ConfigStore(), workspace="default", allowed=READ,
                        echo=lines.append)

    assert not done.done and (done.removed, done.added) == (REQUESTS, READ)
    assert saved(settings)["allow"] == []
    assert "SLACK_APP_TOKEN" not in ConfigStore()._secrets()
    assert lines[-1] == ("The app in Slack still holds the permissions it was made with "
                         "until the manifest below is pasted into it.\n")
    assert not any(line.lstrip().startswith("{") for line in lines)  # the login prints it


def test_a_workspace_that_is_not_connected_has_nothing_to_untick(settings):
    done = slack.untick(settings, ConfigStore(), workspace="default", allowed=BASE,
                        echo=pytest.fail)

    assert not done.done and (done.removed, done.added) == (BASE, BASE)


# --- the voice tools follow the ticks ---------------------------------------------------------


def on(settings, name) -> bool:
    return plugins.tool_path(settings, name).is_file()


def loads(settings, name) -> bool:
    return next(one for one in plugins.status(settings) if one.name == name).on


def test_ticking_reading_turns_on_the_voice_tool_that_reads_and_says_so_in_one_line(settings):
    """It took a second command, `keryx plugins install slack_read`, which nobody ran:
    reading was allowed, nothing on a call could read, and the question was dispatched."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    lines = connect(settings, FakeSlack(), token="", allowed=READS, test=False)

    assert on(settings, "slack_read") and loads(settings, "slack_read")
    assert not on(settings, "slack_message")
    assert lines.count(slack.TURNED_ON["slack_read"]) == 1
    assert slack.TURNED_ON["slack_read"] == (
        "slack_read is on, from the next call: a question about your Slack is answered on "
        "the call")


def test_ticking_sending_turns_on_the_one_that_sends_as_you_and_says_it_asks_first(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    plugins.install(settings, "slack_read")

    lines = connect(settings, FakeSlack(), token="", allowed=EVERYTHING, test=False)

    assert on(settings, "slack_message") and loads(settings, "slack_message")
    assert slack.TURNED_ON["slack_message"] in lines
    assert "after it has said who to and the words and you have said yes" in (
        slack.TURNED_ON["slack_message"])
    assert slack.TURNED_ON["slack_read"] not in lines, "it was on before this login"


def test_sending_alone_turns_on_only_the_tool_that_sends(settings):
    lines = connect(settings, FakeSlack(), token=f"{TOKEN}\n{USER}", allowed=SEND, test=False)

    assert on(settings, "slack_message") and loads(settings, "slack_message")
    assert not on(settings, "slack_read")
    assert slack.TURNED_ON["slack_message"] in lines
    assert ConfigStore()._secrets().keys() == {"SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"}


def test_a_first_connection_with_everything_ticked_turns_on_both(settings):
    lines = connect(settings, FakeSlack(), token=f"{TOKEN}\n{APP}\n{USER}",
                    allowed=EVERYTHING, test=False)

    assert on(settings, "slack_read") and on(settings, "slack_message")
    assert lines.index(slack.TURNED_ON["slack_read"]) < lines.index(
        slack.TURNED_ON["slack_message"])


def test_a_login_that_changes_no_box_turns_nothing_on(settings):
    """A token replaced, by somebody who turned the voice tool off by hand: it stays off."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    lines = connect(settings, FakeSlack(), token="", allowed=READS, test=False)

    assert not on(settings, "slack_read")
    assert not any("slack_read is on" in line for line in lines)


def test_a_tool_turned_off_by_hand_comes_back_when_what_is_ticked_next_changes(settings):
    """Removed by hand it stays off through a login that changes nothing; the next change
    to the boxes makes the tools match them again."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    assert not on(settings, "slack_read")

    connect(settings, FakeSlack(), token="", allowed=EVERYTHING, test=False)

    assert on(settings, "slack_read") and on(settings, "slack_message")


@pytest.mark.parametrize("allowed", [BASE, REQUESTS], ids=named)
def test_with_neither_reading_nor_sending_ticked_no_voice_tool_is_turned_on(settings, allowed):
    token = TOKEN if allowed is BASE else f"{TOKEN}\n{APP}"

    connect(settings, FakeSlack(), token=token, allowed=allowed, test=False)

    assert not on(settings, "slack_read") and not on(settings, "slack_message")


def test_a_voice_tool_that_cannot_be_turned_on_is_said_and_stops_nothing(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    plugins.config_path(settings, "slack_read").write_text("model = [")

    lines = connect(settings, FakeSlack(), token="", allowed=READS, test=False)

    assert saved(settings)["allow"] == ["requests", "read"] and not on(settings, "slack_read")
    assert any(line.startswith("slack_read was not turned on: ") for line in lines)
    assert lines[-1].startswith("no test message was sent")


def test_nothing_sends_anybody_to_a_second_command_to_use_what_they_ticked(settings):
    """Ticking the box is the yes: no line before the link or after the connection names
    `keryx plugins install`."""
    fresh = said(settings, allowed=EVERYTHING)
    lines = connect(settings, FakeSlack(), token=f"{TOKEN}\n{APP}\n{USER}",
                    allowed=EVERYTHING, test=False)

    assert "keryx plugins" not in fresh
    assert not any("keryx plugins install slack_read" in line
                   or "keryx plugins install slack_message" in line for line in lines)
    assert not hasattr(slack, "WILL_TURN_ON") and not hasattr(slack, "READ_NOTE")


def test_a_ticked_box_whose_voice_tool_is_off_is_told_the_one_command_that_turns_it_on(settings):
    """Turned off by hand: the login changes nothing then, so it says the one command
    that does."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    off = said(settings, allowed=READS)
    plugins.install(settings, "slack_read")
    on_already = said(settings, allowed=READS)

    assert ("slack_read is off, so nothing reads Slack on a call: `keryx plugins install "
            "slack_read` turns it back on.") in off
    assert "slack_message" not in off, "sending is not ticked for this workspace"
    assert "slack_read is off" not in on_already


def test_unticking_turns_off_the_voice_tool_that_went_with_the_box(settings):
    """One left on would be refused at every call, and `keryx tools` would say so for ever."""
    settings = connected_at(settings, EVERYTHING, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    for name in ("slack_read", "slack_message"):
        plugins.install(settings, name)
    lines: list[str] = []

    slack.untick(settings, ConfigStore(), workspace="default", allowed=READS,
                 echo=lines.append)

    assert on(settings, "slack_read") and not on(settings, "slack_message")
    assert "slack_message is off with it, from the next call." in lines
    assert not any(line.startswith("slack_read is off") for line in lines)

    slack.untick(settings, ConfigStore(), workspace="default", allowed=REQUESTS,
                 echo=lines.append)

    assert not on(settings, "slack_read")
    assert "slack_read is off with it, from the next call." in lines


def test_unticking_with_the_tokens_piped_in_turns_the_voice_tool_off_as_well(settings):
    """`--stdin --allow …` reaches `connect` with nothing unticked first: it makes the
    tools match what is ticked too."""
    settings = connected_at(settings, EVERYTHING, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    for name in ("slack_read", "slack_message"):
        plugins.install(settings, name)

    lines = connect(settings, FakeSlack(), token="", allowed=READS, test=False)

    assert on(settings, "slack_read") and not on(settings, "slack_message")
    assert "slack_message is off with it, from the next call." in lines


def test_unticking_one_workspace_leaves_a_voice_tool_another_still_has(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake = FakeSlack(workspace=LAB,
                     user=Workspace(team="Lab", team_id="T0LAB", user_id="U0LAB"))
    connect(settings, fake, token="xoxb-lab-secret\nxapp-lab-secret\nxoxp-lab-secret",
            workspace="lab", allowed=READS, test=False)
    assert on(settings, "slack_read"), "a further workspace with reading ticked turns it on too"

    slack.untick(settings, ConfigStore(), workspace="default", allowed=REQUESTS,
                 echo=lambda line: None)

    assert on(settings, "slack_read") and loads(settings, "slack_read")


def test_disconnecting_the_last_workspace_that_reads_turns_the_voice_tools_off(settings):
    settings = connected_at(settings, EVERYTHING, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    for name in ("slack_read", "slack_message"):
        plugins.install(settings, name)
    lines: list[str] = []

    slack.logout(settings, ConfigStore(), workspace="default", echo=lines.append)

    assert not on(settings, "slack_read") and not on(settings, "slack_message")
    assert "slack_read is off with it, from the next call." in lines


# --- a further workspace ------------------------------------------------------------------


def test_a_further_workspace_has_its_own_app_tokens_owner_and_destination(settings):
    settings = connected_at(settings)
    fake = FakeSlack(workspace=LAB,
                     user=Workspace(team="Lab", team_id="T0LAB", user_id="U0LAB"))

    lines = connect(settings, fake, token="xoxb-lab-secret\nxapp-lab-secret\nxoxp-lab-secret",
                    workspace="lab", allowed=REQUESTS)

    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == TOKEN
    assert workspace_tokens() == {"lab": {"bot": "xoxb-lab-secret", "app": "xapp-lab-secret",
                                          "user": "xoxp-lab-secret"}}
    assert ("To send reports somewhere else: set channel_id under [workspace-lab] in "
            "slack_report.toml.") in lines
    data = saved(settings)
    assert {"lab": data["workspace-lab"]} == {"lab": {
        "channel_id": "U0LAB", "allow": ["requests"], "owner_id": "U0LAB", "team": "Lab",
        "team_id": "T0LAB", "bot_user_id": "U0KERYX",
    }}
    # The first workspace is left exactly as it was.
    assert (data["channel_id"], data["allow"], data["team"]) == ("U0OWNER", [], "Acme")
    assert lines[0] == "connected to the Lab workspace as lab; its tokens are saved"
    assert lines[1].startswith("On: sending you reports and files, acting on what you type")
    assert ("post", "U0LAB", said_to_test(REQUESTS, LYRA)) in fake.calls
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


def test_a_further_workspace_is_unticked_by_its_own_name(settings):
    settings = connected_at(settings)
    connect(settings, FakeSlack(workspace=LAB),
            token="xoxb-lab-secret\nxapp-lab-secret", workspace="lab",
            allowed=REQUESTS, test=False)
    lines: list[str] = []

    assert slack.untick(settings, ConfigStore(), workspace="lab", allowed=BASE,
                        echo=lines.append).done

    # Its user token could only ever say who they are, so it is kept.
    assert workspace_tokens() == {"lab": {"bot": "xoxb-lab-secret", "user": USER}}
    assert saved(settings)["workspace-lab"]["allow"] == []
    assert lines[0].startswith("lab (Lab): acting on what you type to the app is off, from")
    assert lines[1] == "lab's app-level token is forgotten."
    assert saved(settings)["allow"] == []  # the first workspace is not touched


def test_a_further_workspace_wants_the_first_one_there_first(settings):
    fake = FakeSlack()

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, workspace="lab")

    assert "lab is added beside your first workspace, and there is none yet" in str(refused.value)
    assert "run it without --workspace first. Nothing was saved" in str(refused.value)
    assert fake.calls == [] and ConfigStore()._secrets() == {}


def test_the_link_for_a_further_workspace_names_it_on_the_next_command(settings):
    settings = connected_at(settings)

    text = said(settings, workspace="lab", allowed=REQUESTS)

    assert new_app_url(REQUESTS, assistant="Lyra") in text
    assert "keryx auth login slack --stdin --workspace lab --allow requests\n" in text


def test_a_workspaces_name_is_a_short_word():
    assert slack.workspace_name(None) == "default" and slack.workspace_name(" Work ") == "work"
    with pytest.raises(SlackSetupError, match="a short word of your own"):
        slack.workspace_name("my work")


def test_allow_is_said_once_for_each_box_and_none_alone_is_nothing_ticked():
    """Not given at all is None, which is never read as "nothing": what is ticked is
    then asked, or what the workspace has, and never guessed."""
    assert slack.parse_allow(None) is None and slack.parse_allow(()) is None
    assert slack.parse_allow(["none"]) == BASE and slack.parse_allow([" None "]) == BASE
    assert slack.parse_allow([" READ ", "requests"]) == READS
    assert slack.parse_allow(["send", "send"]) == SEND
    with pytest.raises(SlackSetupError, match="goes alone"):
        slack.parse_allow(["none", "read"])
    for old in ("converse", "announce", "write", "everything"):
        with pytest.raises(SlackSetupError) as refused:
            slack.parse_allow([old])
        assert str(refused.value) == (f"there is nothing called '{old}' to allow: one or "
                                      "more of requests, read, send, or none")
    assert not hasattr(slack, "parse_access")


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_every_selection_is_said_back_as_the_options_that_say_it(allowed):
    flags = slack.allow_flags(allowed)

    assert slack.parse_allow(flags.replace("--allow", "").split()) == allowed
    assert (flags == " --allow none") is (allowed == BASE)


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


# --- unticking says what stopped ----------------------------------------------------------


def test_unticking_reading_says_so_and_keryx_still_acts_on_messages(settings):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    assert slack.untick(settings, ConfigStore(), workspace="default", allowed=REQUESTS,
                        echo=lines.append).done

    assert lines[0] == ("Acme: reading Slack as you is off, from this moment. A running "
                        "Keryx stops at once.")
    assert "acting on what you type" not in lines[0]
    assert lines[1] == "SLACK_USER_TOKEN is forgotten."
    assert workspaces(settings)[0].listens and not workspaces(settings)[0].mentions
    assert json.loads(lines[-1]) == app_manifest(REQUESTS, assistant="Lyra")


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
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    lines = logged_out(settings)

    assert ConfigStore()._secrets() == {}
    assert not plugins.is_on(settings, "slack_report")
    kept = plugins.read_config(settings, "slack_report")
    assert (kept["channel_id"], kept["allow"], kept["owner_id"], kept["team_id"]) == (
        "", [], "", ""
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
    connect(settings, FakeSlack(), token=f"{TOKEN}\n{APP}", allowed=REQUESTS, test=False)

    assert saved(settings)["bot_user_id"] == "U0KERYX"
    assert workspaces(settings)[0].missing == ()


def test_with_requests_a_bot_token_slack_names_no_member_for_is_refused(settings):
    fake = FakeSlack(workspace=Workspace(team="Acme", team_id="T0ACME"))

    with pytest.raises(SlackSetupError, match="or which member the app is there"):
        connect(settings, fake, token=f"{TOKEN}\n{APP}", allowed=REQUESTS)

    assert ConfigStore()._secrets() == {}


def test_each_box_says_where_keryx_can_be_set_to_work_from_and_what_it_sees():
    requests, read = (slack.MEANING[each].format(app=LYRA)
                      for each in (Permission.REQUESTS, Permission.READ))
    mention = slack.MENTION_MEANING.format(app=LYRA)
    assert "It is sent those messages and nothing else in Slack" in requests
    assert "every message of yours in its direct messages is a request, with no @Lyra" in requests
    assert "only your own member id is acted on" in requests
    assert "without the phone PIN" in requests
    assert "any conversation it has been added to" in mention
    # What is really delivered, and that the answer is read by the others there.
    assert "every message that mentions it there is sent to Keryx" in mention
    assert "anyone's but yours is dropped" in mention
    assert "answered in that thread, where the others in the conversation read the answer" in (
        mention)
    assert "only when you ask about messages" in read


# --- sending as the owner ---------------------------------------------------------------------


def test_sending_says_plainly_before_the_link_that_keryx_can_then_send_as_you(settings):
    text = said(settings, allowed=EVERYTHING)

    send = slack.MEANING[Permission.SEND]
    assert "Keryx sends messages as you, in your name, to other people and to" in send
    assert "you have said yes" in send
    assert text.index(send) < text.index(new_app_url(EVERYTHING, assistant="Lyra"))
    assert slack.MEANING[Permission.READ] in text
    assert "The user token says which member you are: reports go to you" in text


def test_ticking_sending_changes_the_app_there_is_and_asks_for_no_new_kind_of_token(
    settings,
):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    text = said(settings, allowed=EVERYTHING)

    assert text.startswith("Acme is connected. Adding: sending messages as you.")
    assert "The app you have is changed, not made again:" in text
    assert '"chat:write"' in text.split("The manifest:")[1]
    assert "Every token this needs is saved already" in text
    assert ("keryx auth login slack --stdin --allow requests --allow read --allow send "
            "< /dev/null") in text
    assert "If Slack shows a different User OAuth Token after the reinstall" in text
    assert "api.slack.com/apps?new_app=1" not in text


def test_sending_is_refused_while_the_user_token_may_not_post_as_the_owner_yet(settings):
    """The app was not reinstalled: Slack says what the token may do, and it is not this."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=slack_api.user_scopes(READS)))

    with pytest.raises(SlackSetupError) as refused:
        connect(settings, fake, token="", allowed=EVERYTHING)

    assert str(refused.value) == (
        "the user token cannot be used: it may not yet do what you ticked (it lacks "
        "chat:write, im:write): save the manifest in the app, reinstall it to the "
        "workspace, and hand over the User OAuth Token shown after that. Nothing was saved"
    )
    assert saved(settings)["allow"] == ["requests", "read"]


def test_reading_is_refused_while_the_user_token_can_only_say_who_the_owner_is(settings):
    """The token an app with only requests issued has the one scope that sees nothing:
    ticking reading is not saved until the reinstalled app's token may read."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=("emoji:read",)))

    with pytest.raises(slack.TokenRefused) as refused:
        connect(settings, fake, token="", allowed=READS)

    assert refused.value.kind == "user"
    assert "it may not yet do what you ticked (it lacks channels:read" in str(refused.value)
    assert saved(settings)["allow"] == ["requests"]
    assert not plugins.tool_path(settings, "slack_read").is_file()


@pytest.mark.parametrize(
    "scopes", [None, slack_api.user_scopes(EVERYTHING)]
)
def test_sending_is_saved_once_the_token_may_or_when_slack_does_not_say(settings, scopes):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=scopes))

    connect(settings, fake, token="", allowed=EVERYTHING, test=False)

    assert saved(settings)["allow"] == ["requests", "read", "send"]
    assert saved(settings)["owner_id"] == "U0OWNER"
    assert [each.writes for each in workspaces(settings)] == [True]


def test_unticking_sending_keeps_the_token_and_takes_sending_away(settings):
    settings = connected_at(settings, EVERYTHING, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)

    slack.untick(settings, ConfigStore(), workspace="default", allowed=READS,
                 echo=lambda line: None)

    [one] = workspaces(settings)
    assert (one.allowed, one.reads, one.writes) == (READS, True, False)
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == USER


def test_unticking_sending_says_what_stops_and_never_that_the_user_token_is_gone(
    settings,
):
    settings = connected_at(settings, EVERYTHING, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    lines: list[str] = []

    slack.untick(settings, ConfigStore(), workspace="default", allowed=READS,
                 echo=lines.append)

    said = "\n".join(lines)
    assert lines[0] == ("Acme: sending messages as you is off, from this moment. A running "
                        "Keryx stops at once.")
    assert "reading" not in lines[0] and "forgotten" not in said


def test_what_is_said_at_connect_states_the_direct_message_rule(settings):
    """A direct message to the app needs no mention: the test message, the note about
    listening and the owner line all say so, since they are what the owner reads."""
    assert "with no @Lyra needed" in said_to_test(REQUESTS, LYRA)
    assert "a reply under a report resumes that task" in REQUESTS_TEST_MESSAGE
    assert "every message you type to the Lyra app is a request" in LISTEN_NOTE.format(app=LYRA)
    said = owner_line("U0OWNER", READS, LYRA)
    assert "every one typed to the Lyra app itself" in said
    assert "elsewhere only one that mentions it (@Lyra)" in said
    assert slack.LABELS[Permission.REQUESTS].format(app=LYRA) == (
        "Act on what you type to the Lyra app")


# --- the app carries the assistant's name -------------------------------------------------------


def test_everything_that_says_what_to_type_in_slack_follows_the_assistants_name(settings):
    """What each box means, the steps, the link's manifest, the owner line, the note
    about listening and the test message: an owner whose assistant is Jarvis is told to
    type to the Jarvis app and to mention @Jarvis, and never @Keryx."""
    jarvis = settings.model_copy(update={"assistant_name": "Jarvis"})

    shown = said(jarvis, allowed=READS)

    assert "Keryx acts on what you type to the Jarvis app itself" in said(
        jarvis, allowed=REQUESTS)
    assert "you can also mention the app (@Jarvis)" in shown
    assert [box.label for box in slack.choices("Jarvis")][0] == (
        "Act on what you type to the Jarvis app")
    assert "with yours already filled in and named Jarvis, after your assistant" in shown
    assert new_app_url(READS, assistant="Jarvis") in shown.splitlines()
    assert "@Keryx" not in shown and "Keryx app" not in shown

    fake = FakeSlack()
    lines = connect(jarvis, fake, token=f"{TOKEN}\n{APP}", allowed=REQUESTS)

    assert owner_line("U0OWNER", REQUESTS, "Jarvis") in lines
    assert LISTEN_NOTE.format(app="Jarvis") in lines
    assert ("post", "U0OWNER", said_to_test(REQUESTS, "Jarvis")) in fake.calls
    assert not any("@Keryx" in line or "Keryx app" in line for line in lines)


def test_what_the_app_posts_to_show_it_arrives_does_not_call_itself_keryx():
    """The app is the assistant in Slack: under the name Lyra, "Keryx is connected" would
    read as somebody else's message. It names nothing but what the owner would type."""
    for allowed in SELECTIONS:
        text = said_to_test(allowed, "Lyra")
        assert "Keryx" not in text and text.startswith("Connected. ")
        assert "{" not in text
    assert "@Lyra" not in said_to_test(BASE, "Lyra")
    assert "with no @Lyra needed" in said_to_test(EVERYTHING, "Lyra")


def test_the_steps_that_change_an_app_say_that_saving_the_manifest_renames_it(settings):
    settings = connected_at(settings)
    settings = settings.model_copy(update={"assistant_name": "Jarvis"})

    text = said(settings, allowed=REQUESTS)

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
    assert ("is connected, and has no user token on file: it was connected before Keryx "
            "asked for one, or the one it had was forgotten when reading and sending as you "
            "were switched off. Nothing about it stops working.") in text
    lines = text.splitlines()
    for step in (
        "1. Open https://api.slack.com/apps and pick the app you already have for Keryx. Do "
        "not create a second one.",
        "2. App Manifest: replace what is there with the manifest below, and click Save "
        "Changes.",
        "3. Install App: click Reinstall to Workspace, then Allow. The new permissions "
        "count only after that.",
        "4. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…), the "
        "first of the two there.",
    ):
        assert step in lines
    assert "5. Optional:" in text and "6." not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(BASE, assistant="Lyra")
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
        "this also needs the user token (xoxp-), one to a line. Nothing was saved")
    assert saved(settings) == {"channel_id": "D0OLDDM"} or channel(settings) == "D0OLDDM"


def test_that_login_establishes_the_owner_and_keeps_the_destination_it_had(settings):
    settings = older_install(settings, "D0OLDDM")

    lines = connect(settings, FakeSlack(), token="", test=False)

    assert (saved(settings)["owner_id"], saved(settings)["channel_id"]) == ("U0OWNER", "D0OLDDM")
    assert "it keeps sending to D0OLDDM" in lines and slack.ELSEWHERE in lines
    assert slack.OWNER_NOTED.format(who="U0OWNER") in lines


def test_a_user_token_from_another_workspace_is_refused_whatever_is_ticked(settings):
    fake = FakeSlack(user=Workspace(team="Other", team_id="T0OTHER", user_id="U0ELSE"))

    for allowed in SELECTIONS:
        with pytest.raises(SlackSetupError, match="belongs to another workspace than the bot"):
            connect(settings, fake, token=f"{TOKEN}\n{APP}" if Permission.REQUESTS in allowed
                    else TOKEN, allowed=allowed)
    assert ConfigStore()._secrets() == {}


def test_with_neither_box_ticked_the_user_token_is_asked_who_it_is_and_for_no_scope(settings):
    """One that carries only the least scope there is, or more than was asked for."""
    for scopes in (("emoji:read",), slack_api.user_scopes(READS), None):
        fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                        scopes=scopes))
        connect(settings, fake, token=f"{TOKEN}\n{APP}", allowed=REQUESTS, test=False)
        assert saved(settings)["owner_id"] == "U0OWNER"


def test_unticking_requests_keeps_the_user_token_that_only_says_who_they_are(
    settings,
):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    lines: list[str] = []

    slack.untick(settings, ConfigStore(), workspace="default", allowed=BASE,
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
    lines = said(settings, allowed=REQUESTS).splitlines()

    step = next(line for line in lines if "Display Information" in line)
    assert step.startswith("5. Optional: Basic Information > Display Information: upload")
    assert "Skipping it changes nothing else" in step
    path = lines[lines.index(step) + 1].strip()
    assert path == str(slack.ICON) and Path(path).is_file()
    assert "6." not in "\n".join(lines)


@pytest.mark.parametrize("older", [False, True])
def test_updating_an_app_there_is_offers_the_icon_too(settings, older):
    """Ticking more, and an install from before user tokens: the same optional step,
    after the tokens."""
    text = (said(older_install(settings, "D0OLDDM")) if older
            else said(connected_at(settings), allowed=REQUESTS))

    lines = text.splitlines()
    step = next(line for line in lines if "Display Information" in line)
    assert step.startswith("5. Optional:") and "Skipping it changes nothing else" in step
    assert lines[lines.index(step) + 1].strip() == str(slack.ICON)


def test_a_connected_workspace_is_told_where_the_icon_is(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    lines = said(settings).splitlines()

    told = next(line for line in lines if "Display Information" in line)
    assert told.startswith("Optional:") and lines[lines.index(told) + 1].strip() == str(slack.ICON)


def test_the_wizards_guide_has_the_same_optional_icon_step():
    guide = slack.guide(BASE, app="Lyra")
    raised = slack.guide(REQUESTS, app="Lyra", raising=True, have=("bot",))

    for text, number in ((guide, 4), (raised, 6)):
        assert f"{number}. Optional: **Basic Information > Display Information**: " in text
        assert f"`{slack.ICON}`" in text
        assert f"{number + 1}." not in text


# --- at a terminal the login is one command, which waits --------------------------------------


class Terminal:
    """The hidden prompt, scripted: each answer in turn, and an exception where the owner
    leaves. Running out of answers fails the test, so nothing is asked that was not meant."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        assert self.answers, f"asked {question!r} with nothing left to answer"
        answer = self.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        return answer


def at_a_terminal(settings, fake, terminal, **options) -> list[str]:
    """`keryx auth login slack` typed at a terminal; what it printed."""
    lines: list[str] = []
    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=fake.api(),
                **options)
    return lines


BOT_PROMPT = "Bot token (xoxb-…, OAuth & Permissions > OAuth Tokens > Bot User OAuth Token)"
APP_PROMPT = "App-level token (xapp-…, Basic Information > App-Level Tokens)"
USER_PROMPT = "User token (xoxp-…, OAuth & Permissions > OAuth Tokens > User OAuth Token)"
KEEP = ", or Enter to keep the one saved"
PICK_UP = "nothing was saved: run `keryx auth login slack{flags}` again to pick this up"


def test_at_a_terminal_the_login_prints_the_steps_and_then_asks_for_each_token_itself(
    settings,
):
    """One command: what was ticked, the link, the steps, then each token asked for
    there. No second command and no `--stdin`, and the sentence about standard input is
    not said."""
    fake, terminal = FakeSlack(), Terminal(APP, USER, TOKEN)

    lines = at_a_terminal(settings, fake, terminal, allowed=REQUESTS)

    text = "\n".join(lines)
    assert text.startswith("Always on: Keryx sends you reports and files on Slack")
    assert lines[1] == "You ticked: acting on what you type to the app."
    assert new_app_url(REQUESTS, assistant="Lyra") in lines
    assert "1. Open the link" in text and "5. Optional:" in text
    assert "--stdin" not in text and "standard input" not in text
    waiting = lines.index("Then come back to this terminal, which is waiting, and paste the "
                          "tokens where each is asked for. What you paste is not shown.")
    assert lines[waiting - 1].endswith("reports go to you, and nothing else is asked.")
    assert terminal.asked == [APP_PROMPT, USER_PROMPT, BOT_PROMPT]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    # Then what a login has always said, and the one message that shows it arriving.
    after = lines[waiting + 1:]
    assert after[0] == ""
    assert after[1].startswith("connected to the Acme workspace; SLACK_BOT_TOKEN")
    assert "you are U0OWNER there: reports go to you" in after
    assert owner_line("U0OWNER", REQUESTS, LYRA) in after
    assert after[-1] == "sent a test message: it is in Slack, under Apps"
    assert ("post", "U0OWNER", said_to_test(REQUESTS, LYRA)) in fake.calls
    assert not any(secret in text for secret in (TOKEN, APP, USER))


def test_each_prompt_says_which_token_it_is_and_where_slack_shows_it():
    assert slack.prompt_for("bot", saved=False) == BOT_PROMPT
    assert slack.prompt_for("app", saved=False) == APP_PROMPT
    assert slack.prompt_for("user", saved=True) == USER_PROMPT + KEEP


def test_a_token_on_file_that_still_serves_is_kept_with_enter(settings):
    """Ticking requests: the bot and user tokens are on file, so Enter keeps each and only
    the app-level token is typed."""
    settings = connected_at(settings)
    fake, terminal = FakeSlack(), Terminal(APP, "", "")

    lines = at_a_terminal(settings, fake, terminal, allowed=REQUESTS)

    assert terminal.asked == [APP_PROMPT, USER_PROMPT + KEEP, BOT_PROMPT + KEEP]
    assert "Then come back to this terminal, which is waiting, and paste the app-level " \
           "token where it is asked for. What you paste is not shown." in lines
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                        "SLACK_USER_TOKEN": USER}
    assert saved(settings)["allow"] == ["requests"]


def test_a_token_of_the_wrong_kind_is_said_so_without_quoting_it_and_asked_again(settings):
    fake, terminal = FakeSlack(), Terminal(TOKEN, USER, "xapp-typed-here", TOKEN)

    lines = at_a_terminal(settings, fake, terminal)

    assert terminal.asked == [USER_PROMPT, USER_PROMPT, BOT_PROMPT, BOT_PROMPT]
    assert "That is a bot token, which starts xoxb-: the user token starts xoxp-." in lines
    assert ("That is an app-level token, which starts xapp-: the bot token starts "
            "xoxb-.") in lines
    assert not any(secret in line for line in lines for secret in (USER, TOKEN, "typed-here"))
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_USER_TOKEN": USER}


def test_something_that_is_not_a_token_at_all_is_said_so_and_asked_again(settings):
    fake, terminal = FakeSlack(), Terminal("hunter2-my-password", USER, TOKEN)

    lines = at_a_terminal(settings, fake, terminal)

    assert "That is not a Slack token: the user token starts xoxp-." in lines
    assert not any("hunter2" in line for line in lines)
    assert terminal.asked == [USER_PROMPT, USER_PROMPT, BOT_PROMPT]


def test_a_rotating_bot_token_is_taken_where_the_bot_token_is_asked_for(settings):
    """`xoxe.xoxb-…`: told by `xox`, as a bot token always was, since it is asked by name."""
    fake, terminal = FakeSlack(), Terminal(USER, "xoxe.xoxb-1-rotating")

    at_a_terminal(settings, fake, terminal)

    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == "xoxe.xoxb-1-rotating"


def test_an_empty_answer_with_nothing_on_file_asks_again_and_a_second_ends_it_unsaved(
    settings,
):
    fake, terminal = FakeSlack(), Terminal("", USER, "", "")

    lines: list[str] = []
    with pytest.raises(slack.Stopped) as stopped:
        slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=fake.api())

    assert terminal.asked == [USER_PROMPT, USER_PROMPT, BOT_PROMPT, BOT_PROMPT]
    assert lines.count("A user token is needed for this, and none is saved: paste it, or "
                       "press Enter again to stop.") == 1
    assert "A bot token is needed for this, and none is saved: paste it, or press Enter " \
           "again to stop." in lines
    assert str(stopped.value) == PICK_UP.format(flags="")
    assert fake.calls == [] and ConfigStore().stored() == {}


@pytest.mark.parametrize("leaving", [KeyboardInterrupt, EOFError])
def test_leaving_with_ctrl_c_or_ctrl_d_ends_with_nothing_saved_and_how_to_pick_it_up(
    settings, leaving
):
    fake, terminal = FakeSlack(), Terminal(APP, leaving)

    with pytest.raises(slack.Stopped) as stopped:
        at_a_terminal(settings, fake, terminal, allowed=REQUESTS)

    assert str(stopped.value) == PICK_UP.format(flags=" --allow requests")
    assert TOKEN not in str(stopped.value)
    assert fake.calls == [] and ConfigStore().stored() == {}


def test_a_token_slack_refuses_is_named_and_asked_for_again_with_nothing_saved_meanwhile(
    settings,
):
    """All the tokens are checked together, as ever. The one Slack will not take is
    named, nothing is saved, and only that one is asked for again."""
    fake = FakeSlack()
    real = fake.api()
    seen: list[dict] = []

    def auth_test(token):
        seen.append(dict(ConfigStore()._secrets()))
        if token == "xoxb-revoked":
            return Workspace(error="invalid_auth")
        return real.auth_test(token)

    api = SlackApi(auth_test=auth_test, post_text=real.post_text,
                   open_connection=real.open_connection)
    terminal = Terminal(USER, "xoxb-revoked", TOKEN)
    lines: list[str] = []

    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=api)

    assert terminal.asked == [USER_PROMPT, BOT_PROMPT, BOT_PROMPT]
    refusal = next(line for line in lines if line.startswith("The bot token was not taken"))
    assert "invalid_auth" in refusal and refusal.endswith("Nothing was saved.")
    assert "xoxb-revoked" not in "\n".join(lines)
    assert all(secrets == {} for secrets in seen), "nothing was saved until every one passed"
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_USER_TOKEN": USER}
    assert lines[-1] == "sent a test message: it is in Slack, under Apps"


def test_a_user_token_from_another_workspace_is_named_and_asked_for_again(settings):
    fake = FakeSlack()
    real = fake.api()

    def auth_test(token):
        if token == "xoxp-from-the-lab":
            return Workspace(team="Lab", team_id="T0LAB", user_id="U0LAB")
        return real.auth_test(token)

    api = SlackApi(auth_test=auth_test, post_text=real.post_text,
                   open_connection=real.open_connection)
    terminal = Terminal("xoxp-from-the-lab", TOKEN, USER)
    lines: list[str] = []

    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=api)

    assert terminal.asked == [USER_PROMPT, BOT_PROMPT, USER_PROMPT]
    assert ("The user token was not taken: it belongs to another workspace than the bot "
            "token (Lab). Nothing was saved.") in lines
    assert saved(settings)["owner_id"] == "U0OWNER"


def test_an_app_level_token_slack_refuses_is_named_and_asked_for_again(settings):
    fake = FakeSlack()
    real = fake.api()
    api = SlackApi(
        auth_test=real.auth_test, post_text=real.post_text,
        open_connection=lambda token: (Connection(error="not_allowed_token_type")
                                       if token == "xapp-no-scope"
                                       else real.open_connection(token)),
    )
    terminal = Terminal("xapp-no-scope", USER, TOKEN, APP)
    lines: list[str] = []

    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=api,
                allowed=REQUESTS)

    assert terminal.asked == [APP_PROMPT, USER_PROMPT, BOT_PROMPT, APP_PROMPT]
    refusal = next(line for line in lines if line.startswith("The app-level token was not"))
    assert "it needs the connections:write scope, and Socket Mode on in the app" in refusal
    assert ConfigStore()._secrets()["SLACK_APP_TOKEN"] == APP


def test_a_saved_token_slack_refuses_is_not_offered_again_as_the_one_to_keep(settings):
    """Enter kept the bot token on file, and Slack no longer takes it: asked again, it is
    a token to paste, since keeping the same one would be refused the same way."""
    settings = connected_at(settings, user=False)
    fake = FakeSlack(workspace=Workspace(error="token_revoked"))
    terminal = Terminal(USER, "", "", "")

    with pytest.raises(slack.Stopped):
        at_a_terminal(settings, fake, terminal)

    assert terminal.asked == [USER_PROMPT, BOT_PROMPT + KEEP, BOT_PROMPT, BOT_PROMPT]


def test_slack_refusing_again_and_again_ends_the_login_with_nothing_saved(settings):
    fake = FakeSlack(workspace=Workspace(error="invalid_auth"))
    terminal = Terminal(USER, TOKEN, "xoxb-second-try", "xoxb-third-try")
    lines: list[str] = []

    with pytest.raises(slack.Stopped) as stopped:
        slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=fake.api())

    assert slack.MAX_REFUSALS == 3
    assert len([line for line in lines if line.startswith("The bot token was not taken")]) == 3
    assert terminal.asked == [USER_PROMPT, BOT_PROMPT, BOT_PROMPT, BOT_PROMPT]
    assert str(stopped.value) == PICK_UP.format(flags="")
    assert ConfigStore().stored() == {}


def test_an_answer_that_is_not_the_token_is_asked_for_only_so_many_times(settings):
    fake, terminal = FakeSlack(), Terminal("one", "two", "three")
    lines: list[str] = []

    with pytest.raises(slack.Stopped):
        slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=fake.api())

    assert slack.MAX_WRONG == 3 and len(terminal.asked) == 3
    assert lines.count("That is not a Slack token: the user token starts xoxp-.") == 3
    assert fake.calls == []


def test_slack_not_answering_ends_the_login_rather_than_asking_again(settings):
    """A token is asked for again when Slack said no to it. Slack not being reached is
    nothing another paste would mend."""
    fake = FakeSlack(workspace=Workspace(error="unreachable"))
    terminal = Terminal(USER, TOKEN)

    with pytest.raises(SlackSetupError) as failed:
        at_a_terminal(settings, fake, terminal)

    assert not isinstance(failed.value, slack.Stopped | slack.TokenRefused)
    assert "could not be checked (could not reach Slack)" in str(failed.value)
    assert terminal.asked == [USER_PROMPT, BOT_PROMPT]


def test_a_workspace_that_is_whole_is_told_how_it_stands_and_asked_nothing(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake, terminal = FakeSlack(), Terminal()

    lines = at_a_terminal(settings, fake, terminal)

    assert lines[0].startswith("Acme is connected, and nothing was changed. On: ")
    # The owner is asked nothing; Slack is asked once what the app's bot may do.
    assert terminal.asked == [] and fake.calls == [("auth", TOKEN)]


def test_an_older_install_is_asked_at_the_terminal_for_the_user_token_alone(settings):
    """The update steps, the manifest, then the two prompts: Enter keeps the bot token,
    and reports keep going where they went."""
    settings = older_install(settings, "D0OLDDM")
    fake, terminal = FakeSlack(), Terminal(USER, "")

    lines = at_a_terminal(settings, fake, terminal)

    text = "\n".join(lines)
    assert "pick the app you already have for Keryx" in text and "The manifest:" in text
    assert "Then come back to this terminal, which is waiting, and paste the user token " \
           "where it is asked for. What you paste is not shown." in lines
    assert terminal.asked == [USER_PROMPT, BOT_PROMPT + KEEP]
    assert "it keeps sending to D0OLDDM" in lines
    assert saved(settings)["owner_id"] == "U0OWNER" and channel(settings) == "D0OLDDM"


def test_with_every_token_on_file_ticking_more_waits_for_enter_at_each(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake, terminal = FakeSlack(), Terminal("", "", "")

    lines = at_a_terminal(settings, fake, terminal, allowed=READS, test=False)

    assert ("Every token this needs is saved already. Once the app is reinstalled, come "
            "back to this terminal, which is waiting, and press Enter at each token to "
            "keep it, or paste the User OAuth Token if Slack shows a different one after "
            "the reinstall.") in lines
    assert terminal.asked == [APP_PROMPT + KEEP, USER_PROMPT + KEEP, BOT_PROMPT + KEEP]
    assert saved(settings)["allow"] == ["requests", "read"]


def test_stdin_typed_at_a_terminal_asks_for_the_tokens_without_the_steps(settings):
    fake, terminal = FakeSlack(), Terminal(USER, TOKEN)

    lines = at_a_terminal(settings, fake, terminal, steps=False)

    assert lines[0].startswith("connected to the Acme workspace")
    assert terminal.asked == [USER_PROMPT, BOT_PROMPT]
    # Left half-way, it is the command they typed that picks it up.
    with pytest.raises(slack.Stopped) as stopped:
        at_a_terminal(settings, FakeSlack(), Terminal(EOFError), steps=False,
                      allowed=READS)
    assert str(stopped.value) == PICK_UP.format(flags=" --stdin --allow requests --allow read")


def test_a_further_workspace_with_no_first_one_is_refused_before_anything_is_asked(settings):
    fake, terminal = FakeSlack(), Terminal()

    with pytest.raises(SlackSetupError, match="run it without --workspace first"):
        at_a_terminal(settings, fake, terminal, workspace="work")

    assert terminal.asked == [] and fake.calls == []


def test_the_sentence_about_standard_input_is_said_only_where_nobody_is_at_a_terminal(
    settings,
):
    """`start` alone is the command with no terminal: it prints the steps, names
    `--stdin` and ends. At a terminal the same steps end by sending the owner back to it."""
    piped = said(settings, allowed=REQUESTS)
    waiting = said(settings, allowed=REQUESTS, waiting=True)

    assert ("Then hand the tokens over on standard input, never on the command line, one "
            "to a line in any order:\n  keryx auth login slack --stdin --allow requests"
            ) in piped
    assert "standard input" not in waiting and "--stdin" not in waiting
    assert waiting.endswith("paste the tokens where each is asked for. What you paste is "
                            "not shown.")
    assert piped.split("\n\nThen hand")[0] == waiting.split("\n\nThe user token says")[0]


# --- the boxes, at a terminal ---------------------------------------------------------------


class Boxes:
    """The list of boxes, scripted: what was shown, and the values ticked in answer (or
    an exception, where the owner leaves)."""

    def __init__(self, *ticked):
        self.ticked = ticked
        self.shown: list = []

    def __call__(self, question, choices):
        self.shown.append((question, list(choices)))
        if self.ticked and isinstance(self.ticked[0], type):
            raise self.ticked[0]
        return list(self.ticked)

    @property
    def pre_ticked(self) -> list[str]:
        return [box.value for box in self.shown[-1][1] if box.checked]


def ticking(settings, fake, terminal, boxes, **options) -> list[str]:
    """`keryx auth login slack` at a terminal, with the boxes answered by `boxes`."""
    lines: list[str] = []
    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, api=fake.api(),
                choose=boxes, **options)
    return lines


def test_a_first_login_asks_what_to_tick_before_anything_else_and_makes_exactly_that_app(
    settings,
):
    """One command and no option: what is always on, the three boxes, then the link for
    what was ticked, the steps, and each token that takes."""
    fake, terminal, boxes = FakeSlack(), Terminal(APP, USER, TOKEN), Boxes("requests", "read")

    lines = ticking(settings, fake, terminal, boxes, test=False)

    assert lines[0] == ("Always on: Keryx sends you reports and files on Slack, as the Lyra "
                        "app. That alone reads nothing.\n")
    assert sum(line.startswith("Always on:") for line in lines) == 1  # said once
    [(question, shown)] = boxes.shown
    assert question == "What else may Keryx do in your Slack?"
    assert [box.value for box in shown] == ["requests", "read", "send"]
    assert boxes.pre_ticked == []  # nothing is ticked for anybody
    assert new_app_url(READS, assistant="Lyra") in lines
    assert "You ticked: acting on what you type to the app and reading Slack as you." in lines
    assert terminal.asked == [APP_PROMPT, USER_PROMPT, BOT_PROMPT]
    assert saved(settings)["allow"] == ["requests", "read"]
    assert on(settings, "slack_read") and not on(settings, "slack_message")
    text = "\n".join(lines)
    assert "--allow" not in text and "--stdin" not in text and "level" not in text


def test_nothing_ticked_at_all_is_a_choice_and_makes_the_app_that_only_posts(settings):
    fake, terminal, boxes = FakeSlack(), Terminal(USER, TOKEN), Boxes()

    lines = ticking(settings, fake, terminal, boxes, test=False)

    assert new_app_url(BASE, assistant="Lyra") in lines
    assert "You ticked nothing else, so that is all it does." in lines
    assert saved(settings)["allow"] == [] and terminal.asked == [USER_PROMPT, BOT_PROMPT]


def test_a_connected_workspace_is_shown_its_boxes_ticked_and_unchanged_is_told_so(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake, terminal, boxes = FakeSlack(), Terminal(), Boxes("requests")

    lines = ticking(settings, fake, terminal, boxes)

    assert boxes.pre_ticked == ["requests"]
    assert lines[2].startswith("Acme is connected, and nothing was changed. On: sending you")
    assert terminal.asked == [] and saved(settings)["allow"] == ["requests"]


def test_ticking_one_more_box_shows_the_manifest_for_the_app_there_is_and_turns_its_tool_on(
    settings,
):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake = FakeSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                                    scopes=slack_api.user_scopes(READS)))
    terminal, boxes = Terminal("", "xoxp-after-reinstall", ""), Boxes("requests", "read")

    lines = ticking(settings, fake, terminal, boxes, test=False)

    text = "\n".join(lines)
    assert "Acme is connected. Adding: reading Slack as you." in lines
    assert "api.slack.com/apps?new_app=1" not in text
    manifest = text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")]
    assert json.loads(manifest) == app_manifest(READS, assistant="Lyra")
    assert terminal.asked == [APP_PROMPT + KEEP, USER_PROMPT + KEEP, BOT_PROMPT + KEEP]
    assert saved(settings)["allow"] == ["requests", "read"]
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-after-reinstall"
    assert on(settings, "slack_read") and slack.TURNED_ON["slack_read"] in lines
    assert owner_line("U0OWNER", READS, LYRA) in lines  # a mention now counts elsewhere


def test_unticking_a_box_takes_effect_at_once_and_asks_for_nothing(settings):
    """Whether or not the app in Slack is ever changed: saved, the tool off, the token
    forgotten, and the manifest that takes it away there printed. No token is asked for."""
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    plugins.install(settings, "slack_read")
    fake, terminal, boxes = FakeSlack(), Terminal(), Boxes("requests")

    lines = ticking(settings, fake, terminal, boxes)

    assert boxes.pre_ticked == ["requests", "read"]
    assert lines[2:5] == [
        "Acme: reading Slack as you is off, from this moment. A running Keryx stops at once.",
        "SLACK_USER_TOKEN is forgotten.",
        "slack_read is off with it, from the next call.",
    ]
    assert "The app in Slack still holds the permissions it was made with until you" in lines[5]
    assert json.loads(lines[6]) == app_manifest(REQUESTS, assistant="Lyra")
    assert terminal.asked == [] and fake.calls == []
    assert saved(settings)["allow"] == ["requests"] and not on(settings, "slack_read")
    assert not plugin_reads(settings)


def plugin_reads(settings) -> bool:
    from keryx.plugins.slack import can_read

    return can_read(connected_like(settings))


def test_one_box_swapped_for_another_is_off_at_once_and_on_once_its_tokens_are_in(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake = FakeSlack()
    terminal, boxes = Terminal("xoxp-after-reinstall", ""), Boxes("send")

    lines = ticking(settings, fake, terminal, boxes, test=False)

    assert lines[2] == ("Acme: acting on what you type to the app is off, from this moment. "
                        "A running Keryx stops at once.")
    assert "SLACK_APP_TOKEN is forgotten." in lines
    assert "Acme is connected. Adding: sending messages as you." in lines
    assert slack.SEND_ALONE in "\n".join(lines)
    # No app-level token is asked for any more, and the one forgotten is not offered.
    assert terminal.asked == [USER_PROMPT + KEEP, BOT_PROMPT + KEEP]
    assert saved(settings)["allow"] == ["send"]
    assert ConfigStore()._secrets().keys() == {"SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"}
    assert on(settings, "slack_message")


def test_a_token_forgotten_by_unticking_is_not_offered_as_one_to_keep(settings):
    """Reading off and requests on, in one go: the user token that could read is gone,
    so its prompt has nothing to keep, whatever these settings were loaded with."""
    settings = connected_at(settings, READ, SLACK_USER_TOKEN=USER)
    fake, terminal, boxes = FakeSlack(), Terminal(APP, "xoxp-after-reinstall", ""), Boxes(
        "requests")

    ticking(settings, fake, terminal, boxes, test=False)

    assert terminal.asked == [APP_PROMPT, USER_PROMPT, BOT_PROMPT + KEEP]
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-after-reinstall"
    assert saved(settings)["allow"] == ["requests"]


def test_leaving_after_unticking_says_that_stays_off_and_nothing_else_was_saved(settings):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake, terminal, boxes = FakeSlack(), Terminal(EOFError), Boxes("read")

    with pytest.raises(slack.Stopped) as stopped:
        ticking(settings, fake, terminal, boxes)

    assert str(stopped.value) == (
        "what you unticked stays off, and nothing else was saved: run `keryx auth login "
        "slack` again to pick this up")
    assert saved(settings)["allow"] == []


@pytest.mark.parametrize("leaving", [KeyboardInterrupt, EOFError])
def test_leaving_at_the_boxes_changes_nothing(settings, leaving):
    settings = connected_at(settings, READS, SLACK_APP_TOKEN=APP, SLACK_USER_TOKEN=USER)
    fake, terminal, boxes = FakeSlack(), Terminal(), Boxes(leaving)

    with pytest.raises(slack.Stopped) as stopped:
        ticking(settings, fake, terminal, boxes)

    assert str(stopped.value) == PICK_UP.format(flags="")
    assert saved(settings)["allow"] == ["requests", "read"] and fake.calls == []


def test_what_allow_says_is_what_is_ticked_and_no_box_is_shown(settings):
    fake, terminal, boxes = FakeSlack(), Terminal(USER, TOKEN), Boxes("requests")

    ticking(settings, fake, terminal, boxes, allowed=SEND, test=False)

    assert boxes.shown == [] and saved(settings)["allow"] == ["send"]


def test_stdin_at_a_terminal_replaces_a_token_without_asking_what_is_ticked(settings):
    """Connected: the boxes are as they were, and only the tokens are asked for. Not
    connected: what to tick is still asked, since nothing says it."""
    fresh_boxes = Boxes("requests")
    ticking(settings, FakeSlack(), Terminal(APP, USER, TOKEN), fresh_boxes, steps=False,
            test=False)
    assert len(fresh_boxes.shown) == 1 and saved(settings)["allow"] == ["requests"]

    boxes, terminal = Boxes(), Terminal("", "", "xoxb-the-new-one")
    ticking(connected_like(settings), FakeSlack(), terminal, boxes, steps=False, test=False)

    assert boxes.shown == [] and saved(settings)["allow"] == ["requests"]
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == "xoxb-the-new-one"


# --- the steps follow Slack's console, and the prompts follow the steps ----------------------


def numbered(text: str) -> list[str]:
    """The numbered steps of what was printed, without the icon's file under the last."""
    return [line for line in text.splitlines() if re.match(r"\d\. ", line)]


def test_a_new_apps_steps_visit_each_page_once_in_the_order_the_console_shows_them(settings):
    """The owner walked the real console: the app-level token is under Basic Information,
    and one section of OAuth & Permissions lists the user token and, below it, the bot
    token. So: that page before the install, then the install, then the one section."""
    assert numbered(said(settings, allowed=REQUESTS)) == [
        "1. Open the link: it is Slack's page for a new app, with yours already filled in "
        "and named Lyra, after your assistant. Pick your workspace and click through to "
        "Create.",
        "2. Basic Information > App-Level Tokens > Generate Token and Scopes: name the "
        "token anything, add the connections:write scope, generate it, and copy the token "
        "(xapp-…).",
        "3. Install the app to your workspace and click Allow: the install button is at "
        "the top of OAuth & Permissions, and on Install App too.",
        "4. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…), then "
        "the Bot User OAuth Token (xoxb-…) below it.",
        "5. Optional: Basic Information > Display Information: upload the Keryx mark from "
        "the file below as the app's icon. Skipping it changes nothing else.",
    ]


def test_with_nothing_ticked_there_is_no_app_level_token_and_one_page_of_tokens(settings):
    steps = numbered(said(settings, allowed=BASE))

    assert [step[:40] for step in steps] == [
        "1. Open the link: it is Slack's page for",
        "2. Install the app to your workspace and",
        "3. OAuth & Permissions > OAuth Tokens: c",
        "4. Optional: Basic Information > Display",
    ]
    assert "App-Level Tokens" not in "\n".join(steps)


def test_the_install_step_names_both_places_and_no_one_buttons_label(settings):
    """Slack's own pages put the button at the top of OAuth & Permissions and on Install
    App, and its label carries the workspace's name: the step is right wherever it is."""
    step = numbered(said(settings, allowed=BASE))[1]

    assert "OAuth & Permissions" in step and "Install App" in step and "Allow" in step
    assert "Install to Workspace" not in step


def test_updating_an_app_follows_the_same_grouping(settings):
    """The manifest, the app-level token's page, the reinstall, then the tokens page
    once and only for what is not on file: an install from before user tokens, with
    requests and reading ticked, copies the user token there and is told it is the first
    of the two."""
    steps = numbered(said(older_install(settings, "D0OLDDM"), allowed=READS))

    assert steps == [
        "1. Open https://api.slack.com/apps and pick the app you already have for Keryx. "
        "Do not create a second one.",
        "2. App Manifest: replace what is there with the manifest below, and click Save "
        "Changes.",
        "3. Basic Information > App-Level Tokens > Generate Token and Scopes: name the "
        "token anything, add the connections:write scope, generate it, and copy the token "
        "(xapp-…).",
        "4. Install App: click Reinstall to Workspace, then Allow. The new permissions "
        "count only after that.",
        "5. OAuth & Permissions > OAuth Tokens: copy the User OAuth Token (xoxp-…), the "
        "first of the two there.",
        "6. Optional: Basic Information > Display Information: upload the Keryx mark from "
        "the file below as the app's icon. Skipping it changes nothing else.",
    ]


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_the_prompts_ask_for_the_tokens_in_the_order_the_steps_have_them_copied(
    settings, allowed
):
    """Copy, come back, paste: whoever copies the tokens in the steps' order is asked
    for them in that order, at a terminal as in `token_kinds`."""
    typed = {"xapp-": APP, "xoxp-": USER, "xoxb-": TOKEN}
    asked: list[str] = []

    def paste(question: str) -> str:
        """Whichever token the question is for, so the order is the command's own."""
        prefix = next(prefix for prefix in typed if f"({prefix}…" in question)
        asked.append(prefix)
        return typed[prefix]

    lines: list[str] = []
    slack.login(settings, ConfigStore(), ask=paste, echo=lines.append, api=FakeSlack().api(),
                allowed=allowed, test=False)

    steps = "\n".join(numbered("\n".join(lines)))
    copied = sorted((prefix for prefix in typed if prefix in steps), key=steps.index)
    assert asked == copied
    assert [slack_api.TOKEN_PREFIXES[kind] for kind in slack_api.token_kinds(allowed)] == copied
    assert ("xapp-" in copied) is (Permission.REQUESTS in allowed)
    assert copied[-2:] == ["xoxp-", "xoxb-"], "as their page lists them: the user's first"


def test_every_prompt_names_its_place_by_the_path_its_step_uses():
    steps = "\n".join(slack.plain(step) for step in slack.steps(READS, app="Lyra"))

    assert slack.OAUTH_TOKENS_PLACE == "OAuth & Permissions > OAuth Tokens"
    assert slack.APP_TOKENS_PLACE == (
        "Basic Information > App-Level Tokens > Generate Token and Scopes")
    for kind in ("user", "bot"):
        assert slack.OAUTH_TOKENS_PLACE in slack.prompt_for(kind, saved=False)
        assert slack.OAUTH_TOKEN_LABELS[kind] in slack.prompt_for(kind, saved=False)
        assert slack.OAUTH_TOKEN_LABELS[kind] in steps
    assert slack.OAUTH_TOKENS_PLACE in steps and slack.APP_TOKENS_PLACE in steps
    assert "Basic Information > App-Level Tokens" in slack.prompt_for("app", saved=False)
    assert "·" not in steps


def test_standard_input_is_still_told_apart_by_prefix_in_any_order(settings):
    """`--stdin` never read an order, and still does not."""
    for tokens in (f"{TOKEN}\n{APP}\n{USER}", f"{USER}\n{TOKEN}\n{APP}"):
        ConfigStore().unset(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN"])
        connect(settings, FakeSlack(), token=tokens, allowed=REQUESTS, test=False)
        assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": TOKEN, "SLACK_APP_TOKEN": APP,
                                            "SLACK_USER_TOKEN": USER}


# --- an app made before Keryx could send a file -------------------------------------------------

#: What Slack says of the bot token of an app made from the manifest before files.
BEFORE_FILES = Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX",
                         scopes=("chat:write", "im:history", "reactions:write"))
WITH_FILES = Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX",
                       scopes=("chat:write", "files:write", "im:history", "reactions:write"))
NO_FILES_NOTE = (
    "note: the app in Slack lacks files:write, so it cannot send a file yet; everything "
    "else works as it did. To add it: `keryx auth login slack` prints the manifest to "
    "paste into the app, which is then reinstalled")


@pytest.mark.parametrize(("checked", "lacks"), [
    (BEFORE_FILES, ("files:write",)),
    (WITH_FILES, ()),
    (Workspace(team="Acme", scopes=("files:write",)),
     ("chat:write", "im:history", "reactions:write")),
    # Not said is never taken to mean nothing, and neither is not answered.
    (Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"), ()),
    (Workspace(error="unreachable"), ()),
    (Workspace(error="invalid_auth", scopes=()), ()),
])
def test_what_the_apps_bot_may_not_yet_do_is_read_from_what_slack_says(checked, lacks):
    assert slack.lacking_bot_scopes(checked, REQUESTS) == lacks


def test_logging_in_again_with_nothing_changed_says_the_app_lacks_the_scope_and_how_to_add_it(
    settings,
):
    """The same steps ticking more prints: the app there is, its manifest, a reinstall.
    Never the link that makes a second app, and no token is asked for anew."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake = FakeSlack(workspace=BEFORE_FILES)

    text = said(settings, api=fake.api())

    assert fake.calls == [("auth", TOKEN)]
    assert "api.slack.com/apps?new_app=1" not in text
    assert ("Acme is connected, with an app made before Keryx asked "
            "for files:write, which sending a file takes. Nothing about it stops working: "
            "reports arrive as they did, and a file cannot be sent until the app has it. "
            "The app you have is updated, not made again:") in text
    lines = text.splitlines()
    for step in (
        "1. Open https://api.slack.com/apps and pick the app you already have for Keryx. Do "
        "not create a second one.",
        "2. App Manifest: replace what is there with the manifest below, and click Save "
        "Changes.",
        "3. Install App: click Reinstall to Workspace, then Allow. The new permissions "
        "count only after that.",
    ):
        assert step in lines
    assert "OAuth Tokens:" not in text and "4. Optional:" in text and "5." not in text
    manifest = json.loads(
        text[text.index("The manifest:") + len("The manifest:"):text.index("Saving it")])
    assert manifest == app_manifest(REQUESTS, assistant="Lyra")
    assert "files:write" in manifest["oauth_config"]["scopes"]["bot"]
    assert "The bot token stays as it is." in text
    assert ("Every token this needs is saved already, so there is nothing to hand "
            "over. Once the app is reinstalled:\n  keryx auth login slack --stdin < /dev/null"
            ) in text
    assert TOKEN not in text and "**" not in text and "`" not in text


@pytest.mark.parametrize("workspace", [
    WITH_FILES,
    Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
    Workspace(error="unreachable"),
])
def test_an_app_that_has_the_scope_or_cannot_be_asked_is_told_how_it_stands_as_before(
    settings, workspace
):
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)

    text = said(settings, api=FakeSlack(workspace=workspace).api())

    assert text.startswith("Acme is connected, and nothing was changed. On: ")
    assert "made before" not in text and "The manifest:" not in text


def test_slack_is_not_asked_about_scopes_for_a_workspace_that_is_not_whole_as_it_stands(
    settings,
):
    """A new app, one with more ticked and an older install are each shown a manifest
    anyway."""
    fake = FakeSlack(workspace=BEFORE_FILES)

    said(settings, allowed=BASE, api=fake.api())
    said(connected_at(settings), allowed=REQUESTS, api=fake.api())

    assert fake.calls == []


def test_at_a_terminal_that_login_waits_and_enter_at_each_token_is_all_it_takes(settings):
    """The owner pastes the manifest, reinstalls, comes back and presses Enter: the bot
    token is the same one, and Slack now says it may send a file."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    fake, terminal = FakeSlack(workspace=BEFORE_FILES), Terminal("", "", "")
    api = fake.api()
    asked = api.auth_test

    def auth_test(token):
        answer = asked(token)
        # The first question is before the reinstall; every later one is after it.
        if not token.startswith("xoxp-"):
            fake.workspace = WITH_FILES
        return answer

    lines: list[str] = []
    slack.login(settings, ConfigStore(), ask=terminal, echo=lines.append, test=False,
                api=SlackApi(auth_test=auth_test, post_text=api.post_text,
                             open_connection=api.open_connection))

    text = "\n".join(lines)
    assert "with an app made before Keryx asked for files:write" in text
    assert ("Every token this needs is saved already. Once the app is reinstalled, come "
            "back to this terminal, which is waiting, and press Enter at each token to "
            "keep it, or paste the User OAuth Token if Slack shows a different one after "
            "the reinstall.") in lines
    assert terminal.asked == [APP_PROMPT + KEEP, USER_PROMPT + KEEP, BOT_PROMPT + KEEP]
    assert "connected to the Acme workspace; SLACK_BOT_TOKEN, SLACK_APP_TOKEN and " \
           "SLACK_USER_TOKEN saved" in lines
    assert not any(line.startswith("note: the app in Slack lacks") for line in lines)


def test_an_app_still_without_the_scope_is_saved_and_told_what_it_cannot_do_yet(settings):
    """Text reports must keep working untouched, so a missing file scope refuses nothing:
    the login says so in one line and how to mend it."""
    fake = FakeSlack(workspace=BEFORE_FILES)

    lines = connect(settings, fake, token=f"{TOKEN}\n{APP}\n{USER}", allowed=REQUESTS)

    assert NO_FILES_NOTE in lines
    assert "sent a test message: it is in Slack, under Apps" in lines
    assert saved(settings)["allow"] == ["requests"] and connected(settings)


def test_a_further_workspaces_note_names_it(settings):
    connect(settings, FakeSlack(), test=False)
    lab = Workspace(team="Lab", team_id="T0LAB", user_id="U0KERYX", scopes=("chat:write",))

    lines = connect(settings, FakeSlack(workspace=lab), workspace="lab", test=False)

    assert any("`keryx auth login slack --workspace lab` prints the manifest" in line
               for line in lines)


def test_an_app_with_the_scope_or_one_slack_says_nothing_of_hears_no_note(settings):
    for workspace in (WITH_FILES, Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX")):
        lines = connect(settings, FakeSlack(workspace=workspace), test=False)

        assert not any("lacks" in line for line in lines)


def test_a_scope_that_is_not_the_file_one_is_said_without_talk_of_files(settings):
    """The check is of every bot scope the manifest has, not of one."""
    settings = connected_at(settings, REQUESTS, SLACK_APP_TOKEN=APP)
    deaf = Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX",
                     scopes=("chat:write", "files:write", "reactions:write"))

    text = said(settings, api=FakeSlack(workspace=deaf).api())

    assert ("with an app made before Keryx asked for im:history. What it could do it "
            "still does. The app you have is updated, not made again:") in text
    assert "a file" not in text.split("The manifest:")[0]
    assert slack.lacks_note(("im:history",), "default") == (
        "note: the app in Slack lacks im:history, so not everything you ticked works "
        "yet. To add it: `keryx auth login slack` prints the manifest to paste into the "
        "app, which is then reinstalled")
