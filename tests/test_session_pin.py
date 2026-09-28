"""Tests for the PIN gate on a real `VoiceSession` (spec §3.3, §5).

Two ways in: the caller says the PIN (the model calls `submit_pin`) or keys it in on the
phone. The keypad path is the sensitive one — those digits must never reach the model,
the transcript or the log — so most of these tests end by checking what was *not* said.
"""

import asyncio
import logging
import stat
from pathlib import Path

import pytest
from fakes import TIMEOUT, FakeProvider, FakeTransport, eventually
from test_session import make_settings, running

from jarvis.config import pin_file, secure_dir
from jarvis.events import EventBus, PinLockedOut, SessionEnded
from jarvis.pin_guard import PinGuard
from jarvis.realtime.base import (
    FunctionCall,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
)
from jarvis.session import (
    ENROL_CONFIRM_MESSAGE,
    ENROL_DONE_MESSAGE,
    ENROL_FAILED_MESSAGE,
    ENROL_GAVE_UP_MESSAGE,
    ENROL_LENGTH_MESSAGE,
    ENROL_MAX_ATTEMPTS,
    ENROL_MISMATCH_MESSAGE,
    OPENING_MESSAGE,
    PIN_ACCEPTED_MESSAGE,
    PIN_LOCKOUT_MESSAGE,
    PIN_MAX_ATTEMPTS,
    PIN_PAUSED_MESSAGE,
    PIN_REJECTED_MESSAGE,
    VoiceSession,
)
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Dtmf
from jarvis.trust import TrustLevel

PIN = "424242"
WRONG = "111111"


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
    return FakeTransport(channel="phone", caller="+15555555555", audio_format="audio/pcmu")


@pytest.fixture
def tools():
    """A registry with the one tool that matters here.

    It delegates exactly as `jarvis.tools.builtin`'s `submit_pin` does (which
    `tests/tools/test_builtin.py` pins down), so the spoken PIN path can be driven the way
    the model really drives it: a function call, answered with a tool result.
    """
    registry = ToolRegistry()

    async def submit_pin(ctx, arguments: dict) -> dict:
        return await ctx.session.submit_pin(arguments["pin"])

    registry.register(
        "submit_pin", "Check a PIN.", {"type": "object", "properties": {}}, submit_pin
    )
    return registry


@pytest.fixture
def make_session(tmp_path, bus, tools):
    def build(
        transport, prov, *, pin: str | None = PIN, authorized=False, pin_guard=None, **overrides
    ):
        settings = make_settings(tmp_path, pin=pin, **overrides)
        return VoiceSession(
            transport, prov, settings, tools, bus, authorized=authorized, pin_guard=pin_guard
        )

    return build


async def say_pin(provider: FakeProvider, pin: str, call_id: str) -> None:
    """Fail (or pass) one PIN the way the model does: a call into the submit_pin tool."""
    before = len(provider.tool_results)
    provider.feed(FunctionCall(call_id=call_id, name="submit_pin", arguments={"pin": pin}))
    await eventually(lambda: len(provider.tool_results) > before)


async def speak_a_goodbye(provider: FakeProvider, session: VoiceSession) -> None:
    """Play out the response the lockout asked for, start to finish."""
    provider.feed(ResponseStarted(response_id="resp_goodbye"))
    await eventually(lambda: session.response_active)
    provider.feed(ResponseDone(response_id="resp_goodbye", status="completed"))


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
        assert await session.submit_pin("222222") == {"status": "invalid", "attempts_left": 1}
        assert session.authorized is False


async def test_the_third_spoken_failure_hangs_up_only_after_the_goodbye(
    make_session, phone, provider, ended
):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for attempt in range(PIN_MAX_ATTEMPTS):
        await say_pin(provider, WRONG, f"call_{attempt}")

    assert provider.tool_results[-1] == (f"call_{PIN_MAX_ATTEMPTS - 1}", {"status": "locked"})
    assert PIN_LOCKOUT_MESSAGE in texts(provider)
    # The goodbye has been asked for but not spoken: the call must still be up.
    assert session.is_live is True
    assert phone.hung_up is False

    await speak_a_goodbye(provider, session)
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended] == ["pin_lockout"]
    assert phone.hung_up is True
    assert session.authorized is False


async def test_the_lockout_ends_the_call_even_if_the_goodbye_never_comes(
    make_session, phone, provider, ended, monkeypatch
):
    monkeypatch.setattr("jarvis.session.END_GRACE_SECONDS", 0.05)
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for attempt in range(PIN_MAX_ATTEMPTS):
        await say_pin(provider, WRONG, f"call_{attempt}")
    await asyncio.wait_for(task, TIMEOUT)  # not one response event ever arrives

    assert PIN_LOCKOUT_MESSAGE in texts(provider)
    assert [event.reason for event in ended] == ["pin_lockout"]
    assert phone.hung_up is True


async def test_a_lockout_that_cannot_even_ask_for_a_goodbye_ends_at_once(
    make_session, phone, provider, ended
):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS - 1):
        await session.submit_pin(WRONG)
    provider.send_error = RuntimeError("socket gone")
    assert await session.submit_pin(WRONG) == {"status": "locked"}
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended] == ["pin_lockout"]


async def test_speaking_after_the_lockout_does_not_save_the_call(
    make_session, phone, provider, ended
):
    """Unlike the silence goodbye, a lockout is not called off by talking over it."""
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await session.submit_pin(WRONG)
    await eventually(lambda: PIN_LOCKOUT_MESSAGE in texts(provider))

    provider.feed(SpeechStarted(item_id="item_1", audio_start_ms=0))
    await asyncio.sleep(0.01)
    await speak_a_goodbye(provider, session)
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended] == ["pin_lockout"]


async def test_a_locked_session_will_not_accept_the_right_pin(make_session, phone, provider):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await session.submit_pin(WRONG)
    await speak_a_goodbye(provider, session)
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


async def test_a_blank_pin_is_never_accepted(make_session, phone, provider):
    """A blank `JARVIS_PIN` is not a PIN, and a blank candidate is not an answer."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        assert await session.submit_pin("") == {"status": "not_configured"}
        assert session.authorized is False


async def test_a_blank_candidate_burns_an_attempt(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        assert await session.submit_pin("") == {"status": "invalid", "attempts_left": 2}
        assert await session.submit_pin("   ") == {"status": "invalid", "attempts_left": 1}
        assert session.authorized is False


async def test_an_empty_configured_pin_authorizes_nobody(tmp_path, bus, tools, phone, provider):
    """Defence in depth: even an empty PIN that slipped past `Settings` is no PIN."""
    settings = make_settings(tmp_path, pin=PIN).model_copy(update={"pin": ""})
    session = VoiceSession(phone, provider, settings, tools, bus, authorized=False)

    async with running(session):
        assert await session.submit_pin("") == {"status": "not_configured"}
        await press(phone, "1")  # and the keypad must not check anything either
        await asyncio.sleep(0.05)
        assert session.authorized is False

    assert texts(provider) == [OPENING_MESSAGE]


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


async def test_three_wrong_keypad_entries_hang_up_only_after_the_goodbye(
    make_session, phone, provider, ended
):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await press(phone, WRONG)
    await eventually(lambda: PIN_LOCKOUT_MESSAGE in texts(provider))

    assert session.is_live is True
    assert phone.hung_up is False

    await speak_a_goodbye(provider, session)
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended] == ["pin_lockout"]
    assert phone.hung_up is True
    assert_nothing_spoken_had_digits(provider)


async def test_a_keypad_lockout_ends_the_call_without_a_goodbye_too(
    make_session, phone, provider, ended, monkeypatch
):
    monkeypatch.setattr("jarvis.session.END_GRACE_SECONDS", 0.05)
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    for _ in range(PIN_MAX_ATTEMPTS):
        await press(phone, WRONG)
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended] == ["pin_lockout"]
    assert_nothing_spoken_had_digits(provider)


async def test_keypad_digits_are_ignored_when_a_pin_exists_that_cannot_be_read(
    make_session, phone, provider, tmp_path
):
    """No PIN to check and no enrolment either: the file is there, so the door is shut.

    Only `jarvis doctor` gets the owner out of this — delete the file, or set `JARVIS_PIN`
    — and in the meantime the keypad does nothing at all.
    """
    session = make_session(phone, provider, pin=None)
    secure_dir(session._settings.data_dir)
    session._settings.config_dir.mkdir(parents=True, exist_ok=True)
    pin_file(session._settings.config_dir).write_text("not-a-pin\n", encoding="utf-8")

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


# --- across calls ----------------------------------------------------------

GUARD_LIMIT = 4
COOLDOWN = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guard(tmp_path, clock):
    return PinGuard(
        tmp_path / "pin-failures.json",
        limit=GUARD_LIMIT,
        window_seconds=24 * 3600,
        lockout_seconds=COOLDOWN,
        clock=clock,
    )


@pytest.fixture
def lockouts(bus):
    events: list[PinLockedOut] = []
    bus.subscribe(PinLockedOut, events.append)
    return events


def another_phone() -> FakeTransport:
    return FakeTransport(channel="phone", caller="+15555555555", audio_format="audio/pcmu")


async def test_wrong_pins_are_counted_across_calls(make_session, phone, provider, guard, ended):
    """The per-call limit ends a call; a new call used to start the count from nothing."""
    first = make_session(phone, provider, pin_guard=guard)
    async with running(first):
        for _ in range(PIN_MAX_ATTEMPTS):
            await first.submit_pin(WRONG)

    second_provider = FakeProvider()
    second = make_session(another_phone(), second_provider, pin_guard=guard)
    task = asyncio.create_task(second.run())
    await eventually(lambda: second.is_live)

    assert await second.submit_pin(WRONG) == {"status": "locked"}  # the fourth, on any call
    assert PIN_PAUSED_MESSAGE in texts(second_provider)
    assert PIN_LOCKOUT_MESSAGE not in texts(second_provider)
    await speak_a_goodbye(second_provider, second)
    await asyncio.wait_for(task, TIMEOUT)
    assert ended[-1].reason == "pin_lockout"


async def test_while_pin_entry_is_locked_the_right_pin_is_refused_too(
    make_session, phone, provider, guard, ended
):
    for _ in range(GUARD_LIMIT):
        guard.record_failure()
    session = make_session(phone, provider, pin_guard=guard)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    assert await session.submit_pin(PIN) == {"status": "locked"}
    assert session.authorized is False
    assert texts(provider).count(PIN_PAUSED_MESSAGE) == 1
    assert session.is_live is True  # still saying so

    await speak_a_goodbye(provider, session)
    await asyncio.wait_for(task, TIMEOUT)
    assert [event.reason for event in ended] == ["pin_lockout"]
    # Refused without being compared, so it was not counted either way.
    assert guard.record_failure().failures == GUARD_LIMIT + 1


async def test_while_pin_entry_is_locked_a_keyed_pin_is_refused_once(
    make_session, phone, provider, guard
):
    for _ in range(GUARD_LIMIT):
        guard.record_failure()
    session = make_session(phone, provider, pin_guard=guard)

    async with running(session):
        await press(phone, PIN)
        await eventually(lambda: PIN_PAUSED_MESSAGE in texts(provider))
        await press(phone, PIN)  # the call is already ending: nothing more is said
        await asyncio.sleep(0.05)

    assert session.authorized is False
    assert texts(provider).count(PIN_PAUSED_MESSAGE) == 1
    assert_nothing_spoken_had_digits(provider)


async def test_a_right_pin_outside_a_lock_works_and_forgives_nothing(
    make_session, phone, provider, guard
):
    for _ in range(GUARD_LIMIT - 1):
        guard.record_failure()
    session = make_session(phone, provider, pin_guard=guard)

    async with running(session):
        assert await session.submit_pin(PIN) == {"status": "authorized"}

    assert guard.record_failure() is not None  # the three before it still count


async def test_the_lockout_the_owner_has_not_heard_about_is_published_once(
    make_session, phone, provider, guard, clock, lockouts
):
    for _ in range(GUARD_LIMIT - 1):
        guard.record_failure()
    first = make_session(phone, provider, pin_guard=guard)
    async with running(first):
        await first.submit_pin(WRONG)

    assert len(lockouts) == 1
    event = lockouts[0]
    assert (event.session_id, event.caller) == (first.session_id, phone.caller)
    assert (event.until, event.failures) == (clock.now + COOLDOWN, GUARD_LIMIT)

    clock.now += COOLDOWN  # the lock lifts, and the next wrong PIN locks it again
    second_provider = FakeProvider()
    second = make_session(another_phone(), second_provider, pin_guard=guard)
    async with running(second):
        assert await second.submit_pin(WRONG) == {"status": "locked"}

    assert len(lockouts) == 1  # already told
    assert PIN_PAUSED_MESSAGE in texts(second_provider)


# --- enrolling the first PIN (the one-way door) -----------------------------
#
# A new owner has to set `JARVIS_PIN` at the keyboard before the phone is any use, which
# is the one setup step the phone cannot do for them. So the first call may key one in —
# open while no PIN exists, sealed for ever the moment one does. The digits are keyed,
# never spoken, so they take the same path as an ordinary keyed PIN: never to the model,
# never to the transcript, never to a log line.

NEW_PIN = "135790"


def enrolled(session: VoiceSession) -> Path:
    return pin_file(session._settings.config_dir)


async def test_the_first_call_may_key_a_pin_in(make_session, phone, provider, tmp_path):
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_CONFIRM_MESSAGE in texts(provider))
        assert session.authorized is False  # one entry proves nothing

        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_DONE_MESSAGE in texts(provider))

    assert session.authorized is True
    assert enrolled(session).read_text(encoding="utf-8").strip() == NEW_PIN
    assert stat.S_IMODE(enrolled(session).stat().st_mode) == 0o600
    assert session._settings.pin == NEW_PIN  # live in this process, without a restart
    assert_nothing_spoken_had_digits(provider)
    assert NEW_PIN not in session.transcript_path.read_text()


async def test_the_enrolment_is_said_once_and_only_at_the_end(make_session, phone, provider):
    """One action is one sentence: the confirmation asked for is the second entry, not a
    second announcement of the same fact."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_CONFIRM_MESSAGE in texts(provider))
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_DONE_MESSAGE in texts(provider))
        await asyncio.sleep(0.05)

    assert texts(provider).count(ENROL_DONE_MESSAGE) == 1
    assert texts(provider) == [OPENING_MESSAGE, ENROL_CONFIRM_MESSAGE, ENROL_DONE_MESSAGE]


async def test_an_eight_digit_entry_submits_itself(make_session, phone, provider):
    """No configured length to measure against, so the entry runs to the longest a PIN is."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, "13579024")
        await eventually(lambda: ENROL_CONFIRM_MESSAGE in texts(provider))
        await press(phone, "13579024")
        await eventually(lambda: ENROL_DONE_MESSAGE in texts(provider))

    assert enrolled(session).read_text(encoding="utf-8").strip() == "13579024"


async def test_the_two_entries_have_to_match(make_session, phone, provider):
    """A mis-keyed PIN nobody can change afterwards is the worst outcome here."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_CONFIRM_MESSAGE in texts(provider))
        await press(phone, "975310#")
        await eventually(lambda: ENROL_MISMATCH_MESSAGE in texts(provider))

    assert session.authorized is False
    assert not enrolled(session).exists()
    assert_nothing_spoken_had_digits(provider)


async def test_an_entry_that_is_not_a_pin_asks_again(make_session, phone, provider):
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, "1234#")
        await eventually(lambda: ENROL_LENGTH_MESSAGE in texts(provider))
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_CONFIRM_MESSAGE in texts(provider))

    assert not enrolled(session).exists()


async def test_giving_up_leaves_the_call_at_none_and_the_door_open(
    make_session, phone, provider
):
    """A cap, not a lockout: nothing is set, so there is nothing to guess at."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        for _ in range(ENROL_MAX_ATTEMPTS):
            await press(phone, "1234#")
        await eventually(lambda: ENROL_GAVE_UP_MESSAGE in texts(provider))
        await press(phone, f"{NEW_PIN}#")  # and the keypad is done for this call
        await asyncio.sleep(0.05)

    assert session.trust is TrustLevel.NONE
    assert not enrolled(session).exists()
    assert texts(provider).count(ENROL_LENGTH_MESSAGE) == ENROL_MAX_ATTEMPTS - 1
    assert session._settings.pin_enrolment_open is True  # the next call may still set one


async def test_an_enrolled_pin_is_never_re_enrolled(make_session, phone, provider, tmp_path):
    """The door closed on the first call; the keypad is back to checking, not setting."""
    session = make_session(phone, provider, pin=None)
    assert session._settings.enrol_pin(NEW_PIN) is True

    async with running(session):
        await press(phone, "975310#")
        await eventually(lambda: PIN_REJECTED_MESSAGE in texts(provider))
        assert session.authorized is False

        await press(phone, NEW_PIN)
        await eventually(lambda: session.authorized)

    assert enrolled(session).read_text(encoding="utf-8").strip() == NEW_PIN
    assert ENROL_CONFIRM_MESSAGE not in texts(provider)


async def test_a_pin_from_the_environment_leaves_no_door_to_open(make_session, phone, provider):
    session = make_session(phone, provider)  # JARVIS_PIN is set

    async with running(session):
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: PIN_REJECTED_MESSAGE in texts(provider))

    assert ENROL_CONFIRM_MESSAGE not in texts(provider)
    assert not enrolled(session).exists()


async def test_a_spoken_pin_cannot_enrol_one(make_session, phone, provider):
    """The keypad sets it, never the transcription: a mishearing is unfixable here."""
    session = make_session(phone, provider, pin=None)

    async with running(session):
        assert await session.submit_pin(NEW_PIN) == {"status": "not_configured"}

    assert session.authorized is False
    assert not enrolled(session).exists()


async def test_the_enrolled_digits_never_reach_a_log_line(make_session, phone, provider, caplog):
    session = make_session(phone, provider, pin=None)

    with caplog.at_level(logging.DEBUG, logger="jarvis.session"):
        async with running(session):
            await press(phone, f"{NEW_PIN}#")
            await press(phone, f"{NEW_PIN}#")
            await eventually(lambda: session.authorized)

    assert NEW_PIN not in caplog.text


async def test_a_write_that_fails_changes_nothing_and_says_so(
    make_session, phone, provider, monkeypatch
):
    monkeypatch.setattr(
        "jarvis.config.settings.write_enrolled_pin", lambda *_args, **_kwargs: False
    )
    session = make_session(phone, provider, pin=None)

    async with running(session):
        await press(phone, f"{NEW_PIN}#")
        await press(phone, f"{NEW_PIN}#")
        await eventually(lambda: ENROL_FAILED_MESSAGE in texts(provider))

    assert session.authorized is False
    assert session._settings.pin is None
