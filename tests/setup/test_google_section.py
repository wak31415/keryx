"""The Google section: one question, one set of instructions, then a sign-in and a read for
each thing asked for."""

import dataclasses
import json
import stat
from urllib.parse import parse_qs, urlparse

from jarvis.agents import registry
from jarvis.config.store import ConfigStore
from jarvis.integrations.gmail import token_path
from jarvis.setup import google
from jarvis.setup.google import GoogleSetupError, install_client_file

from .fakes import DEFAULT

CLIENT = {"installed": {"client_id": "cid", "client_secret": "csecret"}}


def client_file(tmp_path, body=CLIENT):
    path = tmp_path / "Downloads" / "client_secret_123.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(body))
    return path


class Post:
    def __init__(self):
        self.calls = []

    def __call__(self, url, data, timeout):
        self.calls.append(data)
        return type("R", (), {"status_code": 200, "json": lambda self: {"refresh_token": "rt"}})()


def test_skip_is_the_default_and_touches_nothing(make_ctx):
    ctx = make_ctx([("Connect Google", DEFAULT)])

    google.run_section(ctx)

    assert ctx.ui.done() and ConfigStore().stored() == {}


def test_email_answers_end_to_end(make_ctx, world, tmp_path):
    source = client_file(tmp_path)
    post = Post()

    ctx = make_ctx([])
    ctx.probes = dataclasses.replace(ctx.probes, http_post=post)
    ctx.ui.answers = [
        ("Connect Google", "setup"),
        ("What should Jarvis be able to do", [google.EMAIL]),
        ("Path to the downloaded client JSON", f"'{source}'"),
    ]
    original = ctx.ui.text

    def text(message, **kwargs):
        if "address you landed on" in message:
            [panel] = [line for line in ctx.ui.lines("panel") if "Email answers" in line]
            url = panel.splitlines()[1]
            state = parse_qs(urlparse(url).query)["state"][0]
            ctx.ui.asked.append(("text", message))
            return f"http://localhost:1/?state={state}&code=abc&scope={google.SCOPE}"
        return original(message, **kwargs)

    ctx.ui.text = text

    google.run_section(ctx)

    installed = ctx.settings.config_dir / "google_client_secret.json"
    assert json.loads(installed.read_text()) == CLIENT
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"] == str(installed)
    assert post.calls[0]["code"] == "abc"
    assert token_path(ctx.settings).is_file()
    assert ctx.settings.user_google_email == "sam@example.com"
    assert "email answers: signed in as sam@example.com" in ctx.ui.lines("success")


def test_the_guide_is_shown_only_when_there_is_no_client(make_ctx, tmp_path):
    ctx = make_ctx(
        [("Connect Google", "setup"), ("What should", [google.EMAIL]),
         ("Path to the downloaded", "")]
    )

    google.run_section(ctx)

    [guide] = ctx.ui.lines("markdown")
    assert "console.cloud.google.com/projectcreate" in guide
    assert "Publish app" in guide
    assert "Calendar API" not in guide  # only the email box was ticked


def test_a_wrong_client_file_is_refused_and_asked_again(make_ctx, tmp_path):
    bad = client_file(tmp_path, {"nope": 1})
    ctx = make_ctx(
        [("Connect Google", "setup"), ("What should", [google.EMAIL]),
         ("Path to the downloaded", str(bad)), ("Path to the downloaded", "")]
    )

    google.run_section(ctx)

    assert any("not a Google OAuth client file" in line for line in ctx.ui.lines("error"))
    assert not (ctx.settings.config_dir / "google_client_secret.json").exists()


def test_agents_turn_on_workspace_mcp_and_sign_it_in(make_ctx, world, tmp_path):
    source = client_file(tmp_path)
    ctx = make_ctx(
        [
            ("Connect Google", "setup"),
            ("What should", [google.AGENTS]),
            ("Path to the downloaded", str(source)),
            ("Your Google address", "sam@example.com"),
        ]
    )

    google.run_section(ctx)

    assert ctx.settings.google_workspace_mcp is True
    assert ("workspace",) in world.calls
    assert any("ssh -L 8000:localhost:8000" in line for line in ctx.ui.lines("panel"))


def test_codex_preselects_agents_and_no_claude_extra_disables_email(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ConfigStore().set({"AGENT_BACKEND": "codex"})
    ctx = make_ctx([("Connect Google", "setup"), ("What should", [])])

    google.run_section(ctx)

    email, agents = ctx.ui.choices["What should Jarvis be able to do?"]
    assert email.disabled and "uv sync --extra claude" in email.disabled
    assert agents.checked is True


def test_install_client_file_refuses_what_it_cannot_read(settings, tmp_path):
    try:
        install_client_file(settings, tmp_path / "missing.json")
    except GoogleSetupError as error:
        assert "could not read" in str(error)
    else:  # pragma: no cover
        raise AssertionError("no error")
