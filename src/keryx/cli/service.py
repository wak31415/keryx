"""`keryx service`: Keryx's units, installed, started and stopped."""

import contextlib
import json
from typing import Annotated

import typer

from keryx import units
from keryx.cli.apps import service_app
from keryx.cli.common import _configure_readonly, _install_units, _owner_refusal, _restart_store
from keryx.restart.store import RecordLocked


@service_app.callback(invoke_without_command=True)
def service_command(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Whether each of Keryx's services is installed, and running."""
    if ctx.invoked_subcommand is not None:
        return
    settings = _configure_readonly()
    document = units.status(settings)
    if as_json:
        typer.echo(json.dumps(document, indent=2))
        return
    manager = document["manager"]
    typer.echo(f"service manager: {manager or 'none on this machine'}")
    for kind, unit in document["units"].items():
        name = units.unit_name(settings, manager, kind) if manager else "-"
        state = ("not installed" if not unit["installed"]
                 else "running" if unit["active"] else "installed, not running")
        typer.echo(f"{kind:<9} {name:<23} {state}")


#: Who installs each model server's unit, since `service install` does not: the command
#: that installs it, starts it, checks it answers, and removes it again when it does not.
MODEL_UNIT_OWNERS = {"--llm": ("keryx models start", "the model server"),
                     "--voice": ("keryx voice use here", "the voice server"),
                     "--voice-llm": ("keryx voice llm here MODEL", "the voice LLM server")}


@service_app.command("install")
def service_install(
    llm_unit: Annotated[
        bool, typer.Option("--llm", help="Refused: `keryx models start` installs, starts and "
                                         "checks the model server's unit.")
    ] = False,
    voice_unit: Annotated[
        bool, typer.Option("--voice", help="Refused: `keryx voice use here` installs, starts "
                                           "and checks the voice server's unit.")
    ] = False,
    voice_llm_unit: Annotated[
        bool, typer.Option("--voice-llm", help="Refused: `keryx voice llm here MODEL` (or "
                                               "`keryx models start --voice-llm`) installs, "
                                               "starts and checks the voice LLM server's "
                                               "unit.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Install Keryx and its tunnel as services, through the checkout's installer.

    Not the local model servers' units: `keryx models start`, `keryx voice use here` and
    `keryx voice llm here` install those, start them and check they answer, and remove the
    unit again when they do not — a unit installed bare would restart a server that cannot
    start, for ever.
    """
    flags = [flag for flag, on in (("--llm", llm_unit), ("--voice", voice_unit),
                                   ("--voice-llm", voice_llm_unit)) if on]
    if flags:
        owners = " and ".join(f"`{MODEL_UNIT_OWNERS[flag][0]}` for {MODEL_UNIT_OWNERS[flag][1]}"
                              for flag in flags)
        message = (f"keryx service install {' '.join(flags)} is not offered: use {owners}. "
                   f"{'Each' if len(flags) > 1 else 'It'} installs the unit, starts the server "
                   "and checks it answers, and removes the unit again if it does not.")
        if as_json:
            typer.echo(json.dumps({"ok": False, "message": message}, indent=2))
        else:
            typer.echo(message, err=True)
        raise typer.Exit(2)
    _install_units(as_json=as_json)


@service_app.command("uninstall")
def service_uninstall(
    llm_unit: Annotated[
        bool, typer.Option("--llm", help="The model server's unit, instead of Keryx's.")
    ] = False,
    voice_unit: Annotated[
        bool, typer.Option("--voice", help="The voice server's unit, instead of Keryx's.")
    ] = False,
    voice_llm_unit: Annotated[
        bool, typer.Option("--voice-llm", help="The voice LLM server's unit, instead of "
                                               "Keryx's.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Stop and remove Keryx and its tunnel, or the local model servers' (--llm, --voice,
    --voice-llm)."""
    _install_units(llm_unit=llm_unit, voice_unit=voice_unit, voice_llm_unit=voice_llm_unit,
                   uninstall=True, as_json=as_json)


#: What `service stop|start` is, refused to the service.
CONTROL_THE_SERVICES = "stop or start Keryx's services"
ALL_UNITS_HELP = "The model servers too: every unit Keryx installs, not only itself and its tunnel."


@service_app.command("stop")
def service_stop(
    every: Annotated[bool, typer.Option("--all", help=ALL_UNITS_HELP)] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Stop Keryx and its tunnel, and keep them from starting at login, without removing
    them: `keryx service start` starts them again. A restart in flight is settled as
    stopped by the owner first, so its watchdog rings nobody. Exit 0 stopped (or nothing to
    stop), 1 when a unit would not stop, 2 refused."""
    _control_units("stop", every=every, as_json=as_json)


@service_app.command("start")
def service_start(
    every: Annotated[bool, typer.Option("--all", help=ALL_UNITS_HELP)] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Let Keryx and its tunnel start at login again, and start them — with --all, every
    model server that is installed, first. Idempotent. Exit 0 running, 1 when a unit would
    not start, 2 refused (Keryx not installed, no service manager)."""
    _control_units("start", every=every, as_json=as_json)


def _control_units(action: str, *, every: bool, as_json: bool) -> None:
    """`service stop|start`: one document on stdout with --json on every path."""

    def said(outcome: units.Control) -> None:
        if as_json:
            typer.echo(json.dumps(outcome.as_dict(), indent=2))
            return
        typer.echo(outcome.message, err=not outcome.ok)
        if outcome.detail:
            typer.echo(outcome.detail, err=True)
        for row in outcome.units:
            typer.echo(f"{row['kind']:<7} {row['unit']:<22} {row['state']}")

    if refusal := _owner_refusal(CONTROL_THE_SERVICES):
        said(units.Control(False, 2, refusal, None, []))
        raise typer.Exit(2)

    def refuse(message: str, *, detail: str | None = None) -> None:
        said(units.Control(False, 1, message, detail, []))

    # A stop reads nothing a migration moves, and must work whatever is left to move; a
    # start would hand the manager a `keryx serve` that refuses to run until it has.
    settings = _configure_readonly(refuse=refuse if as_json else None,
                                   migrated=action == "start")
    store = _restart_store(settings)
    outcome = units.control(settings, action, every=every, settle=store.settle_stopped)
    if action == "start" and outcome.ok:
        # Running again: the stop's marker goes, so restarts may be asked for once more. A
        # record that stays locked keeps it until Keryx's own start clears it.
        with contextlib.suppress(RecordLocked):
            store.clear_stopped()
    said(outcome)
    if outcome.code != 0:
        raise typer.Exit(outcome.code)
