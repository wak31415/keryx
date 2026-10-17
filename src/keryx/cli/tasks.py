"""`keryx tasks`: the tasks handed to subagents, and what they spent."""

import asyncio
import contextlib
import dataclasses
import json
import textwrap
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from keryx.app import TASK_DB_NAME
from keryx.cli.apps import tasks_app
from keryx.cli.common import MAX_DESCRIPTION_CHARS, _configure_readonly, _shorten
from keryx.config import Settings
from keryx.tasks.export import read_log, task_json, told
from keryx.tasks.models import ProjectUsage, Task, TaskStatus, task_log_path
from keryx.tasks.store import TaskStore

TIME_FORMAT = "%Y-%m-%d %H:%M"
#: How much of a report `tasks show` prints before it starts truncating.
MAX_REPORT_CHARS = 4000


class StatusFilter(StrEnum):
    """`--status` values: every `TaskStatus`, plus `all` for no filter."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ALL = "all"


def _local_time(value: datetime | None) -> str:
    """A UTC timestamp as local wall-clock time, the way a human reads it."""
    return value.astimezone().strftime(TIME_FORMAT) if value is not None else ""


@contextlib.asynccontextmanager
async def _open_store(settings: Settings) -> AsyncIterator[TaskStore]:
    """The task store for a read-only CLI command: `tasks` never starts a manager or a
    subagent, it just reads straight from the store and always closes it after."""
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    try:
        yield store
    finally:
        await store.close()


async def _read_tasks(
    settings: Settings, status: TaskStatus | None, limit: int, include_internal: bool
) -> list[Task]:
    async with _open_store(settings) as store:
        return await store.list(
            status=status, limit=limit, include_internal=include_internal
        )


async def _read_task(settings: Settings, task_id: int) -> Task | None:
    async with _open_store(settings) as store:
        return await store.get(task_id)


@tasks_app.command("list")
def tasks_list(
    status: Annotated[
        StatusFilter, typer.Option("--status", help="Only show tasks in this status.")
    ] = StatusFilter.ALL,
    limit: Annotated[int, typer.Option("--limit", help="How many tasks to show.")] = 20,
    include_internal: Annotated[
        bool,
        typer.Option(
            "--internal",
            help="Also show Keryx's own housekeeping (the per-call memory updates).",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """List recent tasks, newest first."""
    settings = _configure_readonly()
    wanted = None if status is StatusFilter.ALL else TaskStatus(status.value)
    tasks = asyncio.run(_read_tasks(settings, wanted, limit, include_internal))
    if as_json:
        typer.echo(json.dumps({"tasks": [task_json(task) for task in tasks]}, indent=2))
        return
    if not tasks:
        typer.echo("no tasks" if wanted is None else f"no tasks with status {wanted}")
        return

    typer.echo(
        f"{'ID':>4}  {'STATUS':<9}  {'CREATED':<16}  {'AGENT':<6}  {'TOLD':<5}  DESCRIPTION"
    )
    for task in tasks:
        typer.echo(
            f"{task.id:>4}  {task.status:<9}  "
            f"{_local_time(task.created_at):<16}  "
            f"{task.agent:<6}  "
            f"{_reported_flag(task):<5}  "
            f"{_shorten(task.description, MAX_DESCRIPTION_CHARS)}"
        )


def _reported_flag(task: Task) -> str:
    """Whether Keryx has told them about this one: only meaningful once it has finished.

    Goes through `told()` so the table and the JSON export can never disagree.
    """
    if task.internal:
        return "-"
    flag = told(task)
    if flag is None:
        return ""
    return "yes" if flag else "NO"


@tasks_app.command("show")
def tasks_show(
    task_id: Annotated[int, typer.Argument(help="The task id to show.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """Show everything stored about one task, including its report."""
    settings = _configure_readonly()
    task = asyncio.run(_read_task(settings, task_id))
    if task is None:
        typer.echo(f"no task {task_id}", err=True)
        raise typer.Exit(1)

    if as_json:
        report, truncated = (
            _read_report(Path(task.report_path)) if task.report_path else (None, False)
        )
        log_entries, log_truncated = read_log(task_log_path(settings.data_dir, task.id))
        typer.echo(
            json.dumps(
                {
                    "task": task_json(task),
                    "report": report,
                    "report_truncated": truncated,
                    "log": log_entries,
                    "log_truncated": log_truncated,
                    "resumable": bool(task.claude_session_id),
                },
                indent=2,
            )
        )
        return

    width = max(len(field.name) for field in dataclasses.fields(task))
    for field in dataclasses.fields(task):
        value = getattr(task, field.name)
        printable = _local_time(value) if isinstance(value, datetime) else value
        typer.echo(f"{field.name:<{width}}  {'' if printable is None else printable}")

    if task.report_path:
        _echo_report(Path(task.report_path))


#: What `tasks usage` calls the two groups that carry no project name of their own.
NO_PROJECT_LABEL = "(no project)"
HOUSEKEEPING_LABEL = "(housekeeping)"
#: Printed under the table, because every column in it is easy to over-read.
USAGE_FOOTNOTE = (
    "Attributed by the project each task was dispatched with; \"(no project)\" is work "
    "started in the projects folder, which may still have touched one. COST is what the "
    "agent reported: Claude's list-price estimate, which a subscription login is not "
    "billed per token, and nothing for Codex. PRICED counts the tasks that reported one. "
    "Voice calls are not tasks and are not here; check_billing has the provider's total."
)


async def _read_usage(settings: Settings, since: datetime | None) -> list[ProjectUsage]:
    async with _open_store(settings) as store:
        return await store.usage_by_project(since=since)


def _usage_label(usage: ProjectUsage) -> str:
    if usage.internal:
        return HOUSEKEEPING_LABEL
    return usage.project or NO_PROJECT_LABEL


def _usage_line(label: str, usage: ProjectUsage) -> str:
    cost = "-" if usage.cost_usd is None else f"${usage.cost_usd:,.2f}"
    tokens = usage.measured > 0
    return (
        f"{_shorten(label, 28):<28}  {usage.tasks:>5}  {f'{usage.priced}/{usage.tasks}':>7}  "
        f"{f'{usage.input_tokens:,}' if tokens else '-':>13}  "
        f"{f'{usage.output_tokens:,}' if tokens else '-':>11}  {cost:>9}"
    )


@tasks_app.command("usage")
def tasks_usage(
    days: Annotated[
        int,
        typer.Option("--days", min=0, help="Tasks created in the last N days; 0 for all of them."),
    ] = 30,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """What the tasks spent, per project: tokens and the agents' own cost estimates."""
    settings = _configure_readonly()
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    groups = asyncio.run(_read_usage(settings, since))
    if as_json:
        typer.echo(json.dumps({
            "since": since.isoformat() if since else None,
            "projects": [
                {"label": _usage_label(usage), **dataclasses.asdict(usage)} for usage in groups
            ],
        }, indent=2))
        return
    window = f"in the last {days} day{'s' if days != 1 else ''}" if days else "ever"
    if not groups:
        typer.echo(f"no tasks created {window}")
        return

    typer.echo(f"Tasks created {window}, by project:\n")
    typer.echo(
        f"{'PROJECT':<28}  {'TASKS':>5}  {'PRICED':>7}  {'INPUT TOK':>13}  "
        f"{'OUTPUT TOK':>11}  {'COST':>9}"
    )
    for usage in groups:
        typer.echo(_usage_line(_usage_label(usage), usage))
    typer.echo(_usage_line("total", sum(groups[1:], groups[0])))
    typer.echo("")
    typer.echo(textwrap.fill(USAGE_FOOTNOTE, width=88))


def _read_report(path: Path) -> tuple[str | None, bool]:
    """`path`'s contents sliced to `MAX_REPORT_CHARS`, and whether that cut anything off.

    `(None, False)` when the report cannot be read (most often: it is gone) — the caller
    decides how to say that. Shared by `_echo_report` and `tasks show --json`, so the two
    cannot disagree on where a report gets cut.
    """
    try:
        body = path.read_text(errors="replace")
    except OSError:
        return None, False
    return body[:MAX_REPORT_CHARS], len(body) > MAX_REPORT_CHARS


def _echo_report(path: Path) -> None:
    """Print the task's written report, truncated; a report that is gone is simply skipped."""
    body, truncated = _read_report(path)
    if body is None:
        typer.echo("(report unreadable)")
        return
    typer.echo("\n--- report ---")
    typer.echo(body.rstrip())
    if truncated:
        typer.echo(f"… (truncated at {MAX_REPORT_CHARS} characters; full report: {path})")
