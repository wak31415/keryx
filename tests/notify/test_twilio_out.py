"""Tests for the outbound Twilio wrapper.

No REST client is ever built here: every test either injects a fake one or asserts on
the credentials the lazy constructor would have been handed.
"""

import logging
import traceback
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from keryx.config import Settings
from keryx.logging_util import mask_number
from keryx.notify.twilio_out import TwilioError, TwilioOut, stream_twiml

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
        "data_dir": tmp_path / "keryx",
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

    monkeypatch.setattr("keryx.notify.twilio_out.Client", fake_client)
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
    xml = stream_twiml("keryx.example", {"token": "t0k", "caller": TO, "task_id": "3"})

    stream = ElementTree.fromstring(xml).find("./Connect/Stream")
    assert stream is not None, xml
    assert stream.get("url") == "wss://keryx.example/twilio/media"
    parameters = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert parameters == {"token": "t0k", "caller": TO, "task_id": "3"}


def test_stream_twiml_is_a_string_ready_for_the_calls_api():
    xml = stream_twiml("h", {})

    assert isinstance(xml, str)
    assert '<Stream url="wss://h/twilio/media"' in xml


# --- SMS_ENABLED -----------------------------------------------------------


def test_texting_is_off_by_default(tmp_path):
    """Off by default: many accounts lack SMS permission for their region, and written
    messages go to Slack, which has to be asked for."""
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
    when Keryx itself is down."""
    out = TwilioOut(make_settings(tmp_path))

    assert out.can_text is False
    assert out.configured is True  # which is what `place_call` is gated on


# --- what reaches a log ----------------------------------------------------


async def test_a_text_is_logged_with_the_number_masked(out, caplog):
    """`logging_util.mask_number` is the only shape a phone number may take in a log."""
    with caplog.at_level(logging.INFO, logger="keryx.notify.twilio_out"):
        await out.send_sms(TO, "task 3 finished")

    assert "texted" in caplog.text
    assert TO not in caplog.text
    assert mask_number(TO) in caplog.text


async def test_a_call_is_logged_with_the_number_masked(out, caplog):
    with caplog.at_level(logging.INFO, logger="keryx.notify.twilio_out"):
        await out.place_call(TO, twiml="<Response/>")

    assert "calling" in caplog.text
    assert TO not in caplog.text
    assert mask_number(TO) in caplog.text


@pytest.mark.parametrize("resource", ["messages", "calls"])
async def test_a_twilio_error_does_not_quote_the_number_back(out, client, resource):
    """Twilio's own error text names the number, and every caller logs it with a traceback."""
    getattr(client, resource).error = RuntimeError(
        f"HTTP 400 error: The 'To' number {TO} is not a valid phone number, from {NUMBER}"
    )

    with pytest.raises(TwilioError) as excinfo:
        if resource == "messages":
            await out.send_sms(TO, "never sent")
        else:
            await out.place_call(TO, twiml="<Response/>")

    written = "".join(traceback.format_exception(excinfo.value))
    assert "not a valid phone number" in written
    assert TO not in written
    assert NUMBER not in written
    assert mask_number(TO) in written


# --- the account, for setup and doctor --------------------------------------------------


class FakeAccountClient:
    """The slice of the REST client `RestTwilioAdmin` reaches, recording every update."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.updates: list[tuple[str, dict]] = []
        client = self

        class Numbers:
            def list(self, limit):
                if client.fail:
                    raise RuntimeError("HTTP 401 for +15550001111")
                return [
                    SimpleNamespace(
                        sid="PN1", phone_number="+15550001111", voice_url="", status_callback=None
                    )
                ]

            def __call__(self, sid):
                return SimpleNamespace(update=lambda **kw: client.updates.append((sid, kw)))

        self.incoming_phone_numbers = Numbers()
        self.api = SimpleNamespace(
            accounts=lambda sid: SimpleNamespace(
                fetch=lambda: SimpleNamespace(friendly_name="Ada's account")
            )
        )


def test_the_admin_reads_the_account_and_its_numbers():
    from keryx.notify.twilio_out import RestTwilioAdmin, TwilioNumber

    admin = RestTwilioAdmin("AC1", "tok", client=FakeAccountClient())

    assert admin.account_name() == "Ada's account"
    assert admin.numbers() == [TwilioNumber("PN1", "+15550001111", None, None)]


def test_the_admin_sets_both_webhooks_as_post():
    from keryx.notify.twilio_out import RestTwilioAdmin

    client = FakeAccountClient()
    RestTwilioAdmin("AC1", "tok", client=client).set_webhooks(
        "PN1", voice_url="https://h/twilio/voice", status_url="https://h/twilio/status"
    )

    assert client.updates == [
        (
            "PN1",
            {
                "voice_url": "https://h/twilio/voice",
                "voice_method": "POST",
                "status_callback": "https://h/twilio/status",
                "status_callback_method": "POST",
            },
        )
    ]


def test_an_admin_failure_is_a_twilio_error_with_the_number_masked():
    from keryx.notify.twilio_out import RestTwilioAdmin

    admin = RestTwilioAdmin("AC1", "tok", client=FakeAccountClient(fail=True))

    with pytest.raises(TwilioError) as caught:
        admin.numbers()

    assert "+15550001111" not in str(caught.value)
    assert mask_number("+15550001111") in str(caught.value)
