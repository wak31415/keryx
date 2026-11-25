"""`keryx auth`'s two halves, called directly: every sign-in's state, and one sign-in."""

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest
from slack_sets import READS

from keryx import plugins
from keryx.agents import registry
from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.config.store import ConfigStore
from keryx.integrations.google import SERVICES
from keryx.integrations.slack import (
    Connection,
    Permission,
    SlackResult,
    Workspace,
    app_manifest,
    new_app_url,
    user_scopes,
)
from keryx.setup import auth, google, slack
from keryx.setup.auth import AuthError, LoginOptions
from keryx.setup.slack import SlackApi

from .fakes import FAKE_ID, FAKE_SECRET


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

    assert browser[0].client.client_id == "i"
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith(
        "google_client_secret.json")
    assert any("Google client saved" in line for line in lines)


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


# --- google, with no client of the owner's yet: one is made now --------------------------

CLIENT_JSON = {"installed": {"client_id": "made", "client_secret": "s"}}


def downloaded(name="client_secret_1.json", body=CLIENT_JSON,
               age=0.0):
    """A file Google's console downloaded into `~/Downloads`, `age` seconds ago."""
    path = Path.home() / "Downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    os.utime(path, (time.time() - age, time.time() - age))
    return path


@pytest.fixture
def page(monkeypatch):
    """The browser, for Google's Personal mode page: what it was asked to open."""
    opened = []
    monkeypatch.setattr(google, "open_browser", lambda url: opened.append(url) or True)
    return opened


def at_a_terminal(settings, answers, *, headless=False, secrets=()):
    """A Google login at a terminal; each question answered from `answers` in turn, Enter
    (None) taking its default. What was asked, and with what default, is kept."""
    lines, asked, hidden = [], [], list(secrets or ())
    answers = list(answers)

    def ask(question, default=None):
        asked.append((question, default))
        answer = answers.pop(0)
        return default if answer is None else answer

    def ask_secret(question):
        asked.append((question, "hidden"))
        answer = hidden.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    auth.login("google", settings, ConfigStore(), options=LoginOptions(headless=headless),
               echo=lines.append, run_login=None, ask=ask,
               ask_secret=None if secrets is None else ask_secret)
    return lines, asked


def saved_client(settings):
    return json.loads((settings.config_dir / "google_client_secret.json").read_text())


def test_with_no_client_the_login_makes_one_and_goes_on_to_sign_in(settings, browser, page):
    downloaded("client_secret_old.json", {"installed": {"client_id": "old"}}, age=600)
    newest = downloaded()

    lines, asked = at_a_terminal(settings, [None])

    text = "\n".join(lines)
    # Why, first: Google's terms, and that Personal mode is quick and only theirs.
    assert "terms" in lines[0] and "open-source" in lines[0] and "Personal mode" in lines[0]
    assert page == [google.PERSONAL_MODE_URL] and google.PERSONAL_MODE_URL in lines
    assert "Your browser is opening Google's page for it" in text
    assert "3. Choose Download JSON if it is offered, or copy the Client ID" in text
    assert google.ELSEWHERE_NOTE not in lines  # the browser is on this machine
    assert asked == [("\nThe file you downloaded, or the Client ID", str(newest))]
    assert saved_client(settings) == CLIENT_JSON
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith(
        "google_client_secret.json")
    assert "Select all" in text and "**" not in text  # the consent steps, as plain text
    assert [chosen.client.client_id for chosen in browser] == ["made"]
    assert "Signed in as ada@example.com. Connected: " in text


def test_nothing_downloaded_offers_the_downloads_folder_and_a_folder_takes_its_newest(
    settings, browser, page, tmp_path
):
    (Path.home() / "Downloads").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "client_secret_2.json").write_text(json.dumps(CLIENT_JSON))

    lines, asked = at_a_terminal(settings, [None, str(elsewhere)])

    downloads = str(Path.home() / "Downloads") + os.sep
    assert [default for _, default in asked] == [downloads, downloads]
    assert f"There is no client_secret_*.json file in {downloads.rstrip(os.sep)}." in lines
    assert f"Took {elsewhere / 'client_secret_2.json'}, the newest one there." in lines
    assert saved_client(settings) == CLIENT_JSON


def test_a_downloads_folder_that_is_not_there_is_said(settings, browser, page, tmp_path):
    elsewhere = tmp_path / "client_secret_3.json"
    elsewhere.write_text(json.dumps(CLIENT_JSON))

    lines, _ = at_a_terminal(settings, [None, str(elsewhere)])

    assert f"There is no folder at {Path.home() / 'Downloads'}." in lines


def test_three_wrong_answers_stop_and_save_nothing(settings, page, tmp_path):
    web = downloaded("client_secret_web.json", {"web": {"client_id": "w", "client_secret": "s"}})
    not_json = downloaded("client_secret_bad.json")
    not_json.write_text("nope")

    with pytest.raises(auth.AuthStopped, match="run `keryx auth login google` again") as raised:
        at_a_terminal(settings, [str(web), str(not_json), str(tmp_path / "gone.json")])

    assert "--client-file" in str(raised.value)
    assert not (settings.config_dir / "google_client_secret.json").exists()
    assert ConfigStore().stored() == {}


def test_a_wrong_answer_is_said_in_a_sentence_and_asked_again(settings, browser, page):
    web = downloaded("client_secret_web.json", {"web": {"client_id": "w", "client_secret": "s"}})

    lines, asked = at_a_terminal(settings, [str(web), str(downloaded())])

    [refused] = [line for line in lines if "Desktop app client" in line]
    assert refused.startswith(str(web)) and refused.endswith(".")
    assert len(asked) == 2 and saved_client(settings) == CLIENT_JSON


def test_with_the_browser_elsewhere_the_file_can_be_pasted_hidden(settings, page, monkeypatch):
    finished = paste_back(monkeypatch)

    lines, asked = at_a_terminal(
        settings, ["paste", "http://localhost:1/?state=s&code=c"], headless=True,
        secrets=[json.dumps(CLIENT_JSON)],
    )

    assert page == []  # nothing to open here
    assert "Open the link below in a browser, on any device." in lines
    assert google.ELSEWHERE_NOTE in lines
    assert asked[1] == ("Paste what is in the file, on one line (hidden)", "hidden")
    assert saved_client(settings) == CLIENT_JSON and finished


def test_leaving_the_pasted_file_stops_and_saves_nothing(settings, page):
    with pytest.raises(auth.AuthStopped, match="no Google client was saved"):
        at_a_terminal(settings, ["paste"], headless=True, secrets=[EOFError()])

    assert not (settings.config_dir / "google_client_secret.json").exists()


def test_a_page_that_does_not_open_says_to_open_the_link(settings, browser, monkeypatch):
    monkeypatch.setattr(google, "open_browser", lambda url: False)

    lines, _ = at_a_terminal(settings, [str(downloaded())])

    assert "Open the link below in a browser, on any device." in lines


def test_other_shows_the_way_for_an_account_that_cannot_create_a_project(
    settings, browser, page
):
    lines, asked = at_a_terminal(settings, ["other", str(downloaded())])

    assert google.NO_PROJECT_NOTE in lines  # said under the Personal mode steps
    text = "\n".join(lines)
    assert google.OTHER_INTRO in text
    assert "2. With that project selected" in text
    assert f"   {google.ENABLE_APIS_URL}" in text
    assert "4. Publish it: under Audience, choose Publish app" in text
    assert len(asked) == 2 and asked[0] == asked[1]  # the same question, asked again
    assert saved_client(settings) == CLIENT_JSON


def test_other_is_never_a_wrong_answer(settings, browser, page, tmp_path):
    gone = str(tmp_path / "gone.json")

    at_a_terminal(settings, ["other", gone, "other", gone, "other", str(downloaded())])

    assert saved_client(settings) == CLIENT_JSON


def test_the_wait_says_what_an_organization_that_blocks_the_app_means(client, browser):
    lines = login("google", client)

    assert google.BLOCKED_NOTE in lines


def test_the_paste_back_says_it_too(client, monkeypatch):
    lines = login("google", client, headless=True)

    assert google.BLOCKED_NOTE in "\n".join(lines)


def test_with_nothing_to_paste_into_a_path_is_asked_again(settings, browser, page):
    lines, _ = at_a_terminal(settings, ["paste", str(downloaded())], secrets=None)

    assert "There is nowhere to type that hidden here." in lines


def test_what_is_asked_for_again_unasked_is_said_before_the_link(client, browser):
    from keryx.integrations import google as account

    account.write_token(
        client, account.GoogleToken("rt", "cid", "cs", account.requested_scopes(["gmail"]),
                                    "ada@example.com"),
    )

    lines = login("google", client, services=("calendar",))

    assert lines[0].startswith("Gmail is connected now and is asked for again")
    assert browser[0].services == ("gmail", "calendar")


def test_a_client_id_and_its_secret_are_kept_as_a_file_would_be(settings, browser, page):
    lines, asked = at_a_terminal(
        settings, [FAKE_ID],
        secrets=[FAKE_SECRET])

    assert asked[1] == ("Client Secret (hidden)", "hidden")
    saved = saved_client(settings)["installed"]
    assert (saved["client_id"], saved["client_secret"]) == (
        FAKE_ID, FAKE_SECRET)
    assert saved["redirect_uris"] == ["http://localhost"]
    assert [chosen.client.client_id for chosen in browser] == [
        FAKE_ID]
    assert FAKE_SECRET not in "\n".join(lines)


def test_a_client_id_with_no_secret_is_asked_again(settings, browser, page):
    lines, _ = at_a_terminal(
        settings, [FAKE_ID, str(downloaded())], secrets=[""])

    assert "No Client Secret was given." in lines and saved_client(settings) == CLIENT_JSON


def test_a_secret_typed_where_it_shows_is_never_said_back(settings, browser, page):
    secret = "GOCSPX-typed-where-it-shows"
    typed = '{"installed": {"client_secret": "' + secret + '"'

    lines, _ = at_a_terminal(settings, [secret, typed, str(downloaded())])

    text = "\n".join(lines)
    assert secret not in text
    assert "That looks like the Client Secret: give the Client ID here" in text
    assert ("What was typed is not a whole client file: paste it on one line, as Google's "
            "download is, or give the file's path.") in text


def test_the_first_line_of_a_file_pasted_over_several_asks_for_one_line(settings, browser, page):
    lines, _ = at_a_terminal(settings, ["paste", str(downloaded())], secrets=["{"])

    assert ("What was pasted is not a whole client file: paste it on one line, as Google's "
            "download is, or give the file's path.") in lines


def test_whole_json_typed_at_the_question_is_taken(settings, browser, page):
    at_a_terminal(settings, [json.dumps(CLIENT_JSON)])

    assert saved_client(settings) == CLIENT_JSON


@pytest.mark.parametrize("answer", ["~nosuchuser-keryx/x.json", "x" * 5000])
def test_a_path_that_cannot_be_looked_at_is_a_wrong_answer(settings, browser, page, answer):
    lines, _ = at_a_terminal(settings, [answer, str(downloaded())])

    assert any(line.startswith("Could not look at ") for line in lines)
    assert saved_client(settings) == CLIENT_JSON


def test_a_folder_without_permission_is_a_wrong_answer(
    settings, browser, page, tmp_path, monkeypatch
):
    real = Path.is_dir

    def refuse(self):
        if self.name == "locked":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "is_dir", refuse)

    lines, _ = at_a_terminal(settings, [str(tmp_path / "locked"), str(downloaded())])

    assert f"Could not look at {tmp_path / 'locked'}: Permission denied." in lines


def test_a_relative_path_is_said_back_from_the_root(settings, browser, page):
    lines, _ = at_a_terminal(settings, ["nowhere.json", str(downloaded())])

    assert f"There is no file at {Path.cwd() / 'nowhere.json'}." in lines


def test_the_newest_desktop_client_is_suggested_before_a_newer_web_one(settings, browser, page):
    desktop = downloaded("client_secret_desktop.json", age=60)
    downloaded("client_secret_web.json", {"web": {"client_id": "w", "client_secret": "s"}})

    _, asked = at_a_terminal(settings, [None])

    assert asked[0][1] == str(desktop)


@pytest.mark.parametrize("leave", [EOFError(), KeyboardInterrupt()])
def test_leaving_the_path_question_says_what_was_not_saved(settings, page, leave):
    def ask(question, default=None):
        raise leave

    with pytest.raises(auth.AuthStopped, match="no Google client was saved"):
        auth.login("google", settings, ConfigStore(), options=LoginOptions(),
                   echo=lambda line: None, run_login=None, ask=ask)


def test_skip_at_the_path_question_stops_and_saves_nothing(settings, page):
    with pytest.raises(auth.AuthStopped, match="no Google client was saved"):
        at_a_terminal(settings, ["skip"])

    assert ConfigStore().stored() == {}


def test_leaving_the_pasted_back_address_keeps_the_client_on_file(client):
    def ask(question, default=None):
        raise EOFError

    with pytest.raises(auth.AuthStopped) as raised:
        auth.login("google", client, ConfigStore(), options=LoginOptions(headless=True),
                   echo=lambda line: None, run_login=None, ask=ask)

    assert str(raised.value) == (
        "the sign-in was not saved, and the Google client on file is kept: run `keryx auth "
        "login google --headless` again")
    assert not (client.data_dir / "google_signin.json").exists()


def test_leaving_the_pasted_back_address_keeps_no_client_given_in_this_run(settings, page):
    asked = iter([str(downloaded())])

    def ask(question, default=None):
        if "Paste its whole address" in question:
            raise EOFError
        return next(asked)

    with pytest.raises(auth.AuthStopped, match="the Google client was not kept either"):
        auth.login("google", settings, ConfigStore(), options=LoginOptions(headless=True),
                   echo=lambda line: None, run_login=None, ask=ask)

    assert not (settings.config_dir / "google_client_secret.json").exists()
    assert not google.staged_client_path(settings).exists()
    assert ConfigStore().stored() == {}


# --- a client given here is kept only once a sign-in with it works ------------------------

ID, SECRET = FAKE_ID, FAKE_SECRET


def refusing_the_client(monkeypatch, error="invalid_client"):
    """`browser_signin` running the exchange against a token endpoint that refuses the
    client, the way a wrong secret is refused."""

    def signin(settings, chosen, *, announce, account=None, post=None):
        announce("https://accounts.google.com/o/oauth2/v2/auth?x", True)
        pending = google.Pending("s", "v", "http://127.0.0.1:1", chosen.client.client_id,
                                 chosen.services)

        class Refused:
            status_code = 401

            def json(self):
                return {"error": error}

        return google.exchange(settings, pending, {"state": "s", "code": "c"},
                               post=lambda *a, **k: Refused())

    monkeypatch.setattr(google, "browser_signin", signin)


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
def test_a_client_google_refuses_is_not_kept_and_the_next_login_asks_again(
    settings, page, monkeypatch, error
):
    refusing_the_client(monkeypatch, error)

    with pytest.raises(AuthError, match=f"Google refused the client \\({error}\\).*asks for it"):
        at_a_terminal(settings, [ID], secrets=[SECRET])

    assert not (settings.config_dir / "google_client_secret.json").exists()
    assert not google.staged_client_path(settings).exists()
    assert ConfigStore().stored() == {}
    # So the next login asks for a client again.
    with pytest.raises(auth.AuthStopped, match="no Google client was saved"):
        at_a_terminal(settings, ["skip"])


def test_a_client_is_saved_only_after_the_sign_in_with_it_is_kept(settings, page, monkeypatch):
    seen = []

    def signin(settings_, chosen, *, announce, account=None, post=None):
        seen.append((chosen.client.client_id,
                     (settings.config_dir / "google_client_secret.json").exists()))
        return result(chosen.services)

    monkeypatch.setattr(google, "browser_signin", signin)

    lines, _ = at_a_terminal(settings, [ID], secrets=[SECRET])

    assert seen == [(ID, False)]  # signed in with it before it was on file
    assert google.KEPT_ONCE_IT_WORKS in lines
    assert saved_client(settings)["installed"]["client_id"] == ID
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith(
        "google_client_secret.json")
    assert not google.staged_client_path(settings).exists()


def test_a_client_file_given_with_a_browser_is_kept_only_once_it_works(
    settings, monkeypatch, tmp_path
):
    refusing_the_client(monkeypatch)
    source = tmp_path / "c.json"
    source.write_text(json.dumps(CLIENT_JSON))

    with pytest.raises(AuthError, match="It was not saved"):
        login("google", settings, client_file=source)

    assert not (settings.config_dir / "google_client_secret.json").exists()


def test_a_client_on_file_google_refuses_is_kept_and_the_command_that_replaces_it_named(
    settings, monkeypatch
):
    path = google.install_client_text(settings, json.dumps(CLIENT_JSON))
    settings = settings.model_copy(update={"google_client_secrets_file": path})
    refusing_the_client(monkeypatch)

    with pytest.raises(AuthError) as raised:
        login("google", settings)

    assert "It is the client on file: `keryx auth login google --client-file FILE` replaces" in (
        str(raised.value))
    assert (settings.config_dir / "google_client_secret.json").exists()


def test_a_pair_google_refuses_names_how_to_unset_it(client, monkeypatch):
    refusing_the_client(monkeypatch)

    with pytest.raises(AuthError, match="keryx config unset GOOGLE_OAUTH_CLIENT_ID"):
        login("google", client)


def test_with_no_terminal_a_client_file_is_put_on_file_and_forgotten_if_refused(
    settings, tmp_path
):
    """The two commands of a pasted-back sign-in: the second reads the client from file,
    so the first puts it there; a refusal at the second takes it away again."""
    source = tmp_path / "c.json"
    source.write_text(json.dumps(CLIENT_JSON))

    first = login("google", settings, headless=True, client_file=source)

    assert any("Google client saved to" in line for line in first)
    saved = json.loads((settings.data_dir / "google_signin.json").read_text())
    assert saved["installed_client"] is True

    on_file = Path(ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"])
    second_settings = settings.model_copy(update={"google_client_secrets_file": on_file})

    class Refused:
        status_code = 401

        def json(self):
            return {"error": "invalid_client"}

    with pytest.raises(AuthError, match="so it was forgotten: give the right one"):
        auth.login("google", second_settings, ConfigStore(),
                   options=LoginOptions(callback_url=f"http://localhost:1/?state={saved['state']}"
                                                     "&code=c"),
                   echo=lambda line: None, run_login=None, post=lambda *a, **k: Refused())

    assert not (settings.config_dir / "google_client_secret.json").exists()


def test_the_wait_says_what_a_wrong_client_id_looks_like(settings, browser, page):
    lines, _ = at_a_terminal(settings, [ID], secrets=[SECRET])

    assert google.client_not_found_note(given=True) in lines
    assert "press Ctrl-C, and `keryx auth login google` asks for the client again" in (
        google.client_not_found_note(given=True))


def test_with_a_client_on_file_the_wait_says_how_to_replace_it(client, browser):
    lines = login("google", client)

    assert google.client_not_found_note(given=False) in lines


@pytest.mark.parametrize(("secret", "said"), [
    (ID, "that is a Client ID, not its secret"),
    ("GOCSPX-abc def ghijklmnopqrstuvwxyz", "a Client Secret has no spaces in it"),
    ("GOCSPX-short", "that is too short to be a Client Secret"),
])
def test_a_secret_of_the_wrong_shape_is_refused_without_being_said_back(
    settings, browser, page, secret, said
):
    lines, _ = at_a_terminal(settings, [ID, str(downloaded())], secrets=[secret])

    text = "\n".join(lines)
    assert said[0].upper() + said[1:] in text
    assert secret == ID or secret not in text


def test_an_older_secret_with_no_prefix_is_taken(settings, browser, page):
    at_a_terminal(settings, [ID], secrets=["abcdefghijklmnopqrstuvwx"])

    assert saved_client(settings)["installed"]["client_secret"] == "abcdefghijklmnopqrstuvwx"


def test_a_client_id_pasted_twice_is_said_to_be_one_and_not_quoted(settings, browser, page):
    lines, _ = at_a_terminal(settings, [ID + ID, str(downloaded())])

    assert ("That is not one Client ID: copy it again, once. It looks like "
            "1234-abc.apps.googleusercontent.com.") in lines


@pytest.mark.parametrize("answer", [
    '"client_secret": "' + "GOCSPX" + '-abcdefghijkl",', "abcdefghijklmnopqrstuvwx",
    '"redirect_uris": ["http://localhost"]',
])
def test_an_answer_that_is_not_a_path_is_never_said_back(settings, browser, page, answer):
    lines, _ = at_a_terminal(settings, [answer, str(downloaded())])

    assert "There is no file by that name." in lines
    assert not any(answer in line for line in lines)


def test_with_a_client_on_file_nothing_is_asked_about_one(client, browser, page):
    lines, asked = at_a_terminal(client, [])

    assert page == [] and asked == [] and not any("terms" in line for line in lines)


# --- google, refused ----------------------------------------------------------------------


def test_a_bad_client_file_is_an_auth_error(settings, tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{}")

    with pytest.raises(AuthError, match="not a Google OAuth client file"):
        login("google", settings, client_file=bad)


def test_no_client_and_no_terminal_is_an_auth_error_that_names_the_page(settings, monkeypatch):
    """Without a terminal there is nobody to ask for the file: the refusal names Google's
    Personal mode page and `--client-file`, and opens nothing."""
    monkeypatch.setattr(google, "open_browser", lambda url: pytest.fail("nothing opened"))

    with pytest.raises(auth.AuthStopped) as raised:  # nothing was tried, nothing failed
        login("google", settings)

    said = str(raised.value)
    assert said.startswith(f"no Google client of yours yet. Make one at {google.PERSONAL_MODE_URL}")
    assert "--client-file FILE" in said and "failed" not in said
    # The guide's path on a line of its own, so no full stop is glued to it.
    assert said.endswith(
        f"For an account that cannot create projects, the steps are in\n  {google.guide_path()}")


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
    assert "slack_report is off" in half["slack"]["detail"]


def test_slack_that_is_on_is_ok_and_says_where_it_posts(settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-1"})
    plugins.write_config(settings, "slack_report", {"channel_id": "U0OWNER"})
    plugins.install(settings, "slack_report")

    entry = auth.status(settings, ConfigStore())["slack"]

    assert entry == {
        "state": "ok", "detail": "on — posts to U0OWNER", "optional": True,
        "workspaces": [{"name": "default", "team": "", "label": "your first workspace",
                        "allow": [], "said": NOTHING_TICKED, "listens": False, "missing": [],
                        "owner": ""}],
    }
    # Connected before Keryx asked for a user token: it works, and says what it has not.
    assert auth.slack_lines(entry) == [
        f"your first workspace: {NOTHING_TICKED}, no owner on file yet (the next "
        "`keryx auth login slack` adds one)"]


#: What `auth status` and `doctor` say of a workspace with no box ticked.
NOTHING_TICKED = ("on: sending you reports and files; off: acting on what you type to the "
                  "app, reading Slack as you, sending messages as you")


def test_each_slack_workspace_says_what_is_on_and_off_and_what_it_still_lacks(settings):
    settings = settings.model_copy(update={
        "slack_bot_token": "xoxb-1", "slack_app_token": "xapp-1",
        "slack_workspace_tokens": {"work": {"bot": "xoxb-w"}},
    })
    plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "allow": ["requests"], "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX",
        "workspaces": {"work": {"allow": ["requests", "read"], "channel_id": "U0WORK",
                                "owner_id": "U0WORK",
                                "team_id": "T0WORK", "bot_user_id": "U0WORKBOT"}},
    })
    plugins.install(settings, "slack_report")

    entry = auth.status(settings, ConfigStore())["slack"]

    # The first thing wrong is what the one line says; every workspace is under it.
    assert entry["state"] == "failed" and entry["optional"] is True
    assert entry["detail"] == (
        "on: sending you reports and files, acting on what you type to the app, reading "
        "Slack as you; off: sending messages as you; but without its app token or its "
        "user token — `keryx auth login slack --workspace work`"
    )
    assert [each["allow"] for each in entry["workspaces"]] == [["requests"],
                                                               ["requests", "read"]]
    assert auth.slack_lines(entry) == [
        "Acme: on: sending you reports and files, acting on what you type to the app; off: "
        "reading Slack as you, sending messages as you, you are U0OWNER",
        "work: on: sending you reports and files, acting on what you type to the app, "
        "reading Slack as you; off: sending messages as you, without its app token or its "
        "user token, you are U0WORK",
    ]
    assert "xoxb" not in str(entry) and "xapp" not in str(entry)
    assert not any(word in "".join(auth.slack_lines(entry))
                   for word in ("level", "converse", "announce"))


def test_slack_that_is_off_lists_no_workspace(settings):
    assert auth.status(settings, ConfigStore())["slack"]["workspaces"] == []
    assert auth.slack_lines({"state": "ok", "detail": ""}) == []


def test_a_slack_login_that_says_what_is_ticked_prints_the_link_that_makes_that_app(settings):
    assert new_app_url(assistant="Lyra") in login("slack", settings, allow=("none",))
    lines = login("slack", settings, allow=("requests", "read"))
    assert new_app_url(READS, assistant="Lyra") in lines
    assert any("keryx auth login slack --stdin --allow requests --allow read" in line
               for line in lines)
    assert ConfigStore().stored() == {}


def test_with_no_terminal_and_nothing_said_a_new_slack_login_prints_the_choices_and_guesses_none(
    settings,
):
    """No link, because no app was chosen: every choice in full, and the command that
    says one. Nothing is saved, and nothing was picked on anybody's behalf."""
    lines = login("slack", settings)

    text = "\n".join(lines)
    assert "api.slack.com/apps" not in text
    assert lines[0].startswith("Always on: Keryx sends you reports and files on Slack")
    assert "  keryx auth login slack --allow none" in lines
    assert "  keryx auth login slack --allow requests --allow read" in lines
    assert lines[-1].endswith("Nothing was chosen for you, and nothing was saved.")
    assert ConfigStore().stored() == {}
    assert not plugins.config_path(settings, "slack_report").exists()


def test_tokens_piped_to_a_new_slack_login_that_does_not_say_what_is_ticked_are_refused(settings):
    with pytest.raises(AuthError) as refused:
        login("slack", settings, tokens="xoxb-1\nxoxp-1\n")

    assert str(refused.value) == (
        "nothing says what Keryx may do there, and it is not guessed: add --allow none for "
        "reports and files alone, or --allow once for each of requests, read, send (`keryx "
        "auth login slack` says what each is). Nothing was saved")
    assert ConfigStore().stored() == {}


def test_a_slack_login_that_cannot_go_on_is_an_auth_error(settings):
    with pytest.raises(AuthError, match="no token on standard input"):
        login("slack", settings, tokens=" \n", allow=("none",))
    with pytest.raises(AuthError, match="--allow takes requests, read or send") as refused:
        login("slack", settings, allow=("converse",))
    assert "converse" not in str(refused.value)  # what was given is never said back


def connected_slack(settings, *allow):
    """`settings` with a first workspace connected and `allow` ticked."""
    secrets = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1",
               "SLACK_USER_TOKEN": "xoxp-1"}
    ConfigStore().set(secrets)
    settings = settings.model_copy(update={key.lower(): value for key, value in secrets.items()})
    plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "owner_id": "U0OWNER", "allow": list(allow), "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX"})
    plugins.install(settings, "slack_report")
    return settings


def test_unticking_with_no_terminal_is_done_at_once_and_asks_slack_nothing(settings):
    """`--allow` says everything that stays on: what it leaves out is off from this
    moment, with no token and nothing asked of Slack."""
    settings = connected_slack(settings, "requests", "read")
    plugins.install(settings, "slack_read")

    lines = login("slack", settings, allow=("requests",))

    assert lines[0] == ("Acme: reading Slack as you is off, from this moment. A running "
                        "Keryx stops at once.")
    assert "SLACK_USER_TOKEN is forgotten." in lines
    assert plugins.read_config(settings, "slack_report")["allow"] == ["requests"]
    assert not plugins.tool_path(settings, "slack_read").is_file()
    assert "SLACK_USER_TOKEN" not in ConfigStore()._secrets()
    assert not any("standard input" in line for line in lines)


def test_one_box_swapped_for_another_with_no_terminal_is_off_at_once_and_prints_the_rest(
    settings,
):
    """What `--allow` leaves out is off now; what it adds prints the manifest, the steps
    and the next command, and offers no token that was just forgotten as one on file."""
    settings = connected_slack(settings, "requests")

    lines = login("slack", settings, allow=("send",))

    text = "\n".join(lines)
    assert plugins.read_config(settings, "slack_report")["allow"] == []
    # The app-level token goes with requests, and with it the user token an earlier
    # build kept with neither reading nor sending ticked.
    assert "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten." in lines
    assert "Acme is connected. Adding: sending messages as you." in lines
    assert "The manifest:" in text and "api.slack.com/apps?new_app=1" not in text
    assert text.count('"display_information"') == 1  # the steps print it, once
    assert ("Then hand the user token over on standard input, never on the command line:"
            "\n  keryx auth login slack --stdin --allow send") in text


# --- the tokens piped in: what was unticked first, as at a terminal -----------------------


@dataclasses.dataclass
class PipedSlack:
    """Slack as `keryx auth login slack --stdin` reaches it with no terminal."""

    team: Workspace = Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX")
    user: Workspace = Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER")
    calls: list = dataclasses.field(default_factory=list)

    def api(self) -> SlackApi:
        def auth_test(token):
            self.calls.append(token)
            return self.user if token.startswith("xoxp-") else self.team

        return SlackApi(
            auth_test=auth_test,
            post_text=lambda token, channel, text: SlackResult(),
            open_connection=lambda token: Connection(url="wss://x/?ticket=t"),
        )


def piped(settings, monkeypatch, fake, *, tokens, allow) -> list[str]:
    """`keryx auth login slack --stdin --allow …` with `tokens` on standard input."""
    monkeypatch.setattr(slack, "SlackApi", fake.api)  # what `connect` makes by default
    return login("slack", settings, tokens=tokens, stdin=True, test=False, allow=allow)


def slack_secrets() -> set[str]:
    return {key for key in ConfigStore()._secrets() if key.startswith("SLACK_")}


def test_unticking_with_stdin_and_nothing_piped_needs_nothing_from_slack(settings, monkeypatch):
    """`--stdin --allow none < /dev/null` while Slack cannot be reached: reading is off,
    its tool is off and the user token is forgotten, exactly as without `--stdin`."""
    settings = connected_slack(settings, "read")
    plugins.install(settings, "slack_read")
    down = PipedSlack(team=Workspace(error="unreachable"))

    lines = piped(settings, monkeypatch, down, tokens="", allow=("none",))

    assert down.calls == []
    assert plugins.read_config(settings, "slack_report")["allow"] == []
    assert not plugins.tool_path(settings, "slack_read").is_file()
    assert slack_secrets() == {"SLACK_BOT_TOKEN"}
    assert lines[0] == ("Acme: reading Slack as you is off, from this moment. A running "
                        "Keryx stops at once.")
    assert "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten." in lines
    # The manifest that takes it away in Slack too, which every other path prints.
    assert json.loads(lines[-1]) == app_manifest(frozenset(), assistant="Lyra")


def test_a_swap_with_stdin_unticks_first_and_only_what_was_added_is_refused(
    settings, monkeypatch
):
    """Reading ticked, `--stdin --allow send` with a user token that may not post yet
    (the app was not reinstalled): reading is off whatever becomes of sending, and the
    refusal says so."""
    settings = connected_slack(settings, "read")
    plugins.install(settings, "slack_read")
    not_yet = PipedSlack(user=Workspace(
        team="Acme", team_id="T0ACME", user_id="U0OWNER",
        scopes=user_scopes(frozenset({Permission.READ}))))

    with pytest.raises(AuthError) as refused:
        piped(settings, monkeypatch, not_yet, tokens="xoxp-1\n", allow=("send",))

    assert "may not yet do what you ticked" in str(refused.value)
    assert str(refused.value).endswith(
        "Nothing else was saved, and what you unticked stays off")
    assert plugins.read_config(settings, "slack_report")["allow"] == []
    assert not plugins.tool_path(settings, "slack_read").is_file()
    assert "SLACK_USER_TOKEN" not in slack_secrets()


def test_a_swap_with_nothing_piped_says_which_token_what_was_added_needs(settings, monkeypatch):
    settings = connected_slack(settings, "read")

    with pytest.raises(AuthError) as refused:
        piped(settings, monkeypatch, PipedSlack(), tokens="", allow=("send",))

    assert str(refused.value) == ("no token on standard input: this needs the user token "
                                  "(xoxp-). What you unticked stays off")
    assert plugins.read_config(settings, "slack_report")["allow"] == []


def test_a_user_token_piped_in_with_nothing_ticked_is_never_kept_and_the_manifest_is_printed(
    settings, monkeypatch
):
    """The setup instructions' own command, `printenv BOT USER | keryx auth login slack
    --stdin --allow none`, on a workspace that read and sent, with the same two tokens
    and an app that was not reinstalled: the user token still reads at Slack, so it is
    not on file afterwards, and what trims the app is printed."""
    settings = connected_slack(settings, "read", "send")
    still = PipedSlack(user=Workspace(
        team="Acme", team_id="T0ACME", user_id="U0OWNER",
        scopes=user_scopes(frozenset({Permission.READ, Permission.SEND}))))

    lines = piped(settings, monkeypatch, still, tokens="xoxb-1\nxoxp-1\n", allow=("none",))

    text = "\n".join(lines)
    assert plugins.read_config(settings, "slack_report")["allow"] == []
    assert slack_secrets() == {"SLACK_BOT_TOKEN"}
    assert "SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten." in lines
    assert slack.USED_ONCE in lines
    assert any(line.startswith("note: the app in Slack still grants more than is ticked")
               for line in lines)
    assert "App Manifest page" in text and '"display_information"' in text
    # Somebody else's is still refused, whatever is ticked.
    theirs = PipedSlack(user=Workspace(team="Acme", team_id="T0ACME", user_id="U0ELSE"))
    with pytest.raises(AuthError, match="the user token belongs to U0ELSE"):
        piped(settings, monkeypatch, theirs, tokens="xoxp-theirs\n", allow=("none",))


def test_a_login_rewrites_a_line_from_before_the_boxes_that_is_not_understood(
    settings, monkeypatch
):
    """What the refusal of such a line tells the owner to run. It is read as nothing
    ticked, the file is written back without it, and the login goes on from there."""
    settings = connected_slack(settings)
    path = plugins.config_path(settings, "slack_report")
    path.write_text('access = "reed"\n' + path.read_text().replace("allow = []\n", ""))
    with pytest.raises(plugins.PluginConfigError, match="the access line is not understood"):
        plugins.read_config(settings, "slack_report")
    monkeypatch.setattr(slack, "SlackApi", PipedSlack().api)

    lines = login("slack", settings, allow=("none",))

    assert lines[0] == slack.MENDED
    assert lines[1].startswith("Acme is connected, and nothing was changed.")
    values = plugins.read_config(settings, "slack_report")
    assert values["allow"] == [] and values["owner_id"] == "U0OWNER"
    assert "access" not in [line.split(" = ")[0] for line in path.read_text().splitlines()]


def test_slack_not_answering_after_an_untick_says_the_untick_stays(settings, monkeypatch):
    settings = connected_slack(settings, "requests", "read")
    down = PipedSlack(team=Workspace(error="unreachable"))

    with pytest.raises(AuthError) as refused:
        piped(settings, monkeypatch, down, tokens="xoxb-2\n", allow=("requests",))

    assert str(refused.value).endswith(
        "nothing else was saved, and what you unticked stays off")
    assert plugins.read_config(settings, "slack_report")["allow"] == ["requests"]
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == "xoxb-1"


def test_tokens_piped_in_with_nothing_unticked_are_refused_as_they_always_were(
    settings, monkeypatch
):
    settings = connected_slack(settings, "requests")
    down = PipedSlack(team=Workspace(error="invalid_auth"))

    with pytest.raises(AuthError) as refused:
        piped(settings, monkeypatch, down, tokens="xoxb-2\n", allow=None)

    assert str(refused.value).endswith("nothing was saved")


def test_at_a_terminal_a_slack_login_shows_the_boxes_and_what_is_ticked_is_what_it_makes(
    settings,
):
    shown, lines = [], []

    def choose(question, choices):
        shown.append((question, [(box.value, box.checked) for box in choices]))
        return ["read"]

    def leave(question):
        raise EOFError

    with pytest.raises(auth.AuthStopped):
        auth.login("slack", settings, ConfigStore(), options=LoginOptions(), echo=lines.append,
                   run_login=lambda argv: 0, ask_secret=leave, choose=choose)

    assert shown == [("What else may Keryx do in your Slack?",
                      [("requests", False), ("read", False), ("send", False)])]
    assert new_app_url(frozenset({Permission.READ}), assistant="Lyra") in lines
    assert ConfigStore().stored() == {}


def test_a_slack_login_left_at_the_terminal_is_stopped_and_not_called_a_failure(settings):
    """With somebody to ask (`ask_secret`), the login goes on to the tokens; leaving a
    question is `AuthStopped`, whose sentence says the same command picks it up."""
    lines, asked = [], []

    def leave(question):
        asked.append(question)
        raise KeyboardInterrupt

    with pytest.raises(auth.AuthStopped) as stopped:
        auth.login("slack", settings, ConfigStore(), options=LoginOptions(), echo=lines.append,
                   run_login=lambda argv: 0, ask_secret=leave)

    assert new_app_url(assistant="Lyra") in lines and len(asked) == 1
    assert str(stopped.value) == (
        "nothing was saved: run `keryx auth login slack` again to pick this up")
    assert ConfigStore().stored() == {}


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
    assert "`keryx auth login google` makes one" in report["google"]["detail"]


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


def test_at_a_terminal_a_paste_is_read_whole_by_its_own_reader(settings, browser, page):
    asked = []

    def ask(question, default=None):
        return "paste"

    def ask_paste(question):
        asked.append(question)
        return json.dumps(CLIENT_JSON, indent=2)

    auth.login("google", settings, ConfigStore(), options=LoginOptions(), echo=lambda line: None,
               run_login=None, ask=ask, ask_secret=lambda q: pytest.fail("one line only"),
               ask_paste=ask_paste)

    assert asked == [google.PASTE_WHOLE_QUESTION]
    assert saved_client(settings) == CLIENT_JSON
