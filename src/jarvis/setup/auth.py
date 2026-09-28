"""`jarvis auth`: every sign-in Jarvis needs, one command family.

`login` takes a name — `claude`, `codex`, `gmail`, `google-workspace` — and does that one
sign-in on this terminal; `status` reports all of them, read from the same `doctor` checks
the wizard reads, so the three can never disagree about whether something is signed in.

Gmail is the one in two steps: without `--callback-url` it prints a consent link, and with
it, it finishes. That is so a coding agent can relay the link to the person and the address
they land on back, and so it works on a machine with no browser at all.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.agents import registry
from jarvis.agents.registry import BACKENDS, install_command
from jarvis.config import Settings
from jarvis.config.permissions import OWNER
from jarvis.config.store import ConfigStore
from jarvis.doctor import Check, run_doctor_checks
from jarvis.setup import agents, google

Echo = Callable[[str], None]

#: Every sign-in, and the doctor checks that say whether it is done.
SIGN_INS = {
    "openai": ("OPENAI_API_KEY",),
    "claude": ("Claude Code agent",),
    "codex": ("Codex agent",),
    "gmail": ("email",),
    "google-workspace": ("Google for agents",),
    "twilio": ("Twilio credentials",),
}
LOGINS = ("claude", "codex", "gmail", "google-workspace")


class AuthError(RuntimeError):
    """A sign-in that could not go on, in one sentence."""


def status(
    settings: Settings, store: ConfigStore, *, smoke: bool = False, smoke_test=None
) -> dict[str, dict[str, Any]]:
    """`{name: {"state", "detail", …}}` for every sign-in; `smoke` also runs one task each."""
    checks = run_doctor_checks(settings, store=store)
    report: dict[str, dict[str, Any]] = {}
    for name, prefixes in SIGN_INS.items():
        found = [check for check in checks if check.name.startswith(prefixes)]
        report[name] = _entry(name, found)
    run = smoke_test or agents.run_smoke
    for name in BACKENDS:
        entry = report[name]
        entry["enabled"] = name in settings.enabled_agents
        entry["installed"] = registry.installed(name)
        if not entry["installed"]:
            entry["install_command"] = install_command(name)
        if smoke and entry["enabled"] and entry["state"] == "ok":
            result = asyncio.run(run(settings, name))
            entry["smoke"] = {"ok": agents.passed_smoke(result), "error": result.error}
            if not entry["smoke"]["ok"]:
                entry["state"] = "failed"
    return report


def _entry(name: str, found: list[Check]) -> dict[str, Any]:
    if not found:
        # An agent that is not enabled has no doctor check: say so rather than guess.
        return {"state": "missing", "detail": "not enabled"}
    worst = next((check for check in found if not check.ok), found[0])
    return {"state": worst.state, "detail": worst.detail}


@dataclass
class LoginOptions:
    headless: bool = False
    client_file: Path | None = None
    callback_url: str | None = None


def login(
    name: str,
    settings: Settings,
    store: ConfigStore,
    *,
    options: LoginOptions,
    echo: Echo,
    run_login: Callable[[Sequence[str]], int],
    post: Callable[..., Any] | None = None,
) -> None:
    """Do the one sign-in `name` names; raises `AuthError` with what went wrong."""
    if name in BACKENDS:
        _agent_login(name, settings, options=options, echo=echo, run_login=run_login)
        return
    if options.client_file is not None:
        try:
            path = google.install_client_file(settings, options.client_file)
        except google.GoogleSetupError as exc:
            raise AuthError(str(exc)) from None
        store.set({"GOOGLE_CLIENT_SECRETS_FILE": str(path)}, actor=OWNER)
        settings = settings.model_copy(update={"google_client_secrets_file": path})
        echo(f"Google client saved to {path}")
    try:
        if name == "gmail":
            _gmail(settings, store, options=options, echo=echo, post=post)
        elif name == "google-workspace":
            store.set({"GOOGLE_WORKSPACE_MCP": True}, actor=OWNER)
            google.run_google_setup(settings, echo=echo)
        else:
            raise AuthError(f"nothing called {name!r} to sign in to; one of: {', '.join(LOGINS)}")
    except google.GoogleSetupError as exc:
        raise AuthError(str(exc)) from None


def _agent_login(
    name: str,
    settings: Settings,
    *,
    options: LoginOptions,
    echo: Echo,
    run_login: Callable[[Sequence[str]], int],
) -> None:
    spec = BACKENDS[name]
    if not registry.installed(name):
        raise AuthError(f"{spec.label} is not installed: {install_command(name)}")
    cli = spec.find_cli()
    if cli is None:
        raise AuthError(f"the {name} CLI is missing: {spec.install_hint}")
    argv = agents.login_argv(name, cli, headless=options.headless)
    echo(f"running `{' '.join([name, *argv[1:]])}` — finish the sign-in it asks for.")
    code = run_login(argv)
    if code != 0:
        raise AuthError(f"the login exited with {code}")
    if options.headless and name == "claude":
        echo(
            "store the token it printed with: "
            "jarvis config set CLAUDE_CODE_OAUTH_TOKEN --stdin"
        )


def _gmail(
    settings: Settings,
    store: ConfigStore,
    *,
    options: LoginOptions,
    echo: Echo,
    post: Callable[..., Any] | None,
) -> None:
    if options.callback_url is None:
        url = google.start_signin(settings)
        echo("Open this link on any device and approve read-only Gmail access:\n")
        echo(url)
        echo(
            "\nYou will land on a page that does not load. Copy its whole address, then run"
            "\n  jarvis auth login gmail --callback-url '<that address>'"
        )
        return
    path = google.finish_signin(settings, options.callback_url, post=post)
    echo(f"signed in; saved to {path} (read-only). Restart Jarvis to offer check_email.")
