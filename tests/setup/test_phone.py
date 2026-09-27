"""The phone section: Twilio checked and its number picked, the tunnel's hostname, and the
one write outside the machine — the webhooks — only on a yes."""

import pytest

from jarvis.config.store import ConfigStore
from jarvis.notify.twilio_out import TwilioNumber
from jarvis.setup import phone
from jarvis.setup.phone import hostname_problem, numbers_problem, webhook_urls

from .fakes import DEFAULT, FakeTwilioAdmin

TWILIO = [("Account SID", "AC123"), ("Auth token", "tok"), ("Which number", "+15550001111")]


def test_skipping_asks_nothing_else(make_ctx):
    ctx = make_ctx([("Set up phone calls", "skip")])

    phone.run_section(ctx)

    assert ctx.ui.done() and ConfigStore().stored() == {}


def test_the_whole_phone_with_the_webhooks_set_on_a_yes(make_ctx, world):
    ctx = make_ctx(
        [
            ("Set up phone calls", "setup"),
            *TWILIO,
            ("Public hostname", "jarvis.example.com"),
            ("tunnel name", DEFAULT),
            ("Point +15550001111 at this machine", True),
            ("send you texts", DEFAULT),
        ]
    )

    phone.run_section(ctx)

    stored = ConfigStore().stored()
    assert stored["TWILIO_ACCOUNT_SID"] == "AC123"
    assert stored["TWILIO_NUMBER"] == "+15550001111"
    assert stored["PUBLIC_HOST"] == "jarvis.example.com"
    assert ConfigStore()._secrets() == {"TWILIO_AUTH_TOKEN": "tok"}
    assert world.twilio_admin.updates == [
        ("PN1", "https://jarvis.example.com/twilio/voice",
         "https://jarvis.example.com/twilio/status")
    ]
    assert "SMS_ENABLED" not in stored


def test_the_webhook_is_left_alone_on_a_no(make_ctx, world):
    ctx = make_ctx(
        [
            ("Set up phone calls", "setup"),
            *TWILIO,
            ("Public hostname", "jarvis.example.com"),
            ("tunnel name", DEFAULT),
            ("Point +15550001111", False),
            ("send you texts", False),
        ]
    )

    phone.run_section(ctx)

    assert world.twilio_admin.updates == []
    assert any("console.twilio.com" in line for line in ctx.ui.lines("note"))


def test_a_webhook_already_pointed_here_is_not_asked_about(make_ctx, world):
    voice, status = webhook_urls("jarvis.example.com")
    world.twilio_admin.numbers_ = [TwilioNumber("PN1", "+15550001111", voice, status)]
    ctx = make_ctx(
        [
            ("Set up phone calls", "setup"),
            *TWILIO,
            ("Public hostname", "jarvis.example.com"),
            ("tunnel name", DEFAULT),
            ("send you texts", DEFAULT),
        ]
    )

    phone.run_section(ctx)

    assert world.twilio_admin.updates == []
    assert ctx.ui.done()


def test_credentials_twilio_refuses_stop_the_section_and_store_nothing(make_ctx, world):
    world.twilio_admin.refuse = True
    ctx = make_ctx([("Set up phone calls", "setup"), ("Account SID", "AC1"), ("Auth token", "x")])

    phone.run_section(ctx)

    assert ConfigStore().stored() == {}
    assert any("Twilio refused" in line for line in ctx.ui.lines("error"))


def test_an_account_with_no_number_says_where_to_buy_one(make_ctx, world):
    world.twilio_admin = FakeTwilioAdmin(numbers_=[])
    ctx = make_ctx([("Set up phone calls", "setup"), ("Account SID", "AC1"), ("Auth token", "x")])

    phone.run_section(ctx)

    assert any("no phone number" in line for line in ctx.ui.lines("error"))


def test_texting_is_turned_on_only_when_asked(make_ctx):
    ConfigStore().set(
        {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "PUBLIC_HOST": "j.example.com"}
    )
    ctx = make_ctx(
        [("Set up phone calls", "setup"), ("Which number", DEFAULT), ("Point", False),
         ("send you texts", True)]
    )

    phone.run_section(ctx)

    assert ConfigStore().stored()["SMS_ENABLED"] is True


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("jarvis.example.com", True),
        ("https://jarvis.example.com", False),
        ("jarvis.example.com/twilio", False),
        ("localhost", False),
    ],
)
def test_the_hostname_is_a_bare_name(value, ok):
    assert (hostname_problem(value) is None) is ok


def test_numbers_must_be_e164():
    assert numbers_problem("+15551234567, +447700900123") is None
    assert numbers_problem("") is None
    assert "5551234567" in numbers_problem("+15551234567,5551234567")
