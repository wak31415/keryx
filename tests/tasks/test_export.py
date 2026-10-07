"""Tests for keryx.tasks.export: the JSON shape the Mac app reads."""

from datetime import UTC, datetime

from keryx.tasks.export import MAX_LOG_ENTRIES, parse_log, read_log, task_json, told
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


# --- the agent session log --------------------------------------------------


def test_parse_log_reads_a_progress_line():
    assert parse_log(["[10:30:00] looked at the failing test"]) == [
        {"time": "10:30:00", "kind": "progress", "text": "looked at the failing test"}
    ]


def test_parse_log_strips_the_followup_prefix_and_names_the_kind():
    assert parse_log(["[10:31:05] [followup] also check the README"]) == [
        {"time": "10:31:05", "kind": "followup", "text": "also check the README"}
    ]


def test_parse_log_joins_a_continuation_line_to_the_entry_before_it():
    lines = ["[10:30:00] a multi-line progress note:", "second line", "third line"]

    entries = parse_log(lines)

    assert entries == [
        {
            "time": "10:30:00",
            "kind": "progress",
            "text": "a multi-line progress note:\nsecond line\nthird line",
        }
    ]


def test_parse_log_a_leading_continuation_line_has_no_time():
    entries = parse_log(["stray line with no stamp before it"])

    assert entries == [
        {"time": None, "kind": "progress", "text": "stray line with no stamp before it"}
    ]


def test_parse_log_is_empty_for_no_lines():
    assert parse_log([]) == []


def test_read_log_keeps_only_the_last_500_entries(tmp_path):
    path = tmp_path / "9.log"
    path.write_text("".join(f"[10:00:{i % 60:02d}] entry {i}\n" for i in range(600)))

    entries, truncated = read_log(path)

    assert truncated is True
    assert len(entries) == MAX_LOG_ENTRIES
    assert entries[0]["text"] == "entry 100"  # oldest kept, newest first dropped from the front
    assert entries[-1]["text"] == "entry 599"


def test_read_log_says_untruncated_under_the_limit(tmp_path):
    path = tmp_path / "9.log"
    path.write_text("[10:00:00] only one\n")

    entries, truncated = read_log(path)

    assert truncated is False
    assert entries == [{"time": "10:00:00", "kind": "progress", "text": "only one"}]


def test_read_log_missing_file_is_an_empty_untruncated_list(tmp_path):
    entries, truncated = read_log(tmp_path / "no-such-task.log")

    assert (entries, truncated) == ([], False)
