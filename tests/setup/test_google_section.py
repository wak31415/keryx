"""The Google section: one sign-in for everything, asked nothing when it is all connected."""

import dataclasses
import json
import stat

from keryx.config.store import ConfigStore
from keryx.integrations import google as account
from keryx.integrations.google import SERVICES
from keryx.setup import google
from keryx.setup.google import GoogleSetupError
from keryx.setup.rewind import Recorder
from keryx.setup.ui import Back, Forward
from keryx.setup.wizard import Section, run_walk

from .fakes import DEFAULT

ALL = tuple(SERVICES)
CLIENT = {"installed": {"client_id": "cid", "client_secret": "csecret"}}
SECTION = Section("google", "Google", google.run_section)


def own_client():
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})


def client_file(tmp_path, body=CLIENT):
    path = tmp_path / "Downloads" / "client_secret_123.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(body))
    return path


def published(tmp_path, monkeypatch, **extra):
    path = tmp_path / "google_client.json"
    path.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"},
                                **extra}))
    monkeypatch.setattr(account, "PUBLISHED_CLIENT_FILE", path)


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
    assert "accounts.google.com" in panel and "Your browser is opening this link" in panel
    assert not ctx.ui.lines("markdown")  # a client is there: no Google Cloud steps


def test_every_service_gets_a_line(make_ctx, world):
    own_client()
    world.google_services = ("gmail", "calendar")
    ctx = make_ctx([("Sign in to Google?", "signin")])

    google.run_section(ctx)

    assert "Google: signed in as sam@example.com with your own Google client" in (
        ctx.ui.lines("success")
    )
    assert [line for line in ctx.ui.lines("success") if line.endswith("a call")] == [
        "Gmail: connected — agents read, label, draft and send mail, and email answers on a call"
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
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])
    signed_in(ctx, ["gmail"])

    google.run_section(ctx)

    assert [choice.label for choice in ctx.ui.choices["Sign in to Google?"]] == [
        "Keep this sign-in", "Sign in again"
    ]  # fmt: skip
    assert world.calls == [("google_browser", ALL)]


def test_reviewing_a_complete_sign_in_keeps_it_by_default(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", DEFAULT)], review=True)
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


# --- which client -------------------------------------------------------------------------


def test_the_own_client_guide_is_shown_only_when_there_is_no_client(make_ctx, world, tmp_path):
    source = client_file(tmp_path)
    ctx = make_ctx([("Sign in to Google?", "signin"), ("Path to the downloaded", f"'{source}'")])

    google.run_section(ctx)

    [guide] = ctx.ui.lines("markdown")
    assert "console.cloud.google.com" in guide and "Desktop" in guide
    installed = ctx.settings.config_dir / "google_client_secret.json"
    assert json.loads(installed.read_text()) == CLIENT
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"] == str(installed)
    assert world.calls == [("google_browser", ALL)]


def test_with_no_client_to_hand_skipping_is_the_default_unless_codex_needs_it(make_ctx, world):
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])
    google.run_section(ctx)
    assert ctx.ui.done() and not ctx.ui.lines("markdown")

    ConfigStore().set({"AGENT_BACKEND": "codex", "AGENTS_ENABLED": "codex"})
    codex = make_ctx([("Sign in to Google?", DEFAULT), ("Path to the downloaded", "")])
    google.run_section(codex)
    assert codex.ui.done() and len(codex.ui.lines("markdown")) == 1


def test_a_wrong_client_file_is_refused_and_asked_again(make_ctx, world, tmp_path):
    bad = client_file(tmp_path, {"nope": 1})
    ctx = make_ctx(
        [("Sign in to Google?", "signin"), ("Path to the downloaded", str(bad)),
         ("Path to the downloaded", "")]
    )

    google.run_section(ctx)

    assert any("not a Google OAuth client file" in line for line in ctx.ui.lines("error"))
    assert not (ctx.settings.config_dir / "google_client_secret.json").exists()
    assert world.calls == []


def test_keryxs_own_client_needs_no_google_cloud_steps(make_ctx, world, tmp_path, monkeypatch):
    published(tmp_path, monkeypatch)
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    [consent] = ctx.ui.lines("markdown")
    assert "Google hasn't verified this app" in consent and "Advanced" in consent
    assert "console.cloud.google.com" not in consent
    assert world.calls == [("google_browser", ALL)]
    assert ctx.ui.done()


def test_what_keryxs_client_does_not_offer_is_said_once_and_not_asked_for(
    make_ctx, world, tmp_path, monkeypatch
):
    published(tmp_path, monkeypatch, keryx_services=["calendar", "drive"])
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert world.calls == [("google_browser", ("calendar", "drive"))]
    [note] = [line for line in ctx.ui.lines("note") if "not offered" in line]
    assert note.startswith("Gmail, Docs, Sheets and Slides are not offered by Keryx's Google app")
    assert not any("Gmail" in line for line in ctx.ui.lines("warn"))

    again = make_ctx([])  # everything on offer is connected: nothing left to ask
    google.run_section(again)
    assert again.ui.asked == []


# --- a browser somewhere else -------------------------------------------------------------


def test_with_no_display_the_address_is_pasted_back(make_ctx, world):
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT),
                    ("address you landed on", "http://localhost:1/?state=s&code=c")])

    google.run_section(ctx)

    assert world.calls == [("google_finish", "http://localhost:1/?state=s&code=c")]
    [panel] = ctx.ui.lines("panel")
    assert "open this on any device" in panel and "redirect_uri=http%3A%2F%2Flocalhost%3A1" in panel
    assert ctx.settings.user_google_email == "sam@example.com"


def test_a_blank_address_skips_the_sign_in(make_ctx, world):
    own_client()
    world.headless = True
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("address you landed on", "")])

    google.run_section(ctx)

    assert world.calls == [] and account.read_token(ctx.settings) is None


def test_a_sign_in_that_fails_can_be_tried_the_other_way_or_left(make_ctx, world):
    own_client()
    world.google_error = "the browser did not come back within 5 minutes"
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Try the sign-in again?", DEFAULT),
                    ("address you landed on", "http://localhost:1/?x"),
                    ("Try the sign-in again?", "here"), ("Try the sign-in again?", "skip")])

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


def test_a_different_saved_address_is_replaced_only_after_asking(make_ctx, world):
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "old@example.com"})
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Replace it with sam@example.com?", False)])

    google.run_section(ctx)

    assert ctx.settings.user_google_email == "old@example.com"
    assert ctx.settings.google_workspace_mcp is True


def test_the_same_address_is_not_asked_about(make_ctx, world):
    own_client()
    ConfigStore().set({"USER_GOOGLE_EMAIL": "sam@example.com", "GOOGLE_WORKSPACE_MCP": True})
    ctx = make_ctx([("Sign in to Google?", DEFAULT)])

    google.run_section(ctx)

    assert ctx.ui.done() and not any("saved" in line for line in ctx.ui.lines("success"))


# --- going back -------------------------------------------------------------------------


def test_going_back_and_forward_never_signs_in_twice(make_ctx, world):
    """Esc from the next section lands on this one's question, and Tab goes forward again
    from the record: the sign-in is a probe, so no browser opens a second time."""
    own_client()

    def after(ctx):
        ctx.ui.text("Next question", default="")

    ctx = make_ctx([
        ("Sign in to Google?", "signin"),
        ("Next question", Back()),             # back to Google, now signed in
        ("Sign in to Google?", Forward()),     # and forward again, from the record
        ("Next question", "done"),
    ])

    assert run_walk(ctx, [SECTION, Section("next", "Next", after)], {}) is True

    assert ctx.ui.done()
    assert world.calls == [("google_browser", ALL)]


def test_a_replayed_section_runs_no_probe_and_asks_nothing(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", "signin")])
    probes = ctx.probes
    first = Recorder(ctx.ui)
    ctx.ui, ctx.probes = first, first.probes(probes)
    google.run_section(ctx)
    calls = list(world.calls)

    second = Recorder(first.ui, first.log)
    ctx.ui, ctx.probes, ctx.review = second, second.probes(probes), True
    google.run_section(ctx)

    assert world.calls == calls and first.ui.done()
    assert [entry.name for entry in second.log] == [entry.name for entry in first.log]


def test_the_fake_world_can_stand_in_for_one_probe(make_ctx, world):
    own_client()
    ctx = make_ctx([("Sign in to Google?", DEFAULT), ("Try the sign-in again?", "skip")])

    def fail(settings, chosen, announce):
        raise GoogleSetupError("could not listen on 127.0.0.1")

    ctx.probes = dataclasses.replace(ctx.probes, google_browser_signin=fail)
    google.run_section(ctx)

    assert ctx.ui.lines("error") == ["could not listen on 127.0.0.1"]
