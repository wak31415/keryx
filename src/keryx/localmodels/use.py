"""Making a model the local agent's, and whether its servers are up: `keryx models use`, the
wizard's Local models section, and `servers` in `keryx models list --json`.

`llama_values` and `enable_local` are the one place the settings for "this model, served
here, is the local agent's" are written down. The wizard and `keryx models use` both save
what they return, so the two cannot drift into describing the same model two ways.

`server_status` is read by the Mac app every time it shows the models. Reachability is a
loopback question only: an address elsewhere answers False without being asked, so showing
the list never reaches out to another machine. The model server's `served_model` comes from
the same GET as its `reachable` (`probe`), so the two cannot disagree: `model` is what the
settings say should be served, `served_model` what the running server says it serves.
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
    probe: Callable[[str], tuple[bool, str | None]],
    which: Callable[[str], str | None],
) -> dict[str, Any]:
    """The model server, the voice server and the local agent, as the settings describe
    them and as the machine finds them. `installed` is asked "llm" or "voice"; `probe` is
    asked a `…/v1` address, and only when the settings name one. No key is in it."""
    model = settings.llm_server_model
    llm_url = f"http://{HOST}:{settings.llm_server_port}/v1" if model else None
    llm_up, served = probe(llm_url) if llm_url else (False, None)
    voice_url = settings.voice_base_url or None
    return {
        "llm": {
            "model": model,
            "port": settings.llm_server_port,
            "url": llm_url,
            "binary": runtimes.llama_server(settings.cache_dir, lookup=which),
            "unit_installed": installed("llm"),
            "reachable": llm_up,
            "served_model": served,
        },
        "voice": {
            "url": voice_url,
            "port": settings.voice_server_port,
            "unit_installed": installed("voice"),
            "reachable": bool(voice_url) and probe(voice_url)[0],
        },
        "local_agent": {
            "base_url": settings.local_agent_base_url,
            "model": settings.local_agent_model,
            "api": settings.local_agent_api,
            "enabled": "local" in settings.enabled_agents,
            "default": settings.agent_backend == "local",
        },
    }


def probe(
    url: str, *, get: Callable[..., httpx.Response] | None = None
) -> tuple[bool, str | None]:
    """Whether a server on this machine answers `GET {url}/models` within a second, and the
    first model id it lists there (llama-server's own name for what it has loaded), or None.

    Any answer short of a server error counts as up: the voice server speaks the Realtime
    socket and need not list models, and a 404 from it is still a server that is up. An
    address that is not on this machine is not up and is never asked, and the question
    never goes through a proxy (`trust_env=False`): `HTTP_PROXY`, `ALL_PROXY` or macOS's
    system proxy would carry even a 127.0.0.1 request off the machine, and the proxy's 407
    would read as a server that is up.
    """
    try:
        endpoint = Endpoint.parse(url)
    except ValueError:
        return False, None
    if not endpoint.is_loopback:
        return False, None
    try:
        response = (get or httpx.get)(
            endpoint.url("models"), timeout=REACH_TIMEOUT_S, trust_env=False
        )
    except httpx.HTTPError:
        return False, None
    if response.status_code >= 500:
        return False, None
    return True, _first_model_id(response)


def _first_model_id(response: httpx.Response) -> str | None:
    """`data[0].id` of an OpenAI model list, or None for any other answer."""
    if not response.is_success:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    model = first.get("id") if isinstance(first, dict) else None
    return model if isinstance(model, str) and model else None


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
