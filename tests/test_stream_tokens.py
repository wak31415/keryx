"""Tests for the one-time media-stream tokens that guard `WS /twilio/media` (spec §3.3)."""

from jarvis.stream_tokens import StreamTokenStore


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
