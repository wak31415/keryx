"""Keryx command-line interface."""

import asyncio
import contextlib
import dataclasses
import importlib.metadata
import json
import logging
import logging.handlers
import os
import shlex
import signal
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from pydantic import ValidationError

from keryx import plugins, units
from keryx.app import TASK_DB_NAME, AppState, build_app_state, shutdown_app_state
from keryx.approvals.audit_log import (
    entry_json,
    escalation_counts,
    escalation_enabled,
    read_entries,
    settled_with_escalation,
)
from keryx.approvals.broker import AUDIT_NAME, KILL_SWITCH_NAME, STATE_DIR_NAME
from keryx.config import (
    GROUPS,
    PLACEHOLDER_KEY,
    Settings,
    env_var_name,
    field_for,
    is_secret,
    load_settings,
    secure_dir,
    secure_file,
)
from keryx.config import verify as credentials
from keryx.config.files import HOME_ENV, XDG_HOMES, claude_config_dir, xdg_home
from keryx.config.migrate import MigrationError, make_plan, old_target, retire_old_units
from keryx.config.migrate import Report as MigrationReport
from keryx.config.migrate import Service as MigratingService
from keryx.config.migrate import run as run_migration
from keryx.config.options import has_options, options_for
from keryx.config.permissions import ACTOR_ENVS, SERVICE, current_actor
from keryx.config.pin import pin_file, read_enrolled_pin
from keryx.config.settings import ConfigFileError, tolerate_broken_files
from keryx.config.store import FROM_ENV, ConfigError, ConfigStore
from keryx.continuity.memory import memory_document, memory_path, read_memory
from keryx.continuity.retention import cutoff_for, prune, prune_with
from keryx.doctor import (
    Check,
    EndpointReach,
    fix_permissions,
    format_check,
    has_hard_failure,
    run_doctor_checks,
)
from keryx.events import EventBus
from keryx.integrations import accounts
from keryx.localmodels import (
    catalog,
    download,
    hardware,
    pull,
    runtimes,
    server,
    servers,
    use,
    voice,
)
from keryx.logging_util import mask_number
from keryx.notify.twilio_out import RestTwilioAdmin, TwilioAdmin
from keryx.plugins import ssh_hosts
from keryx.realtime import make_provider
from keryx.restart.health import health_probe, wait_until_serving
from keryx.restart.logscan import errors_since
from keryx.restart.logscan import marks as log_marks
from keryx.restart.service import (
    INSTALLERS,
    UNSUPPORTED_HINT,
    candidate_target,
    resolve_target,
    spawn_watchdog,
    watch_command,
)
from keryx.restart.store import RECORD_NAME, RestartRecord, RestartStore
from keryx.restart.version import loaded_version, mark_running, mark_startup_logs
from keryx.restart.watchdog import watch
from keryx.server import create_app
from keryx.session import VoiceSession
from keryx.setup import auth as auth_flow
from keryx.setup import auth_stream, profile
from keryx.setup import pin as pin_flow
from keryx.setup.agents import is_headless
from keryx.setup.context import SetupContext, run_command
from keryx.setup.plan import StepRefused, build_plan, failed_plan, format_plan, mark_step
from keryx.setup.sections import repo_root
from keryx.setup.ui import Aborted
from keryx.setup.wizard import agent_instructions, run_wizard
from keryx.status import build_status, failed_status, format_status, service_running
from keryx.tasks.export import read_log, task_json, told
from keryx.tasks.manager import install_default_executor
from keryx.tasks.models import ProjectUsage, Task, TaskStatus, task_log_path
from keryx.tasks.store import TaskStore
from keryx.tools import ToolRegistry
from keryx.tools.builtin import BUILTIN_TOOL_NAMES
from keryx.tools.custom import load_custom_tools
from keryx.transports.wav import WavTransport

# No locals in a crash: `pin set` holds the digits in one.
app = typer.Typer(help="Keryx voice agent.", pretty_exceptions_show_locals=False)
tasks_app = typer.Typer(help="Inspect the tasks handed to subagents.")
app.add_typer(tasks_app, name="tasks")
config_app = typer.Typer(
    help="Read and change Keryx's settings (KERYX_HOME/config.toml and secrets.toml)."
)
app.add_typer(config_app, name="config")
auth_app = typer.Typer(help="Sign in to what Keryx needs: the coding agents and Google.")
app.add_typer(auth_app, name="auth")
memory_app = typer.Typer(help="What Keryx remembers between calls.")
app.add_typer(memory_app, name="memory")
plugins_app = typer.Typer(
    help="The optional voice tools (Slack, email, billing, cluster stats): on, off, set up."
)
app.add_typer(plugins_app, name="plugins")
models_app = typer.Typer(
    help="Models on this machine: which fit, downloading one, and the servers that run them."
)
app.add_typer(models_app, name="models")
voice_app = typer.Typer(
    help="The voice on a call: OpenAI's Realtime API, speech-to-speech here, or a server "
    "elsewhere."
)
app.add_typer(voice_app, name="voice")
voice_use_app = typer.Typer(
    help="Make OpenAI, a voice server elsewhere, or this machine the voice."
)
voice_app.add_typer(voice_use_app, name="use")
service_app = typer.Typer(
    help="Keryx's services — itself, its tunnel, the model and voice servers: whether each is "
    "installed and running, and installing or removing them."
)
app.add_typer(service_app, name="service")
setup_app = typer.Typer(
    help="Set Keryx up: the guided walk, and the plan the Mac app's assistant follows."
)
app.add_typer(setup_app, name="setup")
pin_app = typer.Typer(
    help="The phone PIN: whether one is set, and setting or replacing it (the owner's alone)."
)
app.add_typer(pin_app, name="pin")
log = logging.getLogger("keryx.cli")


#: What the old command says, for one release (`pyproject.toml`'s `jarvis` script).
RENAMED_MESSAGE = (
    "Jarvis is now Keryx: run `keryx migrate`, then `keryx` wherever you ran `jarvis`."
)


def renamed() -> None:
    """The `jarvis` command: whatever it was asked, say what happened and fail.

    It fails rather than forwarding, so a unit installed before the rename stops in a
    restart loop that names the fix, instead of starting a service from the old directories.
    """
    typer.echo(RENAMED_MESSAGE, err=True)
    raise SystemExit(1)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"keryx {importlib.metadata.version('keryx-voice')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Keryx voice agent."""


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

TIME_FORMAT = "%Y-%m-%d %H:%M"
#: How much of a task description fits in the `tasks list` table.
MAX_DESCRIPTION_CHARS = 60
#: How much of a report `tasks show` prints before it starts truncating.
MAX_REPORT_CHARS = 4000
#: `keryx memory` prints the whole document, not the slice a prompt gets, so this is only
#: a backstop against a memory that has run away.
MAX_MEMORY_PRINT_CHARS = 100_000


#: What `doctor` puts in place of a field whose configured value does not validate, so a
#: broken install still gets a report rather than a traceback. A field not listed here is
#: one `doctor` cannot work around, and it re-raises.
DOCTOR_FALLBACKS: dict[str, object] = {"openai_api_key": PLACEHOLDER_KEY, "pin": None}
#: Every name a refused value can arrive under, mapped back to its field. `pydantic`
#: reports a field's *alias* when it has one — a bad `KERYX_PIN` comes back as
#: `KERYX_PIN`, where a bad `OPENAI_API_KEY` comes back as `openai_api_key` — so matching
#: on the field name alone quietly stopped `doctor` working in the one state it is for.
DOCTOR_FALLBACK_NAMES: dict[str, str] = {
    name: field
    for field in DOCTOR_FALLBACKS
    for name in (field, env_var_name(field))
}


def _validation_message(detail: Mapping[str, Any]) -> str:
    """One pydantic error as a sentence, without its framing."""
    return str(detail.get("msg", "is not valid")).removeprefix("Value error, ")


def _config_summary(error: ValidationError) -> str:
    """A `ValidationError` in the env-var names whoever set them would recognise."""
    parts = []
    for detail in error.errors():
        field = str(detail["loc"][0]) if detail.get("loc") else ""
        name = env_var_name(field) if field else "the configuration"
        parts.append(f"{name} {_validation_message(detail)}")
    return "; ".join(parts) or "the configuration is not valid"


def _configure(**overrides: object) -> Settings:
    """Load settings (with any command-line overrides), make the data dirs, set up logging."""
    try:
        settings = load_settings()
    except ConfigFileError as error:
        typer.echo(f"keryx cannot start: {error}", err=True)
        raise typer.Exit(2) from None
    except ValidationError as error:
        # This is the path `keryx serve` takes, so this message is what somebody reads in
        # `journalctl` after a restart failed to bring the service back. A traceback would
        # tell them nothing they could act on, and pydantic's own rendering of a rejected
        # `KERYX_PIN` is not something to put in a log at all.
        typer.echo(f"keryx cannot start: {_config_summary(error)}", err=True)
        typer.echo("run `keryx doctor` for the full picture.", err=True)
        raise typer.Exit(2) from error
    if overrides:
        settings = settings.model_copy(update=overrides)
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    return settings


def _load_settings_reporting() -> tuple[Settings, dict[str, str]]:
    """Settings for the read-only commands, plus why any field had to be given up on.

    `doctor` has to run *because* the install is incomplete, and `tasks` never talks to
    OpenAI at all — so a field in `DOCTOR_FALLBACKS` is replaced rather than
    allowed to raise. Each replacement is recorded against its field name, because "not
    set" and "set to something unusable" are different problems and `doctor` has to be
    able to tell them apart. Anything else still raises: an unreportable error beats a
    report built on a guess.
    """
    problems: dict[str, str] = {}
    overrides: dict[str, object] = {}
    tolerant = None
    try:
        while True:
            try:
                return load_settings(**overrides), problems
            except ConfigFileError as error:
                if "config_file" in problems:
                    raise
                # Read on without the broken file; doctor's `config.toml` check says what
                # broke. Only for this load: the next command in the process reads it again.
                problems["config_file"] = str(error)
                tolerant = tolerate_broken_files.set(True)
            except ValidationError as error:
                fresh = {
                    field: _validation_message(detail)
                    for detail in error.errors()
                    if (
                        field := DOCTOR_FALLBACK_NAMES.get(
                            str(detail["loc"][0]) if detail.get("loc") else "", ""
                        )
                    )
                    and field not in overrides
                }
                if not fresh:
                    raise
                problems.update(fresh)
                overrides.update({field: DOCTOR_FALLBACKS[field] for field in fresh})
    finally:
        if tolerant is not None:
            tolerate_broken_files.reset(tolerant)


def _load_settings_optional() -> Settings:
    """`_load_settings_reporting` for the callers that only want the settings."""
    return _load_settings_reporting()[0]


def _configure_readonly(
    *, quiet: bool = True, migrated: bool = True, refuse: Callable[[str], None] | None = None
) -> Settings:
    """`_configure` for the read-only commands (no `OPENAI_API_KEY` required).

    `quiet` because these print an answer, and `LOG_LEVEL` is the service's setting: the
    INFO lines it wants in `keryx.log` (a schema created, a migration run) are noise above
    a table. Warnings still show, and `LOG_LEVEL=DEBUG` still means everything.

    `migrated` refuses, like `serve`, while `keryx migrate` has yet to run: before it, the
    data these commands read is still in the old place, so they would answer from an empty
    one — and `keryx tasks list` would create a new `tasks.db` for the migration to trip
    over. The restart watchdog, which must report whatever it finds, passes False, and so
    does `setup`, whose wizard says it in its own words.

    `refuse` is told each way this stops — the refusal above, or settings that will not
    load — in place of stderr, before the same exit code: `models pull --json`, whose reader
    has only stdout. Without it, settings that will not load raise, as they always have.
    """
    try:
        settings = _load_settings_optional()
    except (ConfigFileError, ValidationError) as error:
        if refuse is None:
            raise
        refuse(_config_summary(error) if isinstance(error, ValidationError) else str(error))
        raise typer.Exit(1) from None
    if migrated and (refusal := settings.storage_refusal()):
        message = f"keryx cannot do that yet: {refusal}"
        if refuse is None:
            typer.echo(message, err=True)
        else:
            refuse(message)
        raise typer.Exit(2)
    settings.ensure_dirs()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    if quiet and level != logging.DEBUG:
        level = max(level, logging.WARNING)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return settings


def _add_file_logging(settings: Settings) -> None:
    """Also log to `state_dir/logs/keryx.log`, rotated, alongside the console handler."""
    log_dir = secure_dir(settings.state_dir / "logs")
    root = logging.getLogger()
    rotating = logging.handlers.RotatingFileHandler
    for existing in [h for h in root.handlers if isinstance(h, rotating)]:
        root.removeHandler(existing)  # a second `serve` in one process replaces, not stacks
        existing.close()
    handler = rotating(
        log_dir / "keryx.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)


@app.command()
def serve(
    no_wakeword: Annotated[
        # Accepted and ignored: service units installed before the wake word left `main`
        # still pass it, and a restart must not fail on an unknown option.
        bool, typer.Option("--no-wakeword", hidden=True)
    ] = False,
    demo: Annotated[
        bool,
        typer.Option(
            "--demo",
            help="Try the phone before a coding agent is set up: every task comes back with "
            "a sample answer, and no agent tokens are spent.",
        ),
    ] = False,
    fake_agents: Annotated[
        # Its name until 2026-09-29, kept for one release.
        bool, typer.Option("--fake-agents", hidden=True)
    ] = False,
    host: Annotated[
        str | None, typer.Option("--host", help="Override HOST for the phone server.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Override PORT for the phone server.")
    ] = None,
) -> None:
    """Run Keryx: the Twilio phone server."""
    # Only pass overrides that were actually asked for; everything else is the store's.
    overrides: dict[str, object] = {}
    if demo or fake_agents:
        overrides["demo_mode"] = True
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    settings = _configure(**overrides)
    if refusal := settings.phone_refusal():
        typer.echo(f"keryx cannot start: {refusal}", err=True)
        raise typer.Exit(2)
    if refusal := settings.agent_refusal():
        typer.echo(f"keryx cannot start: {refusal}", err=True)
        raise typer.Exit(2)
    if refusal := settings.voice_refusal():
        typer.echo(f"keryx cannot start: {refusal}", err=True)
        raise typer.Exit(2)
    if refusal := settings.storage_refusal():
        typer.echo(f"keryx cannot start: {refusal}", err=True)
        raise typer.Exit(2)
    _add_file_logging(settings)
    if leftover := plugins.retired_in(ConfigStore()):
        # Ignored, never fatal: they are plugin settings now, and the plugin is off until
        # they are moved into its file.
        log.warning(
            "config.toml still holds %s, which are plugin settings now: "
            "`keryx plugins install --from-settings`",
            ", ".join(leftover),
        )
    # Everything this process starts — every subagent — is the service, not the owner, when
    # it runs `keryx config set` (keryx.config.permissions), and under the old name too.
    for name in ACTOR_ENVS:
        os.environ[name] = SERVICE
    asyncio.run(_serve(settings))


async def _serve(settings: Settings) -> None:
    """Run the phone server until it stops or ctrl-c."""
    # First, before anything can commit on top of us: the next restart compares against
    # this to say whether it loaded anything, and the checkout will have moved by then.
    mark_running(settings.state_dir)
    # And before we log a line of our own: everything past here is this process's doing,
    # which is what the confirmation call should be reading. See `mark_startup_logs`.
    mark_startup_logs(settings.state_dir)
    # Every running Codex turn parks threads in this executor; see `executor_workers`.
    install_default_executor(settings)
    state = build_app_state(settings)
    # Before anything writes: retention is off by default, so on most installs this looks
    # at two zeroes and returns. `memory.md` is trimmed either way — it is the one file
    # written by a subagent rather than by us.
    report = await prune(settings, state.store)
    if report:
        typer.echo(f"retention removed {report.describe()}")
    # The approval bridge binds a Unix socket, so only a serving process ever starts it;
    # a failure to bind is logged and the bridge simply stays off (keryx/approvals).
    if state.approvals is not None:
        await state.approvals.start()
    server = _build_server(state)
    server_task = asyncio.create_task(server.serve(), name="phone-server")
    typer.echo(f"phone server on http://{settings.host}:{settings.port}")

    # If this process is the far side of a restart, confirm it — by itself, once the phone
    # server is really listening. It is deliberately not one of the `tasks` below: finishing
    # is what it does, and that must not bring the server down with it.
    callback_task = asyncio.create_task(
        _confirm_then_drain(state, ready=lambda: wait_until_serving(server)),
        name="restart-callback",
    )

    def shutdown() -> None:
        """Stop the server; uvicorn asks for `should_exit`."""
        server.should_exit = True

    _run_on_signals(shutdown)
    try:
        await server_task
    except Exception:
        log.exception("the phone server failed")
    finally:
        shutdown()
        callback_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await callback_task
        # Subagents outlive a session but not the process: stop them and close the store.
        await shutdown_app_state(state)


async def _confirm_then_drain(state: AppState, *, ready) -> None:
    """Confirm the restart, then pick up the work the last process never got to.

    In that order, and never the other way round: the confirmation counts what was left
    `queued`, and picking those up first would have it counting tasks this process had
    already started as ones the restart interrupted. Neither half may take the server down
    with it, so both are guarded here rather than left to the caller.
    """
    if state.restart is not None:
        await state.restart.resume(wait_ready=ready)
    if state.manager is None:
        return
    try:
        resumed = await state.manager.resume_queued()
    except Exception:
        log.exception("could not pick up the tasks the last process left queued")
        return
    if resumed:
        typer.echo(f"picked up {len(resumed)} task(s) the last process never started")


def _build_server(state: AppState) -> uvicorn.Server:
    """The Twilio-facing HTTP server, run from inside our own event loop.

    `lifespan="off"`: the app has no startup/shutdown hooks, and everything it needs is
    already built in `AppState`.
    """
    settings = state.settings
    config = uvicorn.Config(
        create_app(state),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        lifespan="off",
    )
    return uvicorn.Server(config)


def _run_on_signals(callback) -> None:
    """Turn ctrl-c / SIGTERM into a call to `callback`.

    `uvicorn.Server.serve()` only installs its own handlers when it runs the loop itself
    (`server.run()`), which it does not here — so this is the only thing that stops it.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, callback)


# --- restart ---------------------------------------------------------------


def _restart_store(settings: Settings) -> RestartStore:
    return RestartStore(settings.state_dir / RECORD_NAME)


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
    if not store.save(record):
        typer.echo(f"could not write {store.path}: the restart would go unconfirmed")
        raise typer.Exit(1)

    if number:
        # The same watch the voice path arms, for the same reason: this command prints a
        # promise that keryx will call back, and nothing else would notice if it never
        # came back to make the call.
        record.watchdog = _arm_watchdog(settings, target)
        store.save(record)
        typer.echo(f"watchdog: {record.watchdog}")

    command = target.command()
    typer.echo(" ".join(command))
    code = subprocess.run(command, check=False).returncode
    if code != 0:
        record.state = "failed"
        record.error = f"{command[0]} exited {code}"
        store.save(record)
        typer.echo(f"the restart failed: {record.error}")
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


@app.command()
def loopback(
    wav: Annotated[
        Path,
        typer.Option(
            "--wav", exists=True, dir_okay=False, help="16-bit WAV to play at the model."
        ),
    ],
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the reply audio.")
    ] = Path("reply.wav"),
    tail_seconds: Annotated[
        float, typer.Option("--tail-seconds", help="Seconds of silence to feed after the file.")
    ] = 8.0,
) -> None:
    """Run one session from a WAV file: the dev harness for the provider without a mic."""
    settings = _configure()
    asyncio.run(_run_loopback(settings, wav, out, tail_seconds))


async def _run_loopback(settings: Settings, wav: Path, out: Path, tail_seconds: float) -> None:
    transport = WavTransport(wav, out_path=out, tail_seconds=tail_seconds)
    session = VoiceSession(
        transport,
        make_provider(settings),
        settings,
        ToolRegistry(),
        EventBus(),
        authorized=True,
    )
    await session.run()

    typer.echo(f"reply audio: {out}")
    typer.echo(f"transcript: {session.transcript_path}")
    if session.transcript_path.exists():
        typer.echo(session.transcript_path.read_text().rstrip())


# --- tasks -----------------------------------------------------------------


class StatusFilter(StrEnum):
    """`--status` values: every `TaskStatus`, plus `all` for no filter."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ALL = "all"


def _local_time(value: datetime | None) -> str:
    """A UTC timestamp as local wall-clock time, the way a human reads it."""
    return value.astimezone().strftime(TIME_FORMAT) if value is not None else ""


def _shorten(text: str, limit: int) -> str:
    """`text` clipped to `limit` characters, ellipsis included."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


@contextlib.asynccontextmanager
async def _open_store(settings: Settings) -> AsyncIterator[TaskStore]:
    """The task store for a read-only CLI command: `tasks` never starts a manager or a
    subagent, it just reads straight from the store and always closes it after."""
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        yield store
    finally:
        await store.close()


async def _read_tasks(
    settings: Settings, status: TaskStatus | None, limit: int, include_internal: bool
) -> list[Task]:
    async with _open_store(settings) as store:
        return await store.list(
            status=status, limit=limit, include_internal=include_internal
        )


async def _read_task(settings: Settings, task_id: int) -> Task | None:
    async with _open_store(settings) as store:
        return await store.get(task_id)


@tasks_app.command("list")
def tasks_list(
    status: Annotated[
        StatusFilter, typer.Option("--status", help="Only show tasks in this status.")
    ] = StatusFilter.ALL,
    limit: Annotated[int, typer.Option("--limit", help="How many tasks to show.")] = 20,
    include_internal: Annotated[
        bool,
        typer.Option(
            "--internal",
            help="Also show Keryx's own housekeeping (the per-call memory updates).",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """List recent tasks, newest first."""
    settings = _configure_readonly()
    wanted = None if status is StatusFilter.ALL else TaskStatus(status.value)
    tasks = asyncio.run(_read_tasks(settings, wanted, limit, include_internal))
    if as_json:
        typer.echo(json.dumps({"tasks": [task_json(task) for task in tasks]}, indent=2))
        return
    if not tasks:
        typer.echo("no tasks" if wanted is None else f"no tasks with status {wanted}")
        return

    typer.echo(
        f"{'ID':>4}  {'STATUS':<9}  {'CREATED':<16}  {'AGENT':<6}  {'TOLD':<5}  DESCRIPTION"
    )
    for task in tasks:
        typer.echo(
            f"{task.id:>4}  {task.status:<9}  "
            f"{_local_time(task.created_at):<16}  "
            f"{task.agent:<6}  "
            f"{_reported_flag(task):<5}  "
            f"{_shorten(task.description, MAX_DESCRIPTION_CHARS)}"
        )


def _reported_flag(task: Task) -> str:
    """Whether Keryx has told them about this one: only meaningful once it has finished.

    Goes through `told()` so the table and the JSON export can never disagree.
    """
    if task.internal:
        return "-"
    flag = told(task)
    if flag is None:
        return ""
    return "yes" if flag else "NO"


@tasks_app.command("show")
def tasks_show(
    task_id: Annotated[int, typer.Argument(help="The task id to show.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """Show everything stored about one task, including its report."""
    settings = _configure_readonly()
    task = asyncio.run(_read_task(settings, task_id))
    if task is None:
        typer.echo(f"no task {task_id}", err=True)
        raise typer.Exit(1)

    if as_json:
        report, truncated = (
            _read_report(Path(task.report_path)) if task.report_path else (None, False)
        )
        log_entries, log_truncated = read_log(task_log_path(settings.data_dir, task.id))
        typer.echo(
            json.dumps(
                {
                    "task": task_json(task),
                    "report": report,
                    "report_truncated": truncated,
                    "log": log_entries,
                    "log_truncated": log_truncated,
                    "resumable": bool(task.claude_session_id),
                },
                indent=2,
            )
        )
        return

    width = max(len(field.name) for field in dataclasses.fields(task))
    for field in dataclasses.fields(task):
        value = getattr(task, field.name)
        printable = _local_time(value) if isinstance(value, datetime) else value
        typer.echo(f"{field.name:<{width}}  {'' if printable is None else printable}")

    if task.report_path:
        _echo_report(Path(task.report_path))


#: What `tasks usage` calls the two groups that carry no project name of their own.
NO_PROJECT_LABEL = "(no project)"
HOUSEKEEPING_LABEL = "(housekeeping)"
#: Printed under the table, because every column in it is easy to over-read.
USAGE_FOOTNOTE = (
    "Attributed by the project each task was dispatched with; \"(no project)\" is work "
    "started in the projects folder, which may still have touched one. COST is what the "
    "agent reported: Claude's list-price estimate, which a subscription login is not "
    "billed per token, and nothing for Codex. PRICED counts the tasks that reported one. "
    "Voice calls are not tasks and are not here; check_billing has the provider's total."
)


async def _read_usage(settings: Settings, since: datetime | None) -> list[ProjectUsage]:
    async with _open_store(settings) as store:
        return await store.usage_by_project(since=since)


def _usage_label(usage: ProjectUsage) -> str:
    if usage.internal:
        return HOUSEKEEPING_LABEL
    return usage.project or NO_PROJECT_LABEL


def _usage_line(label: str, usage: ProjectUsage) -> str:
    cost = "-" if usage.cost_usd is None else f"${usage.cost_usd:,.2f}"
    tokens = usage.measured > 0
    return (
        f"{_shorten(label, 28):<28}  {usage.tasks:>5}  {f'{usage.priced}/{usage.tasks}':>7}  "
        f"{f'{usage.input_tokens:,}' if tokens else '-':>13}  "
        f"{f'{usage.output_tokens:,}' if tokens else '-':>11}  {cost:>9}"
    )


@tasks_app.command("usage")
def tasks_usage(
    days: Annotated[
        int,
        typer.Option("--days", min=0, help="Tasks created in the last N days; 0 for all of them."),
    ] = 30,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """What the tasks spent, per project: tokens and the agents' own cost estimates."""
    settings = _configure_readonly()
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    groups = asyncio.run(_read_usage(settings, since))
    if as_json:
        typer.echo(json.dumps({
            "since": since.isoformat() if since else None,
            "projects": [
                {"label": _usage_label(usage), **dataclasses.asdict(usage)} for usage in groups
            ],
        }, indent=2))
        return
    window = f"in the last {days} day{'s' if days != 1 else ''}" if days else "ever"
    if not groups:
        typer.echo(f"no tasks created {window}")
        return

    typer.echo(f"Tasks created {window}, by project:\n")
    typer.echo(
        f"{'PROJECT':<28}  {'TASKS':>5}  {'PRICED':>7}  {'INPUT TOK':>13}  "
        f"{'OUTPUT TOK':>11}  {'COST':>9}"
    )
    for usage in groups:
        typer.echo(_usage_line(_usage_label(usage), usage))
    typer.echo(_usage_line("total", sum(groups[1:], groups[0])))
    typer.echo("")
    typer.echo(textwrap.fill(USAGE_FOOTNOTE, width=88))


def _read_report(path: Path) -> tuple[str | None, bool]:
    """`path`'s contents sliced to `MAX_REPORT_CHARS`, and whether that cut anything off.

    `(None, False)` when the report cannot be read (most often: it is gone) — the caller
    decides how to say that. Shared by `_echo_report` and `tasks show --json`, so the two
    cannot disagree on where a report gets cut.
    """
    try:
        body = path.read_text(errors="replace")
    except OSError:
        return None, False
    return body[:MAX_REPORT_CHARS], len(body) > MAX_REPORT_CHARS


def _echo_report(path: Path) -> None:
    """Print the task's written report, truncated; a report that is gone is simply skipped."""
    body, truncated = _read_report(path)
    if body is None:
        typer.echo("(report unreadable)")
        return
    typer.echo("\n--- report ---")
    typer.echo(body.rstrip())
    if truncated:
        typer.echo(f"… (truncated at {MAX_REPORT_CHARS} characters; full report: {path})")


# --- memory ----------------------------------------------------------------


@memory_app.callback(invoke_without_command=True)
def memory(
    ctx: typer.Context,
    path_only: Annotated[
        bool, typer.Option("--path", help="Print where the memory lives and nothing else.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Print what Keryx remembers between calls.

    The file a subagent rewrites after every call and every session reads back at the top
    of its prompt. It is plain markdown and safe to edit by hand — the next update merges
    around whatever is there.
    """
    if ctx.invoked_subcommand is not None:
        return
    if path_only and as_json:
        typer.echo("pick one of --path and --json", err=True)
        raise typer.Exit(code=2)
    settings = _configure_readonly()
    path = memory_path(settings.data_dir)
    if path_only:
        typer.echo(str(path))
        return
    if as_json:
        document = memory_document(settings.data_dir, max_chars=MAX_MEMORY_PRINT_CHARS)
        typer.echo(json.dumps(document, indent=2))
        return

    text = read_memory(settings.data_dir, max_chars=MAX_MEMORY_PRINT_CHARS)
    if not text:
        typer.echo(f"nothing remembered yet ({path} does not exist)")
        return
    typer.echo(f"# {path}\n")
    typer.echo(text)


@memory_app.command("seed")
def memory_seed(
    file: Annotated[
        str, typer.Option("--file", help="The facts, one per line; - is stdin.")
    ],
    force: Annotated[
        bool, typer.Option("--force", help="Replace a memory that already has something in it.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print what every call will carry, as JSON.")
    ] = False,
) -> None:
    """Write a first memory from standing facts, before any call has.

    For an agent: `--file - --json` takes the facts on stdin and prints one JSON document.
    Exit 0 means the memory was written, or nothing was given to write; exit 1 means a
    memory was wanted and not written (one is there without --force, or it is longer than a
    call reads); exit 2 means the command line itself is wrong.
    """
    _owner_only("seed the memory")
    settings = _configure_readonly()
    try:
        facts = profile.read_facts(file)
    except OSError as exc:
        typer.echo(f"could not read {file}: {exc}", err=True)
        raise typer.Exit(2) from None
    say = (lambda _line: None) if as_json else typer.echo
    status = profile.seed(settings, facts, force=force, echo=say)
    if as_json:
        typer.echo(json.dumps(profile.setup_summary(settings, status=status), indent=2))
    else:
        for line in profile.setup_report(settings):
            typer.echo(line)
    if code := profile.exit_code(status):
        raise typer.Exit(code)


@app.command("tools")
def tools_command(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """List your own voice tools (DATA_DIR/tools) and any file the next call would refuse.

    The same load a call makes, against every built-in tool's name, so a name clash or a
    file that will not import shows up here first. Exits 1 when anything was refused. A
    new or edited tool is offered from the next call, with no restart.

    Nothing of the service's is built here: a subagent runs this inside the live service's
    environment, and the application's own objects own its socket and its records.
    """
    settings = _configure_readonly()
    directory = settings.custom_tools_dir
    loaded = load_custom_tools(directory, taken=BUILTIN_TOOL_NAMES)
    if as_json:
        typer.echo(json.dumps({
            "directory": str(directory),
            "tools": [
                {"name": tool.name, "needs_pin": tool.needs_pin, "file": path.name,
                 "description": tool.description}
                for path, tool in loaded.tools
            ],
            "refused": [{"file": path.name, "reason": why} for path, why in loaded.errors],
        }, indent=2))
    else:
        typer.echo(f"# {directory}")
        if not loaded.tools and not loaded.errors:
            typer.echo("no custom tools")
        for path, tool in loaded.tools:
            pin = "needs PIN" if tool.needs_pin else "no PIN"
            typer.echo(f"{tool.name:<24} {pin:<10} {path.name}")
        for path, why in loaded.errors:
            typer.echo(f"refused {path.name}: {why}")
    if loaded.errors:
        raise typer.Exit(1)


# --- plugins ---------------------------------------------------------------

PLUGINS_JSON_HELP = (
    "Print one JSON document; each plugin also says whether it is `ready` — would work on a "
    "call once turned on (`on` says whether it is) — and `why` not."
)


@plugins_app.callback(invoke_without_command=True)
def plugins_command(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help=PLUGINS_JSON_HELP)] = False,
) -> None:
    """List the plugins: on or off, their two files, and why one is refused."""
    if ctx.invoked_subcommand is None:
        plugins_list(as_json)


@models_app.command("list")
def models_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The models Keryx can download, which fit this machine, and the local voices."""
    settings = _configure_readonly()
    machine = hardware.detect()
    reserve = catalog.VOICE_SERVER_GB if settings.voice_base_url else 0.0
    best = catalog.recommend(machine, reserve_gb=reserve)
    rows = [
        {
            "name": entry.key,
            "title": entry.title,
            "size_gb": entry.size_gb,
            "needs_gb": entry.memory_gb,
            "fits": catalog.fits(entry, machine, reserve_gb=reserve),
            "downloaded": download.is_downloaded(settings.cache_dir, entry),
            "recommended": entry is best,
            "tested": entry.tested,
            "note": entry.note,
        }
        for entry in catalog.MODELS
    ]
    if as_json:
        typer.echo(json.dumps({
            "hardware": {**dataclasses.asdict(machine), "model_memory_gb":
                         machine.model_memory_gb},
            "models_dir": str(download.models_dir(settings.cache_dir)),
            "models": rows,
            "voices": [{"name": name, "sounds": sounds} for name, sounds in catalog.VOICES],
            "servers": use.server_status(settings, installed=use.unit_installed,
                                         probe=use.probe, which=runtimes.which),
        }, indent=2))
        return
    typer.echo(f"# {machine.describe()}; models in {download.models_dir(settings.cache_dir)}")
    for row in rows:
        marks = "".join([
            "✓ fits " if row["fits"] else "✗ too big ",
            "· downloaded " if row["downloaded"] else "",
            "· recommended " if row["recommended"] else "",
            "· tested " if row["tested"] else "",
        ])
        typer.echo(f"{row['name']:<22} {row['size_gb']:>5} GB  needs {row['needs_gb']:>3} GB  "
                   f"{marks}— {row['note']}")
    typer.echo("\n# voices for the local voice server (VOICE_SERVER_VOICE)")
    for name, sounds in catalog.VOICES:
        typer.echo(f"{name:<22} {sounds}")


@models_app.command("pull")
def models_pull(
    name: Annotated[str, typer.Argument(help="A name from `keryx models list`.")],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: progress, then how it ended."),
    ] = False,
) -> None:
    """Download one model into CACHE_DIR/models, picking up a download that was cut off."""
    if not as_json:
        raise typer.Exit(pull.pull(_configure_readonly(), name, pull.TextReport(typer.echo)))
    events = pull.JsonLines(typer.echo)
    with pull.terminate_as_interrupt():
        code = events.guard(
            lambda: pull.pull(_configure_readonly(refuse=events.error), name, events)
        )
    raise typer.Exit(code)


@models_app.command("use")
def models_use(
    name: Annotated[str, typer.Argument(help="A downloaded model, from `keryx models list`.")],
    default_agent: Annotated[
        bool,
        typer.Option("--default-agent",
                     help="Also make the local agent the one that runs what nobody named."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make a downloaded model the local agent's, served here by llama.cpp.

    It saves what `keryx setup` would; the model server and Keryx pick it up when restarted.
    """
    _owner_only("choose the local agent's model")
    settings = _configure_readonly()
    entry = catalog.by_key(name)
    if entry is None:
        typer.echo(f"there is no model called {name} (`keryx models list`)", err=True)
        raise typer.Exit(2)
    if not download.is_downloaded(settings.cache_dir, entry):
        typer.echo(f"download it first: keryx models pull {name}", err=True)
        raise typer.Exit(1)
    values = use.llama_values(settings, entry.key, entry.key, free_port=servers.free_port)
    values |= use.enable_local(settings, default=default_agent)
    if not as_json:
        _store_values(values)
        typer.echo("next: start it with `keryx models start`, then `keryx restart`")
        return
    saved = _store_values(values, echo=False)
    typer.echo(json.dumps({
        "saved": saved,
        "llama_server": runtimes.llama_server(settings.cache_dir),
        "restart_needed": True,
    }, indent=2))


@models_app.command("serve")
def models_serve(
    kind: Annotated[str, typer.Argument(help="llm (llama.cpp) or voice (speech-to-speech).")],
) -> None:
    """Become the local llm or voice server, as the settings describe it (what the units run)."""
    settings = _configure()
    try:
        servers.serve(kind, settings)
    except servers.ServerConfigError as error:
        typer.echo(f"keryx cannot start the {kind} server: {error}", err=True)
        raise typer.Exit(2) from None


#: What `models start|stop` are, refused to the service: they install and remove units.
RUN_THE_MODEL_SERVER = "start or stop the model server"


@models_app.command("start")
def models_start(
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="llama (llama.cpp, installed when it is missing) or "
                     "ollama (its own copy of the model, pulled)."),
    ] = server.LLAMA,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: each step, then how it ended."),
    ] = False,
) -> None:
    """Run the chosen model's server here, and check that it serves that model.

    llama.cpp is installed when it is missing, then the model server's unit, then Keryx
    waits for the model to load. The model comes first: `keryx models pull NAME`, then
    `keryx models use NAME`. A server that does not come up has its unit removed.
    """
    if not as_json:
        _owner_only(RUN_THE_MODEL_SERVER)
        try:
            code = _models_start(_configure_readonly(), runtime, server.TextSteps(typer.echo))
        except KeyboardInterrupt:
            typer.echo("cancelled: `keryx models start` again picks up where this stopped",
                       err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = server.JsonSteps(typer.echo)

    def run() -> int:
        # Refused before any install, and as an event: the app reads stdout alone.
        if refusal := _owner_refusal(RUN_THE_MODEL_SERVER):
            events.error(refusal)
            return 1
        return _models_start(_configure_readonly(refuse=events.error), runtime, events)

    with pull.terminate_as_interrupt():
        code = events.guard(run)
    raise typer.Exit(code)


def _models_start(settings: Settings, runtime: str, report: server.StartReport) -> int:
    if runtime not in server.RUNTIMES:
        report.error(f"--runtime is {' or '.join(server.RUNTIMES)}, not {runtime}")
        return voice.REFUSED
    return server.start(settings, server.machine(), report, runtime=runtime,
                        save=_saver(ConfigStore(), settings, report), root=repo_root())


@models_app.command("stop")
def models_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Stop the model server and remove its unit; `keryx models start` brings it back."""
    _install_units(llm_unit=True, uninstall=True, as_json=as_json, verb=RUN_THE_MODEL_SERVER)


@voice_app.callback(invoke_without_command=True)
def voice_command(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Where the voice on a call is, and the voice server on this machine."""
    if ctx.invoked_subcommand is not None:
        return
    settings = _configure_readonly()
    document = voice.status(settings, program=runtimes.speech_to_speech,
                            installed=use.unit_installed, probe=use.probe,
                            server_reachable=voice.server_reachable)
    if as_json:
        typer.echo(json.dumps(document, indent=2))
        return
    yes = {True: "yes", False: "no"}
    if document["mode"] == voice.HERE:
        typer.echo(f"the voice: speech-to-speech on this machine: {document['base_url']}")
    elif document["mode"] == voice.SERVER:
        typer.echo(f"the voice: a voice server elsewhere: {document['base_url']} "
                   f"(answers: {yes[document['server']['reachable']]})")
    else:
        typer.echo(f"the voice: OpenAI's Realtime API, {document['realtime_model']}, "
                   f"speaking as {document['voice']}"
                   + ("" if document["openai_key_set"] else " — OPENAI_API_KEY is not set"))
    here = document["here"]
    typer.echo(f"speech-to-speech here: {here['program'] or 'not installed'}; voice "
               f"{here['voice']} on port {here['port']}; unit installed: "
               f"{yes[here['unit_installed']]}; answers: {yes[here['reachable']]}")
    typer.echo(f"its words: {here['words_model'] or voice.NO_WORDS}")


#: What `voice use` is, refused to the service: `VOICE_BASE_URL` is protected.
CHOOSE_THE_VOICE = "choose the voice"


def _shown(saved: dict[str, Any]) -> dict[str, Any]:
    """What was saved, a secret's value as "(set)": never the value itself."""
    def secret(key: str) -> bool:
        name = field_for(key)
        return name is not None and is_secret(name)

    return {key: "(set)" if value is not None and secret(key) else value
            for key, value in saved.items()}


@voice_use_app.command("openai")
def voice_use_openai(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make OpenAI's Realtime API the voice again (it needs OPENAI_API_KEY)."""
    _owner_only(CHOOSE_THE_VOICE)
    settings = _configure_readonly()
    if as_json:
        saved = _store_values(voice.to_openai(), echo=False)
        typer.echo(json.dumps({"saved": _shown(saved), "restart_needed": True}, indent=2))
        return
    _store_values(voice.to_openai())
    typer.echo("calls use OpenAI's Realtime API again; `keryx restart` to have Keryx take it up")
    if not settings.openai_key:
        typer.echo("it needs OPENAI_API_KEY: keryx config set OPENAI_API_KEY --stdin")


@voice_use_app.command("server")
def voice_use_server(
    url: Annotated[str, typer.Argument(help="Its address: http://gpu-box:8765, or its …/v1.")],
    key_stdin: Annotated[
        bool, typer.Option("--key-stdin", help="Read its key from standard input.")
    ] = False,
    clear_key: Annotated[
        bool, typer.Option("--clear-key", help="Forget the key it had, and ask without one.")
    ] = False,
    keep_anyway: Annotated[
        bool, typer.Option("--keep-anyway", help="Save it although it did not answer.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make a voice server elsewhere the voice, once it has opened a Realtime session.

    One that does not is saved only with --keep-anyway. The key it has is kept, and asked
    with, unless --key-stdin replaces it or --clear-key forgets it; it is never on the
    command line.
    """
    _owner_only(CHOOSE_THE_VOICE)  # before the handshake, which carries the stored key
    settings = _configure_readonly()
    if key_stdin and clear_key:
        typer.echo("--key-stdin replaces the key and --clear-key forgets it: one of them",
                   err=True)
        raise typer.Exit(2)
    if key_stdin:
        key = _read_secret_input("VOICE_API_KEY")
        if not key:
            typer.echo("no key on stdin (--clear-key forgets the one it has)", err=True)
            raise typer.Exit(2)
    else:
        key = None if clear_key else settings.voice_api_key
    steps = voice.Silent() if as_json else voice.TextSteps(typer.echo)
    try:
        check = voice.check_server(url, key, probes=voice.machine(), report=steps)
    except ValueError as error:  # its own words, never the address: it may hold a password
        typer.echo(f"VOICE_BASE_URL {error}", err=True)
        raise typer.Exit(2) from None
    if check.problem is not None and not keep_anyway:
        if as_json:
            typer.echo(json.dumps({"saved": None, "problem": check.problem,
                                   "warnings": check.warnings, "restart_needed": False},
                                  indent=2))
        else:
            typer.echo(check.problem, err=True)
            typer.echo("nothing saved: calls would not open until it answers "
                       "(--keep-anyway saves it all the same)", err=True)
        raise typer.Exit(1)
    values = check.values(with_key=key_stdin or clear_key)
    if not as_json:
        _store_values(values)
        typer.echo("next: `keryx restart`, to have Keryx take it up")
        return
    saved = _store_values(values, echo=False)
    typer.echo(json.dumps({"saved": _shown(saved), "problem": check.problem,
                           "warnings": check.warnings, "restart_needed": True}, indent=2))


@voice_use_app.command("here")
def voice_use_here(
    name: Annotated[
        str | None,
        typer.Option("--voice", help="A Kokoro voice (`keryx models list`); else the one "
                     "chosen before, or the assistant's own."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: each step, then how it ended."),
    ] = False,
) -> None:
    """Install speech-to-speech here, start it, and make it the voice once it answers.

    What `keryx setup` does for a voice on this machine, through the same steps. Its words
    come from the local agent's model, so that comes first (`keryx models use NAME`).
    """
    if not as_json:
        _owner_only(CHOOSE_THE_VOICE)
        try:
            code = _voice_here(_configure_readonly(), name, voice.TextSteps(typer.echo))
        except KeyboardInterrupt:
            typer.echo("cancelled: the next `keryx voice use here` picks up where this stopped",
                       err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = voice.JsonSteps(typer.echo)

    def run() -> int:
        # Refused before any install, and as an event: the app reads stdout alone.
        if refusal := _owner_refusal(CHOOSE_THE_VOICE):
            events.error(refusal)
            return 1
        return _voice_here(_configure_readonly(refuse=events.error), name, events)

    with pull.terminate_as_interrupt():
        code = events.guard(run)
    raise typer.Exit(code)


def _voice_here(settings: Settings, name: str | None, report: voice.Report) -> int:
    if name is not None and (problem := voice.voice_name_problem(name)):
        report.error(problem)
        return voice.REFUSED
    store = ConfigStore()
    return voice.here(settings, voice.machine(), report, save=_saver(store, settings, report),
                      root=repo_root(),
                      choose_voice=lambda: name or voice.current_voice(settings, store))


def _saver(
    store: ConfigStore, settings: Settings, report: server.Report
) -> Callable[[dict[str, Any]], bool]:
    """Store values as whoever runs this; a refusal told to `report`, and False."""
    def save(values: dict[str, Any]) -> bool:
        try:
            store.set(values, actor=current_actor(), settings=settings)
        except ConfigError as error:
            report.error(str(error))
            return False
        return True

    return save


# --- service ---------------------------------------------------------------


#: What `service install|uninstall` is, refused to the service.
INSTALL_THE_SERVICES = "install or remove Keryx's services"


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
        typer.echo(f"{kind:<7} {name:<22} {state}")


@service_app.command("install")
def service_install(
    llm_unit: Annotated[
        bool, typer.Option("--llm", help="The model server's unit, instead of Keryx's.")
    ] = False,
    voice_unit: Annotated[
        bool, typer.Option("--voice", help="The voice server's unit, instead of Keryx's.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Install Keryx and its tunnel as services, or the local model servers' (--llm, --voice),
    through the checkout's installer."""
    _install_units(llm_unit=llm_unit, voice_unit=voice_unit, as_json=as_json)


@service_app.command("uninstall")
def service_uninstall(
    llm_unit: Annotated[
        bool, typer.Option("--llm", help="The model server's unit, instead of Keryx's.")
    ] = False,
    voice_unit: Annotated[
        bool, typer.Option("--voice", help="The voice server's unit, instead of Keryx's.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Stop and remove Keryx and its tunnel, or the local model servers' (--llm, --voice)."""
    _install_units(llm_unit=llm_unit, voice_unit=voice_unit, uninstall=True, as_json=as_json)


def _install_units(
    *, llm_unit: bool = False, voice_unit: bool = False, uninstall: bool = False,
    as_json: bool, verb: str = INSTALL_THE_SERVICES,
) -> None:
    _owner_only(verb)
    settings = _configure_readonly()
    outcome = units.install(settings=settings, llm=llm_unit, voice=voice_unit,
                            uninstall=uninstall, root=repo_root(), run=units.run_captured)
    if outcome.code != 0:
        typer.echo(outcome.message, err=True)
        raise typer.Exit(outcome.code)
    if as_json:
        typer.echo(json.dumps({"ok": True, "message": outcome.message}, indent=2))
    else:
        typer.echo(outcome.message)


@plugins_app.command("list")
def plugins_list(
    as_json: Annotated[bool, typer.Option("--json", help=PLUGINS_JSON_HELP)] = False,
) -> None:
    """List the plugins: on or off, their two files, and why one is refused.

    `--json` also describes each for the Mac app: its settings as typed fields, the
    accounts it needs, and `ready` — whether it would work on a call once turned on, which
    the app reads beside `on` — with `why` when it would not.
    """
    settings = _configure_readonly()
    found = plugins.status(settings)
    if as_json:
        needed = dict.fromkeys(name for spec in plugins.PLUGINS.values() for name in spec.needs)
        connected = accounts.connected(settings, needed)
        typer.echo(json.dumps({
            "directory": str(settings.custom_tools_dir),
            "plugins": [status.as_dict() | plugins.describe(status, connected)
                        for status in found],
        }, indent=2))
        return
    typer.echo(f"# {settings.custom_tools_dir}")
    for status in found:
        spec = plugins.PLUGINS[status.name]
        state = "on" if status.on else ("refused" if status.installed else "off")
        typer.echo(f"{status.name:<16} {state:<8} {spec.summary}")
        if status.refused:
            typer.echo(f"{'':<16} {status.refused}")
        if status.installed or status.config_file.exists():
            shown = {key: (status.values or {}).get(key) for key in spec.important}
            settings_line = ", ".join(f"{key}={_plain(value)}" for key, value in shown.items())
            typer.echo(f"{'':<16} {status.config_file}" + (f"  ({settings_line})"
                                                            if settings_line else ""))


@plugins_app.command("hosts")
def plugins_hosts(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The hosts in ~/.ssh/config that cluster_stats could ask, and whose master is up now.

    Only hosts with a ControlMaster are offered: Keryx rides a login you already have open,
    and never opens its own. Nothing here reaches the network (`ssh -G`, `ssh -O check`).
    """
    _configure_readonly()
    rows = []
    for host in ssh_hosts.discover():
        alive = host.control_master and ssh_hosts.master_alive(host.alias)
        rows.append({**host.as_dict(), "master_alive": alive})
    if as_json:
        typer.echo(json.dumps({"hosts": rows}, indent=2))
        return
    if not rows:
        typer.echo(f"no hosts in {ssh_hosts.default_config()}")
    for row in rows:
        master = ("up" if row["master_alive"] else "down") if row["control_master"] else "none"
        who = f"{row['user']}@{row['hostname']}" if row["user"] else row["hostname"]
        typer.echo(f"{row['alias']:<20} {who:<40} ControlMaster: {master}")


@plugins_app.command("install")
def plugins_install(
    name: Annotated[str | None, typer.Argument(help="The plugin (`keryx plugins`).")] = None,
    assignments: Annotated[
        list[str] | None,
        typer.Option("--set", help="KEY=VALUE for one of its settings; repeat for more."),
    ] = None,
    clusters: Annotated[
        list[str] | None,
        typer.Option("--cluster", help="cluster_stats: ALIAS=PARTITION, the whole [clusters] "
                     "table; repeat for more."),
    ] = None,
    guard: Annotated[
        str | None, typer.Option("--guard", help="cluster_stats: a guard script of your own.")
    ] = None,
    from_settings: Annotated[
        bool,
        typer.Option("--from-settings", help="Move the settings it replaced into its file; "
                     "with no NAME, every plugin that was offered before."),
    ] = False,
    template: Annotated[
        bool,
        typer.Option("--template", help="Write its settings file and a draft to edit, and "
                     "leave it off until `keryx plugins install NAME`."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message} and nothing else.")
    ] = False,
) -> None:
    """Turn a plugin on, from its settings file, the values given here, or the old settings.

    With no values, a settings file already edited by hand (and a `--template` draft) is
    what is turned on. A secret is never taken here: `keryx config set KEY --stdin`, or
    `keryx auth login gmail`. It is offered from the next call; no restart.
    """
    _owner_only("install a plugin")
    settings = _configure_readonly()
    if as_json and from_settings:
        typer.echo("--json is for one plugin, and --from-settings may move several", err=True)
        raise typer.Exit(2)
    try:
        if from_settings:
            names = None if name is None else [plugins.plugin(name).name]
            moved = plugins.move_from_settings(settings, ConfigStore(), names)
            if not moved:
                typer.echo("nothing to move: no plugin was set up in the old settings")
            for one in moved:
                typer.echo(f"{one.name}: settings in {one.config_file}")
                if one.problem:
                    typer.echo(f"  {one.problem}")
            _echo_plugin_states(settings, [one.name for one in moved])
            return
        if name is None:
            typer.echo("which plugin? (`keryx plugins`)", err=True)
            raise typer.Exit(2)
        spec = plugins.plugin(name)
        values = _plugin_values(name, assignments, clusters, guard)
        if template:
            path = plugins.write_template(settings, name, values)
            if as_json:
                _echo_plugin_json(name, False, f"{spec.title} stays off: its settings file "
                                  "and a draft are written to finish by hand.")
            else:
                typer.echo(f"edit {path}, then `keryx plugins install {name}`")
            return
        if values:
            plugins.write_config(settings, name, values)
        plugins.install(settings, spec.name)
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not as_json:
        _echo_plugin_states(settings, [name])
        return
    status = _plugin_status(settings, name)
    if not status.on:
        typer.echo(f"{name} is installed but refused: {status.refused}", err=True)
        raise typer.Exit(1)
    _echo_plugin_json(name, True, f"{spec.title} is on from the next call.")


def _plugin_values(
    name: str, assignments: list[str] | None, clusters: list[str] | None, guard: str | None
) -> dict[str, Any]:
    """`--set KEY=VALUE`, `--cluster` and `--guard` for plugin `name`, validated as install
    validates them; a `PluginConfigError` says what is wrong."""
    values = plugins.command_line_values(name, assignments or [])
    if (clusters or guard is not None) and name != "cluster_stats":
        raise plugins.PluginConfigError("--cluster and --guard are for cluster_stats")
    if clusters:
        values["clusters"] = dict(
            plugins.parse_assignment(text, what="ALIAS=PARTITION") for text in clusters
        )
    if guard is not None:
        values["guard"] = guard
    return values


def _plugin_status(settings: Settings, name: str) -> plugins.Status:
    """The plugin as the next call would load it."""
    return next(status for status in plugins.status(settings) if status.name == name)


def _echo_plugin_json(name: str, on: bool, message: str) -> None:
    typer.echo(json.dumps({"name": name, "on": on, "message": message}, indent=2))


def _echo_plugin_states(settings: Settings, names: list[str]) -> None:
    """Whether each of `names` loads now, the way the next call will load it; exit 1 if not."""
    refused = False
    for status in plugins.status(settings):
        if status.name not in names:
            continue
        if status.on:
            typer.echo(f"{status.name} is on, from the next call")
        elif status.installed:
            refused = True
            typer.echo(f"{status.name} is installed but refused: {status.refused}")
    if refused:
        raise typer.Exit(1)


@plugins_app.command("remove")
def plugins_remove(
    name: Annotated[str, typer.Argument(help="The plugin.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message}; asks nothing first.")
    ] = False,
) -> None:
    """Turn a plugin off. Its settings are set aside, so turning it on again keeps them."""
    _owner_only("remove a plugin")
    settings = _configure_readonly()
    try:
        spec = plugins.plugin(name)
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not (yes or as_json):  # the app asks before it runs this
        typer.confirm(f"turn {name} off?", abort=True)
    was_on = plugins.remove(settings, name)
    if as_json:
        _echo_plugin_json(name, False, f"{spec.title} is off from the next call; its "
                          "settings are kept." if was_on else f"{spec.title} was not on.")
    elif was_on:
        typer.echo(f"{name} is off from the next call; its settings are kept in "
                   f"{plugins.off_path(settings, name)}")
    else:
        typer.echo(f"{name} was not on")


@plugins_app.command("set")
def plugins_set(
    name: Annotated[str, typer.Argument(help="The plugin, which must be installed.")],
    assignments: Annotated[
        list[str] | None, typer.Argument(help="KEY=VALUE for one of its settings; several.")
    ] = None,
    clusters: Annotated[
        list[str] | None,
        typer.Option("--cluster", help="cluster_stats: ALIAS=PARTITION, the whole [clusters] "
                     "table; repeat for more."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message} and nothing else.")
    ] = False,
) -> None:
    """Change an installed plugin's settings in its file, validated as install validates
    them. Nothing is reinstalled; a secret is never taken here. From the next call."""
    _owner_only("change a plugin's settings")
    settings = _configure_readonly()
    try:
        spec = plugins.plugin(name)
        if not (assignments or clusters):
            typer.echo(f"give KEY=VALUE for one of {name}'s settings", err=True)
            raise typer.Exit(2)
        plugins.change(settings, name, _plugin_values(name, assignments, clusters, None))
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    status = _plugin_status(settings, name)
    if not as_json:
        typer.echo(f"{name}: settings saved" + (
            "" if status.on else f"; it does not load: {status.refused}"))
        return
    why = None if status.on else plugins.describe(
        status, accounts.connected(settings, spec.needs))["why"]
    _echo_plugin_json(name, status.on, "Settings saved." + (f" {why}" if why else ""))


@app.command()
def forget(
    older_than: Annotated[
        int,
        typer.Option(
            "--older-than",
            "-d",
            min=0,
            help="Delete transcripts and finished tasks older than this many days. 0 = all.",
        ),
    ] = 0,
    transcripts_only: Annotated[
        bool, typer.Option("--transcripts-only", help="Leave the task rows alone.")
    ] = False,
    tasks_only: Annotated[
        bool, typer.Option("--tasks-only", help="Leave the call transcripts alone.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
) -> None:
    """Delete call transcripts and finished task rows, now.

    The same rules as the automatic retention pass, run on demand and regardless of whether
    `TRANSCRIPT_RETENTION_DAYS` / `TASK_RETENTION_DAYS` are set. In particular the one that
    matters: a finished task Keryx has **not told you about yet** is never deleted,
    however old it is, because `reported_at` is the only record that you heard the result.
    Those are reported as kept rather than removed silently.

    This is not undoable, and `--older-than 0` really does mean everything.
    """
    if transcripts_only and tasks_only:
        typer.echo("pick one of --transcripts-only and --tasks-only")
        raise typer.Exit(code=2)

    settings = _configure_readonly()
    # `--older-than 0` is "everything", which is the opposite of what 0 means in the
    # settings ("keep everything"); an explicit command is an explicit decision.
    cutoff = datetime.now(UTC) if older_than == 0 else cutoff_for(older_than)
    window = "everything" if older_than == 0 else f"older than {older_than} days"
    what = (
        "transcripts"
        if transcripts_only
        else "finished tasks"
        if tasks_only
        else "transcripts and finished tasks"
    )
    if not yes:
        typer.echo(f"about to delete {what} {window} from {settings.data_dir}.")
        typer.confirm("this cannot be undone. continue?", abort=True)

    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        report = asyncio.run(
            prune_with(
                settings,
                store,
                transcripts=None if tasks_only else cutoff,
                tasks=None if transcripts_only else cutoff,
            )
        )
    finally:
        asyncio.run(store.close())
    typer.echo(f"removed {report.describe()}")


# --- diagnostics -----------------------------------------------------------


def _twilio_admin(settings: Settings) -> TwilioAdmin | None:
    """A Twilio client for doctor's webhook check, when there is anything to check."""
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.public_host):
        return None
    return RestTwilioAdmin(settings.twilio_account_sid, settings.twilio_auth_token)


def _doctor_checks(
    settings: Settings, problems: Mapping[str, str], store: ConfigStore
) -> list[Check]:
    """Every check `keryx doctor` makes, Twilio's webhook and the model servers included."""
    return run_doctor_checks(
        settings,
        config_problems=problems,
        store=store,
        twilio=_twilio_admin(settings),
        reach=EndpointReach(),
    )


@app.command()
def doctor(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print every check as JSON and nothing else.")
    ] = False,
    fix: Annotated[
        bool, typer.Option("--fix", help="Make every secret file owner-only first. Nothing else.")
    ] = False,
) -> None:
    """Check that this machine is set up to run Keryx; exits non-zero on a hard failure."""
    settings, problems = _load_settings_reporting()
    store = ConfigStore()
    if fix:
        changed = fix_permissions(settings, store)
        if not as_json:
            for line in changed or ["nothing to tighten"]:
                typer.echo(f"fix: {line}")
    checks = _doctor_checks(settings, problems, store)
    failed = sum(1 for check in checks if not check.ok and check.severity == "hard")
    if as_json:
        typer.echo(
            json.dumps({"ok": not failed, "checks": [c.as_dict() for c in checks]}, indent=2)
        )
        if failed:
            raise typer.Exit(1)
        return
    for check in checks:
        typer.echo(format_check(check))
    if has_hard_failure(checks):
        typer.echo(f"\n{failed} check(s) failed.")
        raise typer.Exit(1)
    typer.echo("\nall good.")


@app.command()
def status(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """Say whether Keryx is running, and what it has to tell you; a report, so it exits 0."""
    try:
        settings, problems = _load_settings_reporting()
    except ConfigFileError as error:
        report = failed_status(str(error))
    except ValidationError as error:
        report = failed_status(_config_summary(error))
    else:
        report = build_status(settings, config_problems=problems)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    for line in format_status(report):
        typer.echo(line)


# --- the PIN -----------------------------------------------------------------


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


# --- setup, config, auth ---------------------------------------------------


@setup_app.callback(invoke_without_command=True)
def setup(
    ctx: typer.Context,
    review_all: Annotated[
        bool, typer.Option("--all", help="Walk every section, and ask again about what is set.")
    ] = False,
    instructions: Annotated[
        bool,
        typer.Option(
            "--agent-instructions",
            help="Print how a coding agent sets Keryx up from the command line, and exit.",
        ),
    ] = False,
) -> None:
    """Set Keryx up: a guided walk through only what is still missing.

    Everything it saves goes to KERYX_HOME as you go, so stopping part way loses nothing.
    A coding agent setting Keryx up uses the commands instead: --agent-instructions.
    """
    if ctx.invoked_subcommand is not None:
        return
    settings = _configure_readonly(migrated=False)  # the wizard says so, in its own words
    if instructions:
        typer.echo(agent_instructions(settings, ConfigStore()))
        return
    _owner_only("run setup")
    if not _interactive():
        typer.echo(
            "keryx setup asks questions, so it needs a terminal. A coding agent: "
            "keryx setup --agent-instructions",
            err=True,
        )
        raise typer.Exit(2)
    from keryx.setup.ui import RichPrompter

    ctx = SetupContext(ui=RichPrompter(), store=ConfigStore(), load=_load_settings_optional)
    try:
        code = run_wizard(ctx, review_all=review_all)
    except Aborted:
        typer.echo("\nsetup stopped. What was saved stays saved; `keryx setup` picks up here.")
        raise typer.Exit(130) from None
    if code:
        raise typer.Exit(code)


@setup_app.command("plan")
def setup_plan(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The steps to a first working call, and which are done: a report, so it exits 0.

    What the Mac app's Setup Assistant walks. It asks what `keryx doctor` asks, Twilio
    included; a migration still to run is its first step, and settings that will not load
    fail every step with the reason, as `keryx status` reports them — never a refusal.
    """
    try:
        settings, problems = _load_settings_reporting()
    except ConfigFileError as error:
        plan = failed_plan(str(error))
    except ValidationError as error:
        plan = failed_plan(_config_summary(error))
    else:
        store = ConfigStore()
        plan = build_plan(settings, store, _doctor_checks(settings, problems, store))
    if as_json:
        typer.echo(json.dumps(plan, indent=2))
        return
    for line in format_plan(plan):
        typer.echo(line)


@setup_app.command("mark")
def setup_mark(
    step: Annotated[str, typer.Argument(help="The step, by its id in `keryx setup plan`.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Record a step as walked: an optional one skipped, or the PIN left to the first call."""
    _owner_only("mark a setup step")
    try:
        settings = _load_settings_optional()
    except (ConfigFileError, ValidationError) as error:
        summary = _config_summary(error) if isinstance(error, ValidationError) else str(error)
        typer.echo(f"nothing is marked while the settings do not load: {summary}", err=True)
        raise typer.Exit(1) from None
    if (refusal := settings.storage_refusal()) is not None:
        # What it saves would land where `keryx migrate` is about to move the old files.
        typer.echo(f"nothing is marked until the old files have moved: {refusal}", err=True)
        raise typer.Exit(2)
    store = ConfigStore()
    try:
        marked = mark_step(store, step)
    except StepRefused as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    except (OSError, ValueError) as error:
        typer.echo(f"nothing is marked: {store.config_path} cannot be read ({error}); "
                   "fix it by hand", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(marked) if as_json else f"{marked['id']}: walked")


def _interactive() -> bool:
    """A person at a terminal: both ends of it."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _plain(value: Any) -> str:
    """A setting's value as a script reads it: lists comma-joined, maps as JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ",".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _key_or_exit(key: str) -> str:
    name = field_for(key)
    if name is None:
        typer.echo(f"there is no setting called {key.upper()} (`keryx config list`)", err=True)
        raise typer.Exit(2)
    return env_var_name(name)


@config_app.command("list")
def config_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
    group: Annotated[
        str | None, typer.Option("--group", help=f"Only one group: {', '.join(GROUPS)}.")
    ] = None,
) -> None:
    """Every setting: whether it is set, where from, and whether the service may change it.

    A secret's value is never shown — only whether it is set.
    """
    settings = _load_settings_optional()
    rows = ConfigStore().describe(settings)
    if group is not None:
        if group not in GROUPS:
            typer.echo(f"no group {group!r}; one of: {', '.join(GROUPS)}", err=True)
            raise typer.Exit(2)
        rows = [row for row in rows if row["group"] == group]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for name, title in GROUPS.items():
        members = [row for row in rows if row["group"] == name]
        if not members:
            continue
        typer.echo(f"\n{title}")
        width = max(len(row["key"]) for row in members)
        for row in members:
            if row["secret"]:
                value = "(secret, set)" if row["set"] else "(secret)"
            else:
                value = _plain(row["value"])
            where = row["source"] if row["set"] else ""
            mark = "*" if row["service_writable"] else " "
            typer.echo(f"  {mark} {row['key']:<{width}}  {_shorten(value, 50):<50}  {where}")
    typer.echo("\n* the running service may change it (`keryx config lock KEY` to stop that)")


@config_app.command("get")
def config_get(
    keys: Annotated[list[str], typer.Argument(help="One or more settings, by name.")],
    shell: Annotated[
        bool, typer.Option("--shell", help="Print KEY='value' lines a shell can eval.")
    ] = False,
) -> None:
    """Print the value a setting has now, from wherever it comes. Never a secret's."""
    settings, problems = _load_settings_reporting()
    if "config_file" in problems:  # a script must not be handed the defaults instead
        typer.echo(problems["config_file"], err=True)
        raise typer.Exit(1)
    dumped = settings.model_dump(mode="json")
    for key in keys:
        name = field_for(_key_or_exit(key))
        assert name is not None
        if is_secret(name):
            typer.echo(
                f"{env_var_name(name)} is a secret and is never printed; "
                "`keryx config list` says whether it is set",
                err=True,
            )
            raise typer.Exit(1)
        value = _plain(dumped[name])
        typer.echo(f"{env_var_name(name)}={shlex.quote(value)}" if shell else value)


@config_app.command("options")
def config_options(
    key: Annotated[str, typer.Argument(help="A setting with a list of values, by name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """The values a setting can take, found now: the models a provider lists, the voices.

    A list that cannot be read falls back to the values Keryx knows, and says why; that is
    still a success. `keryx config list --json` marks the settings that have one.
    """
    key = _key_or_exit(key)
    if not has_options(key):
        typer.echo(f"{key} has no list of options", err=True)
        raise typer.Exit(2)
    found = options_for(key, _load_settings_optional())
    if as_json:
        typer.echo(json.dumps(found.as_dict(), indent=2))
        return
    if found.error:
        typer.echo(found.error)
    for option in found.options:
        value = option.value or '""'  # blank is a value too: OPENAI_VOICE's own voice
        detail = f" ({option.detail})" if option.detail else ""
        typer.echo(f"{value}  {option.label}{detail}")


@config_app.command("verify")
def config_verify(
    key: Annotated[str, typer.Argument(help="The key or credential to check, by name.")],
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="Check the value on standard input (one line), unsaved."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """Check that a key or credential works, and save nothing.

    OPENAI_API_KEY, CODEX_API_KEY (an OpenAI key) and ANTHROPIC_API_KEY are asked for their
    provider's list of models. TWILIO_AUTH_TOKEN is checked with the saved TWILIO_ACCOUNT_SID,
    and names the account: save the SID first, since it is not checked on its own. With
    --stdin the value given is checked and not saved (`keryx config set KEY --stdin` saves
    it); without, the saved one. A key that does not work is still a result (exit 0); a key
    that cannot be checked exits 2. No key is ever printed.
    """
    key = _key_or_exit(key)
    if not credentials.can_verify(key):
        hint = "; TWILIO_AUTH_TOKEN checks it with its token" if key == "TWILIO_ACCOUNT_SID" else ""
        supported = ", ".join(credentials.SUPPORTED)
        typer.echo(f"{key} can't be checked{hint} (only {supported})", err=True)
        raise typer.Exit(2)
    value = _read_secret_line(key) if stdin else None
    result = credentials.verify(key, value, _load_settings_optional())
    if as_json:
        typer.echo(json.dumps(result.as_dict(), indent=2))
    elif result.ok:
        typer.echo(f"{key} works" + (f" ({result.detail})" if result.detail else ""))
    else:
        typer.echo(f"{key} does not work: {result.problem}")


def _read_secret_line(key: str) -> str:
    """One line of standard input, without its newline (a hidden prompt at a terminal)."""
    if sys.stdin.isatty():
        return typer.prompt(key, hide_input=True).strip()
    return sys.stdin.readline().strip()


def _read_secret_input(key: str) -> str:
    if sys.stdin.isatty():
        return typer.prompt(key, hide_input=True).strip()
    return sys.stdin.read().strip()


@config_app.command("set")
def config_set(
    pairs: Annotated[list[str], typer.Argument(help="KEY VALUE [KEY VALUE …], or one KEY.")],
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the one KEY's value from standard input.")
    ] = False,
    from_env: Annotated[
        str | None,
        typer.Option("--from-env", help="Take the one KEY's value from this variable."),
    ] = None,
) -> None:
    """Change settings. A secret's value is taken only from --stdin or --from-env.

    Never on the command line, where `ps` and the shell's history keep it. Run inside a
    Keryx task, this acts as the running service, and may change only what that is allowed
    to (`keryx config list`).
    """
    if stdin or from_env is not None:
        if len(pairs) != 1 or (stdin and from_env is not None):
            typer.echo("--stdin and --from-env set exactly one KEY, and only one of them", err=True)
            raise typer.Exit(2)
        key = _key_or_exit(pairs[0])
        if from_env is not None:
            if not os.environ.get(from_env):
                typer.echo(f"{from_env} is not set in this environment", err=True)
                raise typer.Exit(2)
            values = {key: os.environ[from_env]}
        else:
            values = {key: _read_secret_input(key)}
    else:
        if not pairs or len(pairs) % 2:
            typer.echo("give KEY VALUE pairs (or one KEY with --stdin / --from-env)", err=True)
            raise typer.Exit(2)
        values = {}
        for key, value in zip(pairs[::2], pairs[1::2], strict=True):
            key = _key_or_exit(key)
            name = field_for(key)
            if name is not None and is_secret(name):
                typer.echo(
                    f"{key} is a secret: never on the command line. "
                    f"`keryx config set {key} --stdin`, or --from-env VAR",
                    err=True,
                )
                raise typer.Exit(2)
            values[key] = value
    _store_values(values)


def _store_values(values: dict[str, Any], *, echo: bool = True) -> dict[str, Any]:
    """Save `values` as whoever is running this; what was stored. A refusal is said on
    stderr and exits 1. `echo` says, line by line, where each one went."""
    store = ConfigStore()
    settings = _load_settings_optional()
    actor = current_actor()
    try:
        stored = store.set(values, actor=actor, settings=settings)
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not echo:
        return stored
    for key, value in stored.items():
        name = field_for(key)
        where = "secrets.toml" if name is not None and is_secret(name) else "config.toml"
        typer.echo(f"{key} unset" if value is None else f"{key} saved to {where}")
        if store.source_of(key, settings) == FROM_ENV:
            typer.echo(f"note: {key} is also set in the environment, which wins")
    if actor == SERVICE:
        typer.echo("set; takes effect after a restart")
    return stored


@config_app.command("unset")
def config_unset(
    keys: Annotated[list[str], typer.Argument(help="The settings to return to their default.")],
) -> None:
    """Remove settings from the store, so the default (or the environment) applies again."""
    _store_values({_key_or_exit(key): None for key in keys})


@config_app.command("path")
def config_path(
    shell: Annotated[
        bool,
        typer.Option("--shell", help="Print NAME='path' lines a shell can eval, and nothing else."),
    ] = False,
) -> None:
    """Print where the configuration, the data, the state and the cache live."""
    settings, problems = _load_settings_reporting()
    if "config_file" in problems:  # a script must not be handed the defaults instead
        typer.echo(problems["config_file"], err=True)
        raise typer.Exit(1)
    store = ConfigStore()
    if shell:
        # What the installers render into the service, so it resolves exactly these: the
        # four directories, and the XDG base directories they were derived from.
        paths = {
            HOME_ENV: store.home,
            "DATA_DIR": settings.data_dir,
            "STATE_DIR": settings.state_dir,
            "CACHE_DIR": settings.cache_dir,
            **{variable: xdg_home(kind) for kind, (variable, _) in XDG_HOMES.items()},
        }
        for name, path in paths.items():
            typer.echo(f"{name}={shlex.quote(str(path))}")
        return
    rows = [
        (HOME_ENV, store.home),
        ("config", store.config_path),
        ("secrets", store.secrets_path),
        ("data", settings.data_dir),
        ("state", settings.state_dir),
        ("cache", settings.cache_dir),
    ]
    for name, path in rows:
        typer.echo(f"{name:<12} {path}")


@config_app.command("import-env")
def config_import_env(
    path: Annotated[
        Path, typer.Argument(help="The legacy env file.", exists=True, dir_okay=False)
    ] = Path(".env"),
) -> None:
    """Move a legacy .env (and .secrets/client_secret.json) into the store, then rename it."""
    _owner_only("import settings")
    try:
        report = ConfigStore().import_env(path)
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"imported: {', '.join(report.imported) or 'nothing'}")
    if report.defaults:
        typer.echo(f"left at their defaults: {', '.join(report.defaults)}")
    if report.kept:
        typer.echo(f"already set in the store, kept as they were: {', '.join(report.kept)}")
    if report.pin:
        typer.echo(f"PIN: {report.pin}")
    if report.client_file:
        typer.echo(f"Google client file: {report.client_file}")
    if report.unknown:
        typer.echo(f"not settings, not imported: {', '.join(report.unknown)}")
    typer.echo(f"{path} is now {report.renamed_to}; delete it once Keryx runs from the store.")


# --- migrate ---------------------------------------------------------------


#: How many of the tasks that lost their sessions are named one by one.
MAX_LOST_SESSIONS_LISTED = 10


@app.command()
def migrate(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would move, and change nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before starting.")] = False,
) -> None:
    """Move a Jarvis install to Keryx's directories: ~/.config/jarvis and its siblings, or
    ~/.jarvis and a .env here.

    Stops the service while it moves things, re-renders it and the approval hook, and
    starts it again; Jarvis's unit is retired, and Keryx's installed in its place. Run it
    from the checkout the service runs in. Nothing is deleted: ~/.jarvis is renamed aside
    with whatever is left in it.
    """
    _owner_only("migrate Keryx's files")
    plan = make_plan([Path.cwd(), repo_root()])
    if plan.empty and not plan.conflicts:
        typer.echo("nothing to migrate: Keryx already keeps everything where it should")
        return
    for line in plan.describe():
        typer.echo(line)
    settings = _load_settings_optional()
    target = resolve_target(settings, from_outside=True)
    predecessor = old_target(candidate_target(settings))
    if predecessor is not None:
        target = target or candidate_target(settings)
        typer.echo(f"  {predecessor.describe()}: stopped first, and retired once everything "
                   f"has moved; {target.describe()} installed and started in its place")
    elif target is not None:
        typer.echo(f"  {target.describe()}: stopped first, re-rendered, and started again")
    if plan.conflicts:
        typer.echo("\nNothing can move until these are settled:", err=True)
        for conflict in plan.conflicts:
            typer.echo(f"  {conflict}", err=True)
        raise typer.Exit(1)
    if dry_run:
        return
    if (live := health_probe(settings)) and target is not None:
        typer.echo(f"{live} live call(s): migrating stops the service and would cut them off. "
                   "Run it again once the line is clear.", err=True)
        raise typer.Exit(1)
    if not yes:
        if not _interactive():
            typer.echo("pass --yes to migrate without a terminal to ask at", err=True)
            raise typer.Exit(2)
        if not typer.confirm("\nGo ahead?", default=False):
            raise typer.Exit(1)
    try:
        stopping = predecessor or target
        report = run_migration(
            plan,
            service=MigratingService(stopping) if stopping is not None else None,
            successor=MigratingService(target) if predecessor and target else None,
            retire=(lambda: retire_old_units(predecessor.manager)) if predecessor else list,
            fix_permissions=lambda: fix_permissions(_load_settings_optional(), ConfigStore()),
            rerender=lambda: _rerender(target),
            echo=typer.echo,
        )
    except MigrationError as error:
        typer.echo(f"keryx migrate: {error}", err=True)
        raise typer.Exit(1) from None
    _echo_migration(report)


#: The approval hook's file name, and what it was called before the service was Keryx: a
#: settings file naming either has the hook installed, and the installer replaces both.
HOOK_NAMES = ("keryx_approval.py", "jarvis_approval.py")


def _rerender(target) -> list[str]:
    """Re-install what names the old paths: the service's unit, and the approval hook."""
    done = []
    scripts = repo_root() / "scripts"
    if target is not None:
        installer = scripts / Path(INSTALLERS[target.manager]).name
        code = run_command([str(installer)])
        done.append(f"{installer.name}: {'done' if code == 0 else f'exited {code}, run it again'}")
    try:
        hooks = (claude_config_dir() / "settings.json").read_text(encoding="utf-8")
        hooked = any(name in hooks for name in HOOK_NAMES)
    except OSError:
        hooked = False
    if hooked:
        code = run_command([str(scripts / "install-claude-hook.sh")])
        done.append(f"approval hook: {'done' if code == 0 else f'exited {code}, run it again'}")
    return done


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


def _owner_refusal(verb: str) -> str | None:
    """Why this command is refused inside anything `keryx serve` started; None outside."""
    if current_actor() != SERVICE:
        return None
    return f"only the owner may {verb}, at their own terminal"


def _owner_only(verb: str) -> None:
    """Refuse inside anything `keryx serve` started: this command is the owner's alone.

    Each of these writes what the running service may not — protected settings, the PIN,
    the memory whole — so the service-actor rule would mean nothing if they skipped it.
    """
    if refusal := _owner_refusal(verb):
        typer.echo(refusal, err=True)
        raise typer.Exit(1)


@config_app.command("lock")
def config_lock(key: Annotated[str, typer.Argument(help="The setting.")]) -> None:
    """Stop the running service changing KEY."""
    _owner_only("lock a setting")
    try:
        ConfigStore().lock(_key_or_exit(key))
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{key.upper()}: the running service may no longer change it")


@config_app.command("unlock")
def config_unlock(key: Annotated[str, typer.Argument(help="The setting.")]) -> None:
    """Let the running service change KEY. Refused for credentials and protected settings."""
    _owner_only("unlock a setting")
    try:
        ConfigStore().unlock(_key_or_exit(key))
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{key.upper()}: the running service may change it")


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


@app.command("integrations")
def integrations_command(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """The accounts Keryx connects to — Google, Gmail, Slack, GitHub, the admin keys — and
    whether each is connected. Only reads; a secret's value is never shown."""
    settings = _configure_readonly()
    found = accounts.integrations(settings)
    if as_json:
        typer.echo(json.dumps({"integrations": found}, indent=2))
        return
    width = max(len(row["id"]) for row in found)
    for row in found:
        state = "connected" if row["connected"] else "not connected"
        typer.echo(f"{row['id']:<{width}}  {state:<13}  {row['title']}")
        if row["why"]:
            typer.echo(f"{'':<{width}}  {row['why']}")


# --- approvals -------------------------------------------------------------


APPROVALS_EMPTY = "nothing in the approvals log yet"
APPROVALS_HEADER = f"{"WHEN":<17} {"EVENT":<20} {"ID":>3}  WHAT"


@app.command()
def approvals(
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many of the most recent lines to show.")
    ] = 20,
    disable: Annotated[
        bool, typer.Option("--disable", help="Stop the bridge escalating anything, now.")
    ] = False,
    enable: Annotated[
        bool, typer.Option("--enable", help="Undo --disable.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """What the approval bridge has done, and the kill switch that stops it.

    The switch is a file, deliberately: it is checked afresh on every single request, so it
    takes effect on the next prompt with no restart and without editing any settings — and
    it still works when the thing you want to stop is the thing you would have to ask.
    """
    settings = _configure_readonly()
    state_dir = settings.state_dir / STATE_DIR_NAME
    switch = state_dir / KILL_SWITCH_NAME
    if disable and enable:
        typer.echo("pick one of --disable and --enable")
        raise typer.Exit(code=2)
    if as_json and (disable or enable):
        typer.echo("pick one of --json and --disable/--enable")
        raise typer.Exit(code=2)
    if disable:
        secure_dir(state_dir)
        switch.touch()
        secure_file(switch)
        typer.echo(f"approval escalation is off ({switch})")
        return
    if enable:
        switch.unlink(missing_ok=True)
        typer.echo("approval escalation is on")
        return

    entries = read_entries(state_dir / AUDIT_NAME)
    if as_json:
        cancelled, late = escalation_counts(entries)
        typer.echo(json.dumps({
            "escalation_enabled": escalation_enabled(settings.state_dir),
            "escalate_after_s": settings.approval_escalate_seconds,
            "max_per_hour": settings.approval_max_per_hour,
            "cancelled_before_call": cancelled,
            "answered_after_call": late,
            "entries": [entry_json(e) for e in entries[-max(limit, 1):]],
        }, indent=2))
        return

    typer.echo(
        f"escalation: {"OFF (kill switch)" if not escalation_enabled(settings.state_dir)
                       else "on"}, "
        f"after {settings.approval_escalate_seconds:g}s, "
        f"at most {settings.approval_max_per_hour}/hour"
    )
    summary = _escalation_summary(entries)
    if summary:
        typer.echo(summary)
    for line in _audit_lines(entries, limit):
        typer.echo(line)


def _escalation_summary(entries: list[dict]) -> str | None:
    """The table's summary line: whether settling a request stopped its call, over every
    line that records it. `None` while nothing has recorded one yet — not the same as
    `escalation_counts(entries) == (0, 0)`, which a settled-and-escalated request that
    counted toward neither bucket also produces (see `settled_with_escalation`).
    """
    if not settled_with_escalation(entries):
        return None
    cancelled, late = escalation_counts(entries)
    return (
        f"{cancelled} answered before the call was due (no call), "
        f"{late} answered elsewhere after they were told"
    )


def _audit_lines(entries: list[dict], limit: int) -> list[str]:
    """The tail of the audit log as table rows, or one line saying there is none."""
    rows = [APPROVALS_HEADER]
    for entry in entries[-max(limit, 1):]:
        row = entry_json(entry)
        when = row["ts"][:16].replace("T", " ")
        request_id = "" if row["request_id"] is None else str(row["request_id"])
        rows.append(
            f"{when:<17} {row["event"]:<20} "
            f"{request_id:>3}  "
            f"{_shorten(str(row["what"]), MAX_DESCRIPTION_CHARS)}"
        )
    return rows if len(rows) > 1 else [APPROVALS_EMPTY]
