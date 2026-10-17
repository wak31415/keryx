"""`keryx memory`: what Keryx remembers between calls, and seeding it."""

import json
from typing import Annotated

import typer

from keryx.cli.apps import memory_app
from keryx.cli.common import _configure_readonly, _owner_only
from keryx.continuity.memory import memory_document, memory_path, read_memory
from keryx.setup import profile

#: `keryx memory` prints the whole document, not the slice a prompt gets, so this is only
#: a backstop against a memory that has run away.
MAX_MEMORY_PRINT_CHARS = 100_000


@memory_app.callback(invoke_without_command=True)
def memory(
    ctx: typer.Context,
    path_only: Annotated[
        bool, typer.Option("--path", help="Print where the memory lives and nothing else.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """Print what Keryx remembers between calls.

    The file a subagent rewrites after every call and every session reads back at the top
    of its prompt. It is plain markdown and safe to edit by hand — the next update merges
    around whatever is there.
    """
    if ctx.invoked_subcommand is not None:
        return
    if path_only and as_json:
        typer.echo("pick one of --path and --json", err=True)
        raise typer.Exit(code=2)
    settings = _configure_readonly()
    path = memory_path(settings.data_dir)
    if path_only:
        typer.echo(str(path))
        return
    if as_json:
        document = memory_document(settings.data_dir, max_chars=MAX_MEMORY_PRINT_CHARS)
        typer.echo(json.dumps(document, indent=2))
        return

    text = read_memory(settings.data_dir, max_chars=MAX_MEMORY_PRINT_CHARS)
    if not text:
        typer.echo(f"nothing remembered yet ({path} does not exist)")
        return
    typer.echo(f"# {path}\n")
    typer.echo(text)


@memory_app.command("seed")
def memory_seed(
    file: Annotated[
        str, typer.Option("--file", help="The facts, one per line; - is stdin.")
    ],
    force: Annotated[
        bool, typer.Option("--force", help="Replace a memory that already has something in it.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print what every call will carry, as JSON.")
    ] = False,
) -> None:
    """Write a first memory from standing facts, before any call has.

    For an agent: `--file - --json` takes the facts on stdin and prints one JSON document.
    Exit 0 means the memory was written, or nothing was given to write; exit 1 means a
    memory was wanted and not written (one is there without --force, or it is longer than a
    call reads); exit 2 means the command line itself is wrong.
    """
    _owner_only("seed the memory")
    settings = _configure_readonly()
    try:
        facts = profile.read_facts(file)
    except OSError as exc:
        typer.echo(f"could not read {file}: {exc}", err=True)
        raise typer.Exit(2) from None
    say = (lambda _line: None) if as_json else typer.echo
    status = profile.seed(settings, facts, force=force, echo=say)
    if as_json:
        typer.echo(json.dumps(profile.setup_summary(settings, status=status), indent=2))
    else:
        for line in profile.setup_report(settings):
            typer.echo(line)
    if code := profile.exit_code(status):
        raise typer.Exit(code)
