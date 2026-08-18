"""Tests for the PIN gate on a real `VoiceSession` (spec §3.3, §5).

Two ways in: the caller says the PIN (the model calls `submit_pin`) or keys it in on the
phone. The keypad path is the sensitive one — those digits must never reach the model,
the transcript or the log — so most of these tests end by checking what was *not* said.
"""

import asyncio
import logging

import pytest
from fakes import TIMEOUT, FakeProvider, FakeTransport, eventually
from test_session import make_settings, running

from jarvis.events import EventBus, SessionEnded
from jarvis.session import (
    OPENING_MESSAGE,
    PIN_ACCEPTED_MESSAGE,
    PIN_LOCKOUT_MESSAGE,
    PIN_MAX_ATTEMPTS,
    PIN_REJECTED_MESSAGE,
    VoiceSession,
)
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Dtmf

PIN = "4242"
WRONG = "1111"


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
    return FakeTransport(channel="phone", caller="+491555555555", audio_format="audio/pcmu")


@pytest.fixture
def make_session(tmp_path, bus):
    def build(transport, prov, *, pin: str | None = PIN, authorized=False, **overrides):
        settings = make_settings(tmp_path, pin=pin, **overrides)
        return VoiceSession(
            transport, prov, settings, ToolRegistry(), bus, authorized=authorized
        )

    return build


def texts(provider: FakeProvider) -> list[str]:
    return [text for text, _respond, _instructions in provider.injected]


def assert_nothing_spoken_had_digits(provider: FakeProvider) -> None:
    """Not one digit may reach the model: that is the whole point of the keypad path."""
    spoken = " ".join(texts(provider))
    assert not any(char.isdigit() for char in spoken), spoken


async def press(transport: FakeTransport, digits: str) -> None:
    for digit in digits:
        transport.feed(Dtmf(digit))
    await asyncio.sleep(0)  # let the transport pump drain the queue


# --- the spoken PIN --------------------------------------------------------


async def test_a_spoken_pin_authorizes_the_session(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        assert await session.submit_pin(PIN) == {"status": "authorized"}
        assert session.authorized is True

    assert texts(provider) == [OPENING_MESSAGE]


async def test_surrounding_whitespace_is_forgiven(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        assert await session.submit_pin(f"  {PIN} ") == {"status": "authorized"}


async def test_a_wrong_pin_counts_the_attempts_down(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        assert await session.submit_pin(WRONG) == {"status": "invalid", "attempts_left": 2}
        assert await session.submit_pin("2222") == {"status": "invalid", "attempts_left": 1}
        assert session.authorized is False


async def test_the_third_failure_ends_the_call(make_session, phone, provider, ended):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS - 1):
        await session.submit_pin(WRONG)
    assert await session.submit_pin(WRONG) == {"status": "locked"}
    await asyncio.wait_for(task, TIMEOUT)

    assert PIN_LOCKOUT_MESSAGE in texts(provider)
    assert [event.reason for event in ended] == ["pin_lockout"]
    assert session.authorized is False


async def test_a_locked_session_will_not_accept_the_right_pin(make_session, phone, provider):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await session.submit_pin(WRONG)
    await asyncio.wait_for(task, TIMEOUT)

    assert await session.submit_pin(PIN) == {"status": "locked"}
    assert session.authorized is False


async def test_without_a_configured_pin_there_is_nothing_to_check(make_session, phone, provider):
    session = make_session(phone, provider, pin=None)

    async with running(session):
        assert await session.submit_pin(PIN) == {"status": "not_configured"}
        assert session.authorized is False


async def test_an_authorized_session_stays_authorized(make_session, phone, provider):
    session = make_session(phone, provider, authorized=True)

    async with running(session):
        assert await session.submit_pin(WRONG) == {"status": "authorized"}
        assert session.authorized is True


# --- the keypad ------------------------------------------------------------


async def test_a_full_keypad_entry_authorizes_without_a_hash(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        await press(phone, PIN)
        await eventually(lambda: session.authorized)
        await eventually(lambda: PIN_ACCEPTED_MESSAGE in texts(provider))

    assert_nothing_spoken_had_digits(provider)
    assert PIN not in session.transcript_path.read_text()


async def test_a_hash_submits_a_short_entry(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        await press(phone, "12#")
        await eventually(lambda: PIN_REJECTED_MESSAGE in texts(provider))
        assert session.authorized is False

    assert_nothing_spoken_had_digits(provider)


async def test_a_hash_after_a_complete_entry_is_ignored(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        await press(phone, f"{PIN}#")
        await eventually(lambda: session.authorized)
        await asyncio.sleep(0.05)

    assert texts(provider) == [OPENING_MESSAGE, PIN_ACCEPTED_MESSAGE]


async def test_a_stale_digit_is_dropped_after_the_inter_digit_gap(
    make_session, phone, provider, monkeypatch
):
    monkeypatch.setattr("jarvis.session.DTMF_RESET_SECONDS", 0.02)
    session = make_session(phone, provider)

    async with running(session):
        await press(phone, "9")  # a mis-hit, then a pause, then the real PIN
        await asyncio.sleep(0.05)
        await press(phone, PIN)
        await eventually(lambda: session.authorized)


async def test_three_wrong_keypad_entries_end_the_call(make_session, phone, provider, ended):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await press(phone, WRONG)
    await asyncio.wait_for(task, TIMEOUT)

    assert PIN_LOCKOUT_MESSAGE in texts(provider)
    assert [event.reason for event in ended] == ["pin_lockout"]
    assert_nothing_spoken_had_digits(provider)


async def test_keypad_digits_are_ignored_when_no_pin_is_configured(
    make_session, phone, provider
):
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, "4242#")
        await asyncio.sleep(0.05)

    assert texts(provider) == [OPENING_MESSAGE]
    assert session.authorized is False


async def test_keypad_digits_are_ignored_once_authorized(make_session, phone, provider):
    session = make_session(phone, provider, authorized=True)

    async with running(session):
        await press(phone, "9999#")
        await asyncio.sleep(0.05)

    assert texts(provider) == [OPENING_MESSAGE]


async def test_keypad_digits_never_reach_the_log(make_session, phone, provider, caplog):
    session = make_session(phone, provider)

    with caplog.at_level(logging.DEBUG, logger="jarvis.session"):
        async with running(session):
            await press(phone, PIN)
            await eventually(lambda: session.authorized)

    assert PIN not in caplog.text
