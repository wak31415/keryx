"""In-memory websocket doubles used by the realtime provider tests.

`FakeWS` implements the tiny surface the provider needs (`send`, `recv`, `close`) and
lets a test script the server side: `feed` queues a server event, `close_from_server`
makes the pending `recv` raise `ConnectionClosedOK` the way a dropped socket does, and
`sent` records every client message already parsed from JSON.
"""

import asyncio
import json

from websockets.exceptions import ConnectionClosedOK

_CLOSED = object()


class FakeWS:
    """Scripted stand-in for a `websockets` client connection."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._incoming: asyncio.Queue[object] = asyncio.Queue()

    # --- provider-facing surface -------------------------------------------------

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        if item is _CLOSED:
            raise ConnectionClosedOK(None, None)
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(_CLOSED)

    # --- test controls -----------------------------------------------------------

    def feed(self, event: dict) -> None:
        """Queue a server event for the next `recv`."""
        self._incoming.put_nowait(json.dumps(event))

    def feed_raw(self, message: str) -> None:
        """Queue a raw (possibly malformed) text frame for the next `recv`."""
        self._incoming.put_nowait(message)

    def close_from_server(self) -> None:
        """Make the next `recv` raise `ConnectionClosedOK`."""
        self._incoming.put_nowait(_CLOSED)

    def fail_recv(self, exc: Exception) -> None:
        """Make the next `recv` raise `exc` (for the unexpected-exception path)."""
        self._incoming.put_nowait(exc)

    @property
    def sent_types(self) -> list[str]:
        return [message.get("type") for message in self.sent]

    def sent_of_type(self, event_type: str) -> list[dict]:
        return [message for message in self.sent if message.get("type") == event_type]


class FakeConnector:
    """Async websocket factory `(url, headers) -> FakeWS`, recording every call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.sockets: list[FakeWS] = []
        self.fail = fail

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWS:
        self.calls.append((url, dict(headers)))
        if self.fail:
            raise ConnectionRefusedError("cannot reach the realtime API")
        ws = FakeWS()
        self.sockets.append(ws)
        return ws

    @property
    def ws(self) -> FakeWS:
        """The most recently handed out socket."""
        return self.sockets[-1]
