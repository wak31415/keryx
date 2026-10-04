"""`keryx auth`'s two halves, called directly: every sign-in's state, and one sign-in."""

import asyncio
import dataclasses

import pytest

from keryx.agents import registry
from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.config.store import ConfigStore
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
    monkeypatch.setattr(registry, "installed", lambda agent, settings=None: False)

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


def test_google_workspace_turns_the_mcp_on_and_runs_its_sign_in(settings, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(google, "run_google_setup", lambda s, echo: ran.append(s) or True)
    client = tmp_path / "c.json"
    client.write_text('{"web": {"client_id": "i", "client_secret": "s"}}')

    lines = login("google-workspace", settings, client_file=client)

    assert ConfigStore().stored()["GOOGLE_WORKSPACE_MCP"] is True
    assert ran and ran[0].google_oauth_client() == ("i", "s")
    assert any("Google client saved" in line for line in lines)


def test_a_bad_client_file_is_an_auth_error(settings, tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{}")

    with pytest.raises(AuthError, match="not a Google OAuth client file"):
        login("gmail", settings, client_file=bad)


def test_a_google_sign_in_that_fails_is_an_auth_error(settings, monkeypatch):
    def refuse(settings, echo):
        raise google.GoogleSetupError("the server refused")

    monkeypatch.setattr(google, "run_google_setup", refuse)

    with pytest.raises(AuthError, match="the server refused"):
        login("google-workspace", settings)


def test_nothing_else_can_be_signed_in_to(settings):
    with pytest.raises(AuthError, match="one of"):
        login("myspace", settings)


def test_the_gmail_address_is_read_from_the_profile(settings, monkeypatch):
    class Gmail:
        def __init__(self, path):
            self.path = path

        async def get(self, path):
            assert path == "profile"
            return {"emailAddress": "ada@example.com"}

    monkeypatch.setattr(google, "HttpGmail", Gmail)

    assert asyncio.run(google.gmail_address(settings)) == "ada@example.com"


def test_a_gmail_that_will_not_answer_is_a_setup_error(settings, monkeypatch):
    class Gmail:
        def __init__(self, path):
            pass

        async def get(self, path):
            raise RuntimeError("signed_out")

    monkeypatch.setattr(google, "HttpGmail", Gmail)

    with pytest.raises(google.GoogleSetupError, match="would not answer"):
        asyncio.run(google.gmail_address(settings))
