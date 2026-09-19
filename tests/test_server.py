"""Tests for the FastAPI phone server: signature auth, TwiML, stream tokens, media socket.

Nothing here talks to Twilio or OpenAI: signatures are computed locally with Twilio's own
`RequestValidator`, and the media socket runs against a scripted `FakeProvider`.
"""

import base64
import json
import logging
import time
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from jarvis.app import build_app_state
from jarvis.config import Settings
from jarvis.realtime.base import AudioDelta
from jarvis.server import LINE_BUSY_MESSAGE, create_app
from jarvis.session import PIN_PAUSED_MESSAGE
from jarvis.stream_tokens import outbound_extra
from jarvis.trust import TrustLevel

AUTH_TOKEN = "an-auth-token"
CALLER = "+15551234567"
STRANGER = "+15559999999"
CALL_SID = "CA00000000000000000000000000000001"
STREAM_SID = "MZ00000000000000000000000000000001"
TIMEOUT = 5.0


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "jarvis",
        "twilio_auth_token": AUTH_TOKEN,
        "allowed_callers": [CALLER],
        "public_host": "jarvis.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def state(tmp_path):
    return build_app_state(make_settings(tmp_path))


@pytest.fixture
def client(state):
    with TestClient(create_app(state)) as test_client:
        yield test_client


def post_signed(
    client: TestClient,
    path: str,
    params: dict,
    *,
    url: str | None = None,
    token: str = AUTH_TOKEN,
    headers: dict | None = None,
):
    """POST `params` as a form with the `X-Twilio-Signature` Twilio itself would send."""
    signature = RequestValidator(token).compute_signature(url or f"http://testserver{path}", params)
    return client.post(
        path, data=params, headers={"X-Twilio-Signature": signature, **(headers or {})}
    )


def stream_element(response) -> ElementTree.Element:
    """The `<Stream>` inside the TwiML, or an assertion failure."""
    stream = ElementTree.fromstring(response.text).find("./Connect/Stream")
    assert stream is not None, response.text
    return stream


def stream_parameters(response) -> dict[str, str]:
    return {p.get("name"): p.get("value") for p in stream_element(response).findall("Parameter")}


def start_frame(token: str, call_sid: str = CALL_SID) -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": STREAM_SID,
            "start": {
                "streamSid": STREAM_SID,
                "callSid": call_sid,
                "customParameters": {"token": token, "caller": CALLER},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }
    )


def eventually(predicate, *, timeout: float = TIMEOUT) -> None:
    """Poll `predicate` from the test thread until true, or fail after `timeout`."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was still false after the timeout")
        time.sleep(0.01)


# --- POST /twilio/voice ----------------------------------------------------


def test_an_allowed_caller_gets_twiml_that_opens_the_media_stream(client, state):
    response = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert stream_element(response).get("url") == "wss://jarvis.example/twilio/media"
    parameters = stream_parameters(response)
    assert parameters["caller"] == CALLER
    info = state.stream_tokens.redeem(parameters["token"])
    assert info is not None
    assert info.caller == CALLER
    assert info.extra["call_sid"] == CALL_SID


def test_a_bad_signature_is_refused_without_minting_a_token(client, state):
    response = client.post(
        "/twilio/voice",
        data={"From": CALLER, "CallSid": CALL_SID},
        headers={"X-Twilio-Signature": "not-the-signature"},
    )

    assert response.status_code == 403
    assert len(state.stream_tokens) == 0


def test_a_missing_signature_is_refused(client):
    response = client.post("/twilio/voice", data={"From": CALLER, "CallSid": CALL_SID})

    assert response.status_code == 403


def test_the_forwarded_scheme_and_host_are_used_for_the_signature_and_the_stream(tmp_path):
    state = build_app_state(make_settings(tmp_path, public_host=None))
    with TestClient(create_app(state)) as client:
        response = post_signed(
            client,
            "/twilio/voice",
            {"From": CALLER, "CallSid": CALL_SID},
            url="https://jarvis.example/twilio/voice",
            headers={"x-forwarded-proto": "https", "x-forwarded-host": "jarvis.example"},
        )

    assert response.status_code == 200
    assert stream_element(response).get("url") == "wss://jarvis.example/twilio/media"


def test_a_caller_who_is_not_allowed_is_told_the_number_is_private(client, state):
    response = post_signed(client, "/twilio/voice", {"From": STRANGER, "CallSid": CALL_SID})

    assert response.status_code == 200  # Twilio only plays TwiML it got a 200 for
    twiml = ElementTree.fromstring(response.text)
    assert twiml.find("Say") is not None
    assert twiml.find("Hangup") is not None
    assert twiml.find("Connect") is None
    assert len(state.stream_tokens) == 0


def test_a_call_past_the_line_limit_is_told_the_line_is_busy(tmp_path):
    """Calls already answered but not yet streaming count: they are sessions a moment away."""
    state = build_app_state(make_settings(tmp_path, max_phone_sessions=1))
    state.stream_tokens.issue(CALLER, {"call_sid": "CA00000000000000000000000000000002"})
    with TestClient(create_app(state)) as client:
        response = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})

    assert response.status_code == 200
    twiml = ElementTree.fromstring(response.text)
    assert twiml.find("Say").text == LINE_BUSY_MESSAGE
    assert twiml.find("Hangup") is not None
    assert twiml.find("Connect") is None
    assert len(state.stream_tokens) == 1  # nothing new was minted


def test_the_default_line_limit_takes_two_calls(client, state):
    for sid in ("CA00000000000000000000000000000002", "CA00000000000000000000000000000003"):
        response = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": sid})
        assert ElementTree.fromstring(response.text).find("Connect") is not None

    response = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})

    assert ElementTree.fromstring(response.text).find("Connect") is None


def test_an_inbound_call_never_writes_the_caller_number_to_the_log(client, caplog):
    """`~/.jarvis/logs/jarvis.log` and the journal are not a place for a phone number."""
    with caplog.at_level(logging.INFO, logger="jarvis.server"):
        post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})

    assert "answering a call" in caplog.text
    assert CALLER not in caplog.text
    assert CALLER[-4:] in caplog.text  # still enough to tell two callers apart


def test_a_refused_caller_is_not_written_down_in_full_either(client, caplog):
    with caplog.at_level(logging.WARNING, logger="jarvis.server"):
        post_signed(client, "/twilio/voice", {"From": STRANGER, "CallSid": CALL_SID})

    assert "ALLOWED_CALLERS" in caplog.text
    assert STRANGER not in caplog.text


def test_without_an_auth_token_every_request_is_refused(tmp_path):
    state = build_app_state(make_settings(tmp_path, twilio_auth_token=None))
    with TestClient(create_app(state)) as client:
        response = post_signed(
            client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID}, token="guessed"
        )

    assert response.status_code == 403


def test_validation_can_be_skipped_for_local_development(tmp_path):
    settings = make_settings(tmp_path, twilio_auth_token=None, debug_skip_twilio_validation=True)
    state = build_app_state(settings)
    with TestClient(create_app(state)) as client:
        response = client.post("/twilio/voice", data={"From": CALLER, "CallSid": CALL_SID})

    assert response.status_code == 200
    assert stream_element(response).get("url") == "wss://jarvis.example/twilio/media"


def test_every_skipped_signature_check_is_a_warning(tmp_path, caplog):
    settings = make_settings(tmp_path, twilio_auth_token=None, debug_skip_twilio_validation=True)
    state = build_app_state(settings)
    with caplog.at_level(logging.WARNING, logger="jarvis.server"):
        with TestClient(create_app(state)) as client:
            client.post("/twilio/voice", data={"From": CALLER, "CallSid": CALL_SID})

    assert "DEBUG_SKIP_TWILIO_VALIDATION" in caplog.text


# --- POST /twilio/status ---------------------------------------------------


def test_a_status_callback_is_accepted_with_no_content(client):
    response = post_signed(
        client, "/twilio/status", {"CallSid": CALL_SID, "CallStatus": "completed"}
    )

    assert response.status_code == 204
    assert response.content == b""


def test_a_status_callback_with_a_bad_signature_is_refused(client):
    response = client.post(
        "/twilio/status",
        data={"CallSid": CALL_SID, "CallStatus": "completed"},
        headers={"X-Twilio-Signature": "nope"},
    )

    assert response.status_code == 403


# --- what the tunnel does not serve ----------------------------------------


def test_the_openapi_schema_is_not_served(client):
    """The tunnel publishes this whole port; the route schema is not for strangers."""
    assert client.get("/openapi.json").status_code == 404


def test_the_interactive_docs_are_not_served(client):
    for path in ("/docs", "/redoc"):
        assert client.get(path).status_code == 404, path


# --- GET /health -----------------------------------------------------------


def test_health_counts_the_live_sessions(client, state):
    assert client.get("/health").json() == {"ok": True, "live_sessions": 0}

    state.sessions.add(SimpleNamespace(is_live=True))

    assert client.get("/health").json() == {"ok": True, "live_sessions": 1}


# --- WS /twilio/media ------------------------------------------------------


def test_the_media_socket_refuses_an_unknown_token(client):
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame("not-a-real-token"))

        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()

    assert excinfo.value.code == 1008


def test_the_media_socket_refuses_a_token_minted_for_another_call(client, state):
    """The token is single-use, but it must also belong to the call presenting it."""
    state.provider_factory = FakeProvider  # nothing may reach a real provider here
    voice = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})
    token = stream_parameters(voice)["token"]

    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame(token, call_sid="CA00000000000000000000000000000009"))

        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()

    assert excinfo.value.code == 1008


def test_the_media_socket_closes_when_the_stream_never_starts(client):
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))

        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_a_valid_token_runs_a_session_that_speaks_back_to_the_caller(client, state):
    provider = FakeProvider()
    provider.feed(AudioDelta(item_id="item_1", audio=b"\xff" * 160))
    state.provider_factory = lambda: provider
    voice = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})
    token = stream_parameters(voice)["token"]

    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame(token))

        assert json.loads(ws.receive_text()) == {
            "event": "media",
            "streamSid": STREAM_SID,
            "media": {"payload": base64.b64encode(b"\xff" * 160).decode()},
        }
        assert client.get("/health").json()["live_sessions"] == 1

        ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # the session hangs up by closing the socket

    assert provider.config.audio_format == "audio/pcmu"  # no transcoding on the phone path
    eventually(lambda: state.sessions.live() == [])
    eventually(lambda: provider.closed)


def test_a_media_stream_past_the_line_limit_never_opens_a_realtime_session(tmp_path):
    """The webhook cannot see a call-back's stream coming, so the socket counts again."""
    state = build_app_state(make_settings(tmp_path, max_phone_sessions=1))
    providers: list[FakeProvider] = []

    def factory() -> FakeProvider:
        # A second provider fails the session outright, so a broken cap fails this test
        # rather than leaving the extra socket running forever.
        assert not providers, "a realtime session was opened past the line limit"
        providers.append(FakeProvider())
        return providers[-1]

    state.provider_factory = factory
    first = state.stream_tokens.issue(CALLER, {"call_sid": CALL_SID})
    second_sid = "CA00000000000000000000000000000002"
    second = state.stream_tokens.issue(CALLER, {"call_sid": second_sid})

    with TestClient(create_app(state)) as client:
        with client.websocket_connect("/twilio/media") as ws:
            ws.send_text(start_frame(first))
            eventually(lambda: client.get("/health").json()["live_sessions"] == 1)

            with client.websocket_connect("/twilio/media") as extra:
                extra.send_text(start_frame(second, call_sid=second_sid))
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    extra.receive_text()

            assert excinfo.value.code == 1013  # try again later
            assert len(providers) == 1

            ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_a_finished_call_gives_its_line_back(tmp_path):
    state = build_app_state(make_settings(tmp_path, max_phone_sessions=1))
    state.provider_factory = FakeProvider
    with TestClient(create_app(state)) as client:
        for sid in (CALL_SID, "CA00000000000000000000000000000002"):
            token = state.stream_tokens.issue(CALLER, {"call_sid": sid})
            with client.websocket_connect("/twilio/media") as ws:
                ws.send_text(start_frame(token, call_sid=sid))
                eventually(lambda: client.get("/health").json()["live_sessions"] == 1)
                ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    ws.receive_text()
                assert excinfo.value.code != 1013
            eventually(lambda: client.get("/health").json()["live_sessions"] == 0)


def test_a_call_while_pin_entry_is_locked_is_refused_even_the_right_pin(tmp_path):
    """The media route hands every phone session the one guard, so the count spans calls."""
    state = build_app_state(make_settings(tmp_path, pin="424242"))
    for _ in range(state.settings.pin_failure_limit):
        state.pin_guard.record_failure()
    provider = FakeProvider()
    state.provider_factory = lambda: provider
    token = state.stream_tokens.issue(CALLER, {"call_sid": CALL_SID})

    with TestClient(create_app(state)) as client:
        with client.websocket_connect("/twilio/media") as ws:
            ws.send_text(start_frame(token))
            eventually(lambda: client.get("/health").json()["live_sessions"] == 1)
            for digit in "424242":
                ws.send_text(
                    json.dumps({"event": "dtmf", "streamSid": STREAM_SID, "dtmf": {"digit": digit}})
                )
            eventually(lambda: PIN_PAUSED_MESSAGE in [text for text, *_ in provider.injected])

            assert state.sessions.live()[0].authorized is False
            ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_a_call_back_session_knows_the_task_it_was_placed_about(client, state):
    """From the token Jarvis minted, never from the socket's own parameters."""
    provider = FakeProvider()
    provider.feed(AudioDelta(item_id="item_1", audio=b"\x00"))
    state.provider_factory = lambda: provider
    token = state.stream_tokens.issue(CALLER, {"opening_context": "task 3 is done", "task_id": 3})

    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame(token))
        ws.receive_text()

        assert [session.opening_task_id for session in state.sessions.live()] == [3]

        ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_the_session_opens_with_the_context_carried_by_the_token(client, state):
    provider = FakeProvider()
    provider.feed(AudioDelta(item_id="item_1", audio=b"\x00"))
    state.provider_factory = lambda: provider
    token = state.stream_tokens.issue(CALLER, {"opening_context": "task 3 is done"})

    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame(token))
        ws.receive_text()  # the audio delta: the opening injection has happened by now

        assert provider.injected[0][0] == "task 3 is done"

        ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


# --- what a call Jarvis placed opens at ------------------------------------


def live_session(state):
    live = state.sessions.live()
    assert live, "no session opened"
    return live[0]


def run_media_session(client, state, token: str):
    """Open the media socket on `token`, hand back the session, and close it cleanly."""
    provider = FakeProvider()
    provider.feed(AudioDelta(item_id="item_1", audio=b"\x00"))
    state.provider_factory = lambda: provider
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(start_frame(token))
        ws.receive_text()  # the audio delta: the session is open by now
        session = live_session(state)
        trust = session.trust
        ws.send_text(json.dumps({"event": "stop", "streamSid": STREAM_SID}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    return trust


def test_a_call_jarvis_placed_to_the_owner_opens_at_possession(client, state):
    """The token Jarvis minted is the proof, and it is the only thing that may be."""
    token = state.stream_tokens.issue(CALLER, outbound_extra(CALLER, opening_context="hello"))

    assert run_media_session(client, state, token) is TrustLevel.POSSESSION


def test_a_call_jarvis_placed_to_another_number_opens_at_nothing(tmp_path):
    """A call-back may go to a number the owner gave out loud; that is not their phone."""
    state = build_app_state(make_settings(tmp_path, allowed_callers=[CALLER, STRANGER]))
    token = state.stream_tokens.issue(STRANGER, outbound_extra(STRANGER))

    with TestClient(create_app(state)) as client:
        assert run_media_session(client, state, token) is TrustLevel.NONE


def test_an_inbound_call_from_the_owners_own_number_opens_at_nothing(client, state):
    """`From` is the caller's carrier talking, and the whole reason the PIN exists."""
    response = post_signed(client, "/twilio/voice", {"From": CALLER, "CallSid": CALL_SID})
    token = stream_parameters(response)["token"]

    assert run_media_session(client, state, token) is TrustLevel.NONE


def test_a_token_claiming_possession_with_no_owner_number_opens_at_nothing(tmp_path):
    state = build_app_state(make_settings(tmp_path, allowed_callers=[]))
    token = state.stream_tokens.issue(CALLER, outbound_extra(CALLER))

    with TestClient(create_app(state)) as client:
        assert run_media_session(client, state, token) is TrustLevel.NONE
