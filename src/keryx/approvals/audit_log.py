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


def escalation_counts(entries: list[dict]) -> tuple[int, int]:
    """How many settled requests were answered before the call that would have asked about
    them, and how many were answered elsewhere after they were told — counted only over
    lines that record an `escalation` outcome at all (issue #56: a request they were
    rung about first is never counted as "no call").

    Returns `(cancelled_before_call, answered_after_call)`.
    """
    settled = [e for e in entries if e.get("event") == "settled" and e.get("escalation")]
    cancelled = sum(e["escalation"] == "cancelled" for e in settled)
    late = sum(
        e["escalation"] != "cancelled" and e.get("outcome") == "resolved_elsewhere"
        for e in settled
    )
    return cancelled, late


def entry_json(entry: dict) -> dict[str, Any]:
    """One audit line as `--json` and the table both read it: `{ts, event, request_id,
    what}`, where `what` is whichever of a request's summary, a denial's reason or a
    verdict's answer the line actually carries.
    """
    return {
        "ts": str(entry.get("ts", "")),
        "event": str(entry.get("event", "")),
        "request_id": entry.get("request_id"),
        "what": entry.get("summary") or entry.get("reason") or entry.get("answer") or "",
    }


def escalation_enabled(state_dir: Path) -> bool:
    """Whether the kill switch is off, given `Settings.state_dir`.

    The switch is a file (`approvals/DISABLED` under it), checked fresh every time, so
    this and `keryx approvals`'s own "OFF (kill switch)" line can never disagree.
    """
    return not (state_dir / STATE_DIR_NAME / KILL_SWITCH_NAME).exists()
