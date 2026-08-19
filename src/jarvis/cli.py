"""Jarvis command-line interface."""

import asyncio
import contextlib
import dataclasses
import logging
import logging.handlers
import signal
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

from jarvis.app import TASK_DB_NAME, AppState, build_app_state, shutdown_app_state
from jarvis.config import PLACEHOLDER_KEY, Settings, load_settings
from jarvis.doctor import format_check, has_hard_failure, run_doctor_checks
from jarvis.events import EventBus
from jarvis.google_setup import GoogleSetupError, run_google_setup
from jarvis.local_runner import LocalRunner
from jarvis.realtime.openai import OpenAIRealtimeClient
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


def _new_provider(settings: Settings) -> OpenAIRealtimeClient:
    """A fresh (not yet connected) realtime provider."""
    return OpenAIRealtimeClient(settings.openai_api_key, settings.openai_realtime_model)


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
) -> None:
    """Run Jarvis: the Twilio phone server and the local "hey jarvis" listener."""
    # Only pass the override when it was asked for, so the default path stays untouched.
    settings = _configure(fake_agents=True) if fake_agents else _configure()
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
        _new_provider(settings),
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


async def _read_tasks(settings: Settings, status: TaskStatus | None, limit: int) -> list[Task]:
    """Read straight from the store: `tasks` never starts a manager or a subagent."""
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        return await store.list(status=status, limit=limit)
    finally:
        await store.close()


async def _read_task(settings: Settings, task_id: int) -> Task | None:
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        return await store.get(task_id)
    finally:
        await store.close()


@tasks_app.command("list")
def tasks_list(
    status: Annotated[
        StatusFilter, typer.Option("--status", help="Only show tasks in this status.")
    ] = StatusFilter.ALL,
    limit: Annotated[int, typer.Option("--limit", help="How many tasks to show.")] = 20,
) -> None:
    """List recent tasks, newest first."""
    settings = _configure_readonly()
    wanted = None if status is StatusFilter.ALL else TaskStatus(status.value)
    tasks = asyncio.run(_read_tasks(settings, wanted, limit))
    if not tasks:
        typer.echo("no tasks" if wanted is None else f"no tasks with status {wanted}")
        return

    typer.echo(f"{'ID':>4}  {'STATUS':<9}  {'KIND':<8}  {'CREATED':<16}  DESCRIPTION")
    for task in tasks:
        typer.echo(
            f"{task.id:>4}  {task.status:<9}  {task.kind:<8}  "
            f"{_local_time(task.created_at):<16}  "
            f"{_shorten(task.description, MAX_DESCRIPTION_CHARS)}"
        )


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

    failures = [check for check in checks if not check.ok and check.severity == "hard"]
    if has_hard_failure(checks):
        typer.echo(f"\n{len(failures)} check(s) failed.")
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
