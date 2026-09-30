"""Tests for the local wake-word runner (idle <-> session state machine).

No mic, no model: a `FakeAudioDevice` delivers wake frames, a `FakeWakeListener` decides
what counts as a detection, and the sessions are fakes unless a test explicitly asks for
the real `VoiceSession` the default factory builds.
"""

import asyncio
import contextlib

import pytest
from fakes import FakeAudioDevice, FakeProvider, FakeWakeListener, eventually

from keryx.events import EventBus
from keryx.local_runner import LocalRunner
from keryx.session import SessionRegistry
from keryx.tools import ToolRegistry
from keryx.transports.local_audio import LocalTransport

TIMEOUT = 2.0


class FakeSession:
    """A `VoiceSession` stand-in whose `run()` the test controls."""

    def __init__(self, transport, provider, *, authorized: bool, block: bool = False) -> None:
        self.transport = transport
        self.provider = provider
        self.authorized = authorized
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.error: Exception | None = None
        self._block = block

    async def run(self) -> None:
        self.started.set()
        if self._block:
            await self.release.wait()
        self.finished = True
        if self.error is not None:
            raise self.error


class FakeSessionFactory:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.sessions: list[FakeSession] = []

    def __call__(self, transport, provider, *, authorized: bool) -> FakeSession:
        session = FakeSession(transport, provider, authorized=authorized, block=self.block)
        self.sessions.append(session)
        return session


@pytest.fixture
def device():
    return FakeAudioDevice()


@pytest.fixture
def listener():
    return FakeWakeListener()


@pytest.fixture
def sessions():
    return SessionRegistry()


@pytest.fixture
def make_runner(settings, device, listener, sessions):
    def build(**kwargs) -> LocalRunner:
        kwargs.setdefault("provider_factory", FakeProvider)
        kwargs.setdefault("registry", ToolRegistry())
        kwargs.setdefault("bus", EventBus())
        kwargs.setdefault("sessions", sessions)
        return LocalRunner(settings, device, listener, **kwargs)

    return build


@contextlib.asynccontextmanager
async def started(runner: LocalRunner, device: FakeAudioDevice):
    """Run the runner in a task and stop it again, however the test ends."""
    task = asyncio.create_task(runner.run())
    try:
        await eventually(lambda: device.wake_sink is not None)
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, TIMEOUT)


# --- detections ------------------------------------------------------------


async def test_a_detection_chimes_and_runs_a_session(make_runner, device, listener):
    factory = FakeSessionFactory()
    runner = make_runner(session_factory=factory)

    async with started(runner, device):
        device.wake()
        await eventually(lambda: factory.sessions != [])
        session = factory.sessions[0]
        await eventually(lambda: session.finished)

    assert device.started is False  # stopped on the way out
    assert device.chimes == 1
    assert device.idle_waits == [2.0]  # the chime finishes before the mic opens
    assert isinstance(session.transport, LocalTransport)
    assert isinstance(session.provider, FakeProvider)
    assert session.authorized is True


async def test_quiet_frames_do_not_start_a_session(make_runner, device, listener):
    factory = FakeSessionFactory()
    runner = make_runner(session_factory=factory)

    async with started(runner, device):
        device.wake(b"quiet")
        await eventually(lambda: listener.frames == [b"quiet"])
        await asyncio.sleep(0.02)

    assert factory.sessions == []
    assert device.chimes == 0


async def test_detections_during_a_session_are_ignored(make_runner, device, listener):
    factory = FakeSessionFactory(block=True)
    runner = make_runner(session_factory=factory)

    async with started(runner, device):
        device.wake()
        await eventually(lambda: factory.sessions != [])
        await asyncio.wait_for(factory.sessions[0].started.wait(), TIMEOUT)

        device.wake()
        device.wake()
        await asyncio.sleep(0.02)

        assert len(factory.sessions) == 1
        assert listener.frames == [b"wake"]  # mid-session frames never reach the detector

        factory.sessions[0].release.set()
        await eventually(lambda: factory.sessions[0].finished)


async def test_the_listener_is_reset_between_sessions(make_runner, device, listener):
    factory = FakeSessionFactory()
    runner = make_runner(session_factory=factory)

    async with started(runner, device):
        device.wake()
        await eventually(lambda: listener.resets == 1)
        device.wake()
        await eventually(lambda: len(factory.sessions) == 2)

    assert listener.resets == 2


async def test_a_failing_session_does_not_stop_the_runner(make_runner, device, caplog):
    factory = FakeSessionFactory(block=True)
    runner = make_runner(session_factory=factory)

    async with started(runner, device):
        device.wake()
        await eventually(lambda: factory.sessions != [])
        factory.sessions[0].error = RuntimeError("session blew up")
        factory.sessions[0].release.set()
        await eventually(lambda: "session blew up" in caplog.text)

        device.wake()
        await eventually(lambda: len(factory.sessions) == 2)


async def test_cancelling_the_runner_stops_the_device(make_runner, device):
    runner = make_runner(session_factory=FakeSessionFactory())

    async with started(runner, device):
        pass

    assert device.started is False
    assert device.stops == 1
    assert device.wake_sink is None


# --- the default factory ---------------------------------------------------


async def test_the_default_factory_builds_an_authorized_local_voice_session(
    make_runner, device, listener, sessions
):
    provider = FakeProvider()
    runner = make_runner(provider_factory=lambda: provider)

    async with started(runner, device):
        device.wake()
        await eventually(lambda: sessions.live() != [])
        session = sessions.live()[0]

        assert (session.channel, session.caller, session.authorized) == ("local", None, True)
        session.request_end("test over")
        await eventually(lambda: listener.resets == 1)

    assert provider.closed is True
