"""`keryx auth`: every sign-in Keryx needs, one command family.

`login` takes a name — `claude`, `codex`, `google` — and does that one sign-in on this
terminal; `status` reports all of them, read from the same `doctor` checks the wizard
reads, so the three can never disagree about whether something is signed in.

Google is one sign-in for everything (`keryx.setup.google`), finished one of two ways. With
a browser on this machine it is one command: a listener on `127.0.0.1` takes Google's
answer. With the browser somewhere else (`--headless`, or no display) the owner pastes back
the address they land on: at a terminal that is still one command, and without one — a
coding agent relaying the link — it is two, the second with `--callback-url`. `gmail` and
`google-workspace` are the names the two older sign-ins had, and run this one.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keryx.agents import registry
from keryx.agents.registry import BACKENDS, install_command
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.store import ConfigStore
from keryx.doctor import GOOGLE_CHECK, Check, run_doctor_checks
from keryx.integrations.gmail import signed_in as gmail_signed_in
from keryx.integrations.google import read_token
from keryx.setup import agents, google

Echo = Callable[[str], None]

#: Every sign-in, and the doctor checks that say whether it is done.
SIGN_INS = {
    "openai": ("OPENAI_API_KEY",),
    "claude": ("Claude Code agent",),
    "codex": ("Codex agent",),
    "google": (GOOGLE_CHECK,),
    "twilio": ("Twilio credentials",),
}
#: The names the two older Google sign-ins had: still accepted by `login`, and still
#: reported by `status`, for one release.
GOOGLE_ALIASES = ("gmail", "google-workspace")
LOGINS = ("claude", "codex", "google", *GOOGLE_ALIASES)
#: What `logout` can end: a grant Keryx holds, and the files a sign-in left.
LOGOUTS = ("google",)


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
    token = read_token(settings)
    report["google"]["services"] = list(token.connected) if token is not None else []
    for alias in GOOGLE_ALIASES:
        report[alias] = {**report["google"], "alias_of": "google"}
    if report["gmail"]["state"] == "ok" and not gmail_signed_in(settings):
        report["gmail"]["state"] = "missing"
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
    #: Google: the services to ask for; None is every one the client offers.
    services: tuple[str, ...] | None = None
    #: Google: the account the owner means to sign in as, when it is not the one saved.
    account: str | None = None


def login(
    name: str,
    settings: Settings,
    store: ConfigStore,
    *,
    options: LoginOptions,
    echo: Echo,
    run_login: Callable[[Sequence[str]], int],
    post: Callable[..., Any] | None = None,
    ask: Callable[[str], str] | None = None,
) -> None:
    """Do the one sign-in `name` names; raises `AuthError` with what went wrong.

    `ask` puts one question to the person at the terminal and is None when there is no
    terminal, which is what makes a pasted-back Google sign-in two commands instead of one.
    """
    if name in BACKENDS:
        _agent_login(name, settings, options=options, echo=echo, run_login=run_login)
        return
    if name == "google" or name in GOOGLE_ALIASES:
        try:
            _google(settings, store, options=options, echo=echo, post=post, ask=ask)
        except google.GoogleSetupError as exc:
            raise AuthError(str(exc)) from None
        return
    raise AuthError(f"nothing called {name!r} to sign in to; one of: {', '.join(LOGINS)}")


def logout(
    name: str,
    settings: Settings,
    *,
    echo: Echo,
    sign_out: Callable[[Settings], Any] | None = None,
) -> None:
    """End the sign-in `name` names: revoke it where it was granted, and remove what it
    left on this machine. `sign_out` is `google.sign_out`, looked up when called."""
    if name not in LOGOUTS:
        raise AuthError(f"nothing called {name!r} to sign out of; one of: {', '.join(LOGOUTS)}")
    out = (sign_out or google.sign_out)(settings)
    if not out.removed:
        echo("Not signed in to Google: there is nothing to remove.")
        return
    count = f"{len(out.removed)} file{'s are' if len(out.removed) != 1 else ' is'} removed"
    if out.revoked is False:
        echo(
            f"The sign-in is removed from this machine ({count}), but Google did not "
            f"confirm that the grant has ended: remove Keryx at {google.PERMISSIONS_URL}."
        )
    else:
        echo(f"Signed out of Google: the grant is revoked and {count}.")
    echo(
        "USER_GOOGLE_EMAIL is left as it is, so the next sign-in is still held to that "
        "account; `keryx config unset USER_GOOGLE_EMAIL` forgets it."
    )


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
        raise AuthError(f"the {name} CLI is missing: {registry.install_hint(name)}")
    argv = agents.login_argv(name, cli, headless=options.headless)
    echo(f"running `{' '.join([name, *argv[1:]])}` — finish the sign-in it asks for.")
    code = run_login(argv)
    if code != 0:
        raise AuthError(f"the login exited with {code}")
    if options.headless and name == "claude":
        echo(
            "store the token it printed with: "
            "keryx config set CLAUDE_CODE_OAUTH_TOKEN --stdin"
        )


def _google(
    settings: Settings,
    store: ConfigStore,
    *,
    options: LoginOptions,
    echo: Echo,
    post: Callable[..., Any] | None,
    ask: Callable[[str], str] | None,
) -> None:
    if options.client_file is not None:
        path = google.install_client_file(settings, options.client_file)
        store.set({"GOOGLE_CLIENT_SECRETS_FILE": str(path)}, actor=OWNER)
        settings = settings.model_copy(update={"google_client_secrets_file": path})
        echo(f"Google client saved to {path}")
        if settings.google_oauth_client_id and settings.google_oauth_client_secret:
            echo(
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are set and come "
                "first, so this sign-in uses them, not that file: `keryx config unset "
                "GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET` to use the file."
            )
    account = options.account
    if options.callback_url is not None:
        result = google.finish_signin(settings, options.callback_url, account=account, post=post)
    else:
        chosen = google.offer(settings, options.services)
        for line in chosen.notes:
            echo(line)
        if not options.headless:
            result = google.browser_signin(
                settings, chosen, announce=_announcer(echo), account=account, post=post
            )
        else:
            echo("Open this link on any device and approve it:\n")
            echo(google.start_signin(settings, chosen))
            landed = "\nYou will land on a page that does not load. "
            if ask is None:
                echo(
                    f"{landed}Copy its whole address into a file, then run"
                    "\n  keryx auth login google --callback-url - < FILE"
                    "\nThe address holds a one-time code: handed over on standard input, "
                    "it is never on a command line."
                )
                return
            result = google.finish_signin(
                settings,
                ask(f"{landed}Paste its whole address here"),
                account=account,
                post=post,
            )
    # What the sign-in implies is saved before anything is said about it.
    notes = _keep(settings, store, result)
    for line in [*google.summary(result), *notes]:
        echo(line)
    if hint := google.api_hint(result, published=result.whose != google.OWN_CLIENT):
        echo(hint)


def _announcer(echo: Echo) -> Callable[[str, bool], None]:
    def announce(url: str, opened: bool) -> None:
        echo(
            "Your browser is opening Google's sign-in. If it does not, open this link on "
            "this machine:\n"
            if opened
            else "Open this link in a browser on this machine:\n"
        )
        echo(url)
        echo("\nWaiting for you to finish there (Ctrl-C stops; --headless is for a browser "
             "on another machine).")

    return announce


def _keep(settings: Settings, store: ConfigStore, result: google.SignInResult) -> list[str]:
    """Save the address the agents act as and turn their Google tools on the first time;
    what was done comes back as lines to say."""
    values: dict[str, Any] = {}
    notes = []
    if settings.user_google_email != result.email:
        values["USER_GOOGLE_EMAIL"] = result.email
        was = f" (it was {settings.user_google_email})" if settings.user_google_email else ""
        notes.append(f"USER_GOOGLE_EMAIL is now {result.email}{was}.")
    if google.agents_switched_off(settings, store.stored()):
        notes.append(
            "Agents' Google tools stay off, as you set them: `keryx config set "
            "GOOGLE_WORKSPACE_MCP true` turns them on."
        )
    elif not settings.google_workspace_mcp:
        values["GOOGLE_WORKSPACE_MCP"] = True
        notes.append(
            "Agents get the Google tools from the next task (`keryx restart` if Keryx is "
            "running); `keryx config set GOOGLE_WORKSPACE_MCP false` turns them off."
        )
    if values:
        store.set(values, actor=OWNER)
    return notes
