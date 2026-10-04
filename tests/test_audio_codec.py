import warnings

import numpy as np
import pytest

from keryx.audio.codec import Transcoder, ulaw_decode, ulaw_encode

EVERY_SAMPLE = np.arange(-32768, 32768, dtype="<i2").tobytes()


def _tone(seconds: float, rate: int, amplitude: int = 20000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * amplitude).astype("<i2")


def test_silence_and_the_extremes():
    assert ulaw_encode(np.zeros(4, "<i2").tobytes()) == b"\xff" * 4
    assert ulaw_decode(b"\xff\x7f") == np.zeros(2, "<i2").tobytes()
    assert ulaw_encode(np.array([32767, -32768], "<i2").tobytes()) == b"\x80\x00"


def test_decode_inverts_encode_within_the_step_size():
    pcm = np.frombuffer(EVERY_SAMPLE, "<i2").astype(np.int32)
    back = np.frombuffer(ulaw_decode(ulaw_encode(EVERY_SAMPLE)), "<i2").astype(np.int32)
    # µ-law steps double per segment; the largest is 1024, so half of it bounds the error.
    assert np.max(np.abs(back - pcm)) <= 1024


def test_matches_the_reference_codec_where_there_still_is_one():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        audioop = pytest.importorskip("audioop")
    assert ulaw_encode(EVERY_SAMPLE) == audioop.lin2ulaw(EVERY_SAMPLE, 2)
    assert ulaw_decode(bytes(range(256))) == audioop.ulaw2lin(bytes(range(256)), 2)


def test_a_call_frame_by_frame_comes_out_at_three_times_the_samples():
    ulaw = ulaw_encode(_tone(1.0, 8000).tobytes())
    transcoder = Transcoder()
    wire = b"".join(transcoder.to_wire(ulaw[i : i + 160]) for i in range(0, len(ulaw), 160))
    # A streaming resampler holds back its filter's delay, a few milliseconds at most.
    assert 0.95 * 48000 <= len(wire) <= 48000
    assert len(wire) % 2 == 0


def test_the_reply_comes_back_at_a_third_of_the_samples_and_split_samples_are_kept():
    pcm = _tone(1.0, 24000).tobytes()
    transcoder = Transcoder()
    # Deltas of odd sizes split a sample between them; nothing may be lost or misaligned.
    out = b"".join(transcoder.from_wire(pcm[i : i + 4801]) for i in range(0, len(pcm), 4801))
    assert 0.95 * 8000 <= len(out) <= 8000
    assert len(out) + len(transcoder.flush()) == 8000


def test_a_tone_survives_the_round_trip():
    tone = _tone(0.5, 24000, amplitude=8000)
    there, back = Transcoder(), Transcoder()
    ulaw = there.from_wire(tone.tobytes())
    restored = np.frombuffer(back.to_wire(ulaw), "<i2").astype(np.float64)
    spectrum = np.abs(np.fft.rfft(restored))
    peak_hz = np.argmax(spectrum) * 24000 / len(restored)
    assert abs(peak_hz - 440) < 5
