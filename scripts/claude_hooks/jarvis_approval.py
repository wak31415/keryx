#!/usr/bin/env python3
"""The Claude Code half of the approval bridge — the *canonical* copy.

`scripts/install-claude-hook.sh` copies this into `~/.claude/hooks/` and that copy is what
the CLI runs, deliberately: this repository is a shared checkout that several agents edit
at once, and a hook read straight out of it would change under a live session mid-prompt.

What it does, in one sentence per event:

- **`PermissionRequest`** — a prompt is about to go on the owner's screen. Hand it to Jarvis and
  block. Jarvis draws nothing, changes nothing and, if they answer at the keyboard within
  five minutes, says nothing back; the prompt behaves exactly as it does today. Only if they
  do not does Jarvis ring them, and only a PIN-verified keypad digit ever produces a
  decision here.
- **`PostToolUse` / `PermissionDenied` / `Stop` / `SessionEnd`** — that prompt is not
  waiting any more. Tell Jarvis so it does not ring them about something they have dealt with.
  This runs on *every* tool call, so its first act is one `stat`: no pending marker, no
  work, no socket.

Three rules it never breaks: it is stdlib-only (it runs before anything is installed), it
prints nothing unless Jarvis returned a real decision, and **every** failure — no socket, a
timeout, a malformed reply, a bug in this file — ends in silence and exit 0, which leaves
the ordinary on-screen prompt exactly as it was.
"""

import json
import os
import socket
import sys

PROTOCOL = 1
RAISE_EVENT = "PermissionRequest"
RESOLVE_EVENTS = frozenset({"PostToolUse", "PermissionDenied", "Stop", "SessionEnd"})

#: Only these fields are sent. `transcript_path` in particular is not: Jarvis has no use
#: for the conversation, and what it cannot receive it cannot log.
KEEP_FIELDS = (
    "hook_event_name",
    "session_id",
    "cwd",
    "prompt_id",
    "permission_mode",
    "tool_name",
)
#: `Write` carries whole file contents. The hash Jarvis matches a resolution on is taken
#: *after* this trim, and the trim is the same on both events, so the two still agree.
#: Anything the trim shortens is reported as `truncated`: the CLI runs the original, not
#: what Jarvis was shown, so Jarvis refuses to escalate a request it has only seen part of.
MAX_STRING = 4096
MAX_LIST = 50
MAX_DEPTH = 6
MAX_STDIN = 4 * 1024 * 1024
MAX_REPLY = 64 * 1024
CONNECT_TIMEOUT_S = 2.0
RESOLVE_TIMEOUT_S = 3.0
#: A backstop only. Jarvis closes the connection at its own deadline, which is shorter.
DEFAULT_MAX_WAIT_S = 600.0


def _state_dir():
    """Where the broker listens: `JARVIS_STATE_DIR`, else `$XDG_STATE_HOME/jarvis`.

    The installer always writes `JARVIS_STATE_DIR` into the hook's command, so the default
    is only for a hook somebody wired up by hand. `XDG_STATE_HOME` counts only when it is
    absolute, as the specification says and as Jarvis itself resolves it.
    """
    configured = os.environ.get("JARVIS_STATE_DIR")
    if configured:
        return os.path.expanduser(configured)
    base = os.environ.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(base):
        base = os.path.expanduser("~/.local/state")
    return os.path.join(base, "jarvis")


def _trim(value, cuts, depth=0):
    """`value` with every string bounded, so no file's contents ride the socket.

    Every shortening — a string, a list, a nest too deep — is appended to `cuts`.
    """
    if depth > MAX_DEPTH:
        cuts.append("depth")
        return "…"
    if isinstance(value, str):
        if len(value) <= MAX_STRING:
            return value
        cuts.append("string")
        return value[:MAX_STRING] + "…"
    if isinstance(value, dict):
        return {str(key): _trim(item, cuts, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            cuts.append("list")
        return [_trim(item, cuts, depth + 1) for item in value[:MAX_LIST]]
    return value


def _slim(event):
    """The subset of the hook payload Jarvis is given, and whether it had to be cut."""
    slim = {field: event.get(field) for field in KEEP_FIELDS if event.get(field) is not None}
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        cuts = []
        slim["tool_input"] = _trim(tool_input, cuts)
        slim["truncated"] = bool(cuts)
    return slim


def _exchange(payload, path, timeout):
    """Send one request and read one reply. None for anything that did not work."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(path)
        sock.sendall((json.dumps(payload) + "\n").encode())
        sock.settimeout(timeout)
        buffer = b""
        while b"\n" not in buffer and len(buffer) < MAX_REPLY:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        if not buffer.strip():
            return None
        return json.loads(buffer.split(b"\n", 1)[0])
    except (OSError, ValueError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _decision(reply):
    """The `{behavior, message}` Jarvis decided, or None. Anything unexpected is None.

    Validated rather than trusted: this is the one place a message on a socket turns into
    a tool call running, so a reply that is not exactly the shape we know is discarded.
    """
    if not isinstance(reply, dict):
        return None
    decision = reply.get("decision")
    if not isinstance(decision, dict):
        return None
    behavior = decision.get("behavior")
    message = decision.get("message")
    if behavior not in ("allow", "deny") or not isinstance(message, str) or not message.strip():
        return None
    return {"behavior": behavior, "message": message}


def main():
    event = json.loads(sys.stdin.read(MAX_STDIN) or "{}")
    if not isinstance(event, dict):
        return
    name = event.get("hook_event_name")
    state_dir = _state_dir()
    socket_path = os.path.join(state_dir, "approvals.sock")

    if name in RESOLVE_EVENTS:
        # One stat, and on all but a handful of tool calls a year that is the whole cost.
        if not os.path.exists(os.path.join(state_dir, "approvals", "PENDING")):
            return
        _exchange(
            {"op": "resolve", "protocol": PROTOCOL, "event": _slim(event)},
            socket_path,
            RESOLVE_TIMEOUT_S,
        )
        return

    if name != RAISE_EVENT:
        return
    if os.path.exists(os.path.join(state_dir, "approvals", "DISABLED")):
        return
    try:
        max_wait = float(os.environ.get("JARVIS_APPROVAL_MAX_WAIT") or DEFAULT_MAX_WAIT_S)
    except ValueError:
        max_wait = DEFAULT_MAX_WAIT_S

    decision = _decision(
        _exchange(
            {"op": "raise", "protocol": PROTOCOL, "event": _slim(event)}, socket_path, max_wait
        )
    )
    if decision is None:
        return  # nobody answered: the prompt is still on their screen, untouched
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": RAISE_EVENT,
                    "decision": decision,
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # never, ever fail closed: silence leaves the prompt exactly as it is
        pass
    sys.exit(0)
