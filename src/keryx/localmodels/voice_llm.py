"""The voice LLM: where the words of a voice running here come from (`VOICE_LLM`), set up —
`keryx voice llm shared|here|remote`, `keryx models start --voice-llm`, and the first
`keryx voice use here`, which chooses one by the owner's rule (`first_choice`).

`here` is the long one, and it keeps the voice working until the new one answers: the model
downloaded when it is not on disk (`keryx models pull`'s own steps), llama.cpp installed when
it is missing, the `--voice-llm` unit installed, the wait for it to load, and a check that it
serves that model — and only then `VOICE_LLM=here` saved. The unit reads its model and port
from the settings, so those two are saved before it starts, and put back as they were when
it does not come up; a unit that never came up is removed before anything is said
(`voice.removed_if_stopped`), as every model server's is. `serve_here` is that unit, its
wait and its check, which `keryx models start --voice-llm` runs alone for the model already
chosen.

`remote` asks the server for its models with the key it would be sent, and saves it only
once it answers; `shared` saves the local agent's model as the words. Both then remove the
voice LLM's own unit when it is installed: nothing reads it any more, and it holds a model
in memory.

None of them restarts a running voice server, whose command line was built with the old
words: each ending says whether one is running (`voice_restart_needed`), and `keryx voice
use here` restarts it, with its own checks.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, fields, replace
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import httpx

from keryx import endpoints
from keryx.child_env import redact_known
from keryx.config import Settings
from keryx.config.store import FROM_DEFAULT
from keryx.endpoints import Endpoint, Probe
from keryx.localmodels import catalog, download, pull, server, servers, use, voice
from keryx.localmodels.catalog import ModelEntry
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.servers import HOST
from keryx.localmodels.voice import FAILED, OK, REFUSED

#: The voice LLM's unit, as the installers and `voice.remove_unit` name it.
FLAG = "--voice-llm"
#: Its server's own log, which the unit writes.
LOG_NAME = "keryx-voice-llm.err.log"
#: The steps of `here`, in order, as the JSON lines name them; `shared` tells `configure`
#: and `unit`, `remote` `check`, `configure` and `unit`.
STEPS = ("download", "runtime", "unit", "start", "check")
#: The one step `keryx voice use here` tells the whole of a first set-up's voice LLM under.
VOICE_STEP = "voice_llm"
#: What `models start --voice-llm` says, first, while there is no model it could serve.
NOT_READY = "Choose and download the voice LLM's model first."
#: Said while the voice runs here and would be left with no words at all.
NO_WORDS_LEFT = ("The local agent has no model, so the voice here would have no words: "
                 "set one up first.")


class Report(server.Report, Protocol):
    """How a voice LLM set-up is told: its steps, a download's bytes, and how it ended."""

    def progress(self, done: int, total: int | None) -> None: ...

    def done(self, source: str, model: str | None, url: str | None, *,
             voice_restart_needed: bool) -> None: ...


class Steps(server.Report, Protocol):
    """…the steps without the ending: what the first `voice use here` hands a set-up."""

    def progress(self, done: int, total: int | None) -> None: ...


class Probes(server.Probes, Protocol):
    """What these ask of the machine: a model server's, and a free port, a download and a
    server elsewhere's model list."""

    free_port: Callable[[int], int]
    pull: Callable[[Settings, str, pull.Report], int]
    remote_models: Callable[[Endpoint], Probe]


# --- the owner's rule for a new install ------------------------------------------------------


def first_set_up(settings: Settings, source: str) -> bool:
    """Whether this is a local voice's first set-up, the one time Keryx chooses the voice LLM
    itself: `VOICE_LLM` never set (its `source` is the default) and the voice not here yet.
    An existing install keeps `shared`, as it was; a `VOICE_LLM` set by anyone is never
    changed."""
    return source == FROM_DEFAULT and settings.voice_mode != voice.HERE


def first_choice(settings: Settings, hardware: Hardware) -> ModelEntry | None:
    """The voice LLM a first set-up runs (D2, `catalog.voice_llm_pick`): a small model of its
    own when it fits beside the agent's model and the speech models, else None — shared."""
    return catalog.voice_llm_pick(hardware, agent_gb=use.agent_memory_gb(settings))


# --- here ------------------------------------------------------------------------------------


def endpoint(settings: Settings) -> Endpoint:
    """The voice LLM's own server on this machine, at `VOICE_LLM_SERVER_PORT`."""
    return Endpoint.parse(f"http://{HOST}:{settings.voice_llm_server_port}/v1")


def log_path(settings: Settings) -> Path:
    return settings.state_dir / "logs" / LOG_NAME


def here(
    settings: Settings, model: str, probes: Probes, report: Report, *,
    save: Callable[[dict[str, Any]], bool], root: Path, platform: str | None = None,
) -> int:
    """Make `model` (a catalog name or a GGUF's path) the voice LLM, on a server of its own
    here; the exit code. 2 for what refuses it before anything is tried — a model that is
    not there, no installer, no port of its own — 1 for what stopped it, 0 once the server
    serves it and `VOICE_LLM=here` is saved."""
    code, url = _bring_up(settings, model, probes, report, save=save, root=root,
                          platform=platform)
    if code == OK:
        report.done("here", model, url, voice_restart_needed=_voice_running(settings, probes))
    return code


def bring_up(
    settings: Settings, model: str, probes: Probes, report: Steps, *,
    save: Callable[[dict[str, Any]], bool], root: Path, platform: str | None = None,
) -> int:
    """`here` without its ending: what the first `voice use here` runs before the voice."""
    return _bring_up(settings, model, probes, report, save=save, root=root,
                     platform=platform)[0]


def _bring_up(
    settings: Settings, model: str, probes: Probes, report: Steps, *,
    save: Callable[[dict[str, Any]], bool], root: Path, platform: str | None,
) -> tuple[int, str]:
    """`bring_up`'s exit code, and the address its server answers at."""
    platform = platform or voice.PLATFORM
    entry, problem, detail = _model(model)
    if problem is not None:
        report.error(problem, detail=detail)
        return REFUSED, ""
    script, problem = voice.installer(FLAG, root=root, platform=platform)
    if script is None:
        voice.no_installer(report, problem)
        return REFUSED, ""
    try:
        chosen = _chosen(settings, model, probes)
    except OSError as error:
        report.error("There is no free port for the voice LLM's server.", detail=str(error))
        return REFUSED, ""
    if clash := servers.port_clash(chosen):
        report.error(clash)
        return REFUSED, ""
    url = endpoint(chosen).base_url
    _warn_if_too_big(settings, entry, probes, report)
    if entry is not None and not download.is_downloaded(settings.cache_dir, entry):
        if probes.pull(settings, entry.key, _Pulled(report)) != 0:
            return FAILED, url
    if not server.have_llama(settings, probes, report, platform=platform):
        return FAILED, url
    # The unit reads its model and port from the settings: saved first, put back on failure.
    before = {"VOICE_LLM_MODEL": settings.voice_llm_model,
              "VOICE_LLM_SERVER_PORT": (settings.voice_llm_server_port
                                        if "voice_llm_server_port" in settings.model_fields_set
                                        else None)}
    port = chosen.voice_llm_server_port
    if not save({"VOICE_LLM_MODEL": model, "VOICE_LLM_SERVER_PORT": port}):
        return FAILED, url
    served = False
    try:
        served = serve_here(chosen, probes, report, root=root, platform=platform) == OK
    finally:
        if not served:
            save(before)
            if settings.voice_llm == "here" and settings.voice_llm_model not in (None, model):
                report.warn("The voice LLM it replaced was stopped with it, and is still "
                            "chosen: starting its server again brings it back.",
                            detail="`keryx models start --voice-llm`")
    if not served:
        return FAILED, url
    with report.step("configure", "Saving the voice LLM: its own server here"):
        if not save({"VOICE_LLM": "here", "VOICE_LLM_MODEL": model,
                     "VOICE_LLM_SERVER_PORT": port}):
            return FAILED, url
    return OK, url


def serve_here(
    settings: Settings, probes: server.Probes, report: server.Report, *, root: Path,
    platform: str | None = None,
) -> int:
    """Install the voice LLM's unit (`VOICE_LLM_MODEL` on `VOICE_LLM_SERVER_PORT`), wait for
    it to load, and check that it serves that model; the exit code, 0 or 1. A server that
    does not come up, serves another model, or is cancelled on the way has its unit removed
    before anything is said."""
    here_ = endpoint(settings)
    alias = settings.voice_llm_model or ""
    title = _title(alias)
    served: str | None = None
    with voice.removed_if_stopped(FLAG, probes.run_script, root=root, platform=platform):
        if not voice.install_unit_step(FLAG, "Installing the voice LLM server's service",
                                       probes.run_script, report, root=root, platform=platform):
            return FAILED
        with report.step("start", f"Loading {title} into llama.cpp at {here_.base_url}"):
            problem = probes.wait_for_server(here_, "models", server.LLM_START_S)
        if problem is None:
            with report.step("check", f"Asking the voice LLM's server which model it serves, "
                                      f"{title}"):
                _up, served = probes.served(here_.base_url)
    if problem is not None:
        removed = voice.remove_unit(FLAG, probes.run_script, root=root, platform=platform)
        report.error(voice.did_not_come_up("The voice LLM server", removed),
                     detail=f"{problem}; {removed}", log=log_path(settings))
        return FAILED
    if served != alias:
        removed = voice.remove_unit(FLAG, probes.run_script, root=root, platform=platform)
        gone = "so its service was removed" if removed == voice.REMOVED else (
            "and its service could not be removed")
        report.error(f"The voice LLM server serves another model, not {title}, {gone}.",
                     detail=f"{here_.base_url} serves {served or 'no model it names'}; "
                            f"{removed}", log=log_path(settings))
        return FAILED
    return OK


def start(
    settings: Settings, probes: server.Probes, report: server.StartReport, *, root: Path,
    platform: str | None = None,
) -> int:
    """`keryx models start --voice-llm`: the voice LLM's own server for the model already
    chosen (`VOICE_LLM_MODEL`), started and checked as `here` does it — nothing is saved.
    2 until a model is chosen and on disk, or with no installer or no port of its own."""
    platform = platform or voice.PLATFORM
    if problem := _not_ready(settings):
        report.error(NOT_READY, detail=problem)
        return REFUSED
    script, problem = voice.installer(FLAG, root=root, platform=platform)
    if script is None:
        voice.no_installer(report, problem)
        return REFUSED
    if clash := servers.port_clash(settings):
        report.error(clash)
        return REFUSED
    if not server.have_llama(settings, probes, report, platform=platform):
        return FAILED
    if serve_here(settings, probes, report, root=root, platform=platform) != OK:
        return FAILED
    report.done(endpoint(settings).base_url, settings.voice_llm_model or "")
    return OK


def _model(model: str) -> tuple[ModelEntry | None, str | None, str | None]:
    """`model` as a catalog entry (None for a GGUF of your own), or why it cannot be one,
    in a sentence and its detail."""
    entry = catalog.by_key(model)
    if entry is not None:
        return entry, None, None
    path = Path(model).expanduser()
    if path.suffix == ".gguf" or "/" in model:
        if path.is_file():
            return None, None, None
        return None, "That GGUF file is not there.", model
    return None, f"There is no model called {model}.", (
        "`keryx models list` names them; a GGUF file of your own is given by its path")


def _not_ready(settings: Settings) -> str | None:
    """Why `VOICE_LLM_MODEL` cannot be served yet, or None: unchosen, or not on disk whole."""
    entry = catalog.by_key(settings.voice_llm_model or "")
    if entry is not None and not download.is_downloaded(settings.cache_dir, entry):
        return f"{entry.key} is not downloaded (keryx models pull {entry.key})"
    try:
        servers.voice_llm_model(settings)
    except servers.ServerConfigError as error:
        return str(error)
    return None


def _chosen(settings: Settings, model: str, probes: Probes) -> Settings:
    """The settings with `model` on a port of its own: the one it has while its unit is
    installed — the port is taken by its own server — else one found free from
    `VOICE_LLM_SERVER_PORT` up (`use.voice_llm_port`); `OSError` when none is."""
    if probes.unit_installed("voice_llm"):
        port = settings.voice_llm_server_port
    else:
        port = use.voice_llm_port(settings, free_port=probes.free_port)
    return settings.model_copy(update={"voice_llm_model": model, "voice_llm_server_port": port})


def _title(model: str) -> str:
    """How a person reads `model`: its catalog title, or "its model" for a file's path."""
    entry = catalog.by_key(model)
    return entry.title if entry is not None else "its model"


def _warn_if_too_big(settings: Settings, entry: ModelEntry | None, probes: Probes,
                     report: Steps) -> None:
    """A warning, never a refusal, when `entry` beside the agent's model and the speech
    models needs more than this machine gives a model: the owner may know better."""
    agent = use.agent_memory_gb(settings)
    if entry is None or agent is None:
        return
    need, have = agent + entry.memory_gb + catalog.VOICE_SERVER_GB, probes.hardware()
    if need > have.model_memory_gb:
        report.warn(f"{entry.title}, the agent's model and the speech models need about "
                    f"{need:g} GB together, and this machine gives models "
                    f"{have.model_memory_gb:g} GB: they may not all fit at once.")


class _Pulled:
    """`keryx models pull`'s report, told as this set-up's `download` step: its bytes as
    progress, and what stopped it as the error."""

    def __init__(self, report: Steps) -> None:
        self._report = report

    def error(self, message: str, *, detail: str | None = None) -> None:
        self._report.error(message, detail=detail)

    def adopted(self, path: Path, found: Path) -> None:
        with self._report.step("download", "The model is already on this machine: Keryx uses "
                                           "that copy", detail=str(found)):
            pass

    def done(self, path: Path) -> None:
        pass

    @contextmanager
    def downloading(self, title: str, total: int) -> Iterator[download.Progress]:
        with self._report.step("download", f"Downloading {title} for the voice LLM "
                                           f"({total / 1e9:.1f} GB)"):
            yield lambda done: self._report.progress(done, total)


# --- remote and shared -----------------------------------------------------------------------


def remote(
    settings: Settings, url: str, model: str, probes: Probes, report: Report, *,
    key: str | None, new_key: bool, save: Callable[[dict[str, Any]], bool], root: Path,
    platform: str | None = None,
) -> int:
    """Make a server elsewhere the voice LLM, once it lists `model`; the exit code. `key` is
    the key it is to have (the stored one, one typed, or None once cleared), saved only when
    `new_key`; it is asked with that, or with `OPENAI_API_KEY` on OpenAI's own host alone
    (`Settings.voice_llm_key`'s rule). 2 for an address that is none, 1 for a server that
    does not answer or does not list the model, 0 once saved."""
    try:
        where = Endpoint.parse(url, model=model)
    except ValueError as error:  # its own words, never the address: it may hold a password
        report.error("That is not an address Keryx can use.", detail=f"VOICE_LLM_BASE_URL {error}")
        return REFUSED
    if not model.strip():
        report.error("Name the model the voice LLM's server is to run.", detail="--model")
        return REFUSED
    sent = key or (settings.openai_key if where.is_openai else None)
    asked = replace(where, api_key=sent)
    for warning in endpoints.warnings(asked):
        report.warn(warning)
    with report.step("check", f"Asking {where.host} for its models"):
        listed = probes.remote_models(asked)
    if not listed.ok:
        report.error("The voice LLM's server did not answer.",
                     detail=redact_known(listed.problem or where.host, settings))
        return FAILED
    if listed.models and model not in listed.models:
        report.error(f"{where.host} does not list {model}.",
                     detail=f"it lists {', '.join(listed.models[:20])}")
        return FAILED
    values: dict[str, Any] = {"VOICE_LLM": "remote", "VOICE_LLM_BASE_URL": where.base_url,
                              "VOICE_LLM_MODEL": model}
    if new_key:
        values["VOICE_LLM_API_KEY"] = key
    with report.step("configure", f"Saving the voice LLM: {model} at {where.host}"):
        if not save(values):
            return FAILED
    _retire(probes, report, root=root, platform=platform)
    report.done("remote", model, where.base_url,
                voice_restart_needed=_voice_running(settings, probes))
    return OK


def shared(
    settings: Settings, probes: Probes, report: Report, *,
    save: Callable[[dict[str, Any]], bool], root: Path, platform: str | None = None,
) -> int:
    """Make the local agent's model the voice LLM again; the exit code. 2 while the voice
    runs here and the local agent has no model, which would leave it with no words."""
    agent = settings.local_agent_endpoint
    model = agent.model if agent is not None and agent.model else None
    if model is None and settings.voice_mode == voice.HERE:
        report.error(NO_WORDS_LEFT, detail=voice.NO_WORDS_HINT)
        return REFUSED
    with report.step("configure", "Saving the voice LLM: the local agent's model"):
        if not save({"VOICE_LLM": "shared"}):
            return FAILED
    if model is None:
        report.warn("The local agent has no model yet: a voice here would have no words "
                    "until it has one.", detail=voice.NO_WORDS_HINT)
    _retire(probes, report, root=root, platform=platform)
    report.done("shared", model, agent.base_url if agent is not None else None,
                voice_restart_needed=_voice_running(settings, probes))
    return OK


def _retire(probes: Probes, report: Steps, *, root: Path, platform: str | None) -> None:
    """The voice LLM's own unit removed when it is installed (step `unit`): nothing reads it
    once the words come from elsewhere, and it holds its model in memory. One that will not
    go is a warning — the choice is already saved."""
    if not probes.unit_installed("voice_llm"):
        return
    with report.step("unit", "Removing the voice LLM's own server: nothing uses it now"):
        said = voice.remove_unit(FLAG, probes.run_script, root=root, platform=platform)
    if said != voice.REMOVED:
        voice.warn_removed(report, FLAG, said)


def _voice_running(settings: Settings, probes: server.Probes) -> bool:
    """Whether a voice server here runs with the words it started with: the voice is here
    and its unit is installed, so the new words wait for its next start."""
    return settings.voice_mode == voice.HERE and probes.unit_installed("voice")


# --- the command line ----------------------------------------------------------------------


@dataclass(frozen=True)
class Machine(server.Machine):
    """This machine, as `Probes`."""

    free_port: Callable[[int], int]
    pull: Callable[[Settings, str, pull.Report], int]
    remote_models: Callable[[Endpoint], Probe]


def machine() -> Machine:
    """The real machine: a model server's (`server.machine`), and a server elsewhere asked
    through a proxy only when it is not on this machine."""
    base = server.machine()

    def remote_models(where: Endpoint) -> Probe:
        return endpoints.probe(where, get=partial(httpx.get, trust_env=not where.is_loopback))

    return Machine(**{each.name: getattr(base, each.name) for each in fields(base)},
                   free_port=servers.free_port, pull=pull.pull, remote_models=remote_models)


class Under:
    """A set-up's steps told as one step of another stream (`VOICE_STEP` in `keryx voice use
    here`): one row in the app, its message changing as it goes."""

    def __init__(self, report: Any, step: str) -> None:
        self._report, self._step = report, step

    def step(self, step: str, message: str, *,
             detail: str | None = None) -> AbstractContextManager[None]:
        return self._report.step(self._step, message, detail=detail)

    def warn(self, message: str, *, detail: str | None = None) -> None:
        self._report.warn(message, detail=detail)

    def progress(self, done: int, total: int | None) -> None:
        self._report.progress(done, total)

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None:
        self._report.error(message, log=log, detail=detail)


class TextSteps(voice.TextSteps):
    """For a terminal: each step a line, the bytes every tenth of the way, and the ending."""

    def done(self, source: str, model: str | None, url: str | None, *,  # type: ignore[override]
             voice_restart_needed: bool) -> None:
        where = {"shared": "the local agent's model", "here": f"{model}, on its own server here",
                 "remote": f"{model} at {url}"}[source]
        self._echo(f"the voice's words now come from {where}")
        if voice_restart_needed:
            self._echo("the voice server here takes them up when it starts again: "
                       "`keryx voice use here` restarts it")


class JsonSteps(voice.JsonSteps):
    """For the Mac app: a `step` before each step's slow work, `progress` while the model
    downloads, then exactly one of `done` (`source`, `model`, `url`,
    `voice_restart_needed`), `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None], *, first: str = STEPS[0]) -> None:
        super().__init__(write)
        self._current = first

    def done(self, source: str, model: str | None, url: str | None, *,  # type: ignore[override]
             voice_restart_needed: bool) -> None:
        self._end({"event": "done", "source": source, "model": model, "url": url,
                   "voice_restart_needed": voice_restart_needed})
