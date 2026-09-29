"""OpenAI Realtime (GA) provider: websocket client + event translation.

Wire details that matter and are easy to get wrong:
`wss://api.openai.com/v1/realtime?model=…` with `Authorization: Bearer …` and **no**
`OpenAI-Beta` header; the session is configured with a single `session.update` whose
`audio.input` / `audio.output` blocks carry the format, VAD and voice (no beta-era
`modalities` / `temperature` / `*_audio_format` fields).
"""

import asyncio
import base64
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Protocol, cast
from urllib.parse import urlencode
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed

from jarvis.realtime.base import (
    AudioDelta,
    Disconnected,
    FunctionCall,
    ProviderError,
    ProviderEvent,
    ResponseDone,
    ResponseStarted,
    SessionConfig,
    SpeechStarted,
    SpeechStopped,
    Transcript,
)

logger = logging.getLogger("jarvis.realtime.openai")

REALTIME_URL = "wss://api.openai.com/v1/realtime"

#: Sample rate declared for `audio/pcm` sessions — the local mic/speaker path
#: (16-bit LE mono 24 kHz).
PCM_SAMPLE_RATE = 24000

# How long each part of this socket may take, said out loud rather than inherited. All
# four are on the path of a caller who is *already on the line* and hearing silence, so
# the library's defaults (no open timeout at all; a 10-second close) are the wrong shape:
# what matters is failing fast enough for `reconnect()` to get a word in.
#: Opening the socket. Without it a connect against a black-holed route hangs forever, and
#: the caller hears nothing while `VoiceSession.run` waits on `connect`.
WS_OPEN_TIMEOUT_S = 10.0
#: Keepalive ping cadence, and how long a pong may take before the socket is considered
#: dead. A dead socket surfacing as `Disconnected` is what arms the reconnect; a socket
#: that never notices is a call that stays silent.
WS_PING_INTERVAL_S = 20.0
WS_PING_TIMEOUT_S = 20.0
#: Waiting for the closing handshake on the way out. Short: by the time we are closing,
#: the session is over and nobody is listening.
WS_CLOSE_TIMEOUT_S = 5.0

# Errors that mean this socket/session is unusable; everything else is transient
# (e.g. truncating past the end of an item, which happens routinely on barge-in).
FATAL_ERROR_CODES = frozenset({"invalid_api_key", "session_expired", "session_not_found"})

# The server rejects a second `response.create` with this code; ours stays queued.
ACTIVE_RESPONSE_ERROR_CODE = "conversation_already_has_active_response"

# Prefix of the client-generated `event_id` we stamp on every `response.create`, so the
# `error` the server echoes it back on can be attributed to the exact request.
RESPONSE_EVENT_ID_PREFIX = "jarvis_resp"

_SENTINEL = object()


class RealtimeWebSocket(Protocol):
    """The websocket surface this client uses (so tests can pass a fake)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


WebSocketFactory = Callable[[str, dict[str, str]], Awaitable[RealtimeWebSocket]]


class _WebSocketsAdapter:
    """Adapts a `websockets` client connection to `RealtimeWebSocket`."""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def send(self, message: str) -> None:
        await self._connection.send(message)

    async def recv(self) -> str:
        data = await self._connection.recv()
        return data if isinstance(data, str) else data.decode("utf-8")

    async def close(self) -> None:
        await self._connection.close()


async def _default_ws_connect(url: str, headers: dict[str, str]) -> RealtimeWebSocket:
    connection = await websockets.connect(
        url,
        additional_headers=headers,
        max_size=None,
        ping_interval=WS_PING_INTERVAL_S,
        ping_timeout=WS_PING_TIMEOUT_S,
        open_timeout=WS_OPEN_TIMEOUT_S,
        close_timeout=WS_CLOSE_TIMEOUT_S,
    )
    return _WebSocketsAdapter(connection)


def audio_format_block(audio_format: str) -> dict:
    """The `format` block for one side of the session.

    `audio/pcm` has no rate of its own, so the API demands one and refuses the session
    without it; `audio/pcmu` is 8 kHz by definition and refuses a `rate` field instead
    (both verified against the GA API, 2026-08-24).
    """
    if audio_format == "audio/pcm":
        return {"type": audio_format, "rate": PCM_SAMPLE_RATE}
    return {"type": audio_format}


def turn_detection_block(config: SessionConfig) -> dict:
    """How the server decides they have stopped talking.

    Semantic detection waits on the *shape of the sentence*, so a pause for thought does
    not end the turn; it takes an `eagerness` instead of a silence timer, and the API
    rejects `silence_duration_ms` alongside it (verified 2026-08-24).
    """
    if config.vad_mode == "semantic":
        return {
            "type": "semantic_vad",
            "eagerness": config.vad_eagerness,
            "create_response": True,
            "interrupt_response": config.interrupt_response,
        }
    return {
        "type": "server_vad",
        "threshold": config.vad_threshold,
        "prefix_padding_ms": config.vad_prefix_ms,
        "silence_duration_ms": config.vad_silence_ms,
        "create_response": True,
        "interrupt_response": config.interrupt_response,
    }


def build_session_update(config: SessionConfig) -> dict:
    """Build the `session.update` client event for `config`.

    The model is selected by the connection URL, so it is deliberately absent here.
    """
    audio_input: dict = {
        "format": audio_format_block(config.audio_format),
        "turn_detection": turn_detection_block(config),
    }
    if config.noise_reduction is not None:
        # Omitted rather than sent as null when it is off: the API validates this value
        # (`Supported values are: 'near_field' and 'far_field'`, verified 2026-08-26), and
        # an absent field is how it is turned off.
        audio_input["noise_reduction"] = {"type": config.noise_reduction}
    if config.transcription_model is not None:
        transcription = {"model": config.transcription_model}
        if config.transcription_language:
            transcription["language"] = config.transcription_language
        audio_input["transcription"] = transcription

    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": config.instructions,
            "tools": config.tools,
            "tool_choice": "auto",
            "audio": {
                "input": audio_input,
                "output": {
                    "format": audio_format_block(config.audio_format),
                    "voice": config.voice,
                },
            },
        },
    }


# --- server event -> provider event ------------------------------------------


def _audio_delta(event: dict) -> AudioDelta:
    return AudioDelta(item_id=event.get("item_id", ""), audio=base64.b64decode(event["delta"]))


def _speech_started(event: dict) -> SpeechStarted:
    return SpeechStarted(
        item_id=event.get("item_id"), audio_start_ms=int(event.get("audio_start_ms", 0))
    )


def _response_started(event: dict) -> ResponseStarted:
    return ResponseStarted(response_id=event.get("response", {}).get("id", ""))


def _response_done(event: dict) -> ResponseDone:
    response = event.get("response", {})
    return ResponseDone(response_id=response.get("id", ""), status=response.get("status", ""))


def _function_call(event: dict) -> FunctionCall:
    raw = event.get("arguments") or "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("invalid JSON arguments for tool %s; using {}", event.get("name"))
        arguments = {}
    if not isinstance(arguments, dict):
        logger.warning("non-object arguments for tool %s; using {}", event.get("name"))
        arguments = {}
    return FunctionCall(
        call_id=event.get("call_id", ""), name=event.get("name", ""), arguments=arguments
    )


def _assistant_transcript(event: dict) -> Transcript:
    return Transcript(
        role="assistant", text=event.get("transcript", ""), item_id=event.get("item_id")
    )


def _user_transcript(event: dict) -> Transcript:
    return Transcript(role="user", text=event.get("transcript", ""), item_id=event.get("item_id"))


def _provider_error(event: dict) -> ProviderError:
    error = event.get("error") or {}
    code = error.get("code")
    return ProviderError(
        code=code, message=error.get("message", ""), fatal=code in FATAL_ERROR_CODES
    )


# `response.output_item.done` also carries completed function_call items; it is
# deliberately absent so a tool call is emitted exactly once, from the arguments event.
TRANSLATORS: dict[str, Callable[[dict], ProviderEvent]] = {
    "response.output_audio.delta": _audio_delta,
    "input_audio_buffer.speech_started": _speech_started,
    "input_audio_buffer.speech_stopped": lambda event: SpeechStopped(),
    "response.created": _response_started,
    "response.done": _response_done,
    "response.function_call_arguments.done": _function_call,
    "response.output_audio_transcript.done": _assistant_transcript,
    "conversation.item.input_audio_transcription.completed": _user_transcript,
    "error": _provider_error,
}


class OpenAIRealtimeClient:
    """`RealtimeProvider` over the OpenAI Realtime GA websocket API.

    Lifecycle contract the voice session relies on:

    - `events()` may be taken **once** per client and keeps yielding across reconnects.
    - When the socket drops, the reader enqueues `Disconnected(reason)` and stops, but
      the iterator stays open. After a `Disconnected`, call `reconnect()`: if it returns
      True keep iterating on the same iterator; if it returns False the iterator ends.
    - `close()` enqueues `Disconnected("closed")` and then ends the iterator.

    Only one response may be active at a time. `_request_response` sends a
    `response.create` when none is active and marks the session active optimistically
    (before `response.created` arrives, so two quick injections cannot both fire);
    otherwise it queues the request and sends it when `response.done` arrives.

    Every `response.create` carries a client-generated `event_id`, which the server echoes
    in `error.event_id` when it rejects the request. That is the only reliable way to tell
    an error about *our* request from an unrelated one: `response.created` carries no client
    id, and the one that arrives after our send is usually the conflicting response the
    server auto-created from VAD, not ours.
    """

    def __init__(
        self, api_key: str, model: str, *, ws_connect: WebSocketFactory | None = None
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._ws_connect = ws_connect or _default_ws_connect
        self._config: SessionConfig | None = None
        self._ws: RealtimeWebSocket | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._events_taken = False
        self._closed = False
        self._active_response = False
        self._inflight_responses: dict[str, dict] = {}
        self._pending_responses: deque[dict] = deque()

    # --- connection ----------------------------------------------------------

    async def connect(self, config: SessionConfig) -> None:
        """Open the socket, send `session.update`, and start reading server events."""
        self._config = config
        await self._open()

    async def _open(self) -> None:
        config = self._config
        if config is None:
            raise RuntimeError("connect() must be called before opening the socket")
        url = f"{REALTIME_URL}?{urlencode({'model': self._model})}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        self._ws = await self._ws_connect(url, headers)
        await self._send(build_session_update(config))
        self._reader = asyncio.create_task(self._read_loop(self._ws))

    async def reconnect(self) -> bool:
        """One attempt to re-open the socket and re-send the session config."""
        if self._closed or self._config is None:
            return False

        await self._stop_reader()
        await self._close_ws()
        self._reset_response_state()

        try:
            await self._open()
        except Exception as exc:  # any failure here just means "no reconnect"
            logger.warning("realtime reconnect failed: %s", exc)
            await self._queue.put(_SENTINEL)
            return False
        logger.info("realtime session reconnected")
        return True

    async def close(self) -> None:
        """Stop the reader, close the socket, and end `events()`."""
        if self._closed:
            return
        self._closed = True
        await self._stop_reader()
        await self._close_ws()
        self._reset_response_state()
        await self._queue.put(Disconnected("closed"))
        await self._queue.put(_SENTINEL)

    async def _stop_reader(self) -> None:
        task, self._reader = self._reader, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()

    def _reset_response_state(self) -> None:
        self._active_response = False
        self._inflight_responses.clear()
        self._pending_responses.clear()

    # --- reading -------------------------------------------------------------

    def events(self) -> AsyncIterator[ProviderEvent]:
        """Typed provider events. May only be taken once per client; see the class docstring."""
        if self._events_taken:
            raise RuntimeError("events() may only be consumed once per client")
        self._events_taken = True
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[ProviderEvent]:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            yield cast(ProviderEvent, item)

    async def _read_loop(self, ws: RealtimeWebSocket) -> None:
        """Read server events until the socket drops; never exits without a reason."""
        reason = "reader stopped"
        try:
            while True:
                raw = await ws.recv()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("ignoring non-JSON frame from the realtime API")
                    continue
                await self._handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            reason = f"websocket closed: {exc}"
            logger.info("realtime socket closed: %s", exc)
        except Exception as exc:  # the reader must never die silently
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("realtime reader stopped: %s", reason, exc_info=True)
        await self._queue.put(Disconnected(reason))

    async def _handle_server_event(self, event: dict) -> None:
        event_type = event.get("type", "")

        if event_type == "response.created":
            # May be the response the server auto-created from VAD rather than ours, so it
            # says nothing about the fate of a `response.create` still awaiting an error.
            self._active_response = True
        elif event_type == "response.done":
            await self._on_response_finished()
        elif event_type == "error":
            await self._on_error(event)

        translate = TRANSLATORS.get(event_type)
        if translate is None:
            logger.debug("ignoring realtime server event: %s", event_type)
            return
        try:
            provider_event = translate(event)
        except Exception:  # one malformed event must not end the session
            logger.warning("could not translate %s event", event_type, exc_info=True)
            return
        await self._queue.put(provider_event)

    # --- response queue ------------------------------------------------------

    async def _request_response(self, instructions: str | None = None) -> None:
        payload: dict = {"type": "response.create"}
        if instructions is not None:
            payload["response"] = {"instructions": instructions}

        if self._active_response:
            self._pending_responses.append(payload)
            logger.debug(
                "response active; queued response.create (%d pending)",
                len(self._pending_responses),
            )
            return
        await self._send_response_create(payload)

    async def _send_response_create(self, payload: dict) -> None:
        # Optimistic: mark active before awaiting the send so a second request that
        # arrives in between is queued rather than sent. The stored payload is the bare
        # one, so a retry after a rejection goes out with a fresh event_id.
        event_id = f"{RESPONSE_EVENT_ID_PREFIX}_{uuid4().hex[:12]}"
        self._active_response = True
        self._inflight_responses[event_id] = payload
        await self._send({**payload, "event_id": event_id})

    async def _on_response_finished(self) -> None:
        """A response ended: clear the active flag and send the next queued request.

        Any `response.create` still awaiting a verdict was accepted: the server emits the
        rejection while processing our event, i.e. before the response it conflicted with
        can finish, so nothing that reaches this point can still be rejected.
        """
        self._inflight_responses.clear()
        await self._drain_pending()

    async def _drain_pending(self) -> None:
        self._active_response = False
        if self._pending_responses:
            await self._send_response_create(self._pending_responses.popleft())

    async def _on_error(self, event: dict) -> None:
        """Keep the response queue honest when one of our `response.create`s is rejected.

        Attribution is by the `event_id` we stamped on the request and the server echoed
        back; errors about anything else leave the queue alone. If the server says a
        response is already active, ours goes back to the front of the queue and is re-sent
        on the next `response.done`; any other rejection means our request is gone, so the
        active flag is dropped and the queue drains.
        """
        error = event.get("error") or {}
        event_id = error.get("event_id")
        if event_id is None:
            return
        payload = self._inflight_responses.pop(event_id, None)
        if payload is None:
            return

        if error.get("code") == ACTIVE_RESPONSE_ERROR_CODE:
            self._pending_responses.appendleft(payload)
            logger.debug("response.create rejected as conflicting; requeued for the next done")
            return
        logger.warning("dropping rejected response.create: %s", error.get("message", ""))
        await self._drain_pending()

    # --- sending -------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("realtime provider is not connected")
        await self._ws.send(json.dumps(payload))

    async def send_audio(self, data: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(data).decode("ascii"),
            }
        )

    async def submit_tool_result(
        self, call_id: str, output: dict | str, *, respond: bool = True
    ) -> None:
        """Hand a tool's output back, and by default ask for the turn that speaks about it.

        `respond=False` is for a tool whose result has nothing to say (see
        `ToolRegistry.is_silent`): the output still has to reach the conversation, or the
        model's next turn sees a function call with no answer, but nothing is generated
        over it.
        """
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output if isinstance(output, str) else json.dumps(output),
                },
            }
        )
        if respond:
            await self._request_response()

    async def inject_message(
        self,
        text: str,
        *,
        respond: bool = True,
        response_instructions: str | None = None,
    ) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if respond:
            await self._request_response(response_instructions)

    async def update_instructions(self, instructions: str) -> None:
        """Send a `session.update` carrying only the new instructions, and keep them.

        Only the instructions, never the whole config: the API refuses to change the voice
        once the model has produced audio, and a `session.update` that tries is rejected
        whole — the new instructions with it. Partial updates leave the other fields as
        they are.
        """
        if self._config is None:
            raise RuntimeError("connect() must be called before updating the instructions")
        self._config = replace(self._config, instructions=instructions)
        await self._send(
            {
                "type": "session.update",
                "session": {"type": "realtime", "instructions": instructions},
            }
        )

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})
