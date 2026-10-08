"""`keryx auth smoke NAME`: the smoke test `auth status --smoke` runs, for one agent, told as
it runs — JSON lines for the Mac app, or lines of text at a terminal. The agent is faked at
`setup.agents.run_smoke`, which the stream looks up when it runs."""

import dataclasses
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from wording import for_the_app

from keryx.agents import registry
from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.cli import app
from keryx.config.store import ConfigStore
from keryx.setup import smoke_stream

runner = CliRunner()
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def signed_in(monkeypatch, every_agent_installed):
    """Claude and Codex both signed in with a stored login."""
    for name in ("claude", "codex"):
        spec = BACKENDS[name]
        monkeypatch.setitem(BACKENDS, name, dataclasses.replace(
            spec, auth=dataclasses.replace(spec.auth, stored_login=lambda: True)))


class Agent:
    """`setup.agents.run_smoke`, faked: progress, then `result`; who it was asked of."""

    def __init__(self, result: RunResult, progress=("SPOKEN_SUMMARY: ready",)) -> None:
        self.result, self.progress = result, list(progress)
        self.asked: list[str] = []

    async def __call__(self, settings, agent, *, on_progress=None):
        self.asked.append(agent)
        for line in self.progress:
            on_progress(line)
        return self.result


def smoke(monkeypatch, result: RunResult, **kwargs) -> Agent:
    agent = Agent(result, **kwargs)
    monkeypatch.setattr("keryx.setup.agents.run_smoke", agent)
    return agent


def streamed(*argv: str):
    result = runner.invoke(app, ["auth", "smoke", *argv, "--json"])
    return [json.loads(line) for line in result.stdout.splitlines()], result.exit_code


def test_a_smoke_test_that_passes_is_done_and_ok(signed_in, monkeypatch):
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("claude")

    assert agent.asked == ["claude"]
    assert events == [
        {"event": "step", "step": "dispatch", "message": "Asking Claude to run a test task.",
         "detail": None},
        {"event": "step", "step": "answering", "message": "Claude is answering.",
         "detail": None},
        {"event": "done", "ok": True,
         "summary": "Claude ran a test task and said it's ready.", "detail": None},
    ]
    assert code == 0


def test_answering_is_said_once_however_much_it_says(signed_in, monkeypatch):
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"),
          progress=("thinking", "still thinking", "SPOKEN_SUMMARY: ready"))

    events, _ = streamed("codex")

    assert [e.get("step") for e in events] == ["dispatch", "answering", None]


def test_an_agent_that_answers_otherwise_is_done_but_not_ok(signed_in, monkeypatch):
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="Hello there!"))

    events, code = streamed("codex")

    assert events[-1] == {"event": "done", "ok": False,
                          "summary": "Codex ran a test task but didn't answer as asked.",
                          "detail": "It said: Hello there!"}
    assert code == 1


def test_an_agent_that_cannot_run_is_done_but_not_ok_its_error_redacted(
    signed_in, monkeypatch
):
    ConfigStore().set({"ANTHROPIC_API_KEY": SECRET})
    smoke(monkeypatch, RunResult(ok=False, error=f"401: the key {SECRET} was refused"),
          progress=())

    events, code = streamed("claude")

    assert [e["event"] for e in events] == ["step", "done"]
    assert events[-1]["ok"] is False
    assert events[-1]["summary"] == "Claude couldn't run a test task."
    assert SECRET not in events[-1]["detail"] and "was refused" in events[-1]["detail"]
    assert code == 1


def test_a_long_answer_is_clipped_in_the_detail(signed_in, monkeypatch):
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="x" * 5000), progress=())

    events, _ = streamed("claude")

    assert len(events[-1]["detail"]) <= smoke_stream.MAX_DETAIL_CHARS


def test_an_agent_that_is_not_one_is_refused(signed_in, monkeypatch):
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("gemini")

    assert events == [{"event": "error", "message": "There's no coding agent called gemini.",
                       "detail": "one of: claude, codex, local"}]
    assert code == 2 and agent.asked == []


def test_an_agent_not_installed_is_refused_with_how_to_install_it(signed_in, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent, settings=None: agent != "codex")
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("codex")

    assert events == [{"event": "error",
                       "message": "Codex isn't installed in this copy of Keryx.",
                       "detail": "uv sync --extra codex"}]
    assert code == 1 and agent.asked == []


def test_an_agent_without_its_cli_is_refused(signed_in, monkeypatch):
    monkeypatch.setitem(BACKENDS, "codex", dataclasses.replace(BACKENDS["codex"],
                                                              find_cli=lambda: None))
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("codex")

    assert events[-1]["message"] == "Codex's command-line tool is missing."
    assert events[-1]["detail"] == BACKENDS["codex"].install_hint
    assert code == 1


def test_an_agent_not_signed_in_is_refused(every_agent_installed, monkeypatch):
    spec = BACKENDS["claude"]
    monkeypatch.setitem(BACKENDS, "claude", dataclasses.replace(
        spec, auth=dataclasses.replace(spec.auth, stored_login=lambda: False)))
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("claude")

    assert [e["event"] for e in events] == ["error"]
    assert events[0]["message"] == "Claude isn't signed in yet."
    assert events[0]["detail"]  # the terminal's sentence: which tiers it looked for
    assert code == 1 and agent.asked == []


def test_a_local_model_with_no_server_is_refused_in_its_own_words(
    every_agent_installed, monkeypatch
):
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("local")

    assert events[-1]["message"] == "The local model has no server to run on yet."
    assert code == 1


def test_a_local_model_with_a_server_is_asked(every_agent_installed, monkeypatch):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": "qwen3-coder"})
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("local")

    assert agent.asked == ["local"]
    assert events[0]["message"] == "Asking the local model to run a test task."
    assert events[-1]["summary"] == "The local model ran a test task and said it's ready."
    assert code == 0


def test_a_cancel_stops_it_and_says_so(signed_in, monkeypatch):
    async def interrupted(settings, agent, *, on_progress=None):
        raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into

    monkeypatch.setattr("keryx.setup.agents.run_smoke", interrupted)

    events, code = streamed("claude")

    assert [e["event"] for e in events] == ["step", "cancelled"]
    assert code == 130


def test_a_migration_still_to_run_is_refused(signed_in, monkeypatch):
    old = Path.home() / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")
    agent = smoke(monkeypatch, RunResult(ok=True, spoken_summary="ready"))

    events, code = streamed("claude")

    assert [e["event"] for e in events] == ["error"] and code == 2
    assert agent.asked == []


def test_a_cancel_landing_after_a_failed_ending_still_exits_1():
    lines: list[str] = []
    events = smoke_stream.SmokeEvents(lines.append)

    def run() -> int:
        events.done(False, "Claude couldn't run a test task.", detail="quota")
        raise KeyboardInterrupt  # held off while the ending was written, landing now

    assert events.guard(run) == 1
    assert [json.loads(line)["event"] for line in lines] == ["done"]


def test_a_cancel_landing_after_a_passing_ending_exits_0():
    events = smoke_stream.SmokeEvents(lambda line: None)

    def run() -> int:
        events.done(True, "Claude ran a test task and said it's ready.")
        raise KeyboardInterrupt

    assert events.guard(run) == 0


def test_at_a_terminal_it_says_each_step_and_how_it_went(signed_in, monkeypatch):
    smoke(monkeypatch, RunResult(ok=True, spoken_summary="Hello"))

    result = runner.invoke(app, ["auth", "smoke", "claude"])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "Asking Claude to run a test task.",
        "Claude is answering.",
        "Claude ran a test task but didn't answer as asked.",
        "It said: Hello",
    ]


def test_at_a_terminal_a_refusal_and_a_cancel_go_to_stderr(signed_in, monkeypatch):
    async def interrupted(settings, agent, *, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr("keryx.setup.agents.run_smoke", interrupted)

    refused = runner.invoke(app, ["auth", "smoke", "gemini"])
    stopped = runner.invoke(app, ["auth", "smoke", "claude"])

    assert refused.exit_code == 2 and refused.stdout == ""
    assert refused.stderr.splitlines() == ["There's no coding agent called gemini.",
                                           "one of: claude, codex, local"]
    assert stopped.exit_code == 130
    assert (stopped.stdout, stopped.stderr) == ("Asking Claude to run a test task.\n",
                                                "stopped\n")


@pytest.mark.parametrize("message", [
    *(template.format(name="Claude") for template in (
        smoke_stream.DISPATCH, smoke_stream.ANSWERING, smoke_stream.PASSED,
        smoke_stream.UNANSWERED, smoke_stream.UNRUN, smoke_stream.NOT_INSTALLED,
        smoke_stream.NO_CLI, smoke_stream.NOT_SIGNED_IN, smoke_stream.UNKNOWN)),
    smoke_stream.NO_SERVER,
])
def test_every_sentence_is_for_a_person(message):
    assert for_the_app(message)
