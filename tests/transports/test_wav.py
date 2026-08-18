"""Tests for the WAV file transport used by `jarvis loopback`."""

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest

from jarvis.transports.base import AudioIn, Hangup
from jarvis.transports.wav import WavTransport

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 960


def write_wav(path: Path, samples: np.ndarray, *, rate: int = SAMPLE_RATE, channels: int = 1):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())
    return path


async def drain(transport: WavTransport) -> list:
    return [event async for event in transport.events()]


# --- reading ---------------------------------------------------------------


async def test_the_wav_is_yielded_as_20ms_frames_then_silence_then_hangup(tmp_path):
    samples = np.full(3 * SAMPLE_RATE * FRAME_MS // 1000, 1000)  # 60 ms
    path = write_wav(tmp_path / "in.wav", samples)
    transport = WavTransport(path, out_path=tmp_path / "out.wav", tail_seconds=0.04)

    events = await asyncio.wait_for(drain(transport), 5)

    assert isinstance(events[-1], Hangup)
    audio = [event for event in events[:-1] if isinstance(event, AudioIn)]
    assert len(audio) == 5  # 3 frames of speech + 2 of tail silence
    assert all(len(event.data) == FRAME_BYTES for event in audio)
    assert audio[0].data == samples[:480].astype("<i2").tobytes()
    assert audio[-1].data == b"\x00" * FRAME_BYTES


async def test_a_stereo_48k_wav_is_downmixed_and_resampled(tmp_path):
    frames = 48000 * 40 // 1000  # 40 ms at 48 kHz
    stereo = np.empty(frames * 2, dtype="<i2")
    stereo[0::2] = 1000  # left channel: what we keep
    stereo[1::2] = -1000  # right channel: dropped
    path = write_wav(tmp_path / "in.wav", stereo, rate=48000, channels=2)
    transport = WavTransport(path, out_path=tmp_path / "out.wav", tail_seconds=0.0)

    events = await asyncio.wait_for(drain(transport), 5)

    audio = b"".join(event.data for event in events if isinstance(event, AudioIn))
    assert abs(len(audio) - 2 * FRAME_BYTES) <= FRAME_BYTES  # ~40 ms at 24 kHz
    samples = np.frombuffer(audio, dtype="<i2")
    assert samples.mean() > 500  # the left channel survived, not the average of both


def test_an_8_bit_wav_is_rejected(tmp_path):
    path = tmp_path / "in.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8000)
        wav.writeframes(b"\x80" * 100)

    with pytest.raises(ValueError, match="16-bit"):
        WavTransport(path, out_path=tmp_path / "out.wav")


# --- writing ---------------------------------------------------------------


async def test_hangup_writes_the_reply_as_a_24k_mono_wav(tmp_path):
    path = write_wav(tmp_path / "in.wav", np.zeros(480))
    out = tmp_path / "out.wav"
    transport = WavTransport(path, out_path=out, tail_seconds=0.0)
    reply = np.arange(240, dtype="<i2").tobytes()

    await transport.send_audio(reply)
    await transport.clear()  # playback is a file: clearing must not lose audio
    await transport.hangup()

    with wave.open(str(out), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == reply


async def test_metadata_matches_the_local_channel(tmp_path):
    path = write_wav(tmp_path / "in.wav", np.zeros(480))
    transport = WavTransport(path, out_path=tmp_path / "out.wav")

    assert (transport.channel, transport.caller, transport.audio_format) == (
        "local",
        None,
        "audio/pcm",
    )
