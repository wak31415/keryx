"""Socket Mode, against a scripted socket: nothing here opens a connection."""

import asyncio
import json
import logging

import pytest

from keryx.integrations import slack_socket
from keryx.integrations.slack import Connection
from keryx.integrations.slack_socket import (
    BACKOFF_MAX_S,
    WIRE_LOGGER,
    SocketModeListener,
)

URL = "wss://wss.slack.example/link/?ticket=very-secret-ticket"


def frame(**fields) -> str:
    return json.dumps(fields)


def event(envelope_id: str, text: str) -> str:
    return frame(type="events_api", envelope_id=envelope_id,
                 payload={"type": "event_callback", "event": {"text": text}})


class FakeSocket:
    """A socket that says what it was scripted to, then drops."""

    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self):
        await asyncio.sleep(0)  # as a real one would: the event loop gets a turn
        if not self.frames:
            raise ConnectionError(f"connection to {URL} closed")
        item = self.frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True


class World:
    """Slack as the listener meets it: one scripted socket per connection, in order."""

    def __init__(self, *scripts, opened=None, sleeps=1) -> None:
        self.sockets = [FakeSocket(frames) for frames in scripts]
        self.opened = list(opened or [])
        self.connects: list[str] = []
        self.opens: list[str] = []
        self.delays: list[float] = []
        self.handled: list[dict] = []
        self.now = 0.0
        #: How long each connection lasts, by the listener's own clock.
        self.lasts = 0.0
        self._sleeps = sleeps

    def open(self, token: str) -> Connection:
        self.opens.append(token)
        return self.opened.pop(0) if self.opened else Connection(url=URL)

    async def connect(self, url: str):
        self.connects.append(url)
        if isinstance(self.sockets[0], BaseException):
            raise self.sockets.pop(0)
        return self.sockets.pop(0)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        for _ in range(5):
            await asyncio.sleep(0)  # let what is waiting be acted on
        if len(self.delays) >= self._sleeps:
            raise asyncio.CancelledError

    def clock(self) -> float:
        self.now += self.lasts / 2  # read twice a connection: before it and after
        return self.now

    async def handle(self, payload: dict) -> None:
        self.handled.append(payload)

    def listener(self, handle=None) -> SocketModeListener:
        return SocketModeListener(
            "work", "xapp-secret", handle or self.handle, open_connection=self.open,
            connect=self.connect, sleep=self.sleep, clock=self.clock,
        )

    async def run(self, handle=None) -> None:
        with pytest.raises(asyncio.CancelledError):
            await self.listener(handle).run()


async def test_every_envelope_is_acknowledged_and_an_events_payload_is_handed_on_in_order():
    world = World([
        frame(type="hello", num_connections=1),
        event("e1", "first"),
        frame(type="interactive", envelope_id="e2", payload={"type": "block_actions"}),
        event("e3", "second"),
    ])
    socket = world.sockets[0]

    await world.run()

    assert socket.sent == [{"envelope_id": "e1"}, {"envelope_id": "e2"}, {"envelope_id": "e3"}]
    assert [payload["event"]["text"] for payload in world.handled] == ["first", "second"]
    assert world.opens == ["xapp-secret"] and world.connects == [URL]
    assert socket.closed


async def test_what_is_not_an_envelope_is_passed_over():
    world = World([
        "not json", b"\xff\xfe", "[1, 2]", frame(type="events_api", payload={"type": "x"}),
        frame(type="events_api", envelope_id="e1", payload="not an object"),
        frame(type="events_api", envelope_id="", payload={"n": 1}),
        event("e2", "kept"),
    ])
    socket = world.sockets[0]

    await world.run()

    # One with no id cannot be acknowledged; one with no payload has nothing to hand on.
    assert socket.sent == [{"envelope_id": "e1"}, {"envelope_id": "e2"}]
    assert [payload.get("event", payload) for payload in world.handled] == [
        {"type": "x"}, {"n": 1}, {"text": "kept"},
    ]


async def test_a_disconnect_is_answered_with_a_new_connection(caplog):
    world = World(
        [frame(type="hello"), frame(type="disconnect", reason="refresh_requested"),
         event("never", "after the disconnect")],
        [frame(type="hello"), event("e1", "on the second")],
        sleeps=2,
    )
    first = world.sockets[0]

    with caplog.at_level(logging.INFO, logger="keryx.slack.socket"):
        await world.run()

    assert world.opens == ["xapp-secret", "xapp-secret"] and len(world.connects) == 2
    assert first.closed and first.sent == []
    assert [payload["event"]["text"] for payload in world.handled] == ["on the second"]
    assert "renewing the connection for work (refresh_requested)" in caplog.text


async def test_a_disconnects_reason_is_only_repeated_when_it_is_a_word(caplog):
    world = World([frame(type="disconnect", reason="see wss://x/?ticket=other-secret")])

    with caplog.at_level(logging.INFO, logger="keryx.slack.socket"):
        await world.run()

    assert "other-secret" not in caplog.text and "no reason given" in caplog.text


async def test_connections_that_keep_failing_are_tried_less_and_less_often_and_never_never():
    refused = [Connection(error="invalid_auth")] * 12
    world = World(opened=refused, sleeps=12)

    await world.run()

    assert world.connects == []
    assert world.delays == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300, 300]
    assert max(world.delays) == BACKOFF_MAX_S


async def test_a_connection_that_had_settled_is_renewed_at_once():
    world = World([frame(type="hello")], [frame(type="hello")], sleeps=3,
                  opened=[Connection(error="http_503")])
    world.lasts = 0.0
    listener = world.listener()

    async def sleep(delay):
        # The first try failed fast; the connections after it each lasted two hours.
        world.lasts = 7200.0
        await world.sleep(delay)

    listener._sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert world.delays == [2, 1, 1]


async def test_the_address_is_never_logged_whatever_goes_wrong(caplog):
    world = World(sleeps=3)
    world.sockets = [
        OSError(f"cannot connect to {URL}"),
        FakeSocket([ValueError(f"bad frame from {URL}")]),
        FakeSocket([frame(type="hello")]),
    ]

    with caplog.at_level(logging.DEBUG):
        await world.run()

    assert "very-secret-ticket" not in caplog.text and "wss://" not in caplog.text
    assert "the Slack connection for work ended (OSError)" in caplog.text
    assert "the Slack connection for work ended (ValueError)" in caplog.text
    assert "the Slack connection for work ended (ConnectionError)" in caplog.text


async def test_a_token_slack_refuses_is_said_by_its_code_and_nothing_is_connected(caplog):
    world = World(opened=[Connection(error="invalid_auth")])

    await world.run()

    assert world.connects == []
    assert "could not open a Slack connection for work" in caplog.text
    assert "(invalid_auth)" in caplog.text and "xapp-secret" not in caplog.text


async def test_an_event_that_cannot_be_acted_on_does_not_stop_the_next(caplog):
    world = World([event("e1", "breaks"), event("e2", "works")])
    handled = []

    async def handle(payload):
        if payload["event"]["text"] == "breaks":
            raise RuntimeError("no")
        handled.append(payload["event"]["text"])

    await world.run(handle)

    assert handled == ["works"]
    assert "could not act on a Slack event for work" in caplog.text


async def test_events_wait_their_turn_and_past_a_limit_one_is_dropped(monkeypatch, caplog):
    monkeypatch.setattr(slack_socket, "MAX_WAITING", 2)
    world = World([event(f"e{n}", str(n)) for n in range(4)])
    gate = asyncio.Event()
    handled = []

    async def handle(payload):
        await gate.wait()
        handled.append(payload["event"]["text"])

    async def until_handled(delay):
        while len(handled) < 3:
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    socket = world.sockets[0]
    listener = world.listener(handle)
    listener._sleep = until_handled
    running = asyncio.create_task(listener.run())
    for _ in range(40):
        await asyncio.sleep(0)

    # Every one was acknowledged all the same: Slack must not send them again.
    assert [sent["envelope_id"] for sent in socket.sent] == ["e0", "e1", "e2", "e3"]
    assert "too many Slack events are waiting for work; one was dropped" in caplog.text
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    # The first was already being acted on, two waited, and the fourth found no room.
    assert handled == ["0", "1", "2"]


async def test_stopping_the_listener_stops_what_was_acting_on_events():
    world = World([frame(type="hello")], sleeps=99)
    listener = world.listener()

    async def forever(delay):
        await asyncio.Event().wait()

    listener._sleep = forever
    running = asyncio.create_task(listener.run())
    for _ in range(10):
        await asyncio.sleep(0)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "slack-events-work"]


async def test_stopping_it_mid_connection_closes_the_socket():
    waiting = asyncio.Event()

    class Quiet(FakeSocket):
        async def recv(self):
            waiting.set()
            await asyncio.Event().wait()

    world = World()
    world.sockets = [Quiet([])]
    socket = world.sockets[0]
    running = asyncio.create_task(world.listener().run())
    await waiting.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert socket.closed and world.delays == []


async def test_the_real_connection_keeps_the_librarys_own_log_off_the_address(monkeypatch):
    seen = {}

    async def fake_connect(url, **options):
        seen.update(url=url, **options)
        return "the connection"

    monkeypatch.setattr(slack_socket.websockets, "connect", fake_connect)
    logging.getLogger(WIRE_LOGGER).setLevel(logging.DEBUG)

    assert await slack_socket.connect(URL) == "the connection"
    assert seen["url"] == URL
    # At DEBUG the library writes the request line, which is the ticket.
    assert seen["logger"].name == WIRE_LOGGER
    assert seen["logger"].getEffectiveLevel() == logging.WARNING
    assert seen["open_timeout"] and seen["ping_interval"]
