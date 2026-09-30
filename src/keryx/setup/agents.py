"""The coding agents: which are here, signing them in, and proving one can run a task.

Keryx hands its work to a coding agent — Claude Code or Codex — and each can sign in with
an API key or with the subscription its owner already pays for. Three callers use this:

- `keryx setup`'s "Coding agents" section (`run_section`): which agents, which is the
  default, a sign-in for each one that has none, then one real task on each;
- `keryx auth login claude|codex` (`login`): the agent's own login command, on this
  terminal, or its headless variant;
- `keryx auth status --smoke` (`run_smoke`).

**An agent that can already run is never asked for a credential.** The precedence is
`keryx.agents.auth`'s — key, headless token, stored login — and the sign-in question only
comes up for an agent with none of the three. The smoke test is one minimal task through the
real runner, asked to answer `SPOKEN_SUMMARY: ready`: it proves the binary, the credential
and the parsing end to end, which no amount of looking at files can.
"""

import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from keryx.agents import registry
from keryx.agents.auth import AuthMode, AuthStatus
from keryx.agents.base import AgentOpenError, RunResult
from keryx.agents.registry import BACKENDS, auth_status, install_command, resolve_model
from keryx.config import Settings
from keryx.config.permissions import ACTOR_ENV, SERVICE
from keryx.setup.context import SetupContext
from keryx.setup.ui import Choice
from keryx.tasks.models import Task, TaskKind

#: What the smoke task is asked to say, and what counts as having said it.
SMOKE_PROMPT = (
    "This is a connectivity check from Keryx's setup. Do not use any tools and do not "
    "change anything. Reply with exactly one line: SPOKEN_SUMMARY: ready"
)
SMOKE_ANSWER = "ready"
#: Long enough for a cold start of either CLI and one short turn; a hang is a failure.
SMOKE_TIMEOUT_S = 300.0
#: A setup task that reads folders (project context) gets longer.
TASK_TIMEOUT_S = 900.0

#: The settings each agent's API-key and headless-token tiers are stored under.
API_KEY = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}
TOKEN = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "CODEX_ACCESS_TOKEN"}
SUBSCRIPTION = {"claude": "your Claude Pro or Max plan", "codex": "your ChatGPT plan"}


@dataclass
class AgentState:
    """One agent as this machine has it."""

    name: str
    cli: str | None
    auth: AuthStatus
    smoke: RunResult | None = None

    @property
    def installed(self) -> bool:
        return registry.installed(self.name)

    @property
    def ready(self) -> bool:
        return self.installed and self.cli is not None and self.auth.ready

    @property
    def smoke_ok(self) -> bool:
        return self.smoke is not None and passed_smoke(self.smoke)

    def as_dict(self) -> dict:
        spec = BACKENDS[self.name]
        smoke = None
        if self.smoke is not None:
            smoke = {
                "ok": self.smoke_ok,
                "summary": self.smoke.spoken_summary,
                "error": self.smoke.error,
            }
        return {
            "name": self.name,
            "label": spec.label,
            "installed": self.installed,
            "install_command": None if self.installed else install_command(self.name),
            "cli": self.cli,
            "auth": self.auth.mode.value,
            "auth_detail": self.auth.detail,
            "ready": self.ready,
            "smoke": smoke,
        }


def detect(settings: Settings) -> dict[str, AgentState]:
    """Every agent Keryx knows, as this machine has it right now."""
    return {
        name: AgentState(name, spec.find_cli(), auth_status(name, settings))
        for name, spec in BACKENDS.items()
    }


def passed_smoke(result: RunResult) -> bool:
    """The agent ran, and said what it was asked to."""
    return result.ok and result.spoken_summary.strip().rstrip(".").lower() == SMOKE_ANSWER


async def run_task(
    settings: Settings, agent: str, prompt: str, *, timeout_s: float = TASK_TIMEOUT_S
) -> RunResult:
    """One real task on `agent`, through the runner `keryx serve` would use."""
    task = Task(
        id=None,
        kind=TaskKind.AGENT,
        description="setup",
        agent=agent,
        model=resolve_model(agent, None, settings),
    )
    # The agent is not the owner, even when the owner started setup: it runs as the service,
    # so a `keryx config set` it reaches for is held to what the service may change.
    previous = os.environ.get(ACTOR_ENV)
    os.environ[ACTOR_ENV] = SERVICE
    try:
        try:
            session = await BACKENDS[agent].make_runner(settings).open(task)
        except AgentOpenError as exc:  # already redacted: it says why, and nothing more
            return RunResult(ok=False, error=str(exc))
        try:
            return await asyncio.wait_for(
                session.run(prompt, on_progress=lambda _text: None), timeout_s
            )
        except TimeoutError:
            return RunResult(ok=False, error=f"no answer within {timeout_s:.0f}s")
        finally:
            await session.close()
    finally:
        if previous is None:
            os.environ.pop(ACTOR_ENV, None)
        else:
            os.environ[ACTOR_ENV] = previous


async def run_smoke(settings: Settings, agent: str) -> RunResult:
    """One minimal task on `agent`, asked to say it is ready."""
    return await run_task(settings, agent, SMOKE_PROMPT, timeout_s=SMOKE_TIMEOUT_S)


def is_headless() -> bool:
    """No display to finish a browser login on: a Linux box with no X or Wayland."""
    if sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login_argv(agent: str, cli: str, *, headless: bool) -> list[str]:
    """The agent's own login command, run with the CLI that was found.

    The found CLI is the one the runner uses: for Codex that is the SDK's bundled binary,
    and a `codex` on PATH may be another version or not there at all.
    """
    spec = BACKENDS[agent]
    command = spec.headless_login_command if headless else spec.login_commands[0]
    return [cli, *command[1:]]


def parity_notes(settings: Settings, enabled: Sequence[str]) -> list[str]:
    """What one agent does here that another does not, said up front rather than found out."""
    notes = []
    if "codex" in enabled and not settings.google_workspace_mcp:
        notes.append(
            "Codex has no claude.ai connectors, so its tasks have no Gmail or Calendar until "
            "you connect Google for agents (the Google section below)."
        )
    if "codex" in enabled:
        notes.append(
            "The approval bridge (`keryx approvals`) covers Claude Code sessions on your "
            "screen only; it has nothing to do with which agent Keryx dispatches to."
        )
    return notes


# --- the wizard section ----------------------------------------------------------------


def run_section(ctx: SetupContext) -> None:
    """Choose the agents, sign in the ones that need it, and run one task on each."""
    ui = ctx.ui
    ui.note(
        "Keryx hands the work you ask for to a coding agent. Each one can run on the "
        "subscription you already pay for, or on an API key billed per token."
    )
    states = detect(ctx.settings)
    ui.table(("Agent", "Installed", "Signed in"), [_row(state) for state in states.values()])
    available = [name for name, state in states.items() if state.installed]
    if not available:
        ui.error("No coding agent is installed.")
        ui.note(f"Install one with `{install_command('claude')}` (or codex), then run setup again.")
        return

    enabled = _choose_agents(ctx, states)
    default = _choose_default(ctx, enabled)
    if not ctx.save(
        {"AGENT_BACKEND": default, "AGENTS_ENABLED": ",".join(enabled) if len(enabled) > 1 else ""}
    ):
        return
    for name in enabled:
        states[name] = _sign_in(ctx, states[name])
    for name in enabled:
        _smoke_one(ctx, states[name])
    for note in parity_notes(ctx.settings, enabled):
        ui.note(note)


def _row(state: AgentState) -> tuple[str, str, str]:
    spec = BACKENDS[state.name]
    if not state.installed:
        return spec.label, f"no — {install_command(state.name)}", ""
    signed = state.auth.detail if state.auth.mode is not AuthMode.NONE else "not yet"
    return spec.label, "yes", signed


def _choose_agents(ctx: SetupContext, states: dict[str, AgentState]) -> list[str]:
    settings = ctx.settings
    current = [name for name in settings.enabled_agents if states[name].installed]
    ready = [name for name, state in states.items() if state.ready]
    preset = current or ready or [next(name for name, s in states.items() if s.installed)]
    choices = [
        Choice(
            name,
            BACKENDS[name].label,
            hint="ready" if state.ready else "",
            checked=name in preset,
            disabled=None if state.installed else f"not installed: {install_command(name)}",
        )
        for name, state in states.items()
    ]
    while True:
        picked = ctx.ui.checkbox("Which agents should Keryx use?", choices)
        if picked:
            return picked
        ctx.ui.warn("Pick at least one.")


def _choose_default(ctx: SetupContext, enabled: list[str]) -> str:
    if len(enabled) == 1:
        return enabled[0]
    current = ctx.settings.agent_backend
    return ctx.ui.select(
        "Which one does the work when you do not name one?",
        [Choice(name, BACKENDS[name].label) for name in enabled],
        default=current if current in enabled else enabled[0],
    )


def _sign_in(ctx: SetupContext, state: AgentState) -> AgentState:
    """Leave an agent that can already run alone; otherwise ask how it should pay."""
    spec = BACKENDS[state.name]
    if state.auth.ready and not ctx.review:
        ctx.ui.success(f"{spec.label}: {state.auth.detail}")
        return state
    if state.cli is None:
        ctx.ui.error(f"{spec.label}: its CLI is missing — {spec.install_hint}")
        return state
    options = [
        Choice("subscription", "Subscription", hint=f"sign in with {SUBSCRIPTION[state.name]}"),
        Choice("api_key", "API key", hint="billed per token"),
        Choice("token", "Subscription token", hint="for a machine with no browser"),
    ]
    if state.auth.ready:
        options.insert(0, Choice("keep", f"Keep {state.auth.detail}"))
    how = ctx.ui.select(f"How should {spec.label} sign in?", options, default=options[0].value)
    if how == "api_key":
        key = ctx.ui.secret(f"{API_KEY[state.name]}", validate=_not_blank,
                            current=ctx.current(API_KEY[state.name]))
        ctx.save({API_KEY[state.name]: key})
    elif how == "token":
        if state.name == "claude" and ctx.ui.confirm(
            "Run `claude setup-token` now to get one?", default=True
        ):
            ctx.probes.run_login([state.cli, "setup-token"])
        token = ctx.ui.secret(f"{TOKEN[state.name]} (paste it here)", validate=_not_blank,
                              current=ctx.current(TOKEN[state.name]))
        ctx.save({TOKEN[state.name]: token})
    elif how == "subscription":
        headless = ctx.probes.headless()
        argv = login_argv(state.name, state.cli, headless=headless)
        shown = " ".join([state.name, *argv[1:]])
        ctx.ui.note(f"Running `{shown}` — finish the sign-in it asks for.")
        code = ctx.probes.run_login(argv)
        if code != 0:
            ctx.ui.warn(f"the login exited with {code}")
        if headless and state.name == "claude":
            ctx.ui.note("`claude setup-token` printed a token; paste it so Keryx can use it.")
            token = ctx.ui.secret(TOKEN["claude"], validate=_not_blank,
                                  current=ctx.current(TOKEN["claude"]))
            ctx.save({TOKEN["claude"]: token})
    fresh = AgentState(state.name, state.cli, auth_status(state.name, ctx.settings))
    if fresh.auth.ready:
        ctx.ui.success(f"{spec.label}: {fresh.auth.detail}")
    else:
        ctx.ui.error(f"{spec.label} is still not signed in — `keryx auth login {state.name}`")
    return fresh


def _smoke_one(ctx: SetupContext, state: AgentState) -> None:
    if not state.ready:
        return
    label = BACKENDS[state.name].label
    with ctx.ui.spinner(f"Running one real task on {label}…"):
        state.smoke = asyncio.run(ctx.probes.smoke(ctx.settings, state.name))
    if state.smoke_ok:
        ctx.ui.success(f"{label} ran a task")
    else:
        why = state.smoke.error or state.smoke.spoken_summary or "no answer"
        ctx.ui.error(f"{label} could not run a task: {why}")


def _not_blank(value: str) -> str | None:
    return None if value.strip() else "It cannot be blank."
