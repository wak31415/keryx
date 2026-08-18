"""Tests for jarvis.events."""

import asyncio
import logging

from jarvis.events import (
    EventBus,
    SessionEnded,
    SessionStarted,
    TaskCompleted,
    TaskFailed,
    TaskProgress,
    TaskStarted,
)

# --- event dataclasses -------------------------------------------------


def test_task_started_fields():
    event = TaskStarted(task_id=1)
    assert event.task_id == 1


def test_task_progress_fields():
    event = TaskProgress(task_id=1, text="working")
    assert event.task_id == 1
    assert event.text == "working"


def test_task_completed_fields():
    event = TaskCompleted(task_id=1, summary="done")
    assert event.task_id == 1
    assert event.summary == "done"


def test_task_failed_fields():
    event = TaskFailed(task_id=1, error="boom")
    assert event.task_id == 1
    assert event.error == "boom"


def test_session_started_fields():
    event = SessionStarted(session_id="abc", channel="phone", caller="+15550001111")
    assert event.session_id == "abc"
    assert event.channel == "phone"
    assert event.caller == "+15550001111"


def test_session_ended_fields():
    event = SessionEnded(session_id="abc", channel="local", caller=None, reason="user")
    assert event.session_id == "abc"
    assert event.channel == "local"
    assert event.caller is None
    assert event.reason == "user"


# --- EventBus ------------------------------------------------------------


async def test_publish_dispatches_to_sync_handler():
    bus = EventBus()
    received = []
    bus.subscribe(TaskStarted, received.append)

    await bus.publish(TaskStarted(task_id=1))

    assert received == [TaskStarted(task_id=1)]


async def test_publish_awaits_async_handler():
    bus = EventBus()
    received = []

    async def handler(event):
        await asyncio.sleep(0)
        received.append(event)

    bus.subscribe(TaskStarted, handler)

    await bus.publish(TaskStarted(task_id=1))

    assert received == [TaskStarted(task_id=1)]


async def test_publish_only_dispatches_to_matching_type():
    bus = EventBus()
    started_received = []
    progress_received = []
    bus.subscribe(TaskStarted, started_received.append)
    bus.subscribe(TaskProgress, progress_received.append)

    await bus.publish(TaskStarted(task_id=1))

    assert started_received == [TaskStarted(task_id=1)]
    assert progress_received == []


async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    received = []
    unsubscribe = bus.subscribe(TaskStarted, received.append)

    unsubscribe()
    await bus.publish(TaskStarted(task_id=1))

    assert received == []


async def test_unsubscribe_is_idempotent():
    bus = EventBus()
    unsubscribe = bus.subscribe(TaskStarted, lambda e: None)

    unsubscribe()
    unsubscribe()  # must not raise


async def test_wildcard_subscriber_receives_all_event_types():
    bus = EventBus()
    received = []
    bus.subscribe(object, received.append)

    await bus.publish(TaskStarted(task_id=1))
    await bus.publish(TaskProgress(task_id=1, text="hi"))

    assert received == [TaskStarted(task_id=1), TaskProgress(task_id=1, text="hi")]


async def test_specific_and_wildcard_subscribers_both_fire():
    bus = EventBus()
    specific = []
    wildcard = []
    bus.subscribe(TaskStarted, specific.append)
    bus.subscribe(object, wildcard.append)

    await bus.publish(TaskStarted(task_id=1))

    assert specific == [TaskStarted(task_id=1)]
    assert wildcard == [TaskStarted(task_id=1)]


async def test_exception_in_sync_handler_is_isolated_and_logged(caplog):
    bus = EventBus()
    received = []

    def bad_handler(event):
        raise ValueError("boom")

    bus.subscribe(TaskStarted, bad_handler)
    bus.subscribe(TaskStarted, received.append)

    with caplog.at_level(logging.ERROR, logger="jarvis.events"):
        await bus.publish(TaskStarted(task_id=1))  # must not raise

    assert received == [TaskStarted(task_id=1)]
    assert "boom" in caplog.text


async def test_exception_in_async_handler_is_isolated_and_logged(caplog):
    bus = EventBus()
    received = []

    async def bad_handler(event):
        raise ValueError("boom")

    bus.subscribe(TaskStarted, bad_handler)
    bus.subscribe(TaskStarted, received.append)

    with caplog.at_level(logging.ERROR, logger="jarvis.events"):
        await bus.publish(TaskStarted(task_id=1))

    assert received == [TaskStarted(task_id=1)]
    assert "boom" in caplog.text


async def test_publish_from_within_handler_does_not_deadlock():
    bus = EventBus()
    inner_received = []

    async def outer_handler(event):
        await bus.publish(TaskProgress(task_id=event.task_id, text="inner"))

    bus.subscribe(TaskStarted, outer_handler)
    bus.subscribe(TaskProgress, inner_received.append)

    await asyncio.wait_for(bus.publish(TaskStarted(task_id=1)), timeout=1)

    assert inner_received == [TaskProgress(task_id=1, text="inner")]


async def test_subscribe_during_publish_does_not_affect_in_flight_dispatch():
    bus = EventBus()
    late_received = []

    def subscribe_new_handler(event):
        bus.subscribe(TaskStarted, late_received.append)

    bus.subscribe(TaskStarted, subscribe_new_handler)

    await bus.publish(TaskStarted(task_id=1))  # new subscriber must not see this publish
    assert late_received == []

    await bus.publish(TaskStarted(task_id=2))  # but should see the next one
    assert late_received == [TaskStarted(task_id=2)]


async def test_unsubscribe_during_publish_does_not_break_current_dispatch():
    bus = EventBus()
    received = []

    def unsubscribing_handler(event):
        unsubscribe()
        received.append("first")

    def second_handler(event):
        received.append("second")

    unsubscribe = bus.subscribe(TaskStarted, unsubscribing_handler)
    bus.subscribe(TaskStarted, second_handler)

    await bus.publish(TaskStarted(task_id=1))

    assert received == ["first", "second"]
