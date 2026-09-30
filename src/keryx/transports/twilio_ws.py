"""A Twilio Media Stream as a transport.

Twilio dials in over one websocket per call and speaks JSON: `connected`, then `start`
(the only frame carrying the stream/call SIDs and the `<Parameter>`s from the TwiML),
then `media` frames of base64 µ-law, plus `dtmf`, `mark` acks and a final `stop`. We
answer with `media`, `clear` and `mark` frames on the same socket.

The audio is passed straight through in both directions: `audio/pcmu` is what Twilio
sends and what the realtime model is configured to speak, so nothing here transcodes.

The socket is only typed as `WebSocketLike`, the three coroutines this needs — Starlette's
`WebSocket` satisfies it and the tests hand in a scripted fake, so no test opens a port.
"""

import asyncio
import base64
import json
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from starlette.websockets import WebSocketDisconnect

from keryx.audio.util import AudioFormat
from keryx.transports.base import DRAIN_TIMEOUT_SECONDS, AudioIn, Dtmf, Hangup, TransportEvent

log = logging.getLogger("keryx.transports.twilio_ws")

# How long `start()` waits for Twilio's `start` frame before giving up. Short on purpose:
# until that frame and its token arrive the socket is unauthenticated, anyone can open one,
# and Twilio itself sends `start` straight after `connected`.
START_TIMEOUT_SECONDS = 5.0


class WebSocketLike(Protocol):
    """The slice of a websocket this transport uses."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class TransportError(RuntimeError):
    """The media stream ended (or never began) before its `start` frame arrived."""


@dataclass
class StartInfo:
    """What Twilio's `start` frame tells us about the call."""

    stream_sid: str
    call_sid: str
    custom_parameters: dict[str, str] = field(default_factory=dict)
    media_format: dict = field(default_factory=dict)


class TwilioTransport:
    """One phone call's audio pipe over a Twilio media-stream websocket."""

    channel: Literal["phone", "local"] = "phone"
    audio_format: AudioFormat = "audio/pcmu"

    def __init__(self, ws: WebSocketLike) -> None:
        self._ws = ws
        self.caller: str | None = None
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self._closed = False
        self._stopped = False  # Twilio said `stop`, or the socket dropped: no playback left

    def __repr__(self) -> str:
        return f"<TwilioTransport {self.stream_sid or 'unstarted'}>"

    # --- start -------------------------------------------------------------

    async def start(self, timeout: float = START_TIMEOUT_SECONDS) -> StartInfo:
        """Consume frames until `start` arrives; raise `TransportError` if none does.

        `connected`, undecodable frames and anything else Twilio sends first are skipped.
        A `stop`, a dropped socket or `timeout` seconds of silence mean there will never
        be a call to run, which is a failure to start rather than a hangup.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TransportError("timed out waiting for the Twilio start frame")
            try:
                raw = await asyncio.wait_for(self._ws.receive_text(), remaining)
            except TimeoutError as exc:
                raise TransportError("timed out waiting for the Twilio start frame") from exc
            except Exception as exc:
                raise TransportError(f"the media stream closed before it started: {exc!r}") from exc

            message = _decode(raw)
            if message is None:
                continue
            event = message.get("event")
            if event == "start":
                return self._on_start(message)
            if event == "stop":
                raise TransportError("the media stream stopped before it started")
            log.debug("ignoring a %r frame while waiting for start", event)

    def _on_start(self, message: dict) -> StartInfo:
        start = message.get("start") or {}
        custom = dict(start.get("customParameters") or {})
        info = StartInfo(
            stream_sid=start.get("streamSid") or message.get("streamSid") or "",
            call_sid=start.get("callSid") or "",
            custom_parameters=custom,
            media_format=dict(start.get("mediaFormat") or {}),
        )
        self.stream_sid = info.stream_sid
        self.call_sid = info.call_sid
        self.caller = custom.get("caller")
        log.info("media stream %s started for call %s", info.stream_sid, info.call_sid)
        return info

    # --- inbound -----------------------------------------------------------

    def events(self) -> AsyncIterator[TransportEvent]:
        """Caller audio, keypad digits and the hangup, until the socket ends."""
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[TransportEvent]:
        while True:
            try:
                raw = await self._ws.receive_text()
            except WebSocketDisconnect as exc:
                log.info("media stream %s disconnected (%s)", self.stream_sid, exc.code)
                self._stopped = True
                yield Hangup("disconnect")
                return
            except Exception:
                log.warning("media stream %s read failed", self.stream_sid, exc_info=True)
                self._stopped = True
                yield Hangup("disconnect")
                return

            message = _decode(raw)
            if message is None:
                continue
            event = message.get("event")
            if event == "media":
                audio = _audio_in(message)
                if audio is not None:
                    yield audio
            elif event == "dtmf":
                digit = (message.get("dtmf") or {}).get("digit")
                if digit:
                    yield Dtmf(str(digit))
            elif event == "stop":
                log.info("media stream %s stopped", self.stream_sid)
                self._stopped = True
                yield Hangup("stop")
                return
            elif event == "mark":
                log.debug("mark ack: %s", (message.get("mark") or {}).get("name"))
            else:
                log.debug("ignoring a %r frame", event)

    # --- outbound ----------------------------------------------------------

    async def send_audio(self, data: bytes) -> None:
        """Play µ-law audio to the caller."""
        await self._send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(data).decode("ascii")},
            }
        )

    async def clear(self) -> None:
        """Drop whatever Twilio still has buffered for playback (barge-in)."""
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def send_mark(self, name: str) -> bool:
        """Ask Twilio to ack when the audio queued so far has actually played."""
        return await self._send(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
        )

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> bool:
        """Wait until Twilio has played what we queued; False if it never says so.

        Twilio buffers outbound media and plays it back in real time, while the model
        produces that audio far faster, so closing the socket at teardown would cut the
        goodbye off mid-word. A `mark` is the only thing that says otherwise: Twilio acks
        it once everything queued before it has actually been played. Reading the socket
        here is safe because the session's pumps are stopped by the time teardown drains.
        A call that has already ended has nothing left to play, so it returns at once
        rather than waiting for an ack that can no longer come.
        """
        name = f"drain-{secrets.token_hex(4)}"
        if self._closed or self._stopped or not await self.send_mark(name):
            return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.info("media stream %s did not ack the drain mark", self.stream_sid)
                return False
            try:
                raw = await asyncio.wait_for(self._ws.receive_text(), remaining)
            except Exception:
                return False  # a stopped or dropped socket has nothing left to play
            message = _decode(raw)
            if message is None:
                continue
            if message.get("event") == "stop":
                return False
            if (message.get("mark") or {}).get("name") == name:
                return True

    async def hangup(self, code: int = 1000) -> None:
        """Close the websocket, which ends the call. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close(code)
        except Exception:
            log.debug("media stream %s was already closed", self.stream_sid, exc_info=True)

    async def _send(self, message: dict) -> bool:
        """Write one frame; a socket that has gone away is a debug line, not an error.

        Losing a race with a hangup is normal on a phone call, and the session must not
        die because the last chunk of audio had nowhere to go. False means nothing was
        written, which is what `drain()` needs in order not to wait for an ack.
        """
        if self._closed:
            log.debug("dropped a %r frame: the socket is closed", message.get("event"))
            return False
        try:
            await self._ws.send_text(json.dumps(message))
        except Exception:
            log.debug("could not send a %r frame", message.get("event"), exc_info=True)
            return False
        return True


def _decode(raw: str) -> dict | None:
    """Parse one frame; malformed JSON is logged and skipped rather than fatal."""
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("skipping an undecodable media-stream frame")
        return None
    if not isinstance(message, dict):
        log.warning("skipping a media-stream frame that is not an object")
        return None
    return message


def _audio_in(message: dict) -> AudioIn | None:
    """Turn a `media` frame into `AudioIn`, or None if it carries no usable audio."""
    media = message.get("media") or {}
    payload = media.get("payload")
    if not payload:
        return None
    try:
        data = base64.b64decode(payload)
    except (TypeError, ValueError):
        log.warning("skipping a media frame with an undecodable payload")
        return None
    try:
        timestamp_ms: int | None = int(media["timestamp"])
    except (KeyError, TypeError, ValueError):
        timestamp_ms = None
    return AudioIn(data, timestamp_ms=timestamp_ms)
