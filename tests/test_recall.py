"""Tests for looking something up in what has already happened (spec §3.3).

Recall reads real transcript files and a real store, so these tests write both. The
matching is deliberately literal — every assertion here is about it staying that way,
because a fuzzy match is read out loud as if it were fact.
"""

from datetime import UTC, datetime

import pytest

from jarvis.events import EventBus
from jarvis.recall import MAX_LIMIT, Recaller, search_calls, terms
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
    assert terms("what did we decide about the garmin sync") == ["decide", "garmin", "sync"]


def test_a_query_of_nothing_but_filler_has_no_terms():
    assert terms("what did we say about that") == []
    assert terms("") == []


def test_terms_are_lowercased_and_de_duplicated_in_order():
    assert terms("Garmin garmin GARMIN poller") == ["garmin", "poller"]


# --- transcripts -----------------------------------------------------------


def test_a_transcript_hit_comes_back_with_the_line_around_it(settings):
    write_call(
        settings,
        "abc123",
        [
            "user: what should we do about the garmin sync",
            "assistant: poll it every fifteen minutes",
            "user: fine",
        ],
    )

    hits = search_calls(settings.data_dir, ["garmin"], limit=4)

    assert len(hits) == 1
    assert "garmin sync" in hits[0].text
    assert "fifteen minutes" in hits[0].text  # the answer, not just the question
    assert hits[0].source == "call"


def test_a_transcript_hit_is_dated_from_its_own_timestamp(settings):
    write_call(settings, "abc123", ["user: the garmin thing"])

    assert search_calls(settings.data_dir, ["garmin"], limit=4)[0].when == "22 August"


def test_an_older_transcript_with_only_a_wall_clock_is_dated_from_the_file(settings):
    path = settings.data_dir / "calls" / "old.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[14:02:11] user: the garmin thing\n")

    hit = search_calls(settings.data_dir, ["garmin"], limit=4)[0]

    assert hit.when  # dated from the file's mtime rather than left blank
    assert "garmin" in hit.text


def test_session_bookkeeping_lines_are_never_a_hit(settings):
    write_call(settings, "garmin", ["user: nothing relevant here"])

    assert search_calls(settings.data_dir, ["garmin"], limit=4) == []


def test_every_term_has_to_appear_in_the_same_line(settings):
    write_call(settings, "abc123", ["user: the garmin sync", "assistant: and a kayak"])

    assert search_calls(settings.data_dir, ["garmin", "kayak"], limit=4) == []


def test_no_calls_directory_is_not_an_error(settings, tmp_path):
    assert search_calls(tmp_path / "nowhere", ["garmin"], limit=4) == []


# --- the two sources together ----------------------------------------------


async def test_recall_returns_both_past_tasks_and_past_calls(settings, recaller):
    recall, store = recaller
    await _finished(store, "wire up the garmin poller", summary="polls every 15 minutes")
    write_call(settings, "abc123", ["user: remind me about the garmin sync"])

    hits = await recall.recall("garmin")

    assert {hit.source for hit in hits} == {"task", "call"}
    task_hit = next(hit for hit in hits if hit.source == "task")
    assert task_hit.task_id is not None
    assert "polls every 15 minutes" in task_hit.text


async def test_a_query_with_nothing_distinctive_in_it_searches_for_nothing(settings, recaller):
    recall, store = recaller
    await _finished(store, "wire up the garmin poller")

    assert await recall.recall("what did we say about that") == []


async def test_recall_that_finds_nothing_says_so_rather_than_guessing(settings, recaller):
    recall, _ = recaller

    assert await recall.recall("submarine") == []


async def test_the_limit_is_clamped_to_something_speakable(settings, recaller):
    recall, store = recaller
    for index in range(MAX_LIMIT + 5):
        await _finished(store, f"garmin job {index}")

    assert len(await recall.recall("garmin", limit=999)) <= MAX_LIMIT


async def test_housekeeping_never_surfaces_in_a_recall(settings, recaller):
    recall, store = recaller
    await _finished(store, "memory update after the garmin call", internal=True)

    assert await recall.recall("garmin") == []


async def test_half_of_a_recall_failing_still_returns_the_other_half(settings, recaller):
    _, store = recaller
    write_call(settings, "abc123", ["user: the garmin sync again"])

    class BrokenTasks:
        async def search(self, *_, **__):
            raise RuntimeError("the database is gone")

    hits = await Recaller(settings.data_dir, BrokenTasks()).recall("garmin")

    assert [hit.source for hit in hits] == ["call"]
