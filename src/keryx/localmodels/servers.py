"""The two servers' command lines, built from the settings: `keryx models serve llm|voice`.

The units `scripts/install-systemd.sh --llm|--voice` (and the launchd ones) install run
`keryx models serve …`, which reads the settings and becomes the server (`exec`). So a unit
never holds a model path, a port or a voice — change one with `keryx config set` and restart
the unit — and the command line is written once, here, where it is tested.

Both bind 127.0.0.1. A server for other machines to use is one of your own, reached by its
address (`LOCAL_AGENT_BASE_URL`, `VOICE_BASE_URL`), behind Tailscale or a key.
"""

import os
import shlex
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from keryx.config import Settings
from keryx.endpoints import Endpoint, Probe, probe
from keryx.localmodels import runtimes
from keryx.localmodels.catalog import by_key
from keryx.localmodels.download import model_path

HOST = "127.0.0.1"
#: How many calls the voice server holds at once. Two, not its default one: a dropped socket
#: is re-opened at once, before the server has let go of the old one, and the second call
#: of an overlapping pair (a call-back ringing during a call) would be refused outright.
VOICE_PIPELINES = 2
#: Context for a GGUF given by path, which the catalog says nothing about.
DEFAULT_CONTEXT = 65536


#: How often `wait_for` asks a server that is still starting.
POLL_S = 3.0


class ServerConfigError(RuntimeError):
    """What is missing before a server can start, in a sentence."""


def llm_model(settings: Settings) -> tuple[str, Path, int]:
    """`LLM_SERVER_MODEL` as (the id it is served as, its file, its context)."""
    value = settings.llm_server_model
    if not value:
        raise ServerConfigError("LLM_SERVER_MODEL is not set — `keryx setup` picks one")
    entry = by_key(value)
    if entry is not None:
        path, alias, context = model_path(settings.cache_dir, entry), entry.key, entry.context
    else:
        path = Path(value).expanduser()
        alias, context = path.stem, DEFAULT_CONTEXT
    if not path.is_file():
        hint = f"`keryx models pull {value}`" if entry is not None else "is the path right?"
        raise ServerConfigError(f"{path} is not there — {hint}")
    return alias, path, context


def llm_argv(settings: Settings, binary: str) -> list[str]:
    """`llama-server` serving `LLM_SERVER_MODEL` on this machine, every layer on the GPU."""
    alias, path, context = llm_model(settings)
    return [
        binary, "--model", str(path), "--alias", alias,
        "--host", HOST, "--port", str(settings.llm_server_port),
        "--n-gpu-layers", "999", "--ctx-size", str(context), "--jinja",
    ]


def voice_argv(settings: Settings, binary: str) -> tuple[list[str], dict[str, str]]:
    """speech-to-speech on this machine, its words from the local agent's model, and the
    environment it needs: that server's key, in place of whatever `OPENAI_API_KEY` this
    process has — the owner's real one must never reach a local server."""
    words = settings.local_agent_endpoint
    if words is None or not words.model:
        raise ServerConfigError(
            "the voice server takes its words from the local model: set LOCAL_AGENT_BASE_URL "
            "and LOCAL_AGENT_MODEL first (`keryx setup`)"
        )
    argv = [
        binary, "serve", "--host", HOST, "--port", str(settings.voice_server_port),
        "--llm_backend", "responses-api", "--responses_api_base_url", words.base_url,
        "--model_name", words.model,
        "--stt", settings.voice_server_stt, "--tts", settings.voice_server_tts,
        "--num_pipelines", str(VOICE_PIPELINES),
    ]
    if settings.voice_server_tts == "kokoro":
        argv += ["--kokoro_voice", settings.voice_server_voice]
    argv += shlex.split(settings.voice_server_args)
    return argv, {"OPENAI_API_KEY": words.bearer}


def serve(
    kind: str,
    settings: Settings,
    *,
    execvpe: Callable[[str, list[str], dict[str, str]], NoReturn] = os.execvpe,
) -> NoReturn:
    """Become the `kind` server ("llm" or "voice"); `ServerConfigError` when it cannot."""
    if kind == "llm":
        binary = runtimes.llama_server(settings.cache_dir)
        if binary is None:
            raise ServerConfigError("llama-server is not installed — `keryx setup` installs it")
        argv, env = llm_argv(settings, binary), {}
    elif kind == "voice":
        binary = runtimes.speech_to_speech()
        if binary is None:
            raise ServerConfigError(
                "speech-to-speech is not installed — `keryx setup` installs it"
            )
        argv, env = voice_argv(settings, binary)
    else:
        raise ServerConfigError(f"there is no {kind} server: llm or voice")
    execvpe(argv[0], argv, {**os.environ, **env})


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
    take rather than this guessing.
    """
    if realtime is None:
        from keryx.realtime.openai import realtime_problem

        realtime = realtime_problem
    deadline = clock() + timeout_s
    while True:
        if kind == "realtime":
            problem = realtime(endpoint)
        else:
            found = models(endpoint)
            problem = None if found.ok else found.problem
        if problem is None or clock() >= deadline:
            return problem
        sleep(POLL_S)
