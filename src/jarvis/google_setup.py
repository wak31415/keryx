"""`jarvis setup-google`: walk the Google OAuth flow once, so mail and calendar work can run.

Subagents reach Gmail and Calendar through the `workspace-mcp` stdio server
(spec §4). That server only opens its browser consent screen when a tool is actually
called, and a subagent that hits it mid-call would stall behind a login nobody is
watching. So this module starts the very same server by hand — same command, same env as
`google_mcp_server_config` builds for the runner — speaks just enough MCP over stdio to
call one harmless tool, and lets the browser flow complete while the user is sitting
there. The credentials it leaves in `data_dir/google` are what the subagents reuse later.

The MCP stdio framing is newline-delimited JSON-RPC: `initialize` (request),
`notifications/initialized` (notification), then `tools/call`. Anything the server writes
that is not a JSON-RPC reply — including the consent URL — is echoed straight through, so
the user can copy the link if the browser does not open by itself.

The probe answering "you have to authorize first" is the *expected* first reply, not a
failure: that reply is what opens the consent screen, and the server has to stay running
to receive the OAuth callback on localhost. So the flow prints the URL, then waits for the
sign-in by polling `GOOGLE_MCP_CREDENTIALS_DIR` for a credential file that was not already
there (mtime and size included, so a refreshed one counts), and only calls the probe again
— and stops the server — once something has been written or the deadline passes.

This is a dev-time convenience that cannot be exercised against a real Google account in
tests, so it is deliberately defensive: every wait has a deadline, the subprocess is
always stopped, and failures come back as `GoogleSetupError` with what was seen last.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.agents.base import google_mcp_server_config
from jarvis.config import Settings

log = logging.getLogger("jarvis.google_setup")

MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "jarvis-setup-google", "version": "1"}

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
    "A browser window will open: complete the Google sign-in and grant Gmail and "
    "Calendar access.\n"
    "If no window opens, copy the URL printed below into a browser."
)

SIGN_IN_INSTRUCTIONS = (
    "Complete the Google sign-in in the browser now — this window is waiting for it "
    "(the server must stay running to receive the callback)."
)

Popen = Callable[..., Any]
Echo = Callable[[str], None]
Sleep = Callable[[float], None]


class GoogleSetupError(RuntimeError):
    """The setup flow could not be completed; the message says how far it got."""


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
    """Refuse before starting anything when the OAuth client is not configured."""
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.google_oauth_client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.google_oauth_client_secret),
        )
        if not value
    ]
    if missing:
        raise GoogleSetupError(
            f"set {' and '.join(missing)} in .env first "
            "(Google Cloud console → APIs & Services → Credentials → OAuth client ID, "
            "type 'Desktop app')"
        )


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
