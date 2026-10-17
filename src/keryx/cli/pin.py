"""`keryx pin`: whether a PIN is set, and setting it."""

import json
import sys
from typing import Annotated

import typer
from pydantic import ValidationError

from keryx.cli.apps import pin_app
from keryx.cli.common import _config_summary, _load_settings_reporting, _owner_only
from keryx.config import Settings
from keryx.config.pin import pin_file, read_enrolled_pin
from keryx.config.settings import ConfigFileError
from keryx.setup import pin as pin_flow
from keryx.status import service_running


def _pin_settings() -> Settings:
    """The settings `keryx pin` reads, or the reason they will not load and exit 1."""
    try:
        return _load_settings_reporting()[0]
    except ConfigFileError as error:
        typer.echo(str(error), err=True)
    except ValidationError as error:
        typer.echo(_config_summary(error), err=True)
    raise typer.Exit(1)


@pin_app.callback(invoke_without_command=True)
def pin(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Whether a phone PIN is set, where it is kept, and whether PIN entry is locked.

    Never the digits, nor how many there are. A report, so it exits 0.
    """
    if ctx.invoked_subcommand is not None:
        return
    found = pin_flow.pin_status(_pin_settings())
    if as_json:
        typer.echo(json.dumps(found))
        return
    for line in pin_flow.format_pin_status(found):
        typer.echo(line)


@pin_app.command("set")
def pin_set(
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the new PIN from stdin, twice, one per line.")
    ] = False,
    replace: Annotated[
        bool, typer.Option("--replace", help="Put it in place of the PIN already set.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Set the phone PIN, or replace it with --replace: the owner's alone, never the service's.

    The digits come only on stdin, typed twice; they are never printed, logged or quoted back.
    A Keryx already running keeps the PIN it started with until `keryx restart`.
    """
    _owner_only("set the phone PIN")  # before stdin is read: the service never gets that far
    if not stdin:
        typer.echo("the PIN is read from stdin only, never the command line: --stdin", err=True)
        raise typer.Exit(2)
    if sys.stdin.isatty():  # typed there, the digits would stay on the screen and in scrollback
        typer.echo(pin_flow.TERMINAL, err=True)
        raise typer.Exit(2)
    text = sys.stdin.read()
    settings = _pin_settings()
    if refusal := pin_flow.set_refusal(settings, replace=replace):
        typer.echo(refusal, err=True)
        raise typer.Exit(1)
    digits, problem = pin_flow.read_twice(text)
    if digits is None:
        typer.echo(problem, err=True)
        raise typer.Exit(2)
    replacing = pin_flow.existing_pin(settings) is not None
    path = pin_file(settings.config_dir)
    try:
        written = pin_flow.write_pin(settings, digits, replacing=replacing)
    except OSError:
        written = False
    if not written:
        typer.echo(
            # A first write that failed after its O_EXCL create leaves no PIN, just a file.
            pin_flow.ALREADY_SET
            if not replacing and read_enrolled_pin(settings.config_dir) is not None
            else pin_flow.NOT_WRITTEN.format(path=path),
            err=True,
        )
        raise typer.Exit(1)
    restart_needed = service_running(settings)
    if as_json:
        typer.echo(json.dumps({"set": True, "replaced": replacing,
                               "restart_needed": restart_needed}))
        return
    typer.echo(f"PIN {'replaced' if replacing else 'set'}, kept in {path} (readable by you alone)")
    if restart_needed:
        typer.echo("Keryx is running with the PIN it started with; `keryx restart` moves it on.")
