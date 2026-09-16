"""What a phone caller can reach before the PIN (spec §3.3, §5; SECURITY.md).

Caller id is spoofable, so a caller on an allowed number has proved nothing. The ruling
these tests hold: on the phone, before the PIN, nothing private is read out, nothing is
announced into the session, and nothing the caller says or does outlives the call. The
local channel is authorized by construction and is unchanged.
"""

import pytest
from fakes import FakeProvider, FakeTransport
from test_session import make_settings, running

from jarvis.events import EventBus, SessionEnded
from jarvis.session import VoiceSession
from jarvis.tools import ToolRegistry

PIN = "123456"
CALLER = "+15550001111"


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def ended(bus):
    events: list[SessionEnded] = []
    bus.subscribe(SessionEnded, events.append)
    return events


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def phone():
    return FakeTransport(channel="phone", caller=CALLER, audio_format="audio/pcmu")


@pytest.fixture
def local():
    return FakeTransport(channel="local", caller=None, audio_format="audio/pcm")


@pytest.fixture
def make_session(tmp_path, bus):
    def build(transport, prov, *, authorized=False, **kwargs) -> VoiceSession:
        settings = make_settings(tmp_path, pin=PIN)
        return VoiceSession(
            transport, prov, settings, ToolRegistry(), bus, authorized=authorized, **kwargs
        )

    return build


# --- what outlives the call ------------------------------------------------


async def test_a_call_that_never_gave_the_pin_ends_unauthorized(
    make_session, phone, provider, ended
):
    """The memory writer reads this flag: without it, nothing the caller said is kept."""
    session = make_session(phone, provider)

    async with running(session):
        pass

    assert [event.authorized for event in ended] == [False]


async def test_a_call_that_gave_the_pin_ends_authorized(make_session, phone, provider, ended):
    session = make_session(phone, provider)

    async with running(session):
        assert (await session.submit_pin(PIN))["status"] == "authorized"

    assert [event.authorized for event in ended] == [True]


async def test_a_local_session_ends_authorized(make_session, local, provider, ended):
    session = make_session(local, provider, authorized=True)

    async with running(session):
        pass

    assert [event.authorized for event in ended] == [True]
