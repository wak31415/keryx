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
from jarvis.session import (
    PIN_ENTRY_CANCELLED_MESSAGE,
    PIN_ENTRY_MESSAGE,
    VoiceSession,
)
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Dtmf
from jarvis.trust import TrustLevel

PIN = "424242"


@dataclass
class SpyKeypad:
    digits: list[tuple[str, str]] = field(default_factory=list)
    message: str | None = "[system] They pressed a key."
    error: Exception | None = None
    #: Whether a menu has been read out and a key is expected (`ApprovalBroker.armed`).
    waiting: bool = False

    def digit(self, session_id: str, key: str) -> str | None:
        if self.error is not None:
            raise self.error
        self.digits.append((session_id, key))
        return self.message

    def armed(self, session_id: str) -> bool:
        if self.error is not None:
            raise self.error
        return self.waiting


@pytest.fixture
def keypad():
    return SpyKeypad()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def phone():
    return FakeTransport(channel="phone", caller="+15555555555", audio_format="audio/pcmu")


def build(phone, provider, keypad, *, authorized, tmp_path, possession=False):
    return VoiceSession(
        phone,
        provider,
        make_settings(tmp_path, pin=PIN),
        ToolRegistry(),
        EventBus(),
        authorized=authorized,
        possession=possession,
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
            return any("They pressed" in text for text, _, _ in provider.injected)

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
    keypad.message = "[system] They pressed a key: request 1 is approved."
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


# --- a call Jarvis placed ---------------------------------------------------


async def test_a_call_jarvis_placed_can_still_key_the_pin_in(phone, provider, keypad, tmp_path):
    """Possession may answer an approval, and the owner may still want the rest of it.

    So with nothing armed, a digit on such a call is a PIN attempt exactly as before — a
    keypad that swallowed every key would make the PIN unenterable on a call-back.
    """
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)
    assert keypad.digits == []


async def test_an_armed_menu_takes_the_digit_before_the_pin(phone, provider, keypad, tmp_path):
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("1"))
        await eventually(lambda: keypad.digits)
    assert keypad.digits == [(session.session_id, "1")]
    assert session.authorized is False


async def test_a_keypad_that_cannot_be_asked_is_never_armed(phone, provider, tmp_path):
    """A listener with no `armed` at all — the digit goes where it always did."""

    class OldKeypad:
        def __init__(self):
            self.digits = []

        def digit(self, session_id: str, key: str) -> str | None:
            self.digits.append(key)
            return None

    keypad = OldKeypad()
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)
    assert keypad.digits == []


async def test_a_keypad_that_cannot_say_whether_it_is_armed_gets_nothing(
    phone, provider, keypad, tmp_path
):
    """A listener that raises is treated as not waiting: the digit goes where it always did."""
    keypad.error = RuntimeError("the broker fell over")
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)
    assert keypad.digits == []


# --- the keypress that rules out an answering machine -----------------------


async def test_no_key_pressed_is_the_starting_point(phone, provider, keypad, tmp_path):
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)

    assert session.keypressed is False


async def test_any_key_at_all_counts(phone, provider, keypad, tmp_path):
    """Including `*`, which is not part of a PIN: the point is that a machine cannot."""
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        await eventually(lambda: session.keypressed)

    assert session.keypressed is True


# --- the way back to the PIN while a menu is armed --------------------------
#
# The dead end this closes: on an escalation call the owner may want FULL — to dispatch
# work, or to ask for something else while they have Jarvis on the line — and every digit
# they type goes to the armed menu, which reads each one back as an unrecognised key. `*`
# is never part of a PIN and never an answer to a menu, so it is free to mean "the keypad
# is for the PIN now", and free to mean it again in reverse.


async def test_star_while_a_menu_is_armed_hands_the_keypad_to_the_pin(
    phone, provider, keypad, tmp_path
):
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)

    assert keypad.digits == []  # not one digit of the PIN reached the menu
    assert session.trust is TrustLevel.FULL


async def test_star_again_hands_it_back_to_the_menu(phone, provider, keypad, tmp_path):
    """A mis-hit must not be a trap of its own, so the switch goes both ways."""
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        phone.feed(Dtmf("*"))
        phone.feed(Dtmf("1"))
        await eventually(lambda: keypad.digits)

    assert keypad.digits == [(session.session_id, "1")]
    assert session.authorized is False


async def test_a_wrong_pin_does_not_hand_the_keypad_back(phone, provider, keypad, tmp_path):
    """The model is told to ask them to try again, and trying again has to work."""
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        for digit in "999999":
            phone.feed(Dtmf(digit))
        await eventually(lambda: any("incorrect" in text for text, *_ in provider.injected))
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)

    assert keypad.digits == []


async def test_the_right_pin_hands_the_keypad_back_by_itself(phone, provider, keypad, tmp_path):
    """Past the PIN the call is FULL, where every digit is the keypad's again."""
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)
        phone.feed(Dtmf("1"))
        await eventually(lambda: keypad.digits)

    assert keypad.digits == [(session.session_id, "1")]


async def test_star_is_the_keypad_s_own_once_the_pin_is_in(phone, provider, keypad, tmp_path):
    """At FULL nothing is being typed *in*, so `*` is just another key for the listener."""
    session = build(phone, provider, keypad, authorized=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        await eventually(lambda: keypad.digits)

    assert keypad.digits == [(session.session_id, "*")]


async def test_star_with_no_menu_armed_changes_nothing(phone, provider, keypad, tmp_path):
    """There is nothing to switch away from, and `*` was never part of a PIN."""
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: session.authorized)

    assert keypad.digits == []


async def test_the_model_is_told_the_keypad_changed_hands_and_asked_for_nothing(
    phone, provider, keypad, tmp_path
):
    """They are typing. A sentence over the top of that is one nobody is listening to."""
    keypad.waiting = True
    session = build(phone, provider, keypad, authorized=False, possession=True, tmp_path=tmp_path)
    async with running(session):
        phone.feed(Dtmf("*"))
        await eventually(lambda: len(provider.injected) > 1)
        switched = provider.injected[-1]
        phone.feed(Dtmf("*"))
        await eventually(lambda: len(provider.injected) > 2)
        back = provider.injected[-1]

    assert switched[0] == PIN_ENTRY_MESSAGE and switched[1] is False
    assert back[0] == PIN_ENTRY_CANCELLED_MESSAGE and back[1] is False
