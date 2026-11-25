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

The OAuth client is always the owner's own (`google.resolve_client`): Keryx ships none,
because Google's API Terms forbid developer credentials in an open source project.
`guides/google.md` is how an owner makes one, and `guides/google_consent.md` is what the
consent screen asks; `run_section` is the wizard's screen, and `connect` is the part of it
the `check_email` plugin's step reuses.
"""

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
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
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
#: Where an owner ends a grant by hand, when Keryx could not.
PERMISSIONS_URL = "https://myaccount.google.com/permissions"
#: How much of an error code somebody else wrote is ever printed.
MAX_ERROR_CHARS = 80

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


class GoogleStopped(GoogleSetupError):
    """The questions for the client file ended without one: the owner left, or an answer
    was wrong too many times. Nothing was saved, and the sentence says how to pick it up."""


# --- the guides ---------------------------------------------------------------------------

GUIDE = "google.md"
CONSENT_GUIDE = "google_consent.md"


def guide_path(name: str = GUIDE) -> Any:
    return resources.files("keryx.setup.guides").joinpath(name)


def guide(name: str = GUIDE) -> str:
    """A guide's text, shared with the wiki: making a client of your own, or the consent."""
    return guide_path(name).read_text(encoding="utf-8")


def plain(text: str) -> str:
    """A guide as plain lines for a terminal that shows no Markdown: no heading, no bold."""
    lines = [line for line in text.replace("**", "").splitlines() if not line.startswith("#")]
    return "\n".join(lines).strip()


# --- making the owner's own client --------------------------------------------------------
#
# Google's API Terms forbid shipping a client in an open source project, so every owner
# makes one. Google's Personal mode page does it in one go: a project, the Workspace APIs
# and a Desktop client that only the account that made it can use. `keryx auth login
# google` and the wizard both open it, show the clicks, and ask for the file it downloads.

#: Google's page that makes a Personal mode project and a Desktop client at once.
PERSONAL_MODE_URL = (
    "https://console.cloud.google.com/auth/overview/personal"
    "?apis=ws,wsmcp,tasksmcp&clientType=desktop&isAgent=true"
)
#: Google's API Terms, whose section 4(b) is why there is no shared client.
TERMS_URL = "https://developers.google.com/terms"
#: What Google names the file it downloads.
CLIENT_GLOB = "client_secret_*.json"
#: Typed at the path question instead of a path, to paste what is in the file, hidden.
PASTE = "paste"
#: How many wrong answers the path question takes before it stops.
MAX_WRONG = 3

WHY_OWN_CLIENT = (
    "Google's terms do not let open-source apps like Keryx ship a shared sign-in key, so "
    "you make your own, once. Google's Personal mode makes it in about a minute, and it "
    "works only for your account: no \"unverified app\" warning, and no expiry."
)
#: The clicks on that page, in order, as Google's own steps for it have them
#: (developers.google.com/workspace/guides/configure-mcp-servers): those end with copying
#: the Client ID and Client Secret, and the console may offer the file as well.
CLIENT_STEPS = (
    "Sign in as the account Keryx should act as, and pick or create the project it offers.",
    "Give the client any name, tick the box to agree, then choose Continue.",
    "Choose Download JSON if it is offered, or copy the Client ID and the Client Secret: "
    "Google shows the secret only once.",
)
#: Said when the browser is on another machine, so the download is not on this one.
ELSEWHERE_NOTE = (
    f"Downloaded it on another machine? Copy it to this one first (scp works), type {PASTE} "
    "at the question below to paste what is in it, hidden, or give the Client ID."
)
PATH_QUESTION = "The file you downloaded, or the Client ID"
PASTE_QUESTION = "Paste what is in the file, on one line (hidden)"
SECRET_QUESTION = "Client Secret (hidden)"
#: Typed at the path question to stop there, in the wizard and the command alike.
SKIP_WORD = "skip"
#: What a Client ID looks like: never a path, so an answer that is one is taken as one.
CLIENT_ID = re.compile(r"[\w.-]+\.apps\.googleusercontent\.com")
#: What a Client Secret starts with, so one typed at the visible question is never echoed.
SECRET_PREFIX = "GOCSPX-"
#: Said when Google is left for later, by Skip or at the path question.
LATER_NOTE = "Left for later: `keryx setup` comes back to it, or `keryx auth login google`."

# The other way, for an account that cannot create a Cloud project (many work and school
# accounts cannot), which Personal mode cannot serve: it admits only the account that made
# it. The app is made with an account that can, External and published (In production,
# unverified), and the owner signs in to it with the account Keryx acts as. Published,
# because in Testing every sign-in ends after seven days.

#: Typed at the path question instead of a path, for the other way's steps. Never a
#: wrong answer: the same question is asked again after them.
OTHER = "other"
#: Said under the Personal mode steps.
NO_PROJECT_NOTE = (
    "Can't create a project with this account, or that page is not offered to it? Many work "
    f"and school accounts can't. Type {OTHER} at the question below for the way round it."
)
#: The six APIs Keryx's tools use, by the ids the console knows them by.
API_IDS = (
    "gmail.googleapis.com",
    "calendar-json.googleapis.com",
    "drive.googleapis.com",
    "docs.googleapis.com",
    "sheets.googleapis.com",
    "slides.googleapis.com",
)
#: The console's page that turns on every API it is given, in one click, in the form
#: Google's Workspace MCP guide links it (several ids, comma-separated).
ENABLE_APIS_URL = "https://console.cloud.google.com/flows/enableapi?apiid=" + ",".join(API_IDS)
OTHER_INTRO = (
    "The way round it: make the app with a personal Google account, then sign in to it with "
    "this one."
)
#: The other way's steps, each with the page it happens on (None: on this terminal).
OTHER_STEPS: tuple[tuple[str, str | None], ...] = (
    ("In a browser signed in to a personal Google account (any account that can create "
     "projects), create a project.", "https://console.cloud.google.com/projectcreate"),
    ("With that project selected at the top of the page, turn on the six APIs Keryx uses, "
     "in one click.", ENABLE_APIS_URL),
    ("Set up the app: choose Get started. Any name, your email for support, Audience "
     "External, a contact email, then agree and Create.",
     "https://console.cloud.google.com/auth/overview"),
    ("Publish it: under Audience, choose Publish app, so it is In production. In Testing "
     "every sign-in ends after 7 days. No test users are needed.",
     "https://console.cloud.google.com/auth/audience"),
    ("Make the client: Desktop app, any name, Create, then Download JSON, or copy the "
     "Client ID and the Client Secret.", "https://console.cloud.google.com/auth/clients/create"),
    ("Give the file or the Client ID at the question below, then sign in with the account "
     "Keryx should act as. Google says it hasn't verified the app: choose Advanced, then Go "
     "to the app's name.", None),
)
#: Said while the browser is out: an organization that blocks unapproved apps shows its
#: refusal on Google's own page and never sends the browser back.
BLOCKED_NOTE = (
    "If Google says your organization blocks the app or has to approve it, only its "
    "administrators can allow it: nothing on this machine can get past that."
)
#: Google's error for that, should it ever reach the redirect, and the sentence for it.
ADMIN_BLOCKED = "admin_policy_enforced"
ADMIN_BLOCKED_SENTENCE = (
    "your account's organization blocks apps it has not approved: only its administrators "
    "can allow this one, and nothing on this machine can get past that"
)


def steps_text() -> str:
    return "\n".join(f"{number}. {step}" for number, step in enumerate(CLIENT_STEPS, 1))


def own_client_hint() -> str:
    """How to make a client without a terminal to ask at: the page, then `--client-file`."""
    return (
        f"make one at {PERSONAL_MODE_URL}, then run `keryx auth login google --client-file "
        f"FILE`, or run `keryx auth login google` at a terminal. An account that cannot "
        f"create projects: {guide_path()}"
    )


def sentence(exc: BaseException) -> str:
    """A refusal as the owner reads it at the path question: capitalised, with a stop."""
    text = str(exc)
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def downloads() -> Path:
    return Path.home() / "Downloads"


def _is_desktop(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("installed"), dict)


def newest_client_file(directory: Path) -> Path | None:
    """The newest file Google's console could have downloaded in `directory`, a Desktop
    client before any other; None when there is none."""
    try:
        found = [path for path in directory.glob(CLIENT_GLOB) if path.is_file()]
        found.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return next((path for path in found if _is_desktop(path)), found[0] if found else None)


def suggested_client_file() -> str:
    """The path question's default: the newest downloaded client file in `~/Downloads`,
    else the folder itself. Only a default: where it was downloaded is always asked."""
    found = newest_client_file(downloads())
    return str(found) if found is not None else str(downloads()) + os.sep


def client_file_from(answer: str) -> tuple[Path, bool]:
    """The file an answer to the path question names, and whether it was picked as the
    newest client file in a folder that was named instead. A path is said back whole,
    from the root, so a relative one reads as where it was looked for."""
    said = answer.strip().strip("'\"")
    try:
        path = Path(said).expanduser().absolute()
        if path.is_dir():
            found = newest_client_file(path)
            if found is None:
                raise GoogleSetupError(f"there is no {CLIENT_GLOB} file in {path}")
            return found, True
        if not path.is_file():
            what = "folder" if said.endswith(os.sep) else "file"
            raise GoogleSetupError(f"there is no {what} at {path}")
    except GoogleSetupError:
        raise
    except (OSError, RuntimeError) as exc:  # no permission, a name too long, `~nobody`
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else "it cannot be read"
        raise GoogleSetupError(f"could not look at {said}: {why}") from None
    return path, False


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def client_json(client_id: str, client_secret: str) -> str:
    """A Client ID and Secret as the Desktop client file the console downloads, so a client
    copied by hand is kept exactly as a downloaded one is."""
    return json.dumps({"installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": google.TOKEN_URL,
        "redirect_uris": ["http://localhost"],
    }})


def take_client(
    settings: Settings,
    answer: str,
    *,
    paste: Callable[[], str],
    secret: Callable[[], str],
) -> tuple[Path, Path | None]:
    """Install the Desktop client an answer to the path question gives: a file, a folder
    holding one, a Client ID (`secret()` then asks for its secret, hidden), what is in the
    file typed at the question, or `paste` and then what is in it (`paste()`, hidden).
    Returns where it was saved, and the file taken from a folder when it was one.

    Nothing typed that may hold a secret is ever said back: a refusal names what it was,
    never what it said."""
    said = answer.strip()
    if said.lower() == PASTE:
        text = paste().strip()
        if not text:
            raise GoogleSetupError("nothing was pasted")
        if text.startswith("{") and not _parses(text):
            # The first line of a file spread over several: the rest has been dropped.
            raise GoogleSetupError(
                "what was pasted is not a whole client file: paste it on one line, as Google's "
                "download is, or give its path"
            )
        return install_client_text(settings, text, "what was pasted", desktop=True), None
    if said.startswith("{"):
        return install_client_text(settings, said, "what was typed", desktop=True), None
    if said.startswith(SECRET_PREFIX):
        raise GoogleSetupError(
            "that looks like the Client Secret: give the Client ID here, and the secret when "
            "it is asked for, hidden"
        )
    if CLIENT_ID.fullmatch(said):
        given = secret().strip()
        if not given:
            raise GoogleSetupError("no Client Secret was given")
        text = client_json(said, given)
        return install_client_text(settings, text, "that Client ID", desktop=True), None
    source, picked = client_file_from(answer)
    return install_client_file(settings, source, desktop=True), source if picked else None


# --- the OAuth client ---------------------------------------------------------------------


def install_client_file(settings: Settings, source: Path, *, desktop: bool = False) -> Path:
    """Copy a downloaded OAuth client JSON to `KERYX_HOME/google_client_secret.json` (0600).

    Validated first — the `installed` (desktop) or `web` shape, or only the first with
    `desktop` — so a wrong file is refused here rather than at the first sign-in. Returns
    where it now is; the caller points `GOOGLE_CLIENT_SECRETS_FILE` at it.
    """
    path = source.expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        why = exc.strerror if isinstance(exc, OSError) else "it is not text"
        raise GoogleSetupError(f"could not read {path}: {why or exc}") from None
    return install_client_text(settings, text, str(path), desktop=desktop)


def install_client_text(
    settings: Settings, text: str, what: str, *, desktop: bool = False
) -> Path:
    """`install_client_file` for a client's JSON in hand; `what` names it in a refusal."""
    try:
        parse_google_client(text)
    except ValueError as exc:
        raise GoogleSetupError(f"{what} is not a Google OAuth client file: {exc}") from None
    if desktop and "installed" not in json.loads(text):
        raise GoogleSetupError(
            f"{what} is a client for another kind of application, and Keryx needs a Desktop "
            "app client: make one with Desktop app as its application type"
        )
    return write_private(settings.config_dir / GOOGLE_CLIENT_FILE, text)


@dataclass(frozen=True)
class Offer:
    """What one sign-in will ask for, with the owner's client."""

    client: OAuthClient
    services: tuple[str, ...]
    #: Services nobody named that are asked for again because they are connected now.
    kept: tuple[str, ...] = ()

    @property
    def notes(self) -> list[str]:
        """What to say before the link: what is asked for again unasked, in one line.
        Nothing when there is nothing to say."""
        if not self.kept:
            return []
        one = len(self.kept) == 1
        return [
            f"{labels(self.kept)} {'is' if one else 'are'} connected now and "
            f"{'is' if one else 'are'} asked for again, so that this sign-in adds to "
            f"{'it' if one else 'them'}; `keryx auth logout google` is how access is "
            "taken away."
        ]


def offer(settings: Settings, wanted: Sequence[str] | None = None) -> Offer:
    """What a sign-in for `wanted` asks for: every service when None.

    A sign-in replaces the token, so it also asks again for whatever is connected now:
    signing in for one more service adds it, and ends nothing. Raises with what to do when
    the owner has no client yet.
    """
    client = google.resolve_client(settings)
    if client is None:
        raise GoogleSetupError(
            f"no Google client of yours yet: {own_client_hint()}"
        )
    if wanted is None:
        return Offer(client, tuple(SERVICES))
    token = google.read_token(settings)
    now = token.connected if token is not None else ()
    kept = tuple(name for name in now if name not in wanted)
    services = tuple(name for name in SERVICES if name in wanted or name in kept)
    return Offer(client, services, kept=kept)


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
    account: str | None = None,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "SignInResult":
    """Finish the sign-in `start_signin` began, from the address the browser landed on, and
    keep it: refused when the account is not the one expected (`admit`)."""
    grant = finish_grant(settings, redirected_to, post=post, get=get)
    admit(settings, grant, account, post=post)
    return keep(settings, grant)


def finish_grant(
    settings: Settings,
    redirected_to: str,
    *,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "Grant":
    """Exchange the code in the address the browser landed on. Nothing is saved yet."""
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
    return exchange(settings, pending, answer, post=post, get=get)


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
    account: str | None = None,
    timeout_s: float = CALLBACK_TIMEOUT_S,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "SignInResult":
    """The whole sign-in with a browser on this machine, kept: refused when the account is
    not the one expected (`admit`)."""
    grant = browser_grant(
        settings, chosen, announce=announce, timeout_s=timeout_s, post=post, get=get
    )
    admit(settings, grant, account, post=post)
    return keep(settings, grant)


def browser_grant(
    settings: Settings,
    chosen: Offer | None = None,
    *,
    announce: Announce,
    timeout_s: float = CALLBACK_TIMEOUT_S,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> "Grant":
    """A sign-in with a browser on this machine: listen, open, wait, exchange. Nothing is
    saved yet.

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
    return exchange(settings, pending, answer, post=post, get=get)


# --- the exchange -------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """What Google granted, in memory: a sign-in that has not been kept yet."""

    token: GoogleToken
    #: The services the sign-in asked for.
    asked: tuple[str, ...]

    @property
    def email(self) -> str:
        return self.token.email


@dataclass(frozen=True)
class SignInResult:
    """What a finished sign-in came to."""

    email: str
    #: The services whose every scope was granted.
    connected: tuple[str, ...]
    #: The services asked for that were not: unticked on Google's screen.
    missing: tuple[str, ...]
    path: Path


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _plain(text: Any) -> str:
    """Something Google (or whoever answered for it) said, safe to print: no control
    characters, and no longer than an error code has any reason to be."""
    return "".join(char for char in str(text) if char.isprintable())[:MAX_ERROR_CHARS]


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


def revoke(refresh_token: str, *, post: Callable[..., Any] | None = None) -> bool:
    """Ask Google to end the grant a refresh token stands for; True when it says it has.

    Never raises: a grant that could not be revoked is said, not a reason to keep a file.
    Google ends the whole grant, for that account and that client, not the one token.
    """
    post = post or httpx.post
    try:
        response = post(REVOKE_URL, data={"token": refresh_token}, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    # `invalid_token` is a grant that is already gone, which is what was wanted: the second
    # token of one grant gets it, since the first revoke ended them both.
    return response.status_code == 200 or (
        response.status_code == 400 and _json(response).get("error") == "invalid_token"
    )


def _shares_a_saved_grant(settings: Settings, token: GoogleToken) -> bool:
    """Whether revoking `token` could end a sign-in that is saved: Google revokes by
    account and client, so one for the same client, and not known to be another account's,
    would go with it."""
    saved = [google.read_token(settings)]
    legacy = legacy_token_path(settings)
    if legacy.is_file():
        saved.append(google.token_from(legacy))
    return any(
        one is not None
        and one.client_id == token.client_id
        and (not one.email or not token.email or one.email.lower() == token.email.lower())
        for one in saved
    )


def discard(
    settings: Settings, grant: "Grant | GoogleToken", *, post: Callable[..., Any] | None = None
) -> bool:
    """Give up a grant that will not be kept, revoking it at Google so that nothing is left
    there with nobody holding it. Not revoked when that would take a saved sign-in with it:
    signing in again as the same account and then leaving everything unticked must not end
    the sign-in that was working. True when it was revoked."""
    token = grant.token if isinstance(grant, Grant) else grant
    if _shares_a_saved_grant(settings, token):
        return False
    return revoke(token.refresh_token, post=post)


@dataclass(frozen=True)
class SignOut:
    """What signing out came to."""

    removed: tuple[Path, ...]
    #: Whether Google confirmed every grant ended; None when there was none to end.
    revoked: bool | None


def sign_out(settings: Settings, *, post: Callable[..., Any] | None = None) -> SignOut:
    """End every Google grant Keryx holds, and remove everything a sign-in left here.

    The token, a sign-in in progress, the copies in the agents' server's directory (every
    account's), and the Gmail-only token of an older sign-in. The files go whether or not
    Google confirms the revoke: a sign-out that leaves the credential behind is not one.
    """
    files = [
        google.token_path(settings),
        _pending_path(settings),
        legacy_token_path(settings),
        *google.credential_files(settings),
    ]
    found = tuple(path for path in files if path.is_file())
    tokens = [google.token_from(path) for path in found]
    grants = dict.fromkeys(token.refresh_token for token in tokens if token is not None)
    results = [revoke(refresh_token, post=post) for refresh_token in grants]
    for path in found:
        path.unlink(missing_ok=True)
    return SignOut(removed=found, revoked=all(results) if results else None)


def exchange(
    settings: Settings,
    pending: Pending,
    answer: Mapping[str, str],
    *,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> Grant:
    """Exchange the code in Google's `answer` for a grant. Nothing is saved: `keep` does.

    `post` and `get` are `httpx`'s, looked up when called so that nothing bound at import
    time can reach the network from a test that patched them. A grant that turns out to be
    unusable is revoked before the refusal, not left at Google with nobody holding it.
    """
    post, get = post or httpx.post, get or httpx.get
    if answer.get("error") == OPENED_TWICE:
        raise GoogleSetupError(
            "the sign-in page was opened twice on this machine, so it was stopped and "
            f"nothing was saved; run {SIGN_IN} again, or with --headless to paste the "
            "address back instead"
        )
    if answer.get("error") == ADMIN_BLOCKED:
        raise GoogleSetupError(ADMIN_BLOCKED_SENTENCE)
    if answer.get("error"):
        raise GoogleSetupError(f"Google said: {_plain(answer['error'])}")
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
            f"Google refused the code ({_plain(body.get('error', response.status_code))}); "
            f"start again with {SIGN_IN}"
        )
    discard_pending(settings)
    # Only what the token endpoint says it granted is believed. Granular consent can make
    # that less than was asked, and the redirect's own `scope` is whatever the browser sent.
    token = GoogleToken(
        refresh_token=body["refresh_token"],
        client_id=client.client_id,
        client_secret=client.client_secret,
        scopes=tuple(str(body.get("scope") or "").split()),
        email=_id_token_email(body.get("id_token"))
        or _userinfo_email(body.get("access_token"), get),
    )
    refusal = None
    if not token.scopes:
        refusal = f"Google's answer did not say what was granted; try {SIGN_IN} again"
    elif not token.connected:
        refusal = "nothing was granted: tick what Keryx may use on Google's screen and try again"
    elif not token.email:
        refusal = f"Google did not say which account signed in; try {SIGN_IN} again"
    if refusal is not None:
        discard(settings, token, post=post)
        raise GoogleSetupError(refusal)
    return Grant(token=token, asked=pending.services)


def unexpected_account(settings: Settings, grant: Grant, account: str | None = None) -> str | None:
    """The address this sign-in was expected to be, when it is not: the one asked for by
    name, else the one saved from the sign-in before. None when it is the account
    expected, or the first there has been."""
    expected = (account or settings.user_google_email or "").strip()
    return expected if expected and expected.lower() != grant.email.lower() else None


def admit(
    settings: Settings,
    grant: Grant,
    account: str | None = None,
    *,
    post: Callable[..., Any] | None = None,
) -> None:
    """Refuse a sign-in that came back as an account nobody expected, before anything is
    saved, and revoke it.

    An unexpected account is how every way of finishing somebody else's sign-in shows: a
    link read off the machine and approved as another account ends here, as that account.
    So it is never taken silently. `account` is the owner saying which one they mean.
    """
    expected = unexpected_account(settings, grant, account)
    if expected is None:
        return
    discard(settings, grant, post=post)
    if account:
        raise GoogleSetupError(
            f"Google signed in as {grant.email}, not {expected}, so nothing was saved; sign "
            f"in as {expected}, or pass --account {grant.email}"
        )
    raise GoogleSetupError(
        f"Google signed in as {grant.email}, but your Google address is saved as "
        f"{expected}, so nothing was saved. If {grant.email} is the account you mean, run "
        f"`keryx auth login google --account {grant.email}`. If it is not, something else "
        "on this machine may have finished the sign-in: run it again with --headless"
    )


def keep(settings: Settings, grant: Grant) -> SignInResult:
    """Make `grant` the sign-in: the one token and its copy for the agents' MCP server,
    both or neither. A sign-in that cannot be written leaves the one before it in place."""
    path = google.token_path(settings)
    try:
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        google.write_token(settings, grant.token)
        try:
            google.export_for_mcp(settings, grant.token, only=True)
        except OSError:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                write_private(path, before)
            raise
    except OSError as exc:
        raise GoogleSetupError(
            f"could not save the sign-in ({exc.strerror or type(exc).__name__}); the one "
            "before it, if any, is unchanged"
        ) from None
    # One token now. The Gmail-only one an older sign-in left is spent once this one reads
    # Gmail; until it does, that file is still what `check_email` answers from.
    if grant.token.reads_gmail:
        with contextlib.suppress(OSError):
            legacy_token_path(settings).unlink(missing_ok=True)
    connected = grant.token.connected
    return SignInResult(
        email=grant.email,
        connected=connected,
        missing=tuple(name for name in grant.asked if name not in connected),
        path=path,
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


#: The services a Cloud project made by the older guide has no API enabled for.
NEWER_APIS = ("drive", "docs", "sheets", "slides")


def api_hint(result: SignInResult) -> str | None:
    """Where to enable the APIs, said once to an owner whose Cloud project may predate
    them: granting a scope does not enable its API, and the older guide enabled only Gmail
    and Calendar. Nothing can be asked without a call to Google, so it is said rather than
    detected."""
    newer = [name for name in NEWER_APIS if name in result.connected]
    if not newer:
        return None
    return (
        f"If an agent reports that the {labels(newer)} API is not enabled, enable it in your "
        f"Google Cloud project: the links are in {guide_path()}."
    )


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
#: The answers once somebody is signed in. Different values from the two above, and a
#: different question: an answer recorded before the sign-in must not be offered after it,
#: where "sign in" would mean doing it all a second time.
KEEP, AGAIN = "keep", "again"
HERE, ELSEWHERE = "here", "elsewhere"


def run_section(ctx: "SetupContext") -> None:
    """One sign-in for everything Google: asked nothing when everything on offer is connected."""
    ui = ctx.ui
    ui.note("One Google sign-in connects Gmail, Calendar, Drive, Docs, Sheets and Slides: "
            "for the agents, and for email, calendar and document answers on a call.")
    connect(ctx)


def connect(ctx: "SetupContext") -> None:
    """Sign in to Google, unless everything on offer is connected already.

    The sign-in is a probe, so going back through the wizard replays its result and never
    runs it twice. Once it is kept, everything it implies is saved before anything else is
    asked, so no question left unanswered can leave a token without its settings.
    """
    from keryx.setup.ui import Choice

    ui, settings = ctx.ui, ctx.settings
    token = google.read_token(settings)
    client = google.resolve_client(settings)
    done = token is not None and all(name in token.connected for name in SERVICES)
    if token is not None:
        _show(ctx, token.connected, SERVICES, f"Google: signed in as {token.email or 'you'}")
        if done and not ctx.review:
            return
        if ui.select(
            f"You are signed in to Google as {token.email or 'an unnamed account'}. Sign in "
            "again?",
            [Choice(KEEP, "Keep this sign-in"),
             Choice(AGAIN, "Sign in again", hint="one consent screen")],
            default=KEEP if done else AGAIN,
        ) == KEEP:
            return
    else:
        # With no client to hand, signing in starts with making one in the Google Cloud
        # console: worth it by default only for Codex, whose tasks have no Google otherwise.
        wanted = client is not None or "codex" in settings.enabled_agents
        if ui.select(
            "Sign in to Google?",
            [Choice(SKIP, "Skip for now"),
             Choice(SIGN_IN_NOW, "Sign in", hint="one consent screen")],
            default=SIGN_IN_NOW if wanted else SKIP,
        ) == SKIP:
            ui.note(LATER_NOTE)
            return
    if client is None and not _own_client(ctx):
        return
    grant = _sign_in(ctx)
    if grant is None or not _admitted(ctx, grant):
        return
    try:
        result = keep(ctx.settings, grant)
    except GoogleSetupError as exc:
        ui.error(str(exc))
        return
    _keep(ctx, result)
    ui.success(f"Google: signed in as {result.email}")
    _show(ctx, result.connected, [*result.connected, *result.missing])
    if hint := api_hint(result):
        ui.note(hint)


def _admitted(ctx: "SetupContext", grant: Grant) -> bool:
    """Whether this sign-in may be kept: asked only when it came back as an account other
    than the one saved, which is how somebody else's finishing it would show. A no revokes
    it, as a probe, so going back does not revoke twice."""
    ui = ctx.ui
    expected = unexpected_account(ctx.settings, grant)
    if expected is None:
        return True
    if ui.confirm(
        f"Google signed in as {grant.email}, but your Google address is saved as "
        f"{expected}. Use {grant.email}?",
        default=False,
    ):
        return True
    ctx.probes.google_discard(ctx.settings, grant)
    ui.note(f"Nothing was saved, and the sign-in as {grant.email} was given up.")
    return False


def _show(
    ctx: "SetupContext", connected: Sequence[str], offered: Sequence[str], heading: str = ""
) -> None:
    if heading:
        ctx.ui.success(heading)
    for ok, line in service_lines(connected, offered):
        (ctx.ui.success if ok else ctx.ui.warn)(line)


def _own_client(ctx: "SetupContext") -> bool:
    """The owner's own Google client, made now: the Personal mode page opened (a probe, so
    going back does not open it again), the clicks shown, and the downloaded file or the
    Client ID asked for, until it is given or the owner stops.

    Where the file went is never assumed: the newest one in `~/Downloads` is only what
    Enter takes. Nothing is put in the answer box beforehand, so `other`, `paste`, a Client
    ID or `skip` is typed as it is. After a wrong answer the screen keeps the steps the
    owner was following: the wizard shows only what was said since the last question."""
    ui = ctx.ui
    ui.note(WHY_OWN_CLIENT)
    headless = ctx.probes.headless()
    opened = not headless and ctx.probes.open_browser(PERSONAL_MODE_URL)

    def personal(first: bool = False) -> None:
        body = f"{opening(opened)}\n\n{steps_text()}" if first else steps_text()
        ui.panel("Make your Google sign-in app", body)
        ui.link(PERSONAL_MODE_URL)
        for line in [NO_PROJECT_NOTE, *([ELSEWHERE_NOTE] if headless else [])]:
            ui.note(line)

    def other() -> None:
        ui.note(OTHER_INTRO)
        for number, (step, url) in enumerate(OTHER_STEPS, 1):
            ui.note(f"{number}. {step}")
            if url:
                ui.link(url)

    personal(first=True)
    showing = personal
    suggested = suggested_client_file()
    while True:
        ui.note(f"Enter takes {suggested}")
        raw = ui.text(f"{PATH_QUESTION} ({SKIP_WORD} leaves it for later)").strip()
        if raw.lower() == SKIP_WORD:
            ui.note(LATER_NOTE)
            return False
        if raw.lower() == OTHER:
            showing = other
            other()
            continue
        try:
            path, picked = take_client(
                ctx.settings, raw or suggested,
                paste=lambda: ui.secret(PASTE_QUESTION),
                secret=lambda: ui.secret(SECRET_QUESTION),
            )
        except GoogleSetupError as exc:
            showing()
            ui.error(sentence(exc))
            continue
        if picked is not None:
            ui.note(f"Took {picked}, the newest one there.")
        ui.success(f"client saved to {path}")
        ui.markdown(guide(CONSENT_GUIDE))
        return ctx.save({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})


def opening(opened: bool) -> str:
    """The line above the Personal mode page's link."""
    if opened:
        return "Your browser is opening Google's page for it. If it does not, open the link below."
    return "Open the link below in a browser, on any device."


def _sign_in(ctx: "SetupContext") -> Grant | None:
    """Run the sign-in the way that fits this machine; another way, or none, if it fails.
    What comes back is a grant in memory: nothing is saved until it is admitted and kept."""
    from keryx.setup.ui import Choice

    ui = ctx.ui
    try:
        chosen = offer(ctx.settings)
    except GoogleSetupError as exc:
        ui.error(str(exc))
        return None
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
                # The link goes under the panel, bare (`Prompter.link`): inside it, a link
                # this long is cut by the border and cannot be copied in one piece.
                ui.panel(
                    "Sign in to Google — open this on any device",
                    "Open the link below and approve it. The page you land on will not "
                    f"load: copy its whole address and paste it below.\n\n{BLOCKED_NOTE}",
                )
                ui.link(url)
                address = ui.text(
                    "The address you landed on (blank to skip)",
                    validate=_from_this_link(ctx.settings),
                )
                if not address:
                    return None
                result = ctx.probes.google_finish(ctx.settings, address)
            if isinstance(result, Grant):
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


def _from_this_link(settings: Settings) -> Callable[[str], str | None]:
    """Refuse, at the question, an address that is not the answer to the link just shown:
    one pasted from an earlier link, or offered again after going back, is already spent."""

    def problem(value: str) -> str | None:
        if not value.strip():
            return None
        try:
            saved = json.loads(_pending_path(settings).read_text(encoding="utf-8"))
            query = parse_qs(urlparse(value.strip().strip("'\"")).query)
        except (OSError, ValueError):
            return "That is not an address: copy the whole address bar."
        if query.get("error") or _same_state(query.get("state", [""])[0], saved.get("state")):
            return None
        return "That address is not from the link above: open it, and paste where it lands."

    return problem


def _announcer(ctx: "SetupContext") -> Announce:
    def announce(url: str, opened: bool) -> None:
        ctx.ui.panel(
            "Sign in to Google",
            ("Your browser is opening the link below" if opened else "Open the link below "
             "in a browser on this machine")
            + f". Waiting for you to finish there. Ctrl-C stops waiting.\n\n{BLOCKED_NOTE}",
        )
        ctx.ui.link(url)

    return announce


def agents_switched_off(settings: Settings, stored: Mapping[str, Any]) -> bool:
    """Whether the owner has turned the agents' Google tools off on purpose: the setting is
    false because somebody stored it, not because nobody has signed in yet."""
    return not settings.google_workspace_mcp and "GOOGLE_WORKSPACE_MCP" in stored


def _keep(ctx: "SetupContext", result: SignInResult) -> None:
    """Save what a kept sign-in implies, at once and with nothing asked: the address the
    agents act as, and their Google tools, turned on the first time."""
    ui, settings = ctx.ui, ctx.settings
    if settings.user_google_email != result.email:
        ctx.save({"USER_GOOGLE_EMAIL": result.email})
    if agents_switched_off(settings, ctx.store.stored()):
        ui.note("Agents' Google tools stay off, as you set them: `keryx config set "
                "GOOGLE_WORKSPACE_MCP true` turns them on.")
    elif not settings.google_workspace_mcp:
        ctx.save({"GOOGLE_WORKSPACE_MCP": True})
        ui.note("Agents get the Google tools from the next task (`keryx restart` if Keryx is "
                "running). `keryx config set GOOGLE_WORKSPACE_MCP false` turns them off.")
