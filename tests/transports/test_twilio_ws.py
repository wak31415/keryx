"""Tests for the Twilio media-stream transport: a fake socket, never a real one."""

import asyncio
import base64
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from jarvis.transports.base import AudioIn, Dtmf, Hangup
from jarvis.transports.twilio_ws import TransportError, TwilioTransport

STREAM_SID = "MZ0123456789abcdef"
CALL_SID = "CA0123456789abcdef"
TIMEOUT = 2.0


def start_message(**custom: str) -> dict:
    """The `start` frame Twilio sends once the media stream is up (spec §4)."""
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": STREAM_SID,
        "start": {
            "streamSid": STREAM_SID,
            "accountSid": "AC0123456789abcdef",
            "callSid": CALL_SID,
            "tracks": ["inbound"],
            "customParameters": custom or {"token": "tok-1", "caller": "+15551234567"},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


def media_message(payload: bytes, timestamp: str = "120") -> dict:
    return {
        "event": "media",
        "streamSid": STREAM_SID,
        "media": {
            "track": "inbound",
            "chunk": "2",
            "timestamp": timestamp,
            "payload": base64.b64encode(payload).decode(),
        },
    }


class FakeWebSocket:
    """The three coroutines `TwilioTransport` needs, backed by a scripted inbox."""

    def __init__(self, *messages: dict | str | Exception) -> None:
        self._inbox: asyncio.Queue[dict | str | Exception] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closes: list[int] = []
        for message in messages:
            self.feed(message)

    # --- scripting ---------------------------------------------------------

    def feed(self, message: dict | str | Exception) -> None:
        """Queue one inbound frame (a dict is JSON-encoded) or an error to raise."""
        self._inbox.put_nowait(message)

    def disconnect(self) -> None:
        """Make the next `receive_text()` raise, the way a dropped socket does."""
        self.feed(WebSocketDisconnect(code=1000))

    @property
    def closed(self) -> bool:
        return bool(self.closes)

    # --- the websocket surface ---------------------------------------------

    async def receive_text(self) -> str:
        message = await self._inbox.get()
        if isinstance(message, Exception):
            raise message
        return message if isinstance(message, str) else json.dumps(message)

    async def send_text(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("socket is closed")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closes.append(code)


async def started(ws: FakeWebSocket) -> TwilioTransport:
    """A transport that has consumed its `start` frame."""
    transport = TwilioTransport(ws)
    await asyncio.wait_for(transport.start(), TIMEOUT)
    return transport


async def collect(transport: TwilioTransport) -> list:
    """Drain `events()` to completion, bounded so a stuck test fails instead of hanging."""

    async def drain() -> list:
        return [event async for event in transport.events()]

    return await asyncio.wait_for(drain(), TIMEOUT)


# --- start -----------------------------------------------------------------


async def test_start_parses_the_start_frame_and_ignores_connected():
    ws = FakeWebSocket({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    ws.feed(start_message(token="tok-1", caller="+15551234567"))
    transport = TwilioTransport(ws)

    info = await asyncio.wait_for(transport.start(), TIMEOUT)

    assert info.stream_sid == STREAM_SID
    assert info.call_sid == CALL_SID
    assert info.custom_parameters == {"token": "tok-1", "caller": "+15551234567"}
    assert info.media_format["encoding"] == "audio/x-mulaw"
    assert transport.caller == "+15551234567"
    assert transport.channel == "phone"
    assert transport.audio_format == "audio/pcmu"


async def test_start_skips_undecodable_frames():
    ws = FakeWebSocket("not json at all", start_message())

    transport = await started(ws)

    assert transport.stream_sid == STREAM_SID


async def test_start_raises_when_the_stream_stops_first():
    ws = FakeWebSocket({"event": "stop", "streamSid": STREAM_SID})

    with pytest.raises(TransportError):
        await asyncio.wait_for(TwilioTransport(ws).start(), TIMEOUT)


async def test_start_raises_when_the_socket_drops_first():
    ws = FakeWebSocket()
    ws.disconnect()

    with pytest.raises(TransportError):
        await asyncio.wait_for(TwilioTransport(ws).start(), TIMEOUT)


async def test_start_raises_when_no_start_frame_arrives():
    ws = FakeWebSocket()  # silent socket

    with pytest.raises(TransportError):
        await asyncio.wait_for(TwilioTransport(ws).start(timeout=0.05), TIMEOUT)


# --- inbound events --------------------------------------------------------


async def test_media_frames_become_audio_in_with_the_wire_timestamp():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    ws.feed(media_message(b"\xff\xfe", timestamp="440"))
    ws.feed({"event": "stop", "streamSid": STREAM_SID})

    events = await collect(transport)

    assert events == [AudioIn(b"\xff\xfe", timestamp_ms=440), Hangup("stop")]


async def test_dtmf_frames_become_dtmf_events():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    ws.feed({"event": "dtmf", "streamSid": STREAM_SID, "dtmf": {"track": "inbound", "digit": "7"}})
    ws.feed({"event": "stop", "streamSid": STREAM_SID})

    events = await collect(transport)

    assert events == [Dtmf("7"), Hangup("stop")]


async def test_marks_and_broken_frames_are_ignored():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    ws.feed({"event": "mark", "streamSid": STREAM_SID, "mark": {"name": "goodbye"}})
    ws.feed("{ not json")
    ws.feed({"event": "media", "streamSid": STREAM_SID, "media": {"timestamp": "1"}})  # no payload
    ws.feed({"event": "stop", "streamSid": STREAM_SID})

    events = await collect(transport)

    assert events == [Hangup("stop")]


async def test_a_dropped_socket_ends_the_stream_with_a_hangup():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    ws.feed(media_message(b"\x01"))
    ws.disconnect()

    events = await collect(transport)

    assert events == [AudioIn(b"\x01", timestamp_ms=120), Hangup("disconnect")]


async def test_an_unexpected_error_also_ends_the_stream():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    ws.feed(RuntimeError("socket exploded"))

    events = await collect(transport)

    assert events == [Hangup("disconnect")]


# --- outbound --------------------------------------------------------------


async def test_send_audio_writes_a_media_frame():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)

    await transport.send_audio(b"\x01\x02\x03")

    assert ws.sent == [
        {
            "event": "media",
            "streamSid": STREAM_SID,
            "media": {"payload": base64.b64encode(b"\x01\x02\x03").decode()},
        }
    ]


async def test_clear_and_mark_write_their_frames():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)

    await transport.clear()
    await transport.send_mark("goodbye")

    assert ws.sent == [
        {"event": "clear", "streamSid": STREAM_SID},
        {"event": "mark", "streamSid": STREAM_SID, "mark": {"name": "goodbye"}},
    ]


async def test_hangup_closes_the_socket_once():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)

    await transport.hangup()
    await transport.hangup()

    assert ws.closes == [1000]


async def test_hangup_can_close_with_a_policy_code():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)

    await transport.hangup(1008)

    assert ws.closes == [1008]


async def test_sending_after_the_socket_closed_is_swallowed():
    ws = FakeWebSocket(start_message())
    transport = await started(ws)
    await transport.hangup()

    await transport.send_audio(b"\x01")  # must not raise

    assert ws.sent == []
