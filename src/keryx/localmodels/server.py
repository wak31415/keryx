"""The model server set up on this machine: the wizard's Local models section and `keryx
models start` share every step here, so the two cannot drift.

Three steps, each told to a report before its slow work: llama.cpp installed when it is
missing (`install_llama` — Homebrew's on a Mac that has it, else its pinned release build),
the `--llm` unit installed (`serve_here`, through the checkout's installer, as the voice's
unit is), and the wait for it to load the model. Ollama is the other runtime: the model is
pulled into it (`pull_into_ollama`) and the local agent pointed at it (`ollama_values`).

`start` is the whole of `keryx models start`, and exists because of a setup that stopped
halfway: a model chosen and downloaded, llama.cpp never installed and no server ever
started, so the voice server built on it crash-looped. It refuses (2) until a model is
chosen and downloaded, installs what is missing, starts the server, and ends by checking
that the server names the chosen model. A server that does not come up, or serves another
model, has its unit removed before the error is told: no unit is left restarting in a loop.
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import httpx

from keryx import endpoints
from keryx.config import Settings
from keryx.endpoints import Endpoint, Probe
from keryx.localmodels import catalog, download, hardware, runtimes, servers, use, voice
from keryx.localmodels.catalog import ModelEntry
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.runtimes import LLAMA_CPP_BUILD
from keryx.localmodels.servers import HOST
from keryx.localmodels.use import messages_harness
from keryx.localmodels.voice import FAILED, OK, REFUSED

#: How long a model server may take to load.
LLM_START_S = 600.0
OLLAMA_URL = "http://127.0.0.1:11434"
BREW_INSTALL = ("brew", "install", "llama.cpp")
#: The model server's own log, which the unit writes.
LOG_NAME = "keryx-llm.err.log"
LLAMA, OLLAMA = "llama", "ollama"
RUNTIMES = (LLAMA, OLLAMA)
#: The steps of `start`, in order, as the JSON lines name them.
STEPS = ("runtime", "unit", "start", "check")
#: What `start` says, first, while there is no model it could serve.
NOT_READY = "choose and download a model first"
#: Said of a unit `start` removed rather than leave it restarting a server that fails.
REMOVED = voice.REMOVED


class InstallError(RuntimeError):
    """llama.cpp could not be installed, in a sentence."""


class Report(Protocol):
    """How the steps are told: each before its slow work, and what stopped them."""

    def step(self, step: str, message: str) -> AbstractContextManager[None]: ...

    def error(self, message: str, *, log: Path | None = None) -> None: ...


class StartReport(Report, Protocol):
    """…and, for `start`, a download's progress and the server it ended with."""

    def progress(self, done: int, total: int | None) -> None: ...

    def done(self, url: str, served_model: str) -> None: ...

    def warn(self, message: str) -> None: ...


class Probes(Protocol):
    """What `start` asks of the machine: `machine()`, or a test's fakes."""

    llama_server: Callable[[Path], str | None]
    ollama: Callable[[], str | None]
    which: Callable[[str], str | None]
    hardware: Callable[[], Hardware]
    download_llama_cpp: Callable[[Path, Hardware, Callable[[int, int | None], None]], str]
    run_script: Callable[[Sequence[str]], int]
    wait_for_server: Callable[[Endpoint, str, float], str | None]
    served: Callable[[str], tuple[bool, str | None]]
    models: Callable[[Endpoint], Probe]
    unit_installed: Callable[[str], bool]


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
    with report.step("start", f"Loading the model into llama.cpp at {here.base_url}"), \
            _removed_if_stopped(run_script, root=root, platform=platform):
        problem = wait_for_server(here, "models", LLM_START_S)
    if problem is not None:
        removed = remove_unit(run_script, root=root, platform=platform)
        report.error(f"the model server did not come up: {problem}; {removed}",
                     log=log_path(settings))
        return False
    return True


def _removed_if_stopped(
    run_script: Callable[[Sequence[str]], int], *, root: Path, platform: str | None
) -> AbstractContextManager[None]:
    """The model server's unit removed when what this wraps is stopped (`voice`'s rule)."""
    return voice.removed_if_stopped("--llm", run_script, root=root, platform=platform)


def retire_llama(
    run_script: Callable[[Sequence[str]], int], *, installed: bool, root: Path,
    platform: str | None = None,
) -> str | None:
    """Once Ollama serves the model, the llama.cpp server's unit is removed when there is one:
    with `LLM_SERVER_MODEL` cleared it cannot start, and its manager would start it again and
    again. None when there was none; else what became of it, in a clause."""
    if not installed:
        return None
    return remove_unit(run_script, root=root, platform=platform)


def remove_unit(
    run_script: Callable[[Sequence[str]], int], *, root: Path, platform: str | None = None
) -> str:
    """Remove the model server's unit; what became of it, in a clause."""
    return voice.remove_unit("--llm", run_script, root=root, platform=platform)


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


# --- keryx models start --------------------------------------------------------------------


def start(
    settings: Settings,
    probes: Probes,
    report: StartReport,
    *,
    save: Callable[[dict[str, Any]], bool],
    root: Path,
    runtime: str = LLAMA,
    platform: str | None = None,
) -> int:
    """Serve `LLM_SERVER_MODEL` on this machine and check that it is what is served; the
    exit code. 2 for what refuses it before anything is tried — no model chosen and
    downloaded, no installer for the unit — 1 for what stopped it, 0 once the server
    names the chosen model. `save` (Ollama's only) stores values and says why it could not.
    """
    platform = platform or voice.PLATFORM
    if runtime == OLLAMA:
        return _start_ollama(settings, probes, report, save=save, root=root, platform=platform)
    if not settings.llm_server_model and on_ollama(settings):
        report.error(f"Ollama serves {settings.local_agent_model}; there is no llama.cpp "
                     "server to start (`keryx models use NAME` moves the model back to it)")
        return REFUSED
    if problem := _not_ready(settings):
        report.error(f"{NOT_READY}: {problem}")
        return REFUSED
    alias = servers.llm_model(settings)[0]
    script, problem = voice.installer("--llm", root=root, platform=platform)
    if script is None:
        report.error(problem or "no installer")
        return REFUSED
    if not _have_llama(settings, probes, report, platform=platform):
        return FAILED
    if not serve_here(settings, report, run_script=probes.run_script,
                      wait_for_server=probes.wait_for_server, root=root, platform=platform):
        return FAILED
    here = endpoint(settings)
    with report.step("check", f"Asking the model server which model it serves, {alias}"), \
            _removed_if_stopped(probes.run_script, root=root, platform=platform):
        _up, served = probes.served(here.base_url)
    if served != alias:
        removed = remove_unit(probes.run_script, root=root, platform=platform)
        report.error(f"the model server at {here.base_url} serves {served or 'no model it names'}"
                     f", not {alias}; {removed}", log=log_path(settings))
        return FAILED
    report.done(here.base_url, served)
    return OK


def _not_ready(settings: Settings) -> str | None:
    """Why `LLM_SERVER_MODEL` cannot be served yet, or None: unchosen, or not on disk whole."""
    entry = catalog.by_key(settings.llm_server_model or "")
    if entry is not None and not download.is_downloaded(settings.cache_dir, entry):
        return f"{entry.key} is not downloaded (keryx models pull {entry.key})"
    try:
        servers.llm_model(settings)
    except servers.ServerConfigError as error:
        return str(error)
    return None


def _have_llama(settings: Settings, probes: Probes, report: StartReport, *, platform: str
                ) -> bool:
    """llama.cpp's `llama-server` here, installed when it was missing; False once told."""
    found = probes.llama_server(settings.cache_dir)
    if found is None:
        brew = uses_brew(platform=platform, which=probes.which)
        how = "with Homebrew" if brew else f"build {LLAMA_CPP_BUILD}, from its releases"
        with report.step("runtime", f"Installing llama.cpp {how}"):
            try:
                install_llama(settings.cache_dir, probes.hardware(), brew=brew,
                              run_script=probes.run_script,
                              install=lambda cache, machine: probes.download_llama_cpp(
                                  cache, machine, report.progress))
            except InstallError as error:
                report.error(str(error))
                return False
        found = probes.llama_server(settings.cache_dir)
        if found is None:
            report.error("llama.cpp was installed, but its llama-server cannot be found: "
                         "not on PATH, nor in CACHE_DIR/llama.cpp")
            return False
    with report.step("runtime", f"llama.cpp: {found}"):
        pass
    return True


def _start_ollama(settings: Settings, probes: Probes, report: StartReport, *,
                  save: Callable[[dict[str, Any]], bool], root: Path, platform: str) -> int:
    """Ollama's own copy of the chosen catalog model, made the local agent's once listed —
    or, once Ollama serves it, the one it serves — and the llama.cpp server's unit removed."""
    entry = _ollama_entry(settings)
    if entry is None:
        report.error(f"{NOT_READY}: Ollama runs a model from Keryx's list (keryx models "
                     "list), and LLM_SERVER_MODEL names none")
        return REFUSED
    with report.step("runtime", "Looking for Ollama"):
        found = probes.ollama()
    if found is None:
        report.error("Ollama is not installed: install it from ollama.com, or run the model "
                     "with llama.cpp (--runtime llama)")
        return FAILED
    with report.step("start", f"Pulling {entry.title} into Ollama ({entry.size_gb} GB)"):
        problem = pull_into_ollama(entry, probes.run_script)
    if problem is not None:
        report.error(problem)
        return FAILED
    here = Endpoint.parse(f"{OLLAMA_URL}/v1")
    with report.step("check", f"Asking Ollama for {entry.ollama}"):
        listed = probes.models(here)
    if entry.ollama not in listed.models:
        why = listed.problem or f"it lists {', '.join(listed.models) or 'nothing'}"
        report.error(f"Ollama does not list {entry.ollama}: {why}")
        return FAILED
    if not save(ollama_values(entry)):
        return FAILED
    if probes.unit_installed("llm"):
        with report.step("unit", "Removing the llama.cpp model server's service: Ollama "
                                 "serves the model now"):
            said = retire_llama(probes.run_script, installed=True, root=root,
                                platform=platform)
        if said != REMOVED:
            report.warn(f"the llama.cpp model server: {said}")
    report.done(here.base_url, entry.ollama)
    return OK


def on_ollama(settings: Settings) -> bool:
    """Whether the local agent's model is Ollama's, on this machine."""
    current = settings.local_agent_endpoint
    return current is not None and current.base_url == f"{OLLAMA_URL}/v1"


def _ollama_entry(settings: Settings) -> ModelEntry | None:
    """The catalog model to serve under Ollama: the chosen one, else the one it serves."""
    entry = catalog.by_key(settings.llm_server_model or "")
    if entry is None and on_ollama(settings):
        entry = next((e for e in catalog.MODELS if e.ollama == settings.local_agent_model),
                     None)
    return entry


# --- the command line ----------------------------------------------------------------------


@dataclass(frozen=True)
class Machine:
    """This machine, as `Probes`."""

    llama_server: Callable[[Path], str | None]
    ollama: Callable[[], str | None]
    which: Callable[[str], str | None]
    hardware: Callable[[], Hardware]
    download_llama_cpp: Callable[[Path, Hardware, Callable[[int, int | None], None]], str]
    run_script: Callable[[Sequence[str]], int]
    wait_for_server: Callable[[Endpoint, str, float], str | None]
    served: Callable[[str], tuple[bool, str | None]]
    models: Callable[[Endpoint], Probe]
    unit_installed: Callable[[str], bool]


def machine() -> Machine:
    """The real machine, each probe looked up now, through its module. A server here is
    never asked through a proxy, which would carry even a loopback request off the machine."""
    install = runtimes.install_llama_cpp
    return Machine(
        llama_server=runtimes.llama_server, ollama=runtimes.ollama, which=runtimes.which,
        hardware=hardware.detect,
        download_llama_cpp=lambda cache, machine, progress: install(
            cache, machine, get=partial(runtimes.fetch, progress=progress)),
        run_script=voice.run_detached, wait_for_server=servers.wait_for, served=use.probe,
        models=partial(endpoints.probe, get=partial(httpx.get, trust_env=False)),
        unit_installed=use.unit_installed,
    )


class TextSteps(voice.TextSteps):
    """For a terminal: each step a line, what stopped it on stderr, and where it ended."""

    def progress(self, done: int, total: int | None) -> None:
        pass  # llama.cpp's archive is a few tens of megabytes: the step's line says it

    def done(self, url: str, served_model: str) -> None:  # type: ignore[override]
        self._echo(f"the model server is running at {url}, serving {served_model}; "
                   "`keryx restart` to have Keryx take it up")


class JsonSteps(voice.JsonSteps):
    """For the Mac app: a `step` before each step's slow work, `progress` while llama.cpp
    downloads (`total` null when the server does not say), then exactly one of `done`
    (`url`, `served_model`), `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__(write)
        self._current = STEPS[0]

    def progress(self, done: int, total: int | None) -> None:
        self._progressed(done, total, step=self._current)

    def done(self, url: str, served_model: str) -> None:  # type: ignore[override]
        self._end({"event": "done", "url": url, "served_model": served_model})
