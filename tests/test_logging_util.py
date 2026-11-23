"""Tests for `keryx.logging_util`: what a phone number looks like once it is written down,
and what an HTTP client is not allowed to write down at all."""

import json
import logging

import httpx
import pytest

from keryx.logging_util import HTTP_LOGGERS, configure_logging, mask_number, quiet_http_loggers


@pytest.fixture
def http_loggers_restored():
    """Whatever a test does to the HTTP clients' loggers, the next test gets them back."""
    before = {name: logging.getLogger(name).level for name in HTTP_LOGGERS}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("+15551234567", "…4567"),
        ("+442079460123", "…0123"),  # a longer, non-NANP number
        # Short enough that masking would leave nothing; there is nothing to hide either.
        ("911", "911"),
        ("1234", "1234"),
        (None, "nobody"),
        ("", "nobody"),
    ],
)
def test_only_the_last_four_digits_survive(number, expected):
    assert mask_number(number) == expected


def test_a_masked_number_is_still_enough_to_tell_two_callers_apart():
    assert mask_number("+15551234567") != mask_number("+15559999999")


def test_the_full_number_is_not_in_the_masked_form(caplog):
    number = "+15551234567"

    with caplog.at_level(logging.INFO):
        logging.getLogger("keryx.test").info("a call from %s", mask_number(number))

    assert number not in caplog.text
    assert "…4567" in caplog.text


# --- what an HTTP client may not write down ------------------------------------------------


def google(seen=None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "owner@example.org", "primary": True, "timeZone": "UTC"},
                {"id": "colleague@example.org", "summary": "Colleague", "selected": True},
            ]})
        return httpx.Response(200, json={"items": [], "files": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def token_file(tmp_path):
    from keryx.integrations import google as account

    path = tmp_path / "google_token.json"
    path.write_text(json.dumps({
        "client_id": "cid", "refresh_token": "rtoken",
        "scopes": [account.CALENDAR_READONLY, account.DRIVE_READONLY],
    }))
    return path


async def ask_google_things(tmp_path) -> None:
    """A calendar search and a Drive search, through the real request layer."""
    from keryx.integrations import calendar, drive
    from keryx.integrations.google_api import CALENDAR, DRIVE, GoogleReader

    class Unused:
        async def summarise(self, system, prompt):  # pragma: no cover - never asked
            raise AssertionError

    path, client = token_file(tmp_path), google()
    reader = calendar.CalendarReader(
        GoogleReader(path, CALENDAR, needs=calendar.READ_SCOPES, client=client)
    )
    await reader.events(query="oncologist")
    finder = drive.DocumentFinder(
        GoogleReader(path, DRIVE, needs=drive.SCOPES, client=client), Unused()
    )
    await finder.find(search="divorce settlement")


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
async def test_no_address_query_or_id_of_a_google_request_reaches_the_log(
    tmp_path, caplog, http_loggers_restored, level
):
    """httpx logs every request's whole URL at INFO. For Google that is other people's
    addresses (a calendar's id), what was searched for, and file ids."""
    configure_logging(level)
    caplog.set_level(logging.DEBUG)

    await ask_google_things(tmp_path)

    for private in ("example.org", "oncologist", "divorce", "settlement", "googleapis",
                    "HTTP Request", "calendars/", "fullText", "at-1", "rtoken"):
        assert private not in caplog.text
    assert "check_calendar:" in caplog.text  # Keryx's own line, which names no one


async def test_left_alone_the_client_writes_every_address_down(
    caplog, http_loggers_restored
):
    """The tests around this one have teeth: at INFO, httpx logs the whole URL."""
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)
    caplog.set_level(logging.INFO)

    await google().get(
        "https://www.googleapis.com/calendar/v3/calendars/colleague%40example.org/events",
        params={"q": "oncologist"},
    )

    assert "colleague%40example.org" in caplog.text and "oncologist" in caplog.text


async def test_the_google_request_layer_quiets_the_client_itself_however_logging_was_set_up(
    tmp_path, caplog, http_loggers_restored
):
    """A script, a test or a new entry point that calls `logging.basicConfig` by itself
    must not be able to put Google's addresses in a log."""
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    await ask_google_things(tmp_path)

    for private in ("example.org", "oncologist", "divorce", "googleapis", "HTTP Request"):
        assert private not in caplog.text


def test_logging_is_configured_at_the_level_asked_for_with_the_clients_kept_quiet(
    monkeypatch, http_loggers_restored
):
    configured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: configured.update(kwargs))

    configure_logging(logging.DEBUG)

    assert configured["level"] == logging.DEBUG and "%(name)s" in configured["format"]
    assert all(logging.getLogger(name).level == logging.WARNING for name in HTTP_LOGGERS)


def test_quieting_the_clients_twice_is_the_same_as_once(http_loggers_restored):
    quiet_http_loggers()
    quiet_http_loggers()

    assert all(logging.getLogger(name).level == logging.WARNING for name in HTTP_LOGGERS)
