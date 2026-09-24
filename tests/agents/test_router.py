"""Tests for the router: each task opens on the agent it was dispatched to."""

import pytest

from jarvis.agents.base import FakeAgentRunner
from jarvis.agents.router import AgentNotEnabledError, RoutingAgentRunner
from jarvis.tasks.models import Task, TaskKind


def make_task(**overrides) -> Task:
    values = {"id": 3, "kind": TaskKind.AGENT, "description": "check the build"}
    values.update(overrides)
    return Task(**values)


async def test_a_task_opens_on_its_own_agent():
    claude, codex = FakeAgentRunner(), FakeAgentRunner()
    router = RoutingAgentRunner({"claude": claude, "codex": codex}, default="claude")

    await router.open(make_task(agent="codex"))

    assert len(codex.opened) == 1
    assert claude.opened == []


async def test_a_resume_goes_to_the_agent_that_issued_the_session():
    """The default can change between runs; a task's follow-up must not follow it."""
    claude, codex = FakeAgentRunner(), FakeAgentRunner()
    router = RoutingAgentRunner({"claude": claude, "codex": codex}, default="codex")
    task = make_task(agent="claude")

    await router.open(task, resume="sess-1")

    assert claude.opened == [(task, "sess-1")]
    assert codex.opened == []


async def test_a_task_with_no_agent_recorded_takes_the_default():
    claude = FakeAgentRunner()
    router = RoutingAgentRunner({"claude": claude}, default="claude")

    await router.open(make_task(agent=""))

    assert len(claude.opened) == 1


async def test_an_agent_that_is_not_enabled_raises_rather_than_falling_back():
    """Falling back would silently drop the task's session on another agent."""
    router = RoutingAgentRunner({"claude": FakeAgentRunner()}, default="claude")

    with pytest.raises(AgentNotEnabledError, match="codex"):
        await router.open(make_task(agent="codex"))


def test_the_default_must_have_a_runner():
    with pytest.raises(ValueError, match="default"):
        RoutingAgentRunner({"claude": FakeAgentRunner()}, default="codex")
