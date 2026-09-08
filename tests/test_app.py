"""Tests for the shared application state the server and the CLI are built on."""

import pytest
from fakes import FakeVoiceSession

from jarvis.app import AppState, build_app_state, shutdown_app_state
from jarvis.continuity.briefing import Briefer
from jarvis.continuity.memory import MemoryWriter, memory_path
from jarvis.continuity.transcripts import transcript_path
from jarvis.events import SessionEnded, TaskCompleted
from jarvis.notify.notifier import Notifier
from jarvis.notify.twilio_out import TwilioOut
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.restart.coordinator import RestartCoordinator
from jarvis.tasks.agent_runner import ClaudeAgentRunner, FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import Task, TaskKind
from jarvis.tasks.store import TaskStore


@pytest.fixture
async def state(settings):
    """A built state that is always shut down again (the store holds a sqlite handle)."""
    built = build_app_state(settings)
    yield built
    await shutdown_app_state(built)


async def test_build_app_state_wires_the_shared_registries(state, settings):
    assert isinstance(state, AppState)
    assert state.settings is settings
    assert state.sessions.live() == []
    assert len(state.stream_tokens) == 0


async def test_build_app_state_wires_the_task_stack(state, settings):
    assert isinstance(state.store, TaskStore)
    assert isinstance(state.manager, TaskManager)
    assert (settings.data_dir / "tasks.db").exists()


async def test_build_app_state_registers_the_voice_tools(state):
    names = {schema["name"] for schema in state.registry.schemas()}

    assert {"dispatch_task", "list_tasks", "submit_pin", "end_session"} <= names


async def test_build_app_state_wires_the_notifier(state):
    assert isinstance(state.twilio_out, TwilioOut)
    assert isinstance(state.notifier, Notifier)
    assert state.twilio_out.configured is False  # no Twilio credentials in these settings


async def test_build_app_state_wires_the_restart_coordinator(state):
    """It shares the session registry, so it can see a call it must not interrupt."""
    assert isinstance(state.restart, RestartCoordinator)
    assert "restart_service" in {schema["name"] for schema in state.registry.schemas()}
    assert state.restart._sessions is state.sessions


async def test_a_finished_task_is_spoken_into_every_live_session(state):
    session = FakeVoiceSession(channel="local")
    state.sessions.add(session)
    task = await state.store.create(Task(id=None, kind=TaskKind.AGENT, description="dig"))

    await state.bus.publish(TaskCompleted(task.id, "all done"))

    assert session.announced == [f"Task {task.id} finished: all done"]
    assert (await state.store.get(task.id)).announced is True


async def test_shutting_down_takes_the_notifier_off_the_bus(state):
    session = FakeVoiceSession(channel="local")
    state.sessions.add(session)
    task = await state.store.create(Task(id=None, kind=TaskKind.AGENT, description="dig"))

    await shutdown_app_state(state)
    await state.bus.publish(TaskCompleted(task.id, "nobody hears this"))

    assert session.announced == []


async def test_the_real_agent_runner_is_used_unless_fakes_are_asked_for(settings):
    real = build_app_state(settings)
    fake = build_app_state(settings.model_copy(update={"fake_agents": True}))

    assert isinstance(real.manager._runner, ClaudeAgentRunner)
    assert isinstance(fake.manager._runner, FakeAgentRunner)

    await shutdown_app_state(real)
    await shutdown_app_state(fake)


async def test_shutting_down_closes_the_store(settings):
    built = build_app_state(settings)

    await shutdown_app_state(built)
    await shutdown_app_state(built)  # idempotent: the CLI may shut down twice

    assert built.store._conn is None


async def test_the_provider_factory_builds_a_realtime_client_per_call(state):
    provider = state.provider_factory()

    assert isinstance(provider, OpenAIRealtimeClient)
    assert provider is not state.provider_factory()


async def test_the_provider_factory_passes_the_configured_key_and_model(settings, monkeypatch):
    made: list[tuple] = []
    monkeypatch.setattr("jarvis.app.OpenAIRealtimeClient", lambda *args: made.append(args))
    built = build_app_state(settings)

    assert made == []  # nothing is connected until a call actually arrives
    built.provider_factory()

    assert made == [(settings.openai_api_key, settings.openai_realtime_model)]
    await shutdown_app_state(built)


# --- continuity: the briefer, the memory writer and recall ------------------


async def test_build_app_state_wires_the_briefer_and_the_memory_writer(state):
    assert isinstance(state.briefer, Briefer)
    assert isinstance(state.memory, MemoryWriter)


async def test_build_app_state_offers_recall_to_the_voice_model(state):
    assert {"recall", "mark_reported"} <= {s["name"] for s in state.registry.schemas()}


async def test_a_call_that_ends_leaves_a_memory_update_behind(state, settings):
    """The other half of continuity: the notifier carries a result out, this writes it down."""
    transcript_path(settings.data_dir, "abc123").write_text(
        "[2026-08-25T14:00:00] user: how is the sync\n"
        "[2026-08-25T14:00:05] assistant: it landed this morning\n"
    )

    await state.bus.publish(SessionEnded("abc123", "phone", "+15550001111", "user"))

    internal = [task for task in await state.store.list(include_internal=True) if task.internal]
    assert len(internal) == 1
    assert str(memory_path(settings.data_dir)) in internal[0].description


async def test_shutdown_takes_the_memory_writer_off_the_bus_too(state, settings):
    transcript_path(settings.data_dir, "abc123").write_text(
        "[2026-08-25T14:00:00] user: how is the sync\n"
        "[2026-08-25T14:00:05] assistant: it landed\n"
    )
    await shutdown_app_state(state)

    await state.bus.publish(SessionEnded("abc123", "phone", None, "user"))

    assert state.memory._remove is None
