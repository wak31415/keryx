"""`keryx service`: the four units as the service manager finds them, and installing or
removing them through the checkout's installer — every manager and script faked."""

import pytest

from keryx import units
from keryx.config import Settings
from keryx.restart.service import ServiceTarget


def checkout(root):
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (root / "scripts" / name).write_text("#!/bin/sh\n")
    return root


# --- what is installed, and running ------------------------------------------------------


def test_launchd_units_are_asked_by_their_labels():
    asked = []

    def installed(target: ServiceTarget) -> bool:
        asked.append(target)
        return target.unit in ("dev.keryx.agent", "dev.keryx.llm", "dev.keryx.tunnel")

    document = units.status(Settings(), platform="darwin", installed=installed,
                            active=lambda target: target.unit != "dev.keryx.tunnel")

    assert document == {"manager": "launchd", "units": {
        "agent": {"installed": True, "active": True},
        "tunnel": {"installed": True, "active": False},
        "llm": {"installed": True, "active": True},
        "voice": {"installed": False, "active": False},
    }}
    assert {target.manager for target in asked} == {"launchd"}
    assert [target.unit for target in asked] == [
        "dev.keryx.agent", "dev.keryx.tunnel", "dev.keryx.llm", "dev.keryx.voice"]


def test_systemd_units_and_a_renamed_agent_unit():
    asked = []

    document = units.status(Settings(service_unit="mine.service"), platform="linux",
                            installed=lambda target: asked.append(target) or False,
                            active=lambda target: pytest.fail("not installed: not asked"))

    assert document["manager"] == "systemd"
    assert [target.unit for target in asked] == [
        "mine.service", "cloudflared.service", "keryx-llm.service", "keryx-voice.service"]
    assert all(unit == {"installed": False, "active": False}
               for unit in document["units"].values())


@pytest.mark.parametrize(("manager", "platform", "expected"), [
    ("auto", "freebsd", None), ("none", "darwin", None), ("systemd", "darwin", "systemd"),
])
def test_no_service_manager_is_null_and_asks_nothing(manager, platform, expected):
    document = units.status(Settings(service_manager=manager), platform=platform,
                            installed=lambda target: expected is not None,
                            active=lambda target: True)

    assert document["manager"] == expected
    assert all(unit["installed"] is (expected is not None)
               for unit in document["units"].values())


def test_status_asks_the_restart_modules_own_probes(monkeypatch):
    monkeypatch.setattr("keryx.restart.service.is_installed", lambda target, **_: True)
    monkeypatch.setattr("keryx.restart.service.is_active", lambda target, **_: False)

    document = units.status(Settings(), platform="darwin")

    assert document["units"]["agent"] == {"installed": True, "active": False}


# --- installing and removing them --------------------------------------------------------


class Script:
    def __init__(self, code=0, out="", err="") -> None:
        self.code, self.out, self.err, self.ran = code, out, err, []

    def __call__(self, argv):
        self.ran.append(list(argv))
        return self.code, self.out, self.err


@pytest.mark.parametrize(("llm", "voice", "uninstall", "flags", "said"), [
    (False, False, False, [], "installed dev.keryx.agent, dev.keryx.tunnel"),
    (True, False, False, ["--llm"], "installed dev.keryx.llm"),
    (False, True, True, ["--voice", "--uninstall"], "removed dev.keryx.voice"),
    (True, True, False, ["--llm", "--voice"], "installed dev.keryx.llm, dev.keryx.voice"),
])
def test_the_installer_is_run_with_its_flags(tmp_path, llm, voice, uninstall, flags, said):
    root, script = checkout(tmp_path / "checkout"), Script(out="loaded\n")

    outcome = units.install(llm=llm, voice=voice, uninstall=uninstall, root=root,
                            platform="darwin", run=script)

    assert outcome == units.Outcome(0, said)
    assert script.ran == [[str(root / "scripts" / "install-launchd.sh"), *flags]]


def test_a_failed_install_says_the_scripts_last_error_line(tmp_path):
    root = checkout(tmp_path / "checkout")
    failing = Script(code=1, err="warning: x\nPUBLIC_HOST is not set: keryx setup\n\n")

    outcome = units.install(root=root, platform="linux", run=failing)

    assert outcome == units.Outcome(1, "PUBLIC_HOST is not set: keryx setup")
    silent = units.install(llm=True, uninstall=True, root=root, platform="linux",
                           run=Script(code=5))
    assert silent == units.Outcome(1, "install-systemd.sh --llm --uninstall exited with 5")


def test_no_checkout_or_no_service_manager_is_a_refusal(tmp_path):
    script = Script()

    gone = units.install(root=tmp_path, platform="darwin", run=script)
    windows = units.install(root=checkout(tmp_path / "c"), platform="win32", run=script)

    assert gone.code == 2 and "is not here" in gone.message
    assert "from a clone of Keryx" in gone.message
    assert windows.code == 2 and "neither" in windows.message
    assert script.ran == []


def test_the_real_runner_captures_the_scripts_output(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text("#!/bin/sh\necho out\necho err >&2\nexit 3\n")
    script.chmod(0o755)

    assert units.run_captured([str(script)]) == (3, "out\n", "err\n")
    assert units.run_captured([str(tmp_path / "missing")])[0] == 127


def test_a_renamed_agent_unit_is_the_one_named(tmp_path):
    outcome = units.install(settings=Settings(service_unit="mine"), root=checkout(tmp_path / "c"),
                            platform="darwin", run=Script())

    assert outcome.message == "installed mine, dev.keryx.tunnel"


def test_an_installer_that_hangs_is_stopped_and_said(tmp_path, monkeypatch):
    script = tmp_path / "s.sh"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    monkeypatch.setattr(units, "INSTALL_TIMEOUT_S", 0.2)

    code, _out, err = units.run_captured([str(script)])

    assert code != 0 and err == "s.sh did not finish within 0.2 s"
    outcome = units.install(root=checkout(tmp_path / "c"), platform="darwin",
                            run=lambda argv: (code, "", err))
    assert outcome == units.Outcome(1, "s.sh did not finish within 0.2 s")
