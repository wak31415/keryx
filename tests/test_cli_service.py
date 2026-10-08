"""`keryx service` and `keryx models start|stop`: the units and the model server, from the
command line the Mac app runs — every service manager, installer and server faked."""

import asyncio
import json
import os
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner
from wording import for_the_app

from keryx import units
from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.endpoints import Probe
from keryx.localmodels import catalog, download, runtimes, server, servers, use, voice

runner = CliRunner()
CODER = catalog.by_key("qwen3-coder-30b-a3b")
OLLAMA_MODEL = catalog.by_key("gpt-oss-20b")
TERMINAL = {"done", "error", "cancelled"}
URL = "http://127.0.0.1:8090/v1"



def told(event: dict) -> str:
    """An error event's sentence and its detail: everything it says, wherever it says it."""
    return f"{event['message']} {event.get('detail') or ''}"

def events_of(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


def checkout(root) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (root / "scripts" / name).write_text("#!/bin/sh\n")


class Machine:
    """What `keryx models start` and `keryx service` find: their scripts, servers, units."""

    def __init__(self) -> None:
        self.llama: str | None = "/opt/homebrew/bin/llama-server"
        self.ollama: str | None = None
        self.script_code = 0
        self.wait_problem: str | None = None
        self.served: str | None = CODER.key
        self.scripts: list[list[str]] = []
        self.captured = (0, "", "")
        self.installed: set[str] = set()

    def run(self, argv) -> int:
        self.scripts.append(list(argv))
        return self.script_code

    def run_captured(self, argv):
        self.scripts.append(list(argv))
        return self.captured


@pytest.fixture
def machine(monkeypatch, tmp_path):
    found = Machine()
    checkout(tmp_path / "checkout")
    monkeypatch.setattr(voice, "PLATFORM", "darwin")
    monkeypatch.setattr(runtimes, "llama_server", lambda cache, **_: found.llama)
    monkeypatch.setattr(runtimes, "ollama", lambda: found.ollama)
    monkeypatch.setattr(runtimes, "which", lambda name: None)
    monkeypatch.setattr(voice, "run_detached", found.run)
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: found.wait_problem)
    monkeypatch.setattr(use, "probe", lambda url: (found.served is not None, found.served))
    monkeypatch.setattr(units, "run_captured", found.run_captured)
    monkeypatch.setattr("keryx.restart.service.is_installed",
                        lambda target, **_: target.unit in found.installed)
    monkeypatch.setattr("keryx.restart.service.is_active", lambda target, **_: True)
    return found


def chosen() -> Settings:
    """LLM_SERVER_MODEL chosen (as `keryx models use` saves it) and downloaded."""
    ConfigStore().set({"LLM_SERVER_MODEL": CODER.key, "LOCAL_AGENT_BASE_URL": URL,
                       "LOCAL_AGENT_MODEL": CODER.key})
    settings = Settings()
    path = download.model_path(settings.cache_dir, CODER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        out.truncate(CODER.size)
    return settings


def installer(tmp_path) -> str:
    return str(tmp_path / "checkout" / "scripts" / "install-launchd.sh")


# --- keryx service -------------------------------------------------------------------------


def test_service_json_is_every_unit_installed_and_running(machine):
    machine.installed = {"dev.keryx.agent", "dev.keryx.llm"}

    result = runner.invoke(app, ["service", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"manager": "launchd", "units": {
        "agent": {"installed": True, "active": True},
        "tunnel": {"installed": False, "active": False},
        "llm": {"installed": True, "active": True},
        "voice": {"installed": False, "active": False},
    }}


def test_service_in_words(machine):
    machine.installed = {"dev.keryx.agent"}

    result = runner.invoke(app, ["service"])

    assert result.exit_code == 0, result.output
    assert "launchd" in result.stdout
    assert "agent" in result.stdout and "dev.keryx.agent" in result.stdout
    assert "not installed" in result.stdout


@pytest.mark.parametrize(("argv", "flags", "said"), [
    (["service", "install"], [], "installed dev.keryx.agent, dev.keryx.tunnel"),
    (["service", "uninstall", "--voice"], ["--voice", "--uninstall"], "removed dev.keryx.voice"),
    (["service", "uninstall", "--llm", "--voice"], ["--llm", "--voice", "--uninstall"],
     "removed dev.keryx.llm, dev.keryx.voice"),
    (["models", "stop"], ["--llm", "--uninstall"], "removed dev.keryx.llm"),
])
def test_installs_run_the_checkouts_installer(machine, tmp_path, argv, flags, said):
    result = runner.invoke(app, [*argv, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True, "message": said}
    assert machine.scripts == [[installer(tmp_path), *flags]]

    words = runner.invoke(app, argv)
    assert words.exit_code == 0 and said in words.stdout


def test_a_failed_install_exits_1_with_the_scripts_last_error_line(machine):
    machine.captured = (1, "", "PUBLIC_HOST is not set: run keryx setup\n")

    result = runner.invoke(app, ["service", "install", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "PUBLIC_HOST is not set: run keryx setup" in result.stderr


def test_no_checkout_is_exit_2(machine, monkeypatch, tmp_path):
    monkeypatch.setattr("keryx.cli.repo_root", lambda: tmp_path / "not-a-checkout")

    result = runner.invoke(app, ["service", "install", "--json"])

    assert result.exit_code == 2 and result.stdout == ""
    assert "from a clone of Keryx" in result.stderr and machine.scripts == []


@pytest.mark.parametrize(("flags", "named"), [
    (["--llm"], ["keryx models start"]),
    (["--voice"], ["keryx voice use here"]),
    (["--llm", "--voice"], ["keryx models start", "keryx voice use here"]),
])
def test_a_model_servers_unit_is_not_installed_bare(machine, flags, named):
    """`models start` and `voice use here` own those units: install, start, check, and the
    unit removed again when the server does not come up. A bare install would leave one
    restarting for ever."""
    result = runner.invoke(app, ["service", "install", *flags, "--json"])

    assert result.exit_code == 2 and machine.scripts == []
    document = json.loads(result.stdout)
    assert document["ok"] is False and set(document) == {"ok", "message"}
    assert all(command in document["message"] for command in named)

    words = runner.invoke(app, ["service", "install", *flags])
    assert words.exit_code == 2 and words.stdout == "" and machine.scripts == []
    assert all(command in words.stderr for command in named)


def test_the_help_points_the_model_servers_elsewhere():
    result = runner.invoke(app, ["service", "install", "--help"])

    assert result.exit_code == 0
    assert "keryx models start" in result.stdout and "keryx voice use here" in result.stdout


# --- keryx service stop|start --------------------------------------------------------------


def on_disk(*labels):
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in labels:
        (agents / f"{label}.plist").write_text("<plist/>")


def test_service_stop_json_keeps_the_plists_and_disables_them(machine):
    machine.installed = {"dev.keryx.agent", "dev.keryx.tunnel", "dev.keryx.llm"}
    on_disk("dev.keryx.agent", "dev.keryx.tunnel", "dev.keryx.llm")

    result = runner.invoke(app, ["service", "stop", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document == {
        "ok": True, "message": units.STOPPED_MAIN, "detail": None,
        "units": [{"kind": "agent", "unit": "dev.keryx.agent", "state": "stopped"},
                  {"kind": "tunnel", "unit": "dev.keryx.tunnel", "state": "stopped"}],
    }
    uid = os.getuid()
    # Both disabled before either is booted out.
    assert machine.scripts == [["launchctl", "disable", f"gui/{uid}/dev.keryx.agent"],
                               ["launchctl", "disable", f"gui/{uid}/dev.keryx.tunnel"],
                               ["launchctl", "bootout", f"gui/{uid}/dev.keryx.agent"],
                               ["launchctl", "bootout", f"gui/{uid}/dev.keryx.tunnel"]]
    assert not any("dev.keryx.llm" in argv[-1] for argv in machine.scripts)
    assert (Path.home() / "Library" / "LaunchAgents" / "dev.keryx.agent.plist").exists()


def test_service_stop_all_stops_the_model_servers_too(machine):
    machine.installed = {"dev.keryx.agent", "dev.keryx.llm", "dev.keryx.voice"}

    result = runner.invoke(app, ["service", "stop", "--all", "--json"])

    assert result.exit_code == 0, result.output
    states = {row["kind"]: row["state"] for row in json.loads(result.stdout)["units"]}
    assert states == {"agent": "stopped", "tunnel": "not-installed", "llm": "stopped",
                      "voice": "stopped"}


def test_service_stop_settles_a_restart_in_flight_so_the_watchdog_rings_nobody(
    machine, monkeypatch
):
    from keryx.restart.store import RECORD_NAME, RestartRecord, RestartStore
    from keryx.restart.watchdog import REPORTED, watch

    machine.installed = {"dev.keryx.agent"}
    settings = Settings()
    store = RestartStore(settings.state_dir / RECORD_NAME)
    store.save(RestartRecord(requested_at="2026-10-07T09:00:00+00:00", reason="new code",
                             number="+15550000001"))
    settled_before = []
    run = machine.run_captured

    def bootout_sees(argv):
        if argv[1] == "bootout":
            settled_before.append(store.load().state)
        return run(argv)

    monkeypatch.setattr(units, "run_captured", bootout_sees)

    result = runner.invoke(app, ["service", "stop", "--json"])

    assert result.exit_code == 0, result.output
    assert settled_before == ["stopped"]  # settled before the job was booted out
    record = store.load()
    assert (record.state, record.error) == ("stopped",
                                            "Keryx was stopped by its owner before it came back")

    class NoPhone:
        configured = False

    async def no_sleep(seconds):
        raise AssertionError("a settled restart is not waited on")

    outcome = asyncio.run(watch(settings, store=store, twilio=NoPhone(), sleep=no_sleep))
    assert outcome == REPORTED


def test_service_start_all_json_starts_what_is_installed(machine):
    on_disk("dev.keryx.agent", "dev.keryx.tunnel", "dev.keryx.voice")

    result = runner.invoke(app, ["service", "start", "--all", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["ok"] is True and document["message"] == units.RUNNING_ALL
    assert {row["kind"]: row["state"] for row in document["units"]} == {
        "agent": "running", "tunnel": "running", "llm": "not-installed", "voice": "running"}
    verbs = [argv[1] for argv in machine.scripts]
    assert verbs == ["enable"] * 3 + ["bootstrap"] * 3


def test_service_start_with_nothing_installed_is_refused(machine):
    result = runner.invoke(app, ["service", "start", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["message"] == units.NOT_INSTALLED_YET
    assert machine.scripts == []


def test_a_unit_that_will_not_stop_exits_1_with_what_launchctl_said(machine):
    machine.installed = {"dev.keryx.agent"}
    machine.captured = (5, "", "Boot-out failed: 5: Input/output error")

    result = runner.invoke(app, ["service", "stop", "--json"])

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["ok"] is False and "Input/output error" in document["detail"]
    assert for_the_app(document["message"])


def test_service_stop_in_words(machine):
    machine.installed = {"dev.keryx.agent"}

    result = runner.invoke(app, ["service", "stop"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [units.STOPPED_MAIN,
                                          "agent   dev.keryx.agent        stopped",
                                          "tunnel  dev.keryx.tunnel       not-installed"]


@pytest.mark.parametrize("variable", ["KERYX_ACTOR", "JARVIS_ACTOR"])
@pytest.mark.parametrize("verb", ["stop", "start"])
def test_stop_and_start_are_the_owners_alone(machine, monkeypatch, variable, verb):
    monkeypatch.setenv(variable, "service")
    machine.installed = {"dev.keryx.agent"}

    result = runner.invoke(app, ["service", verb, "--all", "--json"])

    assert result.exit_code == 2 and machine.scripts == []
    document = json.loads(result.stdout)
    assert document["ok"] is False and "only the owner may" in document["message"]
    assert document["units"] == []


def test_a_migration_still_to_run_refuses_a_start_and_not_a_stop(machine):
    old = Path.home() / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")
    machine.installed = {"dev.keryx.agent"}

    started = runner.invoke(app, ["service", "start", "--json"])

    assert started.exit_code == 2 and machine.scripts == []
    document = json.loads(started.stdout)
    assert document["ok"] is False and "migrate" in document["message"]
    assert runner.invoke(app, ["service", "stop", "--json"]).exit_code == 0


@pytest.mark.parametrize("message", [
    units.STOPPED_MAIN, units.STOPPED_ALL, units.RUNNING_MAIN, units.RUNNING_ALL,
    units.SERVERS_STOPPED, units.NOTHING_TO_STOP, units.NOT_INSTALLED_YET,
    *units.COULD_NOT.values(), units.NO_MANAGER,
])
def test_every_sentence_is_for_a_person(message):
    assert for_the_app(message)


@pytest.mark.parametrize("argv", [["service", "install"], ["service", "uninstall", "--llm"],
                                  ["models", "stop"]])
def test_installs_are_the_owners_alone(machine, monkeypatch, argv):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, [*argv, "--json"])

    assert result.exit_code == 1 and "only the owner may" in result.stderr
    assert machine.scripts == [] and result.stdout == ""


# --- keryx models start --------------------------------------------------------------------


def test_start_streams_its_steps_and_ends_with_the_served_model(machine, tmp_path):
    chosen()

    result = runner.invoke(app, ["models", "start", "--json"])

    assert result.exit_code == 0, result.output
    events = events_of(result)
    assert [e["step"] for e in events if e["event"] == "step"] == [
        "runtime", "unit", "start", "check"]
    assert events[-1] == {"event": "done", "url": URL, "served_model": CODER.key}
    assert machine.scripts == [[installer(tmp_path), "--llm"]]


def test_start_installs_llama_cpp_when_it_is_missing_and_tells_the_download(
    machine, monkeypatch
):
    settings = chosen()
    machine.llama = None

    def install(cache, hardware, *, get):
        get("https://example.invalid/llama.tar.gz", cache / "llama.tar.gz")
        machine.llama = str(cache / "llama.cpp" / "llama-server")
        return machine.llama

    def fetch(url, dest, *, progress):
        progress(1, 4)
        progress(4, 4)

    monkeypatch.setattr(runtimes, "install_llama_cpp", install)
    monkeypatch.setattr(runtimes, "fetch", fetch)
    monkeypatch.setattr("keryx.localmodels.hardware.detect", lambda: None)

    result = runner.invoke(app, ["models", "start", "--json"])

    assert result.exit_code == 0, result.output
    progress = [e for e in events_of(result) if e["event"] == "progress"]
    assert progress[0] == {"event": "progress", "step": "runtime", "done": 1, "total": 4}
    assert {"event": "step", "step": "runtime", "message": "llama.cpp is installed.",
            "detail": str(settings.cache_dir / "llama.cpp" / "llama-server")} \
        in events_of(result)


def test_start_with_ollama_saves_the_local_agent_once_ollama_lists_it(machine, monkeypatch):
    ConfigStore().set({"LLM_SERVER_MODEL": OLLAMA_MODEL.key})
    machine.ollama = "/usr/local/bin/ollama"
    monkeypatch.setattr("keryx.endpoints.probe",
                        lambda endpoint, **_: Probe(True, (OLLAMA_MODEL.ollama,)))

    result = runner.invoke(app, ["models", "start", "--runtime", "ollama", "--json"])

    assert result.exit_code == 0, result.output
    assert events_of(result)[-1] == {"event": "done", "url": "http://127.0.0.1:11434/v1",
                                     "served_model": OLLAMA_MODEL.ollama}
    settings = Settings()
    assert settings.local_agent_model == OLLAMA_MODEL.ollama
    assert settings.llm_server_model is None
    assert machine.scripts == [["ollama", "pull", OLLAMA_MODEL.ollama]]


def test_start_with_an_unknown_runtime_is_refused(machine):
    chosen()

    result = runner.invoke(app, ["models", "start", "--runtime", "vllm", "--json"])

    assert result.exit_code == 2 and machine.scripts == []


def test_start_in_words(machine):
    chosen()

    result = runner.invoke(app, ["models", "start"])

    assert result.exit_code == 0, result.output
    assert "Installing the model server's service…" in result.stdout
    assert f"the model server is running at {URL}, serving {CODER.key}" in result.stdout


def test_start_in_words_refused_and_cancelled(machine, monkeypatch):
    refused = runner.invoke(app, ["models", "start"])
    assert refused.exit_code == 2 and server.NOT_READY in refused.stderr

    chosen()

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(voice, "run_detached", interrupted)
    cancelled = runner.invoke(app, ["models", "start"])
    assert cancelled.exit_code == 130 and "cancelled" in cancelled.stderr

    monkeypatch.setenv("KERYX_ACTOR", "service")
    service = runner.invoke(app, ["models", "start"])
    assert service.exit_code == 1 and "only the owner may" in service.stderr


def test_start_is_cancelled_by_a_terminate(machine, monkeypatch):
    """What the Mac app's Cancel sends."""
    chosen()
    before = signal.getsignal(signal.SIGTERM)

    def terminated(endpoint, kind, timeout):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the terminate should have stopped it")

    monkeypatch.setattr(servers, "wait_for", terminated)

    result = runner.invoke(app, ["models", "start", "--json"])

    assert result.exit_code == 130
    assert events_of(result)[-1] == {"event": "cancelled"}
    assert signal.getsignal(signal.SIGTERM) is before
    assert machine.scripts[-1][-2:] == ["--llm", "--uninstall"]  # no unit left behind


def _done(monkeypatch, machine, tmp_path):
    chosen()


def _not_chosen(monkeypatch, machine, tmp_path):
    pass


def _not_downloaded(monkeypatch, machine, tmp_path):
    ConfigStore().set({"LLM_SERVER_MODEL": CODER.key})


def _no_checkout(monkeypatch, machine, tmp_path):
    chosen()
    monkeypatch.setattr("keryx.cli.repo_root", lambda: tmp_path / "not-a-checkout")


def _never_up(monkeypatch, machine, tmp_path):
    chosen()
    machine.wait_problem = "could not reach 127.0.0.1 (ConnectError)"


def _another_model(monkeypatch, machine, tmp_path):
    chosen()
    machine.served = "someone-elses"


def _unit_failed(monkeypatch, machine, tmp_path):
    chosen()
    machine.script_code = 4


def _migration(monkeypatch, machine, tmp_path):
    chosen()
    old = tmp_path / "home" / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")


def _service(monkeypatch, machine, tmp_path):
    chosen()
    monkeypatch.setenv("KERYX_ACTOR", "service")


def _cancelled(monkeypatch, machine, tmp_path):
    chosen()

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(voice, "run_detached", interrupted)


def _unexpected(monkeypatch, machine, tmp_path):
    chosen()

    def broken(argv):
        raise PermissionError("the installer is not executable")

    monkeypatch.setattr(voice, "run_detached", broken)


@pytest.mark.parametrize(
    ("arrange", "ending", "code", "message"),
    [(_done, "done", 0, None),
     (_not_chosen, "error", 2, "Choose and download a model first."),
     (_not_downloaded, "error", 2, "Choose and download a model first."),
     (_no_checkout, "error", 2, "from a clone of Keryx"),
     (_never_up, "error", 1, "did not come up"),
     (_another_model, "error", 1, "serves someone-elses"),
     (_unit_failed, "error", 1, "--llm exited with 4"),
     (_migration, "error", 2, "keryx cannot do that yet"),
     (_service, "error", 1, "only the owner may"),
     (_cancelled, "cancelled", 130, None),
     (_unexpected, "error", 1, "the installer is not executable")],
)
def test_start_json_ends_with_exactly_one_ending(
    machine, monkeypatch, tmp_path, arrange, ending, code, message
):
    arrange(monkeypatch, machine, tmp_path)

    result = runner.invoke(app, ["models", "start", "--json"])

    events = events_of(result)
    assert [e["event"] for e in events if e["event"] in TERMINAL] == [ending]
    assert events[-1]["event"] == ending
    assert all(e["event"] in ("step", "progress") for e in events[:-1])
    if message is not None:
        assert message in told(events[-1])
    if ending == "error":  # a sentence for a person; what a tool said is the detail
        assert "detail" in events[-1] and for_the_app(events[-1]["message"]), events[-1]
    for event in events:  # every step and warning too: a path or a command is the detail
        if event["event"] == "step":
            assert "detail" in event and for_the_app(event["message"]), event
    assert result.exit_code == code
    assert result.stderr == ""
    if code == 2 or ending == "error" and "only the owner" in events[-1]["message"]:
        assert machine.scripts == [] and len(events) == 1  # a refusal: nothing was tried


def test_a_server_that_never_comes_up_leaves_no_unit_behind(machine, tmp_path):
    chosen()
    machine.wait_problem = "no answer"

    result = runner.invoke(app, ["models", "start", "--json"])

    assert result.exit_code == 1
    assert machine.scripts == [[installer(tmp_path), "--llm"],
                               [installer(tmp_path), "--llm", "--uninstall"]]
    assert "its service was removed" in events_of(result)[-1]["message"]
    assert "keryx-llm.err.log" in events_of(result)[-1]["detail"]
