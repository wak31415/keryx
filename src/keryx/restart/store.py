"""The record that survives the restart.

Split out of `restart.py` (2026-09-02). The process that runs `systemctl restart` is the
process the service manager kills, so the two halves of a restart cannot hand anything to
each other in memory. `state_dir/restart.json` is the whole handover: what was asked for, by
whom, on which number, what was running, and how the watchdog was armed.

Every method here swallows its I/O errors. This is a breadcrumb, and losing a breadcrumb
must never be the thing that takes the service down.
"""

import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from keryx.config.files import private_lock, write_private

log = logging.getLogger("keryx.restart")

#: Where the two halves of a restart meet, under `state_dir`.
RECORD_NAME = "restart.json"
#: A restart the owner overtook by stopping Keryx (`keryx service stop`): no longer pending,
#: so the watchdog rings nobody about a service that was meant to stay down. With no restart
#: in flight the stop leaves a record in this state all the same, as a marker that refuses
#: a new restart until Keryx is started again (`clear_stopped`).
STOPPED = "stopped"
STOPPED_BY_OWNER = "Keryx was stopped by its owner before it came back"
STOPPED_MARKER = "Keryx was stopped by its owner"
#: A restart refused because Keryx is stopped: there is nothing running to restart.
REFUSED = "refused"
#: The lock that orders every write to a record across processes, beside it.
LOCK_SUFFIX = ".lock"
#: How long a write waits for the lock. Its holders keep it for the milliseconds a read
#: and a write take, so a holder still there after this has hung: the write is not made
#: (`RecordLocked`), never made unlocked.
LOCK_TIMEOUT_S = 5.0

#: What `settle_stopped` found: a restart settled, nothing in flight (a marker written), or
#: the record locked past `LOCK_TIMEOUT_S`, so nothing could be settled.
SETTLED, MARKED, LOCKED_OUT = "settled", "marked", "locked"  # `units.SETTLE_LOCKED`


# --- the record that survives the restart -----------------------------------


@dataclass
class RestartRecord:
    """What the process that asked for the restart left for the one that comes back."""

    requested_at: str = ""
    reason: str = ""
    number: str | None = None
    origin_channel: str = "local"
    origin_session_id: str | None = None
    target: str = ""
    #: What the process being restarted was *running* — its startup stamp, not the checkout
    #: as it stands now. The distinction is the whole point: see `mark_running`.
    version: str | None = None
    #: "pending" until delivered, then the file is gone or "failed" — or `STOPPED` (the owner
    #: stopped Keryx first) or `REFUSED` (Keryx was stopped when it was asked for).
    state: str = "pending"
    attempts: int = 0
    error: str | None = None
    #: The task whose work this restart is loading, when it is loading one. A restart that
    #: exists to pick up a change Keryx made to its own code is the only kind where "did
    #: it work" is a question about the *change* and not just about the process, so the
    #: confirmation names it and the version check below is only worth making with one.
    task_id: int | None = None
    #: How long each service log file was when the restart was asked for, so the process
    #: that comes back can tell this restart's errors from every earlier one (`logscan`).
    log_marks: dict[str, int] = field(default_factory=dict)
    #: How the out-of-process watchdog was started, or why it was not — the only thing that
    #: notices a service that never came back at all. See `keryx.restart.watchdog`.
    watchdog: str = ""
    #: `keryx restart --no-callback`: nobody is waiting to be told, so coming back up is
    #: the whole of it — confirmed in the log and cleared, never a call, a text or a
    #: `failed` record. Not the same as having no `number`: a restart asked for on the
    #: local channel has none either, and is still announced.
    quiet: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "RestartRecord":
        """Build from JSON, ignoring anything an older or newer version wrote."""
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the restart was asked for, or None if the stamp is unreadable."""
        try:
            asked = datetime.fromisoformat(self.requested_at)
        except ValueError:
            return None
        moment = now or datetime.now(UTC)
        return max((moment - asked).total_seconds(), 0.0)


class RecordLocked(Exception):
    """The record's lock could not be had within `LOCK_TIMEOUT_S`: nothing was written."""


class RestartStore:
    """The restart record on disk. Every write is made under the record's cross-process
    lock, and those that depend on what the record says now (still pending? stopped?) are a
    compare and a swap under it. I/O errors are swallowed — this is a breadcrumb, and losing
    it must never be what takes the service down — but a lock that cannot be had raises
    `RecordLocked`: the caller must know its write was not made."""

    def __init__(self, path: Path, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        self._path = path
        self._lock_timeout = lock_timeout

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RestartRecord | None:
        """The record, or None when there is none (or it is unreadable/corrupt). Reads take
        no lock: every write replaces the file whole."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            log.warning("could not read the restart record at %s; ignoring it", self._path)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return RestartRecord.from_dict(data)
        except TypeError:
            log.warning("the restart record at %s has an unexpected shape", self._path)
            return None

    @contextlib.contextmanager
    def locked(self) -> Iterator[bool]:
        """Hold the record's cross-process lock for the block. A hung holder raises
        `RecordLocked` and runs nothing; a lock file that cannot be made (an I/O error,
        swallowed like the rest) gives the block False, and it writes nothing."""
        lock = self._path.with_name(self._path.name + LOCK_SUFFIX)
        with contextlib.ExitStack() as stack:
            try:
                held = stack.enter_context(private_lock(lock, self._lock_timeout))
            except OSError:
                log.exception("could not lock the restart record at %s", self._path)
                yield False
                return
            if not held:
                raise RecordLocked(f"{lock} was held for {self._lock_timeout:g}s")
            yield True

    def save(self, record: RestartRecord) -> bool:
        """Write the record, whatever is there: atomically, 0600 (it holds a phone
        number), under the lock. False when it could not be written."""
        with self.locked() as held:
            if not held:
                return False
            return self._write(record)

    def create(self, record: RestartRecord) -> bool:
        """Write a new restart's record, unless Keryx is stopped (`STOPPED`, the owner's
        stop): a restart asked for then must not bring back what they stopped. False when
        refused, or not written."""
        with self.locked() as held:
            if not held:
                return False
            current = self.load()
            if current is not None and current.state == STOPPED:
                return False
            return self._write(record)

    def update_if_pending(self, requested_at: str,
                          change: Callable[[RestartRecord], object]) -> bool:
        """Apply `change` to the record and save it, only while it is still the pending
        record asked for at `requested_at` — compared and written under the lock, so a stop
        that settled it meanwhile is never written back over. False when it is not."""
        with self.locked() as held:
            if not held:
                return False
            current = self.load()
            if not self.is_pending(requested_at, current):
                return False
            change(current)
            return self._write(current)

    def clear_if_pending(self, requested_at: str) -> bool:
        """Remove the record once its restart is done with, only while it is still that
        pending one: a stop that settled it keeps its record. False when it is not."""
        with self.locked() as held:
            if not held:
                return False
            if not self.is_pending(requested_at):
                return False
            return self._remove()

    def record_failure(self, error: str) -> bool:
        """Mark the record failed with `error` (making one when there is none), unless the
        owner stopped Keryx meanwhile: their stop stands. False when it did not."""
        with self.locked() as held:
            if not held:
                return False
            record = self.load() or RestartRecord()
            if record.state == STOPPED:
                return False
            record.state, record.error = "failed", error
            return self._write(record)

    def is_pending(self, requested_at: str, record: RestartRecord | None = None) -> bool:
        """Whether the record (read now, unless handed in) is the restart asked for at
        `requested_at`, and still pending."""
        record = self.load() if record is None else record
        return (record is not None and record.state == "pending"
                and record.requested_at == requested_at)

    def settle_stopped(self) -> str:
        """Before the owner's stop: a pending restart marked as overtaken by it — the
        watchdog acts only on `pending`, and would otherwise ring them that Keryx never came
        back — or, with none in flight, the `STOPPED` marker that refuses a new one until
        Keryx starts again. A record that says anything else is left as it is. `SETTLED`,
        `MARKED`, or `LOCKED_OUT` when the lock could not be had and nothing was written."""
        try:
            with self.locked() as held:
                if not held:
                    return LOCKED_OUT
                record = self.load()
                if record is None:
                    self._write(RestartRecord(requested_at=datetime.now(UTC).isoformat(),
                                              origin_channel="owner", state=STOPPED,
                                              error=STOPPED_MARKER))
                    return MARKED
                if record.state == "pending":
                    record.state, record.error = STOPPED, STOPPED_BY_OWNER
                    self._write(record)
                    return SETTLED
                return MARKED if record.state == STOPPED else SETTLED
        except RecordLocked:
            log.error("could not settle the restart record before the stop: it is locked")
            return LOCKED_OUT

    def clear_stopped(self) -> bool:
        """Keryx is starting again: the stop's record (or a restart refused while it was
        stopped) goes, so restarts are asked for again. Any other record is kept."""
        with self.locked() as held:
            if not held:
                return False
            record = self.load()
            if record is None or record.state not in (STOPPED, REFUSED):
                return False
            return self._remove()

    def clear(self) -> None:
        """Remove the record, under the lock; a missing one is already cleared."""
        with self.locked() as held:
            if held:
                self._remove()

    def _write(self, record: RestartRecord) -> bool:
        try:
            write_private(self._path, json.dumps(dataclasses.asdict(record), indent=2))
            return True
        except OSError:
            log.exception("could not write the restart record at %s", self._path)
            return False

    def _remove(self) -> bool:
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)
            return True
        return False


# --- summary helpers --------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    """A spoken-length duration: seconds under two minutes, else whole minutes."""
    if seconds is None:
        return "an unknown time"
    if seconds < 90:
        count = max(int(round(seconds)), 1)
        return f"{count} second{'s' if count != 1 else ''}"
    minutes = int(round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
