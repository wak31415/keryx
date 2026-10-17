"""`keryx restart` and the watchdog it arms (`restart-watch`)."""

import asyncio
import contextlib
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from keryx.cli.apps import app
from keryx.cli.common import _configure_readonly, _load_settings_optional, _restart_store
from keryx.config import Settings
from keryx.logging_util import mask_number
from keryx.restart.health import health_probe
from keryx.restart.logscan import errors_since
from keryx.restart.logscan import marks as log_marks
from keryx.restart.service import (
    UNSUPPORTED_HINT,
    candidate_target,
    foreign_units,
    is_stopped,
    resolve_target,
    spawn_watchdog,
    watch_command,
)
from keryx.restart.store import REFUSED as RESTART_REFUSED
from keryx.restart.store import STOPPED, RecordLocked, RestartRecord, RestartStore
from keryx.restart.version import loaded_version
from keryx.restart.watchdog import watch

#: Why `keryx restart` refuses while Keryx is stopped.
STOPPED_REFUSAL = ("Keryx is stopped (`keryx service stop`), so there is nothing running to "
                   "restart; `keryx service start` starts it")


#: Why `keryx restart` stops before handing over: its record was settled meanwhile.
OVERTAKEN = "Keryx was stopped while the restart was being set up, so it was not restarted"


def _restart_write(store: RestartStore, write: Callable[..., Any], *args: Any) -> Any:
    """One of the store's writes. When the record stays locked nothing is written, and the
    restart is abandoned before anything is handed over: said, and recorded as its failure
    once the lock can be had (`keryx restart --status`), exit 1."""
    try:
        return write(*args)
    except RecordLocked as error:
        typer.echo(f"the restart was abandoned: the restart record stayed locked ({error})",
                   err=True)
        with contextlib.suppress(RecordLocked):
            store.record_failure("its record stayed locked, so it was not restarted")
        raise typer.Exit(1) from None


@app.command()
def restart(
    reason: Annotated[
        str, typer.Option("--reason", help="Why, in a few words; you hear it back on the call.")
    ] = "",
    force: Annotated[
        bool, typer.Option("--force", help="Restart even while a call is in progress.")
    ] = False,
    no_callback: Annotated[
        bool, typer.Option("--no-callback", help="Restart quietly, with no call afterwards.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Print how the last restart went, and exit.")
    ] = False,
) -> None:
    """Restart the Keryx service; it phones back by itself once it is up again."""
    settings = _configure_readonly()
    store = _restart_store(settings)
    if status:
        _echo_restart_status(store)
        return

    # A unit another KERYX_HOME rendered is not this one's to restart: refused before
    # anything, the record included, which is this home's and not that service's. The
    # candidate is the unit `resolve_target` would answer with, whatever it answers.
    stopped = candidate_target(settings)
    if stopped is not None and (foreign := foreign_units([stopped])):
        typer.echo(foreign.message, err=True)
        typer.echo(foreign.detail, err=True)
        raise typer.Exit(2)

    # Stopped by `keryx service stop` is not a service to restart: a restart would start a
    # Keryx the owner stopped (systemd), or ask launchd for a job it has unloaded. Said, and
    # left on the record, where `keryx restart --status` finds it.
    if stopped is not None and is_stopped(stopped):
        _restart_write(store, store.save, RestartRecord(
            requested_at=datetime.now(UTC).isoformat(), reason=reason.strip(),
            origin_channel="cli", target=stopped.describe(), state=RESTART_REFUSED,
            error=STOPPED_REFUSAL,
        ))
        typer.echo(f"the restart was refused: {STOPPED_REFUSAL}", err=True)
        raise typer.Exit(1)

    # From a terminal, never from inside the unit: the installed service is the target.
    target = resolve_target(settings, from_outside=True)
    if target is None:
        typer.echo(UNSUPPORTED_HINT)
        raise typer.Exit(1)

    live = health_probe(settings)
    if live is None:
        where = f"http://{settings.host}:{settings.port}"
        typer.echo(f"nothing answering on {where} — restarting anyway")
    elif live and not force:
        typer.echo(f"{live} live session(s): a restart cuts them off. Wait, or pass --force.")
        raise typer.Exit(1)

    number = None if no_callback else settings.owner_number
    if not no_callback and not number:
        typer.echo("no OWNER_NUMBER to call back on: this restart will not be confirmed by phone")
    record = RestartRecord(
        requested_at=datetime.now(UTC).isoformat(),
        reason=reason.strip(),
        number=number,
        origin_channel="cli",
        target=target.describe(),
        quiet=no_callback,
        version=loaded_version(settings.state_dir),
        log_marks=log_marks(settings.state_dir),
    )
    # Refused while the owner's stop stands (its record says `stopped`): a restart then would
    # bring back what they stopped.
    if not _restart_write(store, store.create, record):
        stopped_now = (store.load() or RestartRecord()).state == STOPPED
        typer.echo(f"the restart was refused: {OVERTAKEN}" if stopped_now else
                   f"could not write {store.path}: the restart would go unconfirmed", err=True)
        raise typer.Exit(1)

    asked = record.requested_at
    if number:
        # The same watch the voice path arms, for the same reason: this command prints a
        # promise that keryx will call back, and nothing else would notice if it never
        # came back to make the call. Written only onto the record still pending: never
        # back over a `keryx service stop` that settled it meanwhile.
        watchdog = _arm_watchdog(settings, target)
        if not _restart_write(store, store.update_if_pending, asked,
                              lambda current: setattr(current, "watchdog", watchdog)):
            typer.echo(f"the restart was refused: {OVERTAKEN}", err=True)
            raise typer.Exit(1)
        typer.echo(f"watchdog: {watchdog}")

    command = target.command()
    # The last look before the hand-over. The lock is not held across it: a `systemctl
    # restart` lasts as long as Keryx takes to stop, and the lock's holders keep it for
    # milliseconds.
    if not store.is_pending(asked):
        typer.echo(f"the restart was refused: {OVERTAKEN}", err=True)
        raise typer.Exit(1)
    typer.echo(" ".join(command))
    code = subprocess.run(command, check=False).returncode
    if code != 0:
        error = f"{command[0]} exited {code}"

        def failed(current: RestartRecord) -> None:
            current.state, current.error = "failed", error

        _restart_write(store, store.update_if_pending, asked, failed)
        typer.echo(f"the restart failed: {error}")
        raise typer.Exit(1)

    if number:
        typer.echo(f"restarting; keryx will call {mask_number(number)} when it is back up")
    else:
        typer.echo("restarting; no call back was asked for")
    typer.echo("if the call never comes: keryx restart --status")


@app.command("restart-watch", hidden=True)
def restart_watch() -> None:
    """Watch a pending restart from outside the service; started by the restart itself.

    Not for hand use — `keryx restart` arms this, in a unit of its own so the restart
    cannot kill it (see `keryx.restart.service.watch_command`). Deliberately without
    `_add_file_logging`: its output belongs in `logs/restart-watch.log`, and writing its
    own "the restart never came back" into `keryx.log` would leave the next restart
    scanning that line back as a fault of Keryx's.
    """
    settings = _configure_readonly(quiet=False, migrated=False)
    typer.echo(asyncio.run(watch(settings)))


def _arm_watchdog(settings: Settings, target) -> str:
    """Start the out-of-process watch, and say how it went for the record."""
    plan = watch_command(settings, target)
    if plan is None:
        return "not started: systemd-run is not on PATH, so nothing outlives the restart"
    try:
        return f"{plan.label} (pid {spawn_watchdog(plan, settings)})"
    except Exception as exc:  # a missing binary, a refused fork
        return f"not started: {type(exc).__name__}: {exc}"


def _echo_restart_status(store: RestartStore) -> None:
    """Print the record the last restart left behind — the only trace of one that failed."""
    record = store.load()
    if record is None:
        typer.echo("no restart on record: the last one was confirmed, or there has not been one")
        return
    settings = _load_settings_optional()
    found = errors_since(settings.state_dir, record.log_marks)
    rows = [
        ("asked for", record.requested_at),
        ("reason", record.reason),
        ("loading", f"task {record.task_id}" if record.task_id else ""),
        ("through", record.target),
        ("version", record.version),
        ("call back", mask_number(record.number)),
        ("watchdog", record.watchdog),
        ("state", record.state),
        ("attempts", record.attempts),
        ("error", record.error),
        ("log errors", found.count or ""),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        if value not in (None, ""):
            typer.echo(f"{name:<{width}}  {value}")
    for line in found.lines:
        typer.echo(f"{'':<{width}}  {line}")
