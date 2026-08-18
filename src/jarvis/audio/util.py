"""Audio format helpers: resampling, G.711 µ-law codec, chunking, and the half-duplex
gate + playback FIFO used by the local (PortAudio) transport.

`AudioFormat` is defined here (rather than `transports/base.py`, which doesn't exist yet)
because `ms_for_bytes` needs it; a later task re-exports it from `transports/base.py`.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Literal

import numpy as np
import soxr

AudioFormat = Literal["audio/pcmu", "audio/pcm"]

# Bytes per millisecond for each wire format: pcmu is 8 kHz 8-bit (1 byte/sample),
# pcm is 24 kHz 16-bit mono (2 bytes/sample) -> 24 * 2 = 48 bytes/ms.
_BYTES_PER_MS: dict[AudioFormat, int] = {
    "audio/pcmu": 8,
    "audio/pcm": 48,
}


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit LE mono PCM from `src_rate` to `dst_rate`. Passthrough if equal."""
    if src_rate == dst_rate:
        return data
    samples = np.frombuffer(data, dtype="<i2")
    resampled = soxr.resample(samples, src_rate, dst_rate)
    return resampled.astype("<i2").tobytes()


# --- G.711 µ-law codec (standard reference algorithm: bias 0x84, clip 32635) ------------

_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635
# Segment (exponent) upper bounds; exponent = first index whose bound >= the biased magnitude.
_MULAW_SEG_END = np.array(
    [0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF], dtype=np.int32
)


def pcm16_to_mulaw(data: bytes) -> bytes:
    """Encode 16-bit LE mono PCM to G.711 µ-law (one byte per sample). Vectorised, no loop."""
    samples = np.frombuffer(data, dtype="<i2").astype(np.int32)

    sign = np.where(samples < 0, 0x80, 0x00).astype(np.int32)
    magnitude = np.minimum(np.abs(samples), _MULAW_CLIP) + _MULAW_BIAS
    exponent = np.searchsorted(_MULAW_SEG_END, magnitude, side="left").astype(np.int32)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F

    ulaw = (~(sign | (exponent << 4) | mantissa)) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Decode G.711 µ-law bytes back to 16-bit LE mono PCM."""
    coded = np.frombuffer(data, dtype=np.uint8).astype(np.int32)
    u = (~coded) & 0xFF

    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F

    magnitude = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample = np.where(sign != 0, _MULAW_BIAS - magnitude, magnitude - _MULAW_BIAS)
    sample = np.clip(sample, -32768, 32767).astype(np.int16)
    return sample.astype("<i2").tobytes()


def chunk_bytes(data: bytes, size: int) -> Iterator[bytes]:
    """Yield successive `size`-byte chunks of `data`; the final chunk may be shorter."""
    for i in range(0, len(data), size):
        yield data[i : i + size]


def ms_for_bytes(n: int, fmt: AudioFormat) -> float:
    """Milliseconds of audio represented by `n` bytes in wire format `fmt`."""
    return n / _BYTES_PER_MS[fmt]


class AudioGate:
    """Half-duplex mic/speaker gate for the local (PortAudio) transport.

    Pure logic, no I/O: called from the PortAudio callback thread, so all state mutation
    is guarded by a lock. Two states: LISTENING (mic passes through) and SPEAKING (mic
    gated while assistant audio plays). After playback drains, the mic stays gated for
    `hangover_s` more seconds so the mic doesn't pick up the tail of the speaker output.
    `now` is an injectable monotonic clock so tests can control time without sleeping.
    """

    def __init__(self, hangover_s: float = 0.2, *, now: Callable[[], float] = time.monotonic):
        self._hangover_s = hangover_s
        self._now = now
        self._lock = threading.Lock()
        self._speaking = False
        self._drain_time: float | None = None

    def on_playback_start(self) -> None:
        """Call when speaker output begins (first non-empty read of the playback buffer)."""
        with self._lock:
            self._speaking = True
            self._drain_time = None

    def on_playback_drain(self) -> None:
        """Call when the playback buffer runs empty; starts the hangover window."""
        with self._lock:
            self._speaking = False
            self._drain_time = self._now()

    def should_pass_mic(self) -> bool:
        """False while SPEAKING and for `hangover_s` after the most recent drain."""
        with self._lock:
            if self._speaking:
                return False
            if self._drain_time is not None and (self._now() - self._drain_time) < self._hangover_s:
                return False
            return True

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking


class PlaybackBuffer:
    """Thread-safe byte FIFO feeding the PortAudio output callback.

    `write` is called by the producer (e.g. provider audio deltas arriving on the asyncio
    loop); `read` is called from the realtime audio callback thread and always returns
    exactly `n` bytes. If fewer than `n` real bytes are buffered, the shortfall is
    zero-padded and `drained` is set True for that read (and stays True, since padding
    means the buffer emptied); the next read that is fully satisfied from real data sets
    `drained` back to False. Callers that care about underruns should check `drained`
    right after each `read`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.drained = False

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def read(self, n: int) -> bytes:
        with self._lock:
            available = len(self._buf)
            if available >= n:
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
                self.drained = False
                return chunk

            chunk = bytes(self._buf) + b"\x00" * (n - available)
            self._buf.clear()
            self.drained = True
            return chunk

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self.drained = False

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return len(self._buf) == 0
