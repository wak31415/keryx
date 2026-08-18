"""Jarvis command-line interface."""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from jarvis.app import AppState, build_app_state, shutdown_app_state
from jarvis.config import Settings, load_settings
from jarvis.events import EventBus
from jarvis.local_runner import LocalRunner
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.server import create_app
from jarvis.session import VoiceSession
from jarvis.tools import ToolRegistry
from jarvis.transports.local_audio import LocalAudioDevice
from jarvis.transports.wav import WavTransport
from jarvis.wakeword import OpenWakeWordDetector, WakeWordListener

app = typer.Typer(help="Jarvis voice agent.")
log = logging.getLogger("jarvis.cli")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _configure(**overrides: object) -> Settings:
    """Load settings (with any command-line overrides), make the data dirs, set up logging."""
    settings = load_settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    return settings


def _new_provider(settings: Settings) -> OpenAIRealtimeClient:
    """A fresh (not yet connected) realtime provider."""
    return OpenAIRealtimeClient(settings.openai_api_key, settings.openai_realtime_model)


@app.command("download-models")
def download_models() -> None:
    """Download the configured wake-word model via openwakeword."""
    import openwakeword.utils

    settings = load_settings()
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
