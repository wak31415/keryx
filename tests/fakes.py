"""Scriptable doubles for the transport and provider a `VoiceSession` drives.

Both fakes are event *scripts*: a test feeds provider/transport events one at a time and
asserts on what the session did with them. Neither touches a socket, a mic or a file.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

from jarvis.realtime.base import ProviderEvent, SessionConfig
from jarvis.transports.base import AudioFormat, Hangup, TransportEvent

_END = object()

TIMEOUT = 2.0


async def eventually(predicate: Callable[[], bool], *, timeout: float = TIMEOUT) -> None:
    """Poll `predicate` until it is true, or fail the test after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was still false after the timeout")
        await asyncio.sleep(0.005)


class _Script:
    """A queue of events an async iterator drains until `end()` is queued."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._ended = False

    def feed(self, event: object) -> None:
        self._queue.put_nowait(event)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._queue.put_nowait(_END)

    async def stream(self) -> AsyncIterator:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield item


class FakeProvider:
    """A `RealtimeProvider` that records every call and replays a scripted event stream."""

    def __init__(self, *, reconnect_result: bool = True) -> None:
        self.config: SessionConfig | None = None
        self.connects = 0
        self.connect_error: Exception | None = None
        self.send_error: Exception | None = None
        self.sent_audio: list[bytes] = []
        self.tool_results: list[tuple[str, dict | str]] = []
        #: Whether each tool result asked for a spoken turn, in the same order.
        self.tool_responses: list[bool] = []
        self.injected: list[tuple[str, bool, str | None]] = []
        self.truncations: list[tuple[str, int]] = []
        #: Every `update_instructions` call, in order; `config` stays what `connect` got.
        self.instruction_updates: list[str] = []
        self.cancels = 0
        self.closed = False
        self.reconnects = 0
        self.reconnect_result = reconnect_result
        self._script = _Script()

    # --- scripting ---------------------------------------------------------

    def feed(self, event: ProviderEvent) -> None:
        """Queue one provider event for the session to consume."""
        self._script.feed(event)

    def end(self) -> None:
        """Finish `events()` (what a failed reconnect or `close()` does for real)."""
        self._script.end()

    # --- provider protocol -------------------------------------------------

    async def connect(self, config: SessionConfig) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connects += 1
        self.config = config

    async def close(self) -> None:
        self.closed = True
        self.end()

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._script.stream()

    async def send_audio(self, data: bytes) -> None:
        self._guard()
        self.sent_audio.append(data)

    async def submit_tool_result(
        self, call_id: str, output: dict | str, *, respond: bool = True
    ) -> None:
        self._guard()
        self.tool_results.append((call_id, output))
        self.tool_responses.append(respond)

    async def inject_message(
        self, text: str, *, respond: bool = True, response_instructions: str | None = None
    ) -> None:
        self._guard()
        self.injected.append((text, respond, response_instructions))

    async def update_instructions(self, instructions: str) -> None:
        self._guard()
        self.instruction_updates.append(instructions)

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self._guard()
        self.truncations.append((item_id, audio_end_ms))

    async def cancel_response(self) -> None:
        self._guard()
        self.cancels += 1

    async def reconnect(self) -> bool:
        self.reconnects += 1
        if not self.reconnect_result:
            self.end()  # the real client's iterator ends when a reconnect fails
        return self.reconnect_result

    def _guard(self) -> None:
        """Raise the configured send failure, the way a dropped socket would."""
        if self.send_error is not None:
            raise self.send_error


class FakeTransport:
    """A `Transport` that records outbound audio and replays scripted inbound events."""

    def __init__(
        self,
        *,
        channel: str = "phone",
        caller: str | None = "+15555555555",
        audio_format: AudioFormat = "audio/pcmu",
    ) -> None:
        self.channel = channel
        self.caller = caller
        self.audio_format = audio_format
        self.sent: list[bytes] = []
        self.cleared = 0
        self.hung_up = False
        self.calls: list[str] = []  # ordered record of clear/drain/hangup
        self._script = _Script()

    # --- scripting ---------------------------------------------------------

    def feed(self, event: TransportEvent) -> None:
        """Queue one inbound transport event."""
        self._script.feed(event)

    def end(self) -> None:
        """Finish `events()` without a `Hangup` (a socket that just died)."""
        self._script.end()

    # --- transport protocol ------------------------------------------------

    def events(self) -> AsyncIterator[TransportEvent]:
        return self._script.stream()

    async def send_audio(self, data: bytes) -> None:
        self.sent.append(data)

    async def clear(self) -> None:
        self.cleared += 1
        self.calls.append("clear")

    async def hangup(self) -> None:
        self.hung_up = True
        self.calls.append("hangup")
        self._script.feed(Hangup("hung up"))
        self._script.end()


class DrainingFakeTransport(FakeTransport):
    """A transport that also offers the optional `drain` hook (like `LocalTransport`)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.drains: list[float] = []

    async def drain(self, timeout: float = 5.0) -> bool:
        self.drains.append(timeout)
        self.calls.append("drain")
        return True


class FakeAudioDevice:
    """The slice of `LocalAudioDevice` that `LocalRunner` and `LocalTransport` use."""

    def __init__(self) -> None:
        self.started = False
        self.stops = 0
        self.chimes = 0
        self.played: list[bytes] = []
        self.wake_sink = None
        self.mic_sink = None
        self.idle_waits: list[float] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False
        self.stops += 1

    def set_wake_sink(self, sink) -> None:
        self.wake_sink = sink

    def set_mic_sink(self, sink) -> None:
        self.mic_sink = sink

    def chime(self) -> None:
        self.chimes += 1

    def play(self, data: bytes) -> None:
        self.played.append(data)

    def clear_playback(self) -> None:
        self.played.clear()

    async def wait_until_idle(self, timeout: float = 5.0, *, poll_s: float = 0.01) -> bool:
        self.idle_waits.append(timeout)
        return True

    def wake(self, frame: bytes = b"wake") -> None:
        """Deliver one wake-word frame the way the PortAudio callback would."""
        assert self.wake_sink is not None, "no wake sink installed"
        self.wake_sink(frame)


class FakeWakeListener:
    """A `WakeWordListener` stand-in: every frame equal to `wake_frame` is a detection."""

    def __init__(self, wake_frame: bytes = b"wake") -> None:
        self.wake_frame = wake_frame
        self.frames: list[bytes] = []
        self.resets = 0

    def feed(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return frame == self.wake_frame

    def reset(self) -> None:
        self.resets += 1


class FakeVoiceSession:
    """The slice of `VoiceSession` the notifier and the session registry touch.

    `accepts` is what `announce()` returns — False is a session that was already on its
    way out — and `error` makes it raise, the way a dead provider socket would.
    """

    def __init__(
        self,
        *,
        channel: str = "local",
        session_id: str = "sess-1",
        is_live: bool = True,
        accepts: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.channel = channel
        self.session_id = session_id
        self.is_live = is_live
        self.accepts = accepts
        self.error = error
        self.announced: list[str] = []

    async def announce(self, text: str) -> bool:
        if self.error is not None:
            raise self.error
        if self.accepts:
            self.announced.append(text)
        return self.accepts
