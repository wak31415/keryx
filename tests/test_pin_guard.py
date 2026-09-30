"""Tests for `keryx.pin_guard`: wrong PINs counted across every call, and the lock they set.

The clock is always injected, so a day of failures and an hour of lockout take no time.
"""

import json
import logging
import stat

import pytest

from keryx.config import Settings
from keryx.pin_guard import STATE_NAME, Lockout, PinGuard

LIMIT = 4
WINDOW = 24 * 3600.0
COOLDOWN = 3600.0
START = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "keryx" / STATE_NAME


def make_guard(path, clock) -> PinGuard:
    return PinGuard(
        path, limit=LIMIT, window_seconds=WINDOW, lockout_seconds=COOLDOWN, clock=clock
    )


def fail(guard: PinGuard, times: int) -> list[Lockout | None]:
    return [guard.record_failure() for _ in range(times)]


# --- the count and the lock --------------------------------------------------


def test_a_fresh_guard_is_open(path, clock):
    assert make_guard(path, clock).locked_until() is None


def test_wrong_pins_below_the_limit_lock_nothing(path, clock):
    guard = make_guard(path, clock)

    assert fail(guard, LIMIT - 1) == [None] * (LIMIT - 1)
    assert guard.locked_until() is None


def test_the_wrong_pin_that_reaches_the_limit_locks_pin_entry(path, clock):
    guard = make_guard(path, clock)
    fail(guard, LIMIT - 1)

    lockout = guard.record_failure()

    assert lockout == Lockout(until=START + COOLDOWN, failures=LIMIT, alert=True)
    assert guard.locked_until() == START + COOLDOWN


def test_the_lock_lifts_after_the_cooldown(path, clock):
    guard = make_guard(path, clock)
    fail(guard, LIMIT)

    clock.advance(COOLDOWN - 1)
    assert guard.locked_until() == START + COOLDOWN
    clock.advance(1)
    assert guard.locked_until() is None


def test_past_the_limit_every_further_wrong_pin_locks_it_again(path, clock):
    """Neither the lock nor its lifting resets the count: one guess per cooldown after it."""
    guard = make_guard(path, clock)
    fail(guard, LIMIT)
    clock.advance(COOLDOWN)

    lockout = guard.record_failure()

    assert lockout is not None
    assert lockout.until == clock.now + COOLDOWN
    assert lockout.failures == LIMIT + 1


def test_wrong_pins_older_than_the_window_are_forgotten(path, clock):
    guard = make_guard(path, clock)
    fail(guard, LIMIT - 1)
    clock.advance(WINDOW)

    assert guard.record_failure() is None
    assert guard.locked_until() is None


# --- telling the owner ---------------------------------------------------------


def test_the_owner_is_told_once_while_the_lock_keeps_coming_back(path, clock):
    guard = make_guard(path, clock)
    assert fail(guard, LIMIT)[-1].alert is True

    for _ in range(5):
        clock.advance(COOLDOWN)
        assert guard.record_failure().alert is False


def test_a_lock_still_coming_back_a_whole_window_later_is_told_again(path, clock):
    """A campaign that keeps going: one guess each time the lock lifts, for a whole day."""
    guard = make_guard(path, clock)
    fail(guard, LIMIT)

    alerts = []
    for _ in range(round(WINDOW / COOLDOWN)):
        clock.advance(COOLDOWN)
        alerts.append(guard.record_failure().alert)

    assert alerts == [False] * (len(alerts) - 1) + [True]


# --- the file ----------------------------------------------------------------


def test_the_count_and_the_lock_survive_a_restart(path, clock):
    fail(make_guard(path, clock), LIMIT - 1)
    restarted = make_guard(path, clock)

    assert restarted.locked_until() is None
    assert restarted.record_failure() == Lockout(START + COOLDOWN, LIMIT, alert=True)
    assert make_guard(path, clock).locked_until() == START + COOLDOWN


def test_a_restart_does_not_forget_that_the_owner_was_told(path, clock):
    fail(make_guard(path, clock), LIMIT)
    clock.advance(COOLDOWN)

    assert make_guard(path, clock).record_failure().alert is False


def test_the_file_is_owner_only(path, clock):
    fail(make_guard(path, clock), 1)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_nothing_is_written_until_there_is_something_to_count(path, clock):
    make_guard(path, clock).locked_until()

    assert not path.exists()


def test_starting_up_does_not_rewrite_a_count_that_has_not_changed(path, clock, monkeypatch):
    """A write-back of what was just read could only ever undo somebody else's newer write."""
    fail(make_guard(path, clock), 1)

    def no_writes(*args):
        raise AssertionError("the unchanged count was written back")

    monkeypatch.setattr("keryx.pin_guard.os.replace", no_writes)

    assert make_guard(path, clock).locked_until() is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"failures": "many"}),
        json.dumps({"failures": [START, "yesterday"]}),
        json.dumps({"failures": [], "locked_until": True}),
    ],
)
def test_an_unreadable_file_locks_for_one_cooldown_and_no_longer(path, clock, content, caplog):
    """The one state where "nobody has guessed" and "someone nearly got there" look alike."""
    path.parent.mkdir(parents=True)
    path.write_text(content)

    with caplog.at_level(logging.ERROR, logger="keryx.pin_guard"):
        guard = make_guard(path, clock)

    assert guard.locked_until() == START + COOLDOWN
    assert str(path) in caplog.text
    clock.advance(COOLDOWN / 2)
    assert make_guard(path, clock).locked_until() == START + COOLDOWN  # a restart: no longer
    clock.advance(COOLDOWN / 2)
    assert make_guard(path, clock).locked_until() is None
    assert make_guard(path, clock).record_failure() is None  # and nothing was counted


def test_a_lock_a_clock_change_pushed_far_ahead_is_pulled_back_to_one_cooldown(path, clock):
    path.parent.mkdir(parents=True)
    later = START + 365 * 24 * 3600
    path.write_text(json.dumps({"failures": [later], "locked_until": later, "alerted_at": later}))

    guard = make_guard(path, clock)

    assert guard.locked_until() == START + COOLDOWN
    clock.advance(COOLDOWN)
    assert make_guard(path, clock).locked_until() is None
    clock.advance(WINDOW)
    assert make_guard(path, clock).record_failure() is None  # the future failure aged out too


def test_a_file_that_cannot_be_written_still_counts_in_memory(path, clock, caplog, monkeypatch):
    """A full disk must not be a way to guess for free."""

    def refuse(*args):
        raise OSError("no space left on device")

    monkeypatch.setattr("keryx.pin_guard.os.replace", refuse)
    guard = make_guard(path, clock)

    with caplog.at_level(logging.ERROR, logger="keryx.pin_guard"):
        lockouts = fail(guard, LIMIT)

    assert lockouts[-1] is not None
    assert guard.locked_until() == START + COOLDOWN
    assert "could not write" in caplog.text
    assert not path.with_name(path.name + ".tmp").exists()


# --- settings ------------------------------------------------------------------


def test_the_guard_takes_its_numbers_and_its_file_from_settings(tmp_path, clock):
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "keryx",
        pin_failure_limit=2,
        pin_failure_window_hours=1,
        pin_lockout_minutes=5,
    )
    guard = PinGuard.for_settings(settings, clock=clock)

    assert guard.record_failure() is None
    assert guard.record_failure() == Lockout(START + 300, 2, alert=True)
    assert (settings.data_dir / STATE_NAME).exists()
    clock.advance(3600)
    assert guard.record_failure() is None


def test_the_defaults_are_ten_wrong_pins_a_day_and_an_hour_locked(settings):
    assert settings.pin_failure_limit == 10
    assert settings.pin_failure_window_hours == 24
    assert settings.pin_lockout_minutes == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [("pin_failure_limit", 0), ("pin_failure_window_hours", 0), ("pin_lockout_minutes", 0)],
)
def test_the_lock_cannot_be_configured_away(tmp_path, field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path, **{field: value})
