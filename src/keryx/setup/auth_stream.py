"""`keryx auth login NAME --json`: the four sign-ins as JSON lines, for the Mac app.

Each is one stream with exactly one ending (`done`, `error` or `cancelled`) on every path
(`EventLines`): `open` hands the app a link to open (and, for Codex, the code to type
there), `code` asks it for the one line Keryx then reads from stdin. The terminal's
`keryx auth login` is untouched; these drive the same CLIs and reuse the same Google code.

- **claude** drives `claude setup-token` behind a pseudo-terminal (`terminal.PtyChild`).
  The bundled CLI (2.1.281, run with no network and no browser, and stopped at its first
  question) draws with Ink: a banner, "Opening browser to sign in…", then "Browser didn't
  open? Use the url below to sign in", the link to `https://claude.com/cai/oauth/authorize?…`
  (an OSC 8 hyperlink around the same text), and "Paste code here if prompted >". What it
  prints once the code is accepted is read from its own strings, never seen: "Your OAuth
  token (valid for 1 year):", the token, "Store this token securely…". So the parser is
  strict — an https link on exactly Claude's host, with no user info, no port and nothing a
  browser would read another way (`_https_url`); the question; the heading before the
  token; the token's `sk-ant-oat` shape at its full length, alone on its line — and
  anything else is `CLAUDE_CHANGED`, which sends the owner to the API-key field. The code
  typed back is a code's characters only (`NOT_A_CODE`, exit 2, otherwise). The screen is
  never echoed, logged or put in an error, because it ends with the token on it; the token
  goes straight to `ConfigStore`, and only a fixed sentence ever leaves. The CLI's own
  browser is pointed at nothing (`BROWSER`): the app opens the link, once.
- **codex** runs `codex login --device-auth` (the headless login, `agents.login_argv`). It
  prints "1. Open this link in your browser and sign in to your account", the link, "2.
  Enter this one-time code (expires in 15 minutes)", the code — read from the bundled
  binary's strings and the open-source CLI that prints them, never run, since asking for a
  code is a request to OpenAI — then waits, and exits 0 once the browser has finished.
  Its link must be exactly `https://auth.openai.com/codex/device`.

Every login child gets `child_env.login_env`: Keryx's environment with no secret of
Keryx's in it, whatever the case of the variable's name.
- **gmail** is the two-step sign-in as one stream: the consent link, then the address the
  browser landed on, as one line on stdin.
- **google-workspace** is `run_google_setup`, whose consent link is passed on as `open`
  when the server prints it, and whose wait for new credentials is the wait.
"""

import codecs
import os
import queue
import re
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import parse_qs, urlparse

from keryx.agents.auth import redact
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.store import ConfigError, ConfigStore
from keryx.localmodels.pull import EventLines
from keryx.setup import agents, google
from keryx.setup.auth import LOGINS, AuthError, agent_cli, use_client_file
from keryx.setup.child_env import login_env
from keryx.setup.terminal import STOP_AFTER_S, Child, PtyChild, visible

#: Where the Claude token goes: the headless subscription tier (`agents/auth.py`).
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
#: How long the owner has to finish a sign-in in the browser.
LOGIN_TIMEOUT_S = 600.0
#: Codex's code lives fifteen minutes; its sign-in gets a minute more.
CODEX_TIMEOUT_S = 960.0
#: How long a CLI has to show what is expected of it before it is taken to have changed.
START_TIMEOUT_S = 60.0
#: How often a waiting sign-in looks at its child and at stdin.
POLL_S = 0.2
#: The most of a child's screen kept: the last few frames are all a parser needs.
KEEP_CHARS = 64_000

CLAUDE_CHANGED = "Claude's sign-in changed; use an API key instead."
CLAUDE_SIGNED_IN = "Signed in to Claude with your subscription."
CLAUDE_SIGNED_IN_KEY_FIRST = (
    "Signed in to Claude with your subscription; an Anthropic API key is also set, and it "
    "is used first."
)
NOT_A_CODE = "That is not a sign-in code; copy the whole code Claude showed, and start again."
CODE_REFUSED = "Claude did not accept that code; start the sign-in again."
NO_CODE = "No code came back, so the sign-in was stopped; start it again."
CODEX_CHANGED = "Codex's sign-in changed; use an API key instead."
CODEX_UNREACHABLE = "Codex could not start a sign-in; check the connection and try again."
CODEX_UNFINISHED = "Codex's sign-in did not finish; start it again."
NO_ADDRESS = "No address came back, so the sign-in was stopped; start it again."
TIMED_OUT = "The sign-in took too long, so it was stopped; start it again."

#: The hosts Claude's sign-in link is on (claude.com today, claude.ai before), exactly, and
#: the path it ends in.
CLAUDE_HOSTS = frozenset({"claude.com", "claude.ai"})
CLAUDE_AUTHORIZE_PATH = "/oauth/authorize"
#: The parameters a PKCE authorization link cannot be without.
CLAUDE_AUTHORIZE_PARAMS = frozenset({"client_id", "code_challenge", "state"})
#: Codex's device page, exactly.
CODEX_DEVICE_HOST, CODEX_DEVICE_PATH = "auth.openai.com", "/codex/device"
#: Every character a URL may hold (RFC 3986's unreserved and reserved sets, and `%`):
#: anything else — `\`, which a browser reads as `/`, whitespace, a control character — is
#: a link that parses one way here and another in the browser, and is refused.
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
#: A sign-in code as Claude shows it (`code#state`): nothing a terminal would act on.
_CODE = re.compile(r"[A-Za-z0-9#_.\-]+")

#: A link on a raw screen, whole: it ends at a BEL (an OSC 8 target), an escape or a space.
_LINK = re.compile(r"https://[^\s\x07\x1b]+(?=[\s\x07\x1b])")
_ASKS_FOR_CODE = re.compile(r"Paste\s+code\s+here")
_TOKEN_HEADING = re.compile(r"Your\s+OAuth\s+token")
#: The token, whole: alone at the end of its line, and as long as one is (`sk-ant-oat01-`
#: and about ninety-five characters), so a token wrapped or half drawn is never taken.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(sk-ant-oat\d{2}-[A-Za-z0-9_-]{80,})[ \t]*\n")
_OAUTH_ERROR = re.compile(r"OAuth\s+error")
_DEVICE_LINK = re.compile(r"Open\s+this\s+link[^\n]*\n\s*(https://\S+)\s")
_DEVICE_CODE = re.compile(
    r"one-time\s+code(?:\s+\([^)\n]*\))?\s*\n\s*([A-Z0-9]{3,}(?:-[A-Z0-9]{3,})*)\s"
)
_DEVICE_FAILED = re.compile(r"Error\s+logging\s+in\s+with\s+device\s+code")
_GOOGLE_CONSENT = re.compile(r"https://accounts\.google\.com/[^\s\"'<>)\]]+")


class SignInFailed(RuntimeError):
    """A sign-in that stopped, in one fixed sentence the app can show, and its exit code."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Lines(Protocol):
    """The lines the app writes to Keryx's stdin."""

    def get(self, timeout: float) -> str | None:
        """The next line; None at the end of input; `TimeoutError` when none came in time."""


class StdinLines:
    """`stream`'s lines, read on a thread of their own so a wait for one can time out (or be
    cancelled) rather than block."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._ended = False
        self._reader: threading.Thread | None = None

    def get(self, timeout: float) -> str | None:
        if self._ended:
            return None
        if self._reader is None:
            self._reader = threading.Thread(target=self._read, name="keryx-stdin", daemon=True)
            self._reader.start()
        try:
            line = self._lines.get(timeout=timeout) if timeout > 0 else self._lines.get_nowait()
        except queue.Empty:
            raise TimeoutError from None
        if line is None:
            self._ended = True
        return line

    def _read(self) -> None:
        try:
            for line in iter(self._stream.readline, ""):
                self._lines.put(line)
        finally:
            self._lines.put(None)


class LoginEvents(EventLines):
    """For the Mac app: `open` (`url`, and `code` for Codex), `code`, then exactly one of
    `done` (`name`, `message`, and `stored` for Claude), `error`, `cancelled`."""

    def open(self, url: str, code: str | None = None) -> None:
        self._emit({"event": "open", "url": url, **({"code": code} if code else {})})

    def code(self) -> None:
        self._emit({"event": "code"})

    def done(self, name: str, message: str, **extra: Any) -> None:
        self._end({"event": "done", "name": name, "message": message, **extra})


Spawn = Callable[[list[str], Mapping[str, str] | None], Child]


def stream(
    name: str,
    settings: Settings,
    store: ConfigStore,
    events: LoginEvents,
    *,
    lines: Lines,
    client_file: Path | None = None,
    spawn: Spawn | None = None,
    post: Callable[..., Any] | None = None,
    workspace: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the sign-in `name`, told to `events`; the exit code (0, 1, or 2 for no such name).

    A Cancel is a `KeyboardInterrupt`, and goes on as one once the child is stopped:
    `EventLines.guard` tells it. `spawn`, `post` and `workspace` are looked up when called,
    so a patched module attribute is what runs.
    """
    if name not in LOGINS:
        events.error(f"sign in to one of: {', '.join(LOGINS)}")
        return 2
    try:
        if name == "claude":
            _claude(agent_cli(name), settings, store, events, lines, spawn or PtyChild, clock)
        elif name == "codex":
            _codex(agent_cli(name), events, spawn or PtyChild, clock)
        else:
            if client_file is not None:
                settings = use_client_file(settings, store, client_file)
            if name == "gmail":
                _gmail(settings, events, lines, post)
            else:
                _workspace(settings, store, events, workspace or google.run_google_setup)
    except (AuthError, SignInFailed, google.GoogleSetupError) as error:
        events.error(str(error))
        return error.code if isinstance(error, SignInFailed) else 1
    return 0


def _https_url(url: str, hosts: frozenset[str] | set[str]) -> str | None:
    """`url`'s path and query when it is https on exactly one of `hosts` — no user info, no
    port, no character a browser would read differently — else None."""
    if not _URL_CHARS.fullmatch(url):
        return None
    try:
        parts = urlparse(url)
        host, port = parts.hostname, parts.port
    except ValueError:  # a port out of range, or a host that is not one
        return None
    if (parts.scheme != "https" or host is None or port is not None or parts.netloc != host
            or host not in hosts):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


# --- claude -------------------------------------------------------------------------------


def _claude(cli: str, settings: Settings, store: ConfigStore, events: LoginEvents,
            lines: Lines, spawn: Spawn, clock: Callable[[], float]) -> None:
    argv = agents.login_argv("claude", cli, headless=True)
    # The app opens the link: the CLI's own browser is pointed at nothing, so one tab opens.
    env = login_env(BROWSER=shutil.which("true") or "true",
                     TERM=os.environ.get("TERM") or "xterm-256color")
    try:
        child = spawn(argv, env)
    except OSError as error:
        raise SignInFailed(f"Claude's sign-in could not start ({error.strerror}).") from None
    try:
        token = _setup_token(child, events, lines, clock)
    except OSError:  # the terminal closed under it: what it was doing is unknown
        raise SignInFailed(CLAUDE_CHANGED) from None
    finally:
        child.stop()
    try:
        store.set({TOKEN_KEY: token}, actor=OWNER)
    except ConfigError as error:
        raise SignInFailed(f"The token could not be saved: {redact(str(error), [token])}") \
            from None
    # An API key is the first tier (`agents/auth.py`): the owner should not think the
    # subscription is what their tasks now run on.
    message = CLAUDE_SIGNED_IN_KEY_FIRST if settings.anthropic_api_key else CLAUDE_SIGNED_IN
    events.done("claude", message, stored=TOKEN_KEY)


class Screen:
    """What a child has shown so far: raw, as it came, and as words. Read, never repeated."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.raw = ""

    def feed(self, chunk: bytes) -> None:
        self.raw = (self.raw + self._decoder.decode(chunk))[-KEEP_CHARS:]

    def clear(self) -> None:
        """Forget what was shown: what comes next is read on its own."""
        self.raw = ""

    @property
    def words(self) -> str:
        return visible(self.raw)

    def links(self) -> list[str]:
        return _LINK.findall(self.raw)


def _authorize_url(screen: Screen) -> str | None:
    """Claude's sign-in link, whole, on Claude's own host; None until there is one."""
    for url in screen.links():
        rest = _https_url(url, CLAUDE_HOSTS)
        if rest is None:
            continue
        path, _, query = rest.partition("?")
        if (path.endswith(CLAUDE_AUTHORIZE_PATH)
                and CLAUDE_AUTHORIZE_PARAMS <= parse_qs(query).keys()):
            return url
    return None


def _token(screen: Screen) -> str | None:
    """The token, once its heading and the whole of it are on screen."""
    words = screen.words
    heading = _TOKEN_HEADING.search(words)
    if heading is None:
        return None
    found = _TOKEN.search(words, heading.end())
    return found.group(1) if found else None


def _setup_token(child: Child, events: LoginEvents, lines: Lines,
                 clock: Callable[[], float]) -> str:
    """Drive `claude setup-token` to its token: the link out, the code in."""
    screen, started = Screen(), clock()
    url_told = asked = typed = False
    while True:
        now = clock()
        if now - started >= LOGIN_TIMEOUT_S:
            raise SignInFailed(TIMED_OUT)
        if not asked and now - started >= START_TIMEOUT_S:
            raise SignInFailed(CLAUDE_CHANGED)
        chunk = child.read(POLL_S)
        if chunk is None:  # it ended without a token: refused, or not what it was
            refused = typed and _OAUTH_ERROR.search(screen.words)
            raise SignInFailed(CODE_REFUSED if refused else CLAUDE_CHANGED)
        screen.feed(chunk)
        if token := _token(screen):
            return token
        if typed and _OAUTH_ERROR.search(screen.words):
            raise SignInFailed(CODE_REFUSED)
        if not url_told and (url := _authorize_url(screen)):
            events.open(url)
            url_told = True
        if url_told and not asked and _ASKS_FOR_CODE.search(screen.words):
            events.code()
            asked = True
        if asked and not typed:
            try:
                line = lines.get(0)
            except TimeoutError:
                continue
            code = (line or "").strip()
            if not code:
                raise SignInFailed(NO_CODE)
            if not _CODE.fullmatch(code):
                raise SignInFailed(NOT_A_CODE, code=2)
            screen.clear()
            child.submit(code)
            typed = True


# --- codex --------------------------------------------------------------------------------


def _codex(cli: str, events: LoginEvents, spawn: Spawn, clock: Callable[[], float]) -> None:
    argv = agents.login_argv("codex", cli, headless=True)
    try:
        child = spawn(argv, login_env())
    except OSError as error:
        raise SignInFailed(f"Codex's sign-in could not start ({error.strerror}).") from None
    try:
        _device_auth(child, events, clock)
    except OSError:
        raise SignInFailed(CODEX_CHANGED) from None
    finally:
        child.stop()
    events.done("codex", "Signed in to Codex with ChatGPT.")


def _device_link(screen: Screen) -> tuple[str, str] | None:
    """The device page and the one-time code, once both are whole; None until then."""
    words = screen.words
    link, code = _DEVICE_LINK.search(words), _DEVICE_CODE.search(words)
    if link is None or code is None:
        return None
    url = link.group(1)
    if _https_url(url, {CODEX_DEVICE_HOST}) != CODEX_DEVICE_PATH:
        return None
    return url, code.group(1)


def _device_auth(child: Child, events: LoginEvents, clock: Callable[[], float]) -> None:
    """Hand over Codex's link and code, then wait for it to say the sign-in finished."""
    screen, started, shown = Screen(), clock(), False
    while True:
        now = clock()
        if now - started >= CODEX_TIMEOUT_S:
            raise SignInFailed(TIMED_OUT)
        if not shown and now - started >= START_TIMEOUT_S:
            raise SignInFailed(CODEX_CHANGED)
        chunk = child.read(POLL_S)
        if chunk is None:
            # Its terminal has closed; give it as long to be reaped as a stop would.
            if shown and child.wait(STOP_AFTER_S) == 0:
                return
            if shown:
                raise SignInFailed(CODEX_UNFINISHED)
            raise SignInFailed(
                CODEX_UNREACHABLE if _DEVICE_FAILED.search(screen.words) else CODEX_CHANGED
            )
        screen.feed(chunk)
        if not shown and (found := _device_link(screen)):
            events.open(*found)
            shown = True


# --- google -------------------------------------------------------------------------------


def _gmail(settings: Settings, events: LoginEvents, lines: Lines,
           post: Callable[..., Any] | None) -> None:
    events.open(google.start_signin(settings))
    try:
        address = lines.get(LOGIN_TIMEOUT_S)
    except TimeoutError:
        raise SignInFailed(TIMED_OUT) from None
    if not (address or "").strip():
        raise SignInFailed(NO_ADDRESS)
    google.finish_signin(settings, address, post=post)
    events.done("gmail", "Signed in to Gmail, read-only.")


def _workspace(settings: Settings, store: ConfigStore, events: LoginEvents,
               workspace: Callable[..., Any]) -> None:
    store.set({"GOOGLE_WORKSPACE_MCP": True}, actor=OWNER)
    opened = False

    def echo(text: str) -> None:
        nonlocal opened
        if not opened and (found := _GOOGLE_CONSENT.search(text)):
            events.open(found.group(0))
            opened = True

    workspace(settings, echo=echo, timeout_s=LOGIN_TIMEOUT_S)
    events.done("google-workspace", "Google for agents is signed in: Gmail and Calendar.")
