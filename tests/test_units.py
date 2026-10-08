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


# --- stopping and starting them ----------------------------------------------------------


class Manager:
    """A service manager, faked: which units are loaded (launchd) or running, which have
    a unit file on disk, every command it was handed, and the ones it refuses."""

    def __init__(self, manager="launchd", loaded=(), on_disk=(), refuse=()):
        self.manager = manager
        self.loaded, self.on_disk = set(loaded), set(on_disk) | set(loaded)
        self.refuse = dict(refuse)  # verb -> (code, stderr)
        self.commands: list[list[str]] = []
        self.disabled: set[str] = set()

    def run(self, argv):
        self.commands.append(list(argv))
        verb, unit = argv[2 if argv[0] == "systemctl" else 1], argv[-1].rsplit("/", 1)[-1]
        if verb in self.refuse:
            return (*self.refuse[verb][:1], "", self.refuse[verb][1])
        if verb == "disable":
            self.disabled.add(unit)
        elif verb == "enable":
            self.disabled.discard(unit)
        elif verb == "bootout":
            self.loaded.discard(unit)
        elif verb == "bootstrap":
            self.loaded.add(unit.removesuffix(".plist"))
        return 0, "", ""

    def verbs(self, unit):
        return [argv[2 if argv[0] == "systemctl" else 1] for argv in self.commands
                if argv[-1].rsplit("/", 1)[-1].removesuffix(".plist") == unit]

    def control(self, action, *, every=False, settle=None, settings=None):
        return units.control(
            settings or Settings(), action, every=every, settle=settle,
            platform="darwin" if self.manager == "launchd" else "linux", run=self.run,
            loaded=lambda target: target.unit in self.loaded,
            on_disk=lambda target: target.unit in self.on_disk,
            active=lambda target: target.unit in self.loaded,
            enabled=lambda target: target.unit not in self.disabled,
        )


MAIN = ("dev.keryx.agent", "dev.keryx.tunnel")
EVERY = (*MAIN, "dev.keryx.llm", "dev.keryx.voice")


def states(outcome):
    return {row["unit"]: row["state"] for row in outcome.units}


def test_stop_disables_then_boots_out_keryx_and_its_tunnel():
    launchd = Manager(loaded={*MAIN, "dev.keryx.llm"})

    outcome = launchd.control("stop")

    assert (outcome.ok, outcome.code, outcome.detail) == (True, 0, None)
    assert outcome.message == "Keryx is stopped, and won't start at login until it's started again."
    assert states(outcome) == {"dev.keryx.agent": "stopped", "dev.keryx.tunnel": "stopped"}
    # Disabled first: a stop cut short still leaves nothing to start at login.
    assert launchd.verbs("dev.keryx.agent") == ["disable", "bootout"]
    assert launchd.verbs("dev.keryx.llm") == []  # only with --all
    assert launchd.disabled == set(MAIN)
    assert launchd.loaded == {"dev.keryx.llm"}


def test_stop_all_walks_every_unit_in_the_table(monkeypatch):
    monkeypatch.setitem(units.NAMES, "launchd", {**units.NAMES["launchd"],
                                                 "voice-llm": "dev.keryx.voice-llm"})
    launchd = Manager(loaded={*EVERY, "dev.keryx.voice-llm"})

    outcome = launchd.control("stop", every=True)

    assert outcome.ok and launchd.loaded == set()
    assert [row["kind"] for row in outcome.units] == ["agent", "tunnel", "llm", "voice",
                                                      "voice-llm"]
    assert outcome.message == ("Keryx and its model servers are stopped, and won't start at "
                               "login until they're started again.")


def test_stop_settles_first_and_a_unit_already_stopped_is_left_stopped():
    launchd = Manager(on_disk={*MAIN})
    order = []
    launchd_run = launchd.run
    launchd.run = lambda argv: order.append(argv[1]) or launchd_run(argv)

    outcome = launchd.control("stop", settle=lambda: order.append("settle"))

    assert order[0] == "settle"
    assert outcome.ok and states(outcome) == {"dev.keryx.agent": "stopped",
                                              "dev.keryx.tunnel": "stopped"}
    assert launchd.verbs("dev.keryx.agent") == ["disable"]  # nothing loaded to boot out


def test_a_unit_not_installed_is_said_and_not_touched():
    launchd = Manager(loaded={"dev.keryx.agent"})

    outcome = launchd.control("stop", every=True)

    assert outcome.ok
    assert states(outcome) == {"dev.keryx.agent": "stopped", "dev.keryx.tunnel": "not-installed",
                               "dev.keryx.llm": "not-installed",
                               "dev.keryx.voice": "not-installed"}
    assert launchd.verbs("dev.keryx.tunnel") == []
    assert outcome.message == "Keryx is stopped, and won't start at login until it's started again."


def test_stopping_keryx_that_is_not_installed_is_nothing_to_do():
    outcome = Manager().control("stop")

    assert (outcome.ok, outcome.code) == (True, 0)
    assert outcome.message == ("Keryx isn't installed as a background process, so there's "
                               "nothing to stop.")


def test_start_enables_then_bootstraps_the_servers_first_and_keryx_last():
    launchd = Manager(on_disk=EVERY)

    outcome = launchd.control("start", every=True)

    assert (outcome.ok, outcome.code) == (True, 0)
    assert outcome.message == "Keryx and its model servers are running."
    assert launchd.loaded == set(EVERY)
    assert launchd.verbs("dev.keryx.agent") == ["enable", "bootstrap"]
    started = [argv[-1] for argv in launchd.commands if argv[1] == "bootstrap"]
    assert [path.rsplit("/", 1)[-1] for path in started] == [
        "dev.keryx.voice.plist", "dev.keryx.llm.plist", "dev.keryx.tunnel.plist",
        "dev.keryx.agent.plist"]
    # The table's order, whatever order they were started in.
    assert [row["kind"] for row in outcome.units] == ["agent", "tunnel", "llm", "voice"]


def test_start_of_a_loaded_job_kickstarts_it_and_is_idempotent():
    launchd = Manager(loaded=MAIN)

    first, second = launchd.control("start"), launchd.control("start")

    assert first.ok and second.ok
    assert launchd.verbs("dev.keryx.agent") == ["enable", "kickstart", "enable", "kickstart"]
    assert states(second) == {"dev.keryx.agent": "running", "dev.keryx.tunnel": "running"}


def test_start_all_starts_only_what_is_installed():
    launchd = Manager(on_disk={*MAIN, "dev.keryx.llm"})

    outcome = launchd.control("start", every=True)

    assert states(outcome)["dev.keryx.voice"] == "not-installed"
    assert launchd.verbs("dev.keryx.voice") == []
    assert launchd.loaded == {*MAIN, "dev.keryx.llm"}


def test_starting_keryx_that_is_not_installed_is_refused_and_starts_nothing():
    launchd = Manager(on_disk={"dev.keryx.llm"})

    outcome = launchd.control("start", every=True)

    assert (outcome.ok, outcome.code) == (False, 2)
    assert outcome.message == "Keryx isn't installed as a background process yet."
    assert launchd.commands == []
    assert states(outcome) == {"dev.keryx.agent": "not-installed",
                               "dev.keryx.tunnel": "not-installed",
                               "dev.keryx.llm": "stopped", "dev.keryx.voice": "not-installed"}


def test_only_the_model_servers_to_stop_says_so():
    launchd = Manager(loaded={"dev.keryx.llm"})

    outcome = launchd.control("stop", every=True)

    assert outcome.ok and launchd.loaded == set()
    assert outcome.message.startswith("Keryx isn't installed as a background process; its "
                                      "model servers are stopped")


def test_systemd_stops_and_starts_with_disable_and_enable():
    systemd = Manager("systemd", on_disk={"keryx.service", "cloudflared.service"})

    stopped, started = systemd.control("stop"), systemd.control("start")

    assert stopped.ok and started.ok
    assert systemd.verbs("keryx.service") == ["disable", "stop", "enable", "start"]
    assert systemd.commands[0] == ["systemctl", "--user", "disable", "keryx.service"]


def test_a_unit_that_will_not_stop_fails_the_whole_with_what_the_manager_said():
    launchd = Manager(loaded=MAIN, refuse={"bootout": (5, "Boot-out failed: 5: I/O error")})

    outcome = launchd.control("stop")

    assert (outcome.ok, outcome.code) == (False, 1)
    assert outcome.message == "Not everything could be stopped."
    assert states(outcome)["dev.keryx.agent"] == "failed"
    assert "Boot-out failed: 5: I/O error" in outcome.detail
    assert "dev.keryx.agent" in outcome.detail


def test_a_refusal_the_job_is_gone_anyway_is_no_failure():
    """launchd can say no while the job goes: what it does is what counts."""
    launchd = Manager(loaded=MAIN, refuse={"bootout": (3, "No such process")})
    run = launchd.run

    def run_then_gone(argv):
        result = run(argv)
        launchd.loaded.discard(argv[-1].rsplit("/", 1)[-1])
        return result

    launchd.run = run_then_gone

    assert launchd.control("stop").ok


def test_a_bootstrap_that_says_already_loaded_still_starts():
    launchd = Manager(on_disk=MAIN, refuse={"bootstrap": (5, "Bootstrap failed: 5")})
    run = launchd.run

    def run_and_load(argv):
        result = run(argv)
        if argv[1] == "bootstrap":
            launchd.loaded.add(argv[-1].rsplit("/", 1)[-1].removesuffix(".plist"))
        return result

    launchd.run = run_and_load

    assert launchd.control("start").ok


def test_a_failure_detail_is_redacted(monkeypatch):
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    settings = Settings(openai_api_key=secret)
    systemd = Manager("systemd", on_disk={"keryx.service"},
                      refuse={"start": (1, f"Failed: {secret}")})

    outcome = systemd.control("start", settings=settings)

    assert not outcome.ok and secret not in outcome.detail


def test_no_service_manager_is_a_refusal():
    outcome = units.control(Settings(service_manager="none"), "stop", platform="darwin",
                            run=lambda argv: pytest.fail("nothing to run"))

    assert (outcome.ok, outcome.code, outcome.units) == (False, 2, [])
    assert outcome.message == "There's no service manager on this machine to run Keryx with."


def test_the_document_is_the_contract():
    outcome = Manager(loaded=MAIN).control("stop")

    assert outcome.as_dict() == {
        "ok": True, "message": outcome.message, "detail": None,
        "units": [{"kind": "agent", "unit": "dev.keryx.agent", "state": "stopped"},
                  {"kind": "tunnel", "unit": "dev.keryx.tunnel", "state": "stopped"}],
    }


def test_status_counts_a_unit_on_disk_as_installed_though_it_is_unloaded(tmp_path):
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / "dev.keryx.agent.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")

    document = units.status(Settings(), platform="darwin", installed=lambda target: False,
                            active=lambda target: False)

    assert document["units"]["agent"] == {"installed": True, "active": False}
    assert document["units"]["tunnel"] == {"installed": False, "active": False}


def test_every_unit_is_disabled_before_any_is_stopped():
    """A bootout waits on the job; the app's quit deadline can cut it short there, and by
    then nothing may still be set to start at login."""
    launchd = Manager(loaded=EVERY)

    launchd.control("stop", every=True)

    verbs = [argv[1] for argv in launchd.commands]
    assert verbs == ["disable"] * 4 + ["bootout"] * 4


def test_every_unit_is_enabled_before_any_is_started():
    systemd = Manager("systemd", on_disk={"keryx.service", "cloudflared.service",
                                          "keryx-llm.service"})

    systemd.control("start", every=True)

    assert [argv[2] for argv in systemd.commands] == ["enable"] * 3 + ["start"] * 3


def test_a_failed_disable_is_a_failure_though_the_bootout_went_through():
    launchd = Manager(loaded=MAIN, refuse={"disable": (1, "Could not disable")})

    outcome = launchd.control("stop")

    assert (outcome.ok, outcome.code) == (False, 1)
    assert states(outcome) == {"dev.keryx.agent": "failed", "dev.keryx.tunnel": "failed"}
    assert "Could not disable" in outcome.detail
    assert launchd.loaded == set()  # still stopped now: only login is not secured


def test_a_failed_disable_that_launchd_lists_as_disabled_is_none():
    launchd = Manager(loaded=MAIN, refuse={"disable": (1, "already disabled")})
    launchd.disabled = set(MAIN)  # `print-disabled` says so

    assert launchd.control("stop").ok


def test_a_failed_enable_starts_nothing_and_says_so():
    launchd = Manager(on_disk=MAIN, refuse={"enable": (1, "Could not enable")})
    launchd.disabled = set(MAIN)

    outcome = launchd.control("start")

    assert (outcome.ok, outcome.code) == (False, 1)
    assert [argv[1] for argv in launchd.commands] == ["enable", "enable"]
    assert launchd.loaded == set()


def test_a_bootout_refused_while_the_job_stays_is_a_failure():
    launchd = Manager(loaded=MAIN, refuse={"bootout": (5, "Boot-out failed")})

    outcome = launchd.control("stop")

    assert not outcome.ok and launchd.disabled == set(MAIN)


@pytest.mark.parametrize("manager", ["none", "launchd"])
def test_a_stop_settles_the_restart_before_anything_else(manager):
    """Before the manager is found, before any probe, and on the paths that return early:
    no service manager, nothing installed."""
    settled = []

    def probe(target):
        assert settled, "probed before the restart was settled"
        return False

    outcome = units.control(Settings(service_manager=manager), "stop", platform="darwin",
                            settle=lambda: settled.append(True), run=probe, loaded=probe,
                            on_disk=probe)

    assert settled == [True]
    assert outcome.units == [] if manager == "none" else outcome.ok


def test_a_stop_that_could_not_settle_the_record_stops_all_the_same_and_says_so():
    launchd = Manager(loaded=MAIN)

    outcome = launchd.control("stop", settle=lambda: units.SETTLE_LOCKED)

    assert (outcome.ok, outcome.code, outcome.message) == (False, 1, units.NOT_SETTLED)
    assert outcome.detail == "the restart record stayed locked"
    assert states(outcome) == {"dev.keryx.agent": "stopped", "dev.keryx.tunnel": "stopped"}
    assert launchd.loaded == set() and launchd.disabled == set(MAIN)
