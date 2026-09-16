"""Where a keypad digit goes on a real `VoiceSession`.

The PIN owns the keypad until it has been given, and only then does anything else see a
digit. That ordering is the structural half of "PIN required in the call that approves":
there is no code path in which a digit reaches the approval broker before the PIN gate has
been satisfied, so it cannot be forgotten in a prompt or bypassed by a tool.
"""

from dataclasses import dataclass, field

import pytest
from fakes import FakeProvider, FakeTransport, eventually
from test_session import make_settings, running

from jarvis.events import EventBus
from jarvis.session import VoiceSession
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Dtmf

PIN = "424242"


@dataclass
class SpyKeypad:
    digits: list[tuple[str, str]] = field(default_factory=list)
    message: str | None = "[system] He pressed a key."
    error: Exception | None = None

    def digit(self, session_id: str, key: str) -> str | None:
        if self.error is not None:
            raise self.error
        self.digits.append((session_id, key))
        return self.message


@pytest.fixture
def keypad():
    return SpyKeypad()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def phone():
    return FakeTransport(channel="phone", caller="+15555555555", audio_format="audio/pcmu")


def build(phone, provider, keypad, *, authorized, tmp_path):
    return VoiceSession(
        phone,
        provider,
        make_settings(tmp_path, pin=PIN),
        ToolRegistry(),
        EventBus(),
        authorized=authorized,
        keypad=keypad,
    )


async def test_the_pin_takes_the_digits_first(phone, provider, keypad, tmp_path):
    """An unauthorized session's keypresses are a PIN attempt and nothing else."""
    session = build(phone, provider, keypad, authorized=False, tmp_path=tmp_path)
    async with running(session):
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)
    assert keypad.digits == []


async def test_a_digit_after_the_pin_reaches_the_keypad(phone, provider, keypad, tmp_path):
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("1"))
        await eventually(lambda: keypad.digits)
    assert keypad.digits == [(session.session_id, "1")]


async def test_what_the_keypad_decided_is_put_to_the_model(phone, provider, keypad, tmp_path):
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("1"))
        def pressed():
            return any("He pressed" in text for text, _, _ in provider.injected)

        await eventually(pressed)


async def test_a_digit_nobody_wanted_is_dropped(phone, provider, keypad, tmp_path):
    keypad.message = None
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("9"))
        await eventually(lambda: keypad.digits)
    assert not any("pressed" in text for text, _, _ in provider.injected)


async def test_the_digit_is_never_spoken_to_the_model(phone, provider, keypad, tmp_path):
    """The model is told what the key *decided*, never which key it was."""
    keypad.message = "[system] He pressed a key: request 1 is approved."
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("7"))
        await eventually(lambda: keypad.digits)
        await eventually(lambda: len(provider.injected) > 1)
    assert not any("7" in text for text, _, _ in provider.injected)


async def test_a_broken_keypad_cannot_end_the_call(phone, provider, keypad, tmp_path):
    """A listener that raises drops the digit — it must never become an answer either."""
    keypad.error = RuntimeError("the broker fell over")
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("1"))
        phone.feed(Dtmf("2"))
        await eventually(lambda: session.is_live and not provider.injected[1:])
        assert session.is_live


async def test_a_session_with_no_keypad_behaves_as_before(phone, provider, tmp_path):
    """Backwards compatibility: every existing caller passes no keypad at all."""
    session = VoiceSession(
        phone,
        provider,
        make_settings(tmp_path, pin=PIN),
        ToolRegistry(),
        EventBus(),
        authorized=True,
    )
    async with running(session):
        phone.feed(Dtmf("1"))
        await eventually(lambda: session.is_live)
        assert [text for text, _, _ in provider.injected[1:]] == []
