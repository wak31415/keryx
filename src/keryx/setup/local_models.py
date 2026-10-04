"""`keryx setup`'s "Local models" section: the agent and the voice on your own hardware.

Two questions first — where the local agent's model runs, and where the voice does — and
then the work, so the model can be chosen knowing whether the voice will share its GPU:

- **On this machine.** What the machine holds is shown, the catalog is offered with what
  fits it marked and the best one recommended (`keryx.localmodels.catalog`), and Keryx
  downloads it — or links a copy a Hugging Face tool already downloaded — after checking
  the disk. It runs under llama.cpp (installed if it is not here) as a user service of its
  own, or under Ollama when that is what the machine has. The voice is speech-to-speech,
  installed as a `uv tool` with a voice chosen from Kokoro's, run as a second service whose
  words come from the same model.
- **A server elsewhere.** An address and an optional key, checked by asking it for its
  models (the agent) or for a Realtime session (the voice), with `endpoints.warnings` said
  out loud.

Nothing is saved as the voice until the voice server has answered: a `VOICE_BASE_URL` that
does not work is a phone that rings and says nothing. Every download, install and network
read goes through `Probes`.
"""

import asyncio
import sys
from pathlib import Path

from keryx.agents.registry import BACKENDS
from keryx.config.store import FROM_DEFAULT
from keryx.endpoints import Endpoint, warnings
from keryx.localmodels import catalog
from keryx.localmodels.catalog import VOICE_SERVER_DOWNLOAD_GB, VOICE_SERVER_GB, ModelEntry
from keryx.localmodels.download import (
    DISK_MARGIN_BYTES,
    DownloadError,
    adopt,
    ensure_model_dir,
    is_downloaded,
    model_path,
    models_dir,
    remaining_bytes,
)
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.runtimes import LLAMA_CPP_BUILD, s2s_install_argv, which
from keryx.localmodels.servers import HOST
from keryx.setup.agents import passed_smoke
from keryx.setup.context import SetupContext
from keryx.setup.sections import repo_root
from keryx.setup.ui import Choice

HERE, ELSEWHERE, OPENAI, NONE, KEEP = "here", "elsewhere", "openai", "none", "keep"
OWN_FILE = "\0file"
#: How long a model server may take to load, and the voice server its first start, which
#: downloads its speech models.
LLM_START_S = 600.0
VOICE_START_S = 1200.0
OLLAMA_URL = "http://127.0.0.1:11434"
API_CHOICES = (
    Choice("anthropic-messages", "Anthropic Messages API", hint="Claude Code drives it — "
           "llama.cpp, Ollama 0.14+, LM Studio 0.4.1+"),
    Choice("openai-responses", "OpenAI Responses API", hint="Codex drives it — vLLM, "
           "llama.cpp, Ollama 0.13.3+"),
)


def run_section(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    ui.note(
        "Keryx can run the work and the voice on your own hardware instead of Anthropic's, "
        "OpenAI's or Google's: a model on this machine, or on another one you can reach."
    )
    machine = ctx.probes.hardware()
    free = ctx.probes.free_bytes(models_dir(settings.cache_dir))
    ui.note(f"This machine: {machine.describe()}; {free / 1e9:.0f} GB free for models.")
    agent = _where_agent(ctx)
    voice = _where_voice(ctx)
    if agent == HERE:
        _agent_here(ctx, machine, reserve_gb=VOICE_SERVER_GB if voice == HERE else 0.0)
    elif agent == ELSEWHERE:
        _agent_elsewhere(ctx)
    elif agent == NONE:
        _agent_off(ctx)
    if voice == HERE:
        _voice_here(ctx)
    elif voice == ELSEWHERE:
        _voice_elsewhere(ctx)
    elif voice == OPENAI:
        _voice_to_openai(ctx)


# --- the two questions -------------------------------------------------------------------


def _where_agent(ctx: SetupContext) -> str:
    current = ctx.settings.local_agent_endpoint
    options = [
        Choice(HERE, "On this machine", hint="download a model; Keryx runs it"),
        Choice(ELSEWHERE, "On a server elsewhere",
               hint="Ollama, llama.cpp, vLLM or LM Studio at an address"),
        Choice(NONE, "No local agent", hint="Claude and Codex only"),
    ]
    if current is not None:
        options.insert(0, Choice(KEEP, f"Keep {current.model} at {current.base_url}"))
    return ctx.ui.select("Where should the local agent's model run?", options,
                         default=options[0].value)


def _where_voice(ctx: SetupContext) -> str:
    settings = ctx.settings
    options = [
        Choice(OPENAI, "OpenAI's Realtime API", hint="the most natural voice; billed per minute"),
        Choice(HERE, "On this machine", hint="speech-to-speech, with the local model's words"),
        Choice(ELSEWHERE, "On a server elsewhere", hint="anything that speaks the Realtime API"),
    ]
    if settings.voice_base_url:
        options.insert(0, Choice(KEEP, f"Keep the voice server at {settings.voice_base_url}"))
    return ctx.ui.select("And the voice on a call?", options, default=options[0].value)


# --- the agent ---------------------------------------------------------------------------


def _agent_here(ctx: SetupContext, machine: Hardware, *, reserve_gb: float) -> None:
    ui, settings = ctx.ui, ctx.settings
    runtime = _runtime(ctx, machine)
    if runtime is None:
        return
    entry, own = _pick_model(ctx, machine, reserve_gb, allow_file=runtime == "llama")
    if entry is None and own is None:
        return
    if runtime == "ollama":
        assert entry is not None
        ui.note(f"Pulling {entry.title} into Ollama ({entry.size_gb} GB).")
        if ctx.probes.run_script(["ollama", "pull", entry.ollama]) != 0:
            ui.error("ollama pull did not finish")
            return
        values = {"LOCAL_AGENT_BASE_URL": f"{OLLAMA_URL}/v1", "LOCAL_AGENT_MODEL": entry.ollama,
                  "LLM_SERVER_MODEL": None}
    else:
        if entry is not None and not _have_model(ctx, entry):
            return
        port = settings.llm_server_port
        if not settings.llm_server_model:  # not already ours: make sure nothing else is there
            port = ctx.probes.free_port(port)
        alias = entry.key if entry is not None else own.rsplit("/", 1)[-1].removesuffix(".gguf")
        values = {
            "LLM_SERVER_MODEL": entry.key if entry is not None else own,
            "LLM_SERVER_PORT": port,
            "LOCAL_AGENT_BASE_URL": f"http://{HOST}:{port}/v1",
            "LOCAL_AGENT_MODEL": alias,
        }
    values |= {"LOCAL_AGENT_API_KEY": None, "LOCAL_AGENT_API": "anthropic-messages"}
    if not ctx.save(values):
        return
    if runtime == "llama":
        if not _install_units(ctx, "--llm"):
            return
        endpoint = ctx.settings.local_agent_endpoint
        assert endpoint is not None
        with ui.spinner(f"Loading the model into llama.cpp at {endpoint.base_url}…"):
            problem = ctx.probes.wait_for_server(endpoint, "models", LLM_START_S)
        if problem is not None:
            ui.error(f"the model server did not come up: {problem}")
            ui.note(f"Its log: {settings.state_dir / 'logs' / 'keryx-llm.err.log'}")
            return
        ui.success("the model server is running")
    _use_local(ctx)


def _runtime(ctx: SetupContext, machine: Hardware) -> str | None:
    """llama.cpp (installing it if it is missing) or Ollama; None when neither will do."""
    ui, settings = ctx.ui, ctx.settings
    llama = ctx.probes.llama_server(settings.cache_dir)
    ollama = ctx.probes.ollama()
    if llama is not None:
        ui.success(f"llama.cpp: {llama}")
        return "llama"
    if ollama is not None:
        how = ui.select("What should run the model?", [
            Choice("ollama", "Ollama", hint=f"already here: {ollama}"),
            Choice("llama", "llama.cpp", hint=f"Keryx installs build {LLAMA_CPP_BUILD}"),
        ], default="ollama")
        if how == "ollama":
            return "ollama"
    if not ui.confirm(f"llama.cpp runs the model; install it ({LLAMA_CPP_BUILD})?", default=True):
        return None
    if sys.platform == "darwin" and which("brew"):
        if ctx.probes.run_script(["brew", "install", "llama.cpp"]) != 0:
            ui.error("brew install llama.cpp did not finish")
            return None
        return "llama"
    with ui.spinner("Downloading llama.cpp…"):
        try:
            found = ctx.probes.install_llama_cpp(settings.cache_dir, machine)
        except Exception as exc:  # a network or archive failure, said in a sentence
            ui.error(f"could not install llama.cpp: {exc}")
            return None
    ui.success(f"llama.cpp: {found}")
    return "llama"


def _pick_model(
    ctx: SetupContext, machine: Hardware, reserve_gb: float, *, allow_file: bool
) -> tuple[ModelEntry | None, str | None]:
    """A catalog entry, or the path of a GGUF of the owner's own; (None, None) for neither."""
    ui, settings = ctx.ui, ctx.settings
    best = catalog.recommend(machine, reserve_gb=reserve_gb)
    if reserve_gb:
        ui.note(f"{reserve_gb:.0f} GB is kept for the voice server's own models.")
    rows, choices = [], []
    for entry in catalog.MODELS:
        fits = catalog.fits(entry, machine, reserve_gb=reserve_gb)
        marks = [mark for mark, on in (
            ("recommended", entry is best), ("tested", entry.tested),
            ("downloaded", is_downloaded(settings.cache_dir, entry)),
        ) if on]
        rows.append((entry.title, f"{entry.size_gb} GB", f"{entry.memory_gb:.0f} GB",
                     "yes" if fits else "no", entry.note))
        choices.append(Choice(
            entry.key, f"{entry.title} ({entry.size_gb} GB)", hint=" · ".join(marks),
            disabled=None if fits else f"needs {entry.memory_gb:.0f} GB",
        ))
    ui.table(("Model", "Download", "Needs", "Fits", ""), rows)
    if allow_file:
        choices.append(Choice(OWN_FILE, "A GGUF file of my own"))
    if best is None:
        ui.warn("Nothing in the list fits this machine; a smaller GGUF of your own, or a server "
                "elsewhere, will.")
        if not allow_file:
            return None, None
    current = settings.llm_server_model
    default = current if catalog.by_key(current or "") else (best.key if best else OWN_FILE)
    pick = ui.select("Which model?", choices, default=default)
    if pick != OWN_FILE:
        return catalog.by_key(pick), None
    path = ui.text("The GGUF file's path", default=current or "", validate=_gguf)
    return None, path


def _gguf(value: str) -> str | None:
    path = Path(value.strip()).expanduser()
    if not value.strip().endswith(".gguf"):
        return "A .gguf file."
    return None if path.is_file() else "There is no file there."


def _have_model(ctx: SetupContext, entry: ModelEntry) -> bool:
    """The model on disk, downloaded or linked; False (said) when it could not be."""
    ui, cache = ctx.ui, ctx.settings.cache_dir
    if is_downloaded(cache, entry):
        ui.success(f"{entry.title} is already downloaded")
        return True
    found = ctx.probes.found_model(entry)
    if found is not None:
        adopt(cache, entry, found)
        ui.success(f"{entry.title} was already downloaded, in {found.parent}: linked, not copied")
        return True
    dest = model_path(cache, entry)
    need = remaining_bytes(cache, entry) + DISK_MARGIN_BYTES
    free = ctx.probes.free_bytes(dest.parent)
    if free < need:
        ui.error(f"{entry.title} needs {need / 1e9:.1f} GB free on {dest.parent}, and there "
                 f"is {free / 1e9:.1f} GB. Free some space, or pick a smaller model.")
        return False
    ensure_model_dir(cache, entry)
    try:
        with ui.progress(f"Downloading {entry.title}", entry.size) as advance:
            ctx.probes.download(entry.url, dest, size=entry.size, sha256=entry.sha256,
                                progress=advance)
    except DownloadError as error:
        ui.error(str(error))
        return False
    ui.success(f"downloaded {entry.title} to {dest}")
    return True


def _agent_elsewhere(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    current = settings.local_agent_endpoint
    url = ui.text("The server's address (http://gpu-box:11434, or its …/v1)",
                  default=current.base_url if current else "", validate=_address)
    key = ui.secret("Its key — blank if it has none", current=settings.local_agent_api_key or "")
    endpoint = Endpoint.parse(url, api_key=key or None)
    for warning in warnings(endpoint):
        ui.warn(warning)
    with ui.spinner(f"Asking {endpoint.host} for its models…"):
        found = ctx.probes.endpoint_models(endpoint)
    if not found.ok:
        ui.error(found.problem or "no answer")
        if not ui.confirm("Keep it anyway?", default=False):
            return
    if found.models:
        model = ui.select("Which of its models?", [Choice(name, name) for name in found.models],
                          default=current.model if current and current.model in found.models
                          else found.models[0])
    else:
        model = ui.text("The model's name on that server",
                        default=current.model if current else "", validate=_not_blank)
    api = ui.select("Which API does it speak?", list(API_CHOICES),
                    default=settings.local_agent_api)
    if ctx.save({"LOCAL_AGENT_BASE_URL": endpoint.base_url, "LOCAL_AGENT_API_KEY": key or None,
                 "LOCAL_AGENT_MODEL": model, "LOCAL_AGENT_API": api, "LLM_SERVER_MODEL": None}):
        _use_local(ctx)


def _use_local(ctx: SetupContext) -> None:
    """Enable the local agent, ask whether it is the default, and run one real task on it."""
    ui, settings = ctx.ui, ctx.settings
    enabled = list(dict.fromkeys([*settings.enabled_agents, "local"]))
    backend = settings.agent_backend
    if backend != "local" and ui.confirm(
        "Should the local model do the work when you do not name an agent?", default=False
    ):
        backend = "local"
    if not ctx.save({"AGENTS_ENABLED": ",".join(enabled), "AGENT_BACKEND": backend}):
        return
    label = BACKENDS["local"].label
    with ui.spinner(f"Running one real task on the {label.lower()}…"):
        result = asyncio.run(ctx.probes.smoke(ctx.settings, "local"))
    if passed_smoke(result):
        ui.success("the local model ran a task")
    else:
        why = result.error or result.spoken_summary or "no answer"
        ui.error(f"the local model could not run a task: {why}")


def _agent_off(ctx: SetupContext) -> None:
    settings = ctx.settings
    if settings.local_agent_endpoint is None and "local" not in settings.enabled_agents:
        return
    enabled = [name for name in settings.enabled_agents if name != "local"] or ["claude"]
    backend = settings.agent_backend if settings.agent_backend != "local" else enabled[0]
    ctx.save({"AGENT_BACKEND": backend,
              "AGENTS_ENABLED": ",".join(enabled) if len(enabled) > 1 else "",
              "LOCAL_AGENT_BASE_URL": None, "LOCAL_AGENT_MODEL": None})
    if settings.llm_server_model:
        ctx.ui.note("The model server is still installed: scripts/install-systemd.sh --llm "
                    "--uninstall removes it.")


# --- the voice ---------------------------------------------------------------------------


def _voice_here(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    words = settings.local_agent_endpoint
    if words is None or not words.model:
        ui.error("The voice server takes its words from a local model, and there is none: "
                 "choose one for the agent first.")
        return
    if ctx.probes.speech_to_speech() is None:
        free = ctx.probes.free_bytes(settings.cache_dir)
        if free < (VOICE_SERVER_DOWNLOAD_GB + 2) * 1e9:
            ui.error(f"The voice server needs about {VOICE_SERVER_DOWNLOAD_GB + 2:.0f} GB free "
                     f"for itself and its speech models; there is {free / 1e9:.1f} GB.")
            return
        ui.note("Installing speech-to-speech, Hugging Face's voice pipeline, with uv.")
        if ctx.probes.run_script(s2s_install_argv()) != 0:
            ui.error("the install did not finish")
            return
    voice = ui.select("Which voice?", [
        Choice(name, name, hint=sounds) for name, sounds in catalog.VOICES
    ], default=_current_voice(ctx))
    port = settings.voice_server_port
    if not _is_ours(settings.voice_base_url, port):
        port = ctx.probes.free_port(port)
    if not ctx.save({"VOICE_SERVER_VOICE": voice, "VOICE_SERVER_PORT": port}):
        return
    if not _install_units(ctx, "--voice"):
        return
    endpoint = Endpoint.parse(f"http://{HOST}:{port}")
    with ui.spinner("Starting the voice server — its first start downloads its speech models, "
                    "which takes a few minutes…"):
        problem = ctx.probes.wait_for_server(endpoint, "realtime", VOICE_START_S)
    if problem is not None:
        ui.error(f"the voice server did not come up: {problem}")
        ui.note(f"Its log: {settings.state_dir / 'logs' / 'keryx-voice.err.log'}")
        return
    if ctx.save({"VOICE_BASE_URL": endpoint.base_url, "VOICE_API_KEY": None}):
        ui.success("calls now use the voice server on this machine")
        _after_voice(ctx)


def _current_voice(ctx: SetupContext) -> str:
    settings = ctx.settings
    names = [name for name, _sounds in catalog.VOICES]
    chosen = ctx.store.source_of("VOICE_SERVER_VOICE", settings) != FROM_DEFAULT
    if chosen and settings.voice_server_voice in names:
        return settings.voice_server_voice
    return catalog.persona_voice(settings.assistant_name)


def _is_ours(voice_base_url: str | None, port: int) -> bool:
    return voice_base_url == f"http://{HOST}:{port}/v1"


def _voice_elsewhere(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    url = ui.text("The voice server's address (http://gpu-box:8765, or its …/v1)",
                  default=settings.voice_base_url or "", validate=_address)
    key = ui.secret("Its key — blank if it has none", current=settings.voice_api_key or "")
    endpoint = Endpoint.parse(url, api_key=key or None)
    for warning in warnings(endpoint, carries_voice=True):
        ui.warn(warning)
    with ui.spinner(f"Opening a Realtime session on {endpoint.host}…"):
        problem = ctx.probes.realtime_problem(endpoint)
    if problem is not None:
        ui.error(problem)
        if not ui.confirm("Keep it anyway? Calls will not open until it answers.", default=False):
            return
    if ctx.save({"VOICE_BASE_URL": endpoint.base_url, "VOICE_API_KEY": key or None}):
        _after_voice(ctx)


def _after_voice(ctx: SetupContext) -> None:
    if not ctx.settings.openai_key:
        ctx.ui.note("Without an OpenAI key the assistant has no web search; the Voice section "
                    "takes one, for that alone.")


def _voice_to_openai(ctx: SetupContext) -> None:
    if ctx.settings.voice_base_url and ctx.save({"VOICE_BASE_URL": None, "VOICE_API_KEY": None}):
        ctx.ui.success("calls use OpenAI's Realtime API again")
        if not ctx.settings.openai_key:
            ctx.ui.note("It needs OPENAI_API_KEY: the Voice section takes it.")


# --- shared ------------------------------------------------------------------------------


def _install_units(ctx: SetupContext, flag: str) -> bool:
    name = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    script = repo_root() / "scripts" / name
    if not script.is_file():
        ctx.ui.error(f"scripts/{name} is not here: run it with {flag} from a clone of Keryx")
        return False
    code = ctx.probes.run_script([str(script), flag])
    if code != 0:
        ctx.ui.error(f"{name} {flag} exited with {code}")
    return code == 0


def _address(value: str) -> str | None:
    try:
        Endpoint.parse(value)
    except ValueError:
        return "An http:// or https:// address."
    return None


def _not_blank(value: str) -> str | None:
    return None if value.strip() else "It cannot be blank."

