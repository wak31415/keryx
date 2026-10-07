"""The model server set up on this machine: the wizard's Local models section and `keryx
models start` share every step here, so the two cannot drift.

Three steps, each told to a report before its slow work: llama.cpp installed when it is
missing (`install_llama` — Homebrew's on a Mac that has it, else its pinned release build),
the `--llm` unit installed (`serve_here`, through the checkout's installer, as the voice's
unit is), and the wait for it to load the model. Ollama is the other runtime: the model is
pulled into it (`pull_into_ollama`) and the local agent pointed at it (`ollama_values`).
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from keryx.config import Settings
from keryx.endpoints import Endpoint
from keryx.localmodels import voice
from keryx.localmodels.catalog import ModelEntry
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.servers import HOST
from keryx.localmodels.use import messages_harness

#: How long a model server may take to load.
LLM_START_S = 600.0
OLLAMA_URL = "http://127.0.0.1:11434"
BREW_INSTALL = ("brew", "install", "llama.cpp")
#: The model server's own log, which the unit writes.
LOG_NAME = "keryx-llm.err.log"


class InstallError(RuntimeError):
    """llama.cpp could not be installed, in a sentence."""


class Report(Protocol):
    """How the steps are told: each before its slow work, and what stopped them."""

    def step(self, step: str, message: str) -> AbstractContextManager[None]: ...

    def error(self, message: str, *, log: Path | None = None) -> None: ...


# --- llama.cpp -----------------------------------------------------------------------------


def uses_brew(*, platform: str, which: Callable[[str], str | None]) -> bool:
    """Whether llama.cpp is installed with Homebrew: on a Mac that has it."""
    return platform == "darwin" and bool(which("brew"))


def install_llama(
    cache_dir: Path,
    machine: Hardware,
    *,
    brew: bool,
    run_script: Callable[[Sequence[str]], int],
    install: Callable[[Path, Hardware], str],
) -> str | None:
    """Install llama.cpp: with Homebrew when `brew` (None: it is on PATH now), else its
    release build into `CACHE_DIR/llama.cpp` (the path of its `llama-server`).
    `InstallError` with a sentence when it could not be."""
    if brew:
        if run_script(list(BREW_INSTALL)) != 0:
            raise InstallError("brew install llama.cpp did not finish")
        return None
    try:
        return install(cache_dir, machine)
    except Exception as exc:  # a network or archive failure, said in a sentence
        raise InstallError(f"could not install llama.cpp: {exc}") from exc


# --- its unit, and waiting for it ------------------------------------------------------------


def endpoint(settings: Settings) -> Endpoint:
    """The model server on this machine, at `LLM_SERVER_PORT`."""
    return Endpoint.parse(f"http://{HOST}:{settings.llm_server_port}/v1")


def log_path(settings: Settings) -> Path:
    return settings.state_dir / "logs" / LOG_NAME


def serve_here(
    settings: Settings,
    report: Report,
    *,
    run_script: Callable[[Sequence[str]], int],
    wait_for_server: Callable[[Endpoint, str, float], str | None],
    root: Path,
    platform: str | None = None,
) -> bool:
    """Install the model server's unit from the checkout at `root` and wait for it to load
    `LLM_SERVER_MODEL`; False once the report has been told why it did not."""
    with report.step("unit", "Installing the model server's service"):
        problem = voice.install_unit("--llm", run_script, root=root, platform=platform)
    if problem is not None:
        report.error(problem)
        return False
    here = endpoint(settings)
    with report.step("start", f"Loading the model into llama.cpp at {here.base_url}"):
        problem = wait_for_server(here, "models", LLM_START_S)
    if problem is not None:
        report.error(f"the model server did not come up: {problem}", log=log_path(settings))
        return False
    return True


# --- Ollama --------------------------------------------------------------------------------


def pull_into_ollama(
    entry: ModelEntry, run_script: Callable[[Sequence[str]], int]
) -> str | None:
    """`ollama pull` the catalog model; None, or what went wrong."""
    if run_script(["ollama", "pull", entry.ollama]) != 0:
        return "ollama pull did not finish"
    return None


def ollama_values(entry: ModelEntry) -> dict[str, Any]:
    """The settings that make Ollama's copy of `entry` the local agent's model."""
    return {"LOCAL_AGENT_BASE_URL": f"{OLLAMA_URL}/v1", "LOCAL_AGENT_MODEL": entry.ollama,
            "LLM_SERVER_MODEL": None, **messages_harness()}
