"""Tests for the backend registry: model names and the runner `serve` builds."""

import dataclasses

import pytest

from jarvis.agents.base import FakeAgentRunner
from jarvis.agents.claude import ClaudeAgentRunner
from jarvis.agents.codex import CodexAgentRunner
from jarvis.agents.registry import (
    BACKENDS,
    agent_for_model,
    auth_status,
    build_agent_runner,
    offered_agents,
    ready_backends,
    resolve_model,
    skill_dirs,
)
from jarvis.agents.router import RoutingAgentRunner


@pytest.mark.parametrize(
    ("name", "expected"),
    [("opus", "claude-opus-5"), (" Sonnet ", "claude-sonnet-5"), ("claude-x-1", "claude-x-1")],
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


def test_ready_means_installed_and_signed_in(settings, monkeypatch):
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


def test_the_default_is_always_offered_and_the_rest_only_when_ready(settings, monkeypatch):
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
