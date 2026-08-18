"""A WAV file as a transport: the dev harness behind `jarvis loopback`.

It plays a recording at the model as if it were a microphone — 20 ms frames, paced in
real time so server VAD behaves the way it would on a live call — then feeds silence for
a few seconds so the model gets its turn, and hangs up. Everything the model says back is
collected and written to a WAV file when the session ends.

Same channel semantics as the local device (`audio/pcm`, 24 kHz mono, no barge-in), so a
loopback run exercises the same session code path as "hey jarvis" does.
"""

import asyncio
import logging
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import numpy as np

from jarvis.audio.util import AudioFormat, chunk_bytes, resample_pcm16
from jarvis.transports.base import AudioIn, Hangup, TransportEvent

log = logging.getLogger("jarvis.transports.wav")

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 960 bytes = 20 ms of 24 kHz mono PCM16


def read_wav_pcm24k(path: Path | str) -> bytes:
    """Read a 16-bit WAV as 24 kHz mono PCM16 (stereo keeps the left channel)."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV files are supported (got {width * 8}-bit)")
    if channels > 1:
        samples = np.frombuffer(frames, dtype="<i2").reshape(-1, channels)
        frames = np.ascontiguousarray(samples[:, 0]).tobytes()
    return resample_pcm16(frames, rate, SAMPLE_RATE)


class WavTransport:
    """One session driven by a WAV file, with the reply written to another WAV file."""

    channel: Literal["phone", "local"] = "local"
    caller: str | None = None
    audio_format: AudioFormat = "audio/pcm"

    def __init__(
        self,
        path: Path | str,
        *,
        out_path: Path | str = "reply.wav",
        tail_seconds: float = 8.0,
    ) -> None:
        self.path = Path(path)
        self.out_path = Path(out_path)
        self.tail_seconds = tail_seconds
        self._pcm = read_wav_pcm24k(self.path)
        self._reply = bytearray()

    def events(self) -> AsyncIterator[TransportEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[TransportEvent]:
        for chunk in chunk_bytes(self._pcm, FRAME_BYTES):
            yield AudioIn(chunk)
            await asyncio.sleep(FRAME_MS / 1000)

        silence = b"\x00" * FRAME_BYTES
        for _ in range(int(self.tail_seconds * 1000 // FRAME_MS)):
            yield AudioIn(silence)
            await asyncio.sleep(FRAME_MS / 1000)

        yield Hangup("eof")

    async def send_audio(self, data: bytes) -> None:
        self._reply.extend(data)

    async def clear(self) -> None:
        """No-op: the "playback" is a file, so there is nothing queued to drop."""

    async def hangup(self) -> None:
        """Write everything the model said to `out_path` (24 kHz mono PCM16)."""
        with wave.open(str(self.out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(self._reply))
        log.info("wrote %d bytes of reply audio to %s", len(self._reply), self.out_path)
