"""A sign-in's child behind a pseudo-terminal, and the text its screen shows.

The child here is a few lines of this interpreter's own Python: a terminal to run on is the
thing under test, and no agent's CLI is ever started.
"""

import errno
import os
import sys
import time

import pytest

from keryx.setup import terminal
from keryx.setup.terminal import PtyChild, visible

#: Says whether it has a terminal and how wide, reads one line, says it back, exits 3.
ECHO = (
    "import os, sys; "
    "print('tty', sys.stdin.isatty(), sys.stdout.isatty(), os.get_terminal_size().columns, "
    "flush=True); "
    "line = sys.stdin.readline(); print('got', line.strip().upper(), flush=True); "
    "sys.exit(3)"
)
#: Never ends by itself, and ignores SIGTERM: `stop` must still end it.
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"


def drain(child: PtyChild, *, until: str = "", seconds: float = 10.0) -> str:
    """Everything `child` shows until `until` is on screen, or it closes, or time runs out."""
    seen, deadline = b"", time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunk = child.read(0.1)
        if chunk is None:
            break
        seen += chunk
        if until and until in visible(seen.decode()):
            break
    return visible(seen.decode())


def test_a_child_runs_on_a_wide_terminal_takes_a_line_and_its_exit_is_read():
    child = PtyChild([sys.executable, "-c", ECHO], None, pause=lambda seconds: None)
    try:
        assert "tty True True 500" in drain(child, until="tty")
        child.submit("code#state")
        assert "got CODE#STATE" in drain(child)
        assert child.wait(5) == 3
        assert child.read(0.1) is None  # it has closed its end, for good
    finally:
        child.stop()


def test_stop_ends_a_child_that_ignores_sigterm_and_a_second_stop_is_harmless():
    child = PtyChild([sys.executable, "-c", STUBBORN], None, stop_after=0.5)
    time.sleep(0.2)  # long enough for the handler to be set

    child.stop()

    assert child.wait(1) is not None
    assert child.read(0.1) is None
    child.stop()


def test_ink_cursor_moves_read_as_spaces_and_colour_and_links_vanish():
    screen = (
        "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>\r\r\n"
        "\x1b]8;id=1;https://example.com/a\x07\x1b[38;5;246mhttps://example.com/a\x1b[39m"
        "\x1b]8;;\x07\x1b7\x1b8"
    )

    text = visible(screen)

    assert "Paste code here if prompted >" in " ".join(text.split())
    assert "\x1b" not in text and "\x07" not in text
    assert text.count("https://example.com/a") == 1


def test_a_program_that_is_not_there_raises_and_leaves_no_terminal_open(tmp_path):
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(FileNotFoundError):
        PtyChild([str(tmp_path / "no-such-cli")], None)
    assert len(os.listdir("/dev/fd")) == before


def test_a_quiet_child_reads_as_nothing_yet_and_sigterm_ends_it():
    child = PtyChild([sys.executable, "-c", "import time; time.sleep(60)"], None)

    assert child.read(0.05) == b""
    child.stop()
    assert child.wait(1) is not None


def test_a_closed_terminal_that_says_so_with_eio_is_the_end(monkeypatch):
    child = PtyChild([sys.executable, "-c", "print('x')"], None)
    child.wait(5)

    def closed(fd, size):
        raise OSError(errno.EIO, "Input/output error")  # how Linux says it

    monkeypatch.setattr(terminal.os, "read", closed)
    assert child.read(1) is None
    child.stop()


def test_typing_into_a_child_that_has_gone_is_harmless():
    child = PtyChild([sys.executable, "-c", "pass"], None, pause=lambda seconds: None)
    drain(child)

    child.submit("too late")
    assert child.read(0.1) is None
    child.stop()
