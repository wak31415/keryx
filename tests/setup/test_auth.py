"""`keryx auth`'s two halves, called directly: every sign-in's state, and one sign-in."""

import dataclasses

import pytest

from keryx import plugins
from keryx.agents import registry
from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.config.store import ConfigStore
from keryx.integrations.google import SERVICES
from keryx.integrations.slack import new_app_url
from keryx.setup import auth, google
from keryx.setup.auth import AuthError, LoginOptions


@pytest.fixture
def signed_in(monkeypatch, every_agent_installed):
    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS,
        "claude",
        dataclasses.replace(spec, auth=dataclasses.replace(spec.auth, stored_login=lambda: True)),
    )


def test_smoke_runs_one_task_on_each_ready_enabled_agent(settings, signed_in):
    ran = []

    async def smoke(settings, agent):
        ran.append(agent)
        return RunResult(ok=False, error="quota")

    report = auth.status(settings, ConfigStore(), smoke=True, smoke_test=smoke)

    assert ran == ["claude"]
    assert report["claude"]["smoke"] == {"ok": False, "error": "quota"}
    assert report["claude"]["state"] == "failed"
    assert report["codex"] == {"state": "missing", "detail": "not enabled", "enabled": False,
                               "installed": True}


def test_an_agent_that_is_not_installed_says_how(settings, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: False)

    report = auth.status(settings, ConfigStore())

    assert report["codex"]["install_command"] == "uv sync --extra codex"


def login(name, settings, **options):
    lines = []
    auth.login(
        name,
        settings,
        ConfigStore(),
        options=LoginOptions(**options),
        echo=lines.append,
        run_login=lambda argv: lines.append(("ran", list(argv))) or 0,
    )
    return lines


def test_a_headless_claude_login_says_where_the_token_goes(settings, every_agent_installed):
    lines = login("claude", settings, headless=True)

    assert ("ran", ["/venv/bin/claude", "setup-token"]) in lines
    assert any("CLAUDE_CODE_OAUTH_TOKEN --stdin" in str(line) for line in lines)


def test_a_login_that_fails_is_an_auth_error(settings, every_agent_installed):
    with pytest.raises(AuthError, match="exited with 3"):
        auth.login("codex", settings, ConfigStore(), options=LoginOptions(), echo=print,
                   run_login=lambda argv: 3)


def test_an_agent_with_no_cli_says_how_to_install_it(settings, every_agent_installed, monkeypatch):
    spec = BACKENDS["codex"]
    monkeypatch.setitem(BACKENDS, "codex", dataclasses.replace(spec, find_cli=lambda: None))

    with pytest.raises(AuthError, match="CLI is missing"):
        login("codex", settings)


# --- google -------------------------------------------------------------------------------

ALL = tuple(SERVICES)


def result(connected=ALL, missing=(), email="ada@example.com"):
    return google.SignInResult(
        email=email, connected=connected, missing=missing, path=None,
        whose="your own Google client",
    )  # fmt: skip


@pytest.fixture
def client(settings):
    settings.google_oauth_client_id = "cid"
    settings.google_oauth_client_secret = "csecret"
    return settings


@pytest.fixture
def browser(monkeypatch):
    """`browser_signin`, scripted: what it was asked for, and the result it hands back."""
    asked = []

    def signin(settings, chosen, *, announce, account=None, post=None):
        asked.append(chosen)
        announce("https://accounts.google.com/o/oauth2/v2/auth?x", True)
        return result(chosen.services)

    monkeypatch.setattr(google, "browser_signin", signin)
    return asked


def test_google_is_one_command_with_a_browser_on_this_machine(client, browser):
    lines = login("google", client)

    assert [chosen.services for chosen in browser] == [ALL]
    assert "https://accounts.google.com/o/oauth2/v2/auth?x" in lines
    assert any("Your browser is opening" in line for line in lines)
    assert (
        "Signed in as ada@example.com. Connected: Gmail, Calendar, Drive, Docs, Sheets and Slides."
        in lines
    )
    assert ConfigStore().stored() == {
        "USER_GOOGLE_EMAIL": "ada@example.com",
        "GOOGLE_WORKSPACE_MCP": True,
    }
    assert "USER_GOOGLE_EMAIL is now ada@example.com." in lines
    assert any("keryx config set GOOGLE_WORKSPACE_MCP false" in line for line in lines)


@pytest.mark.parametrize("name", ["gmail", "google-workspace"])
def test_the_two_older_names_run_the_same_sign_in(client, browser, name):
    login(name, client)

    assert [chosen.services for chosen in browser] == [ALL]


def test_services_can_be_chosen(client, browser):
    login("google", client, services=("calendar", "docs"))

    assert browser[0].services == ("calendar", "docs")


def test_the_account_the_owner_names_is_the_one_the_sign_in_is_held_to(client, monkeypatch):
    seen = []

    def signin(settings, chosen, *, announce, account=None, post=None):
        seen.append(account)
        return result()

    monkeypatch.setattr(google, "browser_signin", signin)

    login("google", client, account="ada@example.com")

    assert seen == ["ada@example.com"]


def test_a_link_that_did_not_open_is_still_shown(client, monkeypatch):
    def signin(settings, chosen, *, announce, account=None, post=None):
        announce("https://link", False)
        return result()

    monkeypatch.setattr(google, "browser_signin", signin)

    lines = login("google", client)

    assert "Open this link in a browser on this machine:\n" in lines and "https://link" in lines


def test_a_different_saved_address_is_replaced_and_said(client, browser):
    client.user_google_email = "old@example.com"
    client.google_workspace_mcp = True

    lines = login("google", client)

    assert "USER_GOOGLE_EMAIL is now ada@example.com (it was old@example.com)." in lines
    assert ConfigStore().stored() == {"USER_GOOGLE_EMAIL": "ada@example.com"}


def test_signing_in_again_as_the_same_account_saves_nothing(client, browser):
    client.user_google_email = "ada@example.com"
    client.google_workspace_mcp = True

    login("google", client)

    assert ConfigStore().stored() == {}


def test_agents_tools_the_owner_turned_off_stay_off(client, browser):
    """Stored false is a choice; false because nobody has signed in yet is not."""
    ConfigStore().set({"GOOGLE_WORKSPACE_MCP": False})

    lines = login("google", client)

    assert ConfigStore().stored()["GOOGLE_WORKSPACE_MCP"] is False
    assert any("stay off, as you set them" in line for line in lines)


def test_a_client_file_beside_a_configured_pair_says_which_one_is_used(
    client, browser, tmp_path
):
    source = tmp_path / "c.json"
    source.write_text('{"installed": {"client_id": "from-file", "client_secret": "s"}}')

    lines = login("google", client, client_file=source)

    assert browser[0].client.client_id == "cid"  # the pair, as it always has
    assert any("come first, so this sign-in uses them, not that file" in line for line in lines)


def test_own_client_owners_are_told_where_the_newer_apis_are_enabled(client, browser):
    lines = login("google", client)

    [hint] = [line for line in lines if "API is not enabled" in line]
    assert "Drive, Docs, Sheets and Slides" in hint and "google.md" in hint


def test_a_client_file_is_installed_and_used(settings, browser, tmp_path):
    source = tmp_path / "c.json"
    source.write_text('{"web": {"client_id": "i", "client_secret": "s"}}')

    lines = login("google", settings, client_file=source)

    assert browser[0].client.client_id == "i" and not browser[0].client.published
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith(
        "google_client_secret.json")
    assert any("Google client saved" in line for line in lines)


def test_what_the_published_client_does_not_offer_is_said_first(
    settings, browser, tmp_path, monkeypatch
):
    path = tmp_path / "google_client.json"
    path.write_text('{"installed": {"client_id": "p", "client_secret": "s"},'
                    ' "keryx_services": ["calendar"]}')
    monkeypatch.setattr("keryx.integrations.google.PUBLISHED_CLIENT_FILE", path)

    lines = login("google", settings)

    assert "are not offered by Keryx's Google app yet" in lines[0] and "--client-file" in lines[0]
    assert browser[0].services == ("calendar",)


# --- google, with the browser somewhere else ----------------------------------------------


def paste_back(monkeypatch):
    finished = []

    def finish(settings, address, *, account=None, post=None):
        finished.append(address)
        return result(("gmail",), missing=("calendar",))

    monkeypatch.setattr(google, "finish_signin", finish)
    return finished


def test_headless_at_a_terminal_is_still_one_command(client, monkeypatch):
    finished = paste_back(monkeypatch)
    lines, asked = [], []

    def ask(question):
        asked.append(question)
        return "http://localhost:1/?state=s&code=c"

    auth.login("google", client, ConfigStore(), options=LoginOptions(headless=True),
               echo=lines.append, run_login=None, ask=ask)

    assert any(line.startswith("https://accounts.google.com/") for line in lines)
    assert len(asked) == 1 and "Paste its whole address here" in asked[0]
    assert finished == ["http://localhost:1/?state=s&code=c"]
    assert not any("--callback-url" in line for line in lines)
    assert "Signed in as ada@example.com. Connected: Gmail." in lines
    assert any(line.startswith("Not connected: Calendar") for line in lines)


def test_headless_without_a_terminal_is_two_commands(client, monkeypatch):
    finished = paste_back(monkeypatch)

    first = login("google", client, headless=True)

    assert any("keryx auth login google --callback-url - < FILE" in line for line in first)
    assert finished == [] and ConfigStore().stored() == {}

    second = login("google", client, callback_url="http://localhost:1/?state=s&code=c")

    assert finished == ["http://localhost:1/?state=s&code=c"]
    assert "Signed in as ada@example.com. Connected: Gmail." in second
    assert ConfigStore().stored()["GOOGLE_WORKSPACE_MCP"] is True


def test_a_headless_sign_in_never_binds_a_listener(client, monkeypatch):
    monkeypatch.setattr(google, "listen", lambda: pytest.fail("no listener when headless"))

    login("google", client, headless=True)


# --- google, refused ----------------------------------------------------------------------


def test_a_bad_client_file_is_an_auth_error(settings, tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{}")

    with pytest.raises(AuthError, match="not a Google OAuth client file"):
        login("google", settings, client_file=bad)


def test_no_client_at_all_is_an_auth_error_that_says_what_to_do(settings):
    with pytest.raises(AuthError, match="no Google OAuth client.*--client-file"):
        login("google", settings)


def test_a_google_sign_in_that_fails_is_an_auth_error(client, monkeypatch):
    def refuse(settings, chosen, *, announce, account=None, post=None):
        raise google.GoogleSetupError("the browser did not come back")

    monkeypatch.setattr(google, "browser_signin", refuse)

    with pytest.raises(AuthError, match="did not come back"):
        login("google", client)
    assert ConfigStore().stored() == {}


def test_slack_is_missing_until_it_is_on_and_failed_with_a_token_nothing_uses(settings):
    assert auth.status(settings, ConfigStore())["slack"]["state"] == "missing"

    half = auth.status(settings.model_copy(update={"slack_bot_token": "xoxb-1"}), ConfigStore())

    assert half["slack"]["state"] == "failed"
    assert "send_to_slack is off" in half["slack"]["detail"]


def test_slack_that_is_on_is_ok_and_says_where_it_posts(settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-1"})
    plugins.write_config(settings, "send_to_slack", {"channel_id": "U0OWNER"})
    plugins.install(settings, "send_to_slack")

    entry = auth.status(settings, ConfigStore())["slack"]

    assert entry == {
        "state": "ok", "detail": "on — posts to U0OWNER", "optional": True,
        "workspaces": [{"name": "default", "team": "", "label": "your first workspace",
                        "access": "announce", "listens": False, "missing": []}],
    }
    assert auth.slack_lines(entry) == ["your first workspace: announce level, posts only"]


def test_each_slack_workspace_says_its_level_and_what_it_still_lacks(settings):
    settings = settings.model_copy(update={
        "slack_bot_token": "xoxb-1", "slack_app_token": "xapp-1",
        "slack_workspace_tokens": {"work": {"bot": "xoxb-w"}},
    })
    plugins.write_config(settings, "send_to_slack", {
        "channel_id": "U0OWNER", "access": "converse", "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX",
        "workspaces": {"work": {"access": "read", "channel_id": "U0WORK", "owner_id": "U0WORK",
                                "team_id": "T0WORK", "bot_user_id": "U0WORKBOT"}},
    })
    plugins.install(settings, "send_to_slack")

    entry = auth.status(settings, ConfigStore())["slack"]

    # The first thing wrong is what the one line says; every workspace is under it.
    assert entry["state"] == "failed" and entry["optional"] is True
    assert entry["detail"] == (
        "at the read level, but without its app token or its user token — "
        "`keryx auth login slack --workspace work --access read`"
    )
    assert auth.slack_lines(entry) == [
        "Acme: converse level, acts when you mention the app",
        "work: read level, without its app token or its user token",
    ]
    assert "xoxb" not in str(entry) and "xapp" not in str(entry)


def test_slack_that_is_off_lists_no_workspace(settings):
    assert auth.status(settings, ConfigStore())["slack"]["workspaces"] == []
    assert auth.slack_lines({"state": "ok", "detail": ""}) == []


def test_a_slack_login_with_no_token_prints_the_link_that_makes_the_app(settings):
    assert new_app_url() in login("slack", settings)
    assert ConfigStore().stored() == {}


def test_a_slack_login_that_cannot_go_on_is_an_auth_error(settings):
    with pytest.raises(AuthError, match="no token on standard input"):
        login("slack", settings, tokens=" \n")


def test_a_token_is_never_in_what_the_options_print():
    assert "xoxb-secret" not in repr(LoginOptions(tokens="xoxb-secret"))


def test_nothing_else_can_be_signed_in_to(settings):
    with pytest.raises(AuthError, match="one of: claude, codex, google"):
        login("myspace", settings)


# --- signing out --------------------------------------------------------------------------


def logout(settings, out):
    lines = []
    auth.logout("google", settings, echo=lines.append, sign_out=lambda s: out)
    return lines


def test_signing_out_says_what_was_done(settings, tmp_path):
    lines = logout(settings, google.SignOut((tmp_path / "a", tmp_path / "b"), True))

    assert lines[0] == "Signed out of Google: the grant is revoked and 2 files are removed."
    assert "next sign-in" in lines[1] and "USER_GOOGLE_EMAIL" in lines[1]


def test_a_revoke_that_failed_names_where_to_end_the_grant_by_hand(settings, tmp_path):
    [line] = logout(settings, google.SignOut((tmp_path / "a",), False))[:1]

    assert "removed from this machine" in line
    assert "https://myaccount.google.com/permissions" in line


def test_signing_out_when_nobody_is_signed_in_says_so(settings):
    assert logout(settings, google.SignOut((), None)) == [
        "Not signed in to Google: there is nothing to remove."
    ]


def test_only_google_can_be_signed_out_of(settings):
    with pytest.raises(AuthError, match="one of: google"):
        auth.logout("claude", settings, echo=print)


# --- status -------------------------------------------------------------------------------


def test_google_is_missing_until_somebody_signs_in(settings):
    report = auth.status(settings, ConfigStore())

    assert report["google"]["state"] == "missing" and report["google"]["services"] == []
    assert "keryx setup" in report["google"]["detail"]


def test_a_google_sign_in_shows_under_all_three_names(settings):
    from keryx.integrations import google as account

    account.write_token(
        settings,
        account.GoogleToken("rt", "cid", "cs", account.requested_scopes(["calendar"]), "a@b.c"),
    )

    report = auth.status(settings, ConfigStore())

    settings.google_workspace_mcp = True
    report = auth.status(settings, ConfigStore())
    assert report["google"]["state"] == "ok" and "Calendar" in report["google"]["detail"]
    assert report["google"]["services"] == ["calendar"]
    assert report["google-workspace"] == {**report["google"], "alias_of": "google"}
    # Signed in, but not for Gmail: the old name for the email sign-in says so.
    assert report["gmail"]["state"] == "missing"


def test_the_agents_old_name_is_ok_only_when_agents_would_get_the_tools(settings):
    """`google-workspace` was the agents' sign-in: it says whether they have Google now."""
    from keryx.integrations import google as account
    from keryx.integrations.gmail import legacy_token_path

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy_token_path(settings).write_text("{}")
    older = auth.status(settings, ConfigStore())
    assert older["google"]["state"] == "ok" and older["gmail"]["state"] == "ok"
    assert older["google-workspace"]["state"] == "missing"  # email answers only

    account.write_token(
        settings, account.GoogleToken("rt", "cid", "cs", account.requested_scopes(ALL), "a@b.c")
    )
    switched_off = auth.status(settings, ConfigStore())
    assert switched_off["google"]["state"] == "ok"
    assert switched_off["google-workspace"]["state"] == "missing"
    assert "GOOGLE_WORKSPACE_MCP" in switched_off["google-workspace"]["detail"]

    settings.google_workspace_mcp = True
    assert auth.status(settings, ConfigStore())["google-workspace"]["state"] == "ok"
    assert not account.mcp_credentials_dir(settings).exists()  # asking writes nothing


def test_a_sign_in_that_reads_gmail_is_ok_under_the_old_name_too(settings):
    from keryx.integrations import google as account

    account.write_token(
        settings, account.GoogleToken("rt", "cid", "cs", account.requested_scopes(ALL), "a@b.c")
    )

    assert auth.status(settings, ConfigStore())["gmail"]["state"] == "ok"
