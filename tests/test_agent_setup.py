"""Tests for `jarvis setup-agent`: no terminal, no login, no subagent.

Every agent's CLI and stored login are faked through the registry, the login command and
the smoke task are injected, and the terminal is a script of answers.
"""

import dataclasses
import json

import pytest

from jarvis import agent_setup
from jarvis.agent_setup import (
    SMOKE_PROMPT,
    STATUSES,
    SetupError,
    exit_code,
    passed_smoke,
    run_setup_agent,
)
from jarvis.agents.base import AgentOpenError, FakeAgentRunner, RunResult
from jarvis.agents.registry import BACKENDS

READY = RunResult(ok=True, final_text="SPOKEN_SUMMARY: ready", spoken_summary="ready")


class Machine:
    """Which agents are installed and signed in; what their logins and smoke tests do."""

    def __init__(self, monkeypatch, **agents):
        self.monkeypatch = monkeypatch
        self.logins: list[tuple[str, ...]] = []
        self.smoked: list[str] = []
        self.smoke_results: dict[str, RunResult] = {}
        self.login_fixes: set[str] = set()
        self.signed_in = {name: login for name, (_, login) in agents.items()}
        for name, (cli, _) in agents.items():
            spec = BACKENDS[name]
            monkeypatch.setitem(
                BACKENDS,
                name,
                dataclasses.replace(
                    spec,
                    find_cli=(lambda cli=cli: cli),
                    auth=dataclasses.replace(
                        spec.auth, stored_login=(lambda name=name: self.signed_in[name])
                    ),
                ),
            )

    def run_login(self, argv) -> int:
        self.logins.append(tuple(argv))
        agent = argv[0]
        if agent in self.login_fixes:
            self.signed_in[agent] = True
        return 0

    async def smoke(self, settings, agent) -> RunResult:
        self.smoked.append(agent)
        return self.smoke_results.get(agent, READY)


def script(*answers):
    """An `ask` that answers in order, and records what it was asked."""
    asked: list[str] = []
    remaining = list(answers)

    def ask(text, preset):
        asked.append(text)
        return remaining.pop(0) if remaining else preset

    ask.asked = asked
    return ask


def setup(settings, machine, *, ask=None, confirm=None, **kw):
    lines: list[str] = []
    code = run_setup_agent(
        settings,
        echo=lines.append,
        ask=ask or script(),
        confirm=confirm or (lambda text, preset: preset),
        run_login=machine.run_login,
        smoke_test=machine.smoke,
        headless=kw.pop("headless", False),
        **kw,
    )
    return code, "\n".join(lines)


@pytest.fixture
def both_ready(monkeypatch):
    return Machine(monkeypatch, claude=("/bin/claude", True), codex=("/bin/codex", True))


# --------------------------------------------------------------------- the happy path


def test_one_ready_agent_is_preselected_and_proved(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", False), codex=("/bin/codex", True))

    code, out = setup(settings, machine, yes=True)

    assert code == 0
    assert machine.smoked == ["codex"]
    assert "    AGENT_BACKEND=codex" in out
    assert "    AGENTS_ENABLED=" in out
    assert "    CODEX_MODEL=" in out
    assert out.endswith(f"ready: {STATUSES['ready']}")


def test_the_table_shows_every_agent_and_how_it_signs_in(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", True), codex=(None, False))

    _, out = setup(settings, machine, yes=True, smoke=False)

    table = out.splitlines()[:3]
    assert table[0].split() == ["AGENT", "CLI", "SIGN-IN"]
    assert table[1].split()[:2] == ["claude", "/bin/claude"]
    assert "stored subscription login" in table[1]
    assert table[2].split()[:3] == ["codex", "not", "installed"]
    assert table[2].endswith("not signed in")


def test_asking_picks_the_default_and_offers_the_others(settings, both_ready):
    ask = script("codex")
    offered: list[str] = []

    def confirm(text, preset):
        offered.append(text)
        return True

    code, out = setup(settings, both_ready, ask=ask, confirm=confirm)

    assert code == 0
    assert "which agent should do the work by default?" in ask.asked[0]
    assert offered == ["also enable claude?"]
    assert "    AGENT_BACKEND=codex" in out
    assert "    AGENTS_ENABLED=codex,claude" in out
    assert both_ready.smoked == ["codex", "claude"]


def test_flags_answer_every_question(settings, both_ready):
    ask = script()

    code, out = setup(settings, both_ready, ask=ask, default="claude", enable=["codex"])

    assert code == 0
    assert ask.asked == []
    assert "    AGENTS_ENABLED=claude,codex" in out


def test_no_smoke_runs_nothing(settings, both_ready):
    code, _ = setup(settings, both_ready, yes=True, smoke=False)

    assert (code, both_ready.smoked) == (0, [])


def test_already_enabled_agents_stay_enabled_under_yes(settings, both_ready):
    settings.agents_enabled = ["claude", "codex"]

    _, out = setup(settings, both_ready, yes=True, smoke=False)

    assert "    AGENTS_ENABLED=claude,codex" in out


# ------------------------------------------------------------------------ signing in


def test_a_missing_subscription_login_runs_the_agents_own_login(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", True), codex=("/bin/codex", False))
    machine.login_fixes.add("codex")

    code, out = setup(settings, machine, ask=script("codex", "s"))

    assert machine.logins == [("codex", "login")]
    assert "running `codex login`" in out
    assert code == 0 and machine.smoked == ["codex", "claude"]


def test_a_headless_machine_gets_the_device_code_login(settings, monkeypatch):
    machine = Machine(monkeypatch, codex=("/bin/codex", False), claude=("/bin/claude", True))

    setup(settings, machine, ask=script("codex", "s"), confirm=lambda t, p: False, headless=True)

    assert machine.logins == [("codex", "login", "--device-auth")]


def test_a_headless_claude_is_told_where_the_token_goes(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", False), codex=(None, False))

    code, out = setup(settings, machine, ask=script("claude", "s"), headless=True)

    assert machine.logins == [("claude", "setup-token")]
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in out
    assert code == 1  # still no login it can see: the token is theirs to paste


def test_a_login_that_fails_says_so(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", False), codex=(None, False))
    machine.run_login = lambda argv: 3

    code, out = setup(settings, machine, ask=script("claude", "s"))

    assert "exited with 3" in out
    assert code == 1


def test_an_api_key_is_a_line_to_add_never_a_file_to_edit(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    machine = Machine(monkeypatch, claude=("/bin/claude", False), codex=(None, False))

    code, out = setup(settings, machine, ask=script("claude", "k"))

    assert "    ANTHROPIC_API_KEY=" in out
    assert machine.logins == []
    assert not (tmp_path / ".env").exists()
    assert code == 1


def test_a_missing_cli_is_an_install_command_never_an_install(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", True), codex=(None, False))

    code, out = setup(settings, machine, default="codex")

    assert "uv sync (the openai-codex SDK bundles the codex CLI)" in out
    assert machine.logins == []
    assert code == 1
    assert out.endswith(f"not_ready: {STATUSES['not_ready']}")


def test_yes_signs_nothing_in(settings, monkeypatch):
    machine = Machine(monkeypatch, claude=("/bin/claude", False), codex=(None, False))

    code, _ = setup(settings, machine, yes=True)

    assert (code, machine.logins, machine.smoked) == (1, [], [])


# ----------------------------------------------------------------------- smoke tests


def test_a_smoke_test_that_fails_is_exit_1_and_says_why(settings, both_ready):
    both_ready.smoke_results["claude"] = RunResult(ok=False, error="401 Unauthorized")

    code, out = setup(settings, both_ready, yes=True)

    assert code == 1
    assert "claude: FAILED — 401 Unauthorized" in out
    assert out.endswith(f"smoke_failed: {STATUSES['smoke_failed']}")


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

    result = agent_setup.asyncio.run(agent_setup.run_smoke(settings, "codex"))

    assert passed_smoke(result)
    [(task, resume)] = runner.opened
    assert (task.agent, resume) == ("codex", None)
    assert runner.sessions[0].prompts == [SMOKE_PROMPT]
    assert runner.sessions[0].closed is True


def test_a_smoke_task_that_cannot_even_open_is_a_failure_that_says_why(settings, monkeypatch):
    class Refusing:
        async def open(self, task, *, resume=None):
            raise AgentOpenError("RuntimeError: codex refused CODEX_API_KEY: bad key")

    spec = BACKENDS["codex"]
    monkeypatch.setitem(
        BACKENDS, "codex", dataclasses.replace(spec, make_runner=lambda settings: Refusing())
    )

    result = agent_setup.asyncio.run(agent_setup.run_smoke(settings, "codex"))

    assert result.ok is False
    assert result.error == "RuntimeError: codex refused CODEX_API_KEY: bad key"


def test_a_smoke_task_that_hangs_is_a_failure(settings, monkeypatch):
    monkeypatch.setattr(agent_setup, "SMOKE_TIMEOUT_S", 0.01)
    runner = FakeAgentRunner(delay_s=5)
    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS, "claude", dataclasses.replace(spec, make_runner=lambda settings: runner)
    )

    result = agent_setup.asyncio.run(agent_setup.run_smoke(settings, "claude"))

    assert result.ok is False
    assert "no answer within" in result.error


# ------------------------------------------------------------------------------ json


def test_json_is_one_document_and_nothing_else(settings, both_ready):
    lines: list[str] = []

    code = run_setup_agent(
        settings,
        yes=True,
        as_json=True,
        echo=lines.append,
        ask=script(),
        confirm=lambda t, p: p,
        run_login=both_ready.run_login,
        smoke_test=both_ready.smoke,
        enable=["codex"],
    )

    [document] = lines
    report = json.loads(document)
    assert code == 0
    assert report["status"] == "ready"
    assert (report["default"], report["enabled"]) == ("claude", ["claude", "codex"])
    codex = next(agent for agent in report["agents"] if agent["name"] == "codex")
    assert codex["ready"] is True and codex["smoke"] == {
        "ok": True,
        "summary": "ready",
        "error": None,
    }
    assert codex["instructions_file"].endswith("AGENTS.md")
    assert report["env_lines"] == [
        "AGENT_BACKEND=claude",
        "AGENTS_ENABLED=claude,codex",
        "CODEX_MODEL=",
    ]
    assert any("GOOGLE_WORKSPACE_MCP=true" in note for note in report["notes"])


def test_json_names_no_credential(settings, both_ready):
    settings.codex_api_key = "sk-codex-secret"
    lines: list[str] = []

    run_setup_agent(
        settings,
        yes=True,
        as_json=True,
        echo=lines.append,
        ask=script(),
        confirm=lambda t, p: p,
        run_login=both_ready.run_login,
        smoke_test=both_ready.smoke,
        enable=["codex"],
    )

    assert "sk-codex-secret" not in lines[0]
    assert '"auth": "api_key"' in lines[0]


def test_an_agent_nobody_has_heard_of_is_a_wrong_command_line(settings, both_ready):
    with pytest.raises(SetupError, match="gemini"):
        setup(settings, both_ready, default="gemini")
    with pytest.raises(SetupError, match="gemini"):
        setup(settings, both_ready, enable=["gemini"])
    with pytest.raises(SetupError, match="gemini"):
        setup(settings, both_ready, ask=script("gemini"))


def test_only_ready_is_success():
    assert [exit_code(status) for status in STATUSES] == [0, 1, 1]


def test_codex_carries_its_parity_notes_and_claude_alone_carries_none(settings, both_ready):
    _, alone = setup(settings, both_ready, yes=True, smoke=False)
    settings.google_workspace_mcp = True
    _, with_google = setup(settings, both_ready, yes=True, smoke=False, enable=["codex"])

    assert "note:" not in alone
    assert "Gmail" not in with_google
    assert "approval bridge" in with_google


def test_headlessness_is_a_display_on_linux(monkeypatch):
    monkeypatch.setattr(agent_setup.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert agent_setup.is_headless() is True

    monkeypatch.setenv("DISPLAY", ":0")
    assert agent_setup.is_headless() is False

    monkeypatch.setattr(agent_setup.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY")
    assert agent_setup.is_headless() is False


def test_the_login_command_runs_on_this_terminal(monkeypatch):
    ran: list[list[str]] = []

    def run(argv, check):
        ran.append(argv)
        return agent_setup.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(agent_setup.subprocess, "run", run)
    assert agent_setup.run_login_command(("codex", "login")) == 0
    assert ran == [["codex", "login"]]

    def missing(argv, check):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(agent_setup.subprocess, "run", missing)
    assert agent_setup.run_login_command(("codex", "login")) == 127
