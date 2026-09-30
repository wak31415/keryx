"""Google, done once: the OAuth client, two sign-ins, and a read to prove each works.

Everything Google in Keryx is optional, and it is two separate things that happen to share
one Google Cloud client:

- **Email answers on a call** (the `check_email` plugin). A read-only Gmail sign-in with PKCE,
  in two steps, because the machine Keryx runs on usually has no browser: `start_signin` makes
  the consent link, which can be opened on any device; approving it lands the browser on
  `http://localhost:1/?…`, which does not load, and `finish_signin` takes that address,
  checks its `state`, and exchanges its one-time `code` (with the verifier, which never
  left this machine) for a refresh token in `DATA_DIR/gmail_token.json` (0600).
- **Agents that send mail and manage the calendar** (`GOOGLE_WORKSPACE_MCP`). Subagents
  reach Gmail and Calendar through the `workspace-mcp` stdio server, which only opens its
  consent screen when a tool is actually called — and a subagent that hit it mid-task would
  stall behind a login nobody is watching. So `run_google_setup` starts the very same
  server by hand, speaks just enough MCP over stdio to call one harmless tool, and lets the
  browser flow complete while the owner is sitting there. Its consent listens on
  `localhost:8000`, so a headless machine needs one `ssh -L` line (`guides/google.md`).

The client is `Settings.google_oauth_client()` for both — the id/secret pair, else the
downloaded JSON — and `install_client_file` is how that JSON gets to
`KERYX_HOME/google_client_secret.json`. `run_section` is the wizard's screen for the agents'
half; the email half is the `check_email` plugin's step (`keryx.setup.plugins`), which
calls `_ensure_client` and `_sign_in_email` here. Both read their instructions from
`guides/google.md`, so the wiki can link the same steps.

The MCP stdio framing is newline-delimited JSON-RPC: `initialize` (request),
`notifications/initialized` (notification), then `tools/call`. Anything the server writes
that is not a JSON-RPC reply — including the consent URL — is echoed straight through. The
probe answering "you have to authorize first" is the *expected* first reply: the server has
to stay running to receive the OAuth callback, so the flow waits for a new credential file
in `GOOGLE_MCP_CREDENTIALS_DIR` and only then calls the probe again. Every wait has a
deadline, the subprocess is always stopped, and failures are a `GoogleSetupError` with what
was seen last.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from keryx.agents.base import google_mcp_server_config
from keryx.config import GOOGLE_CLIENT_FILE, Settings, parse_google_client, write_private
from keryx.integrations.gmail import SCOPE, TOKEN_URL, HttpGmail, token_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.setup.context import SetupContext

log = logging.getLogger("keryx.setup.google")

MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "keryx-setup", "version": "1"}

#: A harmless read-only tool, called only to trigger the OAuth flow.
PROBE_TOOL = "list_calendars"

DEFAULT_TIMEOUT_S = 180.0
#: How long the server gets to shut down politely before it is killed.
STOP_TIMEOUT_S = 5.0
#: How often the credentials directory is checked while the user is in the browser.
CREDENTIALS_POLL_S = 2.0
#: Extra time the confirming probe gets after the sign-in, on top of the main deadline.
CONFIRM_TIMEOUT_S = 30.0

_INITIALIZE_ID = 1
_PROBE_ID = 2
_CONFIRM_ID = 3

#: What workspace-mcp says instead of an answer when the account is not authorized yet.
#: Matched loosely, because the wording is the server's, not ours.
AUTH_HINTS = ("authoriz", "authentic", "sign in", "sign-in", "log in", "http://", "https://")

INSTRUCTIONS = (
    "Starting the Google Workspace MCP server once to authorize this machine.\n"
    "Open the link it prints, complete the Google sign-in and grant Gmail and Calendar "
    "access. On a machine with no browser, forward its port first from the one you are "
    "at: ssh -L 8000:localhost:8000 <this host>."
)

SIGN_IN_INSTRUCTIONS = (
    "Complete the Google sign-in in the browser now — this window is waiting for it "
    "(the server must stay running to receive the callback)."
)

Popen = Callable[..., Any]
Echo = Callable[[str], None]
Sleep = Callable[[float], None]


class GoogleSetupError(RuntimeError):
    """A sign-in that cannot go on, in one sentence to print."""


NO_CLIENT = (
    "there is no Google OAuth client yet: download one from the Google Cloud console "
    "(Clients → Create client → Desktop app) and pass it with --client-file"
)


def run_google_setup(
    settings: Settings,
    *,
    popen: Popen | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    echo: Echo = print,
    sleep: Sleep = time.sleep,
) -> bool:
    """Run the one-off OAuth flow. Returns True on success, raises `GoogleSetupError` otherwise.

    `popen` defaults to `subprocess.Popen` *at call time*, so patching the module attribute
    is enough to guarantee no subprocess starts (which is how the tests prove the refusal
    path never reaches one).
    """
    _require_oauth_client(settings)

    config = google_mcp_server_config(settings)
    credentials_dir = Path(config["env"]["GOOGLE_MCP_CREDENTIALS_DIR"])
    argv = [config["command"], *config["args"]]

    echo(INSTRUCTIONS)
    echo(f"credentials will be stored under {credentials_dir}")
    log.info("starting %s for the google oauth flow", " ".join(argv))

    spawn = popen or subprocess.Popen
    process = spawn(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, **config["env"]},
        text=True,
        bufsize=1,
    )
    try:
        replies = _start_reader(process)
        deadline = time.monotonic() + timeout_s
        before = _credentials_snapshot(credentials_dir)

        _send(process, {"jsonrpc": "2.0", "id": _INITIALIZE_ID, "method": "initialize",
                        "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                                   "capabilities": {}, "clientInfo": CLIENT_INFO}})
        handshake = _await_reply(replies, _INITIALIZE_ID, deadline, echo)
        if "error" in handshake:
            raise GoogleSetupError(f"the server refused: {_error_text(handshake['error'])}")

        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = _probe(process, replies, _PROBE_ID, deadline, echo)

        # The first probe is what *triggers* the browser consent screen, so "you have to
        # authorize" is the expected answer, not a failure: the server has to stay alive to
        # receive the OAuth callback on localhost while the user signs in.
        if _looks_like_authorization(reply):
            echo(SIGN_IN_INSTRUCTIONS)
            if not _wait_for_credentials(credentials_dir, before, deadline, sleep, echo):
                raise GoogleSetupError(
                    f"no new credentials appeared in {credentials_dir} within "
                    f"{timeout_s:.0f}s — the browser sign-in was not completed"
                )
            echo("credentials written; checking the access they grant…")
            confirm_deadline = time.monotonic() + CONFIRM_TIMEOUT_S
            reply = _probe(process, replies, _CONFIRM_ID, confirm_deadline, echo)

        if _is_failure(reply):
            raise GoogleSetupError(f"{PROBE_TOOL} failed: {' '.join(_reply_texts(reply))}")

        echo(f"Google access is authorized; credentials are in {credentials_dir}")
        return True
    finally:
        _stop(process)


def _probe(process: Any, replies: Any, call_id: int, deadline: float, echo: Echo) -> dict[str, Any]:
    """Call the probe tool once and echo whatever the server answers (URLs included)."""
    _send(process, {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                    "params": {"name": PROBE_TOOL, "arguments": {}}})
    reply = _await_reply(replies, call_id, deadline, echo)
    for text in _reply_texts(reply):
        echo(text)
    return reply


def _wait_for_credentials(
    credentials_dir: Path,
    before: set[tuple[str, int, int]],
    deadline: float,
    sleep: Sleep,
    echo: Echo,
) -> bool:
    """Poll until the sign-in writes a credential file that was not there before."""
    echo(f"waiting for the sign-in to write credentials to {credentials_dir}…")
    while True:
        if _credentials_snapshot(credentials_dir) - before:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(CREDENTIALS_POLL_S)


def _credentials_snapshot(credentials_dir: Path) -> set[tuple[str, int, int]]:
    """Name/mtime/size of every credential file, so a *rewritten* one counts as new too."""
    if not credentials_dir.is_dir():
        return set()
    snapshot = set()
    for path in credentials_dir.iterdir():
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - a file that vanished mid-scan
            continue
        snapshot.add((path.name, stat.st_mtime_ns, stat.st_size))
    return snapshot


def _looks_like_authorization(reply: dict[str, Any]) -> bool:
    """Is this the server asking for a browser sign-in rather than answering?"""
    if not _is_failure(reply):
        return False
    haystack = " ".join(_reply_texts(reply)).lower()
    return any(hint in haystack for hint in AUTH_HINTS)


def _is_failure(reply: dict[str, Any]) -> bool:
    """A JSON-RPC error, or a tool result flagged `isError`."""
    if "error" in reply:
        return True
    result = reply.get("result")
    return bool(isinstance(result, dict) and result.get("isError"))


def _require_oauth_client(settings: Settings) -> None:
    """Refuse before starting anything when there is no OAuth client, pair or file."""
    if settings.google_oauth_client() is None:
        raise GoogleSetupError(NO_CLIENT)


# --- stdio JSON-RPC --------------------------------------------------------


def _start_reader(process: Any) -> "queue.Queue[str | None]":
    """Drain the server's stdout on a daemon thread; `None` is queued at EOF."""
    replies: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in iter(process.stdout.readline, ""):
                replies.put(line)
        finally:
            replies.put(None)

    threading.Thread(target=pump, name="workspace-mcp-stdout", daemon=True).start()
    return replies


def _send(process: Any, message: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC message to the server."""
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _await_reply(
    replies: "queue.Queue[str | None]", wanted_id: int, deadline: float, echo: Echo
) -> dict[str, Any]:
    """The whole JSON-RPC message answering `wanted_id`, echoing anything else on the way.

    A JSON-RPC `error` comes back as part of the message rather than as an exception: for
    the probe call, "you have to authorize first" arrives that way and is not a failure.
    Only a server that dies or goes silent raises.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GoogleSetupError("timed out waiting for the workspace-mcp server")
        try:
            line = replies.get(timeout=remaining)
        except queue.Empty:
            raise GoogleSetupError("timed out waiting for the workspace-mcp server") from None
        if line is None:
            raise GoogleSetupError("the workspace-mcp server exited before answering")

        try:
            message = json.loads(line)
        except ValueError:
            # Not JSON-RPC: server logging, and where the consent URL usually shows up.
            if line.strip():
                echo(line.rstrip())
            continue
        if not isinstance(message, dict) or message.get("id") != wanted_id:
            continue  # a notification, or a reply to something we are not waiting for
        return message


def _error_text(error: Any) -> str:
    """The human-readable half of a JSON-RPC error object."""
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _reply_texts(reply: dict[str, Any]) -> list[str]:
    """Everything human-readable in a reply: the error message, or the result's text blocks."""
    if "error" in reply:
        return [_error_text(reply["error"])]
    result = reply.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


def _stop(process: Any) -> None:
    """Always leave the machine without a stray MCP server on it."""
    try:
        process.stdin.close()
    except Exception:  # pragma: no cover - a closed pipe is fine
        pass
    try:
        process.terminate()
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - only a wedged server gets here
        process.kill()
    except Exception:  # pragma: no cover - already gone
        log.debug("the workspace-mcp server was already stopped", exc_info=True)


# --- the OAuth client -------------------------------------------------------------------


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


# --- Gmail, read-only, for check_email ------------------------------------------------------

AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
#: Where Google sends the browser afterwards. Nothing listens there: the code is read out
#: of the address bar instead, so this works from a phone while Keryx runs headless.
REDIRECT_URI = "http://localhost:1"
PENDING_FILE = "gmail_signin.json"
GMAIL_PROFILE = "profile"


def _pending_path(settings: Settings) -> Path:
    return settings.data_dir / PENDING_FILE


def _client(settings: Settings) -> tuple[str, str]:
    client = settings.google_oauth_client()
    if client is None:
        raise GoogleSetupError(NO_CLIENT)
    return client


def start_signin(settings: Settings) -> str:
    """The consent URL; the verifier and state wait in `DATA_DIR/gmail_signin.json` (0600)."""
    client_id, _ = _client(settings)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    write_private(
        _pending_path(settings), json.dumps({"state": state, "code_verifier": verifier})
    )
    return f"{AUTH_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )


def finish_signin(
    settings: Settings,
    redirected_to: str,
    *,
    post: Callable[..., Any] | None = None,
) -> Path:
    """Exchange the code in `redirected_to` for a refresh token, saved at 0600.

    `post` is `httpx.post`, looked up when called so that nothing bound at import time can
    reach the network from a test that patched it.
    """
    post = post or httpx.post
    pending_path = _pending_path(settings)
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoogleSetupError(
            "no sign-in in progress: run `keryx auth login gmail` first"
        ) from exc
    query = parse_qs(urlparse(redirected_to.strip()).query)
    if query.get("error"):
        raise GoogleSetupError(f"Google said: {query['error'][0]}")
    if query.get("state", [""])[0] != pending["state"]:
        raise GoogleSetupError(
            "that address belongs to a different sign-in; start again with "
            "`keryx auth login gmail`"
        )
    code = query.get("code", [""])[0]
    if not code:
        raise GoogleSetupError("that address has no code in it; copy the whole address bar")
    if SCOPE not in query.get("scope", [SCOPE])[0].split():
        raise GoogleSetupError("read access to Gmail was not granted; approve it and try again")

    client_id, client_secret = _client(settings)
    response = post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": pending["code_verifier"],
        },
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "refresh_token" not in body:
        raise GoogleSetupError(
            f"Google refused the code ({body.get('error', response.status_code)}); "
            "start again with `keryx auth login gmail`"
        )
    path = write_private(
        token_path(settings),
        json.dumps(
            {
                "refresh_token": body["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
                "token_uri": TOKEN_URL,
                "scopes": [SCOPE],
            }
        ),
    )
    pending_path.unlink(missing_ok=True)
    return path


async def gmail_address(settings: Settings) -> str:
    """The signed-in account's address, from one read-only call: proof the token works."""
    gmail = HttpGmail(token_path(settings))
    try:
        profile = await gmail.get(GMAIL_PROFILE)
    except Exception as exc:
        raise GoogleSetupError(f"Gmail would not answer: {exc}") from None
    return str(profile.get("emailAddress") or "")


# --- the wizard section -------------------------------------------------------------------

GUIDE = "google.md"


def guide() -> str:
    """The numbered Google Cloud steps, shared with the wiki (`setup/guides/google.md`)."""
    return resources.files("keryx.setup.guides").joinpath(GUIDE).read_text(encoding="utf-8")


def run_section(ctx: "SetupContext") -> None:
    """Agents that send mail and manage the calendar: the client, then a sign-in and a read.

    Email answers on a call are the `check_email` plugin, in the Plugins section, which
    reuses `_ensure_client` and `_sign_in_email` from here.
    """
    from keryx.setup.ui import Choice

    ui, settings = ctx.ui, ctx.settings
    ui.note("Agents can send mail and manage your calendar through workspace-mcp. Email "
            "answers on a call are a plugin, in the Plugins section.")
    if "claude" in settings.enabled_agents:
        ui.note("Claude's own claude.ai Gmail and Calendar connectors already reach its agents.")
    if ui.select(
        "Let agents send email and manage your calendar?",
        [Choice("skip", "Skip for now"), Choice("setup", "Set up", hint="workspace-mcp")],
        default="setup" if "codex" in settings.enabled_agents else "skip",
    ) == "skip":
        ui.note("Left for later: `keryx setup --all` comes back to it.")
        return
    if not _ensure_client(ctx, calendar=True):
        return
    _sign_in_agents(ctx)


def _ensure_client(ctx: "SetupContext", *, calendar: bool) -> bool:
    """The one-time Google Cloud client: skipped when one is configured already."""
    ui = ctx.ui
    if ctx.settings.google_oauth_client() is not None:
        ui.success("Google Cloud client: already configured")
        return True
    text = guide()
    if not calendar:
        text = "\n".join(line for line in text.splitlines() if "Calendar API" not in line)
    ui.markdown(text)
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


def _sign_in_email(ctx: "SetupContext") -> None:
    ui = ctx.ui
    url = start_signin(ctx.settings)
    ui.panel(
        "Email answers — open this on any device",
        f"{url}\n\nApprove read-only Gmail access. The page you land on will not load: "
        "copy its whole address and paste it below.",
    )
    while True:
        answer = ui.text("The address you landed on (blank to skip)")
        if not answer:
            return
        try:
            finish_signin(ctx.settings, answer, post=ctx.probes.http_post)
            break
        except GoogleSetupError as exc:
            ui.error(str(exc))
    try:
        with ui.spinner("Reading your Gmail profile…"):
            address = asyncio.run(ctx.probes.gmail_address(ctx.settings))
    except GoogleSetupError as exc:
        ui.error(str(exc))
        return
    ui.success(f"email answers: signed in as {address or 'your account'}")
    if address and not ctx.settings.user_google_email:
        ctx.save({"USER_GOOGLE_EMAIL": address})


def _sign_in_agents(ctx: "SetupContext") -> None:
    ui = ctx.ui
    if not ctx.save({"GOOGLE_WORKSPACE_MCP": True}):
        return
    if not ctx.settings.user_google_email:
        address = ui.text("Your Google address (the account agents act as)")
        if address:
            ctx.save({"USER_GOOGLE_EMAIL": address})
    ui.panel(
        "Agents — sign in",
        "workspace-mcp prints a link: open it on any device and approve. Its consent page "
        "listens on localhost:8000, so on a machine with no browser run this first, from "
        "the one you are at:\n\n    ssh -L 8000:localhost:8000 <this host>",
    )
    try:
        ctx.probes.workspace_signin(ctx.settings, ui.note)
    except GoogleSetupError as exc:
        ui.error(f"agents: {exc}")
        return
    ui.success("agents: Gmail and Calendar are authorized (list_calendars answered)")
