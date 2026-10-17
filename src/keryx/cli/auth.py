"""`keryx auth`: signing in to the coding agents and Google."""

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from keryx.cli.apps import auth_app
from keryx.cli.common import _configure_readonly, _owner_only, _owner_refusal
from keryx.config.store import ConfigStore
from keryx.integrations import accounts
from keryx.localmodels import pull
from keryx.setup import auth as auth_flow
from keryx.setup import auth_stream, smoke_stream
from keryx.setup.agents import is_headless
from keryx.setup.context import run_command


@auth_app.command("login")
def auth_login(
    name: Annotated[str, typer.Argument(help="claude, codex, gmail or google-workspace.")],
    headless: Annotated[
        bool | None,
        typer.Option(
            "--headless/--browser",
            help="The agent login for a machine with no browser (default: detected).",
        ),
    ] = None,
    client_file: Annotated[
        Path | None,
        typer.Option("--client-file", help="The Google OAuth client JSON you downloaded."),
    ] = None,
    callback_url: Annotated[
        str | None,
        typer.Option("--callback-url", help="gmail: the address you landed on, in quotes."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="JSON lines for the Mac app: the link to open, then how "
                     "it ended. A code or address is read as one line on stdin."),
    ] = False,
) -> None:
    """Sign in to one thing: a coding agent, Gmail (read-only), or Google for agents.

    gmail is two steps: once for a link to open on any device, then again with
    --callback-url and the address the browser landed on. With --json it is one, and so is
    every other: Claude's is `claude setup-token`, its token stored as
    CLAUDE_CODE_OAUTH_TOKEN; Codex's is its device code.
    """
    if as_json:
        raise typer.Exit(_auth_login_stream(name, client_file, callback_url))
    if name not in auth_flow.LOGINS:
        typer.echo(f"sign in to one of: {', '.join(auth_flow.LOGINS)}", err=True)
        raise typer.Exit(2)
    _owner_only("sign in")
    settings = _configure_readonly()
    options = auth_flow.LoginOptions(
        headless=is_headless() if headless is None else headless,
        client_file=client_file,
        callback_url=callback_url,
    )
    try:
        auth_flow.login(
            name, settings, ConfigStore(), options=options, echo=typer.echo, run_login=run_command
        )
    except auth_flow.AuthError as error:
        typer.echo(f"{name} sign-in failed: {error}", err=True)
        raise typer.Exit(1) from None


def _auth_login_stream(name: str, client_file: Path | None, callback_url: str | None) -> int:
    """`auth login NAME --json`: one ending on every path, the refusals included."""
    events = auth_stream.LoginEvents(typer.echo)

    def run() -> int:
        if name not in auth_flow.LOGINS:
            events.error(f"sign in to one of: {', '.join(auth_flow.LOGINS)}")
            return 2
        if callback_url is not None:
            events.error("--callback-url is the two-step sign-in; with --json the address "
                         "is one line on stdin")
            return 2
        if refusal := _owner_refusal("sign in"):
            events.error(refusal)
            return 1
        settings = _configure_readonly(refuse=events.error)
        return auth_stream.stream(name, settings, ConfigStore(), events, client_file=client_file,
                                  lines=auth_stream.StdinLines(sys.stdin))

    with pull.terminate_as_interrupt():
        return events.guard(run)


@auth_app.command("status")
def auth_status_command(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
    smoke: Annotated[
        bool, typer.Option("--smoke", help="Also run one real task on each enabled agent.")
    ] = False,
) -> None:
    """Every sign-in Keryx needs, and whether it is done. Exit 1 if one has failed."""
    settings = _configure_readonly()
    report = auth_flow.status(settings, ConfigStore(), smoke=smoke)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        width = max(map(len, report))
        for name, entry in report.items():
            # An agent nobody chose is not a sign-in that is missing. The JSON keeps the
            # state (and says `enabled`), so a script reading it sees no new value.
            state = "off" if entry.get("enabled") is False else entry["state"]
            typer.echo(f"{name:<{width}}  {state:<8}  {entry['detail']}")
    if any(entry["state"] == "failed" for entry in report.values()):
        raise typer.Exit(1)


@auth_app.command("smoke")
def auth_smoke(
    name: Annotated[str, typer.Argument(help="claude, codex or local.")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON lines for the Mac app: each "
                                          "step, then how it ended.")] = False,
) -> None:
    """One real task on one coding agent — the smoke test `auth status --smoke` runs on
    each — told as it runs. Exit 0 when it said it is ready, 1 when it did not (or could not
    be asked: not installed, not signed in), 2 for no such agent, 130 when stopped."""
    if as_json:
        events: smoke_stream.SmokeEvents = smoke_stream.SmokeEvents(typer.echo)
    else:
        events = smoke_stream.SmokeText(typer.echo, lambda line: typer.echo(line, err=True))

    def run() -> int:
        settings = _configure_readonly(refuse=events.error)
        return smoke_stream.stream(settings, name, events)

    with pull.terminate_as_interrupt():
        raise typer.Exit(events.guard(run))


@auth_app.command("logout")
def auth_logout(
    name: Annotated[str, typer.Argument(help="gmail or google-workspace.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """Sign out of Gmail or Google for agents: the tokens that sign-in stored go.

    The OAuth client file stays (it is configuration, and the two share it), and so does
    the other sign-in. The settings that turn them on are `keryx config unset`'s.
    """
    _owner_only("sign out")
    if name not in auth_flow.LOGOUTS:
        typer.echo(f"sign out of one of: {', '.join(auth_flow.LOGOUTS)}", err=True)
        raise typer.Exit(2)
    settings = _configure_readonly()
    removed = auth_flow.logout(name, settings)
    title = next(one.title for one in accounts.INTEGRATIONS.values() if one.signin == name)
    message = (
        f"Signed out of {title}: its stored sign-in was removed." if removed
        else f"{title} was not signed in; there was nothing to remove."
    )
    if as_json:
        typer.echo(json.dumps({"name": name, "signed_in": False, "message": message}, indent=2))
    else:
        typer.echo(message)
