"""A sign-in's child process behind a pseudo-terminal, and the text its screen shows.

`claude setup-token` draws its sign-in with Ink, and only on a terminal: words placed by
cursor moves rather than spaces, the link as an OSC 8 hyperlink around the same text. So it
runs behind one here — the standard library's `os.openpty`, no expect library — wide enough
that a link of several hundred characters is never wrapped, in a session of its own so that
`stop` ends everything it started. `visible` turns what it drew back into words.

What such a child prints can carry a credential, so nothing here logs, echoes or raises
with it: `read` hands the bytes to the one caller, which decides on fixed sentences.
"""

import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

#: The terminal's size: wide enough for a sign-in link on one line, tall enough for Ink's
#: full banner (it draws a short one below 30 rows).
COLUMNS, ROWS = 500, 50
#: Between typing a line and pressing Enter, so the two arrive as two keystrokes.
SUBMIT_PAUSE_S = 0.3
#: How long a child gets to go after SIGTERM before SIGKILL.
STOP_AFTER_S = 2.0

#: OSC (`ESC ]`, to BEL or ST): a title or a hyperlink's target, never on screen.
_OSC = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
#: CSI (`ESC [`): colour, and the cursor moves Ink spaces its words with.
_CSI = r"\x1b\[[0-?]*[ -/]*[@-~]"
#: Any other escape: `ESC 7`, `ESC 8`, `ESC =`, …
_ESC = r"\x1b[ -/]*[0-~]"


class Child(Protocol):
    """A sign-in's child process, as a sign-in reads and answers it."""

    def read(self, timeout: float) -> bytes | None:
        """What it printed within `timeout` seconds — b"" for nothing yet — or None once it
        has closed its end, which is once it has exited."""

    def submit(self, line: str) -> None:
        """Type `line`, then Enter."""

    def wait(self, timeout: float) -> int | None:
        """Its exit code, or None while it is still running after `timeout` seconds."""

    def stop(self) -> None:
        """End it, and everything it started, if it has not ended; idempotent."""


class PtyChild:
    """`argv` on a pseudo-terminal of its own, in a session of its own."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None,
        *,
        pause: Callable[[float], None] = time.sleep,
        stop_after: float = STOP_AFTER_S,
    ) -> None:
        self._pause, self._stop_after = pause, stop_after
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
            self._process = subprocess.Popen(
                list(argv), stdin=slave, stdout=slave, stderr=slave,
                env=None if env is None else dict(env),
                start_new_session=True, close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master: int | None = master
        self._stopped = False
        self._group_gone = False

    def read(self, timeout: float) -> bytes | None:
        if self._master is None:
            return None
        ready, _, _ = select.select([self._master], [], [], timeout)
        if not ready:
            return b""
        try:
            data = os.read(self._master, 65536)
        except OSError:  # EIO: Linux's way of saying the other end has closed
            data = b""
        if not data:
            self._close()
            return None
        return data

    def submit(self, line: str) -> None:
        if self._master is None:
            return
        os.write(self._master, line.encode())
        self._pause(SUBMIT_PAUSE_S)
        os.write(self._master, b"\r")

    def wait(self, timeout: float) -> int | None:
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        """SIGTERM to the whole session, `stop_after` seconds' grace, then SIGKILL to whatever
        is left of it — sent even when the child itself has already exited (its group id is
        still its pid, and what it started may live on), and sent again on the way out, so a
        second Cancel that cuts the first SIGKILL short still ends in one."""
        if self._stopped:
            return
        self._stopped = True
        try:
            try:
                self._signal(signal.SIGTERM)
                self.wait(self._stop_after)
            finally:
                self._signal(signal.SIGKILL)
                self._process.wait()
        finally:
            self._signal(signal.SIGKILL)  # costs nothing when the first one went out
            self._close()

    def _signal(self, signum: int) -> None:
        """`signum` to the child's process group, unless that group has been seen gone.

        Once the child is reaped its pid is free, and so, when nothing it started lives on,
        is its group id: so the group is asked about first (signal 0), and a group found
        empty — or not ours — is never signalled again, rather than signal a recycled one.
        """
        if self._group_gone:
            return
        group = self._process.pid
        try:
            if self._process.returncode is not None:
                os.killpg(group, 0)
            os.killpg(group, signum)
        except (ProcessLookupError, PermissionError):
            self._group_gone = True

    def _close(self) -> None:
        if self._master is not None:
            os.close(self._master)
            self._master = None


def visible(screen: str) -> str:
    """`screen` as the words on it: each cursor move a space, colour and links' targets gone.

    A hyperlink's text is on screen as well as in its target, so a link is still read once.
    """
    text = re.sub(_OSC, "", screen)
    text = re.sub(_CSI, " ", text)
    return re.sub(_ESC, "", text).replace("\r", "")
