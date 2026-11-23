"""Socket Mode: hearing the owner's Slack app with no address of Keryx's own.

An app at the `converse` level and above is sent its events over a WebSocket that Keryx
opens outwards, so nothing of Slack's ever needs a public route here (`cloudflared` puts
the whole of the phone server's port on the internet; this adds nothing to it). The
protocol is small enough to hold in one file rather than take a dependency for:

1. `apps.connections.open`, with the app-level token, answers with a `wss://` address.
   **That address is a secret**: it carries a ticket that is the whole of its
   authentication. It is never logged and never in an error, so a failure here is
   reported by its kind alone (a websocket library's exception can quote the address),
   and the library's own logger for these connections is held at WARNING, because at
   DEBUG it writes the request line.
2. Slack says `hello`, then sends envelopes. Every envelope that carries an
   `envelope_id` is acknowledged with it at once, whatever it holds, so Slack does not
   send it again; what an `events_api` envelope carries is then handed on, one at a time
   and in the order it arrived.
3. Slack says `disconnect` when it wants the connection renewed (every few hours), and
   the connection may simply drop. Either way a new address is asked for and connected
   to, after a wait that doubles while connections keep failing and is bounded.

`SocketModeListener.run` never returns and never raises anything but cancellation: a
listener that fails is a listener that tries again later, and never something that takes
`keryx serve` down or reaches a call. The socket is a `Protocol` with a fake in the
tests; nothing in the suite opens one.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import websockets

from keryx.integrations.slack import Connection, open_connection

log = logging.getLogger("keryx.slack.socket")
#: What the websocket library logs these connections to. Held at WARNING whatever
#: `LOG_LEVEL` says: its DEBUG lines are the request line, which is the ticket.
WIRE_LOGGER = "keryx.slack.socket.wire"

#: The wait before the first reconnect, and after a connection that had settled.
BACKOFF_FIRST_S = 1.0
#: The most it ever waits: connections that keep failing are tried every five minutes.
BACKOFF_MAX_S = 300.0
#: A connection that lasted this long had settled; one that ended sooner counts as failing.
SETTLED_S = 60.0
OPEN_TIMEOUT_S = 20.0
CLOSE_TIMEOUT_S = 5.0
PING_INTERVAL_S = 30.0
PING_TIMEOUT_S = 30.0
#: How many events may wait to be acted on before a new one is dropped, unacted.
MAX_WAITING = 100

_REASON = re.compile(r"[a-z_]{1,32}")


class Socket(Protocol):
    """One WebSocket connection, as much of it as Socket Mode uses."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[Socket]]
Handler = Callable[[dict], Awaitable[None]]


async def connect(url: str) -> Socket:
    """Open the connection at `url`, which is never logged by anything on the way."""
    wire = logging.getLogger(WIRE_LOGGER)
    wire.setLevel(logging.WARNING)
    return await websockets.connect(
        url,
        logger=wire,
        open_timeout=OPEN_TIMEOUT_S,
        close_timeout=CLOSE_TIMEOUT_S,
        ping_interval=PING_INTERVAL_S,
        ping_timeout=PING_TIMEOUT_S,
    )


class SocketModeListener:
    """Keeps one Socket Mode connection up for one app, and hands its events to `handle`.

    `name` is the workspace's, for the log. `handle` is given each event's payload (the
    `event_callback` object), one at a time; whatever it raises is logged and forgotten.
    """

    def __init__(
        self,
        name: str,
        app_token: str,
        handle: Handler,
        *,
        open_connection: Callable[[str], Connection] = open_connection,
        connect: Connect = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._app_token = app_token
        self._handle = handle
        self._open = open_connection
        self._connect = connect
        self._sleep = sleep
        self._clock = clock
        self._waiting: asyncio.Queue[dict] = asyncio.Queue(MAX_WAITING)

    async def run(self) -> None:
        """Listen until cancelled, reconnecting as often as it takes. Never raises else."""
        worker = asyncio.create_task(self._work(), name=f"slack-events-{self._name}")
        delay = BACKOFF_FIRST_S
        try:
            while True:
                started = self._clock()
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Its kind and nothing it says: the message may quote the address.
                    log.warning("the Slack connection for %s ended (%s)", self._name,
                                type(exc).__name__)
                settled = self._clock() - started >= SETTLED_S
                delay = BACKOFF_FIRST_S if settled else min(delay * 2, BACKOFF_MAX_S)
                await self._sleep(delay)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _session(self) -> None:
        """One connection, from asking for its address to its end."""
        opened = await asyncio.to_thread(self._open, self._app_token)
        if not opened.ok:
            log.warning("could not open a Slack connection for %s: %s", self._name,
                        opened.problem)
            return
        socket = await self._connect(opened.url)
        try:
            while True:
                envelope = _envelope(await socket.recv())
                kind = envelope.get("type")
                if kind == "hello":
                    log.info("listening to Slack for %s", self._name)
                    continue
                if kind == "disconnect":
                    reason = envelope.get("reason")
                    log.info("Slack is renewing the connection for %s (%s)", self._name,
                             reason if isinstance(reason, str) and _REASON.fullmatch(reason)
                             else "no reason given")
                    return
                envelope_id = envelope.get("envelope_id")
                if isinstance(envelope_id, str) and envelope_id:
                    # At once, and whatever it carries: an unacknowledged one is sent again.
                    await socket.send(json.dumps({"envelope_id": envelope_id}))
                payload = envelope.get("payload")
                if kind == "events_api" and isinstance(payload, dict):
                    self._queue(payload)
        finally:
            with contextlib.suppress(Exception):
                await socket.close()

    def _queue(self, payload: dict) -> None:
        try:
            self._waiting.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("too many Slack events are waiting for %s; one was dropped", self._name)

    async def _work(self) -> None:
        """Act on the events in the order they arrived, one at a time."""
        while True:
            payload = await self._waiting.get()
            try:
                await self._handle(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("could not act on a Slack event for %s", self._name)


def _envelope(raw: str | bytes) -> dict:
    """What Slack sent, as an object; anything that is not one is an empty one."""
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
