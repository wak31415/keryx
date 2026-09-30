"""The Mac's mic/speaker as a transport (spec §3.2, §4 sounddevice notes).

`LocalAudioDevice` is the only place that touches PortAudio, and it touches it through
an injectable `stream_factory`, so every behaviour below (routing, half-duplex gating,
wake-word resampling, the chime) is testable without hardware. `sounddevice` is imported
lazily inside the default factory.

The device is half-duplex: mic frames only reach the session while `AudioGate` allows
them, so the assistant never hears itself. The wake-word sink is deliberately *not*
gated — it must keep listening while idle — and receives the 16 kHz frames openWakeWord
expects.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Literal, Protocol

import numpy as np

from keryx.audio.util import AudioFormat, AudioGate, PlaybackBuffer, resample_pcm16
from keryx.transports.base import DRAIN_TIMEOUT_SECONDS, AudioIn, Hangup, TransportEvent
from keryx.wakeword import WAKE_SAMPLE_RATE

log = logging.getLogger("keryx.transports.local_audio")

StreamKind = Literal["input", "output"]
AudioSink = Callable[[bytes], None]

CHIME_TONES = (660.0, 880.0)
CHIME_TONE_MS = 125
CHIME_FADE_MS = 8.0
CHIME_AMPLITUDE = 0.25


class AudioStream(Protocol):
    """The slice of a `sounddevice` stream the device uses."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


StreamFactory = Callable[..., AudioStream]


def sounddevice_stream_factory(
    kind: StreamKind, *, samplerate: int, blocksize: int, callback: Callable
) -> AudioStream:
    """Default stream factory: real PortAudio streams (`sounddevice` imported lazily)."""
    import sounddevice as sd

    if kind == "input":
        return sd.InputStream(
            samplerate=samplerate,
            dtype="int16",
            channels=1,
            blocksize=blocksize,
            callback=callback,
        )
    return sd.RawOutputStream(
        samplerate=samplerate,
        dtype="int16",
        channels=1,
        blocksize=blocksize,
        callback=callback,
    )


def build_chime(sample_rate: int = 24000) -> bytes:
    """Generate the "I'm listening" chime: two short sine tones, fade in/out, 16-bit LE."""
    samples_per_tone = int(sample_rate * CHIME_TONE_MS / 1000)
    fade = min(int(sample_rate * CHIME_FADE_MS / 1000), samples_per_tone // 2)
    envelope = np.ones(samples_per_tone)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade)
        envelope[:fade] = ramp
        envelope[-fade:] = ramp[::-1]

    t = np.arange(samples_per_tone) / sample_rate
    parts = [np.sin(2 * np.pi * freq * t) * CHIME_AMPLITUDE * envelope for freq in CHIME_TONES]
    return (np.concatenate(parts) * 32767).astype("<i2").tobytes()


class LocalAudioDevice:
    """The Mac mic + speaker, with half-duplex gating and a wake-word tap.

    Mic frames (`frame_samples` int16 samples at `sample_rate`) go to two sinks: the mic
    sink, only while `gate.should_pass_mic()` (that is the half-duplex rule), and the
    wake sink, always, resampled to 16 kHz / 1280 samples. Sinks are plain callables
    invoked on the asyncio loop captured at `start()` — the PortAudio callbacks run on
    their own thread, so every hand-off goes through `loop.call_soon_threadsafe`.

    Playback is a `PlaybackBuffer` drained by the output callback; the first read that
    finds real audio flips the gate to SPEAKING and the read that empties the buffer
    starts the gate's hangover window.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        frame_samples: int = 1920,
        gate: AudioGate | None = None,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.gate = gate if gate is not None else AudioGate()
        self._stream_factory = stream_factory or sounddevice_stream_factory
        self._playback = PlaybackBuffer()
        self._mic_sink: AudioSink | None = None
        self._wake_sink: AudioSink | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._input: AudioStream | None = None
        self._output: AudioStream | None = None

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Open and start the mic and speaker streams. Must run on the asyncio loop."""
        if self._input is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._input = self._stream_factory(
            "input",
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            callback=self._on_input,
        )
        self._output = self._stream_factory(
            "output",
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            callback=self._on_output,
        )
        self._input.start()
        self._output.start()
        log.info(
            "local audio started (%d Hz, %d-sample frames)", self.sample_rate, self.frame_samples
        )

    def stop(self) -> None:
        """Stop and close both streams and drop any pending playback."""
        for stream in (self._input, self._output):
            if stream is not None:
                stream.stop()
                stream.close()
        self._input = None
        self._output = None
        self._loop = None
        self.clear_playback()

    # --- mic --------------------------------------------------------------

    def set_mic_sink(self, sink: AudioSink | None) -> None:
        """Route gated mic frames (24 kHz) to `sink`, or nowhere when None."""
        self._mic_sink = sink

    def set_wake_sink(self, sink: AudioSink | None) -> None:
        """Route ungated 16 kHz / 1280-sample frames to `sink`, or nowhere when None."""
        self._wake_sink = sink

    def _on_input(self, indata, frames: int, time_info, status) -> None:
        """PortAudio input callback. Runs on the PortAudio thread.

        Nothing may escape: an exception out of a callback stops the stream, and a mic
        that has quietly gone deaf is the one failure nobody notices.
        """
        try:
            self._read_input(indata, status)
        except Exception:
            log.exception("the input callback failed")

    def _read_input(self, indata, status) -> None:
        if status:
            log.warning("input stream status: %s", status)
        pcm = np.ascontiguousarray(indata, dtype="<i2").tobytes()

        wake_sink = self._wake_sink
        if wake_sink is not None:
            self._deliver(wake_sink, resample_pcm16(pcm, self.sample_rate, WAKE_SAMPLE_RATE))

        mic_sink = self._mic_sink
        if mic_sink is not None and self.gate.should_pass_mic():
            self._deliver(mic_sink, pcm)

    def _deliver(self, sink: AudioSink, pcm: bytes) -> None:
        """Hand a frame from the PortAudio thread to the asyncio loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(sink, pcm)
        except RuntimeError:  # loop closed while the stream was still running
            log.debug("dropped a mic frame: event loop is closed")

    # --- speaker ----------------------------------------------------------

    def play(self, data: bytes) -> None:
        """Queue assistant audio (16-bit LE mono at `sample_rate`) for playback."""
        self._playback.write(data)

    def clear_playback(self) -> None:
        """Drop queued playback audio and release the gate (barge-in / session end)."""
        self._playback.clear()
        self.gate.on_playback_drain()

    def chime(self) -> None:
        """Queue the short two-tone "listening" chime; returns before it has played."""
        self.play(build_chime(self.sample_rate))

    @property
    def is_playing(self) -> bool:
        """True while audio is queued or the gate still considers the speaker busy."""
        return not self._playback.is_empty or self.gate.is_speaking

    async def wait_until_idle(
        self, timeout: float = DRAIN_TIMEOUT_SECONDS, *, poll_s: float = 0.01
    ) -> bool:
        """Poll until playback finishes; False if `timeout` elapses first."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.is_playing:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(poll_s)
        return True

    def _on_output(self, outdata, frames: int, time_info, status) -> None:
        """PortAudio output callback. Runs on the PortAudio thread (see `_on_input`)."""
        try:
            self._write_output(outdata, frames, status)
        except Exception:
            log.exception("the output callback failed")

    def _write_output(self, outdata, frames: int, status) -> None:
        if status:
            log.warning("output stream status: %s", status)
        wanted = frames * 2
        pending = self._playback.pending_bytes
        outdata[:] = self._playback.read(wanted)

        if pending > 0:
            self.gate.on_playback_start()
        # `drained` means this read hit the end of the buffer; only report it while the
        # gate still thinks we are speaking, so an idle stream doesn't keep restarting
        # the hangover window.
        if self._playback.drained and self.gate.is_speaking:
            self.gate.on_playback_drain()


class LocalTransport:
    """One voice session over a `LocalAudioDevice` (spec §3.2 `Transport`).

    Mic frames arrive on the device's mic sink and are queued as `AudioIn`; `hangup`
    detaches the sink, drops pending playback and finishes `events()` with `Hangup`.
    There is no DTMF on this channel, and local sessions are pre-authorized.
    """

    channel: Literal["phone", "local"] = "local"
    caller: str | None = None
    audio_format: AudioFormat = "audio/pcm"

    def __init__(self, device: LocalAudioDevice) -> None:
        self._device = device
        self._queue: asyncio.Queue[TransportEvent] = asyncio.Queue()
        self._closed = False
        device.set_mic_sink(self._on_mic_frame)

    def _on_mic_frame(self, pcm: bytes) -> None:
        if self._closed:
            return
        self._queue.put_nowait(AudioIn(pcm))

    def events(self) -> AsyncIterator[TransportEvent]:
        """Mic audio as `AudioIn`, ending with `Hangup`."""
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[TransportEvent]:
        while True:
            event = await self._queue.get()
            yield event
            if isinstance(event, Hangup):
                return

    async def send_audio(self, data: bytes) -> None:
        self._device.play(data)

    async def clear(self) -> None:
        self._device.clear_playback()

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> bool:
        """Wait until queued playback has finished; False if `timeout` elapses first.

        The optional hook `VoiceSession` looks for before hanging up: playback here is a
        queue feeding the speaker, so hanging up straight away would cut the goodbye off
        mid-word. Transports that play audio synchronously simply don't offer it.
        """
        return await self._device.wait_until_idle(timeout)

    async def hangup(self, reason: str = "local session ended") -> None:
        """Detach the mic, drop playback and finish `events()`. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._device.set_mic_sink(None)
        self._device.clear_playback()
        self._queue.put_nowait(Hangup(reason))
        log.info("local session ended: %s", reason)
