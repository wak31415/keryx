"""Tests for the restart flow: schedule one, then confirm it afterwards (spec §3.3).

Nothing here starts a process, opens a socket or waits on a clock. The service manager is
a fake `spawn`, Twilio is a fake, and `sleep` is a double that returns at once — and that
plays dead where the real process would be killed by the restart it just asked for.
"""

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from fakes import FakeVoiceSession

from jarvis.config import Settings
from jarvis.restart.coordinator import (
    EXEC_CONFIRM_S,
    MAX_CALLBACK_ATTEMPTS,
    RestartCoordinator,
)
from jarvis.restart.health import health_probe, wait_until_serving
from jarvis.restart.logscan import log_dir
from jarvis.restart.logscan import marks as log_marks
from jarvis.restart.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    WATCH_UNIT_PREFIX,
    ServiceTarget,
    WatchPlan,
    is_installed,
    read_cgroup,
    resolve_target,
    runs_under,
    spawn_watchdog,
    watch_command,
    watch_log_path,
)
from jarvis.restart.store import RestartRecord, RestartStore, format_duration
from jarvis.restart.version import (
    current_version,
    loaded_version,
    mark_running,
    mark_startup_logs,
    running_version,
    startup_log_marks,
)
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

OWNER = "+15550000001"
CALLER = "+15551234567"
HOST = "jarvis.example"
VERSION = "v1-abc1234"
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/repo/src/jarvis/cli.py", line 12, in <module>\n'
    "ModuleNotFoundError: No module named 'jarvis.nope'\n"
)


def write_log(settings, name: str, text: str) -> None:
    """Append to one of the service log files, as systemd/launchd would."""
    directory = log_dir(settings.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a", encoding="utf-8") as handle:
        handle.write(text)


# --- doubles ---------------------------------------------------------------


class FakeTwilioOut:
    """Records what would have gone to Twilio; `configured` and the errors are settable."""

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
        self.calls.append({"to": to, "twiml": twiml, "status_callback": status_callback})
        return "CA1"


class FakeSleep:
    """A sleep that returns at once, and plays dead where the real process would be killed.

    `_execute` waits `EXEC_CONFIRM_S` to be taken down by the service manager it just asked
    for a restart. Letting that wait *return* is the machine failing to restart, so the
    default double raises `CancelledError` there instead — which is what being killed looks
    like from the inside. `dies=False` is the test that wants the other outcome.
    """

    def __init__(self, *, dies: bool = True, hook=None) -> None:
        self.calls: list[float] = []
        self.dies = dies
        self.hook = hook

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.hook is not None:
            self.hook(len(self.calls))
        if self.dies and seconds >= EXEC_CONFIRM_S:
            raise asyncio.CancelledError("the service manager killed us")
        await asyncio.sleep(0)


class FakeSpawn:
    """The service manager: records the command and returns the exit code it is given."""

    def __init__(self, code: int | None = 0, error: Exception | None = None) -> None:
        self.commands: list[list[str]] = []
        self.code = code
        self.error = error

    async def __call__(self, command) -> int | None:
        self.commands.append(list(command))
        if self.error is not None:
            raise self.error
        return self.code


class FakeWatchSpawn:
    """The watchdog: records the plan it was handed instead of starting anything.

    The real one starts a process that deliberately outlives us, which is exactly what a
    test suite must never do — see the testing rule in CLAUDE.md.
    """

    def __init__(self, *, error: Exception | None = None, pid: int = 4321) -> None:
        self.plans: list = []
        self.error = error
        self.pid = pid

    def __call__(self, plan, settings) -> int:
        self.plans.append(plan)
        if self.error is not None:
            raise self.error
        return self.pid


@pytest.fixture(autouse=True)
def _systemd_on_path(monkeypatch):
    """Pretend this host has systemd, whatever host it is.

    These tests are about what the coordinator *does* with a service manager, not about
    whether the machine running pytest happens to have one — and `resolve_target` refuses
    outright when `systemctl` is not on PATH, so on macOS two dozen of them failed on the
    fixture instead of on anything they were checking. The tests that are about resolution
    itself pass their own `which` and are unaffected.
    """
    real = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: f"/usr/bin/{name}"
        if name in {"systemctl", "systemd-run"}
        else real(name, *a, **k),
    )


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "jarvis",
        "owner_number_explicit": OWNER,
        "public_host": HOST,
        "service_manager": "systemd",
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    settings.ensure_dirs()
    return settings


class Harness:
    """A coordinator with every outside edge faked, plus the pieces to assert on."""

    def __init__(
        self, settings, *, spawn=None, sleep=None, tasks=None, twilio=None, watch=None
    ) -> None:
        self.settings = settings
        self.sessions = SessionRegistry()
        self.twilio = twilio or FakeTwilioOut()
        self.tokens = StreamTokenStore()
        self.spawn = spawn or FakeSpawn()
        self.watch = watch or FakeWatchSpawn()
        self.sleep = sleep or FakeSleep()
        self.store = RestartStore(settings.data_dir / "restart.json")
        self.coordinator = RestartCoordinator(
            settings,
            self.sessions,
            self.twilio,
            self.tokens,
            tasks,
            store=self.store,
            spawn=self.spawn,
            spawn_watch=self.watch,
            sleep=self.sleep,
        )

    async def settle(self) -> None:
        """Let the background restart task run to wherever it gets to."""
        deferred = self.coordinator._deferred
        if deferred is not None:
            with contextlib_suppress():
                await deferred

    def record(self) -> RestartRecord | None:
        return self.store.load()


class contextlib_suppress:
    """`contextlib.suppress(CancelledError)` for an await — spelled out to stay readable."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)


@pytest.fixture(autouse=True)
def _fixed_version(monkeypatch):
    """`git describe` is real work on a real checkout; pin it so summaries are assertable."""
    monkeypatch.setattr("jarvis.restart.version.current_version", lambda repo=None: VERSION)


@pytest.fixture
def harness(tmp_path):
    return Harness(make_settings(tmp_path))


def stream_parameters(twiml: str) -> dict[str, str]:
    """The `<Parameter>` name/value pairs inside a `<Connect><Stream>` document."""
    stream = ElementTree.fromstring(twiml).find("./Connect/Stream")
    assert stream is not None, twiml
    return {p.get("name"): p.get("value") for p in stream.findall("Parameter")}


def pending(**fields) -> RestartRecord:
    """A record as the process that asked for the restart would have left it."""
    values = {
        "requested_at": datetime.now(UTC).isoformat(),
        "reason": "picked up new code",
        "number": OWNER,
        "origin_channel": "phone",
        "target": "systemd jarvis.service",
        "version": "v0-old0000",
    }
    values.update(fields)
    return RestartRecord(**values)


# --- the service manager ---------------------------------------------------


def yes(_target) -> bool:
    return True


def no(_target) -> bool:
    return False


def test_auto_picks_systemd_when_this_process_runs_under_the_unit(tmp_path):
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings, platform="linux", which=lambda name: f"/usr/bin/{name}", supervising=yes
    )

    assert target is not None
    assert (target.manager, target.unit) == ("systemd", SYSTEMD_UNIT)
    assert target.command() == ["systemctl", "--user", "restart", SYSTEMD_UNIT]


def test_auto_picks_launchd_when_this_process_runs_as_the_agent(tmp_path):
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings, platform="darwin", which=lambda name: f"/bin/{name}", supervising=yes
    )

    assert target is not None
    assert (target.manager, target.unit) == ("launchd", LAUNCHD_LABEL)
    assert target.command() == [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{os.getuid()}/{LAUNCHD_LABEL}",
    ]


def test_nothing_supervising_us_is_no_target(tmp_path):
    """A Jarvis started by hand has nothing that would start it again: it must not stop."""
    settings = make_settings(tmp_path, service_manager="auto")

    assert resolve_target(settings, platform="linux", which=lambda name: None) is None


def test_systemctl_on_path_is_not_the_same_as_being_supervised(tmp_path):
    """Every Linux desktop has systemctl. A `jarvis serve` in a terminal is still unsupervised.

    It used to resolve to `systemctl --user restart jarvis.service` regardless — which
    failed "unit not found" where the service was not installed, telling nobody, and where
    it was, restarted the *installed* copy instead of the one that was asked.
    """
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings,
        platform="linux",
        which=lambda name: f"/usr/bin/{name}",
        supervising=no,
        installed=yes,
    )

    assert target is None


def test_from_outside_an_installed_unit_is_the_target(tmp_path):
    """`jarvis restart` in a terminal is never under the unit; restarting it is its job."""
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings,
        platform="linux",
        which=lambda name: f"/usr/bin/{name}",
        from_outside=True,
        supervising=no,
        installed=yes,
    )

    assert target == ServiceTarget("systemd", SYSTEMD_UNIT)


def test_from_outside_nothing_installed_is_no_target(tmp_path):
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings,
        platform="linux",
        which=lambda name: f"/usr/bin/{name}",
        from_outside=True,
        supervising=no,
        installed=no,
    )

    assert target is None


def test_auto_checks_supervision_against_the_configured_unit(tmp_path):
    settings = make_settings(tmp_path, service_manager="auto", service_unit="jarvis-dev.service")
    asked: list[ServiceTarget] = []

    def supervising(target):
        asked.append(target)
        return True

    target = resolve_target(
        settings, platform="linux", which=lambda name: "/usr/bin/x", supervising=supervising
    )

    assert asked == [ServiceTarget("systemd", "jarvis-dev.service")]
    assert target == asked[0]


def test_service_manager_none_refuses(tmp_path):
    settings = make_settings(tmp_path, service_manager="none")

    target = resolve_target(
        settings, platform="linux", which=lambda name: "/usr/bin/x", supervising=yes
    )

    assert target is None


def test_auto_on_a_platform_with_neither_manager_is_no_target(tmp_path):
    settings = make_settings(tmp_path, service_manager="auto")

    target = resolve_target(
        settings, platform="win32", which=lambda name: "/usr/bin/x", supervising=yes
    )

    assert target is None


def test_a_configured_manager_without_its_command_is_no_target(tmp_path):
    settings = make_settings(tmp_path, service_manager="systemd")

    assert resolve_target(settings, platform="linux", which=lambda name: None) is None


def test_a_configured_manager_is_taken_at_its_word(tmp_path):
    """`SERVICE_MANAGER=systemd` is somebody saying so; only `auto` goes looking."""
    settings = make_settings(tmp_path, service_manager="systemd")

    target = resolve_target(
        settings,
        platform="linux",
        which=lambda name: "/usr/bin/systemctl",
        supervising=no,
        installed=no,
    )

    assert target == ServiceTarget("systemd", SYSTEMD_UNIT)


def test_the_unit_can_be_overridden(tmp_path):
    settings = make_settings(tmp_path, service_manager="systemd", service_unit="jarvis-dev.service")

    target = resolve_target(settings, platform="linux", which=lambda name: "/usr/bin/systemctl")

    assert target.unit == "jarvis-dev.service"


# --- is this process the unit? ----------------------------------------------

USER_MANAGER = "/user.slice/user-1000.slice/user@1000.service"


@pytest.mark.parametrize(
    ("cgroup", "expected"),
    [
        (f"0::{USER_MANAGER}/app.slice/jarvis.service\n", True),
        # cgroup v1 (or hybrid): the systemd hierarchy is the named one.
        (f"12:cpu:/\n1:name=systemd:{USER_MANAGER}/app.slice/jarvis.service\n", True),
        # A terminal, a tmux pane, an ssh login: a scope, not the unit.
        (f"0::{USER_MANAGER}/tmux-spawn-0f1e.scope\n", False),
        ("0::/user.slice/user-1000.slice/session-3.scope\n", False),
        # Some other user service that happens to have started us, like a tmux server.
        (f"0::{USER_MANAGER}/app.slice/tmux.service\n", False),
        # A system unit of that name: `systemctl --user` cannot restart it.
        ("0::/system.slice/jarvis.service\n", False),
        ("", False),
    ],
)
def test_systemd_supervision_is_read_from_this_process_cgroup(cgroup, expected):
    target = ServiceTarget("systemd", "jarvis.service")

    assert runs_under(target, cgroup=cgroup) is expected


def test_launchd_supervision_is_the_job_label_launchd_sets():
    target = ServiceTarget("launchd", LAUNCHD_LABEL)

    assert runs_under(target, environ={"XPC_SERVICE_NAME": LAUNCHD_LABEL}) is True
    # What Terminal.app hands a shell.
    assert runs_under(target, environ={"XPC_SERVICE_NAME": "0"}) is False
    assert runs_under(target, environ={}) is False


def test_no_cgroup_file_reads_as_no_cgroup(tmp_path):
    assert read_cgroup(tmp_path / "missing") == ""


# --- is the unit installed? --------------------------------------------------


def recording_run(result=None, error=None):
    """A `subprocess.run` double that records the command and never starts anything."""
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        assert kwargs.get("timeout"), "a probe of the service manager must not hang"
        if error is not None:
            raise error
        return result

    return run, calls


def test_a_loaded_systemd_unit_is_installed():
    run, calls = recording_run(SimpleNamespace(returncode=0, stdout="loaded\n"))

    assert is_installed(ServiceTarget("systemd", "jarvis.service"), run=run) is True
    # A read-only query, and nothing else.
    assert calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", "jarvis.service"]
    ]


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=0, stdout="not-found\n"),
        SimpleNamespace(returncode=1, stdout=""),  # no user bus: ssh without linger
    ],
)
def test_an_unknown_systemd_unit_is_not_installed(result):
    run, _ = recording_run(result)

    assert is_installed(ServiceTarget("systemd", "jarvis.service"), run=run) is False


@pytest.mark.parametrize(
    "error", [OSError("no systemctl"), subprocess.TimeoutExpired("systemctl", 5)]
)
def test_a_probe_that_fails_reads_as_not_installed(error):
    run, _ = recording_run(error=error)

    assert is_installed(ServiceTarget("systemd", "jarvis.service"), run=run) is False


def test_a_loaded_launch_agent_is_installed():
    run, calls = recording_run(SimpleNamespace(returncode=0, stdout="..."))

    assert is_installed(ServiceTarget("launchd", LAUNCHD_LABEL), run=run) is True
    assert calls == [["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]]

    run, _ = recording_run(SimpleNamespace(returncode=113, stdout=""))
    assert is_installed(ServiceTarget("launchd", LAUNCHD_LABEL), run=run) is False


def test_current_version_never_raises(tmp_path):
    """Whatever it finds (or does not), it is decoration and must not throw."""
    assert current_version(tmp_path) is None or isinstance(current_version(tmp_path), str)


# --- the record ------------------------------------------------------------


def test_the_record_round_trips(tmp_path):
    store = RestartStore(tmp_path / "restart.json")
    record = pending()

    assert store.save(record)

    assert store.load() == record


def test_the_record_is_private(tmp_path):
    """It holds a phone number, so it is written 0600."""
    store = RestartStore(tmp_path / "restart.json")

    store.save(pending())

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_a_missing_record_is_no_record(tmp_path):
    assert RestartStore(tmp_path / "nothing.json").load() is None


def test_a_corrupt_record_is_no_record(tmp_path):
    path = tmp_path / "restart.json"
    path.write_text("{not json")

    assert RestartStore(path).load() is None


def test_unknown_fields_in_the_record_are_ignored(tmp_path):
    """A record written by another version must not stop this one from reading it."""
    path = tmp_path / "restart.json"
    path.write_text(json.dumps({"requested_at": "2026-08-24T10:00:00+00:00", "future": 1}))

    record = RestartStore(path).load()

    assert record is not None and record.requested_at.startswith("2026-08-24")


def test_clearing_a_record_that_is_gone_is_fine(tmp_path):
    RestartStore(tmp_path / "nothing.json").clear()


def test_age_of_an_unreadable_stamp_is_unknown():
    assert RestartRecord(requested_at="whenever").age_seconds() is None


def test_age_counts_from_the_request():
    asked = datetime.now(UTC) - timedelta(seconds=30)
    record = RestartRecord(requested_at=asked.isoformat())

    assert 29 <= record.age_seconds() <= 31


# --- asking for a restart --------------------------------------------------


async def test_a_quiet_line_restarts_at_once(harness):
    result = await harness.coordinator.request(reason="new code", origin_channel="phone")

    assert result["status"] == "restarting"
    await harness.settle()
    assert harness.spawn.commands == [["systemctl", "--user", "restart", SYSTEMD_UNIT]]


async def test_the_record_says_who_to_call_and_what_was_running(harness):
    await harness.coordinator.request(reason="new code", number=CALLER, origin_channel="phone")

    record = harness.record()
    assert record.number == CALLER
    assert record.reason == "new code"
    assert record.version == VERSION
    assert record.target == "systemd jarvis.service"
    assert record.state == "pending"


async def test_without_a_number_the_owner_is_called(harness):
    await harness.coordinator.request()

    assert harness.record().number == OWNER


async def test_a_live_call_holds_the_restart_until_it_ends(tmp_path):
    """A restart drops every call, so it waits for the one that asked for it to end."""
    harness = Harness(make_settings(tmp_path))
    session = FakeVoiceSession(channel="phone")
    harness.sessions.add(session)
    # The line clears while the coordinator is waiting for it.
    harness.sleep.hook = lambda calls: session.__setattr__("is_live", False)

    result = await harness.coordinator.request(origin_channel="phone")

    assert result["status"] == "deferred"
    assert harness.spawn.commands == []
    await harness.settle()
    assert harness.spawn.commands == [["systemctl", "--user", "restart", SYSTEMD_UNIT]]


async def test_a_call_that_never_ends_cancels_the_restart(tmp_path):
    """Rather than cut somebody off, the restart gives up and leaves no record behind."""
    harness = Harness(make_settings(tmp_path))
    harness.sessions.add(FakeVoiceSession(channel="phone"))

    await harness.coordinator.request()
    await harness.settle()

    assert harness.spawn.commands == []
    assert harness.record() is None


async def test_force_restarts_through_a_live_call(tmp_path):
    harness = Harness(make_settings(tmp_path))
    harness.sessions.add(FakeVoiceSession(channel="phone"))

    result = await harness.coordinator.request(force=True)

    assert result["status"] == "restarting"
    await harness.settle()
    assert harness.spawn.commands


async def test_a_second_request_does_not_stack(tmp_path):
    harness = Harness(make_settings(tmp_path))
    harness.sessions.add(FakeVoiceSession(channel="phone"))

    await harness.coordinator.request()
    result = await harness.coordinator.request()

    assert result["status"] == "already_pending"


async def test_no_service_manager_refuses_and_writes_nothing(tmp_path):
    harness = Harness(make_settings(tmp_path, service_manager="none"))

    result = await harness.coordinator.request()

    assert result["status"] == "unsupported"
    assert "cannot restart yourself" in result["message"]
    assert harness.record() is None
    assert harness.spawn.commands == []


async def test_a_copy_started_by_hand_refuses_rather_than_restart_the_installed_one(
    tmp_path, monkeypatch
):
    """`auto` with systemctl on PATH, the unit installed, and this process not under it."""
    monkeypatch.setattr("jarvis.restart.service.runs_under", no)
    monkeypatch.setattr("jarvis.restart.service.is_installed", yes)
    harness = Harness(make_settings(tmp_path, service_manager="auto"))

    result = await harness.coordinator.request()
    await harness.settle()

    assert result["status"] == "unsupported"
    assert result["message"].rstrip(".").count(". ") == 0  # one sentence to say
    assert harness.record() is None
    assert harness.spawn.commands == []


async def test_the_model_is_warned_when_it_cannot_ring_back(tmp_path):
    harness = Harness(make_settings(tmp_path), twilio=FakeTwilioOut(configured=False))

    result = await harness.coordinator.request()

    assert result["status"] == "restarting"
    assert "cannot ring them back" in result["message"]


async def test_a_restart_command_that_fails_is_recorded_and_texted(tmp_path):
    harness = Harness(make_settings(tmp_path), spawn=FakeSpawn(code=1))

    await harness.coordinator.request()
    await harness.settle()

    record = harness.record()
    assert record.state == "failed"
    assert "exited 1" in record.error
    assert harness.twilio.sms and "did not go through" in harness.twilio.sms[0][1]


async def test_a_restart_command_that_is_missing_is_recorded(tmp_path):
    spawn = FakeSpawn(error=FileNotFoundError("systemctl"))
    harness = Harness(make_settings(tmp_path), spawn=spawn)

    await harness.coordinator.request()
    await harness.settle()

    assert harness.record().state == "failed"
    assert "FileNotFoundError" in harness.record().error


async def test_being_killed_mid_handover_is_the_restart_working_not_failing(tmp_path):
    """`systemctl` sits in the cgroup the restart tears down, so it dies with us.

    Read as a failure it wrote `systemctl exited -15` onto a service that had in fact come
    back perfectly well — and then went quiet, because the process that would have said so
    was the one being killed. Seen on the live box on 2026-08-26.
    """
    harness = Harness(make_settings(tmp_path), spawn=FakeSpawn(code=-15))

    await harness.coordinator.request(reason="new code")
    await harness.settle()

    record = harness.record()
    assert record is not None
    assert record.state == "pending"  # still for the far side to confirm
    assert not harness.twilio.sms


async def test_a_restart_command_that_really_fails_is_still_a_failure(tmp_path):
    harness = Harness(make_settings(tmp_path), spawn=FakeSpawn(code=1))

    await harness.coordinator.request(reason="new code")
    await harness.settle()

    record = harness.record()
    assert record is not None and record.state == "failed"
    assert "exited 1" in record.error


async def test_a_restart_that_does_nothing_is_not_left_silent(tmp_path):
    """The command returned 0 and we are still here: nothing restarted, so say so."""
    harness = Harness(make_settings(tmp_path), sleep=FakeSleep(dies=False))

    await harness.coordinator.request()
    await harness.settle()

    assert harness.record().state == "failed"
    assert "nothing happened" in harness.record().error


async def test_a_failed_restart_is_told_to_whoever_is_on_the_line(tmp_path):
    harness = Harness(make_settings(tmp_path), spawn=FakeSpawn(code=1), sleep=FakeSleep())
    session = FakeVoiceSession(channel="phone")

    await harness.coordinator.request(force=True)
    harness.sessions.add(session)
    await harness.settle()

    assert any("did not go through" in text for text in session.announced)
    assert harness.twilio.sms == []  # said out loud, so not texted as well


# --- confirming it afterwards ----------------------------------------------


async def ready() -> bool:
    return True


async def not_ready() -> bool:
    return False


async def test_no_record_means_no_call(harness):
    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls == []
    assert harness.twilio.sms == []


async def test_a_finished_restart_calls_back(harness):
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert len(harness.twilio.calls) == 1
    assert harness.twilio.calls[0]["to"] == OWNER
    assert harness.twilio.calls[0]["status_callback"] == f"https://{HOST}/twilio/status"


async def test_the_call_carries_a_redeemable_token_and_the_status(harness):
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    info = harness.tokens.redeem(parameters["token"])
    assert info is not None and info.caller == OWNER
    context = info.extra["opening_context"]
    assert "restarted" in context
    assert "picked up new code" in context  # the reason they gave, read back to them
    assert "back up after" in context
    assert "phone listening" in context


async def test_the_summary_says_what_changed(tmp_path):
    harness = Harness(make_settings(tmp_path))
    record = pending(version="v0-old0000")

    summary = await harness.coordinator.status_summary(record, phone_up=True, wakeword=True)

    assert f"now on {VERSION}, was v0-old0000" in summary
    assert "phone and wake word listening" in summary
    assert "no tasks were lost" in summary


async def test_the_summary_reports_what_went_wrong_since_the_restart(tmp_path):
    """"Back up" is not "working": the logs are the only place the difference is written."""
    settings = make_settings(tmp_path)
    write_log(settings, "jarvis.log", "2026-08-26 INFO    jarvis: from an earlier life\n")
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))
    write_log(settings, "jarvis.err.log", TRACEBACK)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "but 1 error in the log since" in summary
    assert "ModuleNotFoundError" in summary


async def test_a_clean_log_says_nothing_about_errors(tmp_path):
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))
    write_log(settings, "jarvis.log", "2026-08-26 INFO    jarvis: serving\n")

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "error" not in summary


async def test_errors_are_said_before_the_housekeeping(tmp_path):
    """The one question a restart has to answer is "did it work" — it cannot be buried."""
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))
    write_log(settings, "jarvis.err.log", TRACEBACK)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert summary.index("error in the log") < summary.index("listening")


async def test_a_restart_that_loaded_nothing_says_so(tmp_path):
    """Same checkout after a restart asked for to load a change: the change is not there."""
    harness = Harness(make_settings(tmp_path))
    record = pending(version=VERSION, task_id=7)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "the checkout did not change" in summary
    assert "task 7" in summary


async def test_an_unchanged_checkout_is_only_worth_saying_when_a_task_was_loading(tmp_path):
    """They restart to clear a wedged process too, and that one is meant to change nothing."""
    harness = Harness(make_settings(tmp_path))

    summary = await harness.coordinator.status_summary(pending(version=VERSION), phone_up=True)

    assert "did not change" not in summary
    assert f"still on {VERSION}" in summary


# --- what was running, versus what is on the disk now -----------------------


def test_the_stamp_records_what_this_process_imported(tmp_path):
    settings = make_settings(tmp_path)

    assert mark_running(settings.data_dir) == VERSION
    assert running_version(settings.data_dir) == VERSION


def test_without_a_stamp_the_checkout_is_the_best_guess(tmp_path):
    """A service too old to stamp anything: one wrong comparison beats no answer at all."""
    settings = make_settings(tmp_path)

    assert running_version(settings.data_dir) is None
    assert loaded_version(settings.data_dir) == VERSION


def test_the_stamp_beats_a_checkout_that_has_moved_on(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    mark_running(settings.data_dir)
    monkeypatch.setattr("jarvis.restart.version.current_version", lambda repo=None: "v2-newer00")

    assert loaded_version(settings.data_dir) == VERSION


async def test_a_commit_made_before_the_restart_still_counts_as_loaded(tmp_path, monkeypatch):
    """The regression: edit, commit, ask for the restart — the normal order.

    Reading the checkout at request time read the commit that had *already landed*, so the
    "before" and the "after" were the same string and every such restart reported that it
    had loaded nothing. The stamp is taken at process start, where the two still differ.
    """
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    mark_running(settings.data_dir)  # the process that is about to be restarted
    monkeypatch.setattr("jarvis.restart.version.current_version", lambda repo=None: "v2-newer00")

    await harness.coordinator.request(reason="new code", task_id=7)
    await harness.settle()
    record = harness.record()
    assert record.version == VERSION  # what was running, not the commit that just landed

    mark_running(settings.data_dir)  # the process that comes back
    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert f"now on v2-newer00, was {VERSION}" in summary
    assert "did not change" not in summary


async def test_the_call_back_names_the_task_the_restart_loaded(harness):
    harness.store.save(pending(task_id=12))

    await harness.coordinator.resume(wait_ready=lambda: ready())

    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    info = harness.tokens.redeem(parameters["token"])
    assert info is not None
    assert "task 12" in info.extra["opening_context"]
    # So `mark_reported` may stamp it before the PIN: the call opened with its result.
    assert info.extra["task_id"] == 12


async def test_a_restart_records_where_the_logs_had_got_to(tmp_path):
    """Without the mark, the process that comes back cannot tell new errors from old."""
    settings = make_settings(tmp_path)
    write_log(settings, "jarvis.log", "2026-08-26 ERROR   jarvis: yesterday's problem\n")
    harness = Harness(settings)

    await harness.coordinator.request(reason="new code", task_id=5)
    await harness.settle()

    record = harness.record()
    assert record is not None
    assert record.task_id == 5
    assert record.log_marks["jarvis.log"] > 0
    # And the error that was already there is not attributed to this restart.
    assert "error" not in await harness.coordinator.status_summary(record, phone_up=True)


async def test_the_summary_counts_what_the_restart_interrupted(tmp_path):
    """Nothing resumes a running task across a restart, so the call says so plainly."""
    store = TaskStore(":memory:")
    for text, status in (("a", TaskStatus.RUNNING), ("b", TaskStatus.QUEUED)):
        await store.create(Task(id=None, kind=TaskKind.AGENT, description=text, status=status))
    harness = Harness(make_settings(tmp_path), tasks=store)

    summary = await harness.coordinator.status_summary(pending(), phone_up=True)

    assert "1 task(s) were interrupted and will not resume" in summary
    # Queued means it never started, so it is picked back up rather than mourned.
    assert "1 task(s) never started and are being picked back up" in summary
    await store.close()


async def test_the_record_is_cleared_once_the_call_is_placed(harness):
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.record() is None


async def test_somebody_already_talking_is_told_not_rung(harness):
    """The confirmation must never interrupt a call — it is said into the one in progress."""
    session = FakeVoiceSession(channel="phone")
    harness.sessions.add(session)
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls == []
    assert any("restart is done" in text for text in session.announced)
    assert harness.record() is None


async def test_a_busy_line_that_cannot_hear_it_gets_a_text(tmp_path):
    """A session too far gone to speak into is still a session: text, never ring."""
    harness = Harness(make_settings(tmp_path))
    harness.sessions.add(FakeVoiceSession(channel="phone", accepts=False))
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls == []
    assert harness.twilio.sms and "back up" in harness.twilio.sms[0][1]
    assert harness.record() is None


async def test_without_the_phone_server_it_texts_instead(harness):
    """An outbound call is answered by our own media stream; with none, it would ring out."""
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: not_ready())

    assert harness.twilio.calls == []
    to, body = harness.twilio.sms[0]
    assert to == OWNER
    assert "phone server is not listening" in body


async def test_a_call_that_will_not_place_falls_back_to_a_text(harness):
    harness.twilio.call_error = RuntimeError("twilio is down")
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.sms and "twilio is down" in harness.twilio.sms[0][1]
    assert harness.record() is None


async def test_a_confirmation_that_cannot_be_delivered_at_all_is_kept(harness):
    """Nothing got through: the record stays, marked, for `jarvis restart --status`."""
    harness.twilio.call_error = RuntimeError("twilio is down")
    harness.twilio.sms_error = RuntimeError("twilio is still down")
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    record = harness.record()
    assert record.state == "failed"
    assert "could not confirm the restart" in record.error


async def test_a_failed_record_is_not_retried(harness):
    harness.store.save(pending(state="failed", error="whatever"))

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls == []
    assert harness.twilio.sms == []


async def test_a_crash_loop_rings_once_not_once_per_crash(harness, monkeypatch):
    """A service that dies before it can dial leaves the count behind; the cap stops it."""
    harness.store.save(pending())
    monkeypatch.setattr(RestartCoordinator, "_deliver", _nothing)
    for _ in range(MAX_CALLBACK_ATTEMPTS):
        await harness.coordinator.resume(wait_ready=lambda: ready())
    assert harness.record().attempts == MAX_CALLBACK_ATTEMPTS

    monkeypatch.undo()
    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls == []
    assert harness.twilio.sms  # the text is what is left when the phone cannot be trusted
    assert harness.record().state == "failed"
    assert "restarted repeatedly" in harness.record().error


async def _nothing(*args, **kwargs) -> None:
    """A delivery that never happens: the process died between the count and the dial."""


async def test_the_attempt_is_counted_before_the_call(harness):
    """A process that dies mid-dial must come back to a record that shows the attempt."""
    harness.store.save(pending())

    class Exploding(FakeTwilioOut):
        async def place_call(self, to, *, twiml, status_callback=None):
            assert RestartStore(harness.store.path).load().attempts == 1
            raise RuntimeError("died mid-dial")

    harness.coordinator._twilio = Exploding()
    await harness.coordinator.resume(wait_ready=lambda: ready())


async def test_resume_never_raises(harness, monkeypatch):
    """Whatever is broken about the machine, `serve` must still come up."""
    monkeypatch.setattr(
        RestartCoordinator, "status_summary", _boom
    )
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())


async def _boom(*args, **kwargs):
    raise RuntimeError("everything is on fire")


# --- odds and ends ---------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.2, "1 second"), (8.0, "8 seconds"), (95.0, "2 minutes"), (None, "an unknown time")],
)
def test_durations_are_spoken_not_printed(seconds, expected):
    assert format_duration(seconds) == expected


def test_health_probe_reads_the_live_session_count(monkeypatch):
    payload = b'{"ok": true, "live_sessions": 2}'
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(payload))

    assert health_probe(Settings(_env_file=None, openai_api_key="t")) == 2


def test_health_probe_of_a_service_that_is_not_running(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)

    assert health_probe(Settings(_env_file=None, openai_api_key="t")) is None


def _fake_urlopen(payload: bytes):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    return lambda *args, **kwargs: Response()


async def test_wait_until_serving_gives_up(monkeypatch):
    class NeverStarts:
        started = False

    ticks = iter([0.0, 1.0, 2.0])

    assert not await wait_until_serving(
        NeverStarts(), timeout=1.0, poll_s=0, clock=lambda: next(ticks)
    )


async def test_wait_until_serving_returns_once_it_is_up():
    class Started:
        started = True

    assert await wait_until_serving(Started(), timeout=1.0, poll_s=0)


# --- the watchdog that outlives the restart --------------------------------


def test_the_watchdog_is_handed_to_systemd_so_the_cgroup_kill_cannot_take_it(tmp_path):
    """A process we merely fork is inside the unit being restarted, and dies with it."""
    settings = make_settings(tmp_path)
    target = ServiceTarget("systemd", "jarvis.service")

    plan = watch_command(settings, target, which=lambda name: f"/usr/bin/{name}", pid=99)

    assert plan is not None
    assert plan.argv[0] == "/usr/bin/systemd-run"
    assert "--user" in plan.argv
    assert f"--unit={WATCH_UNIT_PREFIX}-99" in plan.argv
    assert plan.label == f"{WATCH_UNIT_PREFIX}-99"
    assert not plan.redirect  # systemd points its output at the log for us


def test_the_watchdog_runs_this_interpreter_not_whatever_is_on_path(tmp_path):
    """`systemd-run` starts with the user manager's environment, which has no venv in it."""
    plan = watch_command(
        make_settings(tmp_path),
        ServiceTarget("systemd", "jarvis.service"),
        which=lambda name: f"/usr/bin/{name}",
    )

    assert plan is not None
    assert plan.argv[-4:] == [sys.executable, "-m", "jarvis", "restart-watch"]


def test_the_watchdogs_own_output_goes_somewhere_a_person_can_find(tmp_path):
    settings = make_settings(tmp_path)
    plan = watch_command(
        settings, ServiceTarget("systemd", "jarvis.service"), which=lambda name: name
    )

    assert plan is not None
    assert any("StandardError=append:" in argument for argument in plan.argv)
    assert all("jarvis.log" not in argument for argument in plan.argv)  # not our own log


def test_launchd_needs_no_help_escaping(tmp_path):
    """There is no cgroup to get out of; a new session outlives `launchctl kickstart -k`."""
    plan = watch_command(make_settings(tmp_path), ServiceTarget("launchd", LAUNCHD_LABEL))

    assert plan is not None
    assert plan.argv == [sys.executable, "-m", "jarvis", "restart-watch"]
    assert plan.redirect  # nobody else will point its output anywhere


def test_without_systemd_run_no_watchdog_is_claimed(tmp_path):
    """Starting one that will be killed with us is worse than saying it cannot be done."""
    plan = watch_command(
        make_settings(tmp_path), ServiceTarget("systemd", "jarvis.service"), which=lambda _: None
    )

    assert plan is None


async def test_the_watchdog_is_armed_before_the_restart_is_handed_over(tmp_path):
    """Armed after would mean the window it exists to watch had already opened."""
    order: list[str] = []

    def note_watch(plan, settings):
        order.append("watch")
        return 4321

    async def note_spawn(command):
        order.append("restart")
        return 0

    harness = Harness(make_settings(tmp_path), watch=note_watch)
    harness.coordinator._spawn = note_spawn

    await harness.coordinator.request(reason="new code")
    await harness.settle()

    assert order == ["watch", "restart"]


async def test_the_record_says_how_the_watch_was_armed(tmp_path):
    harness = Harness(make_settings(tmp_path))

    await harness.coordinator.request(reason="new code")
    await harness.settle()

    record = harness.record()
    assert record is not None
    assert "4321" in record.watchdog  # the pid, so a person can go and look at it


async def test_a_watchdog_that_will_not_start_is_recorded_and_the_restart_goes_on(tmp_path):
    """A restart they asked for must not be held hostage by the thing that watches it."""
    harness = Harness(
        make_settings(tmp_path), watch=FakeWatchSpawn(error=OSError("no fork for you"))
    )

    await harness.coordinator.request(reason="new code")
    await harness.settle()

    record = harness.record()
    assert record is not None
    assert "not started" in record.watchdog
    assert "OSError" in record.watchdog
    assert harness.spawn.commands == [["systemctl", "--user", "restart", "jarvis.service"]]


async def test_a_deferred_restart_arms_its_watch_when_it_actually_restarts(tmp_path):
    """A watch counting down from half an hour ago would give up before the restart."""
    harness = Harness(make_settings(tmp_path))
    session = FakeVoiceSession(channel="phone")
    harness.sessions.add(session)

    result = await harness.coordinator.request(reason="new code")

    assert result["status"] == "deferred"
    assert harness.record().watchdog == ""  # nothing armed while the call is still up

    harness.sessions.remove(session)
    await harness.settle()

    assert "4321" in harness.record().watchdog


# --- a restart waits for the work, not only for the line --------------------


async def running_task(store: TaskStore, status=TaskStatus.RUNNING) -> Task:
    task = Task(id=None, kind=TaskKind.AGENT, description="edit jarvis", status=status)
    return await store.create(task)


async def test_a_restart_waits_for_a_running_task_even_with_the_line_clear(tmp_path):
    """A restart kills every subagent it finds, and nothing resumes them.

    The old wait watched only for a live session, which made the moment a call ends — when
    the memory update is dispatched — the most dangerous moment to restart in. A
    hand-rolled version of this came within two minutes of killing task 9 on 2026-08-24.
    """
    store = TaskStore(":memory:")
    await running_task(store)
    harness = Harness(make_settings(tmp_path), tasks=store)

    result = await harness.coordinator.request(reason="new code")

    assert result["status"] == "deferred"
    assert harness.spawn.commands == []
    await harness.coordinator.shutdown()
    await store.close()


async def test_the_restart_goes_ahead_once_the_task_finishes(tmp_path):
    store = TaskStore(":memory:")
    task = await running_task(store)
    harness = Harness(make_settings(tmp_path), tasks=store)

    result = await harness.coordinator.request(reason="new code")
    assert result["status"] == "deferred"

    await store.update(task.id, status=TaskStatus.DONE)
    await harness.settle()

    assert harness.spawn.commands == [["systemctl", "--user", "restart", "jarvis.service"]]
    await store.close()


async def test_a_queued_task_does_not_hold_a_restart_off(tmp_path):
    """Queued work has not started, so the restart costs it nothing but its place in line."""
    store = TaskStore(":memory:")
    await running_task(store, status=TaskStatus.QUEUED)
    harness = Harness(make_settings(tmp_path), tasks=store)

    result = await harness.coordinator.request(reason="new code")

    assert result["status"] == "restarting"
    await store.close()


async def test_force_still_restarts_over_a_running_task(tmp_path):
    store = TaskStore(":memory:")
    await running_task(store)
    harness = Harness(make_settings(tmp_path), tasks=store)

    result = await harness.coordinator.request(reason="new code", force=True)

    assert result["status"] == "restarting"
    await store.close()


async def test_a_task_store_that_will_not_answer_does_not_wedge_the_restart(tmp_path):
    """A failing query must not be the thing that keeps them off the air."""

    class BrokenStore:
        async def list(self, **kwargs):
            raise RuntimeError("the database is gone")

    harness = Harness(make_settings(tmp_path), tasks=BrokenStore())

    result = await harness.coordinator.request(reason="new code")

    assert result["status"] == "restarting"


async def test_the_confirmation_is_not_held_up_by_the_task_queue(tmp_path):
    """The call-back's idea of quiet is only the line; work running is not its business."""
    store = TaskStore(":memory:")
    await running_task(store)
    harness = Harness(make_settings(tmp_path), tasks=store)
    harness.store.save(pending())

    await harness.coordinator.resume(wait_ready=lambda: ready())

    assert harness.twilio.calls, "the confirmation waited for a task it had no reason to"
    await store.close()


# --- the confirmation for a restart the work asked for ----------------------


async def test_the_call_back_leads_with_the_work_that_asked_for_the_restart(tmp_path):
    """One call, both halves: what the work came to, and whether it is running."""
    store = TaskStore(":memory:")
    task = await store.create(
        Task(
            id=None,
            kind=TaskKind.AGENT,
            description="add a recall tool so I can ask what we decided",
            status=TaskStatus.DONE,
            summary="I added the recall tool and the tests pass.",
        )
    )
    harness = Harness(make_settings(tmp_path), tasks=store)
    harness.store.save(pending(task_id=task.id))

    await harness.coordinator.resume(wait_ready=lambda: ready())

    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    context = harness.tokens.redeem(parameters["token"]).extra["opening_context"]
    assert "add a recall tool" in context  # what they asked for
    assert "I added the recall tool" in context  # what came back
    assert "back up after" in context  # and whether it is running
    assert f"mark_reported for task {task.id}" in context
    await store.close()


async def test_a_restart_naming_a_task_that_is_gone_still_confirms_itself(tmp_path):
    """A missing row must not cost them the confirmation the restart owes them."""
    store = TaskStore(":memory:")
    harness = Harness(make_settings(tmp_path), tasks=store)
    harness.store.save(pending(task_id=999))

    await harness.coordinator.resume(wait_ready=lambda: ready())

    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    context = harness.tokens.redeem(parameters["token"]).extra["opening_context"]
    assert "restarted" in context
    assert "task 999" in context
    await store.close()


# --- whose errors are they anyway ------------------------------------------


async def test_the_dying_process_last_gasps_are_not_this_restarts_errors(tmp_path):
    """A Python shutting down with a subagent still open prints `Event loop is closed` out
    of `base_subprocess.__del__`. Read as the new process's problem, that put "but 1 error
    in the log since" on the confirmation for a restart that had gone perfectly — and it
    would have said it on every self-edit restart, the one case the check exists for."""
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))

    # The old process dies noisily...
    write_log(settings, "jarvis.err.log", "Traceback (most recent call last):\n")
    write_log(settings, "jarvis.err.log", "RuntimeError: Event loop is closed\n")
    # ...and only then do we start, which is where our own story begins.
    mark_startup_logs(settings.data_dir)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "error" not in summary


async def test_what_the_new_process_logs_is_very_much_its_own(tmp_path):
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))
    mark_startup_logs(settings.data_dir)
    write_log(settings, "jarvis.err.log", TRACEBACK)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "ModuleNotFoundError" in summary


async def test_without_a_startup_mark_the_record_is_still_used(tmp_path):
    """A service too old to have stamped one is no worse off than it was before."""
    settings = make_settings(tmp_path)
    harness = Harness(settings)
    record = pending(log_marks=log_marks(settings.data_dir))
    write_log(settings, "jarvis.err.log", TRACEBACK)

    summary = await harness.coordinator.status_summary(record, phone_up=True)

    assert "ModuleNotFoundError" in summary


# --- spawn_watchdog --------------------------------------------------------


class RecordingPopen:
    """`subprocess.Popen`, recorded rather than run.

    No real child here on purpose: `spawn_watchdog` deliberately drops the `Popen` object
    (the caller is about to be killed, so there must be nothing left to reap), and a test
    that started a real process would be left holding exactly the unreaped handle that
    design implies.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        # Read while the file object is still open — `spawn_watchdog` closes it after.
        stdout = kwargs.get("stdout")
        self.calls[-1]["stdout_name"] = getattr(stdout, "name", None)
        return SimpleNamespace(pid=4321)


def test_spawn_watchdog_starts_a_detached_process_and_returns_its_pid(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    popen = RecordingPopen()
    monkeypatch.setattr("jarvis.restart.service.subprocess.Popen", popen)
    plan = WatchPlan(["/usr/bin/true", "--watch"], "jarvis-restart-watch-1", redirect=False)

    assert spawn_watchdog(plan, settings) == 4321

    (call,) = popen.calls
    assert call["argv"] == ["/usr/bin/true", "--watch"]
    # A new session, or the restart that signals our cgroup takes the watchdog with it.
    assert call["start_new_session"] is True
    # systemd redirects for us, so there is nothing to write to and nothing to close.
    assert call["stdout"] is subprocess.DEVNULL
    assert call["stdout_name"] is None


def test_a_redirected_watchdog_is_pointed_at_its_own_log(tmp_path, monkeypatch):
    """On launchd nothing redirects for us, and a watchdog that fails has no other voice."""
    settings = make_settings(tmp_path)
    popen = RecordingPopen()
    monkeypatch.setattr("jarvis.restart.service.subprocess.Popen", popen)
    plan = WatchPlan(["/usr/bin/true"], "detached", redirect=True)

    spawn_watchdog(plan, settings)

    (call,) = popen.calls
    assert call["stdout_name"] == str(watch_log_path(settings))
    assert call["stderr"] is subprocess.STDOUT
    assert watch_log_path(settings).parent.is_dir()


def test_the_watchdog_log_lives_under_the_data_dir(tmp_path):
    settings = make_settings(tmp_path)

    assert watch_log_path(settings).parent == settings.data_dir / "logs"


# --- the record's failure paths ---------------------------------------------


def test_an_unreadable_record_is_ignored_rather_than_fatal(tmp_path):
    """Losing the breadcrumb must never be the thing that takes the service down."""
    path = tmp_path / "restart.json"
    path.write_text("{not json")

    assert RestartStore(path).load() is None


def test_a_record_that_is_not_an_object_is_ignored(tmp_path):
    path = tmp_path / "restart.json"
    path.write_text('["a list"]')

    assert RestartStore(path).load() is None


def test_a_missing_record_is_simply_none(tmp_path):
    assert RestartStore(tmp_path / "never-written.json").load() is None


def test_a_record_that_cannot_be_written_reports_false(tmp_path):
    store = RestartStore(tmp_path / "nope" / "restart.json")
    (tmp_path / "nope").write_text("this is a file, not a directory")

    assert store.save(RestartRecord(reason="whatever")) is False


def test_clearing_a_record_that_is_not_there_is_fine(tmp_path):
    RestartStore(tmp_path / "never-written.json").clear()  # must not raise


def test_a_record_from_a_newer_build_drops_the_fields_it_does_not_know(tmp_path):
    """The other half of "the database runs ahead of the code"."""
    path = tmp_path / "restart.json"
    path.write_text(json.dumps({"reason": "new code", "invented_later": True}))

    record = RestartStore(path).load()

    assert record is not None
    assert record.reason == "new code"


def test_an_unparseable_timestamp_gives_an_unknown_age():
    assert RestartRecord(requested_at="not a date").age_seconds() is None


# --- the version stamp's failure paths --------------------------------------


def test_a_version_that_cannot_be_stamped_is_still_returned(tmp_path, monkeypatch):
    """Decoration: an unwritable data dir costs one line of the spoken summary, not more."""
    monkeypatch.setattr("jarvis.restart.version.current_version", lambda repo=None: "v1-abc")
    unwritable = tmp_path / "a-file"
    unwritable.write_text("not a directory")

    assert mark_running(unwritable / "data") == "v1-abc"


def test_no_stamp_on_disk_reads_back_as_nothing(tmp_path):
    assert running_version(tmp_path) is None


def test_startup_marks_that_were_never_written_read_back_as_none(tmp_path):
    assert startup_log_marks(tmp_path) is None


def test_startup_marks_that_are_corrupt_read_back_as_none(tmp_path):
    (tmp_path / "startup-log-marks.json").write_text("{not json")

    assert startup_log_marks(tmp_path) is None


def test_startup_marks_of_the_wrong_shape_read_back_as_none(tmp_path):
    (tmp_path / "startup-log-marks.json").write_text('["a list"]')

    assert startup_log_marks(tmp_path) is None


def test_git_that_is_not_there_is_no_version_rather_than_an_error(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("jarvis.restart.version.subprocess.run", no_git)

    assert current_version(tmp_path) is None
