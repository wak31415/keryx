"""`keryx forget`: deleting transcripts and finished task rows."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated

import typer

from keryx.app import TASK_DB_NAME
from keryx.cli.apps import app
from keryx.cli.common import _configure_readonly
from keryx.continuity.retention import cutoff_for, prune_with
from keryx.tasks.store import TaskStore


@app.command()
def forget(
    older_than: Annotated[
        int,
        typer.Option(
            "--older-than",
            "-d",
            min=0,
            help="Delete transcripts and finished tasks older than this many days. 0 = all.",
        ),
    ] = 0,
    transcripts_only: Annotated[
        bool, typer.Option("--transcripts-only", help="Leave the task rows alone.")
    ] = False,
    tasks_only: Annotated[
        bool, typer.Option("--tasks-only", help="Leave the call transcripts alone.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
) -> None:
    """Delete call transcripts and finished task rows, now.

    The same rules as the automatic retention pass, run on demand and regardless of whether
    `TRANSCRIPT_RETENTION_DAYS` / `TASK_RETENTION_DAYS` are set. In particular the one that
    matters: a finished task Keryx has **not told you about yet** is never deleted,
    however old it is, because `reported_at` is the only record that you heard the result.
    Those are reported as kept rather than removed silently.

    This is not undoable, and `--older-than 0` really does mean everything.
    """
    if transcripts_only and tasks_only:
        typer.echo("pick one of --transcripts-only and --tasks-only")
        raise typer.Exit(code=2)

    settings = _configure_readonly()
    # `--older-than 0` is "everything", which is the opposite of what 0 means in the
    # settings ("keep everything"); an explicit command is an explicit decision.
    cutoff = datetime.now(UTC) if older_than == 0 else cutoff_for(older_than)
    window = "everything" if older_than == 0 else f"older than {older_than} days"
    what = (
        "transcripts"
        if transcripts_only
        else "finished tasks"
        if tasks_only
        else "transcripts and finished tasks"
    )
    if not yes:
        typer.echo(f"about to delete {what} {window} from {settings.data_dir}.")
        typer.confirm("this cannot be undone. continue?", abort=True)

    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        report = asyncio.run(
            prune_with(
                settings,
                store,
                transcripts=None if tasks_only else cutoff,
                tasks=None if transcripts_only else cutoff,
            )
        )
    finally:
        asyncio.run(store.close())
    typer.echo(f"removed {report.describe()}")
