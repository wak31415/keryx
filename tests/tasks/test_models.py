"""Tests for jarvis.tasks.models."""

from datetime import UTC, datetime

from jarvis.tasks.models import DESTRUCTIVE_KINDS, Task, TaskKind, TaskStatus

# --- enums -------------------------------------------------------------


def test_task_kind_values():
    assert TaskKind.CHAT == "chat"
    assert TaskKind.RESEARCH == "research"
    assert TaskKind.CODING == "coding"
    assert TaskKind.COWORK == "cowork"


def test_task_status_values():
    assert TaskStatus.QUEUED == "queued"
    assert TaskStatus.RUNNING == "running"
    assert TaskStatus.DONE == "done"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.CANCELLED == "cancelled"


def test_destructive_kinds():
    assert DESTRUCTIVE_KINDS == {TaskKind.CODING, TaskKind.COWORK}


# --- Task construction and defaults -------------------------------------


def test_task_positional_construction_with_defaults():
    task = Task(id=None, kind=TaskKind.CHAT, description="hello")

    assert task.id is None
    assert task.kind == TaskKind.CHAT
    assert task.description == "hello"
    assert task.status == TaskStatus.QUEUED
    assert task.project is None
    assert task.cwd is None
    assert task.model == "claude-opus-5"
    assert task.claude_session_id is None
    assert task.summary is None
    assert task.report_path is None
    assert task.error is None
    assert task.origin_channel == "local"
    assert task.origin_caller is None
    assert task.callback_requested is False
    assert task.callback_number is None
    assert task.announced is False
    assert task.sms_sent is False
    assert task.started_at is None
    assert task.finished_at is None


def test_task_created_at_defaults_to_now_utc():
    before = datetime.now(UTC)
    task = Task(id=None, kind=TaskKind.CHAT, description="hello")
    after = datetime.now(UTC)

    assert task.created_at.tzinfo is not None
    assert before <= task.created_at <= after


def test_task_created_at_default_factory_is_fresh_per_instance():
    task1 = Task(id=None, kind=TaskKind.CHAT, description="a")
    task2 = Task(id=None, kind=TaskKind.CHAT, description="b")

    assert task1.created_at is not task2.created_at  # not a shared mutable default


# --- to_row / from_row ----------------------------------------------------


def _sample_task(**overrides):
    defaults = dict(
        id=7,
        kind=TaskKind.CODING,
        description="add README",
        status=TaskStatus.RUNNING,
        project="garmin-voice-agent",
        cwd="/repo",
        model="claude-opus-5",
        claude_session_id="sess-1",
        summary="done",
        report_path="/tmp/report.md",
        error=None,
        origin_channel="phone",
        origin_caller="+15550001111",
        callback_requested=True,
        callback_number="+15550002222",
        announced=True,
        sms_sent=False,
        created_at=datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 18, 12, 1, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, 12, 5, 30, 123456, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_to_row_converts_enums_bools_and_datetimes():
    task = _sample_task()

    row = task.to_row()

    assert row["kind"] == "coding"
    assert row["status"] == "running"
    assert row["callback_requested"] == 1
    assert row["sms_sent"] == 0
    assert row["announced"] == 1
    assert row["created_at"] == "2026-08-18T12:00:00+00:00"
    assert row["started_at"] == "2026-08-18T12:01:00+00:00"
    assert row["finished_at"] == "2026-08-18T12:05:30.123456+00:00"
    assert row["description"] == "add README"
    assert row["id"] == 7
    assert isinstance(row["kind"], str)
    assert isinstance(row["callback_requested"], int)


def test_to_row_handles_none_optional_fields():
    task = Task(id=None, kind=TaskKind.CHAT, description="hi")

    row = task.to_row()

    assert row["id"] is None
    assert row["project"] is None
    assert row["started_at"] is None
    assert row["finished_at"] is None


def test_from_row_round_trips_every_field():
    task = _sample_task()

    round_tripped = Task.from_row(task.to_row())

    assert round_tripped == task


def test_from_row_produces_tz_aware_utc_datetimes():
    task = _sample_task()

    round_tripped = Task.from_row(task.to_row())

    assert round_tripped.created_at.tzinfo == UTC
    assert round_tripped.started_at.tzinfo == UTC
    assert round_tripped.finished_at.tzinfo == UTC


def test_from_row_converts_naive_datetime_as_utc():
    row = _sample_task().to_row()
    row["created_at"] = "2026-08-18T12:00:00"  # no offset

    task = Task.from_row(row)

    assert task.created_at == datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def test_from_row_converts_non_utc_offset_to_utc():
    row = _sample_task().to_row()
    row["created_at"] = "2026-08-18T08:00:00-04:00"

    task = Task.from_row(row)

    assert task.created_at == datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


# --- short_status_line -----------------------------------------------------


def test_short_status_line_format():
    task = Task(
        id=3,
        kind=TaskKind.CODING,
        description="add README to garmin-voice-agent",
        status=TaskStatus.RUNNING,
    )

    assert (
        task.short_status_line() == "task 3 (coding, running): add README to garmin-voice-agent"
    )


def test_short_status_line_truncates_long_description_with_ellipsis():
    long_description = "x" * 100
    task = Task(id=1, kind=TaskKind.CHAT, description=long_description)

    line = task.short_status_line()

    assert line == f"task 1 (chat, queued): {'x' * 80}…"


def test_short_status_line_does_not_truncate_at_exactly_80_chars():
    description = "x" * 80
    task = Task(id=1, kind=TaskKind.CHAT, description=description)

    line = task.short_status_line()

    assert line == f"task 1 (chat, queued): {'x' * 80}"
    assert "…" not in line
