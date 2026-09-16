"""Tests for looking something up in what has already happened (spec §3.3).

Recall reads real transcript files and a real store, so these tests write both. The
matching is deliberately literal — every assertion here is about it staying that way,
because a fuzzy match is read out loud as if it were fact.
"""

import os
from datetime import UTC, datetime

import pytest

from jarvis.continuity.recall import MAX_LIMIT, Recaller, search_calls, terms
from jarvis.events import EventBus
from jarvis.tasks.agent_runner import FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore


@pytest.fixture
async def recaller(settings):
    settings.ensure_dirs()
    store = TaskStore(":memory:")
    manager = TaskManager(store, FakeAgentRunner(), EventBus(), settings)
    yield Recaller(settings.data_dir, manager), store
    await manager.shutdown()
    await store.close()


def write_call(settings, session_id: str, lines: list[str]) -> None:
    """A call transcript in the shape `VoiceSession` writes one."""
    path = settings.data_dir / "calls" / f"{session_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = [f"[2026-08-22T14:0{index}:00] {line}" for index, line in enumerate(lines)]
    path.write_text("\n".join([f"--- session {session_id} channel=phone", *stamped]) + "\n")


async def _finished(store, description: str, **overrides) -> Task:
    defaults = dict(
        id=None,
        kind=TaskKind.AGENT,
        description=description,
        status=TaskStatus.DONE,
        finished_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return await store.create(Task(**defaults))


# --- terms -----------------------------------------------------------------


def test_common_words_are_dropped_so_they_cannot_match_everything():
    assert terms("what did we decide about the orchard sync") == ["decide", "orchard", "sync"]


def test_a_query_of_nothing_but_filler_has_no_terms():
    assert terms("what did we say about that") == []
    assert terms("") == []


def test_terms_are_lowercased_and_de_duplicated_in_order():
    assert terms("Orchard orchard ORCHARD poller") == ["orchard", "poller"]


# --- transcripts -----------------------------------------------------------


def test_a_transcript_hit_comes_back_with_the_line_around_it(settings):
    write_call(
        settings,
        "abc123",
        [
            "user: what should we do about the orchard sync",
            "assistant: poll it every fifteen minutes",
            "user: fine",
        ],
    )

    hits = search_calls(settings.data_dir, ["orchard"], limit=4)

    assert len(hits) == 1
    assert "orchard sync" in hits[0].text
    assert "fifteen minutes" in hits[0].text  # the answer, not just the question
    assert hits[0].source == "call"


def test_a_transcript_hit_is_dated_from_its_own_timestamp(settings):
    write_call(settings, "abc123", ["user: the orchard thing"])

    assert search_calls(settings.data_dir, ["orchard"], limit=4)[0].when == "22 August"


def test_an_older_transcript_with_only_a_wall_clock_is_dated_from_the_file(settings):
    path = settings.data_dir / "calls" / "old.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[14:02:11] user: the orchard thing\n")

    hit = search_calls(settings.data_dir, ["orchard"], limit=4)[0]

    assert hit.when  # dated from the file's mtime rather than left blank
    assert "orchard" in hit.text


def test_a_wall_clock_transcript_is_dated_from_the_files_own_mtime(settings):
    """The fallback, pinned: transcripts written before the stamp carried a date."""
    path = settings.data_dir / "calls" / "old.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[14:02:11] user: the orchard thing\n")
    when = datetime(2026, 7, 4, 11, 30).timestamp()
    os.utime(path, (when, when))

    assert search_calls(settings.data_dir, ["orchard"], limit=4)[0].when == "4 July"


def test_an_unreadable_mtime_leaves_the_date_blank_rather_than_guessing(settings, monkeypatch):
    """A date read out loud is a claim; better to say nothing than to invent one."""
    path = settings.data_dir / "calls" / "old.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[14:02:11] user: the orchard thing\n")
    monkeypatch.setattr("jarvis.continuity.recall._file_date", lambda _path: None)

    assert search_calls(settings.data_dir, ["orchard"], limit=4)[0].when == ""


def test_a_transcript_line_with_a_broken_stamp_falls_back_too(settings):
    """`datetime.fromisoformat` refusing the stamp must not lose the hit."""
    path = settings.data_dir / "calls" / "broken.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[not-a-timestamp] user: the orchard thing\n")
    when = datetime(2026, 7, 4, 11, 30).timestamp()
    os.utime(path, (when, when))

    hit = search_calls(settings.data_dir, ["orchard"], limit=4)[0]

    assert (hit.when, "orchard" in hit.text) == ("4 July", True)


def test_a_transcript_stamp_is_spoken_in_local_time(settings):
    """`_spoken_date` calls `.astimezone()`, so a UTC stamp is said as the host sees it."""
    path = settings.data_dir / "calls" / "tz.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[2026-08-22T23:30:00+00:00] user: the orchard thing\n")

    expected = datetime(2026, 8, 22, 23, 30, tzinfo=UTC).astimezone().strftime("%-d %B")

    assert search_calls(settings.data_dir, ["orchard"], limit=4)[0].when == expected


def test_session_bookkeeping_lines_are_never_a_hit(settings):
    write_call(settings, "orchard", ["user: nothing relevant here"])

    assert search_calls(settings.data_dir, ["orchard"], limit=4) == []


def test_every_term_has_to_appear_in_the_same_line(settings):
    write_call(settings, "abc123", ["user: the orchard sync", "assistant: and a kayak"])

    assert search_calls(settings.data_dir, ["orchard", "kayak"], limit=4) == []


def test_no_calls_directory_is_not_an_error(settings, tmp_path):
    assert search_calls(tmp_path / "nowhere", ["orchard"], limit=4) == []


# --- the two sources together ----------------------------------------------


async def test_recall_returns_both_past_tasks_and_past_calls(settings, recaller):
    recall, store = recaller
    await _finished(store, "wire up the orchard poller", summary="polls every 15 minutes")
    write_call(settings, "abc123", ["user: remind me about the orchard sync"])

    hits = await recall.recall("orchard")

    assert {hit.source for hit in hits} == {"task", "call"}
    task_hit = next(hit for hit in hits if hit.source == "task")
    assert task_hit.task_id is not None
    assert "polls every 15 minutes" in task_hit.text


async def test_a_query_with_nothing_distinctive_in_it_searches_for_nothing(settings, recaller):
    recall, store = recaller
    await _finished(store, "wire up the orchard poller")

    assert await recall.recall("what did we say about that") == []


async def test_recall_that_finds_nothing_says_so_rather_than_guessing(settings, recaller):
    recall, _ = recaller

    assert await recall.recall("submarine") == []


async def test_the_limit_is_clamped_to_something_speakable(settings, recaller):
    recall, store = recaller
    for index in range(MAX_LIMIT + 5):
        await _finished(store, f"orchard job {index}")

    assert len(await recall.recall("orchard", limit=999)) <= MAX_LIMIT


async def test_housekeeping_never_surfaces_in_a_recall(settings, recaller):
    recall, store = recaller
    await _finished(store, "memory update after the orchard call", internal=True)

    assert await recall.recall("orchard") == []


# --- a PIN said aloud before transcripts were redacted ---------------------


def test_a_pin_still_on_disk_never_comes_back_in_a_hit(settings):
    """Transcripts written before redaction hold the PIN he said; recall must not read it
    out, and must not confirm a guess by finding it."""
    write_call(settings, "old", ["assistant: What's your PIN?", "user: 1 2 3 4 5 6."])

    hits = search_calls(settings.data_dir, ["pin"], limit=4, pin="123456")

    assert {hit.text for hit in hits} == {"assistant: What's your PIN? user: [PIN]."}
    assert search_calls(settings.data_dir, ["123456"], limit=4, pin="123456") == []


async def test_a_pin_in_a_task_never_comes_back_either(settings):
    store = TaskStore(":memory:")
    manager = TaskManager(store, FakeAgentRunner(), EventBus(), settings)
    await _finished(store, "log in to the bank with 123456", summary="done")

    hits = await Recaller(settings.data_dir, manager, pin="123456").recall("bank")

    assert [hit.text for hit in hits] == ["he asked: log in to the bank with [PIN] — result: done"]
    await manager.shutdown()
    await store.close()


async def test_half_of_a_recall_failing_still_returns_the_other_half(settings, recaller):
    _, store = recaller
    write_call(settings, "abc123", ["user: the orchard sync again"])

    class BrokenTasks:
        async def search(self, *_, **__):
            raise RuntimeError("the database is gone")

    hits = await Recaller(settings.data_dir, BrokenTasks()).recall("orchard")

    assert [hit.source for hit in hits] == ["call"]
