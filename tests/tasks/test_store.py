"""Tests for jarvis.tasks.store."""

import asyncio
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
    defaults = dict(id=None, kind=TaskKind.CODING, description="add README")
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
        kind=TaskKind.COWORK,
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
