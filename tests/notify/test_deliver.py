"""Tests for the two shared delivery helpers.

`can_text` is asserted here and only here. Before these existed, `Notifier`,
`RestartCoordinator` and `restart/watchdog.py` each had their own copy of this gate, and
the one thing they all had to agree on — that `SMS_ENABLED=false` means no text, whatever
the Twilio credentials say — was three separate chances to disagree.
"""

from types import SimpleNamespace

import pytest
from fakes import FakeVoiceSession

from keryx.notify.deliver import Announced, announce_to_live_sessions, safe_send_sms
from keryx.trust import TrustLevel


class FakeSessions:
    def __init__(self, *sessions) -> None:
        self._sessions = list(sessions)

    def live(self) -> list:
        return self._sessions


class FakeTwilio:
    def __init__(self, *, can_text: bool = True, error: Exception | None = None) -> None:
        self.can_text = can_text
        self.error = error
        self.sent: list[tuple[str, str]] = []

    async def send_sms(self, to: str, body: str) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append((to, body))
        return "SM1"


# --- announcing -------------------------------------------------------------


async def test_nothing_live_is_not_an_error():
    assert await announce_to_live_sessions(FakeSessions(), "hello") == Announced(False, False)


async def test_a_local_session_hears_it_but_is_not_a_phone():
    local = FakeVoiceSession(channel="local")

    result = await announce_to_live_sessions(FakeSessions(local), "hello")

    assert (result.heard, result.delivered) == (True, False)
    assert local.announced == ["hello"]


async def test_a_phone_session_is_a_delivery():
    result = await announce_to_live_sessions(
        FakeSessions(FakeVoiceSession(channel="phone")), "hello"
    )

    assert (result.heard, result.delivered) == (True, True)


async def test_a_call_that_has_proved_nothing_hears_the_news_but_is_not_a_delivery():
    """It may be a spoofer. They heard it; the owner may not have, so the text still goes."""
    stranger = FakeVoiceSession(channel="phone", trust=TrustLevel.NONE)

    result = await announce_to_live_sessions(
        FakeSessions(stranger), "hello", needs=TrustLevel.NONE
    )

    assert (result.heard, result.delivered) == (True, False)
    assert stranger.announced == ["hello"]


async def test_a_call_keryx_placed_is_a_delivery():
    owner = FakeVoiceSession(channel="phone", trust=TrustLevel.POSSESSION)

    result = await announce_to_live_sessions(FakeSessions(owner), "hello", needs=TrustLevel.NONE)

    assert (result.heard, result.delivered) == (True, True)


async def test_an_announcement_that_needs_more_than_the_call_has_is_refused():
    """The level is the announcement's, not the session's: news and an approval differ."""
    stranger = FakeVoiceSession(channel="phone", trust=TrustLevel.NONE)

    result = await announce_to_live_sessions(
        FakeSessions(stranger), "something waiting on your screen", needs=TrustLevel.POSSESSION
    )

    assert (result.heard, result.delivered) == (False, False)
    assert stranger.announced == []


async def test_a_session_that_refuses_did_not_hear_it():
    """`announce` returns False for a session already on its way out."""
    result = await announce_to_live_sessions(
        FakeSessions(FakeVoiceSession(channel="phone", accepts=False)), "hello"
    )

    assert (result.heard, result.delivered) == (False, False)


async def test_a_raising_session_is_reported_not_raised():
    """A dead provider socket must not take the caller's fallback down with it."""
    result = await announce_to_live_sessions(
        FakeSessions(FakeVoiceSession(error=RuntimeError("socket is gone"))), "hello"
    )

    assert bool(result) is False


async def test_a_skipped_session_counts_as_having_heard_it():
    """The session holding the line for this very task: its tool result says the same thing."""
    skipped = FakeVoiceSession(channel="local", session_id="holding")

    result = await announce_to_live_sessions(
        FakeSessions(skipped), "hello", skip=lambda session: session.session_id == "holding"
    )

    assert (result.heard, result.delivered) == (True, True)
    assert skipped.announced == []


async def test_announced_is_truthy_only_when_something_heard_it():
    assert not Announced()
    assert Announced(heard=True)


# --- texting ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("twilio", "to", "why"),
    [
        (FakeTwilio(can_text=False), "+15550000001", "SMS_ENABLED is off"),
        (FakeTwilio(), None, "there is nobody to text"),
        (FakeTwilio(), "", "there is nobody to text"),
        (None, "+15550000001", "there is no Twilio at all"),
    ],
)
async def test_nothing_is_sent_when_it_must_not_be(twilio, to, why):
    assert await safe_send_sms(twilio, to, "body") is False, why
    if twilio is not None:
        assert twilio.sent == []


async def test_configured_is_not_the_gate():
    """The ruling this file exists for: `can_text`, never `configured`.

    A Twilio client with perfectly good credentials and `SMS_ENABLED=false` must not send,
    because every one of those sends comes back an HTTP 400 on this account.
    """
    twilio = SimpleNamespace(configured=True, can_text=False, send_sms=None)

    assert await safe_send_sms(twilio, "+15550000001", "body") is False


async def test_a_text_that_goes_reports_true():
    twilio = FakeTwilio()

    assert await safe_send_sms(twilio, "+15550000001", "body") is True
    assert twilio.sent == [("+15550000001", "body")]


async def test_a_twilio_failure_is_reported_not_raised():
    twilio = FakeTwilio(error=RuntimeError("twilio is down"))

    assert await safe_send_sms(twilio, "+15550000001", "body") is False
