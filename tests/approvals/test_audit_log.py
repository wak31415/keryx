"""Tests for keryx.approvals.audit_log: the audit trail as data, for the table and `--json`
alike.

`keryx.cli.approvals` keeps only the table formatting; everything here is what it formats.
"""

import json

from keryx.approvals.audit_log import (
    entry_json,
    escalation_counts,
    escalation_enabled,
    read_entries,
    settled_with_escalation,
)
from keryx.approvals.broker import KILL_SWITCH_NAME, STATE_DIR_NAME


def _write(path, *lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# --- read_entries ------------------------------------------------------------


def test_read_entries_reads_every_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(
        path,
        json.dumps({"event": "raised", "request_id": 1}),
        json.dumps({"event": "settled", "request_id": 1}),
    )

    entries = read_entries(path)

    assert [e["event"] for e in entries] == ["raised", "settled"]


def test_read_entries_skips_a_corrupt_line_rather_than_failing(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(path, "not json", json.dumps({"event": "raised"}))

    entries = read_entries(path)

    assert [e["event"] for e in entries] == ["raised"]


def test_read_entries_skips_a_json_line_that_is_not_an_object(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(path, json.dumps([1, 2, 3]), json.dumps({"event": "raised"}))

    entries = read_entries(path)

    assert [e["event"] for e in entries] == ["raised"]


def test_read_entries_missing_file_is_empty_not_fatal(tmp_path):
    assert read_entries(tmp_path / "no-such-file.jsonl") == []


# --- settled_with_escalation ------------------------------------------------


def test_settled_with_escalation_excludes_a_line_with_no_escalation_field():
    assert settled_with_escalation([{"event": "settled", "request_id": 7}]) == []


def test_settled_with_escalation_excludes_non_settled_events():
    assert settled_with_escalation([{"event": "raised", "escalation": "cancelled"}]) == []


def test_settled_with_escalation_includes_a_line_whose_counts_are_both_zero():
    """A settled request can record an `escalation` outcome (it did ring, or it could
    have) and still count toward neither `escalation_counts` bucket — e.g. it was
    escalated *and* answered on the call itself, so it was neither cancelled before the
    call nor resolved elsewhere after. That line must still show up here: it is the one
    thing that tells `_escalation_summary` there is something to report at all, which
    `escalation_counts(...) == (0, 0)` cannot distinguish from no data recorded yet.
    """
    entries = [{"event": "settled", "escalation": "call", "outcome": "answered"}]

    assert settled_with_escalation(entries) == entries
    assert escalation_counts(entries) == (0, 0)


# --- escalation_counts ---------------------------------------------------


def test_escalation_counts_matches_the_summary_line():
    """Same fixtures as the CLI's `test_approvals_counts_the_calls_an_earlier_answer_stopped`:
    the counts here are exactly the two numbers that test reads out of the summary line.
    """
    entries = [
        {"event": "settled", "outcome": "answered", "escalation": "call"},
        {"event": "settled", "outcome": "resolved_elsewhere", "escalation": "cancelled"},
        {"event": "settled", "outcome": "resolved_elsewhere", "escalation": "cancelled"},
        {"event": "settled", "outcome": "resolved_elsewhere", "escalation": "call"},
        {"event": "settled", "outcome": "resolved_elsewhere"},  # written before the field was
    ]

    assert escalation_counts(entries) == (2, 1)


def test_escalation_counts_is_zero_over_nothing():
    assert escalation_counts([]) == (0, 0)


def test_escalation_counts_ignores_settled_lines_with_no_escalation_field():
    entries = [{"event": "settled", "request_id": 7}]

    assert escalation_counts(entries) == (0, 0)


def test_escalation_counts_ignores_events_that_are_not_settled():
    entries = [{"event": "raised", "escalation": "cancelled"}]

    assert escalation_counts(entries) == (0, 0)


# --- entry_json ------------------------------------------------------------


def test_entry_json_shape():
    entry = {
        "ts": "2026-09-02T14:05:00Z",
        "event": "raised",
        "request_id": 7,
        "summary": "Claude is asking to run pytest",
    }

    assert entry_json(entry) == {
        "ts": "2026-09-02T14:05:00Z",
        "event": "raised",
        "request_id": 7,
        "what": "Claude is asking to run pytest",
    }


def test_entry_json_falls_back_from_summary_to_reason_to_answer():
    assert entry_json({"reason": "denylisted"})["what"] == "denylisted"
    assert entry_json({"answer": "approve"})["what"] == "approve"
    assert entry_json({"summary": "s", "reason": "r", "answer": "a"})["what"] == "s"
    assert entry_json({})["what"] == ""


def test_entry_json_request_id_can_be_missing_or_a_string():
    assert entry_json({})["request_id"] is None
    assert entry_json({"request_id": "abc"})["request_id"] == "abc"


def test_entry_json_defaults_ts_and_event_to_empty_strings():
    assert entry_json({})["ts"] == ""
    assert entry_json({})["event"] == ""


def test_entry_json_reads_an_odd_line_as_strings_and_keeps_the_request_id():
    """The Mac app decodes `ts`, `event` and `what` as strings: a hand-edited or older line
    with a number, a null or an object in them must not break the whole list."""
    odd = {"ts": 1759654800, "event": None, "request_id": [7], "summary": {"tool": "Bash"}}

    assert entry_json(odd) == {
        "ts": "1759654800", "event": "", "request_id": [7], "what": "{'tool': 'Bash'}",
    }
    assert entry_json({"reason": 403})["what"] == "403"
    assert entry_json({"ts": None, "answer": True})["ts"] == ""
    assert entry_json({"ts": None, "answer": True})["what"] == "True"


# --- escalation_enabled ------------------------------------------------------


def test_escalation_enabled_true_with_no_kill_switch(tmp_path):
    assert escalation_enabled(tmp_path) is True


def test_escalation_enabled_false_once_the_kill_switch_file_exists(tmp_path):
    switch_dir = tmp_path / STATE_DIR_NAME
    switch_dir.mkdir(parents=True)
    (switch_dir / KILL_SWITCH_NAME).touch()

    assert escalation_enabled(tmp_path) is False
