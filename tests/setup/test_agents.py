"""The "Coding agents" section and the pieces `jarvis auth` shares with it: no terminal, no
login, no subagent. Every agent's CLI and stored login are faked through the registry."""

import asyncio
import dataclasses
import subprocess

import pytest

from jarvis.agents import registry
from jarvis.agents.base import AgentOpenError, FakeAgentRunner, RunResult
from jarvis.agents.registry import BACKENDS
from jarvis.config.store import ConfigStore
from jarvis.setup import agents
from jarvis.setup.agents import SMOKE_PROMPT, detect, login_argv, passed_smoke
from jarvis.setup.context import run_command

from .fakes import DEFAULT

READY = RunResult(ok=True, final_text="SPOKEN_SUMMARY: ready", spoken_summary="ready")


def sign_in(monkeypatch, **logins: bool) -> dict[str, bool]:
    """Which agents have a stored subscription login; the dict can be flipped later."""
    state = dict(logins)
    for name in logins:
        spec = BACKENDS[name]
        monkeypatch.setitem(
            BACKENDS,
            name,
            dataclasses.replace(
                spec, auth=dataclasses.replace(spec.auth, stored_login=lambda n=name: state[n])
            ),
        )
    return state


def not_installed(monkeypatch, name):
    original = registry.installed
    monkeypatch.setattr(registry, "installed", lambda agent: agent != name and original(agent))


# --- the section ----------------------------------------------------------------------


def test_an_agent_already_signed_in_is_never_asked_how_to_pay(make_ctx, world, monkeypatch):
    sign_in(monkeypatch, claude=True, codex=False)
    ctx = make_ctx([("Which agents", ["claude"])])

    agents.run_section(ctx)

    assert ctx.ui.done()
    assert not [kind for kind, message in ctx.ui.asked if "sign in" in message]
    assert ("smoke", "claude") in world.calls
    assert ConfigStore().stored() == {"AGENT_BACKEND": "claude", "AGENTS_ENABLED": []}
    assert any("ran a task" in line for line in ctx.ui.lines("success"))


def test_two_agents_ask_which_is_the_default_and_both_are_enabled(make_ctx, monkeypatch):
    sign_in(monkeypatch, claude=True, codex=True)
    ctx = make_ctx(
        [("Which agents", ["claude", "codex"]), ("does the work when you do not name", "codex")]
    )

    agents.run_section(ctx)

    stored = ConfigStore().stored()
    assert stored["AGENT_BACKEND"] == "codex"
    assert stored["AGENTS_ENABLED"] == ["claude", "codex"]
    assert any("Codex has no claude.ai connectors" in line for line in ctx.ui.lines("note"))


def test_a_subscription_runs_the_agents_own_login(make_ctx, world, monkeypatch):
    state = sign_in(monkeypatch, claude=False, codex=False)
    world_login = world.probes()

    def login(argv):
        world.calls.append(("login", list(argv)))
        state["codex"] = True
        return 0

    ctx = make_ctx(
        [("Which agents", ["codex"]), ("How should Codex sign in", "subscription")]
    )
    ctx.probes = dataclasses.replace(world_login, run_login=login)

    agents.run_section(ctx)

    assert ("login", ["/venv/bin/codex", "login"]) in world.calls
    assert any("Codex: stored subscription login" in line for line in ctx.ui.lines("success"))


def test_a_headless_machine_gets_the_device_code_login(make_ctx, world, monkeypatch):
    sign_in(monkeypatch, codex=False, claude=False)
    world.headless = True
    ctx = make_ctx([("Which agents", ["codex"]), ("How should Codex sign in", "subscription")])

    agents.run_section(ctx)

    assert ("login", ["/venv/bin/codex", "login", "--device-auth"]) in world.calls
    assert any("still not signed in" in line for line in ctx.ui.lines("error"))


def test_a_headless_claude_login_asks_for_the_token_it_printed(make_ctx, world, monkeypatch):
    sign_in(monkeypatch, claude=False, codex=False)
    world.headless = True
    ctx = make_ctx(
        [
            ("Which agents", ["claude"]),
            ("How should Claude Code sign in", "subscription"),
            ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-token"),
        ]
    )

    agents.run_section(ctx)

    assert ("login", ["/venv/bin/claude", "setup-token"]) in world.calls
    assert ConfigStore()._secrets() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-token"}
    assert ("smoke", "claude") in world.calls


def test_an_api_key_is_asked_hidden_and_stored_as_a_secret(make_ctx, monkeypatch):
    sign_in(monkeypatch, claude=False, codex=False)
    ctx = make_ctx(
        [("Which agents", ["claude"]), ("How should Claude Code sign in", "api_key"),
         ("ANTHROPIC_API_KEY", "sk-ant-key")]
    )

    agents.run_section(ctx)

    assert ("secret", "ANTHROPIC_API_KEY") in ctx.ui.asked
    assert ConfigStore()._secrets() == {"ANTHROPIC_API_KEY": "sk-ant-key"}
    assert "sk-ant-key" not in " ".join(ctx.ui.lines())


def test_a_failed_smoke_test_says_why(make_ctx, world, monkeypatch):
    sign_in(monkeypatch, claude=True)
    world.smoke_result = RunResult(ok=False, error="401 from the API")
    ctx = make_ctx([("Which agents", DEFAULT)])

    agents.run_section(ctx)

    assert any("401 from the API" in line for line in ctx.ui.lines("error"))


def test_an_agent_that_is_not_installed_cannot_be_picked_and_says_how(make_ctx, monkeypatch):
    sign_in(monkeypatch, claude=True)
    not_installed(monkeypatch, "codex")
    ctx = make_ctx([("Which agents", DEFAULT)])

    agents.run_section(ctx)

    [codex] = [c for c in ctx.ui.choices["Which agents should Jarvis use?"] if c.value == "codex"]
    assert codex.disabled == "not installed: uv sync --extra codex"
    assert any("uv sync --extra codex" in line for line in ctx.ui.lines("table"))


def test_no_agent_installed_at_all_stops_with_the_install_command(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: False)
    ctx = make_ctx([])

    agents.run_section(ctx)

    assert any("No coding agent is installed" in line for line in ctx.ui.lines("error"))
    assert ConfigStore().stored() == {}


def test_reviewing_offers_to_keep_the_sign_in_there_is(make_ctx, monkeypatch):
    sign_in(monkeypatch, claude=True)
    ctx = make_ctx(
        [("Which agents", ["claude"]), ("How should Claude Code sign in", "keep")], review=True
    )

    agents.run_section(ctx)

    assert ctx.ui.done()


# --- the pieces ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "passed"),
    [
        (RunResult(ok=True, spoken_summary="ready"), True),
        (RunResult(ok=True, spoken_summary="Ready."), True),
        (RunResult(ok=True, spoken_summary="I am ready to help!"), False),
        (RunResult(ok=False, spoken_summary="ready"), False),
    ],
)
def test_a_smoke_test_passes_only_on_the_answer_it_asked_for(result, passed):
    assert passed_smoke(result) is passed


def test_the_real_smoke_task_goes_through_the_agents_runner(settings, monkeypatch):
    runner = FakeAgentRunner([READY])
    spec = BACKENDS["codex"]
    monkeypatch.setitem(
        BACKENDS, "codex", dataclasses.replace(spec, make_runner=lambda settings: runner)
    )

    result = asyncio.run(agents.run_smoke(settings, "codex"))

    assert passed_smoke(result)
    [(task, resume)] = runner.opened
    assert (task.agent, resume) == ("codex", None)
    assert runner.sessions[0].prompts == [SMOKE_PROMPT]
    assert runner.sessions[0].closed is True


def test_a_task_that_cannot_even_open_is_a_failure_that_says_why(settings, monkeypatch):
    class Refusing:
        async def open(self, task, *, resume=None):
            raise AgentOpenError("RuntimeError: codex refused CODEX_API_KEY: bad key")

    spec = BACKENDS["codex"]
    monkeypatch.setitem(
        BACKENDS, "codex", dataclasses.replace(spec, make_runner=lambda settings: Refusing())
    )

    result = asyncio.run(agents.run_smoke(settings, "codex"))

    assert result.ok is False
    assert result.error == "RuntimeError: codex refused CODEX_API_KEY: bad key"


def test_a_task_that_hangs_is_a_failure(settings, monkeypatch):
    runner = FakeAgentRunner(delay_s=5)
    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS, "claude", dataclasses.replace(spec, make_runner=lambda settings: runner)
    )

    result = asyncio.run(agents.run_task(settings, "claude", "hi", timeout_s=0.01))

    assert result.ok is False
    assert "no answer within" in result.error


def test_detect_reports_every_agent_without_its_credential(settings, every_agent_installed):
    settings = settings.model_copy(update={"anthropic_api_key": "sk-ant-secret"})

    states = detect(settings)

    assert set(states) == set(BACKENDS)
    as_dict = states["claude"].as_dict()
    assert as_dict["ready"] is True and as_dict["auth"] == "api_key"
    assert "sk-ant-secret" not in str(as_dict)


def test_the_login_runs_the_cli_that_was_found(every_agent_installed):
    assert login_argv("claude", "/venv/bin/claude", headless=False) == [
        "/venv/bin/claude",
        "/login",
    ]
    assert login_argv("claude", "/venv/bin/claude", headless=True) == [
        "/venv/bin/claude",
        "setup-token",
    ]


def test_headlessness_is_a_display_on_linux(monkeypatch):
    monkeypatch.setattr(agents.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert agents.is_headless() is True

    monkeypatch.setenv("DISPLAY", ":0")
    assert agents.is_headless() is False

    monkeypatch.setattr(agents.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY")
    assert agents.is_headless() is False


def test_a_command_runs_on_this_terminal(monkeypatch):
    ran: list[list[str]] = []

    def run(argv, check):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("jarvis.setup.context.subprocess.run", run)
    assert run_command(("codex", "login")) == 0
    assert ran == [["codex", "login"]]

    def missing(argv, check):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("jarvis.setup.context.subprocess.run", missing)
    assert run_command(("codex", "login")) == 127


def test_a_setup_task_runs_as_the_service_and_puts_the_actor_back(settings, monkeypatch):
    import os

    seen = []

    class Recording(FakeAgentRunner):
        async def open(self, task, *, resume=None):
            seen.append(os.environ.get("JARVIS_ACTOR"))
            return await super().open(task, resume=resume)

    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS, "claude", dataclasses.replace(spec, make_runner=lambda s: Recording([READY]))
    )

    asyncio.run(agents.run_task(settings, "claude", "hi"))

    assert seen == ["service"]
    assert "JARVIS_ACTOR" not in os.environ
