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
from dataclasses import dataclass
from typing import Any

import httpx

from keryx.config import Settings
from keryx.endpoints import Endpoint
from keryx.localmodels import catalog, runtimes
from keryx.localmodels.catalog import ModelEntry
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.servers import HOST, free_port_avoiding
from keryx.restart import service
from keryx.restart.service import ServiceTarget

#: How long a loopback server may take to answer whether it is up.
REACH_TIMEOUT_S = 1.0
#: Each server's unit, by service manager: what `scripts/install-*.sh --llm|--voice|
#: --voice-llm` install. `keryx.units` lists them beside Keryx's own.
UNITS = {
    "launchd": {"llm": "dev.keryx.llm", "voice": "dev.keryx.voice",
                "voice_llm": "dev.keryx.voice-llm"},
    "systemd": {"llm": "keryx-llm.service", "voice": "keryx-voice.service",
                "voice_llm": "keryx-voice-llm.service"},
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
    and otherwise asked for, from `AGENT_SERVER_PORT` up, so it never lands on another's,
    nor on the voice LLM's or the voice server's, which may not be running yet.
    """
    port = settings.agent_server_port
    if not settings.agent_server_model:
        port = free_port_avoiding(free_port, port, {settings.voice_llm_server_port,
                                                    settings.voice_server_port})
    return {
        "AGENT_SERVER_MODEL": model,
        "AGENT_SERVER_PORT": port,
        "LOCAL_AGENT_BASE_URL": f"http://{HOST}:{port}/v1",
        "LOCAL_AGENT_MODEL": alias,
        **messages_harness(),
    }


def voice_llm_port(settings: Settings, *, free_port: Callable[[int], int]) -> int:
    """A port for the voice LLM's own server, from `VOICE_LLM_SERVER_PORT` up, never the
    agent's model server's or the voice server's, whether or not they are running."""
    return free_port_avoiding(free_port, settings.voice_llm_server_port,
                              {settings.agent_server_port, settings.voice_server_port})


def agent_memory_gb(settings: Settings) -> float | None:
    """The memory the local agent's model takes on this machine: its catalog figure while it
    is served here (llama.cpp's `AGENT_SERVER_MODEL`, or Ollama's copy of a catalog model on
    loopback), 0 while none is (no local agent, or one elsewhere), and None for one served
    here whose size Keryx cannot tell (a GGUF of your own, another model on a server here)."""
    if settings.agent_server_model:
        entry = catalog.by_key(settings.agent_server_model)
        return entry.memory_gb if entry is not None else None
    agent = settings.local_agent_endpoint
    if agent is None or not agent.is_loopback:
        return 0.0
    entry = next((e for e in catalog.MODELS if e.ollama == settings.local_agent_model), None)
    return entry.memory_gb if entry is not None else None


def voice_llm_memory_gb(settings: Settings) -> float | None:
    """The memory the voice's own LLM takes on this machine: its catalog figure while it
    runs here (`VOICE_LLM=here` with a model named), 0 while the voice's words are the local
    agent's (counted as the agent's) or come from elsewhere, and None for a GGUF of your own,
    whose size Keryx cannot tell."""
    if settings.voice_llm != "here" or not settings.voice_llm_model:
        return 0.0
    entry = catalog.by_key(settings.voice_llm_model)
    return entry.memory_gb if entry is not None else None


def d2_choice(settings: Settings, hardware: Hardware) -> ModelEntry | None:
    """The owner's rule (D2) for this machine as it is now: `catalog.voice_llm_pick` beside
    the agent's model that runs here (`agent_memory_gb`; 0 when none does). The one call
    both a first `keryx voice use here` (`voice_llm.first_choice`) and `recommendations`
    make, so what the Mac app is told is what the command brings up."""
    return catalog.voice_llm_pick(hardware, agent_gb=agent_memory_gb(settings))


def fits_as_voice_llm(
    entry: ModelEntry, hardware: Hardware, *, agent_gb: float | None
) -> bool:
    """Whether `entry` fits as the voice's own LLM beside the agent's model that runs here
    (`agent_gb`; 0 when the agent's work is not here, the chosen model's memory when it is,
    None when it runs here at a size Keryx cannot tell) and the speech models.

    `catalog.fits`'s own sum (`catalog.budget`, `voice_here=True`) is what `voice_llm_pick`
    judges every candidate by inside `d2_choice`'s own call — `entry`'s memory plus
    `agent_gb` plus the speech share either way, addition being the same sum whichever
    name holds which term — so a voice-model list built from this and what
    `voice_llm_recommended` names can never disagree.
    """
    return agent_gb is not None and catalog.fits(entry, hardware, voice_here=True,
                                                 voice_llm_gb=agent_gb)


@dataclass(frozen=True)
class Recommendations:
    """What `keryx models list --json` recommends for this machine, so the Mac app never
    works out a fit itself. Every figure is `catalog.budget`'s sum.

    - `voice_llm`: the voice's own model for a voice here — the one already chosen
      (`VOICE_LLM=here`), else, on a first set-up, exactly what `keryx voice use here` would
      bring up now (`d2_choice`); None is none of its own (shared, or elsewhere).
    - `recommended`: the agent's model as things stand — the speech models reserved while the
      voice is here (`Settings.voice_mode`), and a voice LLM already chosen.
    - `with_local_voice`: the agent's model with the voice here and `voice_llm` reserved, so
      the two always fit together.
    """

    hardware: Hardware
    recommended: ModelEntry | None
    with_local_voice: ModelEntry | None
    voice_llm: str | None
    #: The memory `voice_llm` takes (0 for a GGUF of the owner's own, which cannot be
    #: sized), or None when there is none.
    voice_llm_gb: float | None
    #: The memory a voice LLM already chosen takes: what `recommended` reserves.
    chosen_gb: float

    @property
    def voice_llm_separate(self) -> bool:
        """Whether the voice here has a model of its own (D2 applied), not the agent's."""
        return self.voice_llm is not None

    def fits_with_voice_llm(self, entry: ModelEntry) -> bool:
        """Whether `entry`, as the agent's model, leaves room for the voice here and a model
        of its own: `voice_llm`, else any the owner's rule would pick beside it."""
        if self.voice_llm_gb is not None:
            return catalog.fits(entry, self.hardware, voice_here=True,
                                voice_llm_gb=self.voice_llm_gb)
        return catalog.voice_llm_pick(self.hardware, agent_gb=entry.memory_gb) is not None


def recommendations(settings: Settings, hardware: Hardware, *,
                    first_set_up: bool) -> Recommendations:
    """`Recommendations` for `hardware`, as `settings` describe the voice and the agent;
    `first_set_up` is `voice_llm.first_set_up`, the one time `voice use here` chooses."""
    voice_llm: str | None = None
    voice_llm_gb: float | None = None
    chosen_gb = 0.0
    if settings.voice_llm == "here" and settings.voice_llm_model:
        voice_llm = settings.voice_llm_model
        voice_llm_gb = chosen_gb = voice_llm_memory_gb(settings) or 0.0
    elif first_set_up and (pick := d2_choice(settings, hardware)) is not None:
        voice_llm, voice_llm_gb = pick.key, pick.memory_gb
    recommended = catalog.recommend(hardware, voice_here=settings.voice_mode == "here",
                                    voice_llm_gb=chosen_gb)
    with_voice = catalog.recommend(hardware, voice_here=True, voice_llm_gb=voice_llm_gb or 0.0)
    return Recommendations(hardware, recommended, with_voice, voice_llm, voice_llm_gb,
                           chosen_gb)


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
    model = settings.agent_server_model
    llm_url = f"http://{HOST}:{settings.agent_server_port}/v1" if model else None
    llm_up, served = probe(llm_url) if llm_url else (False, None)
    voice_url = settings.voice_base_url or None
    return {
        "llm": {
            "model": model,
            "port": settings.agent_server_port,
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
    """Whether the `kind` ("llm", "voice" or "voice_llm") server's unit is installed: its
    manager has it loaded, or its unit file is on disk — a server `keryx service stop
    --all` booted out is installed and stopped, as `service --json` says it."""
    if platform == "darwin":
        manager = "launchd"
    elif platform.startswith("linux"):
        manager = "systemd"
    else:
        return False
    # Late-bound through the module, as `keryx.status` binds it.
    ask = is_installed or service.is_present
    return ask(ServiceTarget(manager, UNITS[manager][kind]))
