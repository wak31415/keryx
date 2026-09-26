"""`jarvis setup-agent`: from a fresh machine to a coding agent that has run a task.

Jarvis hands its work to a coding agent — Claude Code or Codex — and each can sign in with
an API key or with the subscription its owner already pays for. This is the guided way
through that, in the order a new install needs it:

1. **Detect.** For every agent Jarvis knows: is its CLI there, and which credential would
   it run on (`jarvis.agents.auth` — the same precedence the runners use)?
2. **Choose.** The default agent, and any others to enable. When exactly one is ready, it
   is the one preselected.
3. **Sign in** to what is missing. A subscription login runs the agent's own login command
   with this terminal; an API key is a line for `.env`, which this never reads or writes;
   a missing CLI is an install command, which this never runs.
4. **Smoke test.** One real, minimal task through the real runner, asked to answer
   `SPOKEN_SUMMARY: ready`. It proves the binary, the credential and the parsing end to
   end, which no amount of looking at files can. `--no-smoke` skips it.
5. **Print the lines to paste**: `AGENT_BACKEND=`, `AGENTS_ENABLED=`, and `CODEX_MODEL=`
   when Codex is in — the way `init` prints `OWNER_NAME=`.

`--json` (with `--yes`) is the same report for an agent setting Jarvis up: one document on
stdout, and the exit code as the contract (`STATUSES`). The terminal and the side effects
come in as callables, so none of this needs a terminal, a login or a subagent to test.
"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from jarvis.agents.auth import AuthMode, AuthStatus
from jarvis.agents.base import AgentOpenError, RunResult
from jarvis.agents.registry import BACKENDS, auth_status, resolve_model
from jarvis.config import Settings, env_var_name
from jarvis.tasks.models import Task, TaskKind

Echo = Callable[[str], None]
Ask = Callable[[str, str], str]
Confirm = Callable[[str, bool], bool]
RunLogin = Callable[[Sequence[str]], int]
Smoke = Callable[[Settings, str], Awaitable[RunResult]]

#: What the smoke task is asked to say, and what counts as having said it.
SMOKE_PROMPT = (
    "This is a connectivity check from Jarvis's setup. Do not use any tools and do not "
    "change anything. Reply with exactly one line: SPOKEN_SUMMARY: ready"
)
SMOKE_ANSWER = "ready"
#: Long enough for a cold start of either CLI and one short turn; a hang is a failure.
SMOKE_TIMEOUT_S = 300.0

#: What became of the setup: what `--json` reports as `status`. Exit 1 is anything but
#: `ready` — an agent that was chosen cannot run a task — and exit 2 is a wrong command line.
STATUSES = {
    "ready": "every chosen agent is installed and signed in, and passed its smoke test if run",
    "not_ready": "a chosen agent has no CLI or no credential",
    "smoke_failed": "a chosen agent is signed in but could not run the smoke task",
}


@dataclass
class AgentState:
    """One agent as this machine has it."""

    name: str
    cli: str | None
    auth: AuthStatus
    smoke: RunResult | None = None

    @property
    def ready(self) -> bool:
        return self.cli is not None and self.auth.ready

    @property
    def smoke_ok(self) -> bool:
        return self.smoke is not None and passed_smoke(self.smoke)

    def as_dict(self, enabled: Sequence[str]) -> dict:
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
            "enabled": self.name in enabled,
            "cli": self.cli,
            "install_hint": None if self.cli else spec.install_hint,
            "auth": self.auth.mode.value,
            "auth_detail": self.auth.detail,
            "ready": self.ready,
            "instructions_file": str(spec.instructions_file()),
            "smoke": smoke,
        }


def detect(settings: Settings) -> dict[str, AgentState]:
    """Every agent Jarvis knows, as this machine has it right now."""
    return {
        name: AgentState(name, spec.find_cli(), auth_status(name, settings))
        for name, spec in BACKENDS.items()
    }


def passed_smoke(result: RunResult) -> bool:
    """The agent ran, and said what it was asked to."""
    return result.ok and result.spoken_summary.strip().rstrip(".").lower() == SMOKE_ANSWER


async def run_smoke(settings: Settings, agent: str) -> RunResult:
    """One real minimal task on `agent`, through the runner `jarvis serve` would use."""
    task = Task(
        id=None,
        kind=TaskKind.AGENT,
        description="setup check: reply that you are ready",
        agent=agent,
        model=resolve_model(agent, None, settings),
    )
    try:
        session = await BACKENDS[agent].make_runner(settings).open(task)
    except AgentOpenError as exc:  # already redacted: it says why, and nothing more
        return RunResult(ok=False, error=str(exc))
    try:
        return await asyncio.wait_for(
            session.run(SMOKE_PROMPT, on_progress=lambda _text: None), SMOKE_TIMEOUT_S
        )
    except TimeoutError:
        return RunResult(ok=False, error=f"no answer within {SMOKE_TIMEOUT_S:.0f}s")
    finally:
        await session.close()


def run_login_command(argv: Sequence[str]) -> int:
    """Run a login command on this terminal — its prompts and its browser link are theirs."""
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError as exc:
        print(f"could not run {' '.join(argv)}: {exc}", file=sys.stderr)
        return 127


def is_headless() -> bool:
    """No display to finish a browser login on: a Linux box with no X or Wayland."""
    if sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def env_lines(settings: Settings, default: str, enabled: Sequence[str]) -> list[str]:
    """The `.env` lines that make this choice stick. Printed, never written."""
    listed = ",".join(enabled) if len(enabled) > 1 else ""
    lines = [
        f"{env_var_name('agent_backend')}={default}",
        f"{env_var_name('agents_enabled')}={listed}",
    ]
    if "codex" in enabled:
        lines.append(f"{env_var_name('codex_model')}={settings.codex_model or ''}")
    return lines


def parity_notes(settings: Settings, enabled: Sequence[str]) -> list[str]:
    """What one agent does here that another does not, said up front rather than found out."""
    notes = []
    if "codex" in enabled and not settings.google_workspace_mcp:
        notes.append(
            "Codex has no claude.ai connectors, so its tasks have no Gmail or Calendar until "
            "you set GOOGLE_WORKSPACE_MCP=true and run `uv run jarvis setup-google`."
        )
    if "codex" in enabled:
        notes.append(
            "The approval bridge (`jarvis approvals`) covers Claude Code sessions on your "
            "screen only; it has nothing to do with which agent Jarvis dispatches to."
        )
    return notes


class SetupError(ValueError):
    """A command line that names an agent Jarvis does not know, or contradicts itself."""


def _resolve_choice(
    settings: Settings,
    states: dict[str, AgentState],
    *,
    default: str | None,
    enable: Sequence[str] | None,
    yes: bool,
    ask: Ask,
    confirm: Confirm,
) -> tuple[str, list[str]]:
    """The default agent and every enabled one, the default first."""
    for name in [default, *(enable or [])]:
        if name is not None and name not in BACKENDS:
            raise SetupError(f"no agent called {name!r}; Jarvis knows {', '.join(BACKENDS)}")
    ready = [name for name, state in states.items() if state.ready]
    preselected = ready[0] if len(ready) == 1 else settings.agent_backend
    if default is None:
        if yes:
            default = preselected
        else:
            choices = "/".join(BACKENDS)
            answer = ask(f"which agent should do the work by default? ({choices})", preselected)
            default = answer.strip().lower() or preselected
            if default not in BACKENDS:
                raise SetupError(f"no agent called {default!r}")
    if enable is None:
        enable = [
            name
            for name in BACKENDS
            if name != default
            and (
                name in settings.agents_enabled
                or (not yes and states[name].ready and confirm(f"also enable {name}?", True))
            )
        ]
    return default, list(dict.fromkeys([default, *enable]))


def _sign_in(
    state: AgentState,
    settings: Settings,
    *,
    echo: Echo,
    ask: Ask,
    run_login: RunLogin,
    headless: bool,
) -> None:
    """Get one agent from not ready to ready, as far as a terminal can."""
    spec = BACKENDS[state.name]
    if state.cli is None:
        echo(f"{spec.label}: the {state.name} CLI is not installed. install it with:")
        echo(f"    {spec.install_hint}")
        return
    if state.auth.ready:
        return
    how = ask(
        f"{spec.label} is not signed in. use your subscription (s) or an API key (k)?", "s"
    )
    if how.strip().lower().startswith("k"):
        echo("add this line to your .env with your key (setup-agent never edits it):")
        echo(f"    {spec.auth.api_key_env}=")
        return
    command = spec.headless_login_command if headless else spec.login_commands[0]
    echo(f"running `{' '.join(command)}` — finish the sign-in it asks for.")
    code = run_login(command)
    if code != 0:
        echo(f"`{' '.join(command)}` exited with {code}.")
    if state.name == "claude" and headless:
        echo(
            "claude setup-token prints a token: add it to your .env as "
            f"{spec.auth.token_env}= (setup-agent never edits it)."
        )
    state.auth = auth_status(state.name, settings)


def run_setup_agent(
    settings: Settings,
    *,
    default: str | None = None,
    enable: Sequence[str] | None = None,
    smoke: bool = True,
    yes: bool = False,
    as_json: bool = False,
    echo: Echo,
    ask: Ask,
    confirm: Confirm,
    run_login: RunLogin = run_login_command,
    smoke_test: Smoke = run_smoke,
    headless: bool | None = None,
) -> int:
    """The whole of `jarvis setup-agent`; returns the exit code (see `STATUSES`).

    `yes` asks nothing and signs nothing in: it reports, smoke-tests what is ready, and
    prints the lines. Raises `SetupError` for a command line that cannot be acted on.
    """
    say: Echo = (lambda _line: None) if as_json else echo
    states = detect(settings)
    say(_table(states))
    default, enabled = _resolve_choice(
        settings, states, default=default, enable=enable, yes=yes, ask=ask, confirm=confirm
    )
    if not yes:
        for name in enabled:
            _sign_in(
                states[name],
                settings,
                echo=say,
                ask=ask,
                run_login=run_login,
                headless=is_headless() if headless is None else headless,
            )
    if smoke:
        for name in enabled:
            state = states[name]
            if not state.ready:
                continue
            say(f"smoke test: one real task on {BACKENDS[name].label}…")
            state.smoke = asyncio.run(smoke_test(settings, name))
            say(
                f"  {name}: passed"
                if state.smoke_ok
                else f"  {name}: FAILED — {state.smoke.error or state.smoke.spoken_summary}"
            )

    status = _status(states, enabled, smoke=smoke)
    lines = env_lines(settings, default, enabled)
    notes = parity_notes(settings, enabled)
    if as_json:
        echo(
            json.dumps(
                {
                    "status": status,
                    "default": default,
                    "enabled": enabled,
                    "agents": [state.as_dict(enabled) for state in states.values()],
                    "env_lines": lines,
                    "notes": notes,
                },
                indent=2,
            )
        )
        return exit_code(status)
    echo("\nadd these lines to your .env (setup-agent never edits it):\n")
    for line in lines:
        echo(f"    {line}")
    for note in notes:
        echo(f"\nnote: {note}")
    echo(f"\n{status}: {STATUSES[status]}")
    return exit_code(status)


def exit_code(status: str) -> int:
    """0 when every chosen agent can run a task, 1 otherwise."""
    return 0 if status == "ready" else 1


def _status(states: dict[str, AgentState], enabled: Sequence[str], *, smoke: bool) -> str:
    if not all(states[name].ready for name in enabled):
        return "not_ready"
    if smoke and not all(states[name].smoke_ok for name in enabled):
        return "smoke_failed"
    return "ready"


def _table(states: dict[str, AgentState]) -> str:
    """The detect step as a small table: one line per agent."""
    rows = [("AGENT", "CLI", "SIGN-IN")]
    for state in states.values():
        mode = state.auth.mode
        auth = state.auth.detail if mode is not AuthMode.NONE else "not signed in"
        rows.append((state.name, state.cli or "not installed", auth))
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    return "\n".join(f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c}" for a, b, c in rows)
