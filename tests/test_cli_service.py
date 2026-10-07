"""`keryx service` and `keryx models start|stop`: the units and the model server, from the
command line the Mac app runs — every service manager, installer and server faked."""

import json
import os
import signal

import pytest
from typer.testing import CliRunner

from keryx import units
from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.endpoints import Probe
from keryx.localmodels import catalog, download, runtimes, servers, use, voice

runner = CliRunner()
CODER = catalog.by_key("qwen3-coder-30b-a3b")
OLLAMA_MODEL = catalog.by_key("gpt-oss-20b")
TERMINAL = {"done", "error", "cancelled"}
URL = "http://127.0.0.1:8090/v1"


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
    (["service", "install", "--llm"], ["--llm"], "installed dev.keryx.llm"),
    (["service", "uninstall", "--voice"], ["--voice", "--uninstall"], "removed dev.keryx.voice"),
    (["service", "install", "--llm", "--voice"], ["--llm", "--voice"],
     "installed dev.keryx.llm, dev.keryx.voice"),
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

    result = runner.invoke(app, ["service", "install", "--llm", "--json"])

    assert result.exit_code == 2 and result.stdout == ""
    assert "from a clone of Keryx" in result.stderr and machine.scripts == []


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
    assert {"event": "step", "step": "runtime",
            "message": f"llama.cpp: {settings.cache_dir / 'llama.cpp' / 'llama-server'}"} \
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
    assert refused.exit_code == 2 and "choose and download a model first" in refused.stderr

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
     (_not_chosen, "error", 2, "choose and download a model first"),
     (_not_downloaded, "error", 2, "choose and download a model first"),
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
        assert message in events[-1]["message"]
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
    assert "keryx-llm.err.log" in events_of(result)[-1]["message"]
