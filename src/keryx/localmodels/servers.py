"""The three servers' command lines, built from the settings: `keryx models serve llm|voice|
voice-llm`.

The units `scripts/install-systemd.sh --llm|--voice|--voice-llm` (and the launchd ones)
install run `keryx models serve …`, which reads the settings and becomes the server (`exec`).
So a unit never holds a model path, a port or a voice — change one with `keryx config set`
and restart the unit — and the command line is written once, here, where it is tested.

Two are llama.cpp, one command line with a model and a port passed in (`llm_argv`): the
local agent's (`AGENT_SERVER_*`) and the voice LLM's own (`VOICE_LLM_MODEL`,
`VOICE_LLM_SERVER_PORT`). The voice server takes its words from whichever the voice LLM is
(`Settings.voice_llm_endpoint`): the agent's, its own, or one elsewhere.

All bind 127.0.0.1. A server for other machines to use is one of your own, reached by its
address (`LOCAL_AGENT_BASE_URL`, `VOICE_BASE_URL`, `VOICE_LLM_BASE_URL`), behind Tailscale
or a key.
"""

import os
import shlex
import socket
import sys
import time
from collections.abc import Callable, Collection
from functools import partial
from pathlib import Path
from typing import NoReturn

from keryx.config import Settings
from keryx.endpoints import LOOPBACK_HOST, Endpoint, Probe, probe
from keryx.localmodels import runtimes
from keryx.localmodels.catalog import by_key
from keryx.localmodels.download import model_path

HOST = LOOPBACK_HOST
#: How many calls the voice server holds at once. Two, not its default one: a dropped socket
#: is re-opened at once, before the server has let go of the old one, and the second call
#: of an overlapping pair (a call-back ringing during a call) would be refused outright.
VOICE_PIPELINES = 2
#: Context for a GGUF given by path, which the catalog says nothing about.
DEFAULT_CONTEXT = 65536


#: How often `wait_for` asks a server that is still starting.
POLL_S = 3.0
#: How long the voice server waits for its words before it starts: as long as the model
#: server may take to load (`server.LLM_START_S`), asking every `WORDS_POLL_S`.
WORDS_WAIT_S = 600.0
WORDS_POLL_S = 2.0


class ServerConfigError(RuntimeError):
    """What is missing before a server can start, in a sentence."""


#: The kinds `serve` becomes, as `keryx models serve` and the units name them.
KINDS = ("llm", "voice", "voice-llm")
#: Who the voice server waits for, by `VOICE_LLM`, and what starts it, in a sentence.
WORDS_SERVERS = {
    "shared": ("the local model server", "`keryx models start` starts it"),
    "here": ("the voice LLM server", "`keryx models start --voice-llm` starts it"),
    "remote": ("the voice LLM server", "is it up, and is VOICE_LLM_BASE_URL right?"),
}


def gguf(
    value: str | None, setting: str, cache_dir: Path, *, unset: str
) -> tuple[str, Path, int]:
    """A catalog name or a GGUF's path, as (the id it is served as, its file, its context):
    a catalog model under its name, a GGUF of your own under its stem. `setting` and
    `unset` say what to do while `value` is blank."""
    if not value:
        raise ServerConfigError(f"{setting} is not set — {unset}")
    entry = by_key(value)
    if entry is not None:
        path, alias, context = model_path(cache_dir, entry), entry.key, entry.context
    else:
        path = Path(value).expanduser()
        alias, context = path.stem, DEFAULT_CONTEXT
    if not path.is_file():
        hint = f"`keryx models pull {value}`" if entry is not None else "is the path right?"
        raise ServerConfigError(f"{path} is not there — {hint}")
    return alias, path, context


def llm_model(settings: Settings) -> tuple[str, Path, int]:
    """`AGENT_SERVER_MODEL` as (the id it is served as, its file, its context)."""
    return gguf(settings.agent_server_model, "AGENT_SERVER_MODEL", settings.cache_dir,
                unset="`keryx setup` picks one")


def voice_llm_model(settings: Settings) -> tuple[str, Path, int]:
    """`VOICE_LLM_MODEL` as (the id it is served as, its file, its context). The id is the
    setting itself, a GGUF's path included: it is the name the voice asks for
    (`Settings.voice_llm_endpoint`), and nothing else writes a second name down. Its whole
    context, as the agent's: none is cut for the voice."""
    _alias, path, context = gguf(settings.voice_llm_model, "VOICE_LLM_MODEL",
                                 settings.cache_dir,
                                 unset="`keryx voice llm here MODEL` picks one")
    assert settings.voice_llm_model  # gguf refuses a blank one
    return settings.voice_llm_model, path, context


def llm_argv(
    settings: Settings, binary: str, *, model: tuple[str, Path, int] | None = None,
    port: int | None = None,
) -> list[str]:
    """`llama-server` serving `model` (`llm_model`'s shape; the agent's unless a caller says)
    on `port` (`AGENT_SERVER_PORT` unless a caller says) on this machine, every layer on the
    GPU."""
    alias, path, context = model or llm_model(settings)
    port = settings.agent_server_port if port is None else port
    return [
        binary, "--model", str(path), "--alias", alias,
        "--host", HOST, "--port", str(port),
        "--n-gpu-layers", "999", "--ctx-size", str(context), "--jinja",
    ]


def port_clash(settings: Settings) -> str | None:
    """Why the voice LLM's own server cannot have its port, in a sentence for a person; None
    when no other server of Keryx's is set to it. Two servers on one port both fail to bind
    and restart for ever, and a check could take the one for the other."""
    port = settings.voice_llm_server_port
    others = [name for name, theirs in (("the agent's model server", settings.agent_server_port),
                                        ("the voice server", settings.voice_server_port))
              if theirs == port]
    if not others:
        return None
    return (f"The voice LLM's server and {' and '.join(others)} are both set to port {port}: "
            "give the voice LLM a port of its own.")


def voice_llm_argv(settings: Settings, binary: str) -> list[str]:
    """`llama-server` serving `VOICE_LLM_MODEL` on `VOICE_LLM_SERVER_PORT`: the agent's
    command line with the voice's model and port."""
    return llm_argv(settings, binary, model=voice_llm_model(settings),
                    port=settings.voice_llm_server_port)


def words(settings: Settings) -> Endpoint:
    """The voice LLM the voice server takes its words from (`Settings.voice_llm_endpoint`);
    `ServerConfigError` naming what is missing when there is none, or it names no model."""
    endpoint = settings.voice_llm_endpoint
    if endpoint is not None and endpoint.model:
        return endpoint
    if settings.voice_llm == "shared":
        raise ServerConfigError(
            "the voice server takes its words from the local model: set LOCAL_AGENT_BASE_URL "
            "and LOCAL_AGENT_MODEL first (`keryx setup`)"
        )
    missing = "VOICE_LLM_BASE_URL" if endpoint is None else "VOICE_LLM_MODEL"
    raise ServerConfigError(
        f"the voice server takes its words from the voice LLM ({settings.voice_llm}): set "
        f"{missing} first (`keryx setup`)"
    )


def voice_argv(settings: Settings, binary: str) -> tuple[list[str], dict[str, str]]:
    """speech-to-speech on this machine, its words from the voice LLM (`words`), and the
    environment it needs: that server's key (`Endpoint.bearer`), in place of whatever
    `OPENAI_API_KEY` this process has, and never on argv. The owner's own reaches it only
    for a voice LLM on OpenAI's host (`Settings.voice_llm_key`)."""
    llm = words(settings)
    argv = [
        binary, "serve", "--host", HOST, "--port", str(settings.voice_server_port),
        "--llm_backend", "responses-api", "--responses_api_base_url", llm.base_url,
        "--model_name", llm.model,
        "--stt", settings.voice_server_stt, "--tts", settings.voice_server_tts,
        "--num_pipelines", str(VOICE_PIPELINES),
    ]
    if settings.voice_server_tts == "kokoro":
        argv += ["--kokoro_voice", settings.voice_server_voice]
    argv += shlex.split(settings.voice_server_args)
    return argv, {"OPENAI_API_KEY": llm.bearer}


def serve(
    kind: str,
    settings: Settings,
    *,
    execvpe: Callable[[str, list[str], dict[str, str]], NoReturn] = os.execvpe,
    words_up: Callable[[Endpoint], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> NoReturn:
    """Become the `kind` server (`KINDS`); `ServerConfigError` when it cannot.

    The voice server first waits for its words (`wait_for_words`): at login its unit and the
    voice LLM's server load together, and speech-to-speech would crash on its first request
    for words, again and again under KeepAlive, until llama.cpp had loaded its model. The
    voice LLM's own server asks only for its model, whatever `VOICE_LLM` says: it is started,
    and seen to answer, before that is saved."""
    if kind == "voice-llm" and (clash := port_clash(settings)):
        raise ServerConfigError(clash)
    if kind in ("llm", "voice-llm"):
        binary = runtimes.llama_server(settings.cache_dir)
        if binary is None:
            raise ServerConfigError("llama-server is not installed — `keryx setup` installs it")
        argv = llm_argv(settings, binary) if kind == "llm" else voice_llm_argv(settings, binary)
        env: dict[str, str] = {}
    elif kind == "voice":
        binary = runtimes.speech_to_speech()
        if binary is None:
            raise ServerConfigError(
                "speech-to-speech is not installed — `keryx setup` installs it"
            )
        argv, env = voice_argv(settings, binary)
        if words_up is None:
            from keryx.localmodels import voice  # it imports this module

            # The voice's own server is asked which model it serves: on a port of its own, an
            # answer from anything else there is not its words.
            words_up = voice.voice_llm_up if settings.voice_llm == "here" else voice.words_up
        name, hint = WORDS_SERVERS[settings.voice_llm]
        wait_for_words(words(settings), up=words_up, sleep=sleep, clock=clock,
                       name=name, hint=hint)
    else:
        raise ServerConfigError(f"there is no {kind} server: {', '.join(KINDS)}")
    execvpe(argv[0], argv, {**os.environ, **env})


def wait_for_words(
    words: Endpoint,
    *,
    up: Callable[[Endpoint], bool],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    name: str = WORDS_SERVERS["shared"][0],
    hint: str = WORDS_SERVERS["shared"][1],
) -> None:
    """Return once the server the voice server takes its words from (`name`) answers, asking
    every `WORDS_POLL_S` for up to `WORDS_WAIT_S`; `ServerConfigError`, ending in `hint`,
    when it never does. Said once on stderr, the voice server's log, while it waits."""
    deadline = clock() + WORDS_WAIT_S
    said = False
    while not up(words):
        if clock() >= deadline:
            raise ServerConfigError(
                f"waiting for {name} at {words.base_url}: it did not answer "
                f"within {WORDS_WAIT_S:g} s — {hint}"
            )
        if not said:
            print(f"waiting for {name} at {words.base_url} before starting "
                  "the voice server", file=sys.stderr, flush=True)
            said = True
        sleep(WORDS_POLL_S)


def free_port_avoiding(
    free_port: Callable[[int], int], preferred: int, taken: Collection[int]
) -> int:
    """`free_port` from `preferred` up, stepping past `taken`: the ports Keryx's other servers
    are set to, which may be free now only because those servers are not running yet."""
    port = free_port(preferred)
    for _ in range(len(taken)):
        if port not in taken:
            break
        port = free_port(port + 1)
    if port in taken:
        raise OSError(f"no free port from {preferred} up")
    return port


def free_port(preferred: int, *, tries: int = 100) -> int:
    """`preferred` when nothing on this machine listens there, else the next free port up.

    Asked, not assumed: speech-to-speech's own default, 8765, is a popular port, and a server
    that cannot bind fails in a log nobody is reading.
    """
    for port in range(preferred, min(preferred + tries, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port from {preferred} up")


def wait_for(
    endpoint: Endpoint,
    kind: str,
    timeout_s: float,
    *,
    models: Callable[[Endpoint], Probe] = probe,
    realtime: Callable[[Endpoint], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Ask `endpoint` until it answers — its model list (`kind` "models") or a Realtime
    session ("realtime") — or `timeout_s` passes; None, or the last reason it did not.

    A server's first start downloads what it runs on, so the caller says how long that may
    take rather than this guessing. The credentials taken out of what a refusal says are
    gathered once for the whole wait, not on every attempt (`child_env.known_secrets`).
    """
    if realtime is None and kind == "realtime":
        from keryx.child_env import known_secrets
        from keryx.realtime.openai import realtime_problem

        try:
            secrets: tuple[str, ...] | None = known_secrets()
        except Exception:  # each attempt then gathers them itself, and withholds on failure
            secrets = None
        realtime = partial(realtime_problem, secrets=secrets)
    deadline = clock() + timeout_s
    while True:
        if kind == "realtime":
            assert realtime is not None
            problem = realtime(endpoint)
        else:
            found = models(endpoint)
            problem = None if found.ok else found.problem
        if problem is None or clock() >= deadline:
            return problem
        sleep(POLL_S)
