"""What is left waiting at the terminal after a question: drained, then flushed."""

import sys
import termios

import pytest

from keryx.setup import terminal


class Terminal:
    def isatty(self):
        return True

    def fileno(self):
        return 7


def test_a_paste_arriving_in_several_writes_is_drained_then_flushed(monkeypatch):
    chunks = [b'"client_id": "x",\n', b'"client_secret": "y"\n}\n']
    reads, flushed, cleared = [], [], []
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(terminal.select, "select",
                        lambda r, w, x, timeout: (r if chunks else [], [], []))
    monkeypatch.setattr(terminal.os, "read",
                        lambda fd, size: reads.append(fd) or chunks.pop(0))
    monkeypatch.setattr(termios, "tcflush", lambda fd, how: flushed.append((fd, how)))
    import prompt_toolkit.input.typeahead as typeahead

    monkeypatch.setattr(typeahead, "clear_typeahead", lambda given: cleared.append(given))
    monkeypatch.setattr("prompt_toolkit.input.defaults.create_input", lambda: "the input")

    terminal.drop_typed_ahead()

    assert reads == [7, 7] and chunks == []
    assert flushed == [(7, termios.TCIFLUSH)]
    assert cleared == ["the input"]


def test_a_drain_stops_at_its_limit_however_much_keeps_coming(monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(terminal.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(terminal.select, "select", lambda r, w, x, timeout: (r, [], []))
    monkeypatch.setattr(terminal.os, "read", lambda fd, size: b"more")
    monkeypatch.setattr(termios, "tcflush", lambda fd, how: None)

    terminal.drop_typed_ahead(limit_s=3)

    assert next(clock) < 10


def test_nothing_happens_without_a_terminal(monkeypatch):
    monkeypatch.setattr(terminal.select, "select", lambda *a: (_ for _ in ()).throw(
        AssertionError("no terminal to read")))

    terminal.drop_typed_ahead()  # pytest's standard input is not a terminal


def test_a_standard_input_with_no_descriptor_is_left_alone(monkeypatch):
    class Odd:
        def isatty(self):
            return True

        def fileno(self):
            raise ValueError("no descriptor")

    monkeypatch.setattr(sys, "stdin", Odd())

    terminal.drop_typed_ahead()


class Typed:
    """Standard input with lines waiting, a terminal or not."""

    def __init__(self, text, tty=False):
        self.lines, self.tty = text.splitlines(keepends=True), tty

    def isatty(self):
        return self.tty

    def fileno(self):
        return 7

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


def test_a_file_pasted_over_several_lines_is_read_whole(monkeypatch, capsys):
    pretty = '{\n  "installed": {\n    "client_id": "x"\n  }\n}\n'
    monkeypatch.setattr(sys, "stdin", Typed(pretty + "the next answer\n"))

    assert terminal.read_hidden_paste("Paste it") == pretty.rstrip("\n")
    assert sys.stdin.lines == ["the next answer\n"]  # read no further than the file
    assert capsys.readouterr().out == "Paste it: \n"


def test_one_line_that_is_not_json_is_all_there_is(monkeypatch):
    monkeypatch.setattr(sys, "stdin", Typed("not json\nmore\n"))

    assert terminal.read_hidden_paste("Paste it") == "not json"


def test_a_file_cut_short_is_read_to_the_end_of_input(monkeypatch):
    monkeypatch.setattr(sys, "stdin", Typed('{\n  "a": 1,\n'))

    assert terminal.read_hidden_paste("Paste it") == '{\n  "a": 1,'


def test_nothing_to_read_is_the_end_of_input(monkeypatch):
    monkeypatch.setattr(sys, "stdin", Typed(""))

    with pytest.raises(EOFError):
        terminal.read_hidden_paste("Paste it")


def test_at_a_terminal_the_echo_is_off_while_it_is_read_and_back_after(monkeypatch):
    modes = []
    monkeypatch.setattr(sys, "stdin", Typed('{"a": 1}\n', tty=True))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [0, 0, 0, termios.ECHO | 1, 0, 0, []])
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, mode: modes.append(mode[3]))
    monkeypatch.setattr(terminal, "drop_typed_ahead", lambda: modes.append("dropped"))

    assert terminal.read_hidden_paste("Paste it") == '{"a": 1}'
    assert modes == [1, termios.ECHO | 1, "dropped"]


def test_a_terminal_whose_mode_cannot_be_read_is_read_as_it_is(monkeypatch):
    def refuse(fd):
        raise termios.error("no")

    monkeypatch.setattr(sys, "stdin", Typed('{"a": 1}\n', tty=True))
    monkeypatch.setattr(termios, "tcgetattr", refuse)
    monkeypatch.setattr(terminal, "drop_typed_ahead", lambda: None)

    assert terminal.read_hidden_paste("Paste it") == '{"a": 1}'
