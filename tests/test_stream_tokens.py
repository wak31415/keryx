"""Tests for the one-time media-stream tokens that guard `WS /twilio/media`."""

from keryx.stream_tokens import (
    StreamTokenStore,
    TokenInfo,
    confers_possession,
    outbound_extra,
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_issue_returns_a_long_random_token():
    store = StreamTokenStore()

    first = store.issue("+15551234567")
    second = store.issue("+15551234567")

    assert first != second
    assert len(first) >= 20


def test_redeem_returns_the_caller_and_extras():
    clock = FakeClock()
    store = StreamTokenStore(now=clock)

    token = store.issue("+15551234567", {"call_sid": "CA1", "opening_context": "task 7 is done"})
    info = store.redeem(token)

    assert info is not None
    assert info.caller == "+15551234567"
    assert info.extra == {"call_sid": "CA1", "opening_context": "task 7 is done"}


def test_a_token_can_only_be_redeemed_once():
    store = StreamTokenStore()
    token = store.issue("+15551234567")

    assert store.redeem(token) is not None
    assert store.redeem(token) is None


def test_an_unknown_or_empty_token_is_rejected():
    store = StreamTokenStore()
    store.issue("+15551234567")

    assert store.redeem("nope") is None
    assert store.redeem("") is None


def test_a_token_expires_after_its_ttl():
    clock = FakeClock()
    store = StreamTokenStore(now=clock)
    token = store.issue("+15551234567", ttl_s=60)

    clock.advance(59)
    assert store.redeem(token) is not None

    token = store.issue("+15551234567", ttl_s=60)
    clock.advance(61)
    assert store.redeem(token) is None


def test_expired_tokens_do_not_pile_up():
    clock = FakeClock()
    store = StreamTokenStore(now=clock)
    for _ in range(5):
        store.issue("+15551234567", ttl_s=60)

    clock.advance(61)
    store.issue("+15551234567")

    assert len(store) == 1


def test_a_token_can_carry_no_caller_and_no_extras():
    store = StreamTokenStore()

    info = store.redeem(store.issue(None))

    assert info is not None
    assert info.caller is None
    assert info.extra == {}


# --- what a token says about who placed the call ---------------------------

OWNER = "+15555555555"
OTHER = "+15550009999"


def test_an_outbound_token_records_that_keryx_placed_it_and_what_it_dialled():
    extra = outbound_extra(OWNER, opening_context="task 3 is done", task_id=3)

    assert extra == {
        "keryx_placed": True,
        "dialled": OWNER,
        "opening_context": "task 3 is done",
        "task_id": 3,
    }


def test_a_call_keryx_placed_to_the_owner_confers_possession():
    info = TokenInfo(caller=OWNER, extra=outbound_extra(OWNER))

    assert confers_possession(info, (OWNER,)) is True


def test_any_phone_of_the_owners_confers_it_not_just_the_one_keryx_rings_first():
    """This is a single-owner agent: the allowlist is their handsets, not a guest list.

    Ringing them back on the second one reaches the same person, so requiring the first
    would only have made the tier fail quietly on a call that proved just as much.
    """
    second = "+15557000000"
    info = TokenInfo(caller=second, extra=outbound_extra(second))

    assert confers_possession(info, (OWNER, second)) is True


def test_a_number_that_is_not_the_owners_confers_nothing():
    """A call-back to a number named on the call is a new decision, and needs the PIN."""
    info = TokenInfo(caller=OTHER, extra=outbound_extra(OTHER))

    assert confers_possession(info, (OWNER,)) is False


def test_an_inbound_token_confers_nothing_however_it_is_dressed():
    """The caller is `From`, which is spoofable: only the placing flag may be believed."""
    numbers = (OWNER,)
    assert confers_possession(TokenInfo(caller=OWNER, extra={"call_sid": "CA1"}), numbers) is False
    assert confers_possession(TokenInfo(caller=OWNER, extra={"dialled": OWNER}), numbers) is False


def test_with_no_number_of_the_owners_nothing_confers_possession():
    """Nothing to compare against is not a match; it is the absence of the rule."""
    assert confers_possession(TokenInfo(caller=OWNER, extra=outbound_extra(OWNER)), ()) is False
    assert confers_possession(TokenInfo(caller=None, extra=outbound_extra("")), ("",)) is False
