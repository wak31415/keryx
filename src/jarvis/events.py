"""In-process event bus and the event dataclasses shared across Jarvis."""

import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("jarvis.events")


@dataclass
class TaskStarted:
    task_id: int


@dataclass
class TaskProgress:
    task_id: int
    text: str


@dataclass
class TaskCompleted:
    task_id: int
    summary: str


@dataclass
class TaskFailed:
    task_id: int
    error: str


@dataclass
class SessionStarted:
    session_id: str
    channel: str
    caller: str | None


@dataclass
class SessionEnded:
    session_id: str
    channel: str
    caller: str | None
    reason: str
    #: Whether the caller was authorized when the call ended: a local session always is, a
    #: phone call only once the PIN was accepted. False unless the publisher says otherwise,
    #: because what reads it (the memory writer) must keep nothing from a caller who
    #: proved nothing.
    authorized: bool = False


@dataclass
class PinLockedOut:
    """Wrong PINs across calls have locked PIN entry, and the owner has not been told.

    Published by the session whose wrong PIN set the lock (`jarvis.pin_guard`); `until` is in
    seconds since the epoch, and `caller` is only what that call's caller ID claimed.
    """

    session_id: str
    caller: str | None
    until: float
    failures: int


# A handler may be a plain sync callable or an async callable; both take the event
# and return None (async handlers return an awaitable that resolves to None).
Handler = Callable[[object], None] | Callable[[object], Awaitable[None]]


class EventBus:
    """Minimal in-process pub/sub.

    Dispatch is by exact `type(event)` match, plus every handler subscribed to the
    wildcard type `object`, which receives every published event (a test seam for
    asserting on everything a component published). `publish` is safe to call from
    within a handler: it takes a snapshot of the subscriber list before iterating, so
    handlers that subscribe, unsubscribe, or publish further events during dispatch
    never mutate the list a publish is currently iterating, and there is no lock that
    could deadlock on re-entrant calls.
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> Callable[[], None]:
        """Register `handler` for `event_type`. Returns a callable that unsubscribes it."""
        self._subscribers[event_type].append(handler)

        def unsubscribe() -> None:
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass  # already unsubscribed; idempotent

        return unsubscribe

    async def publish(self, event: object) -> None:
        """Dispatch `event` to its exact-type subscribers and to wildcard (`object`) subscribers.

        Async handlers are awaited sequentially. Any exception raised by a handler (sync or
        async) is logged and swallowed so one broken handler never stops the others or
        propagates out of `publish`.
        """
        event_type = type(event)
        handlers = list(self._subscribers.get(event_type, ()))
        if event_type is not object:
            handlers += list(self._subscribers.get(object, ()))

        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "event handler %r raised while handling %s", handler, event_type.__name__
                )
