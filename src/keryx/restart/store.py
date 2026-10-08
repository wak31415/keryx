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
#: so the watchdog rings nobody about a service that was meant to stay down.
STOPPED = "stopped"
STOPPED_BY_OWNER = "Keryx was stopped by its owner before it came back"
#: A restart refused because Keryx is stopped: there is nothing running to restart.
REFUSED = "refused"
#: The lock that orders every change to a record across processes, beside it.
LOCK_SUFFIX = ".lock"
#: How long a change waits for the lock. Long enough for a restart command to hand over
#: (the lock is held across it: the systemd unit's own `TimeoutStopSec` and a start), and
#: then the change goes ahead unlocked rather than never.
LOCK_TIMEOUT_S = 90.0


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


class RestartStore:
    """The restart record on disk. Every method swallows I/O errors: this is a breadcrumb,
    and losing it must never be what takes the service down."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RestartRecord | None:
        """The record, or None when there is none (or it is unreadable/corrupt)."""
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

    def save(self, record: RestartRecord) -> bool:
        """Write the record atomically, 0600 (it holds a phone number). False on failure."""
        try:
            write_private(self._path, json.dumps(dataclasses.asdict(record), indent=2))
            return True
        except OSError:
            log.exception("could not write the restart record at %s", self._path)
            return False

    @contextlib.contextmanager
    def locked(self, timeout: float = LOCK_TIMEOUT_S) -> Iterator[bool]:
        """Hold the record's cross-process lock for the block: every change that depends on
        what the record says now — still pending? — is made inside one. True when held; a
        lock that cannot be had (a hung holder, a file that cannot be made) still runs the
        block, given False: this orders writers, and must never be what stops one."""
        with contextlib.ExitStack() as stack:
            try:
                lock = self._path.with_name(self._path.name + LOCK_SUFFIX)
                held = stack.enter_context(private_lock(lock, timeout))
            except OSError:
                log.warning("could not lock the restart record at %s; going on unlocked",
                            self._path)
                held = False
            yield held

    def update_if_pending(self, requested_at: str,
                          change: Callable[[RestartRecord], object]) -> bool:
        """Apply `change` to the record and save it, only while it is still the pending
        record asked for at `requested_at` — compared and written under the lock, so a stop
        that settled it meanwhile is never written back over. False when it is not."""
        with self.locked():
            current = self.load()
            if not self.is_pending(requested_at, current):
                return False
            change(current)
            return self.save(current)

    def is_pending(self, requested_at: str, record: RestartRecord | None = None) -> bool:
        """Whether the record (read now, unless handed in) is the restart asked for at
        `requested_at`, and still pending. Ask it under `locked()` to act on the answer."""
        record = self.load() if record is None else record
        return (record is not None and record.state == "pending"
                and record.requested_at == requested_at)

    def settle_stopped(self) -> bool:
        """A pending restart marked as overtaken by the owner's stop, before the stop: the
        watchdog acts only on `pending`, and would otherwise ring them that Keryx never came
        back. Under the lock, so a restart that is handing over finishes first and is then
        stopped with the rest. True when there was one to settle."""
        with self.locked():
            record = self.load()
            if record is None or record.state != "pending":
                return False
            record.state, record.error = STOPPED, STOPPED_BY_OWNER
            return self.save(record)

    def clear(self) -> None:
        """Remove the record; a missing one is already cleared."""
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)


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
