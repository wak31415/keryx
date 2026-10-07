"""`keryx phone`: the owner's Twilio numbers, where the chosen one points, pointing it here
(the owner's alone), and the tunnel — Twilio a fake admin, the network a fake getter."""

import json
import shutil

import httpx
import pytest
from typer.testing import CliRunner

from keryx import phone, units
from keryx.cli import app
from keryx.config.store import ConfigStore
from keryx.notify.twilio_out import TwilioError, TwilioNumber

runner = CliRunner()
SID = "AC0123456789abcdef0123456789abcdef"
TOKEN = "tok-planted-0123456789abcdef"
NUMBER = "+15550001111"
HOST = "keryx.example.com"
VOICE = f"https://{HOST}/twilio/voice"
STATUS = f"https://{HOST}/twilio/status"


class Account:
    """The fake Twilio account `keryx.phone.rest_admin` hands out, recording the pairs it
    was built with and every update."""

    def __init__(self) -> None:
        self.numbers_ = [TwilioNumber("PN1", NUMBER, "https://old.example/voice", None,
                                      "Office")]
        self.error: Exception | None = None
        self.write_error: Exception | None = None
        self.pairs: list[tuple[str, str]] = []
        self.updates: list[tuple[str, str, str]] = []

    def __call__(self, sid, token):
        self.pairs.append((sid, token))
        return self

    def account_name(self):
        if self.error:
            raise self.error
        return "Ada's account"

    def numbers(self):
        if self.error:
            raise self.error
        return list(self.numbers_)

    def set_webhooks(self, number_sid, *, voice_url, status_url):
        if self.write_error:
            raise self.write_error
        self.updates.append((number_sid, voice_url, status_url))


@pytest.fixture
def account(tmp_path, monkeypatch, every_agent_installed):
    """A store with Twilio, a number and a hostname saved; the account faked."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "keryx"))
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))
    ConfigStore().set({"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN,
                       "TWILIO_NUMBER": NUMBER, "PUBLIC_HOST": HOST})
    fake = Account()
    monkeypatch.setattr(phone, "rest_admin", fake)
    return fake


def run(*args, env=None):
    return runner.invoke(app, ["phone", *args], env=env)


# --- numbers -----------------------------------------------------------------------------


def test_numbers_json_lists_the_accounts_numbers(account):
    account.numbers_.append(TwilioNumber("PN2", "+15550002222", VOICE, STATUS, ""))

    result = run("numbers", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "account": "Ada's account",
        "numbers": [
            {"number": NUMBER, "friendly_name": "Office",
             "voice_url": "https://old.example/voice", "points_here": False},
            {"number": "+15550002222", "friendly_name": "", "voice_url": VOICE,
             "points_here": True},
        ],
        "error": None,
    }
    assert account.pairs == [(SID, TOKEN)]


def test_numbers_that_twilio_refuses_are_a_report_with_its_cause(account):
    account.error = TwilioError(f"Authenticate {TOKEN}", status=401)

    result = run("numbers", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "account": None, "numbers": [],
        "error": "Twilio didn't accept the account SID and auth token.",
    }
    assert TOKEN not in result.output


def test_numbers_as_text(account):
    account.numbers_.append(TwilioNumber("PN2", "+15550002222", VOICE, STATUS, ""))

    result = run("numbers")

    assert result.exit_code == 0, result.output
    assert "Ada's account" in result.output
    assert f"{NUMBER}  Office" in result.output
    assert "+15550002222  (points here)" in result.output


def test_numbers_as_text_says_why_there_are_none(account):
    account.error = TwilioError("down", cause="ConnectionError")

    result = run("numbers")

    assert result.exit_code == 0
    assert "Keryx couldn't reach Twilio (ConnectionError)." in result.output


# --- webhooks ----------------------------------------------------------------------------


def test_webhooks_json_compares_the_saved_number(account):
    result = run("webhooks", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "number": NUMBER,
        "expected": {"voice_url": VOICE, "status_url": STATUS},
        "current": {"voice_url": "https://old.example/voice", "status_url": None},
        "matches": False,
        "error": None,
    }
    assert account.updates == []


def test_webhooks_that_point_here_match(account):
    account.numbers_ = [TwilioNumber("PN1", NUMBER, VOICE, STATUS, "Office")]

    assert json.loads(run("webhooks", "--json").stdout)["matches"] is True


def test_webhooks_as_text_show_both_addresses(account):
    result = run("webhooks")

    assert result.exit_code == 0, result.output
    assert "https://old.example/voice" in result.output and VOICE in result.output
    assert "keryx phone webhooks --apply" in result.output


def test_apply_points_the_number_here(account):
    result = run("webhooks", "--apply", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"applied": True, "number": NUMBER}
    assert account.updates == [("PN1", VOICE, STATUS)]


def test_apply_as_text_says_it_rings_here(account):
    result = run("webhooks", "--apply")

    assert result.exit_code == 0, result.output
    assert f"{NUMBER} now rings Keryx at {VOICE}" in result.output


def test_apply_is_the_owners_alone(account):
    result = run("webhooks", "--apply", "--json", env={"KERYX_ACTOR": "service"})

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "only the owner may" in result.stderr
    assert account.pairs == [] and account.updates == []


def test_apply_that_twilio_refuses_fails_with_its_cause(account):
    account.write_error = TwilioError(f"{TOKEN} for {NUMBER}", status=403)

    result = run("webhooks", "--apply", "--json")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Twilio didn't accept the account SID and auth token."
    assert TOKEN not in result.output


def test_apply_with_nowhere_to_point_is_refused_before_twilio(account):
    ConfigStore().unset(["PUBLIC_HOST"])

    result = run("webhooks", "--apply", "--json")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "PUBLIC_HOST" in result.stderr
    assert account.pairs == []


def test_apply_to_a_public_host_with_a_scheme_is_refused_before_twilio(account):
    ConfigStore().set({"PUBLIC_HOST": "https://keryx.ngrok.app"})

    result = run("webhooks", "--apply", "--json")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "PUBLIC_HOST isn't a bare hostname" in result.stderr
    assert account.pairs == [] and account.updates == []


def test_apply_json_is_indented_as_everywhere_else(account):
    result = run("webhooks", "--apply", "--json")

    assert result.stdout == json.dumps({"applied": True, "number": NUMBER}, indent=2) + "\n"


# --- the tunnel --------------------------------------------------------------------------


def test_tunnel_json(account, monkeypatch):
    asked = []
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/homebrew/bin/ngrok"
                        if name == "ngrok" else None)
    monkeypatch.setattr(units, "status", lambda settings: {
        "manager": "launchd", "units": {"tunnel": {"installed": True, "active": True}}})

    def get(url, **kwargs):
        asked.append(url)
        return httpx.Response(200, json={"ok": True, "live_sessions": 0})

    monkeypatch.setattr(httpx, "get", get)

    result = run("tunnel", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "kind": "ngrok", "binary": "/opt/homebrew/bin/ngrok", "public_host": HOST,
        "unit_installed": True, "reachable": True, "error": None,
    }
    assert asked == [f"https://{HOST}/health"]


def test_tunnel_as_text(account, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(units, "status", lambda settings: {
        "manager": None, "units": {"tunnel": {"installed": False, "active": False}}})
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(502))

    result = run("tunnel")

    assert result.exit_code == 0, result.output
    assert "no tunnel on PATH" in result.output
    assert f"{HOST} does not answer as Keryx" in result.output


def test_tunnel_says_a_public_host_that_is_not_a_hostname(account, monkeypatch):
    ConfigStore().set({"PUBLIC_HOST": "https://keryx.ngrok.app"})
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(units, "status", lambda settings: {
        "manager": None, "units": {"tunnel": {"installed": False, "active": False}}})

    result = run("tunnel")

    assert result.exit_code == 0, result.output
    assert "PUBLIC_HOST isn't a bare hostname" in result.output
