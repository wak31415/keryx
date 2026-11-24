"""Tests for `keryx.logging_util`: what a phone number looks like once it is written down,
and what an HTTP or websocket client is not allowed to write down at all."""

import json
import logging

import httpx
import pytest

from keryx.logging_util import (
    HTTP_LOGGERS,
    configure_logging,
    drop_queries_from_the_access_log,
    mask_number,
    quiet_http_loggers,
    uvicorn_log_level,
)


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


# --- what a websocket client may not write down --------------------------------------------

REALTIME_KEY = "sk-test-not-a-key"
#: The shape of a frame the Realtime socket carries: a tool call's arguments, here a PIN.
PIN_FRAME = json.dumps({
    "type": "response.function_call_arguments.done",
    "arguments": json.dumps({"pin": "246810"}),
}).encode()
WEBSOCKETS_LOGGERS = ("websockets.client", "websockets.server", "websockets.protocol")


def open_the_realtime_socket_on_paper() -> None:
    """The handshake `websockets.connect` sends for the Realtime socket, then one frame,
    through the library's own protocol object. Nothing is opened: it only fills a buffer."""
    from websockets.client import ClientProtocol
    from websockets.protocol import OPEN
    from websockets.uri import parse_uri

    protocol = ClientProtocol(parse_uri("wss://api.openai.com/v1/realtime?model=gpt-realtime"))
    request = protocol.connect()
    # What `additional_headers` does with the dict `OpenAIRealtimeProvider._open` builds.
    request.headers.update({"Authorization": f"Bearer {REALTIME_KEY}"})
    protocol.send_request(request)
    protocol.state = OPEN
    protocol.send_text(PIN_FRAME)


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_neither_the_api_key_nor_a_frame_of_the_realtime_socket_reaches_the_log(
    caplog, http_loggers_restored, level
):
    """At DEBUG `websockets.client` logs every handshake header, and the Realtime socket's
    `Authorization` header is the OpenAI key. It logs both ends of every frame as well,
    and a frame can carry the PIN."""
    configure_logging(level)
    caplog.set_level(logging.DEBUG)

    open_the_realtime_socket_on_paper()

    for private in (REALTIME_KEY, "Authorization", "Bearer", "246810", "api.openai.com"):
        assert private not in caplog.text


def test_left_alone_the_websocket_client_writes_the_key_and_the_frame_down(
    caplog, http_loggers_restored
):
    """The test above has teeth: at DEBUG, the library logs the header and the frame."""
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    open_the_realtime_socket_on_paper()

    assert f"> Authorization: Bearer {REALTIME_KEY}" in caplog.text
    assert "246810" in caplog.text


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("name", WEBSOCKETS_LOGGERS)
def test_every_websockets_logger_is_held_to_warnings_whatever_the_level(
    http_loggers_restored, level, name
):
    """Every name `websockets` 17 logs under: the client, a server of its own, and the
    protocol underneath both."""
    configure_logging(level)

    assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    assert logging.getLogger(name).isEnabledFor(logging.WARNING)


# --- what the phone server may not write down ----------------------------------------------

#: uvicorn's own access line: `access_logger.info(ACCESS_LINE, client, method, path, version,
#: status)`, the path with its query string.
ACCESS_LINE = '%s - "%s %s HTTP/%s" %d'
REPORT_FETCH = ("203.0.113.9:4321", "GET", "/reports/7?t=tok-not-a-token", "1.1", 200)


def access_record(*args, message: str = ACCESS_LINE) -> logging.LogRecord:
    """A record as `uvicorn.access` would be handed it, already through its filters."""
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 0, message, args, None
    )
    assert logging.getLogger("uvicorn.access").filter(record)  # never dropped, only trimmed
    return record


@pytest.fixture
def access_log_filtered():
    """The access log with Keryx's filter on it, and without it again afterwards."""
    before = list(logging.getLogger("uvicorn.access").filters)
    drop_queries_from_the_access_log()
    yield
    logging.getLogger("uvicorn.access").filters[:] = before


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("DEBUG", "info"),  # the one that is clamped
        ("debug", "info"),
        ("INFO", "info"),
        ("WARNING", "warning"),
        ("ERROR", "error"),
        ("CRITICAL", "critical"),
    ],
)
def test_uvicorn_is_never_handed_a_level_below_info(level, expected):
    """At DEBUG uvicorn's websocket protocol logs every frame of the Twilio media socket
    under `uvicorn.error`: each keypad digit of a PIN, and the caller's number."""
    assert uvicorn_log_level(level) == expected


def test_the_access_log_loses_the_query_string_of_a_report_link(access_log_filtered):
    """`GET /reports/7?t=…`: the query is the token that opens the report."""
    record = access_record(*REPORT_FETCH)

    assert record.getMessage() == '203.0.113.9:4321 - "GET /reports/7 HTTP/1.1" 200'
    assert "tok-not-a-token" not in str(record.args)


def test_a_trimmed_access_record_is_still_one_uvicorn_can_format(access_log_filtered):
    from uvicorn.logging import AccessFormatter

    record = access_record("203.0.113.9:4321", "GET", "/reports/7?t=tok-not-a-token", "1.1", 403)
    formatter = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )

    assert formatter.format(record) == '203.0.113.9:4321 - "GET /reports/7 HTTP/1.1" 403 Forbidden'


def test_an_access_line_with_no_query_is_left_as_it_was(access_log_filtered):
    args = ("203.0.113.9:4321", "POST", "/twilio/voice", "1.1", 200)

    record = access_record(*args)

    assert record.args == args
    assert record.getMessage() == '203.0.113.9:4321 - "POST /twilio/voice HTTP/1.1" 200'


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("no arguments at all", ()),
        ("%s and %s", ("two", "of them")),
        ("%s %s %s %s %s %s", ("six", "of", "them?here", "is", "one", "more")),
        ("%s %s %r %s %s", ("the", "path", b"/is/bytes?t=1", "this", "time")),
        ("%s %s %s %s %s", ("the", "path", None, "is", "missing")),
        ("%(name)s is a mapping", ({"name": "this?one"},)),
    ],
)
def test_a_record_of_any_other_shape_passes_through_unharmed(
    access_log_filtered, message, args
):
    """The filter is on a logger it does not own. A line it does not recognise is written
    as it came, and nothing it is handed can make it raise."""
    expected = message % (args[0] if isinstance(args[0], dict) else args) if args else message

    record = access_record(*args, message=message)

    assert record.getMessage() == expected


def test_the_filter_is_put_on_the_access_log_once_however_often_it_is_asked(
    access_log_filtered,
):
    before = len(logging.getLogger("uvicorn.access").filters)

    drop_queries_from_the_access_log()
    drop_queries_from_the_access_log()

    assert len(logging.getLogger("uvicorn.access").filters) == before


def test_left_alone_the_access_log_writes_the_token_down():
    """The tests above have teeth: without the filter the query is in the line."""
    filters = logging.getLogger("uvicorn.access").filters
    before, filters[:] = list(filters), []
    try:
        record = access_record(*REPORT_FETCH)
    finally:
        filters[:] = before

    assert "tok-not-a-token" in record.getMessage()
