"""Tests for telling the owner that PIN entry has locked across calls.

Everything outbound is a double: the sessions, Slack and Twilio. The one rule these tests
exist for is where the alert must *not* go — into a call that has not given the PIN, which
is where the guessing came from.
"""

import asyncio
import logging

import pytest
from fakes import FakeVoiceSession, eventually

from keryx.events import EventBus, PinLockedOut
from keryx.logging_util import mask_number
from keryx.notify.pin_alert import PinLockoutAlerter, lockout_text

CALLER = "+15551234567"
OWNER = "+15550000001"
UNTIL = 1_800_003_600.0


class FakeSessions:
    def __init__(self, *sessions) -> None:
        self._sessions = list(sessions)

    def live(self) -> list:
        return self._sessions


class FakeTwilio:
    def __init__(self, *, can_text: bool) -> None:
        self.can_text = can_text
        self.sent: list[tuple[str, str]] = []

    async def send_sms(self, to: str, body: str) -> str:
        self.sent.append((to, body))
        return "SM1"


class FakeSlack:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self.error is not None:
            raise self.error
        self.sent.append(text)
        return True


def session(*, authorized: bool, channel: str = "phone") -> FakeVoiceSession:
    fake = FakeVoiceSession(channel=channel)
    fake.authorized = authorized
    return fake


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"owner_number_explicit": OWNER})


@pytest.fixture
def bus():
    return EventBus()


def event() -> PinLockedOut:
    return PinLockedOut(session_id="sess-9", caller=CALLER, until=UNTIL, failures=10)


def start(bus, settings, *, sessions=None, twilio=None, slack=None) -> PinLockoutAlerter:
    alerter = PinLockoutAlerter(
        bus, sessions or FakeSessions(), twilio or FakeTwilio(can_text=False), settings,
        slack=None if slack is None else (lambda: slack),
    )
    alerter.start()
    return alerter


# --- the words ------------------------------------------------------------------


def test_the_alert_says_what_happened_until_when_and_what_to_do(settings):
    text = lockout_text(event(), settings)

    assert "10 wrong PINs" in text
    assert "24 hours" in text
    assert mask_number(CALLER) in text
    assert CALLER not in text
    assert "KERYX_PIN" in text


# --- where it goes ----------------------------------------------------------------


async def test_it_is_announced_only_into_a_call_that_has_given_the_pin(bus, settings):
    owner, stranger = session(authorized=True), session(authorized=False)
    at_the_desk = session(authorized=True, channel="local")
    start(bus, settings, sessions=FakeSessions(stranger, owner, at_the_desk))

    await bus.publish(event())

    await eventually(lambda: owner.announced and at_the_desk.announced)
    assert owner.announced == [lockout_text(event(), settings)]
    assert stranger.announced == []


async def test_it_goes_to_slack_when_slack_is_set_up(bus, settings):
    slack = FakeSlack()
    start(bus, settings, slack=slack)

    await bus.publish(event())

    await eventually(lambda: slack.sent)
    assert slack.sent == [lockout_text(event(), settings)]


async def test_slack_is_asked_for_at_alert_time_so_turning_it_on_needs_no_restart(
    bus, settings
):
    slack, routes = FakeSlack(), [None]
    alerter = PinLockoutAlerter(
        bus, FakeSessions(), FakeTwilio(can_text=False), settings, slack=lambda: routes[-1]
    )

    await alerter.deliver(event())  # off: nowhere to post
    routes.append(slack)  # the plugin is turned on while the service runs
    await alerter.deliver(event())

    assert slack.sent == [lockout_text(event(), settings)]


async def test_it_is_texted_only_when_texting_is_on(bus, settings):
    off, on = FakeTwilio(can_text=False), FakeTwilio(can_text=True)
    start(bus, settings, twilio=off)
    start(bus, settings, twilio=on)

    await bus.publish(event())

    await eventually(lambda: on.sent)
    assert on.sent == [(OWNER, lockout_text(event(), settings))]
    assert off.sent == []


async def test_a_broken_slack_does_not_cost_the_announcement(bus, settings, caplog):
    owner = session(authorized=True)
    start(bus, settings, sessions=FakeSessions(owner), slack=FakeSlack(error=OSError("down")))

    with caplog.at_level(logging.ERROR, logger="keryx.notify.pin_alert"):
        await bus.publish(event())
        await eventually(lambda: owner.announced)
        await eventually(lambda: "Slack" in caplog.text)


async def test_an_alert_that_reached_nobody_says_so_in_the_log(bus, settings, caplog):
    alerter = start(bus, settings)

    with caplog.at_level(logging.WARNING, logger="keryx.notify.pin_alert"):
        await alerter.deliver(event())

    assert "reached nobody" in caplog.text
    assert CALLER not in caplog.text


async def test_publishing_does_not_wait_for_the_delivery(bus, settings):
    """It is published from inside the call being locked out, which is about to hang up."""
    release = asyncio.Event()

    class SlowSlack(FakeSlack):
        async def send(self, text: str) -> bool:
            await release.wait()
            return await super().send(text)

    slack = SlowSlack()
    start(bus, settings, slack=slack)

    await asyncio.wait_for(bus.publish(event()), 1.0)
    assert slack.sent == []
    release.set()
    await eventually(lambda: slack.sent)


async def test_stopping_takes_it_off_the_bus_and_drops_what_is_in_flight(bus, settings):
    release = asyncio.Event()

    class StuckSlack(FakeSlack):
        async def send(self, text: str) -> bool:
            await release.wait()
            return await super().send(text)

    slack = StuckSlack()
    alerter = start(bus, settings, slack=slack)
    await bus.publish(event())
    await asyncio.sleep(0)

    alerter.stop()
    alerter.stop()  # idempotent
    release.set()
    await bus.publish(event())
    await asyncio.sleep(0.05)

    assert slack.sent == []
