"""The voice on a call: where it is, and setting it up — `keryx voice`, `keryx voice use …`
and the wizard's Local models section, which share every step here.

Three places a call's voice can be, as `mode` names them: OpenAI's Realtime API (`openai`,
`VOICE_BASE_URL` blank), speech-to-speech on this machine (`here`, `VOICE_BASE_URL` the
loopback address of `VOICE_SERVER_PORT`) and a server elsewhere (`server`).

`here` is the long one: install speech-to-speech when it is missing, download its speech
models when they are not in the cache (as `download` does, through the same steps), save its
voice and port, install its unit, wait for it to answer a Realtime handshake, and only then
save it as the voice — a `VOICE_BASE_URL` that does not answer is a phone that rings and
says nothing. It refuses while the local model server, which the voice server takes its
words from, does not answer: on 2026-10-07 a voice server installed without one crash-looped
under launchd's KeepAlive on its first request for words. And a server that does not come
up, or whose start is cancelled, has its unit removed before anything else is said, so no
unit is ever left restarting in a loop (`removed_if_stopped`, which `keryx models start`
shares).

`download` is `keryx voice download`: speech-to-speech installed as `here` installs it, then
the speech models its settings choose fetched into the Hugging Face cache by its own Python
(`speech`), so its first start has nothing left to download.

Each step is told to a `Report` before its slow work starts; the wizard tells it as notes and
a spinner, `keryx voice use here --json` as JSON lines (`JsonSteps`, on pull's `EventLines`,
so it ends exactly once on every path). Every install, port, disk and network question is
asked through `Probes`: the wizard's own, or `machine()` for the command line.
"""

import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from keryx.child_env import operational_env, redact_known
from keryx.config import Settings
from keryx.config.store import FROM_DEFAULT, ConfigStore
from keryx.endpoints import Endpoint, warnings
from keryx.localmodels import catalog, hardware, runtimes, servers, speech, use
from keryx.localmodels.catalog import VOICE_SERVER_DOWNLOAD_GB
from keryx.localmodels.pull import CANCEL_SIGNALS, EventLines
from keryx.localmodels.servers import HOST

OPENAI, HERE, SERVER = "openai", "here", "server"
#: How long the voice server may take to start once its speech models are in the cache —
#: `here` downloads them first — loading them and warming up: as long as a model server
#: may take to load a small model.
VOICE_READY_START_S = 180.0
#: …and when they are not: a speech model Keryx cannot name, kept from before, whose first
#: start downloads it.
VOICE_START_S = 1200.0
#: How long a voice server elsewhere may take to say it is there, for `keryx voice`: a
#: Realtime handshake is too slow for a poll, so its model list is asked instead.
SERVER_REACH_S = 2.0
#: What `needs` says while there is no local model for the voice server's words.
NEEDS_WORDS = "local_agent_model"
NO_WORDS = "Set up a local model first: the voice server takes its words from it."
#: …and what a terminal runs for it.
NO_WORDS_HINT = "`keryx models use NAME` sets one up"
#: What an installer missing from this copy of Keryx is, said to a person.
NO_CHECKOUT = "Keryx's service installer is not in this copy of Keryx."
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
#: How long a killed installer is given to be reaped.
STOP_S = 5.0
#: The attribute a stop carries naming the units already removed for it.
REMOVED_FOR = "keryx_units_removed"
#: The exit codes of a set-up: a refusal is one nothing was tried for.
OK, FAILED, REFUSED = 0, 1, 2
#: The platform whose service manager installs the units, unless a caller says another.
PLATFORM = sys.platform
#: A Kokoro voice's name: `af_heart`, `bm_george`, `zf_xiaoxiao`.
VOICE_NAME = re.compile(r"[a-z]{2}_[a-z0-9]+")
#: The steps of `here`, in order, as the JSON lines name them.
STEPS = ("install", "download", "configure", "unit", "start")
#: The steps of `download`, in order.
DOWNLOAD_STEPS = ("install", "download")


class Probes(Protocol):
    """What `here` and `check_server` ask of the machine; `keryx setup`'s `Probes` is one."""

    speech_to_speech: Callable[[], str | None]
    free_bytes: Callable[[Path], int]
    run_script: Callable[[Sequence[str]], int]
    free_port: Callable[[int], int]
    wait_for_server: Callable[[Endpoint, str, float], str | None]
    realtime_problem: Callable[[Endpoint], str | None]
    words_up: Callable[[Endpoint | None], bool]
    speech_python: Callable[[], str | None]
    fetch_speech: Callable[[str, list[speech.Repo], Callable[[int, int | None], None]],
                           str | None]
    models_ready: Callable[[Settings], bool]


class Installs(Protocol):
    """What installing speech-to-speech asks of the machine."""

    speech_to_speech: Callable[[], str | None]
    free_bytes: Callable[[Path], int]
    run_script: Callable[[Sequence[str]], int]


class DownloadProbes(Installs, Protocol):
    """…and what fetching its speech models asks: its own Python, and the fetch."""

    speech_python: Callable[[], str | None]
    fetch_speech: Callable[[str, list[speech.Repo], Callable[[int, int | None], None]],
                           str | None]


class Steps(Protocol):
    """How the work is told while it runs: each step before its slow work, and warnings.
    `message` is for a person; a path or a command is the `detail`."""

    def step(self, step: str, message: str, *,
             detail: str | None = None) -> AbstractContextManager[None]: ...

    def warn(self, message: str, *, detail: str | None = None) -> None: ...


class Told(Steps, Protocol):
    """…and what stopped it."""

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None: ...


class Report(Steps, Protocol):
    """…and how a set-up ended: the address it saved, or what stopped it (and its log); the
    speech models' bytes while they download."""

    def progress(self, done: int, total: int | None) -> None: ...

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None: ...

    def done(self, base_url: str) -> None: ...


class DownloadReport(Steps, Protocol):
    """How a download is told: its steps, its bytes, and how it ended."""

    def progress(self, done: int, total: int | None) -> None: ...

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None: ...

    def done(self, stt: str, tts: str) -> None: ...


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
    models_ready: Callable[[Settings], bool] | None = None,
) -> dict[str, Any]:
    """`keryx voice --json`. The voice server here is asked on loopback only (`probe`); a
    server elsewhere only when it is the voice; its speech models in the Hugging Face cache
    (`models_ready`, `speech.models_ready` unless a caller says). No key is in it."""
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
            "stt": settings.voice_server_stt,
            "tts": settings.voice_server_tts,
            "models_ready": (models_ready or speech.models_ready)(settings),
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

    `choose_voice` is asked once speech-to-speech is installed, before its speech models
    download (a Kokoro voice is one of their files); `save` stores values as
    whoever is running this and says why when it cannot (False). `root` is the checkout
    whose `scripts/` installs the unit. 2 for what refuses it before anything is tried —
    no local model for the words, or none answering, no installer for the unit — 1 for
    anything that stopped it, 0 once the server answered and was saved as the voice. A
    half-done install is left for the next run to finish; a unit whose server did not come
    up, or whose start was cancelled, is not, and a download that stopped installs none.
    """
    if words_model(settings) is None:
        report.error(no_words, detail=NO_WORDS_HINT)
        return REFUSED
    if not probes.words_up(settings.local_agent_endpoint):
        report.error(NO_WORDS_SERVER)
        return REFUSED
    script, problem = installer("--voice", root=root, platform=platform)
    if script is None:
        no_installer(report, problem)
        return REFUSED
    if not have_speech_to_speech(settings, probes, report):
        return FAILED
    voice = choose_voice()
    # A Kokoro voice is a file of its own: the models are asked of the voice just chosen.
    chosen = settings.model_copy(update={"voice_server_voice": voice})
    start_s = VOICE_READY_START_S
    if not probes.models_ready(chosen):
        try:
            repos = speech.needed(chosen, platform=platform)
        except speech.UnknownModels as error:  # kept from before: its first start fetches it
            report.warn(str(error))
            start_s = VOICE_START_S
        else:
            if not fetch_models(chosen, probes, report, repos):
                return FAILED
    port = settings.voice_server_port
    with report.step("configure", f"Saving the voice server's voice, {voice}, and its port"):
        if voice not in [name for name, _sounds in catalog.VOICES]:
            report.warn(f"{voice} is not one of the Kokoro voices Keryx lists; Kokoro takes "
                        "others, so it is kept.",
                        detail="`keryx models list` shows the listed ones")
        if not is_ours(settings.voice_base_url, port):
            port = probes.free_port(port)
        if not save({"VOICE_SERVER_VOICE": voice, "VOICE_SERVER_PORT": port}):
            return FAILED
    endpoint = Endpoint.parse(f"http://{HOST}:{port}")
    # One guard from the unit to the server answering: no moment between the two — the
    # start step being told included — where a Cancel would leave the unit behind.
    with removed_if_stopped("--voice", probes.run_script, root=root, platform=platform):
        if not install_unit_step("--voice", "Installing the voice server's service",
                                 probes.run_script, report, root=root, platform=platform):
            return FAILED
        with report.step("start", "Starting the voice server and waiting for it to answer — "
                                  "a first start loads its speech models, which takes a "
                                  "minute"):
            problem = probes.wait_for_server(endpoint, "realtime", start_s)
    if problem is not None:
        removed = remove_unit("--voice", probes.run_script, root=root, platform=platform)
        report.error(did_not_come_up("The voice server", removed), detail=f"{problem}; {removed}",
                     log=settings.state_dir / "logs" / "keryx-voice.err.log")
        return FAILED
    if not save({"VOICE_BASE_URL": endpoint.base_url, "VOICE_API_KEY": None}):
        return FAILED
    report.done(endpoint.base_url)
    return OK


def have_speech_to_speech(
    settings: Settings, probes: Installs, report: "Report | DownloadReport"
) -> bool:
    """speech-to-speech here, installed with uv when it is missing (step `install`); False
    once the report has been told why it is not."""
    if probes.speech_to_speech() is not None:
        return True
    free = probes.free_bytes(settings.cache_dir)
    if free < (VOICE_SERVER_DOWNLOAD_GB + 2) * 1e9:
        report.error(f"The voice server needs about {VOICE_SERVER_DOWNLOAD_GB + 2:.0f} GB "
                     f"free for itself and its speech models; there is {free / 1e9:.1f} GB.")
        return False
    argv = runtimes.s2s_install_argv()
    with report.step("install", "Installing speech-to-speech, Hugging Face's voice "
                                "pipeline, with uv"):
        code = probes.run_script(argv)
    if code != 0:
        report.error("speech-to-speech could not be installed.",
                     detail=exited(" ".join(argv[:3]), code))
        return False
    return True


def download(
    settings: Settings, probes: DownloadProbes, report: DownloadReport, *,
    platform: str | None = None,
) -> int:
    """Fetch the speech models `VOICE_SERVER_STT`, `_TTS` and `_VOICE` need into the Hugging
    Face cache, with speech-to-speech's own Python, installing it first when it is missing;
    the exit code. 2 for models Keryx cannot name (nothing is tried), 1 for what stopped it,
    0 once they are all there."""
    stt, tts = settings.voice_server_stt, settings.voice_server_tts
    try:
        repos = speech.needed(settings, platform=platform)
    except speech.UnknownModels as error:
        report.error(str(error))
        return REFUSED
    if not have_speech_to_speech(settings, probes, report):
        return FAILED
    if not fetch_models(settings, probes, report, repos):
        return FAILED
    report.done(stt, tts)
    return OK


def fetch_models(
    settings: Settings, probes: DownloadProbes, report: "Report | DownloadReport",
    repos: list[speech.Repo],
) -> bool:
    """`repos` fetched into the Hugging Face cache by speech-to-speech's own Python (step
    `download`, its bytes told as progress), for `download` and `here` alike; False once the
    report has been told why they were not."""
    stt, tts = settings.voice_server_stt, settings.voice_server_tts
    python = probes.speech_python()
    if python is None:
        report.error("speech-to-speech is installed, but its own Python cannot be found.",
                     detail="reinstall it with `keryx voice download` after `uv tool uninstall "
                     "speech-to-speech`")
        return False
    with report.step("download", f"Downloading the voice's speech models into the Hugging "
                                 f"Face cache: {stt} to hear, {tts} to speak"):
        problem = probes.fetch_speech(python, repos, report.progress)
    if problem is not None:
        # Its child holds the Hugging Face tokens, and a failed request can quote one.
        report.error("The voice's speech models could not be downloaded.",
                     detail=shortened(redact_known(problem, settings)))
        return False
    return True


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


def install_unit_step(
    flag: str, message: str, run_script: Callable[[Sequence[str]], int], report: Told, *,
    root: Path, platform: str | None = None,
) -> bool:
    """Install the `flag` unit as step `unit`; False once the report has been told why not.

    An installer writes the unit before it loads it, so one that fails, times out or is
    cancelled can leave a unit the service manager loads at the next login: it is removed
    — said as a warning, before the error — and a Cancel then goes on."""
    with report.step("unit", message), \
            removed_if_stopped(flag, run_script, root=root, platform=platform):
        script, problem = installer(flag, root=root, platform=platform)
        code = run_script([str(script), flag]) if script is not None else 0
    if script is None:
        no_installer(report, problem)
        return False
    if code != 0:
        warn_removed(report, flag, remove_unit(flag, run_script, root=root, platform=platform))
        report.error(f"{SERVERS.get(flag, 'The server')}'s service could not be installed.",
                     detail=exited(f"{script.name} {flag}", code))
        return False
    return True


#: What each unit's server is called in a sentence.
SERVERS = {"--llm": "The model server", "--voice": "The voice server"}


def warn_removed(report: Steps, flag: str, said: str) -> None:
    """Warn what became of the `flag` unit (`remove_unit`'s clause): removed, or still
    installed — in plain words, the installer's own the `detail`."""
    server = SERVERS.get(flag, "The server")
    if said == REMOVED:
        report.warn(f"{server}'s service was removed, so it does not restart in a loop.")
    else:
        report.warn(f"{server}'s service is still installed: it could not be removed, and may "
                    "start again at the next login.", detail=said)


def no_installer(report: Told, problem: str | None) -> None:
    """Tell `report` there is no installer here: a checkout without it in a sentence of its
    own, with where it was looked for as the detail; a platform with neither manager as is."""
    if problem is None or problem.startswith("scripts/"):
        report.error(NO_CHECKOUT, detail=problem)
    else:
        report.error(problem)


#: The most of a tool's own words an error's `detail` repeats.
DETAIL_CHARS = 200


def shortened(text: str) -> str:
    """`text` cut to `DETAIL_CHARS` — after its credentials are out, never before."""
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…"


def did_not_come_up(server: str, removed: str) -> str:
    """`server` did not come up, and whether its unit went with it, in a sentence."""
    if removed == REMOVED:
        return f"{server} did not come up, so its service was removed."
    return f"{server} did not come up, and its service could not be removed."


def remove_unit(
    flag: str, run_script: Callable[[Sequence[str]], int], *, root: Path,
    platform: str | None = None,
) -> str:
    """Remove the `flag` unit ("--llm" or "--voice"); what became of it, in a clause.

    Cancel (SIGTERM, SIGINT) is held off while the uninstaller runs and lands once it has
    finished: a second Cancel arriving now would kill it halfway and keep the very unit it
    was removing."""
    script, problem = installer(flag, root=root, platform=platform)
    if script is None:
        return f"its service could not be removed: {problem}"
    held = signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)
    try:
        code = run_script([str(script), flag, "--uninstall"])
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
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
    installed and never seen to work is never left behind to restart in a loop. Guards
    nest — one spanning unit and start, one around the unit's own install — and the unit
    is removed once: the stop carries the flags already removed for it."""
    try:
        yield
    except BaseException as stopped:
        removed: frozenset[str] = getattr(stopped, REMOVED_FOR, frozenset())
        if flag not in removed:
            with suppress(Exception):
                remove_unit(flag, run_script, root=root, platform=platform)
            setattr(stopped, REMOVED_FOR, removed | {flag})
        raise


def exited(command: str, code: int) -> str:
    """How `command` ended, in a clause: stopped at its deadline, or its exit code."""
    if code == TIMED_OUT:
        return f"{command} did not finish within {INSTALL_TIMEOUT_S:g} s"
    return f"{command} exited with {code}"


#: Where a terminal finds the Kokoro voices' names.
VOICE_NAMES_HINT = "`keryx models list` shows them"


def voice_name_problem(name: str) -> str | None:
    """Why `name` cannot be a Kokoro voice (`af_heart`: a language and sex, then a name);
    None for one that can, listed or not."""
    if VOICE_NAME.fullmatch(name):
        return None
    return f"{name!r} is not a Kokoro voice's name, like af_heart."


# --- the command line ----------------------------------------------------------------------


def run_detached(argv: Sequence[str]) -> int:
    """Run an installer for the command line: nothing read from its stdin, and its output on
    stderr (descriptor 2), so stdout stays the JSON lines' alone. One of Keryx's own unit
    installers runs in a session of its own and is stopped at `INSTALL_TIMEOUT_S`
    (`TIMED_OUT`, and a line on stderr saying so) — it and everything it started, so no
    `launchctl` or `uv` is left running; anything else — a download — is given as long as it
    takes. A Cancel stops what it ran, then goes on. None of Keryx's secrets is in its
    environment (`child_env.operational_env`)."""
    name = Path(argv[0]).name if argv else ""
    timed = name in INSTALLERS
    try:
        child = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=2,
                                 start_new_session=timed, env=operational_env())
    except OSError:
        return 127
    try:
        return child.wait(timeout=INSTALL_TIMEOUT_S if timed else None)
    except subprocess.TimeoutExpired:
        _stop(child, group=True)
        print(f"{name} did not finish within {INSTALL_TIMEOUT_S:g} s", file=sys.stderr,
              flush=True)
        return TIMED_OUT
    except BaseException:
        _stop(child, group=timed)
        raise


def _stop(child: subprocess.Popen, *, group: bool) -> None:
    """Kill `child` — with its whole process group when it leads one — and reap it."""
    try:
        if group:
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        child.wait(STOP_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - a kill always lands
        pass


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
    speech_python: Callable[[], str | None]
    fetch_speech: Callable[[str, list[speech.Repo], Callable[[int, int | None], None]],
                           str | None]
    models_ready: Callable[[Settings], bool]


def machine() -> Machine:
    """The real machine, each probe looked up now, through its module."""
    return Machine(
        speech_to_speech=runtimes.speech_to_speech, free_bytes=hardware.free_bytes,
        run_script=run_detached, free_port=servers.free_port,
        wait_for_server=servers.wait_for, realtime_problem=_realtime_problem,
        words_up=words_up, speech_python=runtimes.speech_to_speech_python,
        fetch_speech=speech.fetch, models_ready=speech.models_ready,
    )


class Silent:
    """For one JSON document: nothing said while it runs, since what matters is in it."""

    def step(self, step: str, message: str, *,
             detail: str | None = None) -> AbstractContextManager[None]:
        return nullcontext()

    def warn(self, message: str, *, detail: str | None = None) -> None:
        pass


class TextSteps:
    """For a terminal: each step a line on stdout, warnings and what stopped it on stderr."""

    def __init__(self, echo: Callable[..., None]) -> None:
        self._echo = echo
        self._said = -1

    @contextmanager
    def step(self, step: str, message: str, *, detail: str | None = None):
        self._echo(f"{message}…" if detail is None else f"{message} {detail}")
        yield

    def progress(self, done: int, total: int | None) -> None:
        """The bytes so far, every tenth of the way."""
        if not total:
            return
        tenth = done * 10 // total
        if tenth > self._said:
            self._said = tenth
            self._echo(f"{done / 1e9:.1f} of {total / 1e9:.1f} GB")

    def warn(self, message: str, *, detail: str | None = None) -> None:
        self._echo(f"warning: {message}" + (f" ({detail})" if detail else ""), err=True)

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None:
        self._echo(message, err=True)
        if detail:
            self._echo(detail, err=True)
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

    def step(self, step: str, message: str, *,
             detail: str | None = None) -> AbstractContextManager[None]:
        """A step: `message` for a person, and `detail` — always there, null when there is
        nothing more — a path or a command, which the app does not lead with."""
        self._current = step
        self._emit({"event": "step", "step": step, "message": message, "detail": detail})
        return nullcontext()

    def warn(self, message: str, *, detail: str | None = None) -> None:
        self._emit({"event": "step", "step": self._current, "message": message,
                    "detail": detail})

    def progress(self, done: int, total: int | None) -> None:
        """The speech models' bytes while they download (`total` null when not known)."""
        self._progressed(done, total, step=self._current)

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None:
        """`message` for a person; what a tool said, and the log, are the `detail`."""
        parts = [part for part in (detail, f"its log: {log}" if log else None) if part]
        super().error(message, detail="; ".join(parts) or None)

    def done(self, base_url: str) -> None:
        self._end({"event": "done", "base_url": base_url})


class DownloadTextSteps(TextSteps):
    """For a terminal: each step a line, the bytes every tenth of the way, and the ending."""

    def done(self, stt: str, tts: str) -> None:  # type: ignore[override]
        self._echo(f"the voice's speech models are downloaded: {stt} to hear, {tts} to speak")


class DownloadJsonSteps(JsonSteps):
    """For the Mac app: a `step` before each step's slow work, `progress` (bytes; `total`
    null when Hugging Face does not say) while the models download, then exactly one of
    `done` (`stt`, `tts`), `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__(write)
        self._current = DOWNLOAD_STEPS[0]

    def done(self, stt: str, tts: str) -> None:  # type: ignore[override]
        self._end({"event": "done", "stt": stt, "tts": tts})
