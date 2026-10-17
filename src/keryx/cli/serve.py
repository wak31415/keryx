"""`keryx serve`, and `keryx loopback`, the WAV harness that runs a session the same way."""

import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from keryx import plugins
from keryx.app import AppState, build_app_state, shutdown_app_state
from keryx.cli.apps import app
from keryx.cli.common import LOG_FORMAT, _configure
from keryx.config import Settings, secure_dir
from keryx.config.permissions import ACTOR_ENVS, SERVICE
from keryx.config.store import ConfigStore
from keryx.continuity.retention import prune
from keryx.events import EventBus
from keryx.realtime import make_provider
from keryx.restart.health import wait_until_serving
from keryx.restart.version import mark_running, mark_startup_logs
from keryx.server import create_app
from keryx.session import VoiceSession
from keryx.tasks.manager import install_default_executor
from keryx.tools import ToolRegistry
from keryx.transports.wav import WavTransport

log = logging.getLogger("keryx.cli")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


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
