"""Tests for the interim task announcer wired into `AppState` (spec §3.3).

A finished task has to reach whoever is on the line right now. Task 11 replaces this
with the Notifier (which also does SMS and call-backs); until then `AppState` speaks
task results into every live session itself.
"""

import asyncio
import contextlib

import pytest
from fakes import TIMEOUT, FakeProvider, FakeTransport, eventually

from jarvis.app import build_app_state, shutdown_app_state
from jarvis.events import TaskCompleted, TaskFailed
from jarvis.session import VoiceSession


@pytest.fixture
async def state(settings):
    built = build_app_state(settings.model_copy(update={"fake_agents": True}))
    yield built
    await shutdown_app_state(built)


@contextlib.asynccontextmanager
async def live_session(state, *, channel: str = "local"):
    """A real session running against a fake provider, registered with the state."""
    provider = FakeProvider()
    transport = FakeTransport(channel=channel, caller=None, audio_format="audio/pcm")
    session = VoiceSession(
        transport,
        provider,
        state.settings,
        state.registry,
        state.bus,
        authorized=True,
        registry=state.sessions,
    )
    task = asyncio.create_task(session.run())
    try:
        await eventually(lambda: session.is_live)
        yield session, provider
    finally:
        session.request_end("test over")
        await asyncio.wait_for(task, TIMEOUT)


def texts(provider: FakeProvider) -> list[str]:
    return [text for text, _respond, _instructions in provider.injected]


async def test_a_finished_task_is_spoken_into_every_live_session(state):
    async with live_session(state) as (_first, first_provider):
        async with live_session(state) as (_second, second_provider):
            await state.bus.publish(TaskCompleted(3, "I fixed the failing tests."))

            for provider in (first_provider, second_provider):
                assert "[system] Task 3 finished: I fixed the failing tests." in texts(provider)


async def test_a_failed_task_is_spoken_too(state):
    async with live_session(state) as (_session, provider):
        await state.bus.publish(TaskFailed(4, "the tests never passed"))

        assert "[system] Task 4 failed: the tests never passed" in texts(provider)


async def test_an_announcement_asks_for_a_spoken_response(state):
    async with live_session(state) as (_session, provider):
        await state.bus.publish(TaskCompleted(1, "done"))

    _text, respond, instructions = provider.injected[-1]
    assert respond is True
    assert instructions is not None


async def test_a_task_finishing_with_nobody_on_the_line_is_harmless(state):
    await state.bus.publish(TaskCompleted(9, "nobody heard this"))

    assert state.sessions.live() == []


async def test_the_announcer_can_be_taken_back_out(state):
    state.interim_unsubscribe()

    async with live_session(state) as (_session, provider):
        await state.bus.publish(TaskCompleted(5, "unheard"))

        assert texts(provider) == ["[session opened] Greet the user briefly."]
