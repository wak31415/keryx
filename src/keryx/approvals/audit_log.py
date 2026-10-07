"""The approval bridge's audit trail as data: what `keryx approvals` prints as a table, and
what `--json` hands the Mac app.

`cli.py` keeps only the formatting (the table, the summary line's wording); every read of
`audit.jsonl` and the kill switch lives here, so the next reader of either — the status
command Task 8 adds — reaches the same file through the same two functions instead of a
second, possibly-drifted copy of this logic.
"""

import json
from pathlib import Path
from typing import Any

from keryx.approvals.broker import KILL_SWITCH_NAME, STATE_DIR_NAME


def read_entries(path: Path) -> list[dict]:
    """Every readable line of the audit log; a corrupt one is skipped, never fatal."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in raw:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def settled_with_escalation(entries: list[dict]) -> list[dict]:
    """Every `settled` line that records an `escalation` outcome at all (issue #56: a
    request they were rung about first is never counted as "no call") — a line settled
    before that field existed is excluded, which is "no data", not "zero escalations".

    Shared by `escalation_counts` and `cli._escalation_summary`'s gate, so "is there
    anything to summarize" and "what do the numbers say" can never drift apart: a settled
    line that counts toward neither bucket (escalated *and* answered on the call) still
    means there is something to report, which `escalation_counts(entries) == (0, 0)` alone
    could not tell apart from nothing having recorded one at all.
    """
    return [e for e in entries if e.get("event") == "settled" and e.get("escalation")]


def escalation_counts(entries: list[dict]) -> tuple[int, int]:
    """How many settled requests were answered before the call that would have asked about
    them, and how many were answered elsewhere after they were told — counted only over
    `settled_with_escalation(entries)`.

    Returns `(cancelled_before_call, answered_after_call)`.
    """
    settled = settled_with_escalation(entries)
    cancelled = sum(e["escalation"] == "cancelled" for e in settled)
    late = sum(
        e["escalation"] != "cancelled" and e.get("outcome") == "resolved_elsewhere"
        for e in settled
    )
    return cancelled, late


def _text(value: object) -> str:
    """A field the Mac app decodes as a string, whatever the line held: absent or null is
    empty, anything else its `str()`."""
    return "" if value is None else str(value)


def entry_json(entry: dict) -> dict[str, Any]:
    """One audit line as `--json` and the table both read it: `{ts, event, request_id,
    what}`, where `what` is whichever of a request's summary, a denial's reason or a
    verdict's answer the line actually carries. `ts`, `event` and `what` are always strings,
    so one odd line cannot break the app's decoding of the list; `request_id` is as written.
    """
    return {
        "ts": _text(entry.get("ts")),
        "event": _text(entry.get("event")),
        "request_id": entry.get("request_id"),
        "what": _text(entry.get("summary") or entry.get("reason") or entry.get("answer")),
    }


def escalation_enabled(state_dir: Path) -> bool:
    """Whether the kill switch is off, given `Settings.state_dir`.

    The switch is a file (`approvals/DISABLED` under it), checked fresh every time, so
    this and `keryx approvals`'s own "OFF (kill switch)" line can never disagree.
    """
    return not (state_dir / STATE_DIR_NAME / KILL_SWITCH_NAME).exists()
