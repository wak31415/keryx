"""Jarvis command-line interface."""

import asyncio
import contextlib
import dataclasses
import importlib.metadata
import json
import logging
import logging.handlers
import signal
import subprocess
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from pydantic import ValidationError

from jarvis.app import TASK_DB_NAME, AppState, build_app_state, shutdown_app_state
from jarvis.approvals.broker import AUDIT_NAME, KILL_SWITCH_NAME, STATE_DIR_NAME
from jarvis.config import PLACEHOLDER_KEY, Settings, env_var_name, load_settings
from jarvis.continuity.memory import memory_path, read_memory
from jarvis.continuity.retention import cutoff_for, prune, prune_with
from jarvis.doctor import format_check, has_hard_failure, run_doctor_checks
from jarvis.events import EventBus
from jarvis.google_setup import GoogleSetupError, run_google_setup
from jarvis.local_runner import LocalRunner
from jarvis.logging_util import mask_number
from jarvis.onboarding import read_facts, run_init
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.restart.health import health_probe, wait_until_serving
from jarvis.restart.logscan import errors_since
from jarvis.restart.logscan import marks as log_marks
from jarvis.restart.service import (
    UNSUPPORTED_HINT,
    resolve_target,
    spawn_watchdog,
    watch_command,
)
from jarvis.restart.store import RECORD_NAME, RestartRecord, RestartStore
from jarvis.restart.version import loaded_version, mark_running, mark_startup_logs
from jarvis.restart.watchdog import watch
from jarvis.server import create_app
from jarvis.session import VoiceSession
from jarvis.tasks.models import Task, TaskStatus
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolRegistry
from jarvis.transports.local_audio import LocalAudioDevice
from jarvis.transports.wav import WavTransport
from jarvis.wakeword import OpenWakeWordDetector, WakeWordListener, wakeword_unavailable

app = typer.Typer(help="Jarvis voice agent.")
tasks_app = typer.Typer(help="Inspect the tasks handed to subagents.")
app.add_typer(tasks_app, name="tasks")
log = logging.getLogger("jarvis.cli")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"jarvis {importlib.metadata.version('jarvis')}")
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
    """Jarvis voice agent."""


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

TIME_FORMAT = "%Y-%m-%d %H:%M"
#: How much of a task description fits in the `tasks list` table.
MAX_DESCRIPTION_CHARS = 60
#: How much of a report `tasks show` prints before it starts truncating.
MAX_REPORT_CHARS = 4000
#: `jarvis memory` prints the whole document, not the slice a prompt gets, so this is only
#: a backstop against a memory that has run away.
MAX_MEMORY_PRINT_CHARS = 100_000


#: What `doctor` puts in place of a field whose configured value does not validate, so a
#: broken install still gets a report rather than a traceback. A field not listed here is
#: one `doctor` cannot work around, and it re-raises.
DOCTOR_FALLBACKS: dict[str, object] = {"openai_api_key": PLACEHOLDER_KEY, "pin": None}


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
    except ValidationError as error:
        # This is the path `jarvis serve` takes, so this message is what somebody reads in
        # `journalctl` after a restart failed to bring the service back. A traceback would
        # tell them nothing they could act on, and pydantic's own rendering of a rejected
        # `JARVIS_PIN` is not something to put in a log at all.
        typer.echo(f"jarvis cannot start: {_config_summary(error)}", err=True)
        typer.echo("run `jarvis doctor` for the full picture.", err=True)
        raise typer.Exit(2) from error
    if overrides:
        settings = settings.model_copy(update=overrides)
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    return settings


def _load_settings_reporting() -> tuple[Settings, dict[str, str]]:
    """Settings for the read-only commands, plus why any field had to be given up on.

    `doctor` has to run *because* the install is incomplete, and `tasks`/`download-models`
    never talk to OpenAI at all — so a field in `DOCTOR_FALLBACKS` is replaced rather than
    allowed to raise. Each replacement is recorded against its field name, because "not
    set" and "set to something unusable" are different problems and `doctor` has to be
    able to tell them apart. Anything else still raises: an unreportable error beats a
    report built on a guess.
    """
    problems: dict[str, str] = {}
    overrides: dict[str, object] = {}
    while True:
        try:
            return load_settings(**overrides), problems
        except ValidationError as error:
            fresh = {
                field: _validation_message(detail)
                for detail in error.errors()
                if (field := str(detail["loc"][0]) if detail.get("loc") else "")
                in DOCTOR_FALLBACKS
                and field not in overrides
            }
            if not fresh:
                raise
            problems.update(fresh)
            overrides.update({field: DOCTOR_FALLBACKS[field] for field in fresh})


def _load_settings_optional() -> Settings:
    """`_load_settings_reporting` for the callers that only want the settings."""
    return _load_settings_reporting()[0]


def _configure_readonly(*, quiet: bool = True) -> Settings:
    """`_configure` for the read-only commands (no `OPENAI_API_KEY` required).

    `quiet` because these print an answer, and `LOG_LEVEL` is the service's setting: the
    INFO lines it wants in `jarvis.log` (a schema created, a migration run) are noise above
    a table. Warnings still show, and `LOG_LEVEL=DEBUG` still means everything.
    """
    settings = _load_settings_optional()
    settings.ensure_dirs()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    if quiet and level != logging.DEBUG:
        level = max(level, logging.WARNING)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return settings


def _add_file_logging(settings: Settings) -> None:
    """Also log to `data_dir/logs/jarvis.log`, rotated, alongside the console handler."""
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    rotating = logging.handlers.RotatingFileHandler
    for existing in [h for h in root.handlers if isinstance(h, rotating)]:
        root.removeHandler(existing)  # a second `serve` in one process replaces, not stacks
        existing.close()
    handler = rotating(
        log_dir / "jarvis.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)


@app.command("download-models")
def download_models() -> None:
    """Download the configured wake-word model via openwakeword."""
    why = wakeword_unavailable()
    if why is not None:
        typer.echo(f"{why}: there is no wake-word model to download on this machine", err=True)
        raise typer.Exit(1)
    import openwakeword.utils

    settings = _load_settings_optional()
    openwakeword.utils.download_models(model_names=[settings.wakeword_model])


@app.command()
def serve(
    no_phone: Annotated[
        bool, typer.Option("--no-phone", help="Skip the Twilio phone server.")
    ] = False,
    no_wakeword: Annotated[
        bool, typer.Option("--no-wakeword", help="Skip the local wake-word listener.")
    ] = False,
    fake_agents: Annotated[
        bool,
        typer.Option("--fake-agents", help="Run scripted subagents instead of the Claude SDK."),
    ] = False,
    host: Annotated[
        str | None, typer.Option("--host", help="Override HOST for the phone server.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Override PORT for the phone server.")
    ] = None,
) -> None:
    """Run Jarvis: the Twilio phone server and the local "hey jarvis" listener."""
    # Only pass overrides that were actually asked for, so the .env path stays untouched.
    overrides: dict[str, object] = {}
    if fake_agents:
        overrides["fake_agents"] = True
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    settings = _configure(**overrides)
    if not no_phone and (refusal := settings.phone_refusal()):
        typer.echo(f"jarvis cannot start: {refusal}", err=True)
        raise typer.Exit(2)
    _add_file_logging(settings)
    if no_phone and no_wakeword:
        typer.echo("nothing to run: both the phone server and the wake word are disabled")
        return
    wakeword = not no_wakeword
    # Asked before anything starts, not discovered after the phone server is up: off macOS
    # the wake word's packages are not installed at all, and that is the platform rather
    # than a fault — so the phone channel serves on its own, and says so once.
    why = wakeword_unavailable() if wakeword else None
    if why is not None:
        if no_phone:
            typer.echo(f"nothing to run: {why}, and --no-phone turned the phone off", err=True)
            raise typer.Exit(1)
        typer.echo(f"{why}; serving the phone channel only")
        wakeword = False
    asyncio.run(_serve(settings, phone=not no_phone, wakeword=wakeword))


async def _serve(settings: Settings, *, phone: bool, wakeword: bool) -> None:
    """Run the phone server and/or the wake-word loop until one stops or ctrl-c."""
    # First, before anything can commit on top of us: the next restart compares against
    # this to say whether it loaded anything, and the checkout will have moved by then.
    mark_running(settings.data_dir)
    # And before we log a line of our own: everything past here is this process's doing,
    # which is what the confirmation call should be reading. See `mark_startup_logs`.
    mark_startup_logs(settings.data_dir)
    state = build_app_state(settings)
    # Before anything writes: retention is off by default, so on most installs this looks
    # at two zeroes and returns. `memory.md` is trimmed either way — it is the one file
    # written by a subagent rather than by us.
    report = await prune(settings, state.store)
    if report:
        typer.echo(f"retention removed {report.describe()}")
    # The approval bridge binds a Unix socket, so only a serving process ever starts it;
    # a failure to bind is logged and the bridge simply stays off (jarvis/approvals).
    if state.approvals is not None:
        await state.approvals.start()
    server = _build_server(state) if phone else None
    server_task = None
    runner_task = None

    if server is not None:
        server_task = asyncio.create_task(server.serve(), name="phone-server")
        typer.echo(f"phone server on http://{settings.host}:{settings.port}")
    if wakeword:
        runner_task = asyncio.create_task(_build_local_runner(state).run(), name="local-runner")
        typer.echo('listening — say "hey jarvis" (ctrl-c to quit)')

    # If this process is the far side of a restart, confirm it — by itself, once the phone
    # server is really listening. It is deliberately not one of the `tasks` below: finishing
    # is what it does, and that must not bring the server down with it.
    ready = (lambda: wait_until_serving(server)) if server is not None else None
    callback_task = asyncio.create_task(
        _confirm_then_drain(state, ready=ready, wakeword=wakeword), name="restart-callback"
    )

    def shutdown() -> None:
        """Stop everything; uvicorn asks for `should_exit`, the runner for a cancel."""
        if server is not None:
            server.should_exit = True
        if runner_task is not None:
            runner_task.cancel()

    _run_on_signals(shutdown)
    tasks = [task for task in (server_task, runner_task) if task is not None]
    try:
        # Whichever half stops first (a crash, or ctrl-c) takes the other one with it.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        shutdown()
        if callback_task is not None:
            callback_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await callback_task
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                log.error("serve task failed", exc_info=result)
        # Subagents outlive a session but not the process: stop them and close the store.
        await shutdown_app_state(state)


async def _confirm_then_drain(state: AppState, *, ready, wakeword: bool) -> None:
    """Confirm the restart, then pick up the work the last process never got to.

    In that order, and never the other way round: the confirmation counts what was left
    `queued`, and picking those up first would have it counting tasks this process had
    already started as ones the restart interrupted. Neither half may take the server down
    with it, so both are guarded here rather than left to the caller.
    """
    if state.restart is not None:
        await state.restart.resume(wait_ready=ready, wakeword=wakeword)
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


def _build_local_runner(state: AppState) -> LocalRunner:
    """The wake-word loop, sharing every registry with the phone server."""
    listener = WakeWordListener(
        OpenWakeWordDetector(state.settings.wakeword_model),
        threshold=state.settings.wakeword_threshold,
    )
    return LocalRunner(
        state.settings,
        LocalAudioDevice(),
        listener,
        provider_factory=state.provider_factory,
        registry=state.registry,
        bus=state.bus,
        sessions=state.sessions,
        briefer=state.briefer,
    )


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
    return RestartStore(settings.data_dir / RECORD_NAME)


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
    """Restart the Jarvis service; it phones back by itself once it is up again."""
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
        version=loaded_version(settings.data_dir),
        log_marks=log_marks(settings.data_dir),
    )
    if not store.save(record):
        typer.echo(f"could not write {store.path}: the restart would go unconfirmed")
        raise typer.Exit(1)

    if number:
        # The same watch the voice path arms, for the same reason: this command prints a
        # promise that jarvis will call back, and nothing else would notice if it never
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
        typer.echo(f"restarting; jarvis will call {mask_number(number)} when it is back up")
    else:
        typer.echo("restarting; no call back was asked for")
    typer.echo("if the call never comes: jarvis restart --status")


@app.command("restart-watch", hidden=True)
def restart_watch() -> None:
    """Watch a pending restart from outside the service; started by the restart itself.

    Not for hand use — `jarvis restart` arms this, in a unit of its own so the restart
    cannot kill it (see `jarvis.restart.service.watch_command`). Deliberately without
    `_add_file_logging`: its output belongs in `logs/restart-watch.log`, and writing its
    own "the restart never came back" into `jarvis.log` would leave the next restart
    scanning that line back as a fault of Jarvis's.
    """
    settings = _configure_readonly(quiet=False)
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
    found = errors_since(settings.data_dir, record.log_marks)
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
        OpenAIRealtimeClient(settings.openai_api_key, settings.openai_realtime_model),
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
            help="Also show Jarvis's own housekeeping (the per-call memory updates).",
        ),
    ] = False,
) -> None:
    """List recent tasks, newest first."""
    settings = _configure_readonly()
    wanted = None if status is StatusFilter.ALL else TaskStatus(status.value)
    tasks = asyncio.run(_read_tasks(settings, wanted, limit, include_internal))
    if not tasks:
        typer.echo("no tasks" if wanted is None else f"no tasks with status {wanted}")
        return

    typer.echo(f"{'ID':>4}  {'STATUS':<9}  {'CREATED':<16}  {'TOLD':<5}  DESCRIPTION")
    for task in tasks:
        typer.echo(
            f"{task.id:>4}  {task.status:<9}  "
            f"{_local_time(task.created_at):<16}  "
            f"{_reported_flag(task):<5}  "
            f"{_shorten(task.description, MAX_DESCRIPTION_CHARS)}"
        )


def _reported_flag(task: Task) -> str:
    """Whether Jarvis has told them about this one: only meaningful once it has finished."""
    if task.internal:
        return "-"
    if task.status not in {TaskStatus.DONE, TaskStatus.FAILED}:
        return ""
    return "yes" if task.reported_at else "NO"


@tasks_app.command("show")
def tasks_show(task_id: Annotated[int, typer.Argument(help="The task id to show.")]) -> None:
    """Show everything stored about one task, including its report."""
    settings = _configure_readonly()
    task = asyncio.run(_read_task(settings, task_id))
    if task is None:
        typer.echo(f"no task {task_id}")
        raise typer.Exit(1)

    width = max(len(field.name) for field in dataclasses.fields(task))
    for field in dataclasses.fields(task):
        value = getattr(task, field.name)
        printable = _local_time(value) if isinstance(value, datetime) else value
        typer.echo(f"{field.name:<{width}}  {'' if printable is None else printable}")

    if task.report_path:
        _echo_report(Path(task.report_path))


def _echo_report(path: Path) -> None:
    """Print the task's written report, truncated; a report that is gone is simply skipped."""
    try:
        body = path.read_text(errors="replace")
    except OSError as exc:
        typer.echo(f"(report unreadable: {exc})")
        return
    typer.echo("\n--- report ---")
    typer.echo(body[:MAX_REPORT_CHARS].rstrip())
    if len(body) > MAX_REPORT_CHARS:
        typer.echo(f"… (truncated at {MAX_REPORT_CHARS} characters; full report: {path})")


# --- memory ----------------------------------------------------------------


@app.command()
def init(
    name: Annotated[
        str | None, typer.Option("--name", help="What Jarvis should call you (OWNER_NAME).")
    ] = None,
    fact: Annotated[
        list[str] | None,
        typer.Option("--fact", help="Something Jarvis should know about you; repeatable."),
    ] = None,
    from_file: Annotated[
        str | None,
        typer.Option("--from", help="Read the facts from a file, one per line; - is stdin."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a memory that already has something in it.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Ask nothing: write what was given.")
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the report as JSON and nothing else (needs --yes)."),
    ] = False,
) -> None:
    """Tell Jarvis whom it works for before its first call: a name and a first memory.

    Writes `memory.md` and nothing else — the `OWNER_NAME=` line for `.env` is printed for you
    to add — and ends with what every call will carry.

    For an agent setting this up: `--from - --yes --json` takes the facts on stdin and prints
    one JSON document. Exit 0 means the memory was written, or nothing was given to write;
    exit 1 means a memory was wanted and not written (one is already there without --force,
    or it is longer than a call reads); exit 2 means the command line itself is wrong.
    """
    if (from_file == "-" or as_json) and not yes:
        typer.echo(
            "--from - reads the facts from stdin and --json leaves nowhere to print a "
            "question, so neither can stop to ask: add --yes",
            err=True,
        )
        raise typer.Exit(2)
    settings = _configure_readonly()
    facts: list[str] | None = None
    if fact or from_file is not None:
        facts = list(fact or [])
        try:
            facts += read_facts(from_file) if from_file is not None else []
        except OSError as exc:
            typer.echo(f"could not read {from_file}: {exc}", err=True)
            raise typer.Exit(2) from None
    code = run_init(
        settings,
        name=name,
        facts=facts,
        force=force,
        yes=yes,
        echo=typer.echo,
        ask=lambda text: typer.prompt(text, default="", show_default=False),
        confirm=lambda text: typer.confirm(text, default=True),
        as_json=as_json,
    )
    if code:
        raise typer.Exit(code)


@app.command()
def memory(
    path_only: Annotated[
        bool, typer.Option("--path", help="Print where the memory lives and nothing else.")
    ] = False,
) -> None:
    """Print what Jarvis remembers between calls.

    The file a subagent rewrites after every call and every session reads back at the top
    of its prompt. It is plain markdown and safe to edit by hand — the next update merges
    around whatever is there.
    """
    settings = _configure_readonly()
    path = memory_path(settings.data_dir)
    if path_only:
        typer.echo(str(path))
        return

    text = read_memory(settings.data_dir, max_chars=MAX_MEMORY_PRINT_CHARS)
    if not text:
        typer.echo(f"nothing remembered yet ({path} does not exist)")
        return
    typer.echo(f"# {path}\n")
    typer.echo(text)


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
    matters: a finished task Jarvis has **not told you about yet** is never deleted,
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


@app.command()
def doctor(
    no_mic: Annotated[
        bool, typer.Option("--no-mic", help="Skip the microphone probe (headless machines).")
    ] = False,
) -> None:
    """Check that this machine is set up to run Jarvis; exits non-zero on a hard failure."""
    settings, problems = _load_settings_reporting()
    checks = run_doctor_checks(settings, probe_mic=not no_mic, config_problems=problems)
    for check in checks:
        typer.echo(format_check(check))

    if has_hard_failure(checks):
        failed = sum(1 for check in checks if not check.ok and check.severity == "hard")
        typer.echo(f"\n{failed} check(s) failed.")
        raise typer.Exit(1)
    typer.echo("\nall good.")


@app.command("setup-google")
def setup_google() -> None:
    """Authorize Gmail + Calendar access once, so subagents can use them."""
    settings = _configure_readonly()
    try:
        run_google_setup(settings, echo=typer.echo)
    except GoogleSetupError as exc:
        typer.echo(f"google setup failed: {exc}")
        raise typer.Exit(1) from None


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
) -> None:
    """What the approval bridge has done, and the kill switch that stops it.

    The switch is a file, deliberately: it is checked afresh on every single request, so it
    takes effect on the next prompt with no restart and without editing any settings — and
    it still works when the thing you want to stop is the thing you would have to ask.
    """
    settings = _configure_readonly()
    state_dir = settings.data_dir / STATE_DIR_NAME
    switch = state_dir / KILL_SWITCH_NAME
    if disable and enable:
        typer.echo("pick one of --disable and --enable")
        raise typer.Exit(code=2)
    if disable:
        state_dir.mkdir(parents=True, exist_ok=True)
        switch.touch()
        typer.echo(f"approval escalation is off ({switch})")
        return
    if enable:
        switch.unlink(missing_ok=True)
        typer.echo("approval escalation is on")
        return

    typer.echo(f"escalation: {"OFF (kill switch)" if switch.exists() else "on"}, "
               f"after {settings.approval_escalate_seconds:g}s, "
               f"at most {settings.approval_max_per_hour}/hour")
    for line in _audit_lines(state_dir / AUDIT_NAME, limit):
        typer.echo(line)


def _audit_lines(path: Path, limit: int) -> list[str]:
    """The tail of the audit log as table rows, or one line saying there is none."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [APPROVALS_EMPTY]
    rows = [APPROVALS_HEADER]
    for line in raw[-max(limit, 1):]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = str(entry.get("ts", ""))[:16].replace("T", " ")
        what = entry.get("summary") or entry.get("reason") or entry.get("answer") or ""
        rows.append(
            f"{when:<17} {str(entry.get("event", "")):<20} "
            f"{str(entry.get("request_id", "")):>3}  {_shorten(str(what), MAX_DESCRIPTION_CHARS)}"
        )
    return rows if len(rows) > 1 else [APPROVALS_EMPTY]
