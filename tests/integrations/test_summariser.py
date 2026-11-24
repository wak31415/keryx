"""The one model call `check_email` and `find_document` share: the bare `claude` CLI.

No network and no real `claude`: a fake runner stands in for the process, except for the
two tests of the runner itself, which run the Python that is running the suite.
"""

import asyncio
import json
import sys

import pytest

from keryx.integrations import summariser
from keryx.integrations.summariser import (
    ClaudeCliSummariser,
    SummaryError,
    build_summariser,
    summariser_problem,
)


class FakeRun:
    def __init__(self, code=0, out=None, err=""):
        self.code, self.err = code, err
        self.out = json.dumps({"result": "Ann needs the numbers."}) if out is None else out
        self.calls: list[tuple[list[str], str, dict]] = []

    async def __call__(self, argv, stdin, env):
        self.calls.append((list(argv), stdin, env))
        return self.code, self.out, self.err


async def test_the_summary_is_one_bare_cli_call_on_the_claude_sign_in(settings):
    settings.anthropic_api_key = "sk-ant-key"
    run = FakeRun()

    text = await ClaudeCliSummariser(
        "/bin/claude", settings, model="claude-opus-5-5", effort="low", run=run
    ).summarise("sys", "mail")

    [(argv, stdin, env)] = run.calls
    assert text == "Ann needs the numbers."
    assert argv[:2] == ["/bin/claude", "-p"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "sys"
    assert stdin == "mail"
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-key"}
    assert "sk-ant-key" not in " ".join(argv)


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(code=1, out="", err="auth failed for sk-ant-key"),
        FakeRun(out=json.dumps({"result": "boom", "is_error": True})),
        FakeRun(out="not json"),
    ],
)
async def test_a_failed_summary_is_an_error_that_never_quotes_the_key(settings, run):
    settings.anthropic_api_key = "sk-ant-key"

    with pytest.raises(SummaryError) as raised:
        await ClaudeCliSummariser(
            "/bin/claude", settings, model="m", effort="low", run=run
        ).summarise("s", "p")

    assert "sk-ant-key" not in raised.value.detail


async def test_the_real_runner_feeds_stdin_and_returns_what_came_out():
    code, out, err = await summariser._run_cli(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hello", {}
    )

    assert (code, out.strip(), err) == (0, "HELLO", "")


async def test_a_cancelled_runner_kills_its_process():
    task = asyncio.create_task(
        summariser._run_cli([sys.executable, "-c", "import time; time.sleep(30)"], "", {})
    )
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# --------------------------------------------------------------------------- build


def with_cli(monkeypatch, cli):
    import dataclasses

    from keryx.agents.registry import BACKENDS

    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent: True)
    spec = dataclasses.replace(BACKENDS["claude"], find_cli=lambda: cli)
    monkeypatch.setitem(BACKENDS, "claude", spec)


def test_without_the_claude_extra_it_says_how_to_get_it(settings, monkeypatch):
    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent: False)

    assert "the claude CLI is not installed" in summariser_problem(settings)


def test_the_extra_without_its_cli_is_the_same_problem(settings, monkeypatch):
    with_cli(monkeypatch, None)

    assert "the claude CLI is not installed" in summariser_problem(settings)


def test_with_the_cli_it_is_built_on_a_plugins_model_and_effort(settings, monkeypatch):
    with_cli(monkeypatch, "/bin/claude")

    assert summariser_problem(settings) is None
    built = build_summariser(settings, model="claude-sonnet-5-5", effort="medium")
    assert (built._cli, built._model, built._effort) == (
        "/bin/claude", "claude-sonnet-5-5", "medium",
    )
