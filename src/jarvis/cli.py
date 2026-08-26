"""Jarvis command-line interface."""

import asyncio
import contextlib
import dataclasses
import logging
import logging.handlers
import signal
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

from jarvis.app import TASK_DB_NAME, AppState, build_app_state, shutdown_app_state
from jarvis.briefing import memory_path, read_memory
from jarvis.config import PLACEHOLDER_KEY, Settings, load_settings
from jarvis.doctor import format_check, has_hard_failure, run_doctor_checks
from jarvis.events import EventBus
from jarvis.google_setup import GoogleSetupError, run_google_setup
from jarvis.local_runner import LocalRunner
from jarvis.logscan import errors_since
from jarvis.logscan import marks as log_marks
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.restart import (
    RECORD_NAME,
    UNSUPPORTED_HINT,
    RestartRecord,
    RestartStore,
    current_version,
    health_probe,
    mask_number,
    resolve_target,
    wait_until_serving,
)
from jarvis.restart_watch import watch
from jarvis.server import create_app
from jarvis.session import VoiceSession
from jarvis.tasks.models import Task, TaskStatus
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolRegistry
from jarvis.transports.local_audio import LocalAudioDevice
from jarvis.transports.wav import WavTransport
from jarvis.wakeword import OpenWakeWordDetector, WakeWordListener

app = typer.Typer(help="Jarvis voice agent.")
tasks_app = typer.Typer(help="Inspect the tasks handed to subagents.")
app.add_typer(tasks_app, name="tasks")
log = logging.getLogger("jarvis.cli")

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


def _configure(**overrides: object) -> Settings:
    """Load settings (with any command-line overrides), make the data dirs, set up logging."""
    settings = load_settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    return settings


def _load_settings_optional() -> Settings:
    """Settings for commands that only read: a missing `OPENAI_API_KEY` is not fatal.

    `doctor` has to run *because* the install is incomplete, and `tasks`/`download-models`
    never talk to OpenAI at all — so the one required field falls back to a placeholder
    the doctor knows to report as unset.
    """
    try:
        return load_settings()
    except ValidationError:
        return load_settings(openai_api_key=PLACEHOLDER_KEY)


def _configure_readonly() -> Settings:
    """`_configure` for the read-only commands (no `OPENAI_API_KEY` required)."""
    settings = _load_settings_optional()
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
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
    _add_file_logging(settings)
    if no_phone and no_wakeword:
        typer.echo("nothing to run: both the phone server and the wake word are disabled")
        return
    asyncio.run(_serve(settings, phone=not no_phone, wakeword=not no_wakeword))


async def _serve(settings: Settings, *, phone: bool, wakeword: bool) -> None:
    """Run the phone server and/or the wake-word loop until one stops or ctrl-c."""
    state = build_app_state(settings)
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
    callback_task = (
        asyncio.create_task(
            state.restart.resume(wait_ready=ready, wakeword=wakeword), name="restart-callback"
        )
        if state.restart is not None
        else None
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

    target = resolve_target(settings)
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
        version=current_version(),
        log_marks=log_marks(settings.data_dir),
    )
    if not store.save(record):
        typer.echo(f"could not write {store.path}: the restart would go unconfirmed")
        raise typer.Exit(1)

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
    cannot kill it (see `jarvis.restart.watch_command`). Deliberately without
    `_add_file_logging`: its output belongs in `logs/restart-watch.log`, and writing its
    own "the restart never came back" into `jarvis.log` would leave the next restart
    scanning that line back as a fault of Jarvis's.
    """
    settings = _configure_readonly()
    typer.echo(asyncio.run(watch(settings)))


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
    """Whether Jarvis has told him about this one: only meaningful once it has finished."""
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


# --- diagnostics -----------------------------------------------------------


@app.command()
def doctor(
    no_mic: Annotated[
        bool, typer.Option("--no-mic", help="Skip the microphone probe (headless machines).")
    ] = False,
) -> None:
    """Check that this machine is set up to run Jarvis; exits non-zero on a hard failure."""
    settings = _load_settings_optional()
    checks = run_doctor_checks(settings, probe_mic=not no_mic)
    for check in checks:
        typer.echo(format_check(check))

    if has_hard_failure(checks):
        failed = sum(1 for check in checks if not check.ok and check.severity == "hard")
        typer.echo(f"\n{failed} check(s) failed.")
        raise typer.Exit(1)
    typer.echo("\nall good.")


@app.command("setup-google")
def setup_google() -> None:
    """Authorize Gmail + Calendar access once, so cowork subagents can use them."""
    settings = _configure_readonly()
    try:
        run_google_setup(settings, echo=typer.echo)
    except GoogleSetupError as exc:
        typer.echo(f"google setup failed: {exc}")
        raise typer.Exit(1) from None
