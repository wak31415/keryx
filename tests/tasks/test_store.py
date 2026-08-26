"""Tests for jarvis.tasks.store."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore


@pytest.fixture(params=["file", "memory"])
async def store(request, tmp_path):
    path = tmp_path / "tasks.db" if request.param == "file" else ":memory:"
    task_store = TaskStore(path)
    yield task_store
    await task_store.close()


def _task(**overrides) -> Task:
    defaults = dict(id=None, kind=TaskKind.AGENT, description="add README")
    defaults.update(overrides)
    return Task(**defaults)


# --- create / get --------------------------------------------------------


async def test_create_assigns_id_and_does_not_mutate_argument(store):
    task = _task()

    created = await store.create(task)

    assert task.id is None  # the argument passed in is untouched
    assert isinstance(created.id, int)


async def test_create_then_get_round_trips_every_field(store):
    task = Task(
        id=None,
        kind=TaskKind.AGENT,
        description="ship the release",
        status=TaskStatus.RUNNING,
        project="garmin-voice-agent",
        cwd="/repo",
        model="claude-opus-5",
        claude_session_id="sess-42",
        summary="shipped",
        report_path="/data/tasks/1.md",
        error=None,
        origin_channel="phone",
        origin_caller="+15550001111",
        callback_requested=True,
        callback_number="+15550002222",
        announced=True,
        sms_sent=True,
        created_at=datetime(2026, 8, 18, 9, 0, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 18, 9, 1, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, 9, 5, 0, 500000, tzinfo=UTC),
    )

    created = await store.create(task)
    fetched = await store.get(created.id)

    assert fetched == created


async def test_create_preserves_tz_aware_utc_datetime(store):
    when = datetime(2026, 1, 1, tzinfo=UTC)
    task = _task(created_at=when)

    created = await store.create(task)
    fetched = await store.get(created.id)

    assert fetched.created_at == when
    assert fetched.created_at.tzinfo == UTC


async def test_get_missing_id_returns_none(store):
    result = await store.get(999)

    assert result is None


# --- update ----------------------------------------------------------------


async def test_update_patches_only_given_fields(store):
    task = await store.create(_task())

    updated = await store.update(task.id, status=TaskStatus.DONE, summary="ok")

    assert updated.status == TaskStatus.DONE
    assert updated.summary == "ok"
    assert updated.description == task.description  # untouched field preserved

    persisted = await store.get(task.id)
    assert persisted == updated


async def test_update_unknown_field_raises_value_error(store):
    task = await store.create(_task())

    with pytest.raises(ValueError):
        await store.update(task.id, bogus_field="x")


async def test_update_missing_id_raises_key_error(store):
    with pytest.raises(KeyError):
        await store.update(999, status=TaskStatus.DONE)


async def test_update_with_no_fields_returns_current_task(store):
    task = await store.create(_task())

    result = await store.update(task.id)

    assert result == task


async def test_concurrent_updates_do_not_clobber_each_other(store):
    """Two overlapping patches must both survive: an update rewrites every column."""
    task = await store.create(_task())

    for _ in range(20):
        await store.update(task.id, status=TaskStatus.QUEUED, callback_requested=False)
        await asyncio.gather(
            store.update(task.id, status=TaskStatus.RUNNING),
            store.update(task.id, callback_requested=True),
        )
        fresh = await store.get(task.id)
        assert (fresh.status, fresh.callback_requested) == (TaskStatus.RUNNING, True)


# --- list --------------------------------------------------------------------


async def test_list_orders_newest_first(store):
    first = await store.create(_task(description="first"))
    second = await store.create(_task(description="second"))
    third = await store.create(_task(description="third"))

    tasks = await store.list()

    assert [t.id for t in tasks] == [third.id, second.id, first.id]


async def test_list_filters_by_status(store):
    queued = await store.create(_task(status=TaskStatus.QUEUED))
    done = await store.create(_task(status=TaskStatus.DONE))

    result = await store.list(status=TaskStatus.DONE)

    assert [t.id for t in result] == [done.id]
    assert queued.id not in [t.id for t in result]


async def test_list_respects_limit(store):
    for i in range(5):
        await store.create(_task(description=f"task {i}"))

    result = await store.list(limit=2)

    assert len(result) == 2


async def test_list_empty_store_returns_empty_list(store):
    result = await store.list()

    assert result == []


# --- count_created_since ------------------------------------------------------


async def test_count_created_since_boundary(store):
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    await store.create(_task(created_at=now - timedelta(hours=1)))
    await store.create(_task(created_at=now))
    await store.create(_task(created_at=now + timedelta(hours=1)))

    count = await store.count_created_since(now)

    assert count == 2  # exact boundary is inclusive, the older one is excluded


async def test_count_created_since_converts_non_utc_to_utc(store):
    now_utc = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    await store.create(_task(created_at=now_utc))

    since_other_tz = now_utc.astimezone(timezone(timedelta(hours=-4)))
    count = await store.count_created_since(since_other_tz)

    assert count == 1


# --- close -----------------------------------------------------------------


async def test_close_is_idempotent():
    task_store = TaskStore(":memory:")
    await task_store.close()
    await task_store.close()  # must not raise


async def test_store_persists_across_instances_for_file_path(tmp_path):
    path = tmp_path / "tasks.db"
    store1 = TaskStore(path)
    created = await store1.create(_task(description="persisted"))
    await store1.close()

    store2 = TaskStore(path)
    fetched = await store2.get(created.id)
    await store2.close()

    assert fetched is not None
    assert fetched.description == "persisted"


# --- reported / internal (schema v3) ---------------------------------------


async def _finished(store, description: str, **overrides) -> Task:
    """A task already in a terminal state, the way the notifier leaves one behind."""
    defaults = dict(status=TaskStatus.DONE, summary="all done", finished_at=datetime.now(UTC))
    defaults.update(overrides)
    return await store.create(_task(description=description, **defaults))


async def test_a_finished_task_starts_out_unreported(store):
    created = await _finished(store, "rewrite the ingest script")

    assert created.reported_at is None
    assert [task.id for task in await store.list_unreported()] == [created.id]


async def test_unreported_covers_failures_but_not_running_or_cancelled_work(store):
    done = await _finished(store, "done")
    failed = await _finished(store, "failed", status=TaskStatus.FAILED, error="boom")
    await store.create(_task(description="still going", status=TaskStatus.RUNNING))
    await _finished(store, "cancelled", status=TaskStatus.CANCELLED)

    assert [task.id for task in await store.list_unreported()] == [done.id, failed.id]


async def test_unreported_is_oldest_first_so_it_reads_as_a_sequence(store):
    first = await _finished(store, "first")
    second = await _finished(store, "second")

    assert [task.id for task in await store.list_unreported()] == [first.id, second.id]


async def test_unreported_respects_its_limit_while_the_count_stays_honest(store):
    for index in range(4):
        await _finished(store, f"task {index}")

    assert len(await store.list_unreported(limit=2)) == 2
    assert await store.count_unreported() == 4


async def test_mark_reported_stamps_only_the_ids_that_needed_it(store):
    first = await _finished(store, "first")
    second = await _finished(store, "second")
    when = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)

    stamped = await store.mark_reported([first.id, second.id, 999], when=when)

    assert stamped == [first.id, second.id]
    assert (await store.get(first.id)).reported_at == when
    assert await store.list_unreported() == []


async def test_marking_a_task_reported_twice_keeps_the_first_time(store):
    task = await _finished(store, "already said")
    first_time = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    await store.mark_reported([task.id], when=first_time)

    stamped = await store.mark_reported([task.id], when=datetime(2026, 8, 26, 9, 0, tzinfo=UTC))

    assert stamped == []  # nothing to say a second time
    assert (await store.get(task.id)).reported_at == first_time


async def test_mark_reported_with_no_ids_is_a_no_op(store):
    assert await store.mark_reported([], when=datetime.now(UTC)) == []


async def test_internal_tasks_stay_out_of_the_digest_the_lists_and_the_cap(store):
    await _finished(store, "his work")
    await _finished(store, "jarvis's own memory update", internal=True)

    assert [task.description for task in await store.list_unreported()] == ["his work"]
    assert [task.description for task in await store.list()] == ["his work"]
    assert await store.count_created_since(datetime(2026, 1, 1, tzinfo=UTC)) == 1


async def test_internal_tasks_are_visible_when_asked_for(store):
    await _finished(store, "his work")
    await _finished(store, "housekeeping", internal=True)

    listed = await store.list(include_internal=True)

    assert {task.description for task in listed} == {"his work", "housekeeping"}


# --- search / by session ---------------------------------------------------


async def test_search_requires_every_term_and_looks_at_the_summary_too(store):
    await _finished(store, "wire up the garmin poller", summary="polls every 15 minutes")
    await _finished(store, "unrelated work", summary="nothing to do with it")

    assert len(await store.search(["garmin"])) == 1
    assert len(await store.search(["garmin", "poller"])) == 1
    assert len(await store.search(["garmin", "kayak"])) == 0
    assert len(await store.search(["minutes"])) == 1  # matched in the summary


async def test_search_treats_wildcards_as_literal_characters(store):
    await _finished(store, "plain description")

    assert await store.search(["%"]) == []
    assert await store.search(["_"]) == []


async def test_search_ignores_housekeeping_and_an_empty_query(store):
    await _finished(store, "memory update for the garmin call", internal=True)

    assert await store.search(["garmin"]) == []
    assert await store.search([]) == []


async def test_tasks_can_be_found_by_the_call_that_dispatched_them(store):
    first = await store.create(_task(description="one", origin_session_id="sess-a"))
    second = await store.create(_task(description="two", origin_session_id="sess-a"))
    await store.create(_task(description="other", origin_session_id="sess-b"))

    found = await store.list_for_session("sess-a")

    assert [task.id for task in found] == [first.id, second.id]
    assert await store.list_for_session("") == []


# --- migration -------------------------------------------------------------


def _v2_database(path) -> None:
    """A tasks.db as schema v2 left it: no `reported_at`, no `internal`."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    conn.execute(
        "CREATE TABLE tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, description TEXT NOT NULL,"
        "status TEXT NOT NULL, project TEXT, cwd TEXT, model TEXT NOT NULL,"
        "claude_session_id TEXT, summary TEXT, report_path TEXT, error TEXT,"
        "origin_channel TEXT NOT NULL, origin_caller TEXT, origin_session_id TEXT,"
        "callback_requested INTEGER NOT NULL DEFAULT 0, callback_number TEXT,"
        "callback_note TEXT, announced INTEGER NOT NULL DEFAULT 0,"
        "sms_sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
        "started_at TEXT, finished_at TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (kind, description, status, model, origin_channel, created_at,"
        " finished_at) VALUES ('agent', 'old finished work', 'done', 'claude-opus-5',"
        " 'phone', '2026-08-01T09:00:00+00:00', '2026-08-01T09:05:00+00:00')"
    )
    conn.execute(
        "INSERT INTO tasks (kind, description, status, model, origin_channel, created_at)"
        " VALUES ('agent', 'old queued work', 'queued', 'claude-opus-5', 'phone',"
        " '2026-08-01T09:00:00+00:00')"
    )
    conn.close()


async def test_a_v2_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "tasks.db"
    _v2_database(path)

    store = TaskStore(path)
    try:
        tasks = await store.list()
        assert {task.description for task in tasks} == {"old finished work", "old queued work"}
        assert all(task.internal is False for task in tasks)
    finally:
        await store.close()


async def test_work_that_finished_before_the_upgrade_counts_as_already_told(tmp_path):
    """Otherwise the first call after the upgrade opens by reading out the whole history."""
    path = tmp_path / "tasks.db"
    _v2_database(path)

    store = TaskStore(path)
    try:
        assert await store.list_unreported() == []
        finished = next(t for t in await store.list() if t.description == "old finished work")
        assert finished.reported_at == datetime(2026, 8, 1, 9, 5, tzinfo=UTC)
    finally:
        await store.close()


async def test_migrating_is_idempotent_across_reopens(tmp_path):
    path = tmp_path / "tasks.db"
    _v2_database(path)

    for _ in range(3):
        store = TaskStore(path)
        await store.close()

    store = TaskStore(path)
    try:
        assert len(await store.list()) == 2
    finally:
        await store.close()
