"""`keryx migrate` for the Mac app: the plan as one document, and the run as JSON lines.

The plan is `Plan.as_dict` with the service's part beside it — which unit is stopped first,
which of Jarvis's are retired, which of Keryx's is installed — and the live calls that
would hold the run back. The run is `config.migrate.run`, told phase by phase
(`config.migrate.PHASES`) as `step` events, then exactly one of `done`, `error` or
`cancelled`.

Cancel is honoured only before the first step. From the moment the service is stopped the
migration is carried through to its end — a SIGTERM or SIGINT then is held off (`held`) and
said afterwards, as a step, before `done` — because a migration left half way leaves the
service stopped and `keryx serve` refusing until it is run again. Each entry's move is whole
or not at all even under SIGKILL (`config.migrate.move`), so a run that is killed outright
still finishes on the next.
"""

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from keryx.config.migrate import OLD_UNITS, PHASES, Plan, Report
from keryx.localmodels.pull import CANCEL_SIGNALS, EventLines

#: Each phase of the run, said for a person.
PHASE_MESSAGES = {
    "stop": "Stopping Keryx's background service.",
    "move": "Moving Keryx's files.",
    "update": "Bringing the settings and the task history up to date.",
    "units": "Putting the background service in place again.",
    "start": "Starting Keryx's background service again.",
}
assert set(PHASE_MESSAGES) == set(PHASES)
#: The step told after a run that a Cancel arrived during: it was finished, not stopped.
LATE_CANCEL = "cancel"
LATE_CANCEL_MESSAGE = ("Cancel came while the files were moving, so the migration was "
                       "finished first rather than left half done.")
NOTHING_TO_DO = "Keryx already keeps everything where it should."
CONFLICTS = "Some of Keryx's files from before are in the way, so nothing was moved."
LIVE_CALL = "A call is going on, and migrating would cut it off: try again once the line is clear."
STOPPED = ("The migration stopped before it finished. Migrating again picks up where it "
           "stopped.")


@dataclass(frozen=True)
class Units:
    """The service a migration stops and starts: Keryx's (`target`), and Jarvis's unit when
    it is still the one installed (`predecessor`), stopped first and retired for Keryx's."""

    target: Any  # restart.service.ServiceTarget | None
    predecessor: Any = None

    @property
    def stopping(self) -> Any:
        return self.predecessor or self.target

    def as_dict(self) -> dict[str, Any]:
        stopping = self.stopping
        return {
            "stops": None if stopping is None else stopping.describe(),
            "retires": [] if self.predecessor is None
            else list(OLD_UNITS[self.predecessor.manager]),
            "installs": None if self.target is None else self.target.describe(),
        }


def plan_document(plan: Plan, units: Units, live_calls: int | None) -> dict[str, Any]:
    """What `keryx migrate --json` prints: the plan, the service's part, and whether it could
    run now — something to do, nothing in the way, and no call it would cut off."""
    document = plan.as_dict()
    blocked = bool(live_calls) and units.target is not None
    return {
        **document,
        "service": units.as_dict(),
        "live_calls": live_calls,
        "ready": not document["nothing_to_do"] and not plan.conflicts and not blocked,
    }


class MigrateSteps(EventLines):
    """For the Mac app: a `step` as each phase begins (`step`, `message`, `detail`), then
    exactly one of `done`, `error`, `cancelled`."""

    def step(self, step: str, *, detail: str | None = None) -> None:
        message = LATE_CANCEL_MESSAGE if step == LATE_CANCEL else PHASE_MESSAGES[step]
        self._emit({"event": "step", "step": step, "message": message, "detail": detail})

    def nothing_to_do(self) -> None:
        self._end({"event": "done", "summary": NOTHING_TO_DO, "nothing_to_do": True,
                   "moved": 0, "imported": 0, "rewritten": 0, "lost_sessions": 0,
                   "started": None, "retired_to": None, "leftovers": [], "notes": [],
                   "units_pending": False})

    def done(self, report: Report) -> None:
        self._end({
            "event": "done",
            "summary": summary(report),
            "nothing_to_do": False,
            "moved": len(report.moved),
            "imported": len(report.imported),
            "rewritten": report.rewritten,
            "lost_sessions": len(report.lost_sessions),
            "started": report.started,
            "retired_to": None if report.retired_to is None else str(report.retired_to),
            "leftovers": list(report.leftovers),
            "notes": [*report.notes, *report.tightened, *report.rerendered],
            "units_pending": report.units_pending,
        })


def summary(report: Report) -> str:
    """How a run ended, in a sentence for a person."""
    if report.units_pending:
        return ("Keryx's files were moved, but the background service could not be put in "
                "place again: migrating again finishes it.")
    if report.started is True:
        return "Keryx's files were moved, and its background service is running again."
    if report.started is False:
        return "Keryx's files were moved, but its background service did not start again."
    return "Keryx's files were moved."


@contextmanager
def held() -> Iterator[Callable[[], bool]]:
    """Cancel held off while this lasts; yields whether one has arrived. It lands on the way
    out, once the ending is told, where `EventLines.guard` reads it as after that ending."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)
    try:
        yield lambda: bool(signal.sigpending() & CANCEL_SIGNALS)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
