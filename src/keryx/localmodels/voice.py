"""The voice on a call: where it is, and setting it up — `keryx voice`, `keryx voice use …`
and the wizard's Local models section, which share every step here.

Three places a call's voice can be, as `mode` names them: OpenAI's Realtime API (`openai`,
`VOICE_BASE_URL` blank), speech-to-speech on this machine (`here`, `VOICE_BASE_URL` the
loopback address of `VOICE_SERVER_PORT`) and a server elsewhere (`server`).

`here` is the long one: install speech-to-speech when it is missing, save its voice and
port, install its unit, wait for it to answer a Realtime handshake, and only then save it as
the voice — a `VOICE_BASE_URL` that does not answer is a phone that rings and says nothing.
It refuses while the local model server, which the voice server takes its words from, does
not answer: on 2026-10-07 a voice server installed without one crash-looped under launchd's
KeepAlive on its first request for words. And a server that does not come up, or whose
start is cancelled, has its unit removed before anything else is said, so no unit is ever
left restarting in a loop (`removed_if_stopped`, which `keryx models start` shares).
Each step is told to a `Report` before its slow work starts; the wizard tells it as notes and
a spinner, `keryx voice use here --json` as JSON lines (`JsonSteps`, on pull's `EventLines`,
so it ends exactly once on every path). Every install, port, disk and network question is
asked through `Probes`: the wizard's own, or `machine()` for the command line.
"""

import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from keryx.config import Settings
from keryx.config.store import FROM_DEFAULT, ConfigStore
from keryx.endpoints import Endpoint, warnings
from keryx.localmodels import catalog, hardware, runtimes, servers, use
from keryx.localmodels.catalog import VOICE_SERVER_DOWNLOAD_GB
from keryx.localmodels.pull import EventLines
from keryx.localmodels.servers import HOST

OPENAI, HERE, SERVER = "openai", "here", "server"
#: How long the voice server may take its first start, which downloads its speech models.
VOICE_START_S = 1200.0
#: How long a voice server elsewhere may take to say it is there, for `keryx voice`: a
#: Realtime handshake is too slow for a poll, so its model list is asked instead.
SERVER_REACH_S = 2.0
#: What `needs` says while there is no local model for the voice server's words.
NEEDS_WORDS = "local_agent_model"
NO_WORDS = "set up a local model first: keryx models use NAME"
#: What `here` says, refusing, while the server its words come from does not answer.
NO_WORDS_SERVER = "The local model server isn't running: start it first."
#: Said of a unit removed rather than left restarting a server that did not come up.
REMOVED = "its service was removed, so it does not restart in a loop"
#: Keryx's own installers, the only commands `run_detached` gives a deadline: a few
#: `keryx config get`s and the service manager's answers, with room for `uv` to sync the
#: environment first. A download (uv, brew, ollama) takes as long as it takes.
INSTALLERS = frozenset({"install-launchd.sh", "install-systemd.sh"})
INSTALL_TIMEOUT_S = 120.0
#: The exit code said for an installer stopped at its deadline, as `timeout(1)` says it.
TIMED_OUT = 124
#: The exit codes of a set-up: a refusal is one nothing was tried for.
OK, FAILED, REFUSED = 0, 1, 2
#: The platform whose service manager installs the units, unless a caller says another.
PLATFORM = sys.platform
#: A Kokoro voice's name: `af_heart`, `bm_george`, `zf_xiaoxiao`.
VOICE_NAME = re.compile(r"[a-z]{2}_[a-z0-9]+")
#: The steps of `here`, in order, as the JSON lines name them.
STEPS = ("install", "configure", "unit", "start")


class Probes(Protocol):
    """What `here` and `check_server` ask of the machine; `keryx setup`'s `Probes` is one."""

    speech_to_speech: Callable[[], str | None]
    free_bytes: Callable[[Path], int]
    run_script: Callable[[Sequence[str]], int]
    free_port: Callable[[int], int]
    wait_for_server: Callable[[Endpoint, str, float], str | None]
    realtime_problem: Callable[[Endpoint], str | None]
    words_up: Callable[[Endpoint | None], bool]


class Steps(Protocol):
    """How the work is told while it runs: each step before its slow work, and warnings."""

    def step(self, step: str, message: str) -> AbstractContextManager[None]: ...

    def warn(self, message: str) -> None: ...


class Report(Steps, Protocol):
    """…and how a set-up ended: the address it saved, or what stopped it (and its log)."""

    def error(self, message: str, *, log: Path | None = None) -> None: ...

    def done(self, base_url: str) -> None: ...


# --- where the voice is --------------------------------------------------------------------


def is_ours(voice_base_url: str | None, port: int) -> bool:
    return voice_base_url == f"http://{HOST}:{port}/v1"


def mode(settings: Settings) -> str:
    if is_ours(settings.voice_base_url, settings.voice_server_port):
        return HERE
    return SERVER if settings.voice_base_url else OPENAI


def words_model(settings: Settings) -> str | None:
    """The local agent's model, which the voice server here takes its words from."""
    endpoint = settings.local_agent_endpoint
    return endpoint.model if endpoint is not None and endpoint.model else None


def status(
    settings: Settings,
    *,
    program: Callable[[], str | None],
    installed: Callable[[str], bool],
    probe: Callable[[str], tuple[bool, str | None]],
    server_reachable: Callable[[Endpoint], bool],
) -> dict[str, Any]:
    """`keryx voice --json`. The voice server here is asked on loopback only (`probe`); a
    server elsewhere only when it is the voice. No key is in it."""
    where = mode(settings)
    port = settings.voice_server_port
    words = words_model(settings)
    found = program()
    return {
        "mode": where,
        "base_url": settings.voice_base_url or None,
        "realtime_model": settings.openai_realtime_model,
        "voice": settings.voice_server_voice if where == HERE else settings.voice,
        "assistant": settings.assistant_name,
        "openai_key_set": settings.openai_key is not None,
        "here": {
            "installed": found is not None,
            "program": found,
            "voice": settings.voice_server_voice,
            "port": port,
            "unit_installed": installed("voice"),
            "reachable": probe(f"http://{HOST}:{port}/v1")[0],
            "words_model": words,
        },
        "needs": NEEDS_WORDS if words is None else None,
        "server": {
            "reachable": server_reachable(settings.voice_endpoint) if where == SERVER else None,
        },
    }


def words_up(endpoint: Endpoint | None) -> bool:
    """Whether the server the voice server takes its words from (the local agent's) answers:
    one on this machine through `use.probe`, on loopback and never through a proxy; one
    elsewhere as a voice server elsewhere is asked, with its key."""
    if endpoint is None:
        return False
    if endpoint.is_loopback:
        return use.probe(endpoint.base_url)[0]
    return server_reachable(endpoint)


def server_reachable(endpoint: Endpoint, *, get: Callable[..., httpx.Response] | None = None
                     ) -> bool:
    """Whether a voice server elsewhere answers `GET {base}/models` with its key, within
    `SERVER_REACH_S`: any answer short of a server error, since a Realtime server need not
    list models — but not a proxy's 407, which is the proxy answering. One on loopback (an
    ssh tunnel) is never asked through a proxy."""
    try:
        response = (get or httpx.get)(
            endpoint.url("models"), headers=endpoint.headers(), timeout=SERVER_REACH_S,
            trust_env=not endpoint.is_loopback,
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 500 and response.status_code != 407


def current_voice(settings: Settings, store: ConfigStore) -> str:
    """The voice to offer first: the one chosen before, else the assistant's own."""
    names = [name for name, _sounds in catalog.VOICES]
    chosen = store.source_of("VOICE_SERVER_VOICE", settings) != FROM_DEFAULT
    if chosen and settings.voice_server_voice in names:
        return settings.voice_server_voice
    return catalog.persona_voice(settings.assistant_name)


# --- OpenAI, and a server elsewhere --------------------------------------------------------


def to_openai() -> dict[str, Any]:
    """What makes OpenAI's Realtime API the voice again."""
    return {"VOICE_BASE_URL": None, "VOICE_API_KEY": None}


@dataclass(frozen=True)
class ServerCheck:
    """A voice server elsewhere, as asked: what is worth knowing about where it is, and why
    it would not open a Realtime session (None when it did)."""

    endpoint: Endpoint
    warnings: list[str]
    problem: str | None

    def values(self, *, with_key: bool = True) -> dict[str, Any]:
        """What makes it the voice; without `with_key`, the stored key is left as it is."""
        values: dict[str, Any] = {"VOICE_BASE_URL": self.endpoint.base_url}
        if with_key:
            values["VOICE_API_KEY"] = self.endpoint.api_key or None
        return values


def check_server(url: str, key: str | None, *, probes: Probes, report: Steps) -> ServerCheck:
    """Parse `url` (`ValueError` for one that is no address, or carries a password), say
    its warnings, and open a Realtime session on it. Nothing is saved."""
    endpoint = Endpoint.parse(url, api_key=key or None)
    found = warnings(endpoint, carries_voice=True)
    for warning in found:
        report.warn(warning)
    with report.step("check", f"Opening a Realtime session on {endpoint.host}"):
        problem = probes.realtime_problem(endpoint)
    return ServerCheck(endpoint, found, problem)


# --- this machine --------------------------------------------------------------------------


def here(
    settings: Settings,
    probes: Probes,
    report: Report,
    *,
    choose_voice: Callable[[], str],
    save: Callable[[dict[str, Any]], bool],
    root: Path,
    no_words: str = NO_WORDS,
    platform: str | None = None,
) -> int:
    """Make speech-to-speech on this machine the voice; the exit code.

    `choose_voice` is asked once speech-to-speech is installed; `save` stores values as
    whoever is running this and says why when it cannot (False). `root` is the checkout
    whose `scripts/` installs the unit. 2 for what refuses it before anything is tried —
    no local model for the words, or none answering, no installer for the unit — 1 for
    anything that stopped it, 0 once the server answered and was saved as the voice. A
    half-done install is left for the next run to finish; a unit whose server did not come
    up, or whose start was cancelled, is not.
    """
    if words_model(settings) is None:
        report.error(no_words)
        return REFUSED
    if not probes.words_up(settings.local_agent_endpoint):
        report.error(NO_WORDS_SERVER)
        return REFUSED
    script, problem = installer("--voice", root=root, platform=platform)
    if script is None:
        report.error(problem or "no installer")
        return REFUSED
    if probes.speech_to_speech() is None:
        free = probes.free_bytes(settings.cache_dir)
        if free < (VOICE_SERVER_DOWNLOAD_GB + 2) * 1e9:
            report.error(f"The voice server needs about {VOICE_SERVER_DOWNLOAD_GB + 2:.0f} GB "
                         f"free for itself and its speech models; there is "
                         f"{free / 1e9:.1f} GB.")
            return FAILED
        with report.step("install", "Installing speech-to-speech, Hugging Face's voice "
                                    "pipeline, with uv"):
            code = probes.run_script(runtimes.s2s_install_argv())
        if code != 0:
            report.error("the install did not finish")
            return FAILED
    voice = choose_voice()
    port = settings.voice_server_port
    with report.step("configure", f"Saving the voice server's voice, {voice}, and its port"):
        if voice not in [name for name, _sounds in catalog.VOICES]:
            report.warn(f"{voice} is not one of the Kokoro voices Keryx lists; Kokoro takes "
                        "others, so it is kept — `keryx models list` shows the listed ones")
        if not is_ours(settings.voice_base_url, port):
            port = probes.free_port(port)
        if not save({"VOICE_SERVER_VOICE": voice, "VOICE_SERVER_PORT": port}):
            return FAILED
    with report.step("unit", "Installing the voice server's service"):
        code = probes.run_script([str(script), "--voice"])
    if code != 0:
        report.error(exited(f"{script.name} --voice", code))
        return FAILED
    endpoint = Endpoint.parse(f"http://{HOST}:{port}")
    with report.step("start", "Starting the voice server — its first start downloads its "
                              "speech models, which takes a few minutes"), \
            removed_if_stopped("--voice", probes.run_script, root=root, platform=platform):
        problem = probes.wait_for_server(endpoint, "realtime", VOICE_START_S)
    if problem is not None:
        removed = remove_unit("--voice", probes.run_script, root=root, platform=platform)
        report.error(f"the voice server did not come up: {problem}; {removed}",
                     log=settings.state_dir / "logs" / "keryx-voice.err.log")
        return FAILED
    if not save({"VOICE_BASE_URL": endpoint.base_url, "VOICE_API_KEY": None}):
        return FAILED
    report.done(endpoint.base_url)
    return OK


def installer(
    flag: str, *, root: Path, platform: str | None = None
) -> tuple[Path | None, str | None]:
    """`scripts/install-launchd.sh` (a Mac) or `install-systemd.sh` (Linux) in the checkout
    at `root`, which installs the `flag` unit; or None, and why there is none here."""
    platform = platform or PLATFORM
    if platform == "darwin":
        name = "install-launchd.sh"
    elif platform.startswith("linux"):
        name = "install-systemd.sh"
    else:
        return None, (f"Keryx runs its servers under launchd or systemd, and {platform} has "
                      "neither")
    script = root / "scripts" / name
    if not script.is_file():
        with_flag = f" with {flag}" if flag else ""
        return None, f"scripts/{name} is not here: run it{with_flag} from a clone of Keryx"
    return script, None


def install_unit(
    flag: str, run_script: Callable[[Sequence[str]], int], *, root: Path,
    platform: str | None = None,
) -> str | None:
    """Run the installer for the `flag` unit; None, or what went wrong in a sentence."""
    script, problem = installer(flag, root=root, platform=platform)
    if script is None:
        return problem
    code = run_script([str(script), flag])
    return None if code == 0 else exited(f"{script.name} {flag}", code)


def remove_unit(
    flag: str, run_script: Callable[[Sequence[str]], int], *, root: Path,
    platform: str | None = None,
) -> str:
    """Remove the `flag` unit ("--llm" or "--voice"); what became of it, in a clause."""
    script, problem = installer(flag, root=root, platform=platform)
    if script is None:
        return f"its service could not be removed: {problem}"
    code = run_script([str(script), flag, "--uninstall"])
    if code != 0:
        said = exited(f"{script.name} {flag} --uninstall", code)
        return f"its service could not be removed: {said}"
    return REMOVED


@contextmanager
def removed_if_stopped(
    flag: str, run_script: Callable[[Sequence[str]], int], *, root: Path,
    platform: str | None = None,
) -> Iterator[None]:
    """The `flag` unit removed when what this wraps is stopped — a Cancel, which is what an
    owner does to a server that will not come up — and the stop then goes on: a unit
    installed and never seen to work is never left behind to restart in a loop."""
    try:
        yield
    except BaseException:
        with suppress(Exception):
            remove_unit(flag, run_script, root=root, platform=platform)
        raise


def exited(command: str, code: int) -> str:
    """How `command` ended, in a clause: stopped at its deadline, or its exit code."""
    if code == TIMED_OUT:
        return f"{command} did not finish within {INSTALL_TIMEOUT_S:g} s"
    return f"{command} exited with {code}"


def voice_name_problem(name: str) -> str | None:
    """Why `name` cannot be a Kokoro voice (`af_heart`: a language and sex, then a name);
    None for one that can, listed or not."""
    if VOICE_NAME.fullmatch(name):
        return None
    return f"{name!r} is not a Kokoro voice's name, like af_heart: `keryx models list` shows them"


# --- the command line ----------------------------------------------------------------------


def run_detached(argv: Sequence[str]) -> int:
    """Run an installer for the command line: nothing read from its stdin, and its output on
    stderr (descriptor 2), so stdout stays the JSON lines' alone. One of Keryx's own unit
    installers is stopped at `INSTALL_TIMEOUT_S` (`TIMED_OUT`, and a line on stderr saying
    so); anything else — a download — is given as long as it takes."""
    name = Path(argv[0]).name if argv else ""
    timeout = INSTALL_TIMEOUT_S if name in INSTALLERS else None
    try:
        return subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=2, check=False,
                              timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"{name} did not finish within {INSTALL_TIMEOUT_S:g} s", file=sys.stderr,
              flush=True)
        return TIMED_OUT
    except OSError:
        return 127


def _realtime_problem(endpoint: Endpoint) -> str | None:
    from keryx.realtime.openai import realtime_problem

    return realtime_problem(endpoint)


@dataclass(frozen=True)
class Machine:
    """This machine, as `Probes`."""

    speech_to_speech: Callable[[], str | None]
    free_bytes: Callable[[Path], int]
    run_script: Callable[[Sequence[str]], int]
    free_port: Callable[[int], int]
    wait_for_server: Callable[[Endpoint, str, float], str | None]
    realtime_problem: Callable[[Endpoint], str | None]
    words_up: Callable[[Endpoint | None], bool]


def machine() -> Machine:
    """The real machine, each probe looked up now, through its module."""
    return Machine(
        speech_to_speech=runtimes.speech_to_speech, free_bytes=hardware.free_bytes,
        run_script=run_detached, free_port=servers.free_port,
        wait_for_server=servers.wait_for, realtime_problem=_realtime_problem,
        words_up=words_up,
    )


class Silent:
    """For one JSON document: nothing said while it runs, since what matters is in it."""

    def step(self, step: str, message: str) -> AbstractContextManager[None]:
        return nullcontext()

    def warn(self, message: str) -> None:
        pass


class TextSteps:
    """For a terminal: each step a line on stdout, warnings and what stopped it on stderr."""

    def __init__(self, echo: Callable[..., None]) -> None:
        self._echo = echo

    @contextmanager
    def step(self, step: str, message: str):
        self._echo(f"{message}…")
        yield

    def warn(self, message: str) -> None:
        self._echo(f"warning: {message}", err=True)

    def error(self, message: str, *, log: Path | None = None) -> None:
        self._echo(message, err=True)
        if log is not None:
            self._echo(f"its log: {log}", err=True)

    def done(self, base_url: str) -> None:
        self._echo(f"calls now use the voice server on this machine, {base_url}; "
                   "`keryx restart` to have Keryx take it up")


class JsonSteps(EventLines):
    """For the Mac app: `{"event": "step", …}` before each step's slow work (a warning is
    one too, in the step it belongs to), then exactly one of `done`, `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__(write)
        self._current = STEPS[0]

    def step(self, step: str, message: str) -> AbstractContextManager[None]:
        self._current = step
        self._emit({"event": "step", "step": step, "message": message})
        return nullcontext()

    def warn(self, message: str) -> None:
        self._emit({"event": "step", "step": self._current, "message": message})

    def error(self, message: str, *, log: Path | None = None) -> None:
        super().error(message if log is None else f"{message} (its log: {log})")

    def done(self, base_url: str) -> None:
        self._end({"event": "done", "base_url": base_url})
