"""Tests for reading the service's logs back after a restart (spec §3.3).

Everything here is a real file in a tmp directory: the point of the module is byte offsets
into files that other processes append to, and a fake filesystem would test the fake.
"""

from pathlib import Path

import pytest

from jarvis.logscan import (
    LOG_NAMES,
    MAX_ERRORS,
    MAX_LINE_CHARS,
    LogErrors,
    errors_since,
    log_dir,
    marks,
)

TRACEBACK = """Traceback (most recent call last):
  File "/repo/src/jarvis/cli.py", line 12, in <module>
    from jarvis.tools import builtin
ModuleNotFoundError: No module named 'jarvis.tools.nope'
"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A `data_dir` with an empty `logs/` in it, as `ensure_dirs` would leave one."""
    log_dir(tmp_path).mkdir(parents=True)
    return tmp_path


def write(data_dir: Path, name: str, text: str) -> None:
    """Append `text` to one of the service's log files."""
    with (log_dir(data_dir) / name).open("a", encoding="utf-8") as handle:
        handle.write(text)


# --- marks -----------------------------------------------------------------


def test_marks_records_the_length_of_every_log(data_dir: Path) -> None:
    write(data_dir, "jarvis.log", "hello\n")
    taken = marks(data_dir)
    assert taken["jarvis.log"] == 6


def test_a_log_that_is_not_there_yet_is_marked_at_zero(data_dir: Path) -> None:
    assert marks(data_dir) == dict.fromkeys(LOG_NAMES, 0)


def test_marks_of_a_missing_log_directory_are_all_zero(tmp_path: Path) -> None:
    assert marks(tmp_path / "nowhere") == dict.fromkeys(LOG_NAMES, 0)


# --- errors_since ----------------------------------------------------------


def test_nothing_is_reported_from_before_the_mark(data_dir: Path) -> None:
    write(data_dir, "jarvis.log", "2026-08-26 ERROR   jarvis: last month's problem\n")
    taken = marks(data_dir)
    write(data_dir, "jarvis.log", "2026-08-26 INFO    jarvis: all is well\n")
    assert not errors_since(data_dir, taken)


def test_an_error_after_the_mark_is_reported(data_dir: Path) -> None:
    write(data_dir, "jarvis.log", "2026-08-26 INFO    jarvis: starting\n")
    taken = marks(data_dir)
    write(data_dir, "jarvis.log", "2026-08-26 ERROR   jarvis.tools: no such tool\n")
    found = errors_since(data_dir, taken)
    assert found.count == 1
    assert found.lines[0].endswith("no such tool")


def test_critical_counts_as_an_error(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.log", "2026-08-26 CRITICAL jarvis: the wheels came off\n")
    assert errors_since(data_dir, taken).count == 1


def test_a_traceback_is_reported_as_the_line_that_ends_it(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.err.log", TRACEBACK)
    found = errors_since(data_dir, taken)
    assert found.count == 1
    assert found.lines == ("ModuleNotFoundError: No module named 'jarvis.tools.nope'",)


def test_a_traceback_with_no_ending_still_reports_its_header(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.err.log", "Traceback (most recent call last):\n  File 'x', line 1\n")
    found = errors_since(data_dir, taken)
    assert found.count == 1
    assert found.lines[0].startswith("Traceback")


def test_back_to_back_tracebacks_are_counted_separately(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.err.log", TRACEBACK * 2)
    assert errors_since(data_dir, taken).count == 2


def test_every_log_file_is_scanned(data_dir: Path) -> None:
    taken = marks(data_dir)
    for name in LOG_NAMES:
        write(data_dir, name, f"ERROR something in {name}\n")
    assert errors_since(data_dir, taken).count == len(LOG_NAMES)


def test_only_the_last_few_lines_are_kept(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.log", "".join(f"ERROR problem {n}\n" for n in range(10)))
    found = errors_since(data_dir, taken)
    assert found.count == 10
    assert len(found.lines) == MAX_ERRORS
    assert found.lines[-1].endswith("problem 9")  # the tail: the failure still happening


def test_a_long_line_is_trimmed_for_speech(data_dir: Path) -> None:
    taken = marks(data_dir)
    write(data_dir, "jarvis.log", "ERROR " + "x" * 500 + "\n")
    (line,) = errors_since(data_dir, taken).lines
    assert len(line) == MAX_LINE_CHARS
    assert line.endswith("…")


def test_a_log_that_rotated_under_us_is_read_from_the_start(data_dir: Path) -> None:
    write(data_dir, "jarvis.log", "x" * 5000)
    taken = marks(data_dir)
    # Rotation replaces the file with a short new one, so the mark is now past its end.
    (log_dir(data_dir) / "jarvis.log").write_text("ERROR after the rotation\n", encoding="utf-8")
    assert errors_since(data_dir, taken).count == 1


def test_no_marks_means_nothing_found_rather_than_the_whole_file(data_dir: Path) -> None:
    write(data_dir, "jarvis.log", "ERROR months of history\n" * 50)
    assert not errors_since(data_dir, {})
    assert not errors_since(data_dir, None)


def test_a_missing_log_directory_is_not_an_error(tmp_path: Path) -> None:
    assert not errors_since(tmp_path / "nowhere", {"jarvis.log": 0})


def test_undecodable_bytes_do_not_stop_the_scan(data_dir: Path) -> None:
    taken = marks(data_dir)
    (log_dir(data_dir) / "jarvis.log").write_bytes(b"\xff\xfe ERROR still readable\n")
    assert errors_since(data_dir, taken).count == 1


# --- what the caller says --------------------------------------------------


def test_clean_logs_say_nothing_at_all() -> None:
    assert LogErrors().spoken() == ""
    assert LogErrors().written() == ""
    assert not LogErrors()


def test_one_error_is_spoken_in_the_singular() -> None:
    spoken = LogErrors(count=1, lines=("ValueError: nope",)).spoken()
    assert "1 error in the log" in spoken
    assert spoken.endswith("ValueError: nope")


def test_several_errors_lead_with_the_count_and_quote_the_last() -> None:
    spoken = LogErrors(count=4, lines=("first", "second", "third")).spoken()
    assert "4 errors in the log" in spoken
    assert spoken.endswith("third")


def test_written_errors_are_one_per_line() -> None:
    assert LogErrors(count=2, lines=("a", "b")).written() == "a\nb"
