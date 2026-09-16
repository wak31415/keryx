"""Tests for reading an earlier call back out of its transcript, and keeping a PIN out."""

import pytest

from jarvis.continuity.transcripts import (
    MAX_TRANSCRIPT_CHARS,
    PIN_REDACTED,
    read_tail,
    redact_pin,
    transcript_path,
)

PIN = "123456"


def write_transcript(data_dir, session_id: str, body: str):
    path = transcript_path(data_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_only_what_was_said_comes_back(tmp_path):
    write_transcript(
        tmp_path,
        "abc123",
        "[17:59:34] --- session abc123 channel=phone caller=+15550000000\n"
        "[17:59:36] assistant: Hi, this is Jarvis.\n"
        "[17:59:42] user: Start the build.\n"
        "[17:59:49] --- session ended (hangup)\n",
    )

    assert read_tail(tmp_path, "abc123") == (
        "assistant: Hi, this is Jarvis.\nuser: Start the build."
    )


def test_the_caller_number_in_the_marker_line_is_not_carried(tmp_path):
    """The marker lines are bookkeeping, and one of them holds a phone number."""
    write_transcript(
        tmp_path, "abc123", "[17:59:34] --- session abc123 channel=phone caller=+15550000000\n"
    )

    assert read_tail(tmp_path, "abc123") == ""


def test_a_long_call_keeps_its_end(tmp_path):
    lines = [f"[17:59:{index:02d}] user: line {index}" for index in range(400)]
    write_transcript(tmp_path, "abc123", "\n".join(lines))

    tail = read_tail(tmp_path, "abc123")

    assert len(tail) <= MAX_TRANSCRIPT_CHARS
    assert "line 399" in tail
    assert "line 0\n" not in tail
    assert not tail.startswith("ne ")  # never opens mid-word


def test_a_missing_transcript_is_not_an_error(tmp_path):
    assert read_tail(tmp_path, "nothing-here") == ""
    assert read_tail(tmp_path, "") == ""


def test_a_call_back_never_carries_a_pin_an_older_transcript_still_holds(tmp_path):
    write_transcript(
        tmp_path,
        "abc123",
        "[17:59:36] assistant: What's your PIN?\n[17:59:42] user: 1 2 3 4 5 6.\n",
    )

    assert read_tail(tmp_path, "abc123", pin=PIN) == (
        f"assistant: What's your PIN?\nuser: {PIN_REDACTED}."
    )


# --- a spoken PIN is never written down ------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "123456",
        "1 2 3 4 5 6",
        "12 34 56",
        "123-456",
        "1, 2, 3, 4, 5, 6",
        "1. 2. 3. 4. 5. 6",
        "one two three four five six",
        "One, two, three, four, five, six",
        "one 2 three 4 five 6",
    ],
)
def test_every_way_a_pin_is_said_or_typed_is_redacted(said):
    assert redact_pin(f"user: it's {said}, thanks.", PIN) == f"user: it's {PIN_REDACTED}, thanks."


@pytest.mark.parametrize("zero", ["0", "zero", "oh", "o", "nought", "Oh"])
def test_zero_is_redacted_however_it_is_said(zero):
    said = f"one {zero} two {zero} three {zero}"

    assert redact_pin(f"user: {said}", "102030") == f"user: {PIN_REDACTED}"


@pytest.mark.parametrize(
    "text",
    [
        "user: 1 2 3 4 5 7",  # one digit off is not the PIN
        "user: 1 2 3 4",  # nor is half of it
        "user: call me on +15550001111",
        "assistant: task 12 is done, one of three",
        "user: someone won two thirds",
    ],
)
def test_what_is_not_the_pin_is_left_alone(text):
    assert redact_pin(text, PIN) == text


def test_the_pin_inside_a_longer_run_of_digits_is_still_redacted():
    """The safe direction: a stray digit either side must not smuggle the rest out."""
    assert redact_pin("user: 9 1 2 3 4 5 6 9", PIN) == f"user: 9 {PIN_REDACTED} 9"


@pytest.mark.parametrize("pin", [None, "", "12ab56"])
def test_without_a_usable_pin_nothing_is_redacted(pin):
    assert redact_pin("user: 1 2 3 4 5 6", pin) == "user: 1 2 3 4 5 6"
