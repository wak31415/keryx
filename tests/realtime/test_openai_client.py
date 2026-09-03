"""Tests for the OpenAI Realtime provider (spec §3.2 interfaces, §4 GA wire schema).

Every test drives a `FakeWS`: no network, no real websockets. Server events come from
JSON fixtures under `tests/fixtures/realtime/` so the payload shapes stay honest.
"""

import asyncio
import base64
import inspect
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.realtime import openai as realtime_openai
from jarvis.realtime.base import (
    AudioDelta,
    Disconnected,
    FunctionCall,
    ProviderError,
    ProviderEvent,
    RealtimeProvider,
    ResponseDone,
    ResponseStarted,
    SessionConfig,
    SpeechStarted,
    SpeechStopped,
    Transcript,
)
from jarvis.realtime.openai import OpenAIRealtimeClient, build_session_update

from .fake_ws import FakeConnector, FakeWS

MODEL = "gpt-realtime-2.1"
API_KEY = "sk-test-key"
_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "realtime"

TOOLS = [
    {
        "type": "function",
        "name": "dispatch_task",
        "description": "Start a background task.",
        "parameters": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "required": ["kind"],
        },
    }
]


def server_event(name: str) -> dict:
    """Load a recorded server event fixture."""
    return json.loads((_FIXTURES / f"{name}.json").read_text())


def rejection(fixture: str, event_id: str) -> dict:
    """An error fixture re-pointed at the client event id the server is rejecting."""
    event = server_event(fixture)
    event["error"]["event_id"] = event_id
    return event


def response_creates(ws: FakeWS) -> list[dict]:
    """Sent `response.create` payloads with the client-generated event_id stripped.

    Each one must carry a unique event_id: that is what the server echoes back in
    `error.event_id`, and the only way to attribute a rejection to the exact request.
    """
    payloads = ws.sent_of_type("response.create")
    event_ids = [payload.get("event_id") for payload in payloads]
    assert all(isinstance(event_id, str) and event_id for event_id in event_ids)
    assert len(set(event_ids)) == len(event_ids)
    return [{k: v for k, v in payload.items() if k != "event_id"} for payload in payloads]


def last_response_create_id(ws: FakeWS) -> str:
    return ws.sent_of_type("response.create")[-1]["event_id"]


def phone_config(**overrides) -> SessionConfig:
    """The phone-path config: µ-law, barge-in enabled, transcription on."""
    params: dict = {
        "instructions": "You are Jarvis.",
        "tools": TOOLS,
        "voice": "marin",
        "audio_format": "audio/pcmu",
    }
    params.update(overrides)
    return SessionConfig(**params)


# --- harness ----------------------------------------------------------------


@dataclass
class Harness:
    client: OpenAIRealtimeClient
    connector: FakeConnector
    events: AsyncIterator[ProviderEvent]

    @property
    def ws(self) -> FakeWS:
        return self.connector.ws

    async def next_event(self) -> ProviderEvent:
        """Next provider event, with a timeout so a bug fails instead of hanging."""
        return await asyncio.wait_for(anext(self.events), 2.0)


@pytest.fixture
async def connect():
    """Factory returning a connected `Harness`; every client is closed on teardown."""
    harnesses: list[Harness] = []

    async def _connect(config: SessionConfig | None = None, **kwargs) -> Harness:
        connector = FakeConnector()
        client = OpenAIRealtimeClient(API_KEY, MODEL, ws_connect=connector, **kwargs)
        await client.connect(config or phone_config())
        harness = Harness(client, connector, client.events())
        harnesses.append(harness)
        return harness

    yield _connect

    for harness in harnesses:
        await harness.client.close()
        await harness.events.aclose()


# --- session.update payload (spec §4) ---------------------------------------


def test_build_session_update_matches_ga_schema_for_the_phone_path():
    config = phone_config(vad_mode="server", vad_silence_ms=500)

    assert build_session_update(config) == {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": "You are Jarvis.",
            "tools": TOOLS,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": "marin"},
            },
        },
    }


def test_build_session_update_for_the_local_path_omits_transcription_and_barge_in():
    config = SessionConfig(
        instructions="Local Jarvis.",
        tools=[],
        voice="cedar",
        audio_format="audio/pcm",
        vad_threshold=0.7,
        vad_silence_ms=700,
        vad_prefix_ms=200,
        interrupt_response=False,
        transcription_model=None,
    )

    assert build_session_update(config) == {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": "Local Jarvis.",
            "tools": [],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "medium",
                        "create_response": True,
                        "interrupt_response": False,
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "cedar"},
            },
        },
    }


def test_build_session_update_has_no_beta_era_fields():
    session = build_session_update(phone_config())["session"]

    assert "modalities" not in session
    assert "temperature" not in session
    assert "input_audio_format" not in session
    assert "output_audio_format" not in session


def test_build_session_update_leaves_the_model_to_the_connection_url():
    assert "model" not in build_session_update(phone_config())["session"]


# --- connect ----------------------------------------------------------------


async def test_connect_opens_the_ga_url_with_bearer_auth_and_no_beta_header(connect):
    harness = await connect()

    url, headers = harness.connector.calls[0]
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    assert headers == {"Authorization": f"Bearer {API_KEY}"}


async def test_connect_sends_session_update_first(connect):
    config = phone_config()
    harness = await connect(config)

    assert harness.ws.sent == [build_session_update(config)]


async def test_send_before_connect_raises():
    client = OpenAIRealtimeClient(API_KEY, MODEL, ws_connect=FakeConnector())

    with pytest.raises(RuntimeError):
        await client.send_audio(b"\x00")


# --- client events ----------------------------------------------------------


async def test_send_audio_base64_encodes_the_frame(connect):
    harness = await connect()
    frame = bytes([0x00, 0xFF, 0x7F, 0x80, 0x21, 0x42])

    await harness.client.send_audio(frame)

    assert harness.ws.sent[-1] == {
        "type": "input_audio_buffer.append",
        "audio": "AP9/gCFC",
    }
    assert base64.b64decode(harness.ws.sent[-1]["audio"]) == frame


async def test_truncate_sends_content_index_zero(connect):
    harness = await connect()

    await harness.client.truncate("item_assistant_1", 1200)

    assert harness.ws.sent[-1] == {
        "type": "conversation.item.truncate",
        "item_id": "item_assistant_1",
        "content_index": 0,
        "audio_end_ms": 1200,
    }


async def test_cancel_response_sends_response_cancel(connect):
    harness = await connect()

    await harness.client.cancel_response()

    assert harness.ws.sent[-1] == {"type": "response.cancel"}


async def test_inject_message_creates_a_system_item_and_requests_a_response(connect):
    harness = await connect()

    await harness.client.inject_message("[system] the task finished")

    assert harness.ws.sent[1] == {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "[system] the task finished"}],
        },
    }
    assert response_creates(harness.ws) == [{"type": "response.create"}]


async def test_inject_message_passes_response_instructions(connect):
    harness = await connect()

    await harness.client.inject_message(
        "[system] connection was reset",
        response_instructions="Briefly apologize and continue.",
    )

    assert response_creates(harness.ws) == [
        {"type": "response.create", "response": {"instructions": "Briefly apologize and continue."}}
    ]


async def test_inject_message_without_respond_sends_no_response_create(connect):
    harness = await connect()

    await harness.client.inject_message("[system] fyi", respond=False)

    assert harness.ws.sent_types == ["session.update", "conversation.item.create"]


async def test_submit_tool_result_json_encodes_a_dict_output_then_requests_a_response(connect):
    harness = await connect()

    await harness.client.submit_tool_result("call_abc", {"task_id": 7, "status": "running"})

    assert harness.ws.sent[1] == {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": json.dumps({"task_id": 7, "status": "running"}),
        },
    }
    assert response_creates(harness.ws) == [{"type": "response.create"}]


async def test_submit_tool_result_passes_a_string_output_through(connect):
    harness = await connect()

    await harness.client.submit_tool_result("call_abc", "already a string")

    assert harness.ws.sent[1]["item"]["output"] == "already a string"


# --- server event translation -----------------------------------------------


async def test_audio_delta_is_decoded_to_bytes(connect):
    harness = await connect()
    harness.ws.feed(server_event("response_output_audio_delta"))

    event = await harness.next_event()

    assert event == AudioDelta(
        item_id="item_assistant_1", audio=bytes([0x00, 0xFF, 0x7F, 0x80, 0x21, 0x42])
    )


async def test_speech_started_and_stopped_are_translated(connect):
    harness = await connect()
    harness.ws.feed(server_event("speech_started"))
    harness.ws.feed(server_event("speech_stopped"))

    assert await harness.next_event() == SpeechStarted(item_id="item_user_1", audio_start_ms=1230)
    assert await harness.next_event() == SpeechStopped()


async def test_response_lifecycle_events_are_translated(connect):
    harness = await connect()
    harness.ws.feed(server_event("response_created"))
    harness.ws.feed(server_event("response_done"))

    assert await harness.next_event() == ResponseStarted(response_id="resp_001")
    assert await harness.next_event() == ResponseDone(response_id="resp_001", status="completed")


async def test_transcripts_carry_the_right_role(connect):
    harness = await connect()
    harness.ws.feed(server_event("response_output_audio_transcript_done"))
    harness.ws.feed(server_event("input_audio_transcription_completed"))

    assert await harness.next_event() == Transcript(
        role="assistant", text="Sure, I'll take care of that.", item_id="item_assistant_1"
    )
    assert await harness.next_event() == Transcript(
        role="user", text="Hey Jarvis, what's on my calendar?", item_id="item_user_1"
    )


async def test_function_call_arguments_are_json_parsed(connect):
    harness = await connect()
    harness.ws.feed(server_event("function_call_arguments_done"))

    assert await harness.next_event() == FunctionCall(
        call_id="call_abc",
        name="dispatch_task",
        arguments={"kind": "research", "description": "Summarize today's news"},
    )


async def test_invalid_function_call_arguments_become_an_empty_dict(connect):
    harness = await connect()
    harness.ws.feed(server_event("function_call_arguments_done_invalid"))

    assert await harness.next_event() == FunctionCall(
        call_id="call_broken", name="dispatch_task", arguments={}
    )


async def test_function_call_is_not_emitted_twice_for_the_same_call(connect):
    """`response.output_item.done` repeats the function_call item; only the args event counts."""
    harness = await connect()
    harness.ws.feed(server_event("function_call_arguments_done"))
    harness.ws.feed(server_event("response_output_item_done_function_call"))
    harness.ws.feed(server_event("speech_stopped"))

    assert isinstance(await harness.next_event(), FunctionCall)
    assert await harness.next_event() == SpeechStopped()


async def test_unknown_and_bookkeeping_events_are_ignored(connect):
    harness = await connect()
    harness.ws.feed(server_event("session_created"))
    harness.ws.feed(server_event("rate_limits_updated"))
    harness.ws.feed({"type": "some.brand.new.event", "payload": 1})
    harness.ws.feed(server_event("speech_stopped"))

    assert await harness.next_event() == SpeechStopped()


async def test_malformed_frames_do_not_kill_the_reader(connect):
    harness = await connect()
    harness.ws.feed_raw("not json at all")
    harness.ws.feed(server_event("speech_stopped"))

    assert await harness.next_event() == SpeechStopped()


# --- errors ------------------------------------------------------------------


async def test_truncate_error_is_reported_but_not_fatal(connect):
    harness = await connect()
    harness.ws.feed(server_event("error_truncate_already_shorter"))

    event = await harness.next_event()

    assert event == ProviderError(
        code="invalid_value",
        message="Audio content of item_assistant_1 is already shorter than 1200ms.",
        fatal=False,
    )


async def test_auth_error_is_fatal(connect):
    harness = await connect()
    harness.ws.feed(server_event("error_invalid_api_key"))

    event = await harness.next_event()

    assert event == ProviderError(
        code="invalid_api_key", message="Incorrect API key provided.", fatal=True
    )


@pytest.mark.parametrize(
    ("code", "fatal"),
    [
        ("invalid_api_key", True),
        ("session_expired", True),
        ("session_not_found", True),
        ("invalid_value", False),
        ("conversation_already_has_active_response", False),
        (None, False),
    ],
)
async def test_error_fatality_by_code(connect, code, fatal):
    harness = await connect()
    event = server_event("error_truncate_already_shorter")
    event["error"]["code"] = code
    harness.ws.feed(event)

    provider_event = await harness.next_event()

    assert isinstance(provider_event, ProviderError)
    assert provider_event.code == code
    assert provider_event.fatal is fatal


# --- response queue (only one active response at a time) ---------------------


async def test_second_request_waits_for_response_done(connect):
    harness = await connect()

    await harness.client.inject_message("first")
    await harness.client.inject_message("second")

    # Optimistically active from the first send: only one response.create on the wire.
    assert response_creates(harness.ws) == [{"type": "response.create"}]

    harness.ws.feed(server_event("response_created"))
    assert await harness.next_event() == ResponseStarted(response_id="resp_001")
    assert len(response_creates(harness.ws)) == 1

    harness.ws.feed(server_event("response_done"))
    assert await harness.next_event() == ResponseDone(response_id="resp_001", status="completed")
    assert len(response_creates(harness.ws)) == 2


async def test_only_one_queued_request_is_drained_per_response_done(connect):
    harness = await connect()

    await harness.client.inject_message("first")
    await harness.client.inject_message("second", response_instructions="be brief")
    await harness.client.inject_message("third")

    assert len(response_creates(harness.ws)) == 1

    harness.ws.feed(server_event("response_done"))
    await harness.next_event()
    assert response_creates(harness.ws)[-1] == {
        "type": "response.create",
        "response": {"instructions": "be brief"},
    }
    assert len(response_creates(harness.ws)) == 2

    harness.ws.feed(server_event("response_done"))
    await harness.next_event()
    assert len(response_creates(harness.ws)) == 3


async def test_tool_result_response_is_queued_while_a_response_is_active(connect):
    harness = await connect()
    harness.ws.feed(server_event("response_created"))
    await harness.next_event()

    await harness.client.submit_tool_result("call_abc", {"ok": True})

    # The item is sent immediately; the response.create waits for response.done.
    assert harness.ws.sent_types[1:] == ["conversation.item.create"]

    harness.ws.feed(server_event("response_done"))
    await harness.next_event()
    assert harness.ws.sent_types[1:] == ["conversation.item.create", "response.create"]


async def test_every_response_create_carries_a_unique_event_id(connect):
    harness = await connect()

    await harness.client.inject_message("first")
    await harness.client.inject_message("second")
    harness.ws.feed(server_event("response_done"))
    await harness.next_event()

    event_ids = [payload["event_id"] for payload in harness.ws.sent_of_type("response.create")]
    assert len(event_ids) == 2
    assert all(isinstance(event_id, str) and event_id for event_id in event_ids)
    assert len(set(event_ids)) == 2


async def test_rejected_tool_result_response_is_resent_after_the_next_response_done(connect):
    """The conflicting `response.created` always reaches us before the rejection."""
    harness = await connect()
    await harness.client.submit_tool_result("call_abc", {"ok": True})
    rejected_id = last_response_create_id(harness.ws)

    # The response the server auto-created from VAD, not ours: it must not be taken as
    # confirmation that our request was accepted.
    harness.ws.feed(server_event("response_created"))
    assert await harness.next_event() == ResponseStarted(response_id="resp_001")

    harness.ws.feed(rejection("error_active_response", rejected_id))
    assert isinstance(await harness.next_event(), ProviderError)
    assert len(response_creates(harness.ws)) == 1

    harness.ws.feed(server_event("response_done"))
    await harness.next_event()
    assert response_creates(harness.ws) == [
        {"type": "response.create"},
        {"type": "response.create"},
    ]


async def test_unrelated_error_while_a_response_create_is_in_flight_changes_nothing(connect):
    harness = await connect()
    await harness.client.inject_message("first")
    await harness.client.inject_message("second")
    harness.ws.feed(server_event("response_created"))
    assert isinstance(await harness.next_event(), ResponseStarted)

    # A truncate error for some other client event: no spurious extra response.create.
    harness.ws.feed(server_event("error_truncate_already_shorter"))
    assert isinstance(await harness.next_event(), ProviderError)
    assert len(response_creates(harness.ws)) == 1

    harness.ws.feed(server_event("response_done"))
    await harness.next_event()
    assert len(response_creates(harness.ws)) == 2


async def test_other_error_on_our_own_response_create_unblocks_the_queue(connect):
    harness = await connect()
    await harness.client.inject_message("first")
    await harness.client.inject_message("second")
    rejected_id = last_response_create_id(harness.ws)
    assert len(response_creates(harness.ws)) == 1

    harness.ws.feed(rejection("error_server_error", rejected_id))
    assert isinstance(await harness.next_event(), ProviderError)

    # The failed response.create is dropped and the queued one goes out immediately.
    assert len(response_creates(harness.ws)) == 2


# --- disconnect / reconnect / close ------------------------------------------


async def test_socket_close_yields_disconnected(connect):
    harness = await connect()
    harness.ws.close_from_server()

    event = await harness.next_event()

    assert isinstance(event, Disconnected)
    assert event.reason


async def test_reader_exception_yields_disconnected_instead_of_dying_silently(connect):
    harness = await connect()
    harness.ws.fail_recv(RuntimeError("socket exploded"))

    event = await harness.next_event()

    assert isinstance(event, Disconnected)
    assert "socket exploded" in event.reason


async def test_close_emits_disconnected_then_ends_the_iterator(connect):
    harness = await connect()

    await harness.client.close()

    assert await harness.next_event() == Disconnected("closed")
    with pytest.raises(StopAsyncIteration):
        await harness.next_event()
    assert harness.ws.closed


async def test_reconnect_reopens_the_socket_and_resends_session_update(connect):
    config = phone_config()
    harness = await connect(config)
    harness.ws.close_from_server()
    assert isinstance(await harness.next_event(), Disconnected)

    assert await harness.client.reconnect() is True

    assert len(harness.connector.sockets) == 2
    assert harness.connector.calls[1] == harness.connector.calls[0]
    assert harness.ws.sent == [build_session_update(config)]


async def test_the_same_events_iterator_survives_a_reconnect(connect):
    harness = await connect()
    harness.ws.close_from_server()
    assert isinstance(await harness.next_event(), Disconnected)
    await harness.client.reconnect()

    harness.ws.feed(server_event("speech_stopped"))

    assert await harness.next_event() == SpeechStopped()


async def test_reconnect_clears_the_response_queue_and_active_flag(connect):
    harness = await connect()
    await harness.client.inject_message("first")
    await harness.client.inject_message("second")
    harness.ws.close_from_server()
    assert isinstance(await harness.next_event(), Disconnected)

    await harness.client.reconnect()
    await harness.client.inject_message("after reconnect")

    # Nothing carried over: exactly one response.create on the fresh socket.
    assert response_creates(harness.ws) == [{"type": "response.create"}]


async def test_failed_reconnect_returns_false_and_ends_the_iterator(connect):
    harness = await connect()
    harness.connector.fail = True
    harness.ws.close_from_server()
    assert isinstance(await harness.next_event(), Disconnected)

    assert await harness.client.reconnect() is False

    with pytest.raises(StopAsyncIteration):
        await harness.next_event()


async def test_events_can_only_be_consumed_once(connect):
    harness = await connect()

    with pytest.raises(RuntimeError):
        harness.client.events()


# --- default websocket factory / protocol conformance ------------------------


async def test_default_ws_connect_uses_the_documented_websockets_options(monkeypatch):
    captured: dict = {}

    class FakeConnection:
        async def send(self, message):
            captured["sent"] = message

        async def recv(self):
            return b'{"type": "session.created"}'  # websockets may hand back bytes

        async def close(self):
            captured["closed"] = True

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(realtime_openai.websockets, "connect", fake_connect)

    ws = await realtime_openai._default_ws_connect(
        "wss://api.openai.com/v1/realtime?model=x", {"Authorization": "Bearer k"}
    )

    assert captured["url"] == "wss://api.openai.com/v1/realtime?model=x"
    assert captured["kwargs"] == {
        "additional_headers": {"Authorization": "Bearer k"},
        "max_size": None,
        "ping_interval": realtime_openai.WS_PING_INTERVAL_S,
        "ping_timeout": realtime_openai.WS_PING_TIMEOUT_S,
        "open_timeout": realtime_openai.WS_OPEN_TIMEOUT_S,
        "close_timeout": realtime_openai.WS_CLOSE_TIMEOUT_S,
    }
    # Named rather than inherited: every one of these is on the path of a caller already
    # on the line, and `websockets` has no open timeout by default at all — a connect
    # against a black-holed route would hang forever with the caller hearing silence.
    for option in ("ping_interval", "ping_timeout", "open_timeout", "close_timeout"):
        assert isinstance(captured["kwargs"][option], float), option
    assert await ws.recv() == '{"type": "session.created"}'
    await ws.send("ping")
    assert captured["sent"] == "ping"
    await ws.close()
    assert captured["closed"] is True


def test_client_matches_the_realtime_provider_protocol():
    client = OpenAIRealtimeClient(API_KEY, MODEL)
    method_names = [name for name in vars(RealtimeProvider) if not name.startswith("_")]

    assert set(method_names) == {
        "connect",
        "close",
        "events",
        "send_audio",
        "submit_tool_result",
        "inject_message",
        "truncate",
        "cancel_response",
        "reconnect",
    }
    for name in method_names:
        expected = list(inspect.signature(getattr(RealtimeProvider, name)).parameters)[1:]
        actual = list(inspect.signature(getattr(client, name)).parameters)
        assert actual == expected, f"{name} does not match the protocol signature"


# --- audio format ------------------------------------------------------------


def test_a_pcm_session_declares_its_sample_rate():
    """The GA API refuses an `audio/pcm` session that does not name a rate."""
    update = build_session_update(
        SessionConfig(instructions="hi", tools=[], voice="cedar", audio_format="audio/pcm")
    )

    audio = update["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}


def test_a_pcmu_session_carries_no_rate():
    """G.711 is 8 kHz by definition, and the API rejects a `rate` alongside it."""
    update = build_session_update(
        SessionConfig(instructions="hi", tools=[], voice="cedar", audio_format="audio/pcmu")
    )

    audio = update["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcmu"}
    assert audio["output"]["format"] == {"type": "audio/pcmu"}


# --- turn detection ----------------------------------------------------------


def test_the_default_turn_detection_waits_for_a_finished_sentence():
    """Semantic detection is the default: a pause for thought must not end the turn."""
    update = build_session_update(phone_config())

    assert update["session"]["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "medium",
        "create_response": True,
        "interrupt_response": True,
    }


def test_semantic_turn_detection_carries_no_silence_timer():
    """The API refuses `silence_duration_ms` alongside semantic_vad."""
    update = build_session_update(phone_config(vad_silence_ms=4000))

    assert "silence_duration_ms" not in update["session"]["audio"]["input"]["turn_detection"]


def test_the_silence_timer_is_used_in_server_mode():
    update = build_session_update(phone_config(vad_mode="server", vad_silence_ms=4000))

    detection = update["session"]["audio"]["input"]["turn_detection"]
    assert detection["type"] == "server_vad"
    assert detection["silence_duration_ms"] == 4000


def test_noise_reduction_reaches_the_input_block_when_it_is_set():
    config = SessionConfig(
        instructions="Jarvis.",
        tools=[],
        voice="cedar",
        audio_format="audio/pcmu",
        noise_reduction="near_field",
    )

    audio_input = build_session_update(config)["session"]["audio"]["input"]

    assert audio_input["noise_reduction"] == {"type": "near_field"}


def test_noise_reduction_left_off_is_absent_rather_than_null():
    """The API validates the value, so "off" has to be the missing field, not a null one."""
    config = SessionConfig(
        instructions="Jarvis.", tools=[], voice="cedar", audio_format="audio/pcmu"
    )

    assert "noise_reduction" not in build_session_update(config)["session"]["audio"]["input"]
