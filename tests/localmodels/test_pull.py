"""`keryx models pull`'s two reports: the progress bar, and the JSON lines the Mac app reads."""

import json
import os
import signal

import pytest

from keryx.localmodels import pull


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def lines(written: list[str]) -> list[dict]:
    return [json.loads(line) for line in written]


def test_progress_is_told_at_most_four_times_a_second():
    written: list[str] = []
    clock = Clock()
    events = pull.JsonLines(written.append, clock=clock)

    with events.downloading("Model", 10_000) as progress:
        progress(0)  # the first is always told: it is where a resumed download starts
        progress(10)  # 0.1 %, at once: not told
        clock.now = 0.1
        progress(20)  # 0.2 %, 100 ms on: not told
        clock.now = 0.25
        progress(30)  # 250 ms on: told
        clock.now = 0.3
        progress(140)  # a whole percent since the last: told, however soon

    assert lines(written) == [
        {"event": "progress", "done": 0, "total": 10_000},
        {"event": "progress", "done": 30, "total": 10_000},
        {"event": "progress", "done": 140, "total": 10_000},
    ]


def test_a_download_that_starts_again_from_nothing_says_so_at_once():
    """A server that ignores the range sends the file from its first byte."""
    written: list[str] = []
    events = pull.JsonLines(written.append, clock=Clock())

    with events.downloading("Model", 100) as progress:
        progress(50)
        progress(0)

    assert [event["done"] for event in lines(written)] == [50, 0]


def test_only_the_first_ending_is_told():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    events.done("/models/m.gguf")
    events.error("too late")
    events.cancelled()

    assert lines(written) == [{"event": "done", "path": "/models/m.gguf"}]
    assert events.ended


def test_an_interrupt_is_told_as_cancelled():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def interrupted() -> int:
        raise KeyboardInterrupt

    assert events.guard(interrupted) == pull.CANCELLED == 130
    assert lines(written) == [{"event": "cancelled"}]


def test_anything_unexpected_is_told_as_an_error():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def broken() -> int:
        raise PermissionError("no writing there")

    assert events.guard(broken) == 1
    assert lines(written) == [{"event": "error", "message": "no writing there"}]


def test_what_is_raised_after_the_ending_was_told_goes_on_unchanged():
    """A refusal tells its own error, then exits with its own code."""
    written: list[str] = []
    events = pull.JsonLines(written.append)

    class Exit(RuntimeError):
        pass

    def refused() -> int:
        events.error("keryx cannot do that yet")
        raise Exit

    with pytest.raises(Exit):
        events.guard(refused)
    assert len(written) == 1


def test_an_interrupt_after_the_ending_is_not_told_again():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def finished_then_interrupted() -> int:
        events.done("/m.gguf")
        raise KeyboardInterrupt

    assert events.guard(finished_then_interrupted) == 0  # the code the stream already says
    assert lines(written) == [{"event": "done", "path": "/m.gguf"}]


def test_an_interrupt_after_an_adoption_is_still_a_success():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def adopted_then_interrupted() -> int:
        events.adopted("/m.gguf", "/hub/m.gguf")
        raise KeyboardInterrupt

    assert events.guard(adopted_then_interrupted) == 0
    assert len(written) == 1


def test_an_interrupt_after_an_error_is_still_a_failure():
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def failed_then_interrupted() -> int:
        events.error("did not arrive whole")
        raise KeyboardInterrupt

    assert events.guard(failed_then_interrupted) == 1
    assert lines(written) == [{"event": "error", "message": "did not arrive whole"}]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_cancel_while_the_ending_is_written_waits_for_it(signum):
    """Held off until the line is out, so the stream never ends with no ending at all."""
    written: list[str] = []

    def write(line: str) -> None:
        os.kill(os.getpid(), signum)
        written.append(line)

    events = pull.JsonLines(write)

    def finishing() -> int:
        events.done("/m.gguf")
        return 0

    with pull.terminate_as_interrupt():
        assert events.guard(finishing) == 0

    assert lines(written) == [{"event": "done", "path": "/m.gguf"}]


def test_a_terminate_is_an_interrupt_only_while_it_lasts():
    before = signal.getsignal(signal.SIGTERM)

    with pull.terminate_as_interrupt(), pytest.raises(KeyboardInterrupt):
        os.kill(os.getpid(), signal.SIGTERM)

    assert signal.getsignal(signal.SIGTERM) is before


def test_a_handler_python_did_not_install_is_restored_as_the_default(monkeypatch):
    restored: list = []

    def record(signum, handler):  # `signal.signal` answers None for such a handler
        restored.append(handler)

    monkeypatch.setattr(signal, "signal", record)

    with pull.terminate_as_interrupt():
        pass

    assert restored[-1] is signal.SIG_DFL


@pytest.mark.parametrize(
    ("stopping", "ending", "code"),
    [(KeyboardInterrupt, "cancelled", pull.CANCELLED), (PermissionError, "error", 1)],
)
def test_a_second_cancel_while_the_ending_is_written_keeps_its_code(stopping, ending, code):
    """A Cancel pressed twice: the second lands as `cancelled` or `error` is written."""
    written: list[str] = []

    def write(line: str) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        written.append(line)

    events = pull.JsonLines(write)

    def stopped() -> int:
        raise stopping

    with pull.terminate_as_interrupt():
        assert events.guard(stopped) == code

    assert [event["event"] for event in lines(written)] == [ending]
