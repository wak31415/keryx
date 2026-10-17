"""`keryx approvals`: the approval bridge's audit trail and kill switch."""

import json
from typing import Annotated

import typer

from keryx.approvals.audit_log import (
    entry_json,
    escalation_counts,
    escalation_enabled,
    read_entries,
    settled_with_escalation,
)
from keryx.approvals.broker import AUDIT_NAME, KILL_SWITCH_NAME, STATE_DIR_NAME
from keryx.cli.apps import app
from keryx.cli.common import MAX_DESCRIPTION_CHARS, _configure_readonly, _shorten
from keryx.config import secure_dir, secure_file

APPROVALS_EMPTY = "nothing in the approvals log yet"
APPROVALS_HEADER = f"{"WHEN":<17} {"EVENT":<20} {"ID":>3}  WHAT"


@app.command()
def approvals(
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many of the most recent lines to show.")
    ] = 20,
    disable: Annotated[
        bool, typer.Option("--disable", help="Stop the bridge escalating anything, now.")
    ] = False,
    enable: Annotated[
        bool, typer.Option("--enable", help="Undo --disable.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """What the approval bridge has done, and the kill switch that stops it.

    The switch is a file, deliberately: it is checked afresh on every single request, so it
    takes effect on the next prompt with no restart and without editing any settings — and
    it still works when the thing you want to stop is the thing you would have to ask.
    """
    settings = _configure_readonly()
    state_dir = settings.state_dir / STATE_DIR_NAME
    switch = state_dir / KILL_SWITCH_NAME
    if disable and enable:
        typer.echo("pick one of --disable and --enable")
        raise typer.Exit(code=2)
    if as_json and (disable or enable):
        typer.echo("pick one of --json and --disable/--enable")
        raise typer.Exit(code=2)
    if disable:
        secure_dir(state_dir)
        switch.touch()
        secure_file(switch)
        typer.echo(f"approval escalation is off ({switch})")
        return
    if enable:
        switch.unlink(missing_ok=True)
        typer.echo("approval escalation is on")
        return

    entries = read_entries(state_dir / AUDIT_NAME)
    if as_json:
        cancelled, late = escalation_counts(entries)
        typer.echo(json.dumps({
            "escalation_enabled": escalation_enabled(settings.state_dir),
            "escalate_after_s": settings.approval_escalate_seconds,
            "max_per_hour": settings.approval_max_per_hour,
            "cancelled_before_call": cancelled,
            "answered_after_call": late,
            "entries": [entry_json(e) for e in entries[-max(limit, 1):]],
        }, indent=2))
        return

    typer.echo(
        f"escalation: {"OFF (kill switch)" if not escalation_enabled(settings.state_dir)
                       else "on"}, "
        f"after {settings.approval_escalate_seconds:g}s, "
        f"at most {settings.approval_max_per_hour}/hour"
    )
    summary = _escalation_summary(entries)
    if summary:
        typer.echo(summary)
    for line in _audit_lines(entries, limit):
        typer.echo(line)


def _escalation_summary(entries: list[dict]) -> str | None:
    """The table's summary line: whether settling a request stopped its call, over every
    line that records it. `None` while nothing has recorded one yet — not the same as
    `escalation_counts(entries) == (0, 0)`, which a settled-and-escalated request that
    counted toward neither bucket also produces (see `settled_with_escalation`).
    """
    if not settled_with_escalation(entries):
        return None
    cancelled, late = escalation_counts(entries)
    return (
        f"{cancelled} answered before the call was due (no call), "
        f"{late} answered elsewhere after they were told"
    )


def _audit_lines(entries: list[dict], limit: int) -> list[str]:
    """The tail of the audit log as table rows, or one line saying there is none."""
    rows = [APPROVALS_HEADER]
    for entry in entries[-max(limit, 1):]:
        row = entry_json(entry)
        when = row["ts"][:16].replace("T", " ")
        request_id = "" if row["request_id"] is None else str(row["request_id"])
        rows.append(
            f"{when:<17} {row["event"]:<20} "
            f"{request_id:>3}  "
            f"{_shorten(str(row["what"]), MAX_DESCRIPTION_CHARS)}"
        )
    return rows if len(rows) > 1 else [APPROVALS_EMPTY]
