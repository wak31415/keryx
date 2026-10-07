"""`keryx auth`: every sign-in Keryx needs, one command family.

`login` takes a name — `claude`, `codex`, `gmail`, `google-workspace` — and does that one
sign-in on this terminal; `status` reports all of them, read from the same `doctor` checks
the wizard reads, so the three can never disagree about whether something is signed in.
Gmail has no check of its own since it became a plugin's, so its state is its token file,
the one thing `check_email` reads. `logout` undoes the two Google sign-ins: the files each
left, never the OAuth client (configuration both share) and never the other's.

Gmail is the one in two steps: without `--callback-url` it prints a consent link, and with
it, it finishes. That is so a coding agent can relay the link to the person and the address
they land on back, and so it works on a machine with no browser at all.

`keryx auth login NAME --json` is the same four for the Mac app, as streams
(`keryx.setup.auth_stream`), built on `agent_cli` and `use_client_file` here.
"""

import asyncio
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keryx.agents import registry
from keryx.agents.registry import BACKENDS, install_command
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.store import ConfigStore
from keryx.doctor import Check, google_check, run_doctor_checks
from keryx.setup import agents, google

Echo = Callable[[str], None]

#: Every sign-in, and the doctor checks that say whether it is done. The two Google ones
#: are `sign_in`'s, which needs no other check run.
SIGN_INS = {
    "openai": ("OPENAI_API_KEY",),
    "claude": ("Claude Code agent",),
    "codex": ("Codex agent",),
    "local": ("Local model agent",),
    "gmail": (),
    "google-workspace": ("Google for agents",),
    "twilio": ("Twilio credentials",),
}
LOGINS = ("claude", "codex", "gmail", "google-workspace")
#: The sign-ins `logout` can undo: the ones whose tokens Keryx keeps itself.
LOGOUTS = ("gmail", "google-workspace")


class AuthError(RuntimeError):
    """A sign-in that could not go on, in one sentence."""


def status(
    settings: Settings, store: ConfigStore, *, smoke: bool = False, smoke_test=None
) -> dict[str, dict[str, Any]]:
    """`{name: {"state", "detail", …}}` for every sign-in; `smoke` also runs one task each."""
    checks = run_doctor_checks(settings, store=store)
    report: dict[str, dict[str, Any]] = {}
    for name, prefixes in SIGN_INS.items():
        if name in LOGOUTS:
            report[name] = sign_in(name, settings)
            continue
        found = [check for check in checks if check.name.startswith(prefixes)]
        report[name] = _entry(name, found)
    run = smoke_test or agents.run_smoke
    for name in BACKENDS:
        entry = report[name]
        entry["enabled"] = name in settings.enabled_agents
        entry["installed"] = registry.installed(name, settings)
        if not entry["installed"]:
            entry["install_command"] = install_command(name, settings)
        if smoke and entry["enabled"] and entry["state"] == "ok":
            result = asyncio.run(run(settings, name))
            entry["smoke"] = {"ok": agents.passed_smoke(result), "error": result.error}
            if not entry["smoke"]["ok"]:
                entry["state"] = "failed"
    return report


def sign_in(name: str, settings: Settings) -> dict[str, Any]:
    """`status`'s entry for one Google sign-in, without the rest of `doctor`: it is fast."""
    if name == "google-workspace":
        return _entry(name, [google_check(settings)])
    if name != "gmail":
        raise AuthError(f"{name!r} is not a Google sign-in; one of: {', '.join(LOGOUTS)}")
    path = google.token_path(settings)
    if path.is_file():
        return {"state": "ok", "detail": f"signed in (read-only); {path}"}
    return {"state": "missing", "detail": "not signed in — `keryx auth login gmail`"}


def logout(name: str, settings: Settings) -> list[Path]:
    """Remove what the Google sign-in `name` stored, and nothing else. Returns what went.

    Gmail is its token and a sign-in still waiting for its second step; Google for agents
    is `workspace-mcp`'s credentials directory, whole (a link to one is unlinked, never
    followed). The OAuth client in `KERYX_HOME` is configuration, and stays.
    """
    if name == "gmail":
        removed = []
        for path in (google.token_path(settings), settings.data_dir / google.PENDING_FILE):
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed.append(path)
        return removed
    if name != "google-workspace":
        raise AuthError(f"nothing called {name!r} to sign out of; one of: {', '.join(LOGOUTS)}")
    credentials = google.credentials_dir(settings)
    if credentials.is_symlink() or credentials.is_file():
        credentials.unlink()
        return [credentials]
    if not credentials.is_dir():
        return []
    removed = sorted(path for path in credentials.rglob("*") if not path.is_dir())
    shutil.rmtree(credentials)
    return removed


def _entry(name: str, found: list[Check]) -> dict[str, Any]:
    if not found:
        # An agent that is not enabled has no doctor check: say so rather than guess.
        return {"state": "missing", "detail": "not enabled"}
    worst = next((check for check in found if not check.ok), found[0])
    # The terminal's sentence, hint and all: `keryx auth status` is read at one.
    return {"state": worst.state, "detail": worst.text}


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
        settings = use_client_file(settings, store, options.client_file)
        echo(f"Google client saved to {settings.google_client_secrets_file}")
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


def use_client_file(settings: Settings, store: ConfigStore, source: Path) -> Settings:
    """The downloaded Google OAuth client at `source` installed and saved; the settings that
    now use it. Refused, as an `AuthError`, when it is not a client file."""
    try:
        path = google.install_client_file(settings, source)
    except google.GoogleSetupError as exc:
        raise AuthError(str(exc)) from None
    store.set({"GOOGLE_CLIENT_SECRETS_FILE": str(path)}, actor=OWNER)
    return settings.model_copy(update={"google_client_secrets_file": path})


def agent_cli(name: str) -> str:
    """The agent `name`'s own CLI, the one its runner uses; an `AuthError` saying how to
    install it when it or its SDK is missing."""
    spec = BACKENDS[name]
    if not registry.installed(name):
        raise AuthError(f"{spec.label} is not installed: {install_command(name)}")
    cli = spec.find_cli()
    if cli is None:
        raise AuthError(f"the {name} CLI is missing: {spec.install_hint}")
    return cli


def _agent_login(
    name: str,
    settings: Settings,
    *,
    options: LoginOptions,
    echo: Echo,
    run_login: Callable[[Sequence[str]], int],
) -> None:
    argv = agents.login_argv(name, agent_cli(name), headless=options.headless)
    echo(f"running `{' '.join([name, *argv[1:]])}` — finish the sign-in it asks for.")
    code = run_login(argv)
    if code != 0:
        raise AuthError(f"the login exited with {code}")
    if options.headless and name == "claude":
        echo(
            "store the token it printed with: "
            "keryx config set CLAUDE_CODE_OAUTH_TOKEN --stdin"
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
            "\n  keryx auth login gmail --callback-url '<that address>'"
        )
        return
    path = google.finish_signin(settings, options.callback_url, post=post)
    echo(f"signed in; saved to {path} (read-only). Restart Keryx to offer check_email.")
