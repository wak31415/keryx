"""The Google section: one sign-in for everything, asked nothing when it is all connected."""

import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest

from keryx.config.store import ConfigStore
from keryx.integrations import google as account
from keryx.integrations.google import SERVICES
from keryx.setup import google
from keryx.setup.google import GoogleSetupError
from keryx.setup.rewind import Recorder
from keryx.setup.ui import Back, Forward
from keryx.setup.wizard import Section, run_walk

from .fakes import DEFAULT, FAKE_ID, FAKE_SECRET, Lazy

ALL = tuple(SERVICES)
CLIENT = {"installed": {"client_id": "cid", "client_secret": "csecret"}}
SECTION = Section("google", "Google", google.run_section)


def own_client():
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})


def landed(ctx, code="c"):
    """The address the browser lands on for the link the wizard has just shown."""

    def read() -> str:
        pending = json.loads((ctx.settings.data_dir / "google_signin.json").read_text())
        return f"http://localhost:1/?state={pending['state']}&code={code}"

    return Lazy(read)


def signed_in(ctx, services=ALL, email="sam@example.com"):
    account.write_token(
        ctx.settings,
        account.GoogleToken("rt", "cid", "csecret", account.requested_scopes(services), email),
    )


# --- one sign-in --------------------------------------------------------------------------


def test_with_a_client_it_is_one_question_and_one_sign_in(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert ctx.ui.done()
    assert world.calls == [("google_browser", ALL)]
    assert account.read_token(ctx.settings).connected == ALL
    assert ctx.settings.user_google_email == "sam@example.com"
    assert ctx.settings.google_workspace_mcp is True
    [panel] = ctx.ui.lines("panel")
    assert "Your browser is opening the link below" in panel
    # The link is long: it goes on a line of its own, whole, not wrapped inside the panel.
    [link] = ctx.ui.lines("link")
    assert link.startswith("https://accounts.google.com/") and link not in panel
    assert not ctx.ui.lines("markdown")  # a client is there: no Google Cloud steps


def test_every_service_gets_a_line(make_ctx, world):
    own_client()
    world.google_services = ("gmail", "calendar")
    ctx = make_ctx([("Sign in to Google?", "signin")])

    google.run_section(ctx)

    assert "Google: signed in as sam@example.com" in (
        ctx.ui.lines("success")
    )
    assert [line for line in ctx.ui.lines("success") if line.endswith("a call")] == [
        "Gmail: connected — agents read, label, draft and send mail, and email answers on a call",
        "Calendar: connected — agents see your calendars and create or change events, and "
        "calendar answers on a call",
    ]
    assert ctx.ui.lines("warn") == [
        "Drive: not connected", "Docs: not connected", "Sheets: not connected",
        "Slides: not connected",
    ]  # fmt: skip


def test_signed_in_with_everything_connected_asks_nothing(make_ctx, world):
    own_client()
    ctx = make_ctx([])
    signed_in(ctx)

    google.run_section(ctx)

    assert ctx.ui.asked == [] and world.calls == []
    assert "Google: signed in as sam@example.com" in ctx.ui.lines("success")
    assert ConfigStore().stored().get("GOOGLE_WORKSPACE_MCP") is None  # theirs to turn on or off


def test_a_partial_sign_in_offers_to_sign_in_again(make_ctx, world):
    own_client()
    ctx = make_ctx([("signed in to Google as sam@example.com. Sign in again?", DEFAULT)])
    signed_in(ctx, ["gmail"])

    google.run_section(ctx)

    [(question, choices)] = ctx.ui.choices.items()
    assert [(choice.value, choice.label) for choice in choices] == [
        ("keep", "Keep this sign-in"), ("again", "Sign in again")
    ]  # fmt: skip
    assert world.calls == [("google_browser", ALL)]


def test_reviewing_a_complete_sign_in_keeps_it_by_default(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in again?", DEFAULT)], review=True)
    signed_in(ctx)

    google.run_section(ctx)

    assert world.calls == []


def test_skipping_touches_nothing_and_says_how_to_come_back(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", "skip")])

    google.run_section(ctx)

    assert world.calls == [] and ConfigStore().stored().keys() == {
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"
    }  # fmt: skip
    assert any("keryx auth login google" in line for line in ctx.ui.lines("note"))


# --- which client: one of the owner's own, made now ----------------------------------------

#: The path question, as the wizard asks it.
Q = "The file you downloaded, or the Client ID"


def downloaded(name="client_secret_1.json", body=CLIENT, age=0.0):
    """A file Google's console downloaded into `~/Downloads`, `age` seconds ago."""
    path = Path.home() / "Downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def installed(ctx):
    return ctx.settings.config_dir / "google_client_secret.json"


def test_with_no_client_it_opens_googles_page_and_enter_takes_the_newest_download(
    make_ctx, world
):
    downloaded("client_secret_old.json", {"installed": {"client_id": "old"}}, age=600)
    newest = downloaded()
    ctx = make_ctx([("Sign in to Google?", "signin"), (Q, "")])  # Enter: nothing typed

    google.run_section(ctx)

    assert ctx.ui.done()
    # Why there is a step at all, said first: Google's terms, and what Personal mode is.
    [why] = [line for line in ctx.ui.lines("note") if "terms" in line]
    assert "open-source" in why and "Personal mode" in why
    assert world.calls == [("open", google.PERSONAL_MODE_URL), ("google_browser", ALL)]
    panel = ctx.ui.lines("panel")[0]
    assert "Your browser is opening Google's page" in panel
    assert "Download JSON if it is offered, or copy the Client ID" in panel
    assert ctx.ui.lines("link")[0] == google.PERSONAL_MODE_URL
    assert f"Enter takes {newest}" in ctx.ui.lines("note")
    assert "skip leaves it for later" in ctx.ui.asked[-1][1]
    assert json.loads(installed(ctx).read_text()) == json.loads(newest.read_text())
    assert stat.S_IMODE(installed(ctx).stat().st_mode) == 0o600
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"] == str(installed(ctx))
    [consent] = ctx.ui.lines("markdown")
    assert "Select all" in consent


def test_nothing_is_put_in_the_answer_box_beforehand(make_ctx, world, monkeypatch):
    """A path in the box would have to be deleted before typing `other` or a Client ID."""
    downloaded()
    defaults = []
    ctx = make_ctx([("Sign in to Google?", "signin")])
    text = ctx.ui.text

    def spy(message, *, default="", validate=None, multiline=False):
        defaults.append(default)
        return text(message, default=default, validate=validate, multiline=multiline)

    ctx.ui.text = spy
    ctx.ui.answers.append((Q, "skip"))

    google.run_section(ctx)

    assert defaults == [""]


def test_a_client_id_is_asked_for_its_secret_hidden_and_kept_as_a_file_would_be(
    make_ctx, world
):
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, FAKE_ID),
        ("Client Secret", FAKE_SECRET),
    ])

    google.run_section(ctx)

    assert ctx.ui.asked[-1] == ("secret", "Client Secret (hidden)")
    saved = json.loads(installed(ctx).read_text())["installed"]
    assert (saved["client_id"], saved["client_secret"]) == (
        FAKE_ID, FAKE_SECRET)
    assert account.resolve_client(ctx.settings).client_id == FAKE_ID
    assert world.calls[-1] == ("google_browser", ALL)
    # The secret question says which Client ID it belongs to, on a screen of its own.
    notes = ctx.ui.lines("note")
    assert f"Client ID: {FAKE_ID}" in notes
    assert google.KEPT_ONCE_IT_WORKS in notes


ID, SECRET = FAKE_ID, FAKE_SECRET


def test_the_client_is_saved_only_after_the_sign_in_with_it(make_ctx, world):
    seen = []
    ctx = make_ctx([("Sign in to Google?", "signin"), (Q, ID), ("Client Secret", SECRET)])
    browser = ctx.probes.google_browser_signin

    def signin(settings, chosen, announce):
        seen.append((installed(ctx).exists(), google.is_staged(settings)))
        return browser(settings, chosen, announce)

    ctx.probes = dataclasses.replace(ctx.probes, google_browser_signin=signin)
    google.run_section(ctx)

    assert seen == [(False, True)]
    assert installed(ctx).exists() and not google.staged_client_path(ctx.settings).exists()
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"] == str(installed(ctx))


def test_a_client_google_refuses_is_asked_for_again_and_never_saved(make_ctx, world):
    refusals = [google.ClientRefused("Google refused the client (invalid_client): its "
                                     "Client ID or Client Secret is wrong")]
    browser = None
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, ID), ("Client Secret", "GOCSPX" + "-" + "w" * 28),
        ("Try the sign-in again?", DEFAULT),        # the default: give the client again
        (Q, ID), ("Client Secret", SECRET),
    ])
    browser = ctx.probes.google_browser_signin

    def signin(settings, chosen, announce):
        return refusals.pop() if refusals else browser(settings, chosen, announce)

    ctx.probes = dataclasses.replace(ctx.probes, google_browser_signin=signin)
    google.run_section(ctx)

    assert ctx.ui.done()
    [(question, choices)] = [(q, c) for q, c in ctx.ui.choices.items() if "again" in q]
    assert "client" in [choice.value for choice in choices]
    assert json.loads(installed(ctx).read_text())["installed"]["client_secret"] == SECRET
    # The page is not opened a second time for the second client.
    assert world.calls.count(("open", google.PERSONAL_MODE_URL)) == 1


def test_skipping_when_the_client_is_asked_for_again_saves_nothing(make_ctx, world):
    world.google_error = "Google refused the client (invalid_client)"
    ctx = make_ctx([
        ("Sign in to Google?", "signin"), (Q, ID), ("Client Secret", SECRET),
        ("Try the sign-in again?", "client"),
        (Q, "skip"),
    ])

    google.run_section(ctx)

    assert ctx.ui.done() and not installed(ctx).exists()
    assert ctx.ui.lines("note")[-1] == google.LATER_NOTE


def test_a_failed_sign_in_with_a_client_given_here_saves_no_client(make_ctx, world):
    world.google_error = "the browser did not come back within 5 minutes"
    ctx = make_ctx([
        ("Sign in to Google?", "signin"), (Q, ID), ("Client Secret", SECRET),
        ("Try the sign-in again?", "skip"),
    ])

    google.run_section(ctx)

    assert not installed(ctx).exists() and not google.staged_client_path(ctx.settings).exists()
    assert "GOOGLE_CLIENT_SECRETS_FILE" not in ConfigStore().stored()
    panel = [line for line in ctx.ui.lines("panel") if line.startswith("Sign in to Google")][0]
    assert "the Client ID is wrong: press Ctrl-C, then choose to give the client again" in panel


def test_a_refused_answer_is_never_offered_back_after_esc(make_ctx, world, monkeypatch):
    """A secret typed where it shows is refused; Esc at the next question goes back to
    it, and the box is empty, not holding the secret."""
    defaults = []
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, SECRET),            # refused: it is the secret, typed at the visible question
        (Q, Back()),
        (Q, "skip"),
    ])
    text = ctx.ui.text

    def spy(message, *, default="", validate=None, multiline=False):
        defaults.append(default)
        return text(message, default=default, validate=validate, multiline=multiline)

    ctx.ui.text = spy

    assert run_walk(ctx, [SECTION], {}) is True

    assert SECRET not in defaults and defaults == ["", "", ""]


def test_what_is_left_of_a_paste_is_dropped_after_a_hidden_question_and_a_refusal(
    make_ctx, world, monkeypatch
):
    dropped = []
    monkeypatch.setattr(google, "drop_typed_ahead", lambda: dropped.append(True))
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "paste"), ("Paste what is in the file", "{"),
        (Q, "skip"),
    ])

    google.run_section(ctx)

    assert len(dropped) == 2  # after the hidden question, and after the refusal


def test_where_it_was_downloaded_is_always_asked_and_a_folder_takes_its_newest(
    make_ctx, world, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "client_secret_9.json").write_text(json.dumps(CLIENT))
    (Path.home() / "Downloads").mkdir(parents=True)
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, ""),     # Enter: ~/Downloads/, which is empty
        (Q, str(elsewhere)),
    ])

    google.run_section(ctx)

    assert ctx.ui.lines("error") == [
        f"There is no client_secret_*.json file in {Path.home() / 'Downloads'}."
    ]
    taken = f"Took {elsewhere / 'client_secret_9.json'}, the newest one there."
    assert taken in ctx.ui.lines("note")
    assert json.loads(installed(ctx).read_text()) == CLIENT


def test_a_wrong_answer_is_said_with_the_steps_still_on_screen(make_ctx, world, tmp_path):
    bad = downloaded(body={"nope": 1})
    web = downloaded("client_secret_web.json", {"web": {"client_id": "w", "client_secret": "s"}})
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, str(bad)),
        (Q, f"'{web}'"),
        (Q, str(tmp_path / "nothing.json")),
        (Q, "skip"),
    ])

    google.run_section(ctx)

    errors = ctx.ui.lines("error")
    assert "not a Google OAuth client file" in errors[0]
    assert "Keryx needs a Desktop app client" in errors[1]
    assert errors[2] == f"There is no file at {tmp_path / 'nothing.json'}."
    # Each refusal is said below the link and the `other` hint, said again.
    said = ctx.ui.said
    for error in errors:
        at = said.index(("error", error))
        assert said[at - 2] == ("link", google.PERSONAL_MODE_URL)
        assert said[at - 1] == ("note", google.NO_PROJECT_NOTE)
    assert google.LATER_NOTE in ctx.ui.lines("note")  # skip: left for later, as Skip says
    assert not installed(ctx).exists()
    assert "GOOGLE_CLIENT_SECRETS_FILE" not in ConfigStore().stored()
    assert world.calls == [("open", google.PERSONAL_MODE_URL)]


def test_after_other_a_wrong_answer_keeps_the_other_steps_on_screen(make_ctx, world, tmp_path):
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "other"),
        (Q, str(tmp_path / "nothing.json")),
        (Q, "skip"),
    ])

    google.run_section(ctx)

    said = ctx.ui.said
    at = said.index(("error", f"There is no file at {tmp_path / 'nothing.json'}."))
    assert said[at - 1] == ("note", f"6. {google.OTHER_STEPS[5][0]}")
    assert said[at - 2] == ("link", "https://console.cloud.google.com/auth/clients/create")


def test_with_the_browser_elsewhere_the_link_is_for_any_device_and_the_file_can_be_pasted(
    make_ctx, world
):
    world.headless = True
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "paste"),
        ("Paste what is in the file", json.dumps(CLIENT)),
    ])
    ctx.ui.answers.append(("address you landed on", landed(ctx)))

    google.run_section(ctx)

    assert ctx.ui.done()
    assert ("open", google.PERSONAL_MODE_URL) not in world.calls  # no browser here to open
    assert "Open the link below in a browser, on any device." in ctx.ui.lines("panel")[0]
    assert google.ELSEWHERE_NOTE in ctx.ui.lines("note")
    assert json.loads(installed(ctx).read_text()) == CLIENT
    assert account.read_token(ctx.settings) is not None


def test_nothing_pasted_is_said_and_asked_again(make_ctx, world):
    world.headless = True
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "paste"),
        ("Paste what is in the file", ""),
        (Q, "skip"),
    ])

    google.run_section(ctx)

    assert ctx.ui.lines("error") == ["Nothing was pasted."] and not installed(ctx).exists()


def test_a_page_that_does_not_open_says_to_open_the_link(make_ctx, world):
    world.browser_opens = False
    downloaded()
    ctx = make_ctx([("Sign in to Google?", "signin"), (Q, "")])

    google.run_section(ctx)

    assert ctx.ui.lines("panel")[0].splitlines()[1] == (
        "Open the link below in a browser, on any device.")


def test_with_no_client_to_hand_skipping_is_the_default_unless_codex_needs_it(make_ctx, world):
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])
    google.run_section(ctx)
    assert ctx.ui.done() and world.calls == []
    assert ctx.ui.lines("note")[-1] == google.LATER_NOTE

    ConfigStore().set({"AGENT_BACKEND": "codex", "AGENTS_ENABLED": "codex"})
    codex = make_ctx([("Sign in to Google?", DEFAULT), (Q, "skip")])
    google.run_section(codex)
    assert codex.ui.done() and world.calls == [("open", google.PERSONAL_MODE_URL)]


def test_other_shows_the_way_for_an_account_that_cannot_create_a_project(make_ctx, world):
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "other"),
        (Q, str(downloaded())),
    ])

    google.run_section(ctx)

    assert ctx.ui.done() and not ctx.ui.lines("error")
    notes = ctx.ui.lines("note")
    assert google.NO_PROJECT_NOTE in notes and google.OTHER_INTRO in notes
    assert [line for line in notes if line[:2] in {f"{n}." for n in range(1, 7)}] == [
        f"{number}. {step}" for number, (step, _) in enumerate(google.OTHER_STEPS, 1)
    ]
    # Each link on a line of its own, whole: inside a note it would be wrapped.
    assert ctx.ui.lines("link")[1:6] == [link for _, link in google.OTHER_STEPS if link]
    assert json.loads(installed(ctx).read_text()) == CLIENT
    assert world.calls == [("open", google.PERSONAL_MODE_URL), ("google_browser", ALL)]


def test_the_wait_says_what_an_organization_that_blocks_the_app_means(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert google.BLOCKED_NOTE in ctx.ui.lines("panel")[0]


def test_the_paste_back_says_it_too(make_ctx, world):
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("address you landed on", "")])

    google.run_section(ctx)

    assert google.BLOCKED_NOTE in ctx.ui.lines("panel")[0]


def test_going_back_from_the_path_question_does_not_open_the_page_again(make_ctx, world):
    """`other`, then Esc at the second path question: the section runs again from its
    record, up to the first path question, and the page opened by the probe is not
    opened a second time."""
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "other"),
        (Q, Back()),
        (Q, "skip"),
    ])

    assert run_walk(ctx, [SECTION], {}) is True

    assert ctx.ui.done()
    assert world.calls == [("open", google.PERSONAL_MODE_URL)]


def test_going_back_and_then_forward_does_not_open_the_page_again(make_ctx, world):
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, "other"),
        (Q, Back()),
        (Q, Forward()),     # Tab: replays `other`, to the question not answered yet
        (Q, "skip"),
    ])

    assert run_walk(ctx, [SECTION], {}) is True

    assert ctx.ui.done()
    assert world.calls == [("open", google.PERSONAL_MODE_URL)]


def test_a_replay_says_why_before_the_steps(make_ctx, world):
    """Esc then Tab: the replay ends at the recorded browser probe, and the note held
    before it is shown ahead of the panel, not below the link."""
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        (Q, Back()),
        ("Sign in to Google?", Forward()),
        (Q, "skip"),
    ])

    assert run_walk(ctx, [SECTION], {}) is True

    said = ctx.ui.said
    # The last run: everything from the second "why" on.
    last = len(said) - 1 - [kind for kind, _ in reversed(said)].index("panel")
    why = max(at for at, (kind, text) in enumerate(said) if "terms" in text)
    assert why < last


# --- a browser somewhere else -------------------------------------------------------------


def test_with_no_display_the_address_is_pasted_back(make_ctx, world):
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])
    ctx.ui.answers.append(("address you landed on", landed(ctx)))

    google.run_section(ctx)

    [(probe, address)] = world.calls
    assert probe == "google_finish" and address.startswith("http://localhost:1/?state=")
    assert not (ctx.settings.data_dir / "google_signin.json").exists()
    [panel] = ctx.ui.lines("panel")
    assert "open this on any device" in panel and "accounts.google.com" not in panel
    [link] = ctx.ui.lines("link")  # whole, on a line of its own, to copy to another device
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1" in link
    assert ctx.settings.user_google_email == "sam@example.com"


def test_a_blank_address_skips_the_sign_in_and_leaves_no_verifier_behind(make_ctx, world):
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("address you landed on", "")])

    google.run_section(ctx)

    assert world.calls == [] and account.read_token(ctx.settings) is None
    assert not (ctx.settings.data_dir / "google_signin.json").exists()


def test_a_sign_in_that_fails_can_be_tried_the_other_way_or_left(make_ctx, world):
    own_client()
    world.google_error = "the browser did not come back within 5 minutes"
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Try the sign-in again?", DEFAULT)])
    ctx.ui.answers += [("address you landed on", landed(ctx)),
                       ("Try the sign-in again?", "here"), ("Try the sign-in again?", "skip")]

    google.run_section(ctx)

    assert ctx.ui.done()
    assert [call[0] for call in world.calls] == ["google_browser", "google_finish",
                                                 "google_browser"]
    assert len(ctx.ui.lines("error")) == 3 and account.read_token(ctx.settings) is None


def test_no_client_after_all_is_said_not_raised(make_ctx, world, monkeypatch):
    own_client()
    ctx = make_ctx([("Sign in to Google?", "signin")])

    def refuse(settings, wanted=None):
        raise GoogleSetupError("there is no Google OAuth client to sign in with")

    monkeypatch.setattr(google, "offer", refuse)
    google.run_section(ctx)

    assert ctx.ui.lines("error") == ["there is no Google OAuth client to sign in with"]


# --- the address, and the agents' tools ---------------------------------------------------


def test_an_account_other_than_the_saved_one_is_kept_only_after_a_yes(make_ctx, world):
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Use sam@example.com?", True)])

    google.run_section(ctx)

    assert account.read_token(ctx.settings).email == "sam@example.com"
    assert ctx.settings.user_google_email == "sam@example.com"
    assert ctx.settings.google_workspace_mcp is True
    assert ("google_discard", "sam@example.com") not in world.calls


def test_an_unexpected_account_is_turned_down_by_default_and_nothing_is_saved(make_ctx, world):
    """An unexpected account is what somebody else finishing the sign-in looks like."""
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Use sam@example.com?", DEFAULT)])

    google.run_section(ctx)

    assert account.read_token(ctx.settings) is None
    assert ctx.settings.user_google_email == "old@example.com"
    assert ConfigStore().stored().get("GOOGLE_WORKSPACE_MCP") is None
    assert world.calls[-1] == ("google_discard", "sam@example.com")
    assert any("Nothing was saved" in line for line in ctx.ui.lines("note"))


def test_esc_at_that_question_leaves_nothing_half_saved(make_ctx, world):
    """Nothing is written before the yes, so there is no token without its settings."""
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        ("Use sam@example.com?", Back()),
        ("Sign in to Google?", "skip"),
    ])

    assert run_walk(ctx, [SECTION], {}) is True

    assert account.read_token(ctx.settings) is None
    assert ConfigStore().stored() == {
        "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs",
        "USER_GOOGLE_EMAIL": "old@example.com",
    }  # fmt: skip


def test_a_sign_in_that_cannot_be_saved_is_said_and_saves_no_settings(
    make_ctx, world, monkeypatch
):
    own_client()

    def refuse(settings, grant):
        raise GoogleSetupError("could not save the sign-in (No space left on device)")

    monkeypatch.setattr(google, "keep", refuse)
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert ctx.ui.lines("error") == ["could not save the sign-in (No space left on device)"]
    assert ConfigStore().stored().get("GOOGLE_WORKSPACE_MCP") is None


def test_agents_tools_the_owner_turned_off_stay_off(make_ctx, world):
    own_client()
    ConfigStore().set({"GOOGLE_WORKSPACE_MCP": False})
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert ctx.settings.google_workspace_mcp is False
    assert any("stay off, as you set them" in line for line in ctx.ui.lines("note"))


def test_the_same_address_is_not_asked_about(make_ctx, world):
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "sam@example.com", "GOOGLE_WORKSPACE_MCP": True})
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert ctx.ui.done() and not any("saved" in line for line in ctx.ui.lines("success"))


# --- going back -------------------------------------------------------------------------


def test_going_back_and_forward_never_signs_in_twice(make_ctx, world):
    """Esc twice, back past this section, and Tab forward through it again: the section
    finds everything connected, asks nothing and opens no browser."""
    own_client()

    def before(ctx):
        ctx.ui.text("First question", default="")

    def after(ctx):
        ctx.ui.text("Next question", default="")

    ctx = make_ctx([
        ("First question", "one"),
        ("Sign in to Google?", "signin"),
        ("Next question", Back()),             # back to Google, now signed in
        ("Sign in again?", Back()),            # and back again, to the section before
        ("First question", Forward()),         # then forward through both
        ("Next question", "done"),
    ])
    walk = [Section("first", "First", before), SECTION, Section("next", "Next", after)]

    assert run_walk(ctx, walk, {}) is True

    assert ctx.ui.done()
    assert world.calls == [("google_browser", ALL)]


def test_enter_after_going_back_does_not_sign_in_a_second_time(make_ctx, world):
    """The answer recorded before the sign-in was "sign in". After it, the question is
    another one with other answers, so Enter keeps the sign-in that was just made."""
    own_client()

    def after(ctx):
        ctx.ui.text("Next question", default="")

    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        ("Next question", Back()),
        ("Sign in again?", DEFAULT),           # Enter: whatever is highlighted
        ("Next question", "done"),
    ])

    assert run_walk(ctx, [SECTION, Section("next", "Next", after)], {}) is True

    assert ctx.ui.done() and world.calls == [("google_browser", ALL)]


def test_an_address_from_an_earlier_link_is_refused_at_the_question(make_ctx, world):
    """Going back to the address after it was used offers it again, and it is spent."""
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])
    ctx.ui.answers.append(("address you landed on", "http://localhost:1/?state=earlier&code=c"))

    with pytest.raises(AssertionError, match="not from the link above"):
        google.run_section(ctx)

    assert world.calls == []


def test_esc_after_a_pasted_sign_in_does_not_take_the_spent_address_again(make_ctx, world):
    """Back at the address question there is a new link, and the old address is offered:
    Enter is refused there and then, not exchanged a second time."""
    own_client()
    world.headless = True
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([("Sign in to Google?", "signin")])
    ctx.ui.answers += [
        ("address you landed on", landed(ctx)),
        ("Use sam@example.com?", Back()),
        ("address you landed on", DEFAULT),     # the address from the link before
    ]

    with pytest.raises(AssertionError, match="not from the link above"):
        run_walk(ctx, [SECTION], {})

    assert [call[0] for call in world.calls] == ["google_finish"]
    assert account.read_token(ctx.settings) is None


def test_the_address_question_checks_only_that_it_answers_this_link(settings):
    settings.google_oauth_client_id, settings.google_oauth_client_secret = "cid", "cs"
    problem = google._from_this_link(settings)
    assert "not an address" in problem("http://localhost:1/?state=s")  # no link is out yet

    state = google.start_signin(settings).split("state=")[1].split("&")[0]

    assert problem("") is None and "not an address" in problem("http://[::1")
    assert problem(f"'http://localhost:1/?state={state}&code=c'") is None
    assert "not from the link above" in problem("http://localhost:1/?state=other&code=c")
    assert problem("http://localhost:1/?error=access_denied") is None  # Google's no is an answer


def test_a_sign_in_that_failed_is_not_run_again_on_the_way_back(make_ctx, world):
    """A probe that raises is not recorded, and would run again when the section replays:
    the browser would open a second time for a sign-in the owner already gave up on."""
    own_client()
    world.google_error = "the browser did not come back within 5 minutes"

    def after(ctx):
        ctx.ui.text("Next question", default="")

    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        ("Try the sign-in again?", "skip"),
        ("Next question", Back()),              # back to "Try the sign-in again?"
        ("Try the sign-in again?", "skip"),
        ("Next question", "done"),
    ])

    assert run_walk(ctx, [SECTION, Section("next", "Next", after)], {}) is True

    assert ctx.ui.done()
    assert world.calls == [("google_browser", ALL)]


def test_a_section_replayed_from_its_record_runs_no_probe(make_ctx, world):
    """The record holds the sign-in's result, and a replay is handed it back: the same
    answers, in a section that still has nobody signed in, open no second browser."""
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([("Sign in to Google?", "signin"), ("Use sam@example.com?", False)])
    probes = ctx.probes
    first = Recorder(ctx.ui)
    ctx.ui, ctx.probes = first, first.probes(probes)
    google.run_section(ctx)
    calls = list(world.calls)
    assert calls == [("google_browser", ALL), ("google_discard", "sam@example.com")]

    second = Recorder(first.ui, first.log)
    ctx.ui, ctx.probes, ctx.review = second, second.probes(probes), True
    google.run_section(ctx)

    assert world.calls == calls and first.ui.done()  # not signed in twice, not revoked twice
    assert [entry.name for entry in second.log] == [entry.name for entry in first.log]


def test_a_failure_the_probe_hands_back_is_said(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Try the sign-in again?", "skip")])

    def fail(settings, chosen, announce):
        return GoogleSetupError("could not listen on 127.0.0.1")

    ctx.probes = dataclasses.replace(ctx.probes, google_browser_signin=fail)
    google.run_section(ctx)

    assert ctx.ui.lines("error") == ["could not listen on 127.0.0.1"]
