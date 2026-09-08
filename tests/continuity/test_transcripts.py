"""Tests for reading an earlier call back out of its transcript."""

from jarvis.continuity.transcripts import MAX_TRANSCRIPT_CHARS, read_tail, transcript_path


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
