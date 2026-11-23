"""The HTTP every Google voice tool shares: the token, one request, and what a failure means.

No network: an httpx MockTransport answers for the token endpoint and for the API. The
refresh itself (reuse, expiry, a 401) is held by `HttpGmail`'s tests in `test_gmail.py`,
which run through this same class; what is here is what Gmail never needed.
"""

import json

import httpx
import pytest

from keryx.integrations import google
from keryx.integrations.google_api import (
    CALENDAR,
    MESSAGES,
    GoogleApiError,
    GoogleReader,
    GoogleWriter,
    access_problem,
    failure,
    offered,
)

NEEDS = (google.CALENDAR_READONLY,)


def token_file(tmp_path, **extra):
    path = tmp_path / "google_token.json"
    data = {
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtoken",
        "scopes": list(google.requested_scopes(["calendar"])),
        **extra,
    }
    path.write_text(json.dumps(data))
    return path


def client_for(answer, seen=None):
    """A client whose token endpoint always works and whose API answers with `answer`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if seen is not None:
            seen.append(request)
        return answer(request) if callable(answer) else answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def reader(tmp_path, answer, seen=None, **extra):
    return GoogleReader(
        token_file(tmp_path, **extra), CALENDAR, needs=NEEDS, client=client_for(answer, seen)
    )


async def failing(tmp_path, response) -> GoogleApiError:
    with pytest.raises(GoogleApiError) as raised:
        await reader(tmp_path, response).get("calendars/someone@example.org/events")
    return raised.value


def google_error(status, *, reason=None, detail=None, state=None, message="secret-body"):
    error = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"reason": reason, "message": message}]
    if detail:
        error["details"] = [{"reason": detail}]
    if state:
        error["status"] = state
    return httpx.Response(status, json={"error": error})


# ------------------------------------------------------------------------- requests


async def test_a_get_is_a_path_under_the_base_with_the_access_token(tmp_path):
    seen: list[httpx.Request] = []
    api = reader(tmp_path, httpx.Response(200, json={"items": []}), seen)

    assert await api.get("users/me/calendarList", {"maxResults": 5}) == {"items": []}

    [request] = seen
    assert str(request.url) == f"{CALENDAR}/users/me/calendarList?maxResults=5"
    assert request.method == "GET"
    assert request.headers["Authorization"] == "Bearer at-1"


async def test_a_parameter_is_escaped_whatever_it_holds(tmp_path):
    seen: list[httpx.Request] = []
    api = reader(tmp_path, httpx.Response(200, json={}), seen)

    await api.get("events", {"q": "a&b=c #d'"})

    assert seen[0].url.params["q"] == "a&b=c #d'"
    assert seen[0].url.query.count(b"&") == 0


async def test_only_a_writer_can_post(tmp_path):
    seen: list[httpx.Request] = []
    writer = GoogleWriter(
        token_file(tmp_path),
        CALENDAR,
        client=client_for(httpx.Response(200, json={"id": "e1"}), seen),
    )

    created = await writer.post("calendars/primary/events", {"summary": "x"}, {"a": "b"})

    assert created == {"id": "e1"}
    assert seen[0].method == "POST" and json.loads(seen[0].content) == {"summary": "x"}
    assert seen[0].url.params["a"] == "b"
    assert not hasattr(GoogleReader, "post")


async def test_text_is_decoded_and_cut_to_the_limit(tmp_path):
    body = "﻿line one\nline two".encode()
    api = reader(tmp_path, httpx.Response(200, content=body))

    assert await api.text("files/abc/export", limit=100) == ("line one\nline two", False)
    assert await api.text("files/abc/export", limit=8) == ("line one", True)


async def test_an_answer_that_is_not_an_object_is_an_empty_one(tmp_path):
    assert await reader(tmp_path, httpx.Response(200, json=[1, 2])).get("x") == {}
    assert await reader(tmp_path, httpx.Response(200, text="not json")).get("x") == {}


# ------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (google_error(403, reason="accessNotConfigured"), "api_disabled"),
        (google_error(403, detail="SERVICE_DISABLED", state="PERMISSION_DENIED"), "api_disabled"),
        (google_error(403, reason="insufficientPermissions"), "not_connected"),
        (google_error(403, detail="ACCESS_TOKEN_SCOPE_INSUFFICIENT"), "not_connected"),
        (google_error(403, reason="exportSizeLimitExceeded"), "too_large"),
        (google_error(403, reason="rateLimitExceeded"), "failed"),
        (google_error(403, reason="somethingElse"), "forbidden"),
        (httpx.Response(403, text="no json at all"), "forbidden"),
        (httpx.Response(403, json={"error": "a string"}), "forbidden"),
        (google_error(404, reason="notFound"), "not_found"),
        (google_error(400, reason="invalid"), "rejected"),
        (google_error(429, reason="rateLimitExceeded"), "failed"),
        (httpx.Response(500), "failed"),
    ],
)
async def test_what_google_refused_is_told_apart(tmp_path, response, code):
    error = await failing(tmp_path, response)

    assert error.code == code and error.status == response.status_code
    assert error.spoken("Calendar")


async def test_a_failure_never_quotes_the_answer_or_which_thing_was_asked_for(tmp_path, caplog):
    error = await failing(tmp_path, google_error(403, reason="accessNotConfigured"))

    told = f"{error} {error.detail} {caplog.text}"
    assert error.detail == "calendars: HTTP 403 (api_disabled)"
    assert "secret-body" not in told and "someone@example.org" not in told
    assert "rtoken" not in told and "csecret" not in told and "at-1" not in told


async def test_a_timeout_is_told_from_a_connection_that_failed(tmp_path):
    def slow(request):
        raise httpx.ReadTimeout("slow")

    def down(request):
        raise httpx.ConnectError("down")

    assert (await failing(tmp_path, slow)).code == "timeout"
    assert (await failing(tmp_path, down)).code == "failed"


async def test_a_scope_the_sign_in_lacks_is_known_before_anything_is_sent(tmp_path):
    seen: list[httpx.Request] = []
    api = reader(
        tmp_path, httpx.Response(200, json={}), seen, scopes=[google.GMAIL_MODIFY]
    )

    with pytest.raises(GoogleApiError) as raised:
        await api.get("users/me/calendarList")

    assert (raised.value.code, raised.value.status) == ("not_connected", None)
    assert seen == []


async def test_a_broader_scope_stands_in_and_a_file_with_none_listed_is_tried(tmp_path):
    full = reader(tmp_path, httpx.Response(200, json={"a": 1}), scopes=[google.CALENDAR_FULL])
    unlisted = reader(tmp_path, httpx.Response(200, json={"a": 2}), scopes=None)

    assert await full.get("x") == {"a": 1}
    assert await unlisted.get("x") == {"a": 2}


@pytest.mark.parametrize("content", ["", "[]", '{"client_id": "c"}', "not json"])
async def test_a_token_file_that_cannot_refresh_is_not_signed_in(tmp_path, content):
    path = tmp_path / "google_token.json"
    path.write_text(content)

    with pytest.raises(GoogleApiError) as raised:
        await GoogleReader(path, CALENDAR).get("x")

    assert raised.value.code == "not_signed_in"


def test_every_failure_has_a_sentence_that_names_what_was_asked_and_the_fix():
    for code in ("not_signed_in", "not_connected", "signed_out"):
        said = GoogleApiError(code).spoken("Calendar")
        assert "Calendar" in said and "`keryx auth login google`" in said
    assert "signing in again won't fix it" in GoogleApiError("api_disabled").spoken("Drive")
    assert failure(GoogleApiError("failed"), "Drive") == {
        "status": "failed",
        "message": MESSAGES["failed"].format(service="Drive"),
    }
    assert all("{" not in GoogleApiError(code).spoken("Drive") for code in MESSAGES)


# ---------------------------------------------------------------------- the sign-in


def sign_in(settings, services):
    google.write_token(
        settings,
        google.GoogleToken("rt", "cid", "cs", google.requested_scopes(services), "a@b.c"),
    )


def test_no_sign_in_is_a_problem_with_the_command(settings):
    settings.ensure_dirs()

    assert access_problem(settings, "calendar", NEEDS) == (
        "not signed in to Google: `keryx auth login google`"
    )


def test_a_sign_in_without_the_scope_names_the_service(settings):
    settings.ensure_dirs()
    sign_in(settings, ["gmail"])

    assert access_problem(settings, "calendar", NEEDS) == (
        "Calendar is not part of your Google sign-in: `keryx auth login google`"
    )


def test_a_sign_in_with_the_scope_is_no_problem(settings):
    settings.ensure_dirs()
    sign_in(settings, ["calendar"])

    assert access_problem(settings, "calendar", NEEDS) is None


def test_holding_a_scope_is_narrower_than_a_service_being_connected():
    read_only = ["openid", google.CALENDAR_READONLY]

    assert google.holds(read_only, (google.CALENDAR_READONLY,))
    assert not google.holds(read_only, (google.CALENDAR_EVENTS,))
    assert "calendar" not in google.connected_services(read_only)
    assert google.holds([google.DRIVE_FULL], (google.DRIVE_READONLY,))


def test_a_service_keryxs_own_app_does_not_offer_names_what_would_add_it(
    settings, tmp_path, monkeypatch
):
    """Signing in again cannot add what the client does not ask for."""
    client = tmp_path / "google_client.json"
    client.write_text(json.dumps({
        "installed": {"client_id": "keryx.apps", "client_secret": "GOCSPX-published"},
        "keryx_services": ["gmail"],
    }))
    monkeypatch.setattr(google, "PUBLISHED_CLIENT_FILE", client)
    settings.ensure_dirs()
    sign_in(settings, ["gmail"])

    problem = access_problem(settings, "calendar", NEEDS)

    assert "Keryx's Google app does not offer it yet" in problem
    assert "`keryx auth login google --client-file FILE`" in problem
    assert not offered(settings, "calendar") and offered(settings, "gmail")


# ---------------------------------------------------- a request with no answer, and text


def stream_of(chunks, served):
    async def body():
        for chunk in chunks:
            served.append(len(chunk))
            yield chunk

    return httpx.Response(200, content=body())


async def test_text_stops_reading_at_the_cap_whatever_the_size_of_the_file(tmp_path):
    served: list[int] = []
    api = reader(tmp_path, lambda request: stream_of([b"x" * 100] * 1000, served))

    text, cut = await api.text("files/abc/export", limit=50)

    assert (text, cut) == ("x" * 50, True)
    assert len(served) < 10  # 100 kB on offer, a few hundred bytes taken


async def test_text_that_fits_is_all_read_and_a_cut_never_shows_half_a_character(tmp_path):
    whole = reader(tmp_path, lambda request: stream_of([b"caf", "é!".encode()], []))
    assert await whole.text("files/abc/export", limit=50) == ("café!", False)

    # Three-byte characters, cut by the byte budget in the middle of one.
    wide = reader(tmp_path, lambda request: stream_of(["€".encode() * 40], []))
    text, cut = await wide.text("files/abc/export", limit=4)
    assert (text, cut) == ("€€€€", True)


async def test_a_connection_lost_while_reading_text_is_a_failure_with_a_sentence(tmp_path):
    def broken(request):
        async def body():
            yield b"some"
            raise httpx.ReadError("gone")

        return httpx.Response(200, content=body())

    with pytest.raises(GoogleApiError) as raised:
        await reader(tmp_path, broken).text("files/abc/export", limit=50)

    assert raised.value.code == "failed" and raised.value.detail == "files: ReadError"


@pytest.mark.parametrize(
    ("error", "code", "may_have_landed"),
    [
        (httpx.ConnectError("refused"), "failed", False),
        (httpx.ConnectTimeout("slow to connect"), "timeout", False),
        (httpx.PoolTimeout("no connection free"), "timeout", False),
        (httpx.ReadTimeout("sent, no answer"), "timeout", True),
        (httpx.WriteError("lost while sending"), "failed", True),
        (httpx.RemoteProtocolError("cut off"), "failed", True),
    ],
)
async def test_a_request_never_sent_is_told_from_one_never_answered(
    tmp_path, error, code, may_have_landed
):
    def fail(request):
        raise error

    raised = await failing(tmp_path, fail)

    assert (raised.code, raised.may_have_landed) == (code, may_have_landed)


@pytest.mark.parametrize(
    ("status", "may_have_landed"), [(500, True), (503, True), (400, False), (403, False),
                                    (404, False)]
)
async def test_only_a_fault_of_googles_own_leaves_a_write_in_doubt(
    tmp_path, status, may_have_landed
):
    assert (await failing(tmp_path, httpx.Response(status))).may_have_landed is may_have_landed


async def test_nothing_that_fails_before_the_request_is_in_doubt(tmp_path):
    def no_refresh(request):
        if request.url.host == "oauth2.googleapis.com":
            raise httpx.ReadTimeout("the token endpoint, not the API")
        raise AssertionError("never reached")

    api = GoogleWriter(
        token_file(tmp_path), CALENDAR,
        client=httpx.AsyncClient(transport=httpx.MockTransport(no_refresh)),
    )
    told: list[str] = []

    with pytest.raises(GoogleApiError) as raised:
        await api.post("calendars/primary/events", {}, sending=lambda: told.append("sending"))

    assert (raised.value.code, raised.value.may_have_landed) == ("timeout", False)
    assert told == []
    assert not GoogleApiError("signed_out").may_have_landed
    assert not GoogleApiError("not_connected").may_have_landed


async def test_a_writer_says_when_the_request_is_about_to_leave(tmp_path):
    seen: list[httpx.Request] = []
    order: list[str] = []

    def answer(request):
        order.append("request")
        return httpx.Response(200, json={"id": "e1"})

    api = GoogleWriter(token_file(tmp_path), CALENDAR, client=client_for(answer, seen))

    await api.post("calendars/primary/events", {"summary": "x"},
                   sending=lambda: order.append("sending"))

    assert order == ["sending", "request"]


async def test_a_401_closes_what_it_was_reading_and_tries_once_more(tmp_path):
    answers = [httpx.Response(401, json={}), httpx.Response(200, content=b"fine")]
    api = reader(tmp_path, lambda request: answers.pop(0))

    assert await api.text("files/abc/export", limit=10) == ("fine", False)


async def test_the_connections_it_opened_are_closed_and_opened_again_when_needed(tmp_path):
    api = GoogleReader(token_file(tmp_path), CALENDAR)
    first = api._http()

    assert first.timeout.connect == 5.0 and first.timeout.read == 15.0
    await api.aclose()
    await api.aclose()  # nothing open is nothing to close

    assert first.is_closed and api._client is None
    assert api._http() is not first
    await api.aclose()


async def test_a_client_it_was_handed_is_not_its_to_close(tmp_path):
    client = client_for(httpx.Response(200, json={}))
    api = GoogleReader(token_file(tmp_path), CALENDAR, client=client)

    await api.get("x")
    await api.aclose()

    assert not client.is_closed
    await client.aclose()


async def test_a_refusal_whose_body_breaks_off_is_still_a_failure_with_a_sentence(tmp_path):
    def broken(request):
        async def body():
            yield b'{"error": '
            raise httpx.ReadError("gone")

        return httpx.Response(403, content=body())

    with pytest.raises(GoogleApiError) as raised:
        await reader(tmp_path, broken).text("files/abc/export", limit=50)

    assert raised.value.code == "failed"
