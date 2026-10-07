"""The model server, set up: the one implementation `keryx setup` and `keryx models start`
share (`keryx.localmodels.server`), driven here through fakes and a recording report."""

import contextlib
from pathlib import Path

import pytest

from keryx.config import Settings
from keryx.endpoints import Endpoint
from keryx.localmodels import server
from keryx.localmodels.catalog import by_key
from keryx.localmodels.hardware import Hardware

MACHINE = Hardware("metal", "Apple silicon", 0.0, 36.0)
ENTRY = by_key("gpt-oss-20b")


class Recorder:
    """What the report was told, and what the fakes were asked, in one order."""

    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def kinds(self) -> list[str]:
        return [line[0] for line in self.lines]

    def of(self, kind: str) -> list[tuple]:
        return [line[1:] for line in self.lines if line[0] == kind]

    @contextlib.contextmanager
    def step(self, step: str, message: str):
        self.lines.append(("step", step, message))
        yield

    def warn(self, message: str) -> None:
        self.lines.append(("warn", message))

    def error(self, message: str, *, log: Path | None = None) -> None:
        self.lines.append(("error", message, log))

    def script(self, code: int = 0):
        def run(argv) -> int:
            self.lines.append(("script", list(argv)))
            return code

        return run

    def wait(self, problem: str | None = None):
        def wait_for(endpoint: Endpoint, kind: str, timeout: float) -> str | None:
            self.lines.append(("wait", kind, endpoint.base_url, timeout))
            return problem

        return wait_for


def checkout(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (root / "scripts" / name).write_text("#!/bin/sh\n")
    return root


# --- llama.cpp -----------------------------------------------------------------------------


def test_homebrew_is_used_on_a_mac_that_has_it_and_only_there():
    assert server.uses_brew(platform="darwin", which=lambda name: f"/opt/homebrew/bin/{name}")
    assert not server.uses_brew(platform="darwin", which=lambda name: None)
    assert not server.uses_brew(platform="linux", which=lambda name: "/usr/bin/brew")


def test_llama_cpp_from_homebrew(tmp_path):
    log = Recorder()

    found = server.install_llama(tmp_path, MACHINE, brew=True, run_script=log.script(0),
                                 install=lambda cache, machine: pytest.fail("not downloaded"))

    assert found is None  # Homebrew puts it on PATH; the caller looks for it there
    assert log.of("script") == [(["brew", "install", "llama.cpp"],)]


def test_homebrew_that_does_not_finish_is_a_sentence(tmp_path):
    with pytest.raises(server.InstallError, match="^brew install llama.cpp did not finish$"):
        server.install_llama(tmp_path, MACHINE, brew=True, run_script=Recorder().script(1),
                             install=lambda cache, machine: "x")


def test_llama_cpp_from_its_release_builds(tmp_path):
    asked = []

    def install(cache, machine):
        asked.append((cache, machine))
        return str(cache / "llama.cpp" / "llama-server")

    found = server.install_llama(tmp_path, MACHINE, brew=False,
                                 run_script=lambda argv: pytest.fail("no script"),
                                 install=install)

    assert found == str(tmp_path / "llama.cpp" / "llama-server")
    assert asked == [(tmp_path, MACHINE)]


def test_a_release_download_that_fails_is_a_sentence(tmp_path):
    def broken(cache, machine):
        raise RuntimeError("llama.cpp has no release build for this system")

    with pytest.raises(server.InstallError,
                       match="^could not install llama.cpp: llama.cpp has no release build"):
        server.install_llama(tmp_path, MACHINE, brew=False, run_script=Recorder().script(),
                             install=broken)


# --- its unit, and waiting for it ------------------------------------------------------------


def test_the_unit_is_installed_and_the_server_waited_for(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")
    settings = Settings(llm_server_port=8091)

    ok = server.serve_here(settings, log, run_script=log.script(0), wait_for_server=log.wait(),
                           root=root, platform="darwin")

    assert ok is True
    assert log.lines == [
        ("step", "unit", "Installing the model server's service"),
        ("script", [str(root / "scripts" / "install-launchd.sh"), "--llm"]),
        ("step", "start", "Loading the model into llama.cpp at http://127.0.0.1:8091/v1"),
        ("wait", "models", "http://127.0.0.1:8091/v1", server.LLM_START_S),
    ]


def test_a_unit_that_will_not_install_is_said_and_not_waited_for(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")

    ok = server.serve_here(Settings(), log, run_script=log.script(3), wait_for_server=log.wait(),
                           root=root, platform="linux")

    assert ok is False
    assert log.of("error") == [("install-systemd.sh --llm exited with 3", None)]
    assert not log.of("wait")


def test_no_installer_here_is_said(tmp_path):
    log = Recorder()

    ok = server.serve_here(Settings(), log, run_script=log.script(), wait_for_server=log.wait(),
                           root=tmp_path, platform="darwin")

    assert ok is False and "is not here" in log.of("error")[0][0]
    assert not log.of("script")


def test_a_server_that_does_not_come_up_points_at_its_log(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")
    settings = Settings()

    ok = server.serve_here(settings, log, run_script=log.script(),
                           wait_for_server=log.wait("could not reach 127.0.0.1 (ConnectError)"),
                           root=root, platform="darwin")

    assert ok is False
    [(message, where)] = log.of("error")
    assert message.startswith(
        "the model server did not come up: could not reach 127.0.0.1 (ConnectError)")
    assert where == settings.state_dir / "logs" / "keryx-llm.err.log"


# --- Ollama --------------------------------------------------------------------------------


def test_ollama_pulls_the_catalog_model_and_is_pointed_at():
    log = Recorder()

    assert server.pull_into_ollama(ENTRY, log.script(0)) is None
    assert log.of("script") == [(["ollama", "pull", ENTRY.ollama],)]
    assert server.pull_into_ollama(ENTRY, log.script(1)) == "ollama pull did not finish"
    assert server.ollama_values(ENTRY) == {
        "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:11434/v1", "LOCAL_AGENT_MODEL": ENTRY.ollama,
        "LLM_SERVER_MODEL": None, "LOCAL_AGENT_API_KEY": None,
        "LOCAL_AGENT_API": "anthropic-messages",
    }
