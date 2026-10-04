"""G.711 µ-law, and a transcoder between the phone's audio and a voice server's.

Twilio carries a call as µ-law at 8 kHz, and OpenAI's Realtime API takes that as it is
(`audio/pcmu`), so the phone path has never transcoded anything. A voice server of the
owner's own may not: speech-to-speech reads every input as 16-bit PCM whatever the format
says. `Transcoder` stands between the two — µ-law 8 kHz on the call's side, PCM16 24 kHz on
the server's — so the session, barge-in's arithmetic and Twilio never see the difference.

Both directions resample as streams, so a 20 ms frame is not filtered in isolation and the
seams between frames stay inaudible. A stream holds back its filter's delay (about 35 ms at
the quality used, which is plenty for a phone line), so the end of each reply is flushed
out explicitly rather than left behind until the next one.
"""

import numpy as np
import soxr

#: The rates on each side: µ-law is 8 kHz by definition; `audio/pcm` is declared at 24 kHz.
ULAW_RATE = 8000
PCM_RATE = 24000
#: soxr's medium quality: a third less delay than its default, and the line is 8 kHz.
RESAMPLE_QUALITY = "MQ"

_BIAS = 0x84
#: The top of each µ-law segment, on the 14-bit scale the standard encodes from.
_SEGMENT_ENDS = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])
_CLIP_14 = 8159


def _decode_table() -> np.ndarray:
    codes = ~np.arange(256, dtype=np.int32) & 0xFF
    exponent = (codes >> 4) & 0x07
    magnitude = ((((codes & 0x0F) << 3) + _BIAS) << exponent) - _BIAS
    return np.where(codes & 0x80, -magnitude, magnitude).astype("<i2")


def _encode_table() -> np.ndarray:
    """Every 16-bit sample's code, the way G.711 (and CPython's old `audioop`) computes it:
    from the top 14 bits, with the sign folded in by the mask."""
    value = np.arange(-32768, 32768, dtype=np.int32) >> 2
    negative = value < 0
    value = np.minimum(np.where(negative, -value, value), _CLIP_14) + (_BIAS >> 2)
    segment = np.searchsorted(_SEGMENT_ENDS, value)
    code = np.where(
        segment >= 8, 0x7F, (segment << 4) | ((value >> np.minimum(segment + 1, 8)) & 0x0F)
    )
    return (code ^ np.where(negative, 0x7F, 0xFF)).astype(np.uint8)


_DECODE = _decode_table()
#: Indexed by a sample plus 32768.
_ENCODE = _encode_table()


def ulaw_decode(data: bytes) -> bytes:
    """µ-law bytes as 16-bit little-endian PCM at the same rate."""
    return _DECODE[np.frombuffer(data, dtype=np.uint8)].tobytes()


def ulaw_encode(pcm: bytes) -> bytes:
    """16-bit little-endian PCM as µ-law bytes at the same rate."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    return _ENCODE[samples + 32768].tobytes()


class Transcoder:
    """µ-law 8 kHz on the call's side, PCM16 24 kHz on the voice server's, both ways."""

    def __init__(self) -> None:
        self._up = _stream(ULAW_RATE, PCM_RATE)
        self._down = _stream(PCM_RATE, ULAW_RATE)
        #: A delta that splits a sample in two leaves its first byte here for the next.
        self._odd = b""

    def to_wire(self, ulaw: bytes) -> bytes:
        """Caller audio, as the voice server takes it."""
        samples = np.frombuffer(ulaw_decode(ulaw), dtype="<i2")
        return self._up.resample_chunk(samples).astype("<i2").tobytes()

    def from_wire(self, pcm: bytes) -> bytes:
        """Assistant audio, as the call carries it."""
        pcm, self._odd = self._odd + pcm, b""
        if len(pcm) % 2:
            pcm, self._odd = pcm[:-1], pcm[-1:]
        samples = np.frombuffer(pcm, dtype="<i2")
        return ulaw_encode(self._down.resample_chunk(samples).astype("<i2").tobytes())

    def flush(self) -> bytes:
        """The end of a reply, which the resampler was still holding, as µ-law."""
        tail = self._down.resample_chunk(np.zeros(0, dtype="<i2"), last=True)
        self._down, self._odd = _stream(PCM_RATE, ULAW_RATE), b""
        return ulaw_encode(tail.astype("<i2").tobytes())


def _stream(src: int, dst: int) -> soxr.ResampleStream:
    return soxr.ResampleStream(src, dst, 1, dtype="int16", quality=RESAMPLE_QUALITY)
