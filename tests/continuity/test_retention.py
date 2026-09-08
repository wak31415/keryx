"""Tests for `jarvis.continuity.retention`: what gets deleted, and the one thing that never does.

The rule with a consequence behind it is that a finished task the caller has not been told
about survives any prune, however old. `Task.reported_at` is the only record that Jarvis
said a result out loud, so deleting an unreported row is deleting a result he will never
hear — and unlike hearing something twice, that is not recoverable.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from jarvis.config import Settings
from jarvis.continuity.memory import MAX_MEMORY_FILE_CHARS, memory_path, trim_memory
from jarvis.continuity.retention import (
    PruneReport,
    cutoff_for,
    prune,
    prune_transcripts,
    prune_with,
)
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"openai_api_key": "test", "data_dir": tmp_path / "jarvis"}
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    settings.ensure_dirs()
    return settings


def write_call(settings, session_id: str, *, age_days: float) -> None:
    path = settings.data_dir / "calls" / f"{session_id}.log"
    path.write_text(f"[12:00:00] user: hello from {session_id}\n", encoding="utf-8")
    when = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (when, when))


async def store_for(settings) -> TaskStore:
    return TaskStore(settings.data_dir / "tasks.db")


async def add_task(store, *, age_days: float, **overrides) -> Task:
    finished = NOW - timedelta(days=age_days)
    values = {
        "id": None,
        "kind": TaskKind.AGENT,
        "description": "some work",
        "status": TaskStatus.DONE,
        "summary": "done",
        "created_at": finished,
        "finished_at": finished,
    }
    values.update(overrides)
    return await store.create(Task(**values))


# --- retention is off by default -------------------------------------------


def test_zero_days_is_off_rather_than_delete_everything():
    assert cutoff_for(0) is None
    assert cutoff_for(-1) is None
    assert cutoff_for(1, now=NOW) == NOW - timedelta(days=1)


async def test_nothing_is_pruned_when_retention_is_off(tmp_path):
    settings = make_settings(tmp_path)
    write_call(settings, "ancient", age_days=4000)
    store = await store_for(settings)
    old = await add_task(store, age_days=4000, reported_at=NOW - timedelta(days=4000))

    report = await prune(settings, store, now=NOW)

    assert report == PruneReport()
    assert (settings.data_dir / "calls" / "ancient.log").exists()
    assert await store.get(old.id) is not None
    await store.close()


async def test_the_defaults_really_are_zero(tmp_path):
    settings = make_settings(tmp_path)

    assert (settings.transcript_retention_days, settings.task_retention_days) == (0, 0)


def test_a_negative_window_is_refused(tmp_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_api_key="t", data_dir=tmp_path, task_retention_days=-1)


# --- transcripts ------------------------------------------------------------


def test_only_transcripts_past_the_window_go(tmp_path):
    settings = make_settings(tmp_path, transcript_retention_days=30)
    write_call(settings, "old", age_days=31)
    write_call(settings, "fresh", age_days=29)

    removed = prune_transcripts(settings.data_dir, cutoff_for(30, now=NOW))

    assert removed == 1
    assert not (settings.data_dir / "calls" / "old.log").exists()
    assert (settings.data_dir / "calls" / "fresh.log").exists()


def test_a_live_session_keeps_its_transcript(tmp_path):
    """A call in progress is being appended to; its record must not go out from under it."""
    settings = make_settings(tmp_path)
    write_call(settings, "live", age_days=4000)

    removed = prune_transcripts(settings.data_dir, cutoff_for(1, now=NOW), keep=["live"])

    assert removed == 0
    assert (settings.data_dir / "calls" / "live.log").exists()


def test_a_missing_calls_directory_is_not_an_error(tmp_path):
    settings = Settings(_env_file=None, openai_api_key="t", data_dir=tmp_path / "never-made")

    assert prune_transcripts(settings.data_dir, cutoff_for(1, now=NOW)) == 0


# --- tasks ------------------------------------------------------------------


async def test_an_old_reported_task_and_its_files_go(tmp_path):
    settings = make_settings(tmp_path, task_retention_days=30)
    store = await store_for(settings)
    task = await add_task(store, age_days=40, reported_at=NOW - timedelta(days=40))
    log = settings.data_dir / "tasks" / f"{task.id}.log"
    report_file = settings.data_dir / "tasks" / f"{task.id}.md"
    log.write_text("progress")
    report_file.write_text("# report")

    report = await prune(settings, store, now=NOW)

    assert report.tasks == 1
    assert await store.get(task.id) is None
    assert not log.exists()
    assert not report_file.exists()
    await store.close()


async def test_an_unreported_task_survives_however_old_it_is(tmp_path):
    """The whole point. `reported_at` is the only record that he was told."""
    settings = make_settings(tmp_path, task_retention_days=1)
    store = await store_for(settings)
    task = await add_task(store, age_days=4000, reported_at=None)

    report = await prune(settings, store, now=NOW)

    assert report.tasks == 0
    assert report.kept_unreported == 1
    assert await store.get(task.id) is not None
    assert [kept.id for kept in await store.list_unreported()] == [task.id]
    await store.close()


async def test_an_unreported_internal_task_is_pruned(tmp_path):
    """Housekeeping Jarvis asked for itself is owed to nobody, so it never rode the digest."""
    settings = make_settings(tmp_path, task_retention_days=1)
    store = await store_for(settings)
    task = await add_task(store, age_days=40, internal=True, reported_at=None)

    await prune(settings, store, now=NOW)

    assert await store.get(task.id) is None
    await store.close()


async def test_a_cancelled_task_is_pruned_without_ever_being_reported(tmp_path):
    """He stopped the work; there was never a result to tell him about."""
    settings = make_settings(tmp_path, task_retention_days=1)
    store = await store_for(settings)
    task = await add_task(store, age_days=40, status=TaskStatus.CANCELLED, reported_at=None)

    await prune(settings, store, now=NOW)

    assert await store.get(task.id) is None
    await store.close()


async def test_a_running_task_is_never_pruned(tmp_path):
    settings = make_settings(tmp_path, task_retention_days=1)
    store = await store_for(settings)
    task = await add_task(store, age_days=40, status=TaskStatus.RUNNING, finished_at=None)

    await prune(settings, store, now=NOW)

    assert await store.get(task.id) is not None
    await store.close()


async def test_a_task_cancelled_before_it_ever_ran_is_dated_by_creation(tmp_path):
    """`finished_at` is NULL on those, and NULL < cutoff is never true in SQL."""
    settings = make_settings(tmp_path, task_retention_days=1)
    store = await store_for(settings)
    task = await add_task(
        store, age_days=40, status=TaskStatus.CANCELLED, finished_at=None, reported_at=None
    )

    await prune(settings, store, now=NOW)

    assert await store.get(task.id) is None
    await store.close()


async def test_a_recent_reported_task_stays(tmp_path):
    settings = make_settings(tmp_path, task_retention_days=30)
    store = await store_for(settings)
    task = await add_task(store, age_days=10, reported_at=NOW - timedelta(days=10))

    await prune(settings, store, now=NOW)

    assert await store.get(task.id) is not None
    await store.close()


# --- the two windows are independent ----------------------------------------


async def test_the_two_windows_do_not_touch_each_other(tmp_path):
    settings = make_settings(tmp_path, transcript_retention_days=1, task_retention_days=0)
    write_call(settings, "old", age_days=40)
    store = await store_for(settings)
    task = await add_task(store, age_days=40, reported_at=NOW - timedelta(days=40))

    report = await prune(settings, store, now=NOW)

    assert (report.transcripts, report.tasks) == (1, 0)
    assert await store.get(task.id) is not None
    await store.close()


async def test_prune_with_takes_the_cutoffs_outright(tmp_path):
    """What `jarvis forget` uses: delete now, whatever the configured windows say."""
    settings = make_settings(tmp_path)  # both windows off
    write_call(settings, "yesterday", age_days=1)
    store = await store_for(settings)
    task = await add_task(store, age_days=1, reported_at=NOW - timedelta(days=1))

    report = await prune_with(settings, store, transcripts=NOW, tasks=NOW)

    assert (report.transcripts, report.tasks) == (1, 1)
    assert await store.get(task.id) is None
    await store.close()


# --- memory.md --------------------------------------------------------------


def test_the_memory_is_bounded_on_disk(tmp_path):
    settings = make_settings(tmp_path)
    memory_path(settings.data_dir).write_text("x" * (MAX_MEMORY_FILE_CHARS * 2), encoding="utf-8")

    assert trim_memory(settings.data_dir) is True
    assert len(memory_path(settings.data_dir).read_text()) <= MAX_MEMORY_FILE_CHARS + 1


def test_a_memory_within_budget_is_left_exactly_alone(tmp_path):
    settings = make_settings(tmp_path)
    text = "## Standing facts\n\nHe drinks tea.\n"
    memory_path(settings.data_dir).write_text(text, encoding="utf-8")

    assert trim_memory(settings.data_dir) is False
    assert memory_path(settings.data_dir).read_text() == text


def test_trimming_keeps_the_beginning_where_the_standing_facts_are(tmp_path):
    settings = make_settings(tmp_path)
    head = "## Standing facts\n\nHe drinks tea.\n"
    memory_path(settings.data_dir).write_text(head + "y" * MAX_MEMORY_FILE_CHARS, encoding="utf-8")

    trim_memory(settings.data_dir)

    assert memory_path(settings.data_dir).read_text().startswith(head)


def test_a_missing_memory_is_not_an_error(tmp_path):
    assert trim_memory(make_settings(tmp_path).data_dir) is False


def test_the_trimmed_memory_is_left_owner_only(tmp_path):
    import stat

    settings = make_settings(tmp_path)
    path = memory_path(settings.data_dir)
    path.write_text("x" * (MAX_MEMORY_FILE_CHARS * 2), encoding="utf-8")
    path.chmod(0o644)

    trim_memory(settings.data_dir)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
