"""Google, done once: one sign-in that connects everything Keryx does with a Google account.

`keryx auth login google` and the wizard's Google section both run the flow here. It is an
OAuth authorization-code grant with PKCE (S256) and `state`, asking for the scopes of the
chosen services (`keryx.integrations.google.SERVICES`, the only place a scope is written),
and it ends one of two ways, chosen before the link is made:

- **The browser is on this machine** (`browser_signin`). A listener is bound on
  `127.0.0.1`, on a port the OS picks, and its address is the redirect. The browser is
  opened on the listener's own `/start`, which sends it on to Google, once: a command line
  is readable by every user of the machine, and the link itself carries this sign-in's
  `state`. The link is always shown on the terminal as well. Google sends the browser back
  to the listener, which answers with a line of text saying the tab can be closed and is
  shut. It takes only the request that carries this sign-in's `state`, and logs nothing,
  because the request line holds the one-time code.
- **The browser is somewhere else** (`start_signin`, then `finish_signin`): a machine
  reached over ssh, or a coding agent relaying the link. The redirect is a loopback address
  nothing listens on, so the browser lands on a page that does not load, and the owner
  pastes that address back. The verifier and state wait in `DATA_DIR/google_signin.json`
  (0600), so the two halves can be two commands.

Either way `_complete` exchanges the code (with the verifier, which never left this
machine), keeps exactly the scopes Google says it granted — the owner may untick any of
them — and writes the one token (`google.write_token`), its copy for the agents' MCP
server, and nothing else. Saving the address and turning the agents' tools on are the
caller's, because only the owner's commands write settings.

The OAuth client is the owner's own when they have made one, else the one Keryx publishes
(`google.resolve_client`). `guides/google.md` is how an owner makes their own, and
`guides/google_consent.md` is what the consent screen asks; `run_section` is the wizard's
screen, and `connect` is the part of it the `check_email` plugin's step reuses.
"""

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from keryx.config import GOOGLE_CLIENT_FILE, Settings, parse_google_client, write_private
from keryx.integrations import google
from keryx.integrations.gmail import legacy_token_path
from keryx.integrations.google import PENDING_FILE, SERVICES, GoogleToken, OAuthClient, labels

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.setup.context import SetupContext

log = logging.getLogger("keryx.setup.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
#: The only address the listener binds: this machine, and nothing that can be reached.
LOOPBACK = "127.0.0.1"
#: Where Google sends a browser that is somewhere else. Nothing listens there: the code is
#: read out of the address bar instead, so this works from a phone while Keryx runs headless.
PASTE_REDIRECT_URI = "http://localhost:1"
#: How long the listener waits for the browser to come back.
CALLBACK_TIMEOUT_S = 300.0
#: How long one connection to the listener may take, start to finish, so a stalled or
#: dribbling one cannot hold it.
CALLBACK_READ_TIMEOUT_S = 5.0
#: How often the wait looks for the answer a request's thread has set.
CALLBACK_POLL_S = 0.1
#: Where the browser is first sent: the listener's own address, which redirects to Google.
START_PATH = "/start"
#: What the listener answers itself with when its start page is asked for a second time.
OPENED_TWICE = "opened_twice"
REQUEST_TIMEOUT_S = 30.0

SIGN_IN = "`keryx auth login google`"
CLOSE_PAGE = "Keryx has Google's answer. You can close this tab and go back to the terminal.\n"
REFUSED_PAGE = "Google did not grant access. You can close this tab and go back to the terminal.\n"
TWICE_PAGE = (
    "This sign-in was already opened once, so Keryx has stopped it. Go back to the terminal "
    "and start it again.\n"
)

Announce = Callable[[str, bool], None]


class GoogleSetupError(RuntimeError):
    """A sign-in that cannot go on, in one sentence to print."""


# --- the guides ---------------------------------------------------------------------------

GUIDE = "google.md"
CONSENT_GUIDE = "google_consent.md"


def guide_path(name: str = GUIDE) -> Any:
    return resources.files("keryx.setup.guides").joinpath(name)


def guide(name: str = GUIDE) -> str:
    """A guide's text, shared with the wiki: making a client of your own, or the consent."""
    return guide_path(name).read_text(encoding="utf-8")


def own_client_hint() -> str:
    return f"`keryx auth login google --client-file FILE`; the steps are in {guide_path()}"


# --- the OAuth client ---------------------------------------------------------------------


def install_client_file(settings: Settings, source: Path) -> Path:
    """Copy a downloaded OAuth client JSON to `KERYX_HOME/google_client_secret.json` (0600).

    Validated first — the `installed` (desktop) or `web` shape — so a wrong file is refused
    here rather than at the first sign-in. Returns where it now is; the caller points
    `GOOGLE_CLIENT_SECRETS_FILE` at it.
    """
    path = source.expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoogleSetupError(f"could not read {path}: {exc.strerror or exc}") from None
    try:
        parse_google_client(text)
    except ValueError as exc:
        raise GoogleSetupError(f"{path} is not a Google OAuth client file: {exc}") from None
    return write_private(settings.config_dir / GOOGLE_CLIENT_FILE, text)


@dataclass(frozen=True)
class Offer:
    """What one sign-in will ask for, and with whose client."""

    client: OAuthClient
    services: tuple[str, ...]
    #: Services left out because Keryx's published client is not approved for them yet.
    withheld: tuple[str, ...] = ()

    @property
    def note(self) -> str | None:
        """One line on what is left out and how to get it; None when nothing is."""
        if not self.withheld:
            return None
        one = len(self.withheld) == 1
        return (
            f"{labels(self.withheld)} {'is' if one else 'are'} not offered by Keryx's Google "
            f"app yet: {'it needs' if one else 'they need'} your own Google client "
            f"({own_client_hint()})."
        )


def offer(settings: Settings, wanted: Sequence[str] | None = None) -> Offer:
    """What a sign-in for `wanted` asks for: every service the client offers when None.

    Raises with what to do when there is no client at all, or when a service asked for by
    name is one Keryx's published client may not request.
    """
    client = google.resolve_client(settings)
    if client is None:
        raise GoogleSetupError(
            "there is no Google OAuth client to sign in with: make one of your own and "
            f"pass it ({own_client_hint()})"
        )
    refused = [name for name in wanted or () if name not in client.services]
    if refused or not client.services:
        what = labels(refused) if refused else "any service"
        raise GoogleSetupError(
            f"Keryx's Google app does not offer {what} yet: that needs your own Google "
            f"client ({own_client_hint()})"
        )
    if wanted is None:
        return Offer(client, client.services, client.withheld)
    return Offer(client, tuple(name for name in SERVICES if name in wanted))


# --- the consent link ---------------------------------------------------------------------


@dataclass(frozen=True)
class Pending:
    """A sign-in whose link is out: what its answer has to match."""

    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    services: tuple[str, ...]


def _begin(settings: Settings, chosen: Offer, redirect_uri: str) -> tuple[Pending, str]:
    """A fresh sign-in for `chosen` that comes back to `redirect_uri`, and its consent link."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    pending = Pending(
        state=secrets.token_urlsafe(24),
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=chosen.client.client_id,
        services=chosen.services,
    )
    query = {
        "response_type": "code",
        "client_id": pending.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(google.requested_scopes(chosen.services)),
        "state": pending.state,
        "code_challenge": challenge.decode().rstrip("="),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    if settings.user_google_email:
        query["login_hint"] = settings.user_google_email
    return pending, f"{AUTH_URL}?{urlencode(query)}"


def _pending_path(settings: Settings) -> Path:
    return settings.data_dir / PENDING_FILE


def start_signin(settings: Settings, chosen: Offer | None = None) -> str:
    """The consent link for a browser somewhere else; its verifier and state wait in
    `DATA_DIR/google_signin.json` (0600) for `finish_signin`."""
    pending, url = _begin(settings, chosen or offer(settings), PASTE_REDIRECT_URI)
    write_private(_pending_path(settings), json.dumps(asdict(pending)))
    return url


def finish_signin(
    settings: Settings,
    redirected_to: str,
    *,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "SignInResult":
    """Finish the sign-in `start_signin` began, from the address the browser landed on."""
    try:
        saved = json.loads(_pending_path(settings).read_text(encoding="utf-8"))
        pending = Pending(**{**saved, "services": tuple(saved["services"])})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GoogleSetupError(f"no sign-in in progress: run {SIGN_IN} first") from exc
    try:
        query = parse_qs(urlparse(redirected_to.strip().strip("'\"")).query)
    except ValueError:
        raise GoogleSetupError("that is not an address; copy the whole address bar") from None
    answer = {key: values[0] for key, values in query.items()}
    return _complete(settings, pending, answer, post=post, get=get)


def discard_pending(settings: Settings) -> None:
    """Forget a pasted-back sign-in that will not be finished: its verifier is spent."""
    _pending_path(settings).unlink(missing_ok=True)


# --- the listener, for a browser on this machine ------------------------------------------


def _same_state(given: str, expected: str | None) -> bool:
    """Whether a redirect carries this sign-in's `state`, compared in constant time."""
    return bool(expected) and secrets.compare_digest(given.encode(), str(expected).encode())


class _Server(ThreadingHTTPServer):
    """The listener's socket: the `state` it is waiting for, and the answer once it has it.

    Every connection is served on a thread of its own. Served one at a time, a few
    connections held open and silent would keep the owner's browser queued behind them,
    and "the second request for `/start` stops the sign-in" would never get to run.
    """

    daemon_threads = True
    state: str | None = None
    answer: dict[str, str] | None = None
    #: Where `/start` sends the browser, and whether it has been asked for already.
    start_url: str | None = None
    started: bool = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Nothing: a request that broke is waited past like any other stray, and the
        default would put a traceback on the terminal in the middle of a sign-in."""


class _Callback(BaseHTTPRequestHandler):
    """The two requests a sign-in makes of the listener: `GET /start`, which sends the
    browser to Google, and Google's redirect back, `GET /?code=…&state=…`."""

    timeout = CALLBACK_READ_TIMEOUT_S

    def setup(self) -> None:
        # The socket timeout above is per read, so a client that sends a byte now and then
        # would never trip it. This ends the connection on the clock instead.
        self._cutoff = threading.Timer(CALLBACK_READ_TIMEOUT_S, self._cut)
        self._cutoff.daemon = True
        self._cutoff.start()
        super().setup()

    def _cut(self) -> None:
        with contextlib.suppress(OSError):
            self.request.shutdown(socket.SHUT_RDWR)

    def finish(self) -> None:
        self._cutoff.cancel()
        with contextlib.suppress(OSError):  # a connection the cutoff has already ended
            super().finish()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        answer = {key: values[0] for key, values in parse_qs(url.query).items()}
        server: Any = self.server
        if url.path == START_PATH:
            self._start(server)
        elif url.path not in ("", "/"):
            self._say(404, "Not found.\n")
        elif not _same_state(answer.get("state", ""), getattr(server, "state", None)):
            self._say(400, "This is not the sign-in Keryx is waiting for.\n")
        else:
            with server.lock:  # the first answer is the answer, whichever thread has it
                taken = server.answer is None
                if taken:
                    server.answer = answer
            if not taken:
                self._say(409, "This sign-in has already ended.\n")
            else:
                self._say(200, REFUSED_PAGE if "error" in answer else CLOSE_PAGE)

    def _start(self, server: Any) -> None:
        """Send the browser on to Google, once.

        The second time it is asked for, somebody else has the link: the browser Keryx
        opened, and something that read its command line. Nobody can tell which came
        first, so the sign-in is stopped rather than finished for the wrong one.
        """
        with server.lock:
            target = getattr(server, "start_url", None)
            again = bool(target) and server.started
            if target:
                server.started = True
            if again and server.answer is None:
                server.answer = {"error": OPENED_TWICE}
        if not target:
            self._say(404, "Not found.\n")
        elif again:
            self._say(410, TWICE_PAGE)
        else:
            self._say(302, "Redirecting to Google.\n", location=target)

    def _say(self, status: int, text: str, *, location: str | None = None) -> None:
        body = text.encode()
        self.send_response(status)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Nothing: the request line carries the one-time code."""


class Listener:
    """A loopback HTTP server that lives until one sign-in's redirect has arrived."""

    def __init__(self, server: Any) -> None:
        self._server = server
        server.state = None
        server.answer = None
        #: Requests are served on threads: what they decide and set, they do under this.
        server.lock = threading.Lock()

    @property
    def redirect_uri(self) -> str:
        return f"http://{LOOPBACK}:{self._server.server_address[1]}"

    def start_page(self, url: str) -> str:
        """The address to open in the browser, which redirects to the consent link `url`
        once: a link with nothing in it worth reading off a command line."""
        self._server.start_url, self._server.started = url, False
        return self.redirect_uri + START_PATH

    def wait(
        self, state: str, timeout_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> dict[str, str]:
        """The redirect's query, once a request carrying `state` arrives; anything else is
        answered with an error and waited past."""
        self._server.state = state
        deadline = clock() + timeout_s
        while self._server.answer is None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise GoogleSetupError(
                    f"the browser did not come back within {timeout_s / 60:.0f} minutes; "
                    f"run {SIGN_IN} again, with --headless if the browser is on another "
                    "machine"
                )
            # Each call hands at most one connection to a thread and comes back, so the
            # answer a thread sets is seen within a poll.
            self._server.timeout = min(remaining, CALLBACK_POLL_S)
            self._server.handle_request()
        return self._server.answer

    def close(self) -> None:
        self._server.server_close()


def listen() -> Listener:
    """Bind the listener: this machine only, on a port the OS picks."""
    try:
        return Listener(_Server((LOOPBACK, 0), _Callback))
    except OSError as exc:
        raise GoogleSetupError(
            f"could not listen on {LOOPBACK} ({exc.strerror or exc}); run {SIGN_IN} "
            "--headless to paste the address back instead"
        ) from None


def open_browser(url: str) -> bool:
    """Open `url` in this machine's browser; False when there is none to open."""
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False


def browser_signin(
    settings: Settings,
    chosen: Offer | None = None,
    *,
    announce: Announce,
    timeout_s: float = CALLBACK_TIMEOUT_S,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "SignInResult":
    """The whole sign-in with a browser on this machine: listen, open, wait, exchange.

    The browser is opened on the listener's start page, never on the consent link itself:
    a browser's command line can be read by every user of the machine, and the link holds
    this sign-in's `state`. `announce(link, opened)` is told the link before the wait,
    opened or not, so it can always be shown on the terminal. `listen` and `open_browser`
    are looked up when called, which is how the tests keep this from binding a socket or
    opening anything.
    """
    chosen = chosen or offer(settings)
    listener = listen()
    try:
        pending, url = _begin(settings, chosen, listener.redirect_uri)
        announce(url, open_browser(listener.start_page(url)))
        answer = listener.wait(pending.state, timeout_s)
    finally:
        listener.close()
    return _complete(settings, pending, answer, post=post, get=get)


# --- the exchange -------------------------------------------------------------------------


@dataclass(frozen=True)
class SignInResult:
    """What a finished sign-in came to."""

    email: str
    #: The services whose every scope was granted.
    connected: tuple[str, ...]
    #: The services asked for that were not: unticked on Google's screen.
    missing: tuple[str, ...]
    path: Path
    #: Whose client it was signed in with, for the sentence that says so.
    whose: str


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _id_token_email(id_token: Any) -> str:
    """The `email` claim of an id token. Its signature is not checked: it came straight
    from Google's token endpoint over TLS, which is what vouches for it."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return ""
    return str(claims.get("email") or "") if isinstance(claims, dict) else ""


def _userinfo_email(access_token: Any, get: Callable[..., Any]) -> str:
    """The signed-in address from the userinfo endpoint, when the id token did not say."""
    if not access_token:
        return ""
    try:
        response = get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return ""
    return str(_json(response).get("email") or "") if response.status_code == 200 else ""


def _complete(
    settings: Settings,
    pending: Pending,
    answer: Mapping[str, str],
    *,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> SignInResult:
    """Exchange the code in Google's `answer` and keep what was granted.

    `post` and `get` are `httpx`'s, looked up when called so that nothing bound at import
    time can reach the network from a test that patched them.
    """
    post, get = post or httpx.post, get or httpx.get
    if answer.get("error") == OPENED_TWICE:
        raise GoogleSetupError(
            "the sign-in page was opened twice on this machine, so it was stopped and "
            f"nothing was saved; run {SIGN_IN} again, or with --headless to paste the "
            "address back instead"
        )
    if answer.get("error"):
        raise GoogleSetupError(f"Google said: {answer['error']}")
    if not _same_state(answer.get("state", ""), pending.state):
        raise GoogleSetupError(
            f"that address belongs to a different sign-in; start again with {SIGN_IN}"
        )
    if not answer.get("code"):
        raise GoogleSetupError("that address has no code in it; copy the whole address bar")
    client = google.resolve_client(settings)
    if client is None or client.client_id != pending.client_id:
        raise GoogleSetupError(
            f"the Google client changed since the link was made; start again with {SIGN_IN}"
        )
    data = {
        "code": answer["code"],
        "client_id": client.client_id,
        "redirect_uri": pending.redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": pending.code_verifier,
    }
    if client.client_secret:  # an empty one is refused where a missing one may not be
        data["client_secret"] = client.client_secret
    try:
        response = post(google.TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise GoogleSetupError(f"could not reach Google ({type(exc).__name__})") from None
    body = _json(response)
    if response.status_code != 200 or not body.get("refresh_token"):
        raise GoogleSetupError(
            f"Google refused the code ({body.get('error', response.status_code)}); "
            f"start again with {SIGN_IN}"
        )
    # What Google says it granted, which granular consent can make less than was asked.
    granted = str(body.get("scope") or answer.get("scope") or "").split()
    scopes = tuple(granted) or google.requested_scopes(pending.services)
    email = _id_token_email(body.get("id_token")) or _userinfo_email(
        body.get("access_token"), get
    )
    token = GoogleToken(
        refresh_token=body["refresh_token"],
        client_id=client.client_id,
        client_secret=client.client_secret,
        scopes=scopes,
        email=email,
    )
    if not token.connected:
        raise GoogleSetupError(
            "nothing was granted: tick what Keryx may use on Google's screen and try again"
        )
    if not email:
        raise GoogleSetupError(f"Google did not say which account signed in; try {SIGN_IN} again")
    path = google.write_token(settings, token)
    google.export_for_mcp(settings, token, only=True)
    # One token now. The Gmail-only one an older sign-in left is spent once this one reads
    # Gmail; until it does, that file is still what `check_email` answers from.
    if token.reads_gmail:
        legacy_token_path(settings).unlink(missing_ok=True)
    discard_pending(settings)
    connected = token.connected
    return SignInResult(
        email=email,
        connected=connected,
        missing=tuple(name for name in pending.services if name not in connected),
        path=path,
        whose=client.whose,
    )


def summary(result: SignInResult) -> list[str]:
    """One or two lines for the owner: who signed in, what is connected, and what is not."""
    lines = [f"Signed in as {result.email}. Connected: {labels(result.connected)}."]
    if result.missing:
        # A document service is not connected without Drive, whatever was ticked for it.
        built_on = [n for n in result.missing if set(SERVICES[n].needs) & set(result.missing)]
        lines.append(
            f"Not connected: {labels(result.missing)}, not granted on Google's screen"
            + (f" ({labels(built_on)} need{'s' * (len(built_on) == 1)} Drive as well)"
               if built_on else "")
            + f"; {SIGN_IN} again to add {'it' if len(result.missing) == 1 else 'them'}."
        )
    return lines


def service_lines(connected: Sequence[str], offered: Sequence[str]) -> list[tuple[bool, str]]:
    """One line per service on offer: whether it is connected, and what that enables."""
    return [
        (name in connected, f"{SERVICES[name].label}: "
         + (f"connected — {SERVICES[name].enables}" if name in connected else "not connected"))
        for name in SERVICES
        if name in offered or name in connected
    ]


# --- the wizard section -------------------------------------------------------------------

SKIP, SIGN_IN_NOW = "skip", "signin"
HERE, ELSEWHERE = "here", "elsewhere"


def run_section(ctx: "SetupContext") -> None:
    """One sign-in for everything Google: asked nothing when everything on offer is connected."""
    ui = ctx.ui
    ui.note("One Google sign-in connects Gmail, Calendar, Drive, Docs, Sheets and Slides: "
            "for the agents, and for email answers on a call.")
    connect(ctx)


def connect(ctx: "SetupContext") -> None:
    """Sign in to Google, unless everything on offer is connected already.

    The same question whether or not somebody is signed in, so going back through the
    wizard replays its answer, and the sign-in itself is a probe that is never run twice.
    """
    from keryx.setup.ui import Choice

    ui, settings = ctx.ui, ctx.settings
    token = google.read_token(settings)
    client = google.resolve_client(settings)
    offered = client.services if client is not None else tuple(SERVICES)
    done = token is not None and all(name in token.connected for name in offered)
    if token is not None:
        _show(ctx, token.connected, offered, f"Google: signed in as {token.email or 'you'}")
    if done and not ctx.review:
        return
    # With no client to hand, signing in starts with making one in the Google Cloud
    # console: worth it by default only for Codex, whose tasks have no Google otherwise.
    wanted = client is not None or "codex" in settings.enabled_agents
    choice = ui.select(
        "Sign in to Google?",
        [
            Choice(SKIP, "Keep this sign-in" if token is not None else "Skip for now"),
            Choice(SIGN_IN_NOW, "Sign in again" if token is not None else "Sign in",
                   hint="one consent screen"),
        ],
        default=SIGN_IN_NOW if wanted and not done else SKIP,
    )
    if choice == SKIP:
        if token is None:
            ui.note(f"Left for later: `keryx setup` comes back to it, or {SIGN_IN}.")
        return
    if client is None and not _own_client(ctx):
        return
    result = _sign_in(ctx)
    if result is None:
        return
    ui.success(f"Google: signed in as {result.email} with {result.whose}")
    _show(ctx, result.connected, [*result.connected, *result.missing])
    _keep(ctx, result)


def _show(
    ctx: "SetupContext", connected: Sequence[str], offered: Sequence[str], heading: str = ""
) -> None:
    if heading:
        ctx.ui.success(heading)
    for ok, line in service_lines(connected, offered):
        (ctx.ui.success if ok else ctx.ui.warn)(line)


def _own_client(ctx: "SetupContext") -> bool:
    """The owner's own Google Cloud client, asked for only when Keryx has none to offer."""
    ui = ctx.ui
    ui.markdown(guide())
    while True:
        raw = ui.text("Path to the downloaded client JSON (blank to stop here)")
        if not raw:
            return False
        try:
            path = install_client_file(ctx.settings, Path(raw.strip().strip("'\"")))
        except GoogleSetupError as exc:
            ui.error(str(exc))
            continue
        ui.success(f"client saved to {path}")
        return ctx.save({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})


def _sign_in(ctx: "SetupContext") -> SignInResult | None:
    """Run the sign-in the way that fits this machine; another way, or none, if it fails."""
    from keryx.setup.ui import Choice

    ui = ctx.ui
    try:
        chosen = offer(ctx.settings)
    except GoogleSetupError as exc:
        ui.error(str(exc))
        return None
    if chosen.note:
        ui.note(chosen.note)
    if chosen.client.published:
        ui.markdown(guide(CONSENT_GUIDE))
    way = ELSEWHERE if ctx.probes.headless() else HERE
    try:
        while True:
            # Each probe hands a failure back as a value, never raises it: a result is
            # recorded, so going back through the wizard replays it instead of running the
            # sign-in a second time.
            if way == HERE:
                result = ctx.probes.google_browser_signin(ctx.settings, chosen, _announcer(ctx))
            else:
                url = start_signin(ctx.settings, chosen)
                ui.panel(
                    "Sign in to Google — open this on any device",
                    f"{url}\n\nApprove it. The page you land on will not load: copy its "
                    "whole address and paste it below.",
                )
                address = ui.text("The address you landed on (blank to skip)")
                if not address:
                    return None
                result = ctx.probes.google_finish(ctx.settings, address)
            if isinstance(result, SignInResult):
                return result
            ui.error(str(result))
            way = ui.select(
                "Try the sign-in again?",
                [
                    Choice(HERE, "In a browser on this machine"),
                    Choice(ELSEWHERE, "In a browser somewhere else",
                           hint="paste the address back"),
                    Choice(SKIP, "Skip for now"),
                ],
                default=ELSEWHERE,
            )
            if way == SKIP:
                return None
    finally:
        # Finished or given up, nothing of a pasted-back sign-in waits on disk.
        discard_pending(ctx.settings)


def _announcer(ctx: "SetupContext") -> Announce:
    def announce(url: str, opened: bool) -> None:
        ctx.ui.panel(
            "Sign in to Google",
            ("Your browser is opening this link" if opened else "Open this link in a browser "
             "on this machine")
            + f":\n\n{url}\n\nWaiting for you to finish there. Ctrl-C stops waiting.",
        )

    return announce


def agents_switched_off(settings: Settings, stored: Mapping[str, Any]) -> bool:
    """Whether the owner has turned the agents' Google tools off on purpose: the setting is
    false because somebody stored it, not because nobody has signed in yet."""
    return not settings.google_workspace_mcp and "GOOGLE_WORKSPACE_MCP" in stored


def _keep(ctx: "SetupContext", result: SignInResult) -> None:
    """Save the address the agents act as, and turn their Google tools on the first time."""
    ui, settings = ctx.ui, ctx.settings
    current = settings.user_google_email
    if current != result.email:
        if not current or ui.confirm(
            f"You signed in as {result.email}, and your Google address is saved as "
            f"{current}. Save {result.email} instead?",
            default=True,
        ):
            ctx.save({"USER_GOOGLE_EMAIL": result.email})
        else:
            ui.warn(f"Agents act as {result.email}, the account that signed in, whatever is "
                    f"saved. To use {current}, sign in again as that account.")
    if agents_switched_off(settings, ctx.store.stored()):
        ui.note("Agents' Google tools stay off, as you set them: `keryx config set "
                "GOOGLE_WORKSPACE_MCP true` turns them on.")
    elif not settings.google_workspace_mcp:
        ctx.save({"GOOGLE_WORKSPACE_MCP": True})
        ui.note("Agents get the Google tools from the next task (`keryx restart` if Keryx is "
                "running). `keryx config set GOOGLE_WORKSPACE_MCP false` turns them off.")
