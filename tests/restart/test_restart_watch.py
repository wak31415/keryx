"""Tests for the watchdog that outlives the restart (spec §3.3).

The whole point of the module is a process that survives being killed, so nothing here
starts one: the clock only moves when the code sleeps, the health probe is a lambda, and
Twilio is the same fake the rest of the restart tests use.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest

from jarvis.config import Settings
from jarvis.restart.logscan import log_dir
from jarvis.restart.logscan import marks as log_marks
from jarvis.restart.store import RECORD_NAME, RestartRecord, RestartStore
from jarvis.restart.watchdog import (
    ALERTED,
    CONFIRMED,
    MAX_SMS_CHARS,
    MUTE,
    NOTHING,
    REPORTED,
    watch,
)

OWNER = "+15550000001"
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/repo/src/jarvis/cli.py", line 12, in <module>\n'
    "ModuleNotFoundError: No module named 'jarvis.nope'\n"
)


class FakeTwilioOut:
    """Records what would have been sent; `configured` and the errors are settable."""

    def __init__(self, *, configured: bool = True, sms_enabled: bool = True) -> None:
        self.configured = configured
        #: What `SMS_ENABLED` decides on the real one: texting off, calling unaffected.
        self.sms_enabled = sms_enabled
        self.sms: list[tuple[str, str]] = []
        self.calls: list[dict] = []
        self.sms_error: Exception | None = None
        self.call_error: Exception | None = None

    @property
    def can_text(self) -> bool:
        """Mirrors the real one: credentials *and* `SMS_ENABLED`, derived not snapshotted,
        so a test that drops `configured` afterwards stops texting the way Jarvis would."""
        return self.configured and self.sms_enabled

    async def send_sms(self, to: str, body: str) -> str:
        if self.sms_error is not None:
            raise self.sms_error
        self.sms.append((to, body))
        return "SM1"

    async def place_call(self, to: str, *, twiml: str, status_callback: str | None = None) -> str:
        if self.call_error is not None:
            raise self.call_error
        self.calls.append({"to": to, "twiml": twiml})
        return "CA1"


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps.

    The deadline is then reached in exactly as many polls as the arithmetic says, with no
    wall-clock time spent and nothing to flake on a loaded machine.
    """

    def __init__(self, *, on_sleep=None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.slept))


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "jarvis",
        "state_dir": tmp_path / "state",
        "owner_number_explicit": OWNER,
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    settings.ensure_dirs()
    return settings


def write_log(settings: Settings, name: str, text: str) -> None:
    directory = log_dir(settings.state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a", encoding="utf-8") as handle:
        handle.write(text)


def pending(**fields) -> RestartRecord:
    values = {
        "requested_at": (datetime.now(UTC) - timedelta(seconds=90)).isoformat(),
        "reason": "picked up new code",
        "number": OWNER,
        "target": "systemd jarvis.service",
        "version": "v1-abc1234",
    }
    values.update(fields)
    return RestartRecord(**values)


class Harness:
    """A watchdog with a real record on disk and every outside edge faked."""

    def __init__(self, settings: Settings, *, live: int | None = None, twilio=None) -> None:
        self.settings = settings
        self.store = RestartStore(settings.state_dir / RECORD_NAME)
        self.twilio = twilio or FakeTwilioOut()
        self.clock = FakeClock()
        self.live = live

    async def run(self, *, deadline_s: float = 10.0, poll_s: float = 3.0) -> str:
        return await watch(
            self.settings,
            store=self.store,
            twilio=self.twilio,
            deadline_s=deadline_s,
            poll_s=poll_s,
            sleep=self.clock.sleep,
            probe=lambda settings: self.live,
            clock=self.clock,
        )

    def record(self) -> RestartRecord | None:
        return self.store.load()

    def sms_body(self) -> str:
        assert self.twilio.sms, "nothing was texted"
        return self.twilio.sms[0][1]


@pytest.fixture
def harness(tmp_path):
    return Harness(make_settings(tmp_path))


def spoken(twiml: str) -> str:
    """The words of a `<Say>` document — and proof it is not a media stream."""
    root = ElementTree.fromstring(twiml)
    assert root.find("./Connect") is None, "the alert must not need our own server to answer"
    say = root.find("./Say")
    assert say is not None, twiml
    return say.text or ""


# --- the quiet outcomes ----------------------------------------------------


async def test_no_pending_restart_is_nothing_to_watch(harness):
    assert await harness.run() == NOTHING
    assert not harness.twilio.sms and not harness.twilio.calls


async def test_a_record_that_disappears_means_jarvis_confirmed_it_itself(harness):
    harness.store.save(pending())
    harness.clock.on_sleep = lambda count: harness.store.clear() if count == 2 else None

    assert await harness.run() == CONFIRMED
    assert not harness.twilio.sms and not harness.twilio.calls


async def test_a_restart_jarvis_already_reported_is_left_alone(harness):
    """`failed` means it came back far enough to know — and therefore to have said so."""
    harness.store.save(pending(state="failed", error="systemctl exited 1"))

    assert await harness.run() == REPORTED
    assert not harness.twilio.sms and not harness.twilio.calls


async def test_a_record_that_vanishes_on_the_last_poll_is_still_confirmed(harness):
    """The gap between the final poll and the alert is the one race worth closing."""
    harness.store.save(pending())
    harness.clock.on_sleep = lambda count: harness.store.clear() if count == 4 else None

    assert await harness.run() == CONFIRMED
    assert not harness.twilio.sms


# --- the alert -------------------------------------------------------------


async def test_a_service_that_never_comes_back_is_texted_and_rung(harness):
    harness.store.save(pending())

    assert await harness.run() == ALERTED

    to, body = harness.twilio.sms[0]
    assert to == OWNER
    assert "did not come back" in body
    assert "picked up new code" in body  # the reason they gave
    assert "v1-abc1234" in body  # what it was running, so they can put it back
    assert harness.twilio.calls[0]["to"] == OWNER


async def test_the_alert_call_is_spoken_and_needs_nothing_of_ours_to_answer(harness):
    """A <Connect><Stream> would be answered by the very server that is down."""
    harness.store.save(pending())

    await harness.run()

    words = spoken(harness.twilio.calls[0]["twiml"])
    assert "Jarvis alert" in words
    assert "did not come back" in words
    assert "by text" in words  # the detail is in the message, not read out


async def test_the_alert_carries_the_error_the_logs_have(harness):
    """This is the answer to "did the update work", and it is the only one there is."""
    write_log(harness.settings, "jarvis.log", "old news\n")
    harness.store.save(pending(log_marks=log_marks(harness.settings.state_dir)))
    write_log(harness.settings, "jarvis.err.log", TRACEBACK)

    await harness.run()

    assert "ModuleNotFoundError" in harness.sms_body()


async def test_the_alert_names_the_task_the_restart_was_loading(harness):
    harness.store.save(pending(task_id=9))

    await harness.run()

    assert "task 9" in harness.sms_body()


async def test_a_service_that_answers_but_never_confirmed_is_worded_differently(tmp_path):
    """Up and silent is a different problem from gone, and a different thing to go and do."""
    harness = Harness(make_settings(tmp_path), live=0)
    harness.store.save(pending())

    assert await harness.run() == ALERTED

    body = harness.sms_body()
    assert "never confirmed it" in body
    assert "did not come back" not in body
    assert "never confirmed it" in spoken(harness.twilio.calls[0]["twiml"])


async def test_the_alert_marks_the_record_so_a_late_start_does_not_ring_about_it(harness):
    """A record left pending is a Jarvis that starts an hour later and calls about this."""
    harness.store.save(pending())

    await harness.run()

    record = harness.record()
    assert record is not None
    assert record.state == "failed"
    assert record.error == "never came back"


async def test_a_text_is_one_message(harness):
    harness.store.save(pending(reason="x" * 4000, log_marks=log_marks(harness.settings.state_dir)))

    await harness.run()

    assert len(harness.sms_body()) <= MAX_SMS_CHARS


# --- when even the alert cannot go out -------------------------------------


async def test_without_twilio_the_failure_is_still_recorded(tmp_path):
    harness = Harness(make_settings(tmp_path), twilio=FakeTwilioOut(configured=False))
    harness.store.save(pending())

    assert await harness.run() == MUTE
    assert harness.record().state == "failed"


async def test_a_failed_text_still_gets_the_call_placed(harness):
    harness.twilio.sms_error = RuntimeError("twilio is down")
    harness.store.save(pending())

    assert await harness.run() == ALERTED

    words = spoken(harness.twilio.calls[0]["twiml"])
    assert "by text" not in words  # nothing to point them at, so it does not promise one
    assert "Check the machine" in words


async def test_a_failed_call_still_counts_when_the_text_went(harness):
    harness.twilio.call_error = RuntimeError("twilio is down")
    harness.store.save(pending())

    assert await harness.run() == ALERTED
    assert harness.twilio.sms


async def test_the_watchdog_never_raises(harness, monkeypatch):
    """It is the last thing standing; a traceback from here reaches nobody."""

    def explode(self):
        raise OSError("the disk is gone")

    monkeypatch.setattr(RestartStore, "load", explode)

    assert await harness.run() == MUTE


async def test_the_deadline_is_reached_by_polling_not_by_waiting(harness):
    """Nothing in the suite may spend real time; the clock moves only when we sleep."""
    harness.store.save(pending())

    await harness.run(deadline_s=10.0, poll_s=3.0)

    assert harness.clock.slept == [3.0, 3.0, 3.0, 3.0]
    assert asyncio.get_running_loop() is not None  # nothing detached itself from the loop


async def test_with_texting_off_the_alert_is_the_call(tmp_path):
    """Jarvis is down, so the `<Say>` call is the only channel left that works at all."""
    harness = Harness(make_settings(tmp_path), twilio=FakeTwilioOut(sms_enabled=False))
    harness.store.save(pending())

    assert await harness.run() == ALERTED

    assert harness.twilio.sms == []
    words = spoken(harness.twilio.calls[0]["twiml"])
    assert "by text" not in words  # it must not point them at a message they will never get
    assert "Check the machine" in words
