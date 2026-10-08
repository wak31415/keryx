"""`restart.service`'s half of `keryx service stop|start`: the commands that keep a unit from
starting and let it start again, where its unit file is, and whether it is stopped — every
service manager answered by a fake `run`."""

import os
import plistlib
import pwd
import subprocess
from pathlib import Path
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


# --- whose units they are ------------------------------------------------------------------


def write_plist(path, **env):
    """A launchd plist as `install-launchd.sh` renders it, with `env` its environment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": path.stem, "EnvironmentVariables": env}))
    return path


def agents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def test_no_unit_file_belongs_to_nobody(tmp_path):
    assert service.foreign_units([LAUNCHD, SYSTEMD]) is None


def test_a_plist_rendered_for_this_home_is_ours(tmp_path):
    write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=os.environ["KERYX_HOME"])

    assert service.foreign_units([LAUNCHD]) is None


def test_a_plist_rendered_for_another_home_is_refused_in_one_sentence(tmp_path):
    plist = write_plist(agents_dir() / "dev.keryx.agent.plist",
                        KERYX_HOME="/Users/owner/.config/keryx")

    foreign = service.foreign_units([LAUNCHD])

    assert foreign is not None and foreign.unit_file == plist
    assert foreign.message == (
        "These units belong to KERYX_HOME=/Users/owner/.config/keryx; this command runs with "
        f"KERYX_HOME={os.environ['KERYX_HOME']}.")
    assert foreign.detail == f"rendered in {plist}"


def test_homes_are_compared_as_real_paths(tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    real.mkdir()
    (tmp_path / "linked-home").symlink_to(real)
    monkeypatch.setenv("KERYX_HOME", str(tmp_path / "linked-home"))
    write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=str(real))

    assert service.foreign_units([LAUNCHD]) is None
    assert service.foreign_units([LAUNCHD], home=tmp_path / "elsewhere") is not None


def test_the_accounts_own_units_are_judged_whatever_home_says(tmp_path, monkeypatch):
    """A sandbox moves `HOME`; launchd still reads the account's LaunchAgents, and the
    label is the same one."""
    account = tmp_path / "account"
    monkeypatch.setattr(service, "account_home", lambda: account)
    plist = write_plist(account / "Library" / "LaunchAgents" / "dev.keryx.agent.plist",
                        KERYX_HOME=str(account / ".config" / "keryx"))

    foreign = service.foreign_units([LAUNCHD])

    assert foreign is not None and foreign.unit_file == plist
    assert service.unit_files(LAUNCHD) == [unit_file(LAUNCHD), plist]


def test_a_systemd_unit_is_read_as_systemd_unquotes_it(tmp_path, monkeypatch):
    home = tmp_path / 'odd "dir"\\100%'
    monkeypatch.setenv("KERYX_HOME", str(home))
    path = unit_file(SYSTEMD)
    path.parent.mkdir(parents=True)
    quoted = str(home).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    path.write_text("[Service]\n"
                    'Environment="PATH=/usr/bin"\n'
                    f'Environment="KERYX_HOME={quoted}"\n'
                    "Environment=broken\"quote\n"
                    "Environment=NO_VALUE\n")

    assert service.rendered_env(path, "systemd")["KERYX_HOME"] == str(home)
    assert service.foreign_units([SYSTEMD]) is None
    # The account's default location, when XDG_CONFIG_HOME moved the installer's.
    assert service.unit_files(SYSTEMD)[-1] == (
        service.account_home() / ".config" / "systemd" / "user" / "keryx.service")


def test_a_unit_that_renders_no_home_belongs_to_the_default_one(tmp_path, monkeypatch):
    """A template from before KERYX_HOME was rendered: its service ran with the default."""
    write_plist(agents_dir() / "dev.keryx.agent.plist", PATH="/usr/bin")
    default = service.account_home() / ".config" / "keryx"

    assert service.foreign_units([LAUNCHD]).theirs == default
    monkeypatch.setenv("KERYX_HOME", str(default))
    assert service.foreign_units([LAUNCHD]) is None


def test_a_rendered_xdg_config_home_says_where_the_default_home_was(tmp_path):
    write_plist(agents_dir() / "dev.keryx.agent.plist", XDG_CONFIG_HOME="/srv/config")

    assert service.foreign_units([LAUNCHD]).theirs.as_posix() == "/srv/config/keryx"


@pytest.mark.parametrize("content", [
    b"not a plist", b"<plist><array/></plist>",
    plistlib.dumps({"EnvironmentVariables": "KERYX_HOME=/x"}),
])
def test_a_plist_that_says_nothing_belongs_to_the_default_home(content, tmp_path):
    path = agents_dir() / "dev.keryx.agent.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    assert service.rendered_env(path, "launchd") == {}
    assert service.foreign_units([LAUNCHD]).theirs == service.account_home() / ".config" / "keryx"


def test_an_unreadable_unit_file_belongs_to_the_default_home(tmp_path, monkeypatch):
    path = write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME="/x")
    monkeypatch.setattr(type(path), "read_bytes", lambda self: (_ for _ in ()).throw(
        PermissionError(13, "denied")))

    assert service.rendered_env(path, "launchd") == {}


def test_the_account_home_comes_from_the_user_database(monkeypatch):
    """Read here and nowhere else in the suite: the conftest stands in for it everywhere."""
    monkeypatch.undo()  # the suite's stand-in, and the rest of its isolation, for this read
    assert service.account_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    assert service.account_home() == Path.home()
