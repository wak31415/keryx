"""The always-on local channel: listen for "hey jarvis", run a session, listen again.

This is the idle <-> session state machine for the Mac's mic and speaker. The wake-word
sink stays installed for the whole run — detections are ignored while a session is live
rather than by unplugging the microphone — so a stray "hey jarvis" in the middle of a
conversation cannot restart the session underneath itself.

The sink is a plain callable that `LocalAudioDevice` hands over from the PortAudio thread
with `call_soon_threadsafe`, so `feed()` runs on the event loop and a detection is simply
an `asyncio.Event` the run loop is waiting on.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.realtime.base import RealtimeProvider
from jarvis.session import SessionRegistry, VoiceSession
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Transport
from jarvis.transports.local_audio import LocalAudioDevice, LocalTransport
from jarvis.wakeword import WakeWordListener

log = logging.getLogger("jarvis.local_runner")

# How long to wait for the "I'm listening" chime to finish before opening the mic.
CHIME_TIMEOUT_SECONDS = 2.0

ProviderFactory = Callable[[], RealtimeProvider]


class SessionFactory(Protocol):
    """Builds the session for one detection (injectable so tests can watch it)."""

    def __call__(
        self, transport: Transport, provider: RealtimeProvider, *, authorized: bool
    ) -> VoiceSession: ...


class LocalRunner:
    """Wake word in, voice session out, forever (until cancelled).

    `registry` is the *tool* registry shared with every session; `sessions` is the
    `SessionRegistry` of live sessions that announcements are routed to. Both factories
    are injectable so tests never touch a socket.
    """

    def __init__(
        self,
        settings: Settings,
        device: LocalAudioDevice,
        listener: WakeWordListener,
        *,
        provider_factory: ProviderFactory,
        registry: ToolRegistry,
        bus: EventBus,
        sessions: SessionRegistry,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._device = device
        self._listener = listener
        self._provider_factory = provider_factory
        self._registry = registry
        self._bus = bus
        self._sessions = sessions
        self._session_factory = session_factory or self._build_session
        self._woken = asyncio.Event()
        self._in_session = False

    async def run(self) -> None:
        """Listen for the wake word and run one session per detection, until cancelled."""
        self._device.start()
        self._device.set_wake_sink(self._on_wake_frame)
        log.info("listening for the wake word")
        try:
            while True:
                await self._woken.wait()
                self._woken.clear()
                await self._run_session()
        finally:
            self._device.set_wake_sink(None)
            self._device.stop()
            log.info("local runner stopped")

    def _on_wake_frame(self, frame: bytes) -> None:
        """Score one 16 kHz mic frame. Runs on the event loop, never the audio thread."""
        if self._in_session:
            return  # a "hey jarvis" mid-conversation is just conversation
        if self._listener.feed(frame):
            self._woken.set()

    async def _run_session(self) -> None:
        self._in_session = True
        try:
            self._device.chime()
            await self._device.wait_until_idle(CHIME_TIMEOUT_SECONDS)
            session = self._session_factory(
                LocalTransport(self._device), self._provider_factory(), authorized=True
            )
            await session.run()
        except Exception:  # one bad session must not stop the listener
            log.exception("local session failed")
        finally:
            self._in_session = False
            self._listener.reset()

    def _build_session(
        self, transport: Transport, provider: RealtimeProvider, *, authorized: bool
    ) -> VoiceSession:
        """The default factory: local sessions are pre-authorized (spec §3.3)."""
        return VoiceSession(
            transport,
            provider,
            self._settings,
            self._registry,
            self._bus,
            authorized=authorized,
            registry=self._sessions,
        )
