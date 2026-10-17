"""`keryx migrate`: moving a Jarvis install to Keryx's directories."""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from keryx import units
from keryx.child_env import redact_known
from keryx.cli.apps import app
from keryx.cli.common import (
    MAX_DESCRIPTION_CHARS,
    SETTINGS_UNREADABLE,
    _config_summary,
    _interactive,
    _load_settings_optional,
    _owner_only,
    _owner_refusal,
    _shorten,
)
from keryx.config import Settings
from keryx.config.migrate import (
    InstallFailed,
    MigrationError,
    hook_installed,
    make_plan,
    old_target,
    retire_old_units,
)
from keryx.config.migrate import Report as MigrationReport
from keryx.config.migrate import Service as MigratingService
from keryx.config.migrate import run as run_migration
from keryx.config.settings import ConfigFileError
from keryx.config.store import ConfigStore
from keryx.doctor import fix_permissions
from keryx.localmodels import pull
from keryx.restart.health import health_probe
from keryx.restart.service import (
    INSTALLERS,
    ForeignUnits,
    ServiceTarget,
    candidate_target,
    resolve_target,
)
from keryx.setup import migrate_stream
from keryx.setup.context import run_command
from keryx.setup.sections import repo_root

#: How many of the tasks that lost their sessions are named one by one.
MAX_LOST_SESSIONS_LISTED = 10


#: What `migrate` is, refused to the service.
MIGRATE_FILES = "migrate Keryx's files"


@app.command()
def migrate(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would move, and change nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before starting.")] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="The plan as one JSON document; with --yes, the run as "
                     "JSON lines: each step, then how it ended."),
    ] = False,
) -> None:
    """Move a Jarvis install to Keryx's directories: ~/.config/jarvis and its siblings, or
    ~/.jarvis and a .env here.

    Stops the service while it moves things, re-renders it and the approval hook, and
    starts it again; Jarvis's unit is retired, and Keryx's installed in its place. Run it
    from the checkout the service runs in. Nothing is deleted: ~/.jarvis is renamed aside
    with whatever is left in it.
    """
    if as_json and yes and not dry_run:
        raise typer.Exit(_migrate_stream())
    _owner_only(MIGRATE_FILES)
    plan = make_plan([Path.cwd(), repo_root()])
    if as_json:
        _migrate_plan_json(plan)
        return
    if plan.empty and not plan.conflicts:
        typer.echo("nothing to migrate: Keryx already keeps everything where it should")
        return
    for line in plan.describe():
        typer.echo(line)
    settings = _load_settings_optional()
    units_ = _migration_units(settings, plan)
    target, predecessor = units_.target, units_.predecessor
    if predecessor is not None:
        typer.echo(f"  {predecessor.describe()}: stopped first, and retired once everything "
                   f"has moved; {target.describe()} installed and started in its place")
    elif target is not None:
        typer.echo(f"  {target.describe()}: stopped first, re-rendered, and started again")
    if refusal := _migration_refusal(plan, settings, units_, live_calls=not dry_run):
        typer.echo(f"\n{refusal.message}", err=True)
        for line in refusal.details:
            typer.echo(f"  {line}", err=True)
        raise typer.Exit(refusal.code)
    if dry_run:
        return
    if not yes:
        if not _interactive():
            typer.echo("pass --yes to migrate without a terminal to ask at", err=True)
            raise typer.Exit(2)
        if not typer.confirm("\nGo ahead?", default=False):
            raise typer.Exit(1)
    try:
        report = _run_migration(plan, units_, echo=typer.echo)
    except MigrationError as error:
        typer.echo(f"keryx migrate: {error}", err=True)
        raise typer.Exit(1) from None
    _echo_migration(report)


def _migration_units(settings: Settings, plan) -> migrate_stream.Units:
    """The service a migration stops and starts again, and Jarvis's unit when it is still
    the one installed: stopped first, and retired for Keryx's. A run that did not finish the
    units names the one it was installing, which may not be installed to be found."""
    target = resolve_target(settings, from_outside=True)
    predecessor = old_target(candidate_target(settings))
    if predecessor is not None:
        target = target or candidate_target(settings)
    if target is None and (pending := plan.pending_target) is not None:
        target = ServiceTarget(*pending)
    return migrate_stream.Units(target, predecessor)


def _foreign_migration(
    settings: Settings, units_: migrate_stream.Units
) -> ForeignUnits | None:
    """Keryx's own units, when another KERYX_HOME than the one this migration fills rendered
    them (`units.owner_mismatch`): what a migration must not stop, re-render or start. Only
    Keryx's: Jarvis's unit (`predecessor`) was rendered for the old home by definition, and
    is retired, not judged."""
    if units_.target is None:
        return None
    return units.owner_mismatch(settings, units.MAIN_KINDS,
                                service_manager=units_.target.manager)


@dataclasses.dataclass(frozen=True)
class _MigrationRefusal:
    """Why a migration may not start: a sentence for a person, what lies behind it, and the
    exit code — 2 when the owner must settle something first, 1 when later will do."""

    message: str
    details: list[str]
    code: int


def _migration_refusal(
    plan, settings: Settings, units_: migrate_stream.Units, *, live_calls: bool = True
) -> _MigrationRefusal | None:
    """What stops `plan` before anything is touched, asked by `migrate` and `migrate --yes
    --json` alike so the terminal and the app refuse the same runs the same way: a conflict,
    Keryx's units rendered for another home, and — unless `live_calls` is off, as for a dry
    run — a call the stop would cut off."""
    if plan.conflicts:
        return _MigrationRefusal(migrate_stream.CONFLICTS, list(plan.conflicts), 2)
    if foreign := _foreign_migration(settings, units_):
        return _MigrationRefusal(foreign.message, [foreign.detail], 2)
    if live_calls and units_.target is not None and (live := health_probe(settings)):
        return _MigrationRefusal(migrate_stream.LIVE_CALL, [f"{live} live call(s)"], 1)
    return None


def _run_migration(
    plan, units_: migrate_stream.Units, *, echo: Callable[[str], None],
    on_step: Callable[[str], None] = lambda step: None,
    run_installer: Callable[[list[str]], int] | None = None,
) -> MigrationReport:
    """`config.migrate.run` with this machine's service, told to `echo` and `on_step`;
    `run_installer` runs the re-rendering installers (`run_command`, on this terminal)."""
    target, predecessor = units_.target, units_.predecessor
    stopping = units_.stopping
    return run_migration(
        plan,
        service=MigratingService(stopping) if stopping is not None else None,
        successor=MigratingService(target) if predecessor and target else None,
        retire=(lambda: retire_old_units(predecessor.manager)) if predecessor else list,
        fix_permissions=lambda: fix_permissions(_load_settings_optional(), ConfigStore()),
        rerender=lambda: _rerender(target, hook=plan.hook, run=run_installer),
        echo=echo,
        on_step=on_step,
    )


def _migrate_plan_json(plan) -> None:
    """`migrate --json`: the plan as one document, and exit 0 — a conflict is in it, not a
    failure of the read. Settings that will not load are said on stderr, exit 1."""
    try:
        settings = _load_settings_optional()
    except (ConfigFileError, ValidationError) as error:
        summary = _config_summary(error) if isinstance(error, ValidationError) else str(error)
        typer.echo(f"keryx migrate: the settings do not load: {summary}", err=True)
        raise typer.Exit(1) from None
    units_ = _migration_units(settings, plan)
    live = health_probe(settings) if units_.target is not None else None
    typer.echo(json.dumps(migrate_stream.plan_document(plan, units_, live), indent=2))


def _migrate_stream() -> int:
    """`migrate --yes --json`: the run told as JSON lines, one ending on every path.

    Exit 0 done (`nothing_to_do` when there was nothing), 1 an error (the service's
    refusal, settings that do not load, a live call, a run that stopped part way), 2 a
    plan with conflicts (nothing touched), 130 cancelled before the first step.
    """
    events = migrate_stream.MigrateSteps(typer.echo)

    def run() -> int:
        if refusal := _owner_refusal(MIGRATE_FILES):
            events.error(refusal)
            return 1
        plan = make_plan([Path.cwd(), repo_root()])
        if plan.empty and not plan.conflicts:
            events.nothing_to_do()
            return 0
        try:
            settings = _load_settings_optional()
        except (ConfigFileError, ValidationError) as error:
            summary = (_config_summary(error) if isinstance(error, ValidationError)
                       else str(error))
            events.error(SETTINGS_UNREADABLE, detail=redact_known(summary))
            return 1
        units_ = _migration_units(settings, plan)
        if refusal := _migration_refusal(plan, settings, units_):
            events.error(refusal.message,
                         detail=redact_known("; ".join(refusal.details), settings))
            return refusal.code
        with migrate_stream.held() as cancelled:
            try:
                report = _run_migration(
                    plan, units_, echo=lambda line: None, on_step=events.step,
                    run_installer=lambda argv: units.run_captured(argv)[0],
                )
            except MigrationError as error:
                events.error(migrate_stream.STOPPED, detail=redact_known(str(error), settings))
                return 1
            except Exception as error:  # told before Cancel can land on it
                events.error(pull.UNEXPECTED, detail=pull.unexpected_detail(error))
                return 1
            if cancelled():
                events.step(migrate_stream.LATE_CANCEL)
            events.done(report)
            return 0

    with pull.terminate_as_interrupt():
        return events.guard(run)


def _rerender(
    target, *, hook: bool | None = None, run: Callable[[list[str]], int] | None = None
) -> list[str]:
    """Re-install what names the old paths: the service's unit, and the approval hook —
    when `hook` says (the plan's, which a run that did not finish recorded), else when
    Claude's settings name it now. `run` runs each installer: `run_command`, on this
    terminal, unless one is given."""
    run = run or run_command
    done = []
    scripts = repo_root() / "scripts"
    if target is not None:
        installer = scripts / Path(INSTALLERS[target.manager]).name
        code = run([str(installer)])
        done.append(_installed(installer.name, code))
    if hook_installed() if hook is None else hook:
        code = run([str(scripts / "install-claude-hook.sh")])
        done.append(_installed("approval hook", code))
    return done


def _installed(name: str, code: int) -> str:
    """What became of one installer; a failure keeps the units pending (`InstallFailed`)."""
    if code == 0:
        return f"{name}: done"
    return InstallFailed(f"{name}: exited {code}, run it again")


def _echo_migration(report: MigrationReport) -> None:
    typer.echo(f"\nmoved {len(report.moved)} entr{'y' if len(report.moved) == 1 else 'ies'}")
    for line in report.imported:
        typer.echo(f"imported {line}")
    if report.rewritten:
        typer.echo(f"tasks.db: {report.rewritten} path(s) rewritten")
    for line in report.notes + report.tightened + report.rerendered:
        typer.echo(line)
    if report.lost_sessions:
        typer.echo(
            f"\n{len(report.lost_sessions)} Claude task(s) ran in the old workspace, so their "
            "sessions are gone; a follow-up to one starts afresh:"
        )
        for task_id, status, description in report.lost_sessions[:MAX_LOST_SESSIONS_LISTED]:
            what = _shorten(description, MAX_DESCRIPTION_CHARS)
            typer.echo(f"  #{task_id:<5} {status:<9} {what}")
        if len(report.lost_sessions) > MAX_LOST_SESSIONS_LISTED:
            typer.echo(f"  … and {len(report.lost_sessions) - MAX_LOST_SESSIONS_LISTED} more")
    if report.retired_to is not None:
        typer.echo(f"\nthe old directory is now {report.retired_to}")
    if report.leftovers:
        typer.echo("\nNothing reads these any more. Delete them once you are sure (and revoke a "
                   "token before you delete it):")
        for path in report.leftovers:
            typer.echo(f"  {path}")
    if report.started is True:
        typer.echo("\nthe service is running again")
    elif report.started is False:
        typer.echo("\nthe service did not start: `keryx doctor`, and its logs, say why")
    if report.units_pending:
        typer.echo("the service and the approval hook are not in place yet: `keryx migrate` "
                   "again finishes them")
