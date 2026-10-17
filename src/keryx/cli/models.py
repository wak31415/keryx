"""`keryx models`: the models that fit this machine, and the servers that run them."""

import dataclasses
import functools
import json
from typing import Annotated

import typer

from keryx.cli.apps import models_app
from keryx.cli.common import (
    _configure,
    _configure_readonly,
    _foreign_servers,
    _install_units,
    _owner_only,
    _owner_refusal,
    _saver,
    _store_values,
)
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.localmodels import (
    catalog,
    download,
    hardware,
    pull,
    runtimes,
    server,
    servers,
    use,
    voice,
    voice_llm,
)
from keryx.setup.sections import repo_root


def _key(entry: catalog.ModelEntry | None) -> str | None:
    return entry.key if entry is not None else None


@models_app.command("list")
def models_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The models Keryx can download, which fit this machine, and the local voices."""
    settings = _configure_readonly()
    machine = hardware.detect()
    first = voice_llm.first_set_up(settings, ConfigStore().source_of("VOICE_LLM", settings))
    advice = use.recommendations(settings, machine, first_set_up=first)
    voice_here = settings.voice_mode == "here"
    reserved = advice.chosen_gb
    agent_gb = use.agent_memory_gb(settings)
    rows = [
        {
            "name": entry.key,
            "title": entry.title,
            "size_gb": entry.size_gb,
            "needs_gb": entry.memory_gb,
            "fits": catalog.fits(entry, machine, voice_here=voice_here, voice_llm_gb=reserved),
            "fits_with_voice_llm": advice.fits_with_voice_llm(entry),
            "fits_as_voice_llm": use.fits_as_voice_llm(entry, machine, agent_gb=agent_gb),
            "downloaded": download.is_downloaded(settings.cache_dir, entry),
            "recommended": entry is advice.recommended,
            "tested": entry.tested,
            "note": entry.note,
        }
        for entry in catalog.MODELS
    ]
    if as_json:
        typer.echo(json.dumps({
            "hardware": {**dataclasses.asdict(machine), "model_memory_gb":
                         machine.model_memory_gb},
            "models_dir": str(download.models_dir(settings.cache_dir)),
            "models": rows,
            "recommended": _key(advice.recommended),
            "recommended_with_local_voice": _key(advice.with_local_voice),
            "voice_llm_recommended": advice.voice_llm,
            "voice_llm_separate": advice.voice_llm_separate,
            "voices": [{"name": name, "sounds": sounds} for name, sounds in catalog.VOICES],
            "servers": use.server_status(settings,
                                         installed=functools.partial(use.unit_installed,
                                                                     settings=settings),
                                         probe=use.probe, which=runtimes.which),
        }, indent=2))
        return
    typer.echo(f"# {machine.describe()}; models in {download.models_dir(settings.cache_dir)}")
    for row in rows:
        marks = "".join([
            "✓ fits " if row["fits"] else "✗ too big ",
            "· downloaded " if row["downloaded"] else "",
            "· recommended " if row["recommended"] else "",
            "· tested " if row["tested"] else "",
        ])
        typer.echo(f"{row['name']:<22} {row['size_gb']:>5} GB  needs {row['needs_gb']:>3} GB  "
                   f"{marks}— {row['note']}")
    words = advice.voice_llm or "the agent's model"
    typer.echo(f"# with the voice here: {_key(advice.with_local_voice) or 'nothing fits'} for "
               f"the agent, and the voice's words from {words}")
    typer.echo("\n# voices for the local voice server (VOICE_SERVER_VOICE)")
    for name, sounds in catalog.VOICES:
        typer.echo(f"{name:<22} {sounds}")


@models_app.command("pull")
def models_pull(
    name: Annotated[str, typer.Argument(help="A name from `keryx models list`.")],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: progress, then how it ended."),
    ] = False,
) -> None:
    """Download one model into CACHE_DIR/models, picking up a download that was cut off."""
    if not as_json:
        raise typer.Exit(pull.pull(_configure_readonly(), name, pull.TextReport(typer.echo)))
    events = pull.JsonLines(typer.echo)
    with pull.terminate_as_interrupt():
        code = events.guard(
            lambda: pull.pull(_configure_readonly(refuse=events.error), name, events)
        )
    raise typer.Exit(code)


@models_app.command("use")
def models_use(
    name: Annotated[str, typer.Argument(help="A downloaded model, from `keryx models list`.")],
    default_agent: Annotated[
        bool,
        typer.Option("--default-agent",
                     help="Also make the local agent the one that runs what nobody named."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make a downloaded model the local agent's, served here by llama.cpp.

    It saves what `keryx setup` would; the model server and Keryx pick it up when restarted.
    """
    _owner_only("choose the local agent's model")
    settings = _configure_readonly()
    entry = catalog.by_key(name)
    if entry is None:
        typer.echo(f"there is no model called {name} (`keryx models list`)", err=True)
        raise typer.Exit(2)
    if not download.is_downloaded(settings.cache_dir, entry):
        typer.echo(f"download it first: keryx models pull {name}", err=True)
        raise typer.Exit(1)
    values = use.llama_values(settings, entry.key, entry.key, free_port=servers.free_port)
    values |= use.enable_local(settings, default=default_agent)
    if not as_json:
        _store_values(values)
        typer.echo("next: start it with `keryx models start`, then `keryx restart`")
        return
    saved = _store_values(values, echo=False)
    typer.echo(json.dumps({
        "saved": saved,
        "llama_server": runtimes.llama_server(settings.cache_dir),
        "restart_needed": True,
    }, indent=2))


@models_app.command("serve")
def models_serve(
    kind: Annotated[str, typer.Argument(
        help="llm (the agent's llama.cpp), voice (speech-to-speech) or voice-llm (the "
             "voice's own llama.cpp).")],
) -> None:
    """Become a local server — llm, voice or voice-llm — as the settings describe it (what
    the units run)."""
    settings = _configure()
    try:
        servers.serve(kind, settings)
    except servers.ServerConfigError as error:
        typer.echo(f"keryx cannot start the {kind} server: {error}", err=True)
        raise typer.Exit(2) from None


#: What `models start|stop` are, refused to the service: they install and remove units.
RUN_THE_MODEL_SERVER = "start or stop the model server"


@models_app.command("start")
def models_start(
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="llama (llama.cpp, installed when it is missing) or "
                     "ollama (its own copy of the model, pulled)."),
    ] = server.LLAMA,
    voice_llm_unit: Annotated[
        bool,
        typer.Option("--voice-llm", help="The voice LLM's own server instead, for the model "
                     "VOICE_LLM_MODEL already names (`keryx voice llm here MODEL` chooses "
                     "one); llama.cpp only."),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: each step, then how it ended."),
    ] = False,
) -> None:
    """Run the chosen model's server here, and check that it serves that model.

    llama.cpp is installed when it is missing, then the model server's unit, then Keryx
    waits for the model to load. The model comes first: `keryx models pull NAME`, then
    `keryx models use NAME`. A server that does not come up has its unit removed. With
    --voice-llm, the same for the voice LLM's own server; nothing is saved.
    """
    if not as_json:
        _owner_only(RUN_THE_MODEL_SERVER)
        try:
            code = _models_start(_configure_readonly(), runtime, server.TextSteps(typer.echo),
                                 voice_llm_unit=voice_llm_unit)
        except KeyboardInterrupt:
            typer.echo("cancelled: `keryx models start` again picks up where this stopped",
                       err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = server.JsonSteps(typer.echo)

    def run() -> int:
        # Refused before any install, and as an event: the app reads stdout alone.
        if refusal := _owner_refusal(RUN_THE_MODEL_SERVER):
            events.error(refusal)
            return 1
        return _models_start(_configure_readonly(refuse=events.error), runtime, events,
                             voice_llm_unit=voice_llm_unit)

    with pull.terminate_as_interrupt():
        code = events.guard(run)
    raise typer.Exit(code)


def _models_start(settings: Settings, runtime: str, report: server.StartReport, *,
                  voice_llm_unit: bool = False) -> int:
    if _foreign_servers(settings, report):
        return voice.REFUSED
    if runtime not in server.RUNTIMES:
        report.error(f"--runtime is {' or '.join(server.RUNTIMES)}, not {runtime}")
        return voice.REFUSED
    if voice_llm_unit:
        if runtime != server.LLAMA:
            report.error("The voice LLM's own server runs on llama.cpp only.",
                         detail="--voice-llm takes no --runtime ollama: an Ollama of yours "
                                "is `keryx voice llm remote http://127.0.0.1:11434 --model M`")
            return voice.REFUSED
        return voice_llm.start(settings, server.machine(), report, root=repo_root())
    return server.start(settings, server.machine(), report, runtime=runtime,
                        save=_saver(ConfigStore(), settings, report), root=repo_root())


@models_app.command("stop")
def models_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Stop the model server and remove its unit; `keryx models start` brings it back."""
    _install_units(llm_unit=True, uninstall=True, as_json=as_json, verb=RUN_THE_MODEL_SERVER)
