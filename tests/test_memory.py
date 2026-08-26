"""Tests for the per-call memory update (spec §3.3).

The job is a real dispatch through a real `TaskManager`, with a `FakeAgentRunner` behind
it — so what these assert on is the task that gets created: that it is internal, that it
is aimed at the right two files, and that a call not worth remembering creates nothing.
"""

import pytest

from jarvis.briefing import memory_path
from jarvis.events import EventBus, SessionEnded
from jarvis.memory import MIN_SPOKEN_LINES, MemoryWriter, count_spoken_lines, describe_tasks
from jarvis.tasks.agent_runner import FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.store import TaskStore
from jarvis.transcripts import transcript_path


@pytest.fixture
async def writer(settings):
    settings.ensure_dirs()
    bus = EventBus()
    store = TaskStore(":memory:")
    manager = TaskManager(store, FakeAgentRunner(), bus, settings)
    memory_writer = MemoryWriter(bus, manager, settings)
    memory_writer.start()
    yield memory_writer, bus, manager, store
    memory_writer.stop()
    await manager.shutdown()
    await store.close()


def write_transcript(settings, session_id: str, lines: list[str]) -> None:
    path = transcript_path(settings.data_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = [f"[2026-08-25T14:0{index}:00] {line}" for index, line in enumerate(lines)]
    path.write_text("\n".join([f"--- session {session_id} channel=phone", *stamped]) + "\n")


def ended(session_id: str = "abc123") -> SessionEnded:
    return SessionEnded(session_id, "phone", "+15550001111", "user")


async def internal_tasks(store):
    return [task for task in await store.list(include_internal=True) if task.internal]


# --- what counts as a call worth remembering -------------------------------


def test_spoken_lines_are_counted_and_bookkeeping_is_not(settings):
    write_transcript(settings, "abc123", ["user: hello", "assistant: hi", "--- session ended"])

    assert count_spoken_lines(transcript_path(settings.data_dir, "abc123")) == 2


def test_a_transcript_that_is_not_there_counts_as_nothing_said(settings, tmp_path):
    assert count_spoken_lines(tmp_path / "nowhere.log") == 0


async def test_a_real_conversation_dispatches_a_memory_update(settings, writer):
    _, bus, _, store = writer
    write_transcript(settings, "abc123", ["user: how is the sync", "assistant: it landed"])

    await bus.publish(ended())

    tasks = await internal_tasks(store)
    assert len(tasks) == 1
    assert tasks[0].internal is True


async def test_a_misfire_is_not_worth_a_subagent(settings, writer):
    _, bus, _, store = writer
    write_transcript(settings, "abc123", ["assistant: hello?"])  # below MIN_SPOKEN_LINES

    await bus.publish(ended())

    assert await internal_tasks(store) == []
    assert MIN_SPOKEN_LINES == 2


async def test_a_call_with_no_transcript_at_all_is_skipped(writer):
    _, bus, _, store = writer

    await bus.publish(ended("never-happened"))

    assert await internal_tasks(store) == []


# --- what the subagent is told ---------------------------------------------


async def test_the_update_names_the_transcript_and_the_memory_file(settings, writer):
    _, bus, _, store = writer
    write_transcript(settings, "abc123", ["user: how is the sync", "assistant: it landed"])

    await bus.publish(ended())

    description = (await internal_tasks(store))[0].description
    assert str(transcript_path(settings.data_dir, "abc123")) in description
    assert str(memory_path(settings.data_dir)) in description


async def test_the_update_runs_in_the_data_directory_not_a_repo(settings, writer):
    _, bus, _, store = writer
    write_transcript(settings, "abc123", ["user: how is the sync", "assistant: it landed"])

    await bus.publish(ended())

    assert (await internal_tasks(store))[0].cwd == str(settings.data_dir)


async def test_the_update_is_told_what_the_call_dispatched(settings, writer):
    _, bus, manager, store = writer
    write_transcript(settings, "abc123", ["user: fix the poller", "assistant: on it"])
    task = await manager.dispatch(
        "fix the poller",
        origin_channel="phone",
        origin_caller="+15550001111",
        origin_session_id="abc123",
    )

    await bus.publish(ended())

    description = (await internal_tasks(store))[0].description
    assert f"task {task.id}" in description


def test_a_call_that_dispatched_nothing_says_so():
    assert describe_tasks([]) == "none"


# --- it can never break a call ---------------------------------------------


async def test_a_manager_that_will_not_dispatch_does_not_raise_into_the_bus(settings):
    bus = EventBus()
    write_transcript(settings, "abc123", ["user: hello there", "assistant: hello"])

    class Broken:
        async def tasks_for_session(self, _):
            return []

        async def dispatch(self, *_, **__):
            raise RuntimeError("no room")

    MemoryWriter(bus, Broken(), settings).start()

    await bus.publish(ended())  # must not raise


async def test_start_and_stop_are_idempotent(settings, writer):
    memory_writer, bus, _, store = writer
    write_transcript(settings, "abc123", ["user: worth remembering", "assistant: noted"])
    memory_writer.start()
    memory_writer.start()
    memory_writer.stop()
    memory_writer.stop()

    await bus.publish(ended())

    assert await internal_tasks(store) == []  # unsubscribed, so nothing was dispatched
