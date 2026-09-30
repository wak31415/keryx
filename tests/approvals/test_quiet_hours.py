"""Tests for `ApprovalBroker._quiet_now`, which had none.

The window is compared against a naive `datetime.now()`, so "23:00-07:00" means eleven at
night *where the machine is*. That is a real assumption about the deployment — the host's
timezone is taken to be the owner's — and it is written down on the wiki's
"Assumptions and Deployments" page and at the line itself. These tests pin the
behaviour so a later change to a timezone-aware clock is a deliberate one, not an accident
that only shows up as a phone call at four in the morning.
"""

from datetime import datetime

import pytest

from keryx.approvals.broker import ApprovalBroker
from keryx.config import Settings


def broker_at(monkeypatch, tmp_path, *, window: str | None, hour: int, minute: int = 0):
    """A broker whose idea of "now" is a fixed naive local time."""
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "keryx",
        approval_quiet_hours=window,
    )
    broker = ApprovalBroker(settings, sessions=None, twilio_out=None, stream_tokens=None)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None, "quiet hours are deliberately naive local time"
            return datetime(2026, 9, 2, hour, minute)

    monkeypatch.setattr("keryx.approvals.broker.datetime", FixedClock)
    return broker


@pytest.mark.parametrize("window", [None, "", "   "])
def test_no_window_is_never_quiet(monkeypatch, tmp_path, window):
    assert broker_at(monkeypatch, tmp_path, window=window, hour=3)._quiet_now() is False


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(8, False), (9, True), (12, True), (16, True), (17, False), (22, False)],
)
def test_an_ordinary_window_covers_start_up_to_but_not_including_end(
    monkeypatch, tmp_path, hour, quiet
):
    broker = broker_at(monkeypatch, tmp_path, window="09:00-17:00", hour=hour)

    assert broker._quiet_now() is quiet


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(22, False), (23, True), (2, True), (6, True), (7, False), (12, False)],
)
def test_a_window_that_crosses_midnight_wraps(monkeypatch, tmp_path, hour, quiet):
    """`23:00-07:00` is the one anybody actually sets, and it is the one that wraps."""
    broker = broker_at(monkeypatch, tmp_path, window="23:00-07:00", hour=hour)

    assert broker._quiet_now() is quiet


@pytest.mark.parametrize(("minute", "quiet"), [(15, False), (30, True), (45, True), (59, True)])
def test_the_minutes_are_not_ignored(monkeypatch, tmp_path, minute, quiet):
    broker = broker_at(monkeypatch, tmp_path, window="22:30-23:00", hour=22, minute=minute)

    assert broker._quiet_now() is quiet


@pytest.mark.parametrize("window", ["23:00", "not a window", "25:00-26:00", "23:00-", "-07:00"])
def test_an_unparseable_window_never_silences_the_phone(monkeypatch, tmp_path, window):
    """Failing open is the right way round: a typo must not quietly stop them being told."""
    assert broker_at(monkeypatch, tmp_path, window=window, hour=3)._quiet_now() is False
