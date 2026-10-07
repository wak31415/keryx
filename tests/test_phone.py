"""`keryx.phone`: the owner's Twilio numbers, where they point, and the tunnel — the one
implementation behind the wizard's phone section and `keryx phone`.

Twilio is a fake admin and the network a fake getter: nothing here leaves the process.
"""

import logging
import shutil
from dataclasses import dataclass, field

import httpx
import pytest

from keryx import phone
from keryx.notify.twilio_out import TwilioError, TwilioNumber

HOST = "keryx.example.com"
VOICE = f"https://{HOST}/twilio/voice"
STATUS = f"https://{HOST}/twilio/status"
NUMBER = "+15550001111"


@dataclass
class FakeAdmin:
    """A Twilio account: its name, its numbers, and every update it was asked for. `error`
    is raised by every call, `write_error` by the update alone."""

    numbers_: list[TwilioNumber] = field(
        default_factory=lambda: [TwilioNumber("PN1", NUMBER, None, None, "Office")]
    )
    name: str = "Ada's account"
    error: TwilioError | None = None
    write_error: TwilioError | None = None
    updates: list[tuple[str, str, str]] = field(default_factory=list)

    def account_name(self) -> str:
        if self.error:
            raise self.error
        return self.name

    def numbers(self) -> list[TwilioNumber]:
        if self.error:
            raise self.error
        return list(self.numbers_)

    def set_webhooks(self, number_sid: str, *, voice_url: str, status_url: str) -> None:
        if self.error or self.write_error:
            raise self.error or self.write_error
        self.updates.append((number_sid, voice_url, status_url))


# --- where a number points ---------------------------------------------------------------


def test_the_webhooks_for_a_hostname():
    assert phone.webhook_urls(HOST) == (VOICE, STATUS)


def test_a_number_matches_only_when_both_webhooks_point_here():
    assert phone.matches(TwilioNumber("PN1", NUMBER, VOICE, STATUS), HOST)
    assert phone.matches(TwilioNumber("PN1", NUMBER, VOICE + "/", STATUS + "/"), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, VOICE, None), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, "https://old.example/v", STATUS), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, None, None), HOST)


def test_pointing_a_number_here_writes_both_webhooks_and_logs_it_masked(caplog):
    admin = FakeAdmin()

    with caplog.at_level(logging.INFO, logger="keryx.phone"):
        phone.point_here(admin, TwilioNumber("PN1", NUMBER, None, None), HOST)

    assert admin.updates == [("PN1", VOICE, STATUS)]
    assert NUMBER not in caplog.text
    assert "1111" in caplog.text


# --- the account's numbers ---------------------------------------------------------------

SID = "AC0123456789abcdef0123456789abcdef"
TOKEN = "tok-planted-0123456789abcdef"


@pytest.fixture
def phoned(settings):
    """Settings with Twilio, a number and a hostname saved."""
    return settings.model_copy(update={
        "twilio_account_sid": SID, "twilio_auth_token": TOKEN, "twilio_number": NUMBER,
        "public_host": HOST,
    })


class Factory:
    """`(sid, token)` → the fake admin, recording each pair it was handed."""

    def __init__(self, admin: FakeAdmin | None = None) -> None:
        self.admin = admin or FakeAdmin()
        self.pairs: list[tuple[str, str]] = []

    def __call__(self, sid: str, token: str) -> FakeAdmin:
        self.pairs.append((sid, token))
        return self.admin


def test_the_numbers_name_the_account_and_where_each_points(phoned):
    factory = Factory(FakeAdmin(numbers_=[
        TwilioNumber("PN1", NUMBER, VOICE, STATUS, "Office"),
        TwilioNumber("PN2", "+15550002222", "https://demo.twilio.com/welcome/voice/", None,
                     "(555) 000-2222"),
    ]))

    report = phone.numbers_report(phoned, factory)

    assert report == {
        "account": "Ada's account",
        "numbers": [
            {"number": NUMBER, "friendly_name": "Office", "voice_url": VOICE,
             "points_here": True},
            {"number": "+15550002222", "friendly_name": "(555) 000-2222",
             "voice_url": "https://demo.twilio.com/welcome/voice/", "points_here": False},
        ],
        "error": None,
    }
    assert factory.pairs == [(SID, TOKEN)]


def test_a_number_points_here_by_its_voice_webhook(phoned):
    """The calls ring here: the status callback is what `webhooks` compares as well."""
    factory = Factory(FakeAdmin(numbers_=[TwilioNumber("PN1", NUMBER, VOICE + "/", None, "")]))

    assert phone.numbers_report(phoned, factory)["numbers"][0]["points_here"] is True


def test_without_a_hostname_no_number_points_here(phoned):
    bare = phoned.model_copy(update={"public_host": None})
    factory = Factory(FakeAdmin(numbers_=[TwilioNumber("PN1", NUMBER, VOICE, STATUS, "")]))

    assert phone.numbers_report(bare, factory)["numbers"][0]["points_here"] is False


@pytest.mark.parametrize(
    ("missing", "error"),
    [({"twilio_account_sid": None, "twilio_auth_token": None},
      "There's no TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN saved."),
     ({"twilio_auth_token": None}, "There's no TWILIO_AUTH_TOKEN saved.")],
)
def test_without_credentials_nobody_is_asked(phoned, missing, error):
    factory = Factory()

    report = phone.numbers_report(phoned.model_copy(update=missing), factory)

    assert report == {"account": None, "numbers": [], "error": error}
    assert factory.pairs == []


@pytest.mark.parametrize(
    ("error", "problem"),
    [(TwilioError(f"Authenticate {TOKEN} {NUMBER}", status=401),
      "Twilio didn't accept the account SID and auth token."),
     (TwilioError(TOKEN, status=500), "Twilio answered with HTTP 500."),
     (TwilioError(TOKEN, cause="ConnectionError"),
      "Keryx couldn't reach Twilio (ConnectionError)."),
     (RuntimeError(TOKEN), "Keryx couldn't reach Twilio (RuntimeError).")],
)
def test_a_twilio_failure_is_one_sentence_of_cause(phoned, error, problem):
    report = phone.numbers_report(phoned, Factory(FakeAdmin(error=error)))

    assert report == {"account": None, "numbers": [], "error": problem}


def test_the_numbers_are_never_logged_in_full(phoned, caplog):
    with caplog.at_level(logging.DEBUG):
        phone.numbers_report(phoned, Factory())
        phone.webhooks_report(phoned, Factory())
        phone.apply_webhooks(phoned, Factory())

    assert NUMBER not in caplog.text and TOKEN not in caplog.text


# --- the webhooks ------------------------------------------------------------------------


def test_webhooks_that_point_here_match(phoned):
    factory = Factory(FakeAdmin(numbers_=[TwilioNumber("PN1", NUMBER, VOICE, STATUS, "")]))

    assert phone.webhooks_report(phoned, factory) == {
        "number": NUMBER,
        "expected": {"voice_url": VOICE, "status_url": STATUS},
        "current": {"voice_url": VOICE, "status_url": STATUS},
        "matches": True,
        "error": None,
    }


def test_webhooks_that_point_elsewhere_do_not(phoned):
    old = "https://demo.twilio.com/welcome/voice/"
    factory = Factory(FakeAdmin(numbers_=[
        TwilioNumber("PN2", "+15550002222", VOICE, STATUS, ""),
        TwilioNumber("PN1", NUMBER, old, None, ""),
    ]))

    report = phone.webhooks_report(phoned, factory)

    assert report["current"] == {"voice_url": old, "status_url": None}
    assert (report["matches"], report["error"]) == (False, None)
    assert factory.admin.updates == []


def test_without_a_number_there_is_nothing_to_compare(phoned):
    factory = Factory()

    report = phone.webhooks_report(phoned.model_copy(update={"twilio_number": None}), factory)

    assert report == {
        "number": None,
        "expected": {"voice_url": VOICE, "status_url": STATUS},
        "current": None,
        "matches": False,
        "error": "There's no TWILIO_NUMBER saved.",
    }
    assert factory.pairs == []


def test_without_a_hostname_nothing_is_expected_but_the_number_is_read(phoned):
    bare = phoned.model_copy(update={"public_host": None})

    report = phone.webhooks_report(bare, Factory())

    assert report["expected"] == {"voice_url": None, "status_url": None}
    assert report["current"] == {"voice_url": None, "status_url": None}
    assert report["matches"] is False
    assert report["error"] == "There's no PUBLIC_HOST saved, so there's nowhere to point it."


def test_a_number_not_on_the_account_is_said_without_it(phoned):
    factory = Factory(FakeAdmin(numbers_=[TwilioNumber("PN2", "+15550002222", None, None, "")]))

    report = phone.webhooks_report(phoned, factory)

    assert report["current"] is None and report["matches"] is False
    assert report["error"] == "TWILIO_NUMBER isn't one of this Twilio account's numbers."


def test_a_webhook_read_twilio_refuses_is_one_sentence(phoned):
    factory = Factory(FakeAdmin(error=TwilioError(TOKEN, status=401)))

    report = phone.webhooks_report(phoned, factory)

    assert report["current"] is None and report["matches"] is False
    assert report["error"] == "Twilio didn't accept the account SID and auth token."


# --- pointing it here ------------------------------------------------------------------------


def test_applying_points_the_saved_number_here(phoned):
    factory = Factory(FakeAdmin(numbers_=[
        TwilioNumber("PN2", "+15550002222", None, None, ""),
        TwilioNumber("PN1", NUMBER, "https://old.example/v", None, ""),
    ]))

    assert phone.apply_webhooks(phoned, factory) == {"applied": True, "number": NUMBER}
    assert factory.admin.updates == [("PN1", VOICE, STATUS)]
    assert factory.pairs == [(SID, TOKEN)]


@pytest.mark.parametrize(
    ("missing", "why"),
    [({"twilio_auth_token": None}, "There's no TWILIO_AUTH_TOKEN saved."),
     ({"twilio_number": None}, "There's no TWILIO_NUMBER saved."),
     ({"public_host": None}, "There's no PUBLIC_HOST saved, so there's nowhere to point it.")],
)
def test_applying_with_something_missing_is_refused_before_twilio(phoned, missing, why):
    factory = Factory()

    with pytest.raises(phone.Refused, match=f"^{why}$"):
        phone.apply_webhooks(phoned.model_copy(update=missing), factory)

    assert factory.pairs == []


@pytest.mark.parametrize(
    "admin",
    [FakeAdmin(write_error=TwilioError(f"{TOKEN} {NUMBER}", status=403)),
     FakeAdmin(error=TwilioError(TOKEN, status=401))],
)
def test_twilio_refusing_the_change_is_a_failure_of_one_sentence(phoned, admin):
    with pytest.raises(phone.Failed) as failed:
        phone.apply_webhooks(phoned, Factory(admin))

    assert str(failed.value) == "Twilio didn't accept the account SID and auth token."
    assert admin.updates == []


def test_applying_to_a_number_not_on_the_account_fails(phoned):
    factory = Factory(FakeAdmin(numbers_=[]))

    with pytest.raises(phone.Failed,
                       match="^TWILIO_NUMBER isn't one of this Twilio account's numbers.$"):
        phone.apply_webhooks(phoned, factory)


# --- the tunnel --------------------------------------------------------------------------


class FakeGet:
    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status, json=self.body if self.body is not None else {})


def which(*found):
    paths = {name: f"/opt/homebrew/bin/{name}" for name in found}
    return lambda name: paths.get(name)


def units_with(tunnel_installed):
    def status(settings):
        return {"manager": "launchd", "units": {
            "tunnel": {"installed": tunnel_installed, "active": tunnel_installed}}}
    return status


def test_no_tunnel_and_no_hostname_asks_nothing(settings):
    get = FakeGet()

    report = phone.tunnel_report(settings, which=which(), get=get, unit_status=units_with(False))

    assert report == {"kind": None, "binary": None, "public_host": None,
                      "unit_installed": False, "reachable": None}
    assert get.calls == []


@pytest.mark.parametrize(
    ("found", "kind"),
    [(("ngrok",), "ngrok"), (("cloudflared",), "cloudflared"),
     (("ngrok", "cloudflared"), "cloudflared")],
)
def test_the_tunnel_is_found_as_doctor_finds_it(settings, found, kind):
    report = phone.tunnel_report(settings, which=which(*found), get=FakeGet(),
                                 unit_status=units_with(True))

    assert (report["kind"], report["binary"]) == (kind, f"/opt/homebrew/bin/{kind}")
    assert report["unit_installed"] is True


def test_reachable_is_keryx_answering_through_the_hostname(phoned):
    get = FakeGet(body={"ok": True, "live_sessions": 0})

    report = phone.tunnel_report(phoned, which=which("ngrok"), get=get,
                                 unit_status=units_with(True))

    assert (report["public_host"], report["reachable"]) == (HOST, True)
    assert get.calls == [(f"https://{HOST}/health",
                          {"timeout": 3.0, "follow_redirects": False})]


@pytest.mark.parametrize(
    "get",
    [FakeGet(status=502), FakeGet(status=404, body={"error": "ERR_NGROK_3200"}),
     FakeGet(body={"something": "else"}), FakeGet(raises=httpx.ConnectTimeout("slow"))],
)
def test_a_hostname_that_does_not_answer_as_keryx_is_not_reachable(phoned, get):
    report = phone.tunnel_report(phoned, which=which(), get=get, unit_status=units_with(False))

    assert report["reachable"] is False


def test_the_real_detection_asks_path_and_the_units(phoned, monkeypatch):
    from keryx import units

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(units, "status", units_with(True))

    report = phone.tunnel_report(phoned, get=FakeGet(body={"ok": True}))

    assert report["kind"] is None and report["unit_installed"] is True


def test_the_real_admin_carries_the_credentials_in_its_auth_and_a_timeout():
    admin = phone.rest_admin(SID, TOKEN)
    client = admin._client

    assert (client.username, client.password) == (SID, TOKEN)
    assert client.http_client.timeout == phone.TIMEOUT_S


def test_the_real_admin_never_reaches_twilio_from_a_test(phoned):
    """The suite's guard: a missed fake is Twilio unreachable, never a request."""
    report = phone.numbers_report(phoned)

    assert report["error"] == "Keryx couldn't reach Twilio (AssertionError)."
