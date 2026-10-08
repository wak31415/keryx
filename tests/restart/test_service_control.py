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
from wording import for_the_app

from keryx.restart import service
from keryx.restart.service import (
    ServiceTarget,
    is_enabled,
    is_installed,
    is_present,
    is_stopped,
    manager_owners,
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

OWNER = "/Users/owner/.config/keryx"


def write_plist(path, **env):
    """A launchd plist as `install-launchd.sh` renders it, with `env` its environment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": path.stem, "EnvironmentVariables": env}))
    return path


def agents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def ours():
    return os.environ["KERYX_HOME"]


def test_no_unit_file_belongs_to_nobody(tmp_path):
    assert service.foreign_units([LAUNCHD, SYSTEMD]) is None


def test_a_plist_rendered_for_this_home_is_ours(tmp_path):
    write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=ours())

    assert service.foreign_units([LAUNCHD]) is None


def test_a_plist_rendered_for_another_home_is_refused_in_one_sentence(tmp_path):
    """A sentence for a person, with no path in it; both homes and the file in the detail."""
    plist = write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=OWNER)

    foreign = service.foreign_units([LAUNCHD])

    assert foreign is not None and foreign.unit_file == str(plist)
    assert foreign.message == ("Keryx's background services here belong to another Keryx "
                               "settings folder, so this command leaves them alone.")
    assert for_the_app(foreign.message)
    assert foreign.detail == (
        f"The units were rendered for KERYX_HOME={OWNER} (in {plist}); "
        f"this command runs with KERYX_HOME={ours()}.")


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

    assert foreign is not None and foreign.unit_file == str(plist)
    assert service.unit_files(LAUNCHD) == [unit_file(LAUNCHD), plist]


def test_a_systemd_unit_is_read_as_systemd_unquotes_it(tmp_path, monkeypatch):
    home = tmp_path / 'odd "dir"\\100%'
    monkeypatch.setenv("KERYX_HOME", str(home))
    path = unit_file(SYSTEMD)
    path.parent.mkdir(parents=True)
    quoted = str(home).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    path.write_text("# KERYX_HOME, in a comment, says nothing\n[Service]\n"
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
    agents_dir().joinpath("dev.keryx.agent.plist").write_bytes(plistlib.dumps({"Label": "x"}))
    assert service.foreign_units([LAUNCHD]) is None  # no EnvironmentVariables at all


def test_a_rendered_xdg_config_home_says_where_the_default_home_was(tmp_path):
    write_plist(agents_dir() / "dev.keryx.agent.plist", XDG_CONFIG_HOME="/srv/config")

    assert service.foreign_units([LAUNCHD]).theirs.as_posix() == "/srv/config/keryx"


# Fix round 1: whatever cannot be read is foreign.


@pytest.mark.parametrize("content", [
    b"not a plist", b"<plist><array/></plist>",
    plistlib.dumps({"EnvironmentVariables": "KERYX_HOME=/x"}),
    plistlib.dumps({"EnvironmentVariables": {"KERYX_HOME": 7}}),
    plistlib.dumps({"EnvironmentVariables": {"KERYX_HOME": "  "}}),
])
def test_a_plist_that_cannot_be_read_is_refused(content, tmp_path):
    path = agents_dir() / "dev.keryx.agent.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    foreign = service.foreign_units([LAUNCHD], home=service.account_home() / ".config" / "keryx")

    assert foreign is not None and foreign.theirs is None
    assert foreign.message == service.FOREIGN_UNITS
    assert foreign.detail == f"could not read {path}"


def test_an_unreadable_unit_file_is_refused(tmp_path, monkeypatch):
    path = write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=ours())
    monkeypatch.setattr(type(path), "read_bytes", lambda self: (_ for _ in ()).throw(
        PermissionError(13, "denied")))

    assert service.foreign_units([LAUNCHD]).detail == f"could not read {path}"


def test_a_unit_file_that_cannot_even_be_looked_at_is_refused(tmp_path, monkeypatch):
    """`Path.is_file` raises for EACCES on a directory on the way: refused, never raised."""
    monkeypatch.setattr(type(Path()), "is_file", lambda self: (_ for _ in ()).throw(
        PermissionError(13, "denied")))

    foreign = service.foreign_units([LAUNCHD])

    assert foreign is not None and foreign.detail.startswith("could not read ")


def test_a_systemd_unit_naming_keryx_home_with_no_value_to_read_is_refused(tmp_path):
    path = unit_file(SYSTEMD)
    path.parent.mkdir(parents=True)
    path.write_text('[Service]\nEnvironment="KERYX_HOME=/never closed\n')

    assert service.foreign_units([SYSTEMD]).detail == f"could not read {path}"


@pytest.mark.parametrize(("rendered", "foreign"), [
    ("~/.config/keryx", False), ("~", True), ("~someone/keryx", True), ("relative/keryx", True),
])
def test_a_rendered_tilde_is_the_accounts_home_and_a_relative_home_is_nobodys(
    rendered, foreign, tmp_path, monkeypatch
):
    monkeypatch.setenv("KERYX_HOME", str(service.account_home() / ".config" / "keryx"))
    write_plist(agents_dir() / "dev.keryx.agent.plist", KERYX_HOME=rendered)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))  # never what `~` means here

    assert (service.foreign_units([LAUNCHD]) is not None) is foreign


# Fix round 1: what the service manager has loaded, asked of it read-only.


def answers(code, stdout="", raises=None):
    """A `subprocess.run` for the probe: what it was asked, and one answer."""
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        assert kwargs["timeout"] == service.PROBE_TIMEOUT_S
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    run.asked = asked
    return run


LAUNCHCTL_PRINT = """gui/501/dev.keryx.agent = {
	active count = 1
	path = {path}
	type = LaunchAgent
	state = running

	inherited environment = {
		KERYX_HOME => /inherited/not/the/jobs
	}

	default environment = {
		PATH => /usr/bin:/bin
	}

	environment = {
		XPC_SERVICE_NAME => dev.keryx.agent
{home}	}
}
"""


def launchctl_print(path="/gone/dev.keryx.agent.plist", home=OWNER):
    line = f"\t\tKERYX_HOME => {home}\n" if home else ""
    return LAUNCHCTL_PRINT.replace("{path}", path).replace("{home}", line)


def test_launchd_is_asked_what_it_has_loaded_and_its_environment_judged():
    run = answers(0, launchctl_print())

    [owner] = manager_owners(LAUNCHD, run=run)

    assert owner == service.Owner(Path(OWNER), "/gone/dev.keryx.agent.plist")
    assert run.asked == [["launchctl", "print", f"gui/{UID}/dev.keryx.agent"]]


def test_a_loaded_job_without_keryx_home_in_its_environment_is_judged_by_its_file(tmp_path):
    plist = write_plist(tmp_path / "loaded.plist", KERYX_HOME=ours())

    [owner] = manager_owners(LAUNCHD, run=answers(0, launchctl_print(str(plist), home=None)))
    assert owner == service.Owner(Path(ours()), str(plist))
    # Its file gone (an uninstall cut short): whose it is cannot be told.
    [gone] = manager_owners(LAUNCHD, run=answers(0, launchctl_print(home=None)))
    assert gone == service.Owner(None, "/gone/dev.keryx.agent.plist")
    # A file that does not parse, or no path at all.
    (tmp_path / "bad.plist").write_text("nope")
    assert manager_owners(LAUNCHD, run=answers(0, launchctl_print(
        str(tmp_path / "bad.plist"), home=None)))[0].home is None
    assert manager_owners(LAUNCHD, run=answers(0, "gui/501/x = {\n}\n"))[0].home is None
    (tmp_path / "blank.plist").write_bytes(plistlib.dumps(
        {"EnvironmentVariables": {"PATH": "/usr/bin"}}))
    assert manager_owners(LAUNCHD, run=answers(0, launchctl_print(
        str(tmp_path / "blank.plist"), home=None)))[0].home == (
        service.account_home() / ".config" / "keryx")
    assert manager_owners(LAUNCHD, run=answers(0, launchctl_print(home=" ")))[0].home is None


def test_launchd_with_nothing_loaded_or_no_launchctl_says_nothing():
    assert manager_owners(LAUNCHD, run=answers(service.LAUNCHD_NOT_LOADED)) == []
    assert manager_owners(LAUNCHD, run=answers(0, raises=FileNotFoundError("launchctl"))) == []


@pytest.mark.parametrize("run", [
    answers(5, "Bad request"),
    answers(0, raises=subprocess.TimeoutExpired("launchctl", 5)),
    answers(0, raises=PermissionError(13, "denied")),
])
def test_a_manager_that_cannot_be_asked_is_refused(run):
    [owner] = manager_owners(LAUNCHD, run=run)

    assert owner == service.Owner(None, "launchd's answer about dev.keryx.agent")


def test_systemd_is_asked_for_the_units_environment_drop_ins_included():
    run = answers(0, "LoadState=loaded\nFragmentPath=/srv/xdg/systemd/user/keryx.service\n"
                     f'Environment=PATH=/usr/bin "KERYX_HOME={OWNER}"\n')

    [owner] = manager_owners(SYSTEMD, run=run)

    assert owner == service.Owner(Path(OWNER), "/srv/xdg/systemd/user/keryx.service")
    assert run.asked == [["systemctl", "--user", "show", "-p", "LoadState", "-p",
                          "FragmentPath", "-p", "Environment", "keryx.service"]]


@pytest.mark.parametrize(("stdout", "code", "expected"), [
    ("LoadState=not-found\nFragmentPath=\nEnvironment=\n", 0, []),
    ("LoadState=loaded\nFragmentPath=\nEnvironment=\n", 0,
     "default"),  # an old template: the default home's
    ('LoadState=loaded\nFragmentPath=/u\nEnvironment="KERYX_HOME=/a\n', 0, None),
    ("LoadState=loaded\nFragmentPath=/u\nEnvironment=KERYX_HOME=\n", 0, None),
    ("", 1, None),  # no user bus
])
def test_what_systemd_says_is_judged(stdout, code, expected):
    owners = manager_owners(SYSTEMD, run=answers(code, stdout))

    if expected == []:
        assert owners == []
    elif expected == "default":
        assert owners == [service.Owner(service.account_home() / ".config" / "keryx",
                                        "systemd's answer about keryx.service")]
    else:
        assert len(owners) == 1 and owners[0].home is None


def test_a_loaded_unit_whose_file_moved_out_of_reach_is_still_refused(tmp_path, monkeypatch):
    """The finding: a sandbox that moves XDG_CONFIG_HOME finds no file, and systemd still
    has the owner's unit loaded from elsewhere."""
    run = answers(0, "LoadState=loaded\nFragmentPath=/real/xdg/systemd/user/keryx.service\n"
                     f"Environment=KERYX_HOME={OWNER}\n")
    real = manager_owners
    monkeypatch.setattr(service, "manager_owners", lambda target, **_: real(target, run=run))

    foreign = service.foreign_units([SYSTEMD])

    assert foreign is not None and foreign.unit_file == "/real/xdg/systemd/user/keryx.service"
    assert foreign.theirs == Path(OWNER)


def test_the_account_home_comes_from_the_user_database(monkeypatch):
    """Read here and nowhere else in the suite: the conftest stands in for it everywhere."""
    monkeypatch.undo()  # the suite's stand-in, and the rest of its isolation, for this read
    assert service.account_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    assert service.account_home() == Path.home()
