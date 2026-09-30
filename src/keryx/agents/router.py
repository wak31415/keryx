"""One `AgentRunner` in front of several: each task opens on the agent it was dispatched to.

The task manager holds exactly one runner, and it does not know there is more than one
agent. This is that runner. It opens `runners[task.agent]` — the agent stored on the row,
never the current default — so a follow-up or a re-run always lands on the agent that
started the task. That is not a preference: a session id belongs to the agent that
issued it, and a Claude session handed to Codex cannot be resumed.
"""

import logging

from keryx.agents.base import AgentRunner, AgentSession
from keryx.tasks.models import Task

log = logging.getLogger("keryx.agents.router")


class AgentNotEnabledError(RuntimeError):
    """A task names an agent this process has no runner for."""

    def __init__(self, agent: str, enabled: list[str]) -> None:
        super().__init__(
            f"task needs the {agent} agent, which is not enabled here"
            f" (enabled: {', '.join(enabled) or 'none'})"
        )
        self.agent = agent


class RoutingAgentRunner(AgentRunner):
    """Opens each task on its own agent's runner."""

    def __init__(self, runners: dict[str, AgentRunner], default: str) -> None:
        if default not in runners:
            raise ValueError(f"the default agent {default!r} has no runner")
        self.runners = dict(runners)
        self.default = default

    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession:
        """Open `task` on `task.agent`; a task with no agent recorded takes the default.

        An agent that is not enabled raises rather than falling back: running the task on
        another agent would silently drop its session, and the manager turns the raise
        into a failed row that says why.
        """
        agent = task.agent or self.default
        runner = self.runners.get(agent)
        if runner is None:
            raise AgentNotEnabledError(agent, sorted(self.runners))
        return await runner.open(task, resume=resume)
