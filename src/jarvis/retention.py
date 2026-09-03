"""Deleting what is no longer worth keeping (spec §3.3).

Everything Jarvis writes down accumulated forever. `jarvis.log` rotates; nothing else did.
`data_dir/calls/<session_id>.log` is one file per call holding every word of it, `tasks.db`
had no `DELETE` anywhere in the codebase, and `memory.md` was bounded on *read* and by the
memory subagent's own discipline on write, which is not a bound.

Retention is **off by default**, and that is deliberate rather than an oversight: deleting
a man's own transcripts because a default said so is not a decision this code gets to make.
`TRANSCRIPT_RETENTION_DAYS` and `TASK_RETENTION_DAYS` are `0` — keep everything — until
somebody sets them.

One rule survives everything here: **a task the caller has not been told about is never
deleted.** `Task.reported_at` is the only record that Jarvis *said* a result out loud, and
a result deleted before it was reported is one he will never hear. So the prune skips
exactly what `TaskStore.list_unreported` would return, however old it is. Internal
housekeeping tasks and cancelled ones are not owed to anybody and go on schedule.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis.briefing import memory_path, trim_memory
from jarvis.config import Settings
from jarvis.tasks.store import TaskStore

log = logging.getLogger("jarvis.retention")

#: Transcripts live here, one file per session (`jarvis.transcripts`).
CALLS_DIR = "calls"
#: A task's progress log and its written report.
TASKS_DIR = "tasks"


@dataclass(frozen=True)
class PruneReport:
    """What a prune actually removed, for a log line and for `jarvis forget` to print."""

    transcripts: int = 0
    tasks: int = 0
    kept_unreported: int = 0

    def __bool__(self) -> bool:
        return bool(self.transcripts or self.tasks)

    def describe(self) -> str:
        parts = []
        if self.transcripts:
            parts.append(f"{self.transcripts} transcript{'s' if self.transcripts != 1 else ''}")
        if self.tasks:
            parts.append(f"{self.tasks} task{'s' if self.tasks != 1 else ''}")
        summary = " and ".join(parts) if parts else "nothing"
        if self.kept_unreported:
            summary += (
                f" (kept {self.kept_unreported} he has not been told about)"
            )
        return summary


def cutoff_for(days: int, *, now: datetime | None = None) -> datetime | None:
    """The moment before which things may be deleted, or `None` when retention is off.

    `days <= 0` is off, not "delete everything": zero is the default, and a default that
    deletes is a default nobody would forgive.
    """
    if days <= 0:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def prune_transcripts(data_dir: Path, cutoff: datetime | None, *, keep: Iterable[str] = ()) -> int:
    """Delete call transcripts last written before `cutoff`. Returns how many went.

    `keep` names session ids to spare — the live ones, since a call in progress is being
    appended to and must not have its record pulled out from under it.
    """
    if cutoff is None:
        return 0
    spared = set(keep)
    removed = 0
    for path in sorted((data_dir / CALLS_DIR).glob("*.log")):
        if path.stem in spared:
            continue
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff:
                continue
            path.unlink()
        except OSError:
            log.warning("could not prune the transcript %s", path.name)
            continue
        removed += 1
    return removed


def remove_task_files(data_dir: Path, task_ids: Iterable[int]) -> None:
    """Delete the log and the written report belonging to task rows that have gone."""
    for task_id in task_ids:
        for suffix in (".log", ".md"):
            try:
                (data_dir / TASKS_DIR / f"{task_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove the %s file of task %s", suffix, task_id)


async def prune(
    settings: Settings,
    store: TaskStore,
    *,
    now: datetime | None = None,
    keep_sessions: Iterable[str] = (),
) -> PruneReport:
    """One pass of both retention windows. Never raises; a failure is a log line.

    Called once at the top of `jarvis serve` and by `jarvis forget`. Both windows are
    independent — keeping transcripts for a week and tasks for a year is a reasonable thing
    to want, and either at `0` simply does nothing.
    """
    return await prune_with(
        settings,
        store,
        transcripts=cutoff_for(settings.transcript_retention_days, now=now),
        tasks=cutoff_for(settings.task_retention_days, now=now),
        keep_sessions=keep_sessions,
    )


async def prune_with(
    settings: Settings,
    store: TaskStore,
    *,
    transcripts: datetime | None,
    tasks: datetime | None,
    keep_sessions: Iterable[str] = (),
) -> PruneReport:
    """`prune`, with the two cutoffs given outright — what `jarvis forget` needs."""
    removed_transcripts = prune_transcripts(settings.data_dir, transcripts, keep=keep_sessions)

    removed_tasks: list[int] = []
    kept = 0
    if tasks is not None:
        removed_tasks = await store.delete_finished_before(tasks)
        remove_task_files(settings.data_dir, removed_tasks)
        kept = await store.count_unreported()

    # `memory.md` is written by a subagent rather than by us, so this is the other place it
    # is bounded — see `MemoryWriter`, which trims it as soon as that subagent finishes.
    trim_memory(settings.data_dir)

    report = PruneReport(
        transcripts=removed_transcripts, tasks=len(removed_tasks), kept_unreported=kept
    )
    if report:
        log.info("retention removed %s", report.describe())
    return report


__all__ = [
    "PruneReport",
    "cutoff_for",
    "memory_path",
    "prune",
    "prune_transcripts",
    "prune_with",
    "remove_task_files",
]
