"""Jarvis command-line interface."""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Annotated

import typer

from jarvis.config import Settings, load_settings
from jarvis.events import EventBus
from jarvis.local_runner import LocalRunner
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.session import SessionRegistry, VoiceSession
from jarvis.tools import ToolRegistry
from jarvis.transports.local_audio import LocalAudioDevice
from jarvis.transports.wav import WavTransport
from jarvis.wakeword import OpenWakeWordDetector, WakeWordListener

app = typer.Typer(help="Jarvis voice agent.")
log = logging.getLogger("jarvis.cli")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _configure() -> Settings:
    """Load settings, create the data directories, and set up logging."""
    settings = load_settings()
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
) -> None:
    """Run Jarvis: the local "hey jarvis" listener (the phone server lands in task 6)."""
    settings = _configure()
    if not no_phone:
        typer.echo("phone server not implemented yet (Task 6); continuing without it")
    if no_wakeword:
        typer.echo("nothing to run: wake word disabled and there is no phone server yet")
        return
    asyncio.run(_serve_local(settings))


async def _serve_local(settings: Settings) -> None:
    """Run the wake-word loop until ctrl-c."""
    device = LocalAudioDevice()
    listener = WakeWordListener(
        OpenWakeWordDetector(settings.wakeword_model), threshold=settings.wakeword_threshold
    )
    runner = LocalRunner(
        settings,
        device,
        listener,
        provider_factory=lambda: _new_provider(settings),
        registry=ToolRegistry(),  # task 10 fills this with the real tools
        bus=EventBus(),
        sessions=SessionRegistry(),
    )

    task = asyncio.create_task(runner.run(), name="local-runner")
    _cancel_on_signals(task)
    typer.echo('listening — say "hey jarvis" (ctrl-c to quit)')
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _cancel_on_signals(task: asyncio.Task) -> None:
    """Turn ctrl-c / SIGTERM into a clean cancellation of `task`."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, task.cancel)


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
