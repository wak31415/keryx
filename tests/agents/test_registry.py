"""Tests for the backend registry: model names and the runner `serve` builds."""

import dataclasses
import subprocess
import sys

import pytest

from jarvis.agents.base import FakeAgentRunner
from jarvis.agents.claude import ClaudeAgentRunner
from jarvis.agents.codex import CodexAgentRunner
from jarvis.agents.registry import (
    BACKENDS,
    agent_for_model,
    auth_status,
    build_agent_runner,
    installed,
    offered_agents,
    ready_backends,
    resolve_model,
    skill_dirs,
)
from jarvis.agents.router import RoutingAgentRunner


@pytest.mark.parametrize(
    ("name", "expected"),
    [("opus", "claude-opus-5-5"), (" Sonnet ", "claude-sonnet-5-5"), ("claude-x-1", "claude-x-1")],
)
def test_a_claude_alias_resolves_and_an_id_passes_through(settings, name, expected):
    assert resolve_model("claude", name, settings) == expected


@pytest.mark.parametrize("name", [None, "", "  "])
def test_no_model_is_the_agents_configured_default(settings, name):
    settings.subagent_model = "claude-sonnet-5"

    assert resolve_model("claude", name, settings) == "claude-sonnet-5"


def test_every_backend_names_itself(settings):
    for name, spec in BACKENDS.items():
        assert spec.name == name
        assert spec.label


def test_serve_gets_one_runner_per_enabled_agent_behind_the_router(settings):
    runner = build_agent_runner(settings)

    assert isinstance(runner, RoutingAgentRunner)
    assert runner.default == "claude"
    assert isinstance(runner.runners["claude"], ClaudeAgentRunner)


def test_fake_agents_is_the_whole_runner(settings):
    settings.fake_agents = True

    assert isinstance(build_agent_runner(settings), FakeAgentRunner)


def test_a_codex_alias_resolves_and_blank_is_codexs_own_default(settings):
    assert resolve_model("codex", "Terra", settings) == "gpt-5.6-terra"
    assert resolve_model("codex", None, settings) == ""
    settings.codex_model = "gpt-5.5"
    assert resolve_model("codex", "", settings) == "gpt-5.5"


def test_an_alias_names_its_agent():
    assert agent_for_model("opus") == "claude"
    assert agent_for_model(" SOL ") == "codex"
    assert agent_for_model("gpt-5.5") is None
    assert agent_for_model(None) is None


def test_no_alias_is_claimed_by_two_agents():
    seen: set[str] = set()
    for spec in BACKENDS.values():
        assert not seen & set(spec.models), "an alias would not say which agent it means"
        seen |= set(spec.models)


def fake_backend(monkeypatch, name, *, cli, login):
    spec = BACKENDS[name]
    monkeypatch.setitem(
        BACKENDS,
        name,
        dataclasses.replace(
            spec,
            find_cli=lambda: "/bin/agent" if cli else None,
            auth=dataclasses.replace(spec.auth, stored_login=lambda: login),
        ),
    )


def test_ready_means_installed_and_signed_in(
    settings, monkeypatch, every_agent_installed
):
    settings.agents_enabled = ["claude", "codex"]
    fake_backend(monkeypatch, "claude", cli=True, login=True)
    fake_backend(monkeypatch, "codex", cli=True, login=False)
    assert ready_backends(settings) == ["claude"]

    fake_backend(monkeypatch, "codex", cli=False, login=True)
    assert ready_backends(settings) == ["claude"]

    fake_backend(monkeypatch, "codex", cli=True, login=True)
    assert ready_backends(settings) == ["claude", "codex"]
    assert auth_status("codex", settings).ready


def test_an_agent_that_is_not_enabled_is_never_ready(settings, monkeypatch):
    fake_backend(monkeypatch, "codex", cli=True, login=True)

    assert "codex" not in ready_backends(settings)


def test_both_agents_enabled_means_a_runner_for_each(settings):
    settings.agents_enabled = ["claude", "codex"]
    settings.agent_backend = "codex"

    runner = build_agent_runner(settings)

    assert runner.default == "codex"
    assert isinstance(runner.runners["codex"], CodexAgentRunner)
    assert isinstance(runner.runners["claude"], ClaudeAgentRunner)


def test_the_default_is_always_offered_and_the_rest_only_when_ready(
    settings, monkeypatch, every_agent_installed
):
    settings.agents_enabled = ["claude", "codex"]
    fake_backend(monkeypatch, "claude", cli=False, login=False)
    fake_backend(monkeypatch, "codex", cli=True, login=False)
    assert offered_agents(settings) == ["claude"]

    fake_backend(monkeypatch, "codex", cli=True, login=True)
    assert offered_agents(settings) == ["claude", "codex"]


def test_fake_agents_offers_every_enabled_agent(settings):
    settings.agents_enabled = ["claude", "codex"]
    settings.fake_agents = True

    assert offered_agents(settings) == ["claude", "codex"]


def test_skills_come_from_every_enabled_agent_the_default_first(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis.agents.registry.codex_home", lambda: tmp_path / "codex")
    settings.agents_enabled = ["claude", "codex"]
    settings.agent_backend = "codex"

    assert skill_dirs(settings) == [tmp_path / "codex" / "skills", settings.skills_dir]


# ------------------------------------------------------------- installed or not

#: The module each backend's extra installs, and so the one whose absence says it is missing.
PACKAGES = {"claude": "claude_agent_sdk", "codex": "openai_codex"}


def uninstall(monkeypatch, *agents):
    """As if `uv sync` had been run without these agents' extras."""
    for agent in agents:
        monkeypatch.setitem(sys.modules, PACKAGES[agent], None)
    if "codex" in agents:
        monkeypatch.setitem(sys.modules, "codex_cli_bin", None)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_an_agent_whose_extra_is_missing_is_not_installed_and_says_how(monkeypatch, agent):
    uninstall(monkeypatch, agent)

    assert not installed(agent)
    assert BACKENDS[agent].find_cli() is None
    assert BACKENDS[agent].install_hint.startswith(f"uv sync --extra {agent} ")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_an_agent_that_is_not_installed_is_never_offered_not_even_as_the_default(
    settings, monkeypatch, agent
):
    """Not the default either: `serve` refuses a default that is not installed."""
    settings.agents_enabled = ["claude", "codex"]
    settings.agent_backend = agent
    fake_backend(monkeypatch, "claude", cli=True, login=True)
    fake_backend(monkeypatch, "codex", cli=True, login=True)
    uninstall(monkeypatch, agent)

    assert agent not in offered_agents(settings)
    assert agent not in ready_backends(settings)


def test_fake_agents_needs_neither_extra(settings, monkeypatch):
    settings.agents_enabled = ["claude", "codex"]
    settings.fake_agents = True
    uninstall(monkeypatch, "claude", "codex")

    assert offered_agents(settings) == ["claude", "codex"]
    assert isinstance(build_agent_runner(settings), FakeAgentRunner)


def test_all_of_jarvis_imports_with_neither_sdk_installed():
    """A machine with one extra, or none, must still import every module: the SDKs are
    only ever imported where an agent actually runs."""
    script = """
import importlib, pkgutil, sys
for name in ("claude_agent_sdk", "openai_codex", "codex_cli_bin"):
    sys.modules[name] = None
import jarvis
for module in pkgutil.walk_packages(jarvis.__path__, "jarvis."):
    if module.name.endswith(("local_audio", "wakeword")):
        continue  # macOS-only, and imported only on macOS
    importlib.import_module(module.name)
from jarvis.agents.registry import BACKENDS, installed
assert not any(installed(name) for name in BACKENDS)
assert all(spec.find_cli() is None for spec in BACKENDS.values())
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
