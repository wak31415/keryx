"""Making a model the local agent's, and whether its servers are up: `keryx models use`, the
wizard's Local models section, and `servers` in `keryx models list --json`.

`llama_values` and `enable_local` are the one place the settings for "this model, served
here, is the local agent's" are written down. The wizard and `keryx models use` both save
what they return, so the two cannot drift into describing the same model two ways.

`server_status` is read by the Mac app every time it shows the models. Reachability is a
loopback question only: an address elsewhere answers False without being asked, so showing
the list never reaches out to another machine.
"""

import sys
from collections.abc import Callable
from typing import Any

import httpx

from keryx.config import Settings
from keryx.endpoints import Endpoint
from keryx.localmodels import runtimes
from keryx.localmodels.servers import HOST
from keryx.restart import service
from keryx.restart.service import ServiceTarget

#: How long a loopback server may take to answer whether it is up.
REACH_TIMEOUT_S = 1.0
#: Each server's unit, by service manager: what `scripts/install-*.sh --llm|--voice` install.
UNITS = {
    "launchd": {"llm": "dev.keryx.llm", "voice": "dev.keryx.voice"},
    "systemd": {"llm": "keryx-llm.service", "voice": "keryx-voice.service"},
}


def messages_harness() -> dict[str, Any]:
    """What a model served here is driven by: Claude Code over the Messages API, no key."""
    return {"LOCAL_AGENT_API_KEY": None, "LOCAL_AGENT_API": "anthropic-messages"}


def llama_values(
    settings: Settings, model: str, alias: str, *, free_port: Callable[[int], int]
) -> dict[str, Any]:
    """The settings that serve `model` (a catalog name or a GGUF's path) under llama.cpp as
    `alias`, and point the local agent at it.

    The port is kept when the model server is already ours — the port is taken by itself —
    and otherwise asked for, from `LLM_SERVER_PORT` up, so it never lands on another's.
    """
    port = settings.llm_server_port
    if not settings.llm_server_model:
        port = free_port(port)
    return {
        "LLM_SERVER_MODEL": model,
        "LLM_SERVER_PORT": port,
        "LOCAL_AGENT_BASE_URL": f"http://{HOST}:{port}/v1",
        "LOCAL_AGENT_MODEL": alias,
        **messages_harness(),
    }


def enable_local(settings: Settings, *, default: bool) -> dict[str, Any]:
    """The local agent among the ones a call may name, and, when `default`, the one that
    does what nobody named an agent for."""
    enabled = list(dict.fromkeys([*settings.enabled_agents, "local"]))
    return {
        "AGENTS_ENABLED": ",".join(enabled),
        "AGENT_BACKEND": "local" if default else settings.agent_backend,
    }


def server_status(
    settings: Settings,
    *,
    installed: Callable[[str], bool],
    reachable: Callable[[str], bool],
    which: Callable[[str], str | None],
) -> dict[str, Any]:
    """The model server, the voice server and the local agent, as the settings describe
    them and as the machine finds them. `installed` is asked "llm" or "voice"; `reachable`
    is asked a `…/v1` address. No key is in it."""
    model = settings.llm_server_model
    llm_url = f"http://{HOST}:{settings.llm_server_port}/v1" if model else None
    voice_url = settings.voice_base_url or None
    return {
        "llm": {
            "model": model,
            "port": settings.llm_server_port,
            "url": llm_url,
            "binary": runtimes.llama_server(settings.cache_dir, lookup=which),
            "unit_installed": installed("llm"),
            "reachable": bool(llm_url) and reachable(llm_url),
        },
        "voice": {
            "url": voice_url,
            "port": settings.voice_server_port,
            "unit_installed": installed("voice"),
            "reachable": bool(voice_url) and reachable(voice_url),
        },
        "local_agent": {
            "base_url": settings.local_agent_base_url,
            "model": settings.local_agent_model,
            "api": settings.local_agent_api,
            "enabled": "local" in settings.enabled_agents,
            "default": settings.agent_backend == "local",
        },
    }


def reachable(url: str, *, get: Callable[..., httpx.Response] | None = None) -> bool:
    """Whether a server on this machine answers `GET {url}/models` within a second.

    Any answer short of a server error counts: the voice server speaks the Realtime socket
    and need not list models, and a 404 from it is still a server that is up. An address
    that is not on this machine is False without being asked.
    """
    try:
        endpoint = Endpoint.parse(url)
    except ValueError:
        return False
    if not endpoint.is_loopback:
        return False
    try:
        response = (get or httpx.get)(endpoint.url("models"), timeout=REACH_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def unit_installed(
    kind: str,
    *,
    platform: str = sys.platform,
    is_installed: Callable[[ServiceTarget], bool] | None = None,
) -> bool:
    """Whether the service manager knows the `kind` ("llm" or "voice") server's unit."""
    if platform == "darwin":
        manager = "launchd"
    elif platform.startswith("linux"):
        manager = "systemd"
    else:
        return False
    # Late-bound through the module, as `keryx.status` binds it.
    ask = is_installed or service.is_installed
    return ask(ServiceTarget(manager, UNITS[manager][kind]))
