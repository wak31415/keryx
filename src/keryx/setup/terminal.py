"""The terminal a question was asked at, after the answer: what is left waiting there.

A paste of several lines is answered by its first: the rest is still waiting to be read,
and would be read as the answers to whatever is asked next, shown as typed. So after a
hidden question, and after a refused answer, whatever is waiting is drained and thrown
away (`drop_typed_ahead`): read with the terminal's echo off until it has been quiet for a
moment, since a paste can arrive in several quick writes, then flushed, along with any
keys prompt_toolkit read ahead and keeps for its next prompt. Nothing happens without a
terminal, or where there is no `termios` to drive one with.

A client file pasted at the command line is read whole, hidden (`read_hidden_paste`): line
after line with the echo off until it is a whole JSON object, so no line of it is ever
shown, however it was formatted. It stops at a blank line, and when no next line follows
within a moment, so typing that comes later is never taken for part of the paste.
"""

import contextlib
import json
import os
import select
import sys
import time
from collections.abc import Iterator

try:
    import termios
except ImportError:  # pragma: no cover - not on Linux or macOS
    termios = None  # type: ignore[assignment]

#: How long the terminal has to stay quiet for a paste to be over.
QUIET_S = 0.1
#: How long a paste may pause between two of its lines before it is taken to be over.
LINE_WAIT_S = 0.5
#: The longest a drain takes, however much keeps arriving.
DRAIN_LIMIT_S = 1.0
#: The most lines a pasted client file is read to; Google's is one, pretty-printed ten.
MAX_PASTE_LINES = 64


def _terminal_fd() -> int | None:
    if termios is None:  # pragma: no cover - not on Linux or macOS
        return None
    try:
        return sys.stdin.fileno() if sys.stdin.isatty() else None
    except (AttributeError, OSError, ValueError):
        return None


@contextlib.contextmanager
def _no_echo(fd: int) -> Iterator[None]:
    """The terminal's echo off for the length of the block, and back as it was after."""
    try:
        before = termios.tcgetattr(fd)
        quiet = list(before)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    except (termios.error, OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(termios.error, OSError, ValueError):
            termios.tcsetattr(fd, termios.TCSANOW, before)


def drop_typed_ahead(*, quiet_s: float = QUIET_S, limit_s: float = DRAIN_LIMIT_S) -> None:
    """Throw away whatever is waiting to be read at the terminal, never showing it."""
    fd = _terminal_fd()
    if fd is None:
        return
    with _no_echo(fd), contextlib.suppress(termios.error, OSError, ValueError):
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], quiet_s)
            if not ready or not os.read(fd, 65536):
                break
        termios.tcflush(fd, termios.TCIFLUSH)
    with contextlib.suppress(Exception):  # no prompt_toolkit input here: nothing kept
        from prompt_toolkit.input.defaults import create_input
        from prompt_toolkit.input.typeahead import clear_typeahead

        clear_typeahead(create_input())


def _whole(text: str) -> bool:
    """Whether a paste read so far is all there is: JSON that parses, braces that close,
    or anything that is not JSON at all."""
    if not text.lstrip().startswith("{"):
        return True
    try:
        json.loads(text)
    except ValueError:
        return text.count("{") <= text.count("}")
    return True


def _more_follows(fd: int | None) -> bool:
    """Whether another line of the paste is on its way: at a terminal, only when it
    arrives within a moment; without one, standard input is read as it comes."""
    if fd is None:
        return True
    try:
        ready, _, _ = select.select([fd], [], [], LINE_WAIT_S)
    except (OSError, ValueError):
        return False
    return bool(ready)


def read_hidden_paste(question: str) -> str:
    """Ask `question` and read a pasted client file, hidden, over as many lines as it was
    pasted on. Ctrl-C is `KeyboardInterrupt` and the end of input `EOFError`, as getpass
    has them. Without a terminal, standard input as it comes."""
    sys.stdout.write(f"{question}: ")
    sys.stdout.flush()
    fd = _terminal_fd()
    lines: list[str] = []
    with _no_echo(fd) if fd is not None else contextlib.nullcontext():
        while len(lines) < MAX_PASTE_LINES:
            if lines and not _more_follows(fd):
                break
            line = sys.stdin.readline()
            if not line:
                if not lines:
                    raise EOFError
                break
            if lines and not line.strip():
                break  # a blank line ends a paste
            lines.append(line.rstrip("\n"))
            if _whole("\n".join(lines)):
                break
    sys.stdout.write("\n")
    drop_typed_ahead()
    return "\n".join(lines)
