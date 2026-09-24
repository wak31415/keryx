"""Tests for the backend registry: model names and the runner `serve` builds."""

import pytest

from jarvis.agents.base import FakeAgentRunner
from jarvis.agents.claude import ClaudeAgentRunner
from jarvis.agents.registry import BACKENDS, build_agent_runner, resolve_model
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
