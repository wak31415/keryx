"""Tests for what a call opens knowing.

The digest and the memory are assembled before the provider is connected, so everything
here is about two questions: does the right thing reach the prompt, and does a broken
source still let the call happen.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from jarvis.continuity.briefing import Briefer, Briefing, format_digest
from jarvis.continuity.memory import MAX_MEMORY_CHARS, memory_path, read_memory
from jarvis.events import EventBus
from jarvis.tasks.agent_runner import FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore


@dataclass
class Tasks:
    """A manager and the store behind it, so a test can seed a finished task directly."""

    manager: TaskManager
    store: TaskStore


@pytest.fixture
async def tasks(settings):
    store = TaskStore(":memory:")
    manager = TaskManager(store, FakeAgentRunner(), EventBus(), settings)
    yield Tasks(manager, store)
    await manager.shutdown()
    await store.close()


def _finished(**overrides) -> Task:
    defaults = dict(
        id=None,
        kind=TaskKind.AGENT,
        description="rewrite the ingest script",
        status=TaskStatus.DONE,
        summary="rewrote it and the tests pass",
        finished_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Task(**defaults)


# --- the memory file -------------------------------------------------------


def test_no_memory_file_is_an_empty_memory_not_an_error(settings):
    assert read_memory(settings.data_dir) == ""


def test_the_memory_is_read_back_whole_when_it_fits(settings):
    settings.ensure_dirs()
    memory_path(settings.data_dir).write_text("# What Jarvis knows\n\nThey hate jargon.\n")

    assert "They hate jargon." in read_memory(settings.data_dir)


def test_an_oversized_memory_keeps_its_head_and_says_it_was_trimmed(settings):
    settings.ensure_dirs()
    body = "standing facts first\n" + ("x" * MAX_MEMORY_CHARS) + "\nrecent calls last"
    memory_path(settings.data_dir).write_text(body)

    text = read_memory(settings.data_dir)

    assert text.startswith("standing facts first")
    assert "trimmed" in text
    assert "recent calls last" not in text


# --- the digest ------------------------------------------------------------


def test_nothing_unreported_is_no_digest_at_all():
    assert format_digest([], 0) == ""


def test_the_digest_carries_the_id_the_request_and_the_result():
    digest = format_digest([_finished(id=41)], 1)

    assert "task 41" in digest
    assert "rewrite the ingest script" in digest
    assert "rewrote it and the tests pass" in digest
    assert "mark_reported" in digest  # the model is told how to close the loop


def test_a_failure_is_shown_as_a_failure_with_its_error():
    digest = format_digest(
        [_finished(id=43, status=TaskStatus.FAILED, summary=None, error="could not reach the API")],
        1,
    )

    assert "failed" in digest
    assert "could not reach the API" in digest


def test_the_digest_says_how_many_more_are_waiting():
    digest = format_digest([_finished(id=1), _finished(id=2)], 7)

    assert "5 more" in digest


# --- the opening nudge -----------------------------------------------------


def test_nothing_pending_means_nothing_appended_to_the_opening():
    assert Briefing().opening_nudge() == ""


def test_one_pending_task_is_spoken_of_in_the_singular():
    nudge = Briefing(pending="…", pending_count=1).opening_nudge()

    assert "1 task finished" in nudge


def test_nothing_pending_means_no_nudge_after_the_pin_either():
    assert Briefing(memory="something").after_pin_nudge() == ""


def test_the_nudge_after_the_pin_does_not_greet_them_a_second_time():
    """They have been talking for a while by the time the PIN goes in."""
    nudge = Briefing(pending="…", pending_count=2).after_pin_nudge()

    assert nudge.startswith("[system] 2 tasks finished")
    assert "What the owner has not heard yet" in nudge
    assert "greeting" not in nudge and "not greet them again" in nudge


def test_several_pending_tasks_are_plural():
    assert "3 tasks finished" in Briefing(pending="…", pending_count=3).opening_nudge()


# --- Briefer ---------------------------------------------------------------


async def test_a_fresh_machine_briefs_with_nothing_and_that_is_fine(settings, tasks):
    briefing = await Briefer(settings, tasks.manager).build()

    assert briefing == Briefing()


async def test_the_briefer_finds_the_memory_and_the_unreported_work(settings, tasks):
    settings.ensure_dirs()
    memory_path(settings.data_dir).write_text("They are mid-way through the orchard sync.")
    task = await tasks.store.create(_finished(description="wire up the poller"))

    briefing = await Briefer(settings, tasks.manager).build()

    assert "orchard sync" in briefing.memory
    assert f"task {task.id}" in briefing.pending
    assert briefing.pending_count == 1


async def test_a_task_already_reported_is_not_briefed_again(settings, tasks):
    task = await tasks.store.create(_finished())
    await tasks.manager.mark_reported([task.id])

    briefing = await Briefer(settings, tasks.manager).build()

    assert briefing.pending == ""
    assert briefing.pending_count == 0


async def test_a_store_that_will_not_answer_still_lets_the_call_open(settings, tasks):
    class Broken:
        async def unreported(self, **_):
            raise RuntimeError("the database is gone")

        async def count_unreported(self):
            raise RuntimeError("the database is gone")

    briefing = await Briefer(settings, Broken()).build()

    assert briefing == Briefing()
