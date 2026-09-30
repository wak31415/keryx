"""The three levels of trust a call can be at, and what each of them is worth.

`TrustLevel` is deliberately ordered: everything downstream asks "at least this much"
rather than enumerating the levels it will accept, so a level added between two of these
is a comparison that keeps working rather than a match that silently stops matching.
"""

import pytest
from fakes import FakeProvider, FakeTransport
from test_session import make_settings

from keryx.events import EventBus
from keryx.session import VoiceSession
from keryx.tools import ToolContext, ToolRegistry
from keryx.trust import TrustLevel


def test_the_levels_are_ordered_from_nothing_to_everything():
    assert TrustLevel.NONE < TrustLevel.POSSESSION < TrustLevel.FULL


@pytest.fixture
def make_session(tmp_path):
    def build(*, channel="phone", authorized=False, possession=False) -> VoiceSession:
        transport = FakeTransport(channel=channel, caller="+15555555555")
        return VoiceSession(
            transport,
            FakeProvider(),
            make_settings(tmp_path, pin="123456"),
            ToolRegistry(),
            EventBus(),
            authorized=authorized,
            possession=possession,
        )

    return build


def test_an_inbound_call_before_the_pin_has_proved_nothing(make_session):
    assert make_session().trust is TrustLevel.NONE


def test_a_call_keryx_placed_to_the_owner_holds_the_phone(make_session):
    assert make_session(possession=True).trust is TrustLevel.POSSESSION


def test_the_pin_is_full_trust_however_the_call_started(make_session):
    assert make_session(authorized=True).trust is TrustLevel.FULL
    assert make_session(authorized=True, possession=True).trust is TrustLevel.FULL


def test_the_microphone_is_full_trust_and_needs_no_token(make_session):
    """Nobody spoofs their way onto the machine's own microphone."""
    assert make_session(channel="local").trust is TrustLevel.FULL


def test_authorizing_mid_call_moves_the_level(make_session):
    session = make_session(possession=True)

    session.authorize()

    assert session.trust is TrustLevel.FULL


def test_trusted_is_the_old_spelling_of_full(make_session):
    """Every previous use of `trusted` meant "may see what is the owner's" — that is FULL."""
    assert make_session().trusted is False
    assert make_session(possession=True).trusted is False
    assert make_session(authorized=True).trusted is True
    assert make_session(channel="local").trusted is True


def test_a_tool_reads_the_level_off_the_session_as_it_stands(make_session):
    """A PIN keyed while a tool was thinking has to be visible to the next check."""
    session = make_session()
    ctx = ToolContext(session=session, channel=session.channel, caller=session.caller)

    assert ctx.trust is TrustLevel.NONE
    session.authorize()
    assert ctx.trust is TrustLevel.FULL
