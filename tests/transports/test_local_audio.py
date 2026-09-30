"""Tests for the local audio device and transport (spec §3.2, §4 sounddevice notes).

No PortAudio is involved: every test drives `FakeStreamFactory`'s callbacks directly, so
the routing, gating, resampling and chime logic is exercised without a mic or speaker.
"""

import asyncio
import inspect
import sys

import numpy as np
import pytest
from fakes_audio import FakeStreamFactory

from keryx.audio.util import AudioGate, resample_pcm16
from keryx.transports.base import AudioIn, Hangup, Transport
from keryx.transports.local_audio import LocalAudioDevice, LocalTransport, build_chime

SAMPLE_RATE = 24000
FRAME_SAMPLES = 1920
FRAME_BYTES = FRAME_SAMPLES * 2
SILENT_FRAME = b"\x00" * FRAME_BYTES
HANGOVER_S = 0.2


class FakeClock:
    """Monotonic clock a test can advance without sleeping."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def tone(n: int = FRAME_SAMPLES, freq: float = 440.0, amplitude: int = 12000) -> bytes:
    t = np.arange(n)
    return (np.sin(2 * np.pi * freq * t / SAMPLE_RATE) * amplitude).astype("<i2").tobytes()


@pytest.fixture
def factory():
    return FakeStreamFactory()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def gate(clock):
    return AudioGate(HANGOVER_S, now=clock)


@pytest.fixture
async def device(factory, gate):
    dev = LocalAudioDevice(gate=gate, stream_factory=factory)
    dev.start()
    yield dev
    dev.stop()


# --- streams --------------------------------------------------------------


async def test_start_opens_and_starts_an_input_and_an_output_stream(device, factory):
    assert [stream.kind for stream in factory.streams] == ["input", "output"]
    assert factory.input.samplerate == SAMPLE_RATE
    assert factory.input.blocksize == FRAME_SAMPLES
    assert factory.output.samplerate == SAMPLE_RATE
    assert factory.output.blocksize == FRAME_SAMPLES
    assert all(stream.started for stream in factory.streams)


async def test_start_is_idempotent(device, factory):
    device.start()

    assert len(factory.streams) == 2


async def test_stop_stops_and_closes_the_streams_and_is_idempotent(device, factory):
    device.stop()
    device.stop()

    assert [stream.started for stream in factory.streams] == [False, False]
    assert [stream.closed for stream in factory.streams] == [True, True]


async def test_stop_drops_pending_playback(device, factory):
    device.play(tone())
    factory.pull_speaker()  # gate is now SPEAKING

    device.stop()

    assert not device.is_playing


# --- mic routing ----------------------------------------------------------


async def test_mic_frames_reach_the_mic_sink_while_the_gate_allows(device, factory):
    frames: list[bytes] = []
    device.set_mic_sink(frames.append)
    pcm = tone()

    factory.push_mic(pcm)
    assert frames == []  # handed to the loop, never delivered on the PortAudio thread

    await asyncio.sleep(0)

    assert frames == [pcm]


async def test_mic_frames_are_dropped_while_the_gate_is_speaking(device, factory, gate, clock):
    frames: list[bytes] = []
    device.set_mic_sink(frames.append)

    gate.on_playback_start()
    factory.push_mic(tone())
    await asyncio.sleep(0)
    assert frames == []

    gate.on_playback_drain()  # hangover window: still gated
    factory.push_mic(tone())
    await asyncio.sleep(0)
    assert frames == []

    clock.advance(HANGOVER_S + 0.01)
    factory.push_mic(tone())
    await asyncio.sleep(0)
    assert len(frames) == 1


async def test_mic_frames_are_dropped_when_no_sink_is_set(device, factory):
    factory.push_mic(tone())
    await asyncio.sleep(0)  # no sink, no crash

    frames: list[bytes] = []
    device.set_mic_sink(frames.append)
    device.set_mic_sink(None)
    factory.push_mic(tone())
    await asyncio.sleep(0)

    assert frames == []


async def test_wake_sink_always_gets_1280_sample_16k_frames(device, factory, gate):
    wake: list[bytes] = []
    device.set_wake_sink(wake.append)
    pcm = tone()

    gate.on_playback_start()  # gated for the mic, but the wake word keeps listening
    factory.push_mic(pcm)
    await asyncio.sleep(0)

    assert len(wake) == 1
    assert len(wake[0]) == 1280 * 2
    assert wake[0] == resample_pcm16(pcm, SAMPLE_RATE, 16000)


async def test_wake_and_mic_sinks_both_receive_the_same_frame(device, factory):
    wake: list[bytes] = []
    mic: list[bytes] = []
    device.set_wake_sink(wake.append)
    device.set_mic_sink(mic.append)
    pcm = tone()

    factory.push_mic(pcm)
    await asyncio.sleep(0)

    assert mic == [pcm]
    assert len(wake) == 1


# --- speaker / gate -------------------------------------------------------


async def test_played_audio_is_read_back_and_drives_the_gate(device, factory, gate, clock):
    pcm = tone()
    device.play(pcm)
    assert device.is_playing

    assert factory.pull_speaker() == pcm
    assert gate.is_speaking
    assert not gate.should_pass_mic()

    assert factory.pull_speaker() == SILENT_FRAME
    assert not gate.is_speaking  # the buffer drained
    assert not gate.should_pass_mic()  # ...but the hangover still gates the mic
    assert not device.is_playing

    clock.advance(HANGOVER_S + 0.01)
    assert gate.should_pass_mic()


async def test_a_partial_frame_starts_and_drains_the_gate_in_one_callback(device, factory, gate):
    device.play(tone(100))

    out = factory.pull_speaker()

    assert out[:200] == tone(100)
    assert out[200:] == b"\x00" * (FRAME_BYTES - 200)
    assert not gate.is_speaking
    assert not gate.should_pass_mic()  # hangover started


async def test_speaker_callbacks_on_an_idle_device_leave_the_gate_alone(device, factory, gate):
    assert factory.pull_speaker() == SILENT_FRAME
    assert factory.pull_speaker() == SILENT_FRAME

    assert not gate.is_speaking
    assert gate.should_pass_mic()  # no phantom drain restarting the hangover


async def test_clear_playback_drops_pending_audio_and_releases_the_gate(
    device, factory, gate, clock
):
    device.play(tone() * 3)
    factory.pull_speaker()
    assert gate.is_speaking

    device.clear_playback()

    assert not device.is_playing
    assert not gate.is_speaking
    assert factory.pull_speaker() == SILENT_FRAME
    clock.advance(HANGOVER_S + 0.01)
    assert gate.should_pass_mic()


async def test_is_playing_stays_true_while_the_gate_is_speaking(device, factory, gate):
    device.play(tone())
    factory.pull_speaker()

    assert device.is_playing  # buffer is empty but the speaker is still busy
    assert gate.is_speaking


# --- chime ----------------------------------------------------------------


def test_build_chime_is_a_faded_two_tone_burst():
    samples = np.frombuffer(build_chime(SAMPLE_RATE), dtype="<i2")

    assert len(samples) == SAMPLE_RATE * 250 // 1000
    assert np.abs(samples).max() > 3000
    assert samples[0] == 0
    assert samples[-1] == 0


async def test_chime_queues_audio_for_playback(device, factory):
    device.chime()

    assert device.is_playing
    assert factory.pull_speaker() != SILENT_FRAME


async def test_wait_until_idle_returns_true_once_playback_drains(device, factory):
    device.play(tone())

    async def drain() -> None:
        await asyncio.sleep(0.02)
        factory.pull_speaker()
        factory.pull_speaker()

    draining = asyncio.create_task(drain())
    assert await device.wait_until_idle(1.0) is True
    await draining


async def test_wait_until_idle_returns_false_on_timeout(device):
    device.play(tone())

    assert await device.wait_until_idle(0.05) is False


# --- LocalTransport -------------------------------------------------------


async def test_transport_metadata_matches_the_local_channel(device):
    transport = LocalTransport(device)

    assert transport.channel == "local"
    assert transport.caller is None
    assert transport.audio_format == "audio/pcm"


async def test_transport_yields_mic_audio_as_audio_in(device, factory):
    transport = LocalTransport(device)
    events = transport.events()
    pcm = tone()

    factory.push_mic(pcm)
    await asyncio.sleep(0)

    assert await asyncio.wait_for(anext(events), 1) == AudioIn(pcm)
    await events.aclose()


async def test_transport_send_audio_and_clear_drive_the_device(device, factory):
    transport = LocalTransport(device)

    await transport.send_audio(tone())
    assert device.is_playing
    assert factory.pull_speaker() == tone()

    await transport.send_audio(tone())
    await transport.clear()
    assert not device.is_playing


async def test_transport_hangup_emits_hangup_and_finishes_the_stream(device, factory):
    transport = LocalTransport(device)
    events = transport.events()
    device.play(tone())

    await transport.hangup("goodbye")

    assert await asyncio.wait_for(anext(events), 1) == Hangup("goodbye")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 1)
    assert not device.is_playing


async def test_transport_stops_consuming_mic_frames_after_hangup(device, factory):
    transport = LocalTransport(device)
    events = transport.events()

    await transport.hangup("goodbye")
    factory.push_mic(tone())
    await asyncio.sleep(0)

    assert await asyncio.wait_for(anext(events), 1) == Hangup("goodbye")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 1)


async def test_transport_hangup_is_idempotent(device):
    transport = LocalTransport(device)
    events = transport.events()

    await transport.hangup("first")
    await transport.hangup("second")

    assert await asyncio.wait_for(anext(events), 1) == Hangup("first")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 1)


async def test_transport_drain_waits_for_queued_playback(device, factory):
    """The session calls `drain` before `hangup`, so a goodbye is heard in full."""
    transport = LocalTransport(device)
    await transport.send_audio(tone())

    drain = asyncio.create_task(transport.drain(1.0))
    await asyncio.sleep(0)
    assert not drain.done()

    factory.pull_speaker()  # the speaker consumes the queued audio
    factory.pull_speaker()  # ... and the next callback finds the buffer empty

    assert await asyncio.wait_for(drain, 1) is True


async def test_transport_drain_gives_up_after_the_timeout(device):
    transport = LocalTransport(device)
    await transport.send_audio(tone())

    assert await asyncio.wait_for(transport.drain(0.05), 1) is False


async def test_transport_matches_the_transport_protocol(device):
    transport = LocalTransport(device)
    method_names = [name for name in vars(Transport) if not name.startswith("_")]

    assert set(method_names) == {"events", "send_audio", "clear", "hangup"}
    for name in method_names:
        expected = list(inspect.signature(getattr(Transport, name)).parameters)[1:]
        params = inspect.signature(getattr(transport, name)).parameters
        actual = list(params)
        assert actual[: len(expected)] == expected, f"{name} does not match the protocol"
        extra = actual[len(expected) :]
        assert all(params[p].default is not inspect.Parameter.empty for p in extra)


def test_sounddevice_is_never_imported_at_module_scope():
    assert "sounddevice" not in sys.modules


# --- callbacks never raise into PortAudio ---------------------------------


class BrokenGate:
    """Raises at what the audio callbacks ask it, the way a bug in that path would.

    `on_playback_drain` stays harmless: teardown calls it, and this is about the two
    PortAudio callbacks, not about `stop()`.
    """

    def should_pass_mic(self) -> bool:
        raise RuntimeError("boom")

    def on_playback_start(self) -> None:
        raise RuntimeError("boom")

    def on_playback_drain(self) -> None:
        pass

    @property
    def is_speaking(self) -> bool:
        raise RuntimeError("boom")


async def test_a_failing_input_callback_is_logged_not_raised(device, factory, monkeypatch, caplog):
    """An exception out of a PortAudio callback kills the stream, silently."""
    monkeypatch.setattr(device, "gate", BrokenGate())
    device.set_mic_sink(lambda pcm: None)

    factory.push_mic(tone())  # must not raise

    assert "input callback" in caplog.text


async def test_a_failing_output_callback_is_logged_not_raised(device, factory, monkeypatch, caplog):
    device.play(tone())
    monkeypatch.setattr(device, "gate", BrokenGate())

    factory.pull_speaker()  # must not raise

    assert "output callback" in caplog.text
