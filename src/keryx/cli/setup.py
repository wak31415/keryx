"""`keryx setup`: the wizard, and the plan the Mac app's assistant follows."""

import json
import sys
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from keryx.cli.apps import setup_app
from keryx.cli.common import (
    _config_summary,
    _configure_readonly,
    _doctor_checks,
    _interactive,
    _load_settings_optional,
    _load_settings_reporting,
    _owner_only,
    _owner_refusal,
)
from keryx.config.permissions import OWNER
from keryx.config.settings import ConfigFileError
from keryx.config.store import ConfigError, ConfigStore
from keryx.localmodels import pull
from keryx.setup import profile, project_stream
from keryx.setup.context import SetupContext
from keryx.setup.plan import StepRefused, build_plan, failed_plan, format_plan, mark_step
from keryx.setup.ui import Aborted
from keryx.setup.wizard import agent_instructions, run_wizard


@setup_app.callback(invoke_without_command=True)
def setup(
    ctx: typer.Context,
    review_all: Annotated[
        bool, typer.Option("--all", help="Walk every section, and ask again about what is set.")
    ] = False,
    instructions: Annotated[
        bool,
        typer.Option(
            "--agent-instructions",
            help="Print how a coding agent sets Keryx up from the command line, and exit.",
        ),
    ] = False,
) -> None:
    """Set Keryx up: a guided walk through only what is still missing.

    Everything it saves goes to KERYX_HOME as you go, so stopping part way loses nothing.
    A coding agent setting Keryx up uses the commands instead: --agent-instructions.
    """
    if ctx.invoked_subcommand is not None:
        return
    settings = _configure_readonly(migrated=False)  # the wizard says so, in its own words
    if instructions:
        typer.echo(agent_instructions(settings, ConfigStore()))
        return
    _owner_only("run setup")
    if not _interactive():
        typer.echo(
            "keryx setup asks questions, so it needs a terminal. A coding agent: "
            "keryx setup --agent-instructions",
            err=True,
        )
        raise typer.Exit(2)
    from keryx.setup.ui import RichPrompter

    ctx = SetupContext(ui=RichPrompter(), store=ConfigStore(), load=_load_settings_optional)
    try:
        code = run_wizard(ctx, review_all=review_all)
    except Aborted:
        typer.echo("\nsetup stopped. What was saved stays saved; `keryx setup` picks up here.")
        raise typer.Exit(130) from None
    if code:
        raise typer.Exit(code)


@setup_app.command("plan")
def setup_plan(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The steps to a first working call, and which are done: a report, so it exits 0.

    What the Mac app's Setup Assistant walks. It asks what `keryx doctor` asks, Twilio
    included; a migration still to run is its first step, and settings that will not load
    fail every step with the reason, as `keryx status` reports them — never a refusal.
    """
    try:
        settings, problems = _load_settings_reporting()
    except ConfigFileError as error:
        plan = failed_plan(str(error))
    except ValidationError as error:
        plan = failed_plan(_config_summary(error))
    else:
        store = ConfigStore()
        plan = build_plan(settings, store, _doctor_checks(settings, problems, store))
    if as_json:
        typer.echo(json.dumps(plan, indent=2))
        return
    for line in format_plan(plan):
        typer.echo(line)


@setup_app.command("mark")
def setup_mark(
    step: Annotated[str, typer.Argument(help="The step, by its id in `keryx setup plan`.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Record a step as walked: an optional one skipped, the PIN left to the first call, or
    the phone or the service left for later (still not done, so not ready: "deferred")."""
    _owner_only("mark a setup step")
    try:
        settings = _load_settings_optional()
    except (ConfigFileError, ValidationError) as error:
        summary = _config_summary(error) if isinstance(error, ValidationError) else str(error)
        typer.echo(f"nothing is marked while the settings do not load: {summary}", err=True)
        raise typer.Exit(1) from None
    if (refusal := settings.storage_refusal()) is not None:
        # What it saves would land where `keryx migrate` is about to move the old files.
        typer.echo(f"nothing is marked until the old files have moved: {refusal}", err=True)
        raise typer.Exit(2)
    store = ConfigStore()
    try:
        marked = mark_step(store, step)
    except StepRefused as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    except (OSError, ValueError) as error:
        typer.echo(f"nothing is marked: {store.config_path} cannot be read ({error}); "
                   "fix it by hand", err=True)
        raise typer.Exit(1) from None
    said = "left for later" if marked["deferred"] else "walked"
    typer.echo(json.dumps(marked) if as_json else f"{marked['id']}: {said}")


#: What the Mac app's answers come as: one JSON object on stdin, never typed at a terminal.
ANSWERS_ON_STDIN = "the answers are one JSON object on stdin, piped in, not typed here"


def _read_answers() -> dict[str, Any]:
    """The one JSON object on stdin, or exit 2 saying what is wrong with it."""
    if sys.stdin.isatty():
        typer.echo(ANSWERS_ON_STDIN, err=True)
        raise typer.Exit(2)
    try:
        answers = json.loads(sys.stdin.read() or "null")
    except json.JSONDecodeError as error:
        typer.echo(f"{ANSWERS_ON_STDIN}: {error}", err=True)
        raise typer.Exit(2) from None
    if not isinstance(answers, dict):
        typer.echo(ANSWERS_ON_STDIN, err=True)
        raise typer.Exit(2)
    return answers


@setup_app.command("profile")
def setup_profile(
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the answers read on stdin, as the wizard does.")
    ] = False,
    preview: Annotated[
        bool, typer.Option("--preview", help="Show what the answers on stdin would write.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The "About you" questions `keryx setup` asks, as data, and their answers applied.

    With --apply (or --preview) the answers are one JSON object on stdin, `{id: value}`.
    Exit 0 when they were applied (written, kept, or nothing to write); 1 when one is not an
    answer, and nothing was written; 2 when stdin is not one JSON object.
    """
    if apply and preview:
        typer.echo("pick one of --apply and --preview", err=True)
        raise typer.Exit(2)
    if apply:
        _owner_only("write the memory")
    settings = _configure_readonly()
    if not (apply or preview):
        document = profile.questions(settings)
        if as_json:
            typer.echo(json.dumps(document, indent=2))
            return
        for question in document["questions"]:
            typer.echo(f"{question['id']}: {question['prompt']}")
        return
    outcome = profile.apply_answers(settings, _read_answers(), write=apply)
    # The wizard walks the section whenever it runs to its end, a memory too long for a call
    # included; only answers that are not answers leave it unwalked.
    invalid = outcome.pop("invalid")
    walked = apply and not invalid
    if walked:
        ConfigStore().mark_walked(profile.SECTION)
    if as_json:
        typer.echo(json.dumps({**outcome, "walked": walked}, indent=2))
    else:
        for problem in outcome["problems"]:
            typer.echo(f"{problem['id']}: {problem['message']}", err=True)
        typer.echo(outcome["text"] or outcome["status"])
    if apply and outcome["status"] == profile.REFUSED:
        raise typer.Exit(1)


#: What `setup project-context` is, refused to the service: it dispatches, and writes.
PROJECT_CONTEXT = "set up project context"


@setup_app.command("project-context")
def setup_project_context(
    describe: Annotated[
        bool, typer.Option("--describe", help="What the step says and asks, and what is kept.")
    ] = False,
    explore: Annotated[
        bool, typer.Option("--explore", help="Let the agent find the projects, from home.")
    ] = False,
    folder: Annotated[
        list[str] | None, typer.Option("--folder", help="A folder to look in (repeatable).")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Keep what stdin says was accepted of a draft.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON: a document, or with "
                                          "--explore/--folder the draft's JSON lines.")] = False,
) -> None:
    """The wizard's project-context step for the Mac app, in three calls.

    --describe is the question; --explore or --folder PATH runs the agent's draft (JSON lines,
    --json only); --apply keeps what the owner accepted of it, one JSON object on stdin:
    {"folders": […], "projects": [{"name", "path", "summary"}], "facts": […]}.
    """
    if sum((describe, apply, explore or bool(folder))) > 1:
        typer.echo("pick one of --describe, --apply and --explore/--folder", err=True)
        raise typer.Exit(2)
    if describe:
        document = project_stream.describe(_configure_readonly())
        if as_json:
            typer.echo(json.dumps(document, indent=2))
        else:
            offered = ", ".join(choice["value"] for choice in document["choices"])
            typer.echo(f"{document['question']} ({offered})")
        return
    if apply:
        _setup_project_context_apply(as_json)
        return
    if not as_json:
        typer.echo("the draft streams for the Mac app (--json); at a terminal, `keryx setup` "
                   "walks it", err=True)
        raise typer.Exit(2)
    raise typer.Exit(_project_context_stream(explore, folder or []))


def _setup_project_context_apply(as_json: bool) -> None:
    """`setup project-context --apply`: exit 0 kept, 1 a problem, 2 stdin not one object."""
    _owner_only(PROJECT_CONTEXT)
    settings = _configure_readonly()
    kept = _read_answers()
    store = ConfigStore()

    def save(values: dict[str, Any]) -> bool:
        try:
            store.set(values, actor=OWNER, settings=settings)
        except ConfigError:
            return False
        return True

    outcome = project_stream.apply(settings, kept, save=save)
    walked = outcome.pop("applied")
    if walked:
        store.mark_walked(project_stream.SECTION)
    if as_json:
        typer.echo(json.dumps({**outcome, "walked": walked}, indent=2))
    else:
        for problem in outcome["problems"]:
            typer.echo(f"{problem['id']}: {problem['message']}", err=True)
        typer.echo(f"{len(outcome['kept'])} project summaries kept, "
                   f"{outcome['facts_added']} facts added")
    if outcome["problems"]:
        raise typer.Exit(1)


def _project_context_stream(explore: bool, named: list[str]) -> int:
    """`setup project-context --explore|--folder … --json`: one ending on every path.
    Exit 0 the draft, 1 an error (the service's refusal, the agent, its answer), 2 where to
    look not said, or not folders; 130 cancelled."""
    events = project_stream.ProjectSteps(typer.echo)

    def run() -> int:
        if refusal := _owner_refusal(PROJECT_CONTEXT):
            events.error(refusal)
            return 1
        folders, problem = project_stream.folders_for(explore, named)
        if folders is None:
            if problem:
                events.error(project_stream.NOT_FOLDERS, detail=problem)
            else:
                events.error(project_stream.WHERE)
            return 2
        settings = _configure_readonly(refuse=events.error)
        return project_stream.stream(settings, events, folders, exploring=explore)

    with pull.terminate_as_interrupt():
        return events.guard(run)
