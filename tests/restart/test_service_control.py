"""`restart.service`'s half of `keryx service stop|start`: the commands that keep a unit from
starting and let it start again, where its unit file is, and whether it is stopped — every
service manager answered by a fake `run`."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from keryx.restart import service
from keryx.restart.service import (
    ServiceTarget,
    is_enabled,
    is_installed,
    is_present,
    is_stopped,
    unit_file,
)

LAUNCHD = ServiceTarget("launchd", "dev.keryx.agent")
SYSTEMD = ServiceTarget("systemd", "keryx.service")
UID = os.getuid()


def test_launchd_disables_enables_and_kickstarts_by_its_label():
    assert LAUNCHD.disable_command() == ["launchctl", "disable", f"gui/{UID}/dev.keryx.agent"]
    assert LAUNCHD.enable_command() == ["launchctl", "enable", f"gui/{UID}/dev.keryx.agent"]
    # No `-k`: a job that already runs is left running, so a start is idempotent.
    assert LAUNCHD.kickstart_command() == ["launchctl", "kickstart",
                                           f"gui/{UID}/dev.keryx.agent"]


def test_systemd_disables_and_enables_its_user_unit():
    assert SYSTEMD.disable_command() == ["systemctl", "--user", "disable", "keryx.service"]
    assert SYSTEMD.enable_command() == ["systemctl", "--user", "enable", "keryx.service"]
    assert SYSTEMD.kickstart_command() == SYSTEMD.start_command()


def test_each_unit_file_is_where_its_installer_writes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert unit_file(LAUNCHD) == tmp_path / "Library" / "LaunchAgents" / "dev.keryx.agent.plist"
    assert unit_file(SYSTEMD) == tmp_path / "config" / "systemd" / "user" / "keryx.service"
    assert LAUNCHD.start_command()[-1] == str(unit_file(LAUNCHD))


def answering(**by_verb):
    """A `subprocess.run` that answers each verb (`print`, `is-enabled`, `is-active`) with
    (returncode, stdout), and raises what it is told to."""

    def run(command, **kwargs):
        verb = next(word for word in command if word in by_verb)
        answer = by_verb[verb]
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(returncode=answer[0], stdout=answer[1], stderr="")

    return run


def write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unit\n")


def test_a_launchd_job_on_disk_but_not_loaded_is_stopped(monkeypatch):
    monkeypatch.setattr(service, "is_installed", is_installed)  # the real probe, faked `run`
    write(unit_file(LAUNCHD))

    assert is_stopped(LAUNCHD, run=answering(print=(113, ""))) is True
    assert is_stopped(LAUNCHD, run=answering(print=(0, "state = running"))) is False


def test_a_unit_with_no_file_is_not_installed_not_stopped():
    assert is_stopped(LAUNCHD, run=answering(print=(113, ""))) is False
    assert is_stopped(SYSTEMD, run=answering(**{"is-enabled": (1, "disabled\n")})) is False


@pytest.mark.parametrize(("enabled", "active", "stopped"), [
    ((1, "disabled\n"), (3, "inactive\n"), True),
    ((0, "enabled\n"), (3, "inactive\n"), False),  # merely down: it would start at login
    ((1, "disabled\n"), (0, "active\n"), False),  # disabled while still running
])
def test_a_systemd_unit_is_stopped_when_disabled_and_inactive(enabled, active, stopped):
    write(unit_file(SYSTEMD))

    run = answering(**{"is-enabled": enabled, "is-active": active})

    assert is_stopped(SYSTEMD, run=run) is stopped


def test_a_probe_that_cannot_answer_is_not_stopped():
    write(unit_file(SYSTEMD))

    run = answering(**{"is-enabled": subprocess.TimeoutExpired("systemctl", 5)})

    assert is_stopped(SYSTEMD, run=run) is False


DISABLED_LIST = """disabled services = {
\t"com.apple.example" => enabled
\t"dev.keryx.agent" => disabled
\t"dev.keryx.tunnel" => true
\t"dev.keryx.llm" => false
}
"""


@pytest.mark.parametrize(("label", "enabled"), [
    ("dev.keryx.agent", False), ("dev.keryx.tunnel", False),  # older macOS: `true`
    ("dev.keryx.llm", True), ("dev.keryx.voice", True),  # not listed: the default
])
def test_launchds_disabled_list_says_whether_a_job_is_enabled(label, enabled):
    run = answering(**{"print-disabled": (0, DISABLED_LIST)})

    assert is_enabled(ServiceTarget("launchd", label), run=run) is enabled


@pytest.mark.parametrize(("answer", "enabled"), [
    ((0, "enabled\n"), True), ((1, "disabled\n"), False), ((0, "static\n"), None),
    (subprocess.TimeoutExpired("systemctl", 5), None),
])
def test_systemds_is_enabled_says_it_or_none(answer, enabled):
    assert is_enabled(SYSTEMD, run=answering(**{"is-enabled": answer})) is enabled


def test_a_launchd_list_that_cannot_be_read_says_nothing():
    assert is_enabled(LAUNCHD, run=answering(**{"print-disabled": (1, "")})) is None


def test_present_is_loaded_or_on_disk(monkeypatch):
    monkeypatch.setattr(service, "is_installed", lambda target, **_: target is SYSTEMD)

    assert is_present(SYSTEMD) is True  # loaded
    assert is_present(LAUNCHD) is False
    write(unit_file(LAUNCHD))
    assert is_present(LAUNCHD) is True  # booted out, its plist kept
