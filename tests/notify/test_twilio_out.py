"""Tests for the outbound Twilio wrapper (spec §4).

No REST client is ever built here: every test either injects a fake one or asserts on
the credentials the lazy constructor would have been handed.
"""

from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from jarvis.config import Settings
from jarvis.notify.twilio_out import TwilioOut, stream_twiml

SID = "AC00000000000000000000000000000001"
AUTH_TOKEN = "an-auth-token"
NUMBER = "+15550000000"
TO = "+15551234567"


class FakeResource:
    """`client.messages` / `client.calls`: records kwargs, returns an object with a sid."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.created: list[dict] = []
        self.error: Exception | None = None

    def create(self, **kwargs) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        self.created.append(kwargs)
        return SimpleNamespace(sid=self.sid)


class FakeClient:
    """The slice of `twilio.rest.Client` `TwilioOut` uses."""

    def __init__(self) -> None:
        self.messages = FakeResource("SM1")
        self.calls = FakeResource("CA1")


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "jarvis",
        "twilio_account_sid": SID,
        "twilio_auth_token": AUTH_TOKEN,
        "twilio_number": NUMBER,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def out(tmp_path, client) -> TwilioOut:
    return TwilioOut(make_settings(tmp_path), client=client)


# --- configuration ---------------------------------------------------------


def test_twilio_is_configured_when_the_sid_token_and_number_are_all_set(out):
    assert out.configured is True


@pytest.mark.parametrize("missing", ["twilio_account_sid", "twilio_auth_token", "twilio_number"])
def test_twilio_is_not_configured_when_a_credential_is_missing(tmp_path, missing):
    out = TwilioOut(make_settings(tmp_path, **{missing: None}), client=FakeClient())

    assert out.configured is False


async def test_the_rest_client_is_built_lazily_from_the_credentials_and_reused(
    tmp_path, monkeypatch
):
    built: list[tuple] = []

    def fake_client(sid, auth_token):
        built.append((sid, auth_token))
        return FakeClient()

    monkeypatch.setattr("jarvis.notify.twilio_out.Client", fake_client)
    out = TwilioOut(make_settings(tmp_path))

    assert built == []  # nothing is constructed until something is actually sent

    await out.send_sms(TO, "one")
    await out.send_sms(TO, "two")

    assert built == [(SID, AUTH_TOKEN)]


# --- SMS -------------------------------------------------------------------


async def test_send_sms_texts_from_the_configured_number_and_returns_the_sid(out, client):
    sid = await out.send_sms(TO, "task 3 finished")

    assert sid == "SM1"
    assert client.messages.created == [{"from_": NUMBER, "to": TO, "body": "task 3 finished"}]


async def test_a_twilio_error_reaches_the_caller(out, client):
    client.messages.error = RuntimeError("twilio said no")

    with pytest.raises(RuntimeError):
        await out.send_sms(TO, "never sent")


# --- outbound calls --------------------------------------------------------


async def test_place_call_dials_out_with_the_twiml_and_returns_the_sid(out, client):
    sid = await out.place_call(TO, twiml="<Response/>")

    assert sid == "CA1"
    assert client.calls.created == [{"to": TO, "from_": NUMBER, "twiml": "<Response/>"}]


async def test_place_call_only_sends_a_status_callback_when_one_is_given(out, client):
    await out.place_call(TO, twiml="<Response/>", status_callback="https://h/twilio/status")

    assert client.calls.created[0]["status_callback"] == "https://h/twilio/status"


# --- TwiML -----------------------------------------------------------------


def test_stream_twiml_connects_the_media_socket_over_wss_with_every_parameter():
    xml = stream_twiml("jarvis.example", {"token": "t0k", "caller": TO, "task_id": "3"})

    stream = ElementTree.fromstring(xml).find("./Connect/Stream")
    assert stream is not None, xml
    assert stream.get("url") == "wss://jarvis.example/twilio/media"
    parameters = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert parameters == {"token": "t0k", "caller": TO, "task_id": "3"}


def test_stream_twiml_is_a_string_ready_for_the_calls_api():
    xml = stream_twiml("h", {})

    assert isinstance(xml, str)
    assert '<Stream url="wss://h/twilio/media"' in xml


# --- SMS_ENABLED -----------------------------------------------------------


def test_texting_is_off_by_default(tmp_path):
    """This account has no SMS geo-permission for the owner's region, and he does not
    want the channel: written messages go to Slack, which he has to ask for."""
    out = TwilioOut(make_settings(tmp_path))

    assert out.configured is True
    assert out.can_text is False


def test_texting_on_needs_credentials_as_well(tmp_path):
    out = TwilioOut(make_settings(tmp_path, sms_enabled=True, twilio_number=None))

    assert out.can_text is False


def test_texting_can_be_turned_back_on(tmp_path):
    out = TwilioOut(make_settings(tmp_path, sms_enabled=True))

    assert out.can_text is True


def test_calling_is_unaffected_by_texting_being_off(tmp_path):
    """The restart watchdog's alert is a call, and it is the last thing still working
    when Jarvis itself is down."""
    out = TwilioOut(make_settings(tmp_path))

    assert out.can_text is False
    assert out.configured is True  # which is what `place_call` is gated on
