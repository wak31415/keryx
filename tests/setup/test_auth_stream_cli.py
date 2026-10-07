"""`keryx auth login NAME --json` on the command line: the stream the Mac app reads.

The sign-ins themselves are `tests/setup/test_auth_stream.py`'s; here, what the command adds
— the refusals as events, the stdin line, Cancel — with the child faked at its factory.
"""

import json

import pytest
from typer.testing import CliRunner

from keryx.cli import app
from keryx.config.store import ConfigStore
from keryx.setup import auth_stream

from .test_auth_stream import SIGN_IN, TOKEN, TOKEN_SHOWN, TYPED, URL, Clock, FakeChild

runner = CliRunner()


def events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


@pytest.fixture
def child(monkeypatch, every_agent_installed):
    """The child `claude setup-token` would be, scripted, in place of a real terminal."""
    made = {}

    def spawn(script):
        def factory(argv, env):
            made["child"] = FakeChild(script, Clock())
            made["argv"] = argv
            return made["child"]

        monkeypatch.setattr(auth_stream, "PtyChild", factory)
        return made

    return spawn


def test_claude_streams_the_link_the_question_and_stores_the_token(child):
    made = child([*SIGN_IN, TYPED, *TOKEN_SHOWN])

    result = runner.invoke(app, ["auth", "login", "claude", "--json"], input="abc#def\n")

    assert result.exit_code == 0, result.output
    assert [event["event"] for event in events(result)] == ["open", "code", "done"]
    assert events(result)[0]["url"] == URL
    assert made["child"].typed == ["abc#def"]
    assert ConfigStore().stored()["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in result.output


def test_cancel_is_the_one_ending_and_the_child_is_stopped(child):
    made = child([*SIGN_IN[:5], KeyboardInterrupt()])

    result = runner.invoke(app, ["auth", "login", "claude", "--json"])

    assert result.exit_code == 130
    assert events(result)[-1] == {"event": "cancelled"}
    assert made["child"].stopped


def test_the_service_is_refused_as_one_event(child, monkeypatch):
    monkeypatch.setenv("KERYX_ACTOR", "service")
    made = child([*SIGN_IN])

    result = runner.invoke(app, ["auth", "login", "claude", "--json"])

    assert result.exit_code == 1
    assert events(result) == [{"event": "error",
                               "message": "only the owner may sign in, at their own terminal"}]
    assert "child" not in made


@pytest.mark.parametrize(
    "argv",
    [["auth", "login", "twitter", "--json"],
     ["auth", "login", "gmail", "--json", "--callback-url", "http://localhost:1/?code=c"]],
    ids=["unknown", "two-step"],
)
def test_a_wrong_command_line_is_one_error_event_and_exit_2(argv):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert [event["event"] for event in events(result)] == ["error"]


def test_an_unexpected_failure_is_still_one_ending(child, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(auth_stream, "stream", broken)

    result = runner.invoke(app, ["auth", "login", "codex", "--json"])

    assert result.exit_code == 1
    assert events(result) == [{"event": "error", "message": "boom"}]
