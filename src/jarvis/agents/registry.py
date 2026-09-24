"""Every coding agent Jarvis knows, as data: `BACKENDS`.

A backend is a name, a runner factory, and the model names that can be said for it out
loud. Adding a third agent is one module under `jarvis/agents/` and one entry here; the
router, the manager and the voice tools read this table and nothing else.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jarvis.agents.base import AgentRunner, FakeAgentRunner
from jarvis.agents.claude import CLAUDE_MODELS, ClaudeAgentRunner
from jarvis.agents.router import RoutingAgentRunner
from jarvis.config import Settings

log = logging.getLogger("jarvis.agents.registry")


@dataclass(frozen=True)
class BackendSpec:
    """One coding agent, as the rest of Jarvis needs to know it."""

    #: The word in `AGENT_BACKEND`, `Task.agent` and the `dispatch_task` enum.
    name: str
    #: What a person calls it.
    label: str
    #: Spoken aliases and the model ids they mean.
    models: Mapping[str, str]
    #: The model a task runs on when nobody named one; blank is the agent's own default.
    default_model: Callable[[Settings], str]
    make_runner: Callable[[Settings], AgentRunner]


BACKENDS: dict[str, BackendSpec] = {
    "claude": BackendSpec(
        name="claude",
        label="Claude Code",
        models=CLAUDE_MODELS,
        default_model=lambda settings: settings.subagent_model,
        make_runner=ClaudeAgentRunner,
    ),
}


def resolve_model(agent: str, name: str | None, settings: Settings) -> str:
    """A spoken alias or full model id, as the id `agent` should run.

    Blank is the agent's configured default. An id that is not an alias passes through
    untouched: the agent is the judge of whether it exists.
    """
    spec = BACKENDS[agent]
    alias = (name or "").strip()
    if not alias:
        return spec.default_model(settings)
    return spec.models.get(alias.lower(), alias)


def build_agent_runner(settings: Settings) -> AgentRunner:
    """The runner `jarvis serve` hands the task manager.

    `--fake-agents` is the whole runner, whatever agents are enabled: it is for exercising
    everything else without a single real subagent. Otherwise one runner per enabled
    agent, behind the router.
    """
    if settings.fake_agents:
        return FakeAgentRunner()
    runners = {name: BACKENDS[name].make_runner(settings) for name in settings.enabled_agents}
    return RoutingAgentRunner(runners, default=settings.agent_backend)
