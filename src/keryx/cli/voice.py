"""`keryx voice`: where the voice on a call is, and where its words come from."""

import functools
import json
from collections.abc import Callable
from typing import Annotated, Any

import typer

from keryx.cli.apps import voice_app, voice_llm_app, voice_use_app
from keryx.cli.common import (
    _configure_readonly,
    _foreign_servers,
    _owner_only,
    _owner_refusal,
    _read_secret_input,
    _saver,
    _store_values,
)
from keryx.config import Settings, field_for, is_secret
from keryx.config.store import ConfigStore
from keryx.localmodels import pull, runtimes, speech, use, voice, voice_llm
from keryx.setup.sections import repo_root


@voice_app.callback(invoke_without_command=True)
def voice_command(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Where the voice on a call is, the voice server on this machine, and where its words
    come from (the voice LLM)."""
    if ctx.invoked_subcommand is not None:
        return
    settings = _configure_readonly()
    document = voice.status(settings, program=runtimes.speech_to_speech,
                            installed=functools.partial(use.unit_installed,
                                                        settings=settings),
                            probe=use.probe,
                            server_reachable=voice.server_reachable,
                            models_ready=speech.models_ready, llm_up=voice.words_up,
                            own_llm_up=voice.voice_llm_up)
    if as_json:
        typer.echo(json.dumps(document, indent=2))
        return
    yes = {True: "yes", False: "no"}
    if document["mode"] == voice.HERE:
        typer.echo(f"the voice: speech-to-speech on this machine: {document['base_url']}")
    elif document["mode"] == voice.SERVER:
        typer.echo(f"the voice: a voice server elsewhere: {document['base_url']} "
                   f"(answers: {yes[document['server']['reachable']]})")
    else:
        typer.echo(f"the voice: OpenAI's Realtime API, {document['realtime_model']}, "
                   f"speaking as {document['voice']}"
                   + ("" if document["openai_key_set"] else " — OPENAI_API_KEY is not set"))
    here = document["here"]
    typer.echo(f"speech-to-speech here: {here['program'] or 'not installed'}; voice "
               f"{here['voice']} on port {here['port']}; unit installed: "
               f"{yes[here['unit_installed']]}; answers: {yes[here['reachable']]}")
    typer.echo(voice.words_line(document))
    typer.echo(f"its speech: {here['stt']} to hear, {here['tts']} to speak; downloaded: "
               f"{yes[here['models_ready']]}" + ("" if here["models_ready"] else
                                                  " (keryx voice download)"))


#: What `voice use` is, refused to the service: `VOICE_BASE_URL` is protected.
CHOOSE_THE_VOICE = "choose the voice"


def _shown(saved: dict[str, Any]) -> dict[str, Any]:
    """What was saved, a secret's value as "(set)": never the value itself."""
    def secret(key: str) -> bool:
        name = field_for(key)
        return name is not None and is_secret(name)

    return {key: "(set)" if value is not None and secret(key) else value
            for key, value in saved.items()}


@voice_use_app.command("openai")
def voice_use_openai(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make OpenAI's Realtime API the voice again (it needs OPENAI_API_KEY)."""
    _owner_only(CHOOSE_THE_VOICE)
    settings = _configure_readonly()
    if as_json:
        saved = _store_values(voice.to_openai(settings), echo=False)
        typer.echo(json.dumps({"saved": _shown(saved), "restart_needed": True}, indent=2))
        return
    _store_values(voice.to_openai(settings))
    typer.echo("calls use OpenAI's Realtime API again; `keryx restart` to have Keryx take it up")
    if not settings.openai_key:
        typer.echo("it needs OPENAI_API_KEY: keryx config set OPENAI_API_KEY --stdin")


@voice_use_app.command("server")
def voice_use_server(
    url: Annotated[str, typer.Argument(help="Its address: http://gpu-box:8765, or its …/v1.")],
    key_stdin: Annotated[
        bool, typer.Option("--key-stdin", help="Read its key from standard input.")
    ] = False,
    clear_key: Annotated[
        bool, typer.Option("--clear-key", help="Forget the key it had, and ask without one.")
    ] = False,
    keep_anyway: Annotated[
        bool, typer.Option("--keep-anyway", help="Save it although it did not answer.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Make a voice server elsewhere the voice, once it has opened a Realtime session.

    One that does not is saved only with --keep-anyway. The key it has is kept, and asked
    with, unless --key-stdin replaces it or --clear-key forgets it; it is never on the
    command line.
    """
    _owner_only(CHOOSE_THE_VOICE)  # before the handshake, which carries the stored key
    settings = _configure_readonly()
    if key_stdin and clear_key:
        typer.echo("--key-stdin replaces the key and --clear-key forgets it: one of them",
                   err=True)
        raise typer.Exit(2)
    if key_stdin:
        key = _read_secret_input("VOICE_API_KEY")
        if not key:
            typer.echo("no key on stdin (--clear-key forgets the one it has)", err=True)
            raise typer.Exit(2)
    else:
        key = None if clear_key else settings.voice_api_key
    steps = voice.Silent() if as_json else voice.TextSteps(typer.echo)
    try:
        check = voice.check_server(url, key, probes=voice.machine(), report=steps)
    except ValueError as error:  # its own words, never the address: it may hold a password
        typer.echo(f"VOICE_BASE_URL {error}", err=True)
        raise typer.Exit(2) from None
    if check.problem is not None and not keep_anyway:
        if as_json:
            typer.echo(json.dumps({"saved": None, **_server_problem(check),
                                   "warnings": check.warnings, "restart_needed": False},
                                  indent=2))
        else:
            typer.echo(check.problem, err=True)
            typer.echo("nothing saved: calls would not open until it answers "
                       "(--keep-anyway saves it all the same)", err=True)
        raise typer.Exit(1)
    values = check.values(settings, with_key=key_stdin or clear_key)
    if not as_json:
        _store_values(values)
        typer.echo("next: `keryx restart`, to have Keryx take it up")
        return
    saved = _store_values(values, echo=False)
    typer.echo(json.dumps({"saved": _shown(saved), **_server_problem(check),
                           "warnings": check.warnings, "restart_needed": True}, indent=2))


#: `voice use server --json`'s `problem` while the server opened no session: for a person.
SERVER_REFUSED = "The voice server did not open a Realtime session."


def _server_problem(check: voice.ServerCheck) -> dict[str, str | None]:
    """`problem` for the app, and `detail` — what the server or its socket said, one line
    with no credential in it (`realtime.openai.handshake`) — both null when it answered."""
    if check.problem is None:
        return {"problem": None, "detail": None}
    return {"problem": SERVER_REFUSED, "detail": check.problem}


@voice_use_app.command("here")
def voice_use_here(
    name: Annotated[
        str | None,
        typer.Option("--voice", help="A Kokoro voice (`keryx models list`); else the one "
                     "chosen before, or the assistant's own."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: each step, then how it ended."),
    ] = False,
) -> None:
    """Install speech-to-speech here, start it, and make it the voice once it answers.

    What `keryx setup` does for a voice on this machine, through the same steps. Its words
    come from the voice LLM (`keryx voice llm`), which must answer first. On the first
    set-up, with VOICE_LLM never chosen, Keryx chooses it: a small model of the voice's own,
    brought up first, when it fits beside the agent's model, else the local agent's.
    """
    if not as_json:
        _owner_only(CHOOSE_THE_VOICE)
        try:
            code = _voice_here(_configure_readonly(), name, voice.TextSteps(typer.echo))
        except KeyboardInterrupt:
            typer.echo("cancelled: the next `keryx voice use here` picks up where this stopped",
                       err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = voice.JsonSteps(typer.echo)

    def run() -> int:
        # Refused before any install, and as an event: the app reads stdout alone.
        if refusal := _owner_refusal(CHOOSE_THE_VOICE):
            events.error(refusal)
            return 1
        return _voice_here(_configure_readonly(refuse=events.error), name, events)

    with pull.terminate_as_interrupt():
        code = events.guard(run)
    raise typer.Exit(code)


def _voice_here(settings: Settings, name: str | None, report: voice.Report) -> int:
    if _foreign_servers(settings, report):
        return voice.REFUSED
    if name is not None and (problem := voice.voice_name_problem(name)):
        report.error(problem, detail=voice.VOICE_NAMES_HINT)
        return voice.REFUSED
    store = ConfigStore()
    also: dict[str, Any] = {}
    if voice_llm.first_set_up(settings, store.source_of("VOICE_LLM", settings)):
        # The owner's rule for a new install: the voice LLM chosen once, and brought up first.
        machine = voice_llm.machine()
        pick = voice_llm.first_choice(settings, machine.hardware())
        if pick is None:
            also = {"VOICE_LLM": "shared"}
        else:
            code = voice_llm.bring_up(settings, pick.key, machine,
                                      voice_llm.Under(report, voice_llm.VOICE_STEP),
                                      save=_saver(store, settings, report), root=repo_root())
            if code != voice.OK:
                return code
            settings = _configure_readonly()
    return voice.here(settings, voice.machine(), report, save=_saver(store, settings, report),
                      root=repo_root(), also_save=also,
                      choose_voice=lambda: name or voice.current_voice(settings, store))


#: What `voice llm` is, refused to the service: `VOICE_LLM` is protected.
CHOOSE_THE_VOICE_LLM = "choose the voice LLM"


def _voice_llm_stream(
    as_json: bool, first: str, run: Callable[[Settings, voice_llm.Report], int],
    again: str,
) -> None:
    """A `voice llm` set-up told to a terminal, or as JSON lines whose first step is `first`;
    the owner's alone, refused before anything (as an event, with `--json`)."""
    def unless_foreign(settings: Settings, report: voice_llm.Report) -> int:
        return voice.REFUSED if _foreign_servers(settings, report) else run(settings, report)

    if not as_json:
        _owner_only(CHOOSE_THE_VOICE_LLM)
        try:
            code = unless_foreign(_configure_readonly(), voice_llm.TextSteps(typer.echo))
        except KeyboardInterrupt:
            typer.echo(f"cancelled: {again}", err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = voice_llm.JsonSteps(typer.echo, first=first)

    def guarded() -> int:
        if refusal := _owner_refusal(CHOOSE_THE_VOICE_LLM):
            events.error(refusal)
            return 1
        return unless_foreign(_configure_readonly(refuse=events.error), events)

    with pull.terminate_as_interrupt():
        code = events.guard(guarded)
    raise typer.Exit(code)


VOICE_LLM_JSON_HELP = "Print it as JSON lines: each step, then how it ended."


@voice_llm_app.command("shared")
def voice_llm_shared(
    as_json: Annotated[bool, typer.Option("--json", help=VOICE_LLM_JSON_HELP)] = False,
) -> None:
    """Take the voice's words from the local agent's model, as before VOICE_LLM was a
    setting; the voice LLM's own server is removed, since nothing uses it then."""

    def run(settings: Settings, report: voice_llm.Report) -> int:
        return voice_llm.shared(settings, voice_llm.machine(), report,
                                save=_saver(ConfigStore(), settings, report), root=repo_root())

    _voice_llm_stream(as_json, "configure", run, "`keryx voice llm shared` again finishes it")


@voice_llm_app.command("here")
def voice_llm_here(
    model: Annotated[str, typer.Argument(
        help="A name from `keryx models list` (downloaded first when it is not), or a GGUF "
             "file's path.")],
    as_json: Annotated[bool, typer.Option("--json", help=VOICE_LLM_JSON_HELP)] = False,
) -> None:
    """Give the voice a model of its own, on a llama.cpp server of its own here.

    The model is downloaded when it is not on disk and llama.cpp installed when it is
    missing; then the server's unit, the wait for the model to load and a check that it
    serves it. VOICE_LLM is saved only then, so the voice keeps its words until the new ones
    answer, and a server that does not come up has its unit removed.
    """

    def run(settings: Settings, report: voice_llm.Report) -> int:
        return voice_llm.here(settings, model, voice_llm.machine(), report,
                              save=_saver(ConfigStore(), settings, report), root=repo_root())

    _voice_llm_stream(as_json, voice_llm.STEPS[0], run,
                      "the next `keryx voice llm here` picks up where this stopped")


@voice_llm_app.command("remote")
def voice_llm_remote(
    url: Annotated[str, typer.Argument(
        help="Its address: http://gpu-box:11434, https://api.openai.com/v1, or its …/v1.")],
    model: Annotated[str, typer.Option("--model", help="The server's own name for the "
                                                       "model.")],
    key_stdin: Annotated[
        bool, typer.Option("--key-stdin", help="Read its key from standard input.")
    ] = False,
    clear_key: Annotated[
        bool, typer.Option("--clear-key", help="Forget the key it had, and ask without one.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help=VOICE_LLM_JSON_HELP)] = False,
) -> None:
    """Take the voice's words from a server elsewhere — a box of yours, or a cloud's
    Responses API — once it lists the model.

    The key it has is kept, and asked with, unless --key-stdin replaces it or --clear-key
    forgets it; it is never on the command line. Without one, OPENAI_API_KEY is sent to
    OpenAI's own address and to no other.
    """

    def run(settings: Settings, report: voice_llm.Report) -> int:
        if key_stdin and clear_key:
            report.error("Give the key on standard input, or forget it: not both.",
                         detail="--key-stdin replaces the key and --clear-key forgets it")
            return voice.REFUSED
        key = settings.voice_llm_api_key
        if key_stdin:
            key = _read_secret_input("VOICE_LLM_API_KEY")
            if not key:
                report.error("There was no key on standard input.",
                             detail="--clear-key forgets the one it has")
                return voice.REFUSED
        elif clear_key:
            key = None
        return voice_llm.remote(settings, url, model, voice_llm.machine(), report, key=key,
                                new_key=key_stdin or clear_key,
                                save=_saver(ConfigStore(), settings, report), root=repo_root())

    _voice_llm_stream(as_json, "check", run, "`keryx voice llm remote` again finishes it")


#: What `voice download` is, refused to the service: it installs, and fills the disk.
DOWNLOAD_THE_VOICE = "download the voice's speech models"


@voice_app.command("download")
def voice_download(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print it as JSON lines: each step, the bytes, then how it "
                     "ended."),
    ] = False,
) -> None:
    """Download the voice server's speech models into the Hugging Face cache, ahead of its
    first start.

    The ones VOICE_SERVER_STT, VOICE_SERVER_TTS and VOICE_SERVER_VOICE need, fetched by
    speech-to-speech's own Python; speech-to-speech is installed first when it is missing.
    """
    if not as_json:
        _owner_only(DOWNLOAD_THE_VOICE)
        try:
            code = voice.download(_configure_readonly(), voice.machine(),
                                  voice.DownloadTextSteps(typer.echo))
        except KeyboardInterrupt:
            typer.echo("cancelled: `keryx voice download` again picks up where this stopped",
                       err=True)
            code = pull.CANCELLED
        raise typer.Exit(code)
    events = voice.DownloadJsonSteps(typer.echo)

    def run() -> int:
        # Refused before any install, and as an event: the app reads stdout alone.
        if refusal := _owner_refusal(DOWNLOAD_THE_VOICE):
            events.error(refusal)
            return 1
        return voice.download(_configure_readonly(refuse=events.error), voice.machine(), events)

    with pull.terminate_as_interrupt():
        code = events.guard(run)
    raise typer.Exit(code)
