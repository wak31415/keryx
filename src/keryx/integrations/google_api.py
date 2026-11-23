"""Google's REST APIs on the one sign-in: the HTTP every Google voice tool goes through.

`check_email`, `check_calendar`, `add_calendar_event` and `find_document` each ask Google
something in the middle of a call. What they share is here: the access token (refreshed
from `DATA_DIR/google_token.json`, kept and reused), one authorized request, what a
failure *means*, because the voice has to say it in a sentence, and the three small things
every answer is put together from (`Unclear`, `clip`, `say_day`).

Two classes, so that reading is a matter of construction and not of care: a `GoogleReader`
has `get` and `text` and nothing else, whatever the token would allow, and only a
`GoogleWriter` can `post`. A read-only tool is built on the first and has no method to
write with.

A failure is a `GoogleApiError` with a `code`:

- `not_signed_in` — no token file, or one that cannot refresh.
- `not_connected` — the sign-in does not hold a scope this tool needs (Google lets the
  owner untick any), checked against the file before any request, and again when Google
  says so itself.
- `signed_out` — Google refused the refresh (`invalid_grant`: revoked, or a Testing-mode
  app past its seven days), or a 401 that a fresh access token did not cure.
- `api_disabled` — the scope was granted but the API is not enabled in the Cloud project
  the OAuth client belongs to. Signing in again does not fix this one.
- `forbidden`, `rejected` (a 400: the request itself was wrong), `not_found`, `too_large`,
  `timeout`, `failed` — as they read.

`detail` is for the log and holds a status and a reason from a short list of Google's own
words, never a response body: a body can quote a token or a document back. The credential
is sent only to Google and to the token endpoint the file names, and never logged. A client
with no secret sends none, since an empty `client_secret` is refused where a missing one
may not be.
"""

import asyncio
import codecs
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx

from keryx.config import Settings
from keryx.integrations import google
from keryx.integrations.google import TOKEN_URL
from keryx.logging_util import quiet_http_loggers

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"

REQUEST_TIMEOUT_S = 15.0
#: Making the connection gets less: a host that cannot be reached is known quickly, and
#: known to be a request that was never sent.
CONNECT_TIMEOUT_S = 5.0
#: One line of somebody else's text, clipped: a title, a place, a file's name.
TEXT_CHARS = 120
BYTE_ORDER_MARK = "\ufeff"
#: Refresh the access token this long before Google says it expires.
EXPIRY_MARGIN_S = 60.0

ErrorCode = Literal[
    "not_signed_in",
    "not_connected",
    "signed_out",
    "api_disabled",
    "forbidden",
    "rejected",
    "not_found",
    "too_large",
    "timeout",
    "failed",
]

#: What Google calls each refusal worth telling apart, in the `reason` of an error's
#: `errors` or `details`, or in its `status`. Only these words ever reach a log line.
_REASONS: dict[str, ErrorCode] = {
    "accessNotConfigured": "api_disabled",
    "SERVICE_DISABLED": "api_disabled",
    "insufficientPermissions": "not_connected",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "not_connected",
    "exportSizeLimitExceeded": "too_large",
    "rateLimitExceeded": "failed",
    "userRateLimitExceeded": "failed",
    "RATE_LIMIT_EXCEEDED": "failed",
}

#: What the voice model is handed for each failure: what happened, and how to say it.
#: `{service}` is the tool's own word for what it reads ("Calendar", "Drive").
MESSAGES: dict[ErrorCode, str] = {
    "not_signed_in": (
        "Nobody is signed in to Google on this machine, so {service} can't be reached. Say "
        "that in one sentence; it needs `keryx auth login google` at the keyboard."
    ),
    "not_connected": (
        "{service} isn't part of the Google sign-in on this machine. Say that in one "
        "sentence; `keryx auth login google` at the keyboard adds it."
    ),
    "signed_out": (
        "The Google sign-in has stopped working, so {service} can't be reached. Say that in "
        "one sentence; it needs `keryx auth login google` at the keyboard."
    ),
    "api_disabled": (
        "The sign-in is fine, but the {service} API isn't enabled in the Google Cloud "
        "project it belongs to. Say that in one sentence; signing in again won't fix it, "
        "enabling that API in the project will."
    ),
    "forbidden": (
        "Google refused that. Say so in one sentence; it can be handed to Claude instead."
    ),
    "rejected": (
        "Google would not take that as it was put. Say so in one sentence; it can be handed "
        "to Claude instead."
    ),
    "not_found": "Google has nothing there. Say so in one sentence.",
    "too_large": (
        "That is too large to read on a call. Say so in one sentence; it can be handed to "
        "Claude instead."
    ),
    "timeout": (
        "{service} took too long, so it was stopped. Say so in one sentence; trying again "
        "may work, or it can be handed to Claude."
    ),
    "failed": "{service} didn't answer just now. Say so in one sentence; trying again may work.",
}


class GoogleApiError(Exception):
    """A Google request that failed in a way worth saying out loud.

    `code` is what went wrong, `detail` is for the log (never a credential, never a
    response body), and `status` is the HTTP status when Google itself answered.
    `unanswered` is the one case with no answer that is not a plain failure: the request
    went out and nothing came back (a read that timed out, a connection lost after
    sending), so Google may have acted on it.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str = "",
        *,
        status: int | None = None,
        unanswered: bool = False,
    ) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        self.unanswered = unanswered
        super().__init__(detail or code)

    @property
    def may_have_landed(self) -> bool:
        """Whether a write that ended this way might have been carried out all the same:
        it went out and was not answered, or Google answered with a fault of its own.

        False for everything known to have happened before the request was sent (no
        sign-in, a missing scope, a refused refresh, a connection never made) and for
        everything Google plainly refused.
        """
        return self.unanswered or (self.status is not None and self.status >= 500)

    def spoken(self, service: str) -> str:
        """The sentence the voice model is handed, for a tool that reads `service`."""
        return MESSAGES[self.code].format(service=service)


#: `not_connected`, when signing in again could not connect it either: Keryx's own Google
#: app does not offer the service, and only a client of the owner's does.
NOT_OFFERED = (
    "{service} isn't part of the Google sign-in on this machine, and Keryx's own Google "
    "app doesn't offer it yet. Say that in one sentence; it needs a Google client of "
    "their own, set up at the keyboard, and signing in again as things are won't add it."
)


def failure(exc: GoogleApiError, service: str, *, offered: bool = True) -> dict[str, str]:
    """What a tool returns for `exc`: a status and one sentence written to be spoken.

    `offered` is whether a sign-in on this machine could connect the service at all
    (`google_api.offered`, asked when the tool was built). When it could not, the
    sentence for a missing scope does not send the owner to sign in again.
    """
    if exc.code == "not_connected" and not offered:
        return {"status": exc.code, "message": NOT_OFFERED.format(service=service)}
    return {"status": exc.code, "message": exc.spoken(service)}


class Unclear(ValueError):
    """An argument the voice model gave that cannot be read. The message says what is
    wanted, and goes back to the model as an `error` it can fix by calling again."""


def clip(text: Any, limit: int = TEXT_CHARS) -> str:
    """Somebody else's text as one line of at most `limit` characters.

    Every title, place and file name a tool hands to the voice model goes through here: it
    is whatever an inviter or a sharer typed, so it arrives short, on one line, and beside
    a note that says to say it and never act on it.
    """
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def say_day(day: date, today: date) -> str:
    """`Monday 23 November`, with the year only when it is not this one."""
    said = f"{day:%A} {day.day} {day:%B}"
    return said if day.year == today.year else f"{said} {day.year}"


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _refusal(response: httpx.Response) -> ErrorCode | None:
    """Which refusal Google's error body names, from `_REASONS`; None when it names none."""
    error = _json(response).get("error")
    if not isinstance(error, dict):
        return None
    named = [error.get("status")]
    for key in ("errors", "details"):
        items = error.get(key)
        named += [i.get("reason") for i in items if isinstance(i, dict)] if isinstance(
            items, list
        ) else []
    return next((_REASONS[n] for n in named if isinstance(n, str) and n in _REASONS), None)


def _classify(response: httpx.Response) -> ErrorCode:
    status = response.status_code
    if status == 403:
        return _refusal(response) or "forbidden"
    if status == 404:
        return "not_found"
    if status == 400:
        return "rejected"
    return "failed"


class GoogleReader:
    """One Google API over HTTPS with the sign-in's refresh token: `get`, `text`, no more.

    `path` is the token file, read at every refresh, so a sign-in made since is the one
    used. `base` is the API's root, and every request is a path under it. `needs` is the
    scopes this tool cannot work without: a token whose saved scopes lack one is
    `not_connected` before anything is sent. Only a file with no list of scopes at all is
    sent as it is, for Google to judge: that is the Gmail-only file an older sign-in left,
    which `check_email` still reads. Keryx's own token always lists its scopes, and a
    plugin that asks `access_problem` first is refused before it gets here without them.

    The client is opened when first needed and closed by `aclose`, which a tool calls when
    it has answered: one question is one set of connections, and none outlives it. The
    access token is kept across them. Two questions may be in the air at once (a model can
    call a tool twice in one turn), so a close asked for while a request is still out
    waits for it: nothing is ever closed under a request.
    """

    def __init__(
        self,
        path: Path,
        base: str,
        *,
        needs: Sequence[str] = (),
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._base = base
        self._needs = tuple(needs)
        self._client = client
        #: A client handed in is its owner's to close.
        self._owns_client = client is None
        self._clock = clock
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self._close_due = False
        # Every address this sends is somebody's: a calendar's id, a search, a file's id.
        # The entry points keep the HTTP client from logging them; so does this, for a
        # process that set its logging up some other way.
        quiet_http_loggers()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the connections this opened, once no request is out on them. The next
        request opens them again."""
        if not self._owns_client or self._client is None:
            return
        if self._in_flight:
            self._close_due = True
            return
        client, self._client = self._client, None
        await client.aclose()

    @contextlib.asynccontextmanager
    async def _request(self) -> AsyncIterator[None]:
        """One request out: a close asked for meanwhile is carried out when it is back."""
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            if self._close_due and not self._in_flight:
                self._close_due = False
                await self.aclose()

    async def _token(self, *, force: bool = False) -> str:
        async with self._lock:
            if self._access and not force and self._clock() < self._expires_at:
                return self._access
            try:
                creds = json.loads(self._path.read_text(encoding="utf-8"))
                data = {
                    "client_id": creds["client_id"],
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                }
                if creds.get("client_secret"):
                    data["client_secret"] = creds["client_secret"]
                token_url = creds.get("token_uri") or TOKEN_URL
                scopes = creds.get("scopes")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise GoogleApiError("not_signed_in", f"unreadable {self._path.name}") from exc
            if isinstance(scopes, list) and not google.holds(scopes, self._needs):
                raise GoogleApiError("not_connected", "the sign-in lacks a scope this needs")
            try:
                response = await self._http().post(token_url, data=data)
            except httpx.HTTPError as exc:
                raise _unreachable("token refresh", exc) from exc
            if response.status_code != 200:
                code = _json(response).get("error", response.status_code)
                if code in ("invalid_grant", "unauthorized_client", "invalid_client"):
                    raise GoogleApiError("signed_out", f"token refresh refused: {code}")
                raise GoogleApiError("failed", f"token refresh: HTTP {response.status_code}")
            body = _json(response)
            self._access = body.get("access_token")
            if not self._access:
                raise GoogleApiError("failed", "token refresh returned no access token")
            lifetime = float(body.get("expires_in", 3600))
            self._expires_at = self._clock() + lifetime - EXPIRY_MARGIN_S
            return self._access

    async def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None = None,
        *,
        stream: bool = False,
        sending: Callable[[], None] | None = None,
    ) -> httpx.Response:
        """One authorized request; a 2xx response, or the `GoogleApiError` it came to.

        With `stream` the body has not been read: the caller reads what it wants and closes
        it. `sending` is called just before each attempt is handed to the network, after
        the token is in hand, so its caller knows nothing went out before that.
        """
        # The log names the kind of thing asked for and never which: the rest of a path is
        # an id, and a calendar's id is somebody's address. (The HTTP client's own request
        # log would name it in full; `logging_util.configure_logging` keeps that quiet.)
        what = path.split("/", 1)[0]
        for attempt in range(2):
            token = await self._token(force=attempt > 0)
            client = self._http()
            request = client.build_request(
                method,
                f"{self._base}/{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if sending is not None:
                sending()
            try:
                response = await client.send(request, stream=stream)
            except httpx.HTTPError as exc:
                raise _unreachable(what, exc, sent=True) from exc
            status = response.status_code
            if 200 <= status < 300:
                return response
            try:
                await response.aread()
            except httpx.HTTPError as exc:
                raise _unreachable(what, exc, sent=True) from exc
            finally:
                await response.aclose()
            if status == 401:
                if attempt == 0:
                    continue  # an access token revoked early: refresh once, then believe it
                raise GoogleApiError("signed_out", f"{what}: HTTP 401 after a refresh", status=401)
            code = _classify(response)
            raise GoogleApiError(code, f"{what}: HTTP {status} ({code})", status=status)
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        async with self._request():
            return _json(await self._send("GET", path, params))

    async def text(
        self, path: str, params: Mapping[str, Any] | None = None, *, limit: int
    ) -> tuple[str, bool]:
        """A response that is text rather than JSON (an export), cut to `limit` characters.

        Returns the text and whether it was cut. The body is read as it arrives and no
        further than the cut: a file of any size costs the memory of `limit` characters,
        and the connection is closed on what was not read.
        """
        # UTF-8 at its widest is four bytes a character; a byte-order mark is three more.
        budget = limit * 4 + 3
        received = bytearray()
        async with self._request():
            response = await self._send("GET", path, params, stream=True)
            try:
                async for chunk in response.aiter_bytes():
                    received += chunk
                    if len(received) > budget:
                        break
            except httpx.HTTPError as exc:
                raise _unreachable(path.split("/", 1)[0], exc, sent=True) from exc
            finally:
                await response.aclose()
        cut = len(received) > budget
        # Bytes that are not UTF-8 are shown as damage, wherever they are. A cut that
        # falls inside a character is the one exception: an incremental decoder holds
        # an unfinished character back, and it is told the text is over only when it is.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(bytes(received[:budget]), final=not cut)
        text = text.lstrip(BYTE_ORDER_MARK)
        return text[:limit], cut or len(text) > limit


class GoogleWriter(GoogleReader):
    """A `GoogleReader` that can also `post`: the one class a tool that writes is built on."""

    async def post(
        self,
        path: str,
        body: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        sending: Callable[[], None] | None = None,
    ) -> dict:
        """`sending` is called once the request is about to leave: before it, a failure is
        known to have created nothing."""
        async with self._request():
            return _json(await self._send("POST", path, params, body, sending=sending))


#: The failures of a connection that was never made: nothing was sent.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
               httpx.UnsupportedProtocol, httpx.ProxyError)  # fmt: skip


def _unreachable(what: str, exc: httpx.HTTPError, *, sent: bool = False) -> GoogleApiError:
    """A request with no answer. `sent` says it was a request to the API itself, which was
    then either never connected or, failing any other way, is `unanswered`."""
    code: ErrorCode = "timeout" if isinstance(exc, httpx.TimeoutException) else "failed"
    unanswered = sent and not isinstance(exc, _NEVER_SENT)
    return GoogleApiError(code, f"{what}: {type(exc).__name__}", unanswered=unanswered)


def access_problem(settings: Settings, service: str, needs: Sequence[str]) -> str | None:
    """Why the sign-in cannot serve a tool that needs `needs`, in a sentence; None when it
    can. `service` is the name of the Google service those scopes belong to.

    The sentence names what fixes it, which is not always signing in again: Keryx's own
    Google app may not offer the service yet, and then only a client of the owner's does.
    """
    token = google.read_token(settings)
    if token is None:
        return "not signed in to Google: `keryx auth login google`"
    if not google.holds(token.scopes, needs):
        label = google.SERVICES[service].label
        if not offered(settings, service):
            return (
                f"{label} is not part of your Google sign-in, and Keryx's Google app does "
                "not offer it yet: `keryx auth login google --client-file FILE` with a "
                "Google client of your own"
            )
        return f"{label} is not part of your Google sign-in: `keryx auth login google`"
    return None


def offered(settings: Settings, service: str) -> bool:
    """Whether a sign-in on this machine could connect `service` at all: the client it
    would use offers it. True with no client to ask, since making one offers everything."""
    client = google.resolve_client(settings)
    return client is None or service in client.services
