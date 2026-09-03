"""Tests for the FastAPI phone server: signature auth, TwiML, stream tokens, media socket.

Nothing here talks to Twilio or OpenAI: signatures are computed locally with Twilio's own
`RequestValidator`, and the media socket runs against a scripted `FakeProvider`.
"""

import base64
import json
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
from jarvis.server import create_app

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
