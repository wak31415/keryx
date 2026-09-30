"""Every coding agent Keryx knows, as data: `BACKENDS`.

A backend is a name, a runner factory, the model names that can be said for it out loud,
where its credentials come from, and where it keeps its instructions and skills. The
router, the manager, the voice tools, `doctor` and `keryx setup` read this table and
nothing else (see docs/agents.md, "Adding a third agent").

Each agent's SDK is an optional extra of the same name (`uv sync --extra codex`), so an
agent can be simply not installed; `installed` says so, and such an agent is never ready,
never offered, and refused as the default.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from keryx.agents.auth import AuthSource, AuthStatus, resolve_auth
from keryx.agents.base import AgentRunner, FakeAgentRunner
from keryx.agents.claude import CLAUDE_AUTH, CLAUDE_MODELS, ClaudeAgentRunner, claude_cli
from keryx.agents.codex import (
    CODEX_AUTH,
    CODEX_MODELS,
    CodexAgentRunner,
    codex_cli,
    codex_cli_version,
    codex_home,
)
from keryx.agents.router import RoutingAgentRunner
from keryx.config import Settings
from keryx.config.files import claude_config_dir

log = logging.getLogger("keryx.agents.registry")


@dataclass(frozen=True)
class BackendSpec:
    """One coding agent, as the rest of Keryx needs to know it."""

    #: The word in `AGENT_BACKEND`, `Task.agent` and the `dispatch_task` enum.
    name: str
    #: The module its extra (named `name` too) installs: missing means not installed.
    package: str
    #: What a person calls it on a screen, and what the voice model calls it out loud.
    label: str
    spoken_name: str
    #: Spoken aliases and the model ids they mean, and how the voice tool describes them.
    models: Mapping[str, str]
    model_hint: str
    #: The model a task runs on when nobody named one; blank is the agent's own default.
    default_model: Callable[[Settings], str]
    make_runner: Callable[[Settings], AgentRunner]
    auth: AuthSource
    #: The agent's CLI, or None when it is not installed.
    find_cli: Callable[[], str | None]
    #: How to install that CLI, for when it is not.
    install_hint: str
    #: The instructions file the agent reads on every run, and its skills directory.
    instructions_file: Callable[[], Path]
    skills_dir: Callable[[Settings], Path]
    #: The interactive commands that store a subscription login.
    login_commands: tuple[tuple[str, ...], ...]
    #: A headless variant, for a machine with no browser to finish the login in.
    headless_login_command: tuple[str, ...]
    #: The CLI's version, for `doctor`, when the agent's package says what it is.
    cli_version: Callable[[], str | None] = lambda: None


BACKENDS: dict[str, BackendSpec] = {
    "claude": BackendSpec(
        name="claude",
        package="claude_agent_sdk",
        label="Claude Code",
        spoken_name="Claude",
        models=CLAUDE_MODELS,
        model_hint="opus (strongest, the default), sonnet, fable or haiku (fastest)",
        default_model=lambda settings: settings.subagent_model,
        make_runner=ClaudeAgentRunner,
        auth=CLAUDE_AUTH,
        find_cli=claude_cli,
        install_hint="uv sync --extra claude (the Claude Agent SDK bundles the claude CLI)",
        instructions_file=lambda: claude_config_dir() / "CLAUDE.md",
        # `SKILLS_DIR`, which predates a second agent and is Claude's.
        skills_dir=lambda settings: settings.skills_dir,
        login_commands=(("claude", "/login"),),
        headless_login_command=("claude", "setup-token"),
    ),
    "codex": BackendSpec(
        name="codex",
        package="openai_codex",
        label="Codex",
        spoken_name="Codex",
        models=CODEX_MODELS,
        model_hint="astra, sol, luna or terra",
        default_model=lambda settings: settings.codex_model or "",
        make_runner=CodexAgentRunner,
        auth=CODEX_AUTH,
        find_cli=codex_cli,
        install_hint="uv sync --extra codex (the openai-codex SDK bundles the codex CLI)",
        instructions_file=lambda: codex_home() / "AGENTS.md",
        skills_dir=lambda settings: codex_home() / "skills",
        login_commands=(("codex", "login"),),
        headless_login_command=("codex", "login", "--device-auth"),
        cli_version=codex_cli_version,
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


def agent_for_model(name: str | None) -> str | None:
    """The agent a spoken alias belongs to ("opus" is Claude's), or None for anything else."""
    alias = (name or "").strip().lower()
    for spec in BACKENDS.values():
        if alias in spec.models:
            return spec.name
    return None


def auth_status(agent: str, settings: Settings) -> AuthStatus:
    """Which credential `agent` would run on, probing for a stored login."""
    return resolve_auth(BACKENDS[agent].auth, settings)


def installed(agent: str) -> bool:
    """Is `agent`'s SDK — its extra — installed? Checked without importing it."""
    return find_spec(BACKENDS[agent].package) is not None


def install_command(agent: str) -> str:
    """What installs `agent`: its extra."""
    return f"uv sync --extra {agent}"


def is_ready(agent: str, settings: Settings) -> bool:
    """Its SDK and CLI are installed and some credential resolves."""
    spec = BACKENDS[agent]
    return (
        installed(agent) and spec.find_cli() is not None and auth_status(agent, settings).ready
    )


def ready_backends(settings: Settings) -> list[str]:
    """The enabled agents that could run a task right now, the default first."""
    return [name for name in settings.enabled_agents if is_ready(name, settings)]


def offered_agents(settings: Settings) -> list[str]:
    """The agents the voice model may name: the default, then every other one that is ready.

    The default is always there, ready or not — a task nobody named an agent for goes to it
    either way, and its failure says why, which beats a tool that silently lost its agent —
    unless it is not installed at all, which `serve` refuses before anything is offered.
    Under `--fake-agents` every enabled agent is offered, since none of them is real.
    """
    if settings.fake_agents:
        return list(settings.enabled_agents)
    return [
        name
        for name in settings.enabled_agents
        if (name == settings.agent_backend and installed(name)) or is_ready(name, settings)
    ]


def skill_dirs(settings: Settings) -> list[Path]:
    """Where every enabled agent keeps its skills, the default's first."""
    dirs = (BACKENDS[name].skills_dir(settings) for name in settings.enabled_agents)
    return list(dict.fromkeys(dirs))


def build_agent_runner(settings: Settings) -> AgentRunner:
    """The runner `keryx serve` hands the task manager.

    `--fake-agents` is the whole runner, whatever agents are enabled: it is for exercising
    everything else without a single real subagent. Otherwise one runner per enabled
    agent, behind the router.
    """
    if settings.fake_agents:
        return FakeAgentRunner()
    runners = {name: BACKENDS[name].make_runner(settings) for name in settings.enabled_agents}
    return RoutingAgentRunner(runners, default=settings.agent_backend)
