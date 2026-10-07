"""Tests for keryx.tasks.export: the JSON shape the Mac app reads."""

from datetime import UTC, datetime

from keryx.tasks.export import task_json, told
from keryx.tasks.models import Task, TaskKind, TaskStatus


def _task(**kw):
    return Task(id=7, kind=TaskKind.AGENT, description="fix the parser", **kw)


def test_task_json_carries_the_fields_the_app_reads_and_no_phone_numbers():
    task = _task(
        status=TaskStatus.DONE,
        origin_caller="+15551234567",
        callback_number="+15551234567",
        callback_note="private",
        created_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
    )
    data = task_json(task)
    assert data["id"] == 7 and data["status"] == "done"
    assert data["created_at"] == "2026-10-06T12:00:00+00:00"
    for hidden in ("origin_caller", "callback_number", "callback_note", "claude_session_id"):
        assert hidden not in data
    assert "+1555" not in str(data)


def test_task_json_null_datetimes_and_description():
    task = _task(status=TaskStatus.QUEUED)

    data = task_json(task)

    assert data["started_at"] is None
    assert data["finished_at"] is None
    assert data["reported_at"] is None
    assert data["description"] == "fix the parser"


def test_told_is_none_until_finished_and_for_housekeeping():
    assert told(_task(status=TaskStatus.RUNNING)) is None
    assert told(_task(status=TaskStatus.DONE, internal=True)) is None
    assert told(_task(status=TaskStatus.DONE)) is False
    assert told(_task(status=TaskStatus.FAILED, reported_at=datetime.now(UTC))) is True


def test_task_json_told_agrees_with_told():
    done_unreported = _task(status=TaskStatus.DONE)
    done_reported = _task(status=TaskStatus.DONE, reported_at=datetime.now(UTC))
    running = _task(status=TaskStatus.RUNNING)

    assert task_json(done_unreported)["told"] is False
    assert task_json(done_reported)["told"] is True
    assert task_json(running)["told"] is None
