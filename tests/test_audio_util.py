"""Tests for jarvis.audio.util."""

import threading

import numpy as np

from jarvis.audio.util import (
    AudioGate,
    PlaybackBuffer,
    chunk_bytes,
    ms_for_bytes,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_pcm16,
)


def _sine_pcm16(freq: float, n: int, rate: int, amplitude: int = 20000) -> bytes:
    t = np.arange(n)
    samples = (np.sin(2 * np.pi * freq * t / rate) * amplitude).astype("<i2")
    return samples.tobytes()


# --- resample_pcm16 -------------------------------------------------------


def test_resample_pcm16_passthrough_when_rates_equal():
    data = _sine_pcm16(440, 2400, 24000)
    assert resample_pcm16(data, 24000, 24000) == data


def test_resample_pcm16_downsamples_to_expected_length_ratio():
    data = _sine_pcm16(440, 2400, 24000)  # 100 ms @ 24 kHz
    out = resample_pcm16(data, 24000, 16000)

    out_samples = len(out) // 2
    expected = 2400 * 16000 // 24000  # 1600
    assert abs(out_samples - expected) <= 1


def test_resample_pcm16_upsamples_to_expected_length_ratio():
    data = _sine_pcm16(440, 1600, 16000)  # 100 ms @ 16 kHz
    out = resample_pcm16(data, 16000, 24000)

    out_samples = len(out) // 2
    expected = 1600 * 24000 // 16000  # 2400
    assert abs(out_samples - expected) <= 1


def test_resample_pcm16_returns_int16_bytes():
    data = _sine_pcm16(440, 480, 24000)
    out = resample_pcm16(data, 24000, 16000)
    assert len(out) % 2 == 0


# --- mu-law ----------------------------------------------------------------


def test_mulaw_round_trip_error_bounded():
    data = _sine_pcm16(440, 8000, 8000, amplitude=20000)
    encoded = pcm16_to_mulaw(data)
    decoded = mulaw_to_pcm16(encoded)

    assert len(encoded) == len(data) // 2
    assert len(decoded) == len(data)

    original = np.frombuffer(data, dtype="<i2").astype(np.int64)
    round_tripped = np.frombuffer(decoded, dtype="<i2").astype(np.int64)
    max_abs_error = np.abs(original - round_tripped).max()

    assert max_abs_error < 0.03 * 32768


def test_mulaw_silence_round_trips_to_zero():
    data = np.zeros(10, dtype="<i2").tobytes()
    encoded = pcm16_to_mulaw(data)
    decoded = mulaw_to_pcm16(encoded)
    assert np.frombuffer(decoded, dtype="<i2").tolist() == [0] * 10


def test_pcm16_to_mulaw_output_length_is_half_input():
    data = _sine_pcm16(440, 160, 8000)  # 160 samples = 320 bytes of PCM16
    encoded = pcm16_to_mulaw(data)
    assert len(encoded) == len(data) // 2 == 160


# --- chunk_bytes -------------------------------------------------------


def test_chunk_bytes_splits_into_fixed_size_chunks_with_remainder():
    data = bytes(range(10))
    chunks = list(chunk_bytes(data, 3))
    assert chunks == [bytes([0, 1, 2]), bytes([3, 4, 5]), bytes([6, 7, 8]), bytes([9])]


def test_chunk_bytes_exact_multiple():
    data = bytes(range(9))
    chunks = list(chunk_bytes(data, 3))
    assert chunks == [bytes([0, 1, 2]), bytes([3, 4, 5]), bytes([6, 7, 8])]


def test_chunk_bytes_empty_input_yields_nothing():
    assert list(chunk_bytes(b"", 4)) == []


# --- ms_for_bytes ------------------------------------------------------


def test_ms_for_bytes_pcmu():
    assert ms_for_bytes(160, "audio/pcmu") == 20.0  # 20ms @ 8kHz mu-law, 1 byte/sample


def test_ms_for_bytes_pcm():
    assert ms_for_bytes(960, "audio/pcm") == 20.0  # 20ms @ 24kHz, 16-bit mono


# --- AudioGate -----------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_audio_gate_starts_listening():
    gate = AudioGate()
    assert gate.is_speaking is False
    assert gate.should_pass_mic() is True


def test_audio_gate_blocks_mic_while_speaking():
    gate = AudioGate()
    gate.on_playback_start()
    assert gate.is_speaking is True
    assert gate.should_pass_mic() is False


def test_audio_gate_blocks_mic_during_hangover_after_drain():
    clock = _FakeClock()
    gate = AudioGate(hangover_s=0.2, now=clock)
    gate.on_playback_start()
    gate.on_playback_drain()
    assert gate.is_speaking is False
    assert gate.should_pass_mic() is False  # still in hangover

    clock.advance(0.1)
    assert gate.should_pass_mic() is False  # still within 0.2s

    clock.advance(0.11)
    assert gate.should_pass_mic() is True  # hangover elapsed


def test_audio_gate_restart_during_hangover_blocks_mic_again():
    clock = _FakeClock()
    gate = AudioGate(hangover_s=0.2, now=clock)
    gate.on_playback_start()
    gate.on_playback_drain()
    clock.advance(0.05)

    gate.on_playback_start()
    assert gate.should_pass_mic() is False
    assert gate.is_speaking is True


# --- PlaybackBuffer ----------------------------------------------------


def test_playback_buffer_write_then_read_full():
    buf = PlaybackBuffer()
    buf.write(b"abcdef")
    assert buf.read(6) == b"abcdef"
    assert buf.drained is False


def test_playback_buffer_read_pads_with_zeros_on_underrun():
    buf = PlaybackBuffer()
    buf.write(b"ab")
    out = buf.read(6)
    assert out == b"ab\x00\x00\x00\x00"
    assert buf.drained is True


def test_playback_buffer_read_partial_across_multiple_writes():
    buf = PlaybackBuffer()
    buf.write(b"abc")
    buf.write(b"def")
    assert buf.read(4) == b"abcd"
    assert buf.drained is False
    assert buf.read(2) == b"ef"


def test_playback_buffer_clear_drops_pending_data():
    buf = PlaybackBuffer()
    buf.write(b"abcdef")
    buf.clear()
    assert buf.pending_bytes == 0
    assert buf.is_empty is True
    assert buf.read(3) == b"\x00\x00\x00"
    assert buf.drained is True


def test_playback_buffer_pending_bytes_and_is_empty():
    buf = PlaybackBuffer()
    assert buf.is_empty is True
    assert buf.pending_bytes == 0
    buf.write(b"abc")
    assert buf.is_empty is False
    assert buf.pending_bytes == 3


def test_playback_buffer_is_thread_safe_under_concurrent_write_read():
    buf = PlaybackBuffer()
    total_written = 0
    lock = threading.Lock()

    def writer():
        nonlocal total_written
        for _ in range(1000):
            buf.write(b"x")
            with lock:
                total_written += 1

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert buf.pending_bytes == total_written == 4000
