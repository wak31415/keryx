"""`jarvis setup-google`: walk the Google OAuth flow once, so cowork tasks can run.

Cowork subagents reach Gmail and Calendar through the `workspace-mcp` stdio server
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
from typing import Any

from jarvis.config import Settings
from jarvis.tasks.agent_runner import google_mcp_server_config

log = logging.getLogger("jarvis.google_setup")

MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "jarvis-setup-google", "version": "1"}

#: A harmless read-only tool, called only to trigger the OAuth flow.
PROBE_TOOL = "list_calendars"

DEFAULT_TIMEOUT_S = 180.0
#: How long the server gets to shut down politely before it is killed.
STOP_TIMEOUT_S = 5.0

_INITIALIZE_ID = 1
_PROBE_ID = 2

INSTRUCTIONS = (
    "Starting the Google Workspace MCP server once to authorize this machine.\n"
    "A browser window will open: complete the Google sign-in and grant Gmail and "
    "Calendar access.\n"
    "If no window opens, copy the URL printed below into a browser."
)

Popen = Callable[..., Any]
Echo = Callable[[str], None]


class GoogleSetupError(RuntimeError):
    """The setup flow could not be completed; the message says how far it got."""


def run_google_setup(
    settings: Settings,
    *,
    popen: Popen = subprocess.Popen,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    echo: Echo = print,
) -> bool:
    """Run the one-off OAuth flow. Returns True on success, raises `GoogleSetupError` otherwise."""
    _require_oauth_client(settings)

    config = google_mcp_server_config(settings)
    credentials_dir = config["env"]["GOOGLE_MCP_CREDENTIALS_DIR"]
    argv = [config["command"], *config["args"]]

    echo(INSTRUCTIONS)
    echo(f"credentials will be stored under {credentials_dir}")
    log.info("starting %s for the google oauth flow", " ".join(argv))

    process = popen(
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

        _send(process, {"jsonrpc": "2.0", "id": _INITIALIZE_ID, "method": "initialize",
                        "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                                   "capabilities": {}, "clientInfo": CLIENT_INFO}})
        _await_reply(replies, _INITIALIZE_ID, deadline, echo)

        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(process, {"jsonrpc": "2.0", "id": _PROBE_ID, "method": "tools/call",
                        "params": {"name": PROBE_TOOL, "arguments": {}}})
        result = _await_reply(replies, _PROBE_ID, deadline, echo)

        for text in _result_texts(result):
            echo(text)
        if result.get("isError"):
            raise GoogleSetupError(f"{PROBE_TOOL} failed: {' '.join(_result_texts(result))}")

        echo(f"Google access is authorized; credentials are in {credentials_dir}")
        return True
    finally:
        _stop(process)


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
    """The reply to `wanted_id`, echoing everything else the server says on the way."""
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
        if "error" in message:
            raise GoogleSetupError(f"the server refused: {_error_text(message['error'])}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _error_text(error: Any) -> str:
    """The human-readable half of a JSON-RPC error object."""
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _result_texts(result: dict[str, Any]) -> list[str]:
    """The text blocks of an MCP `tools/call` result."""
    content = result.get("content")
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
