"""Wrong PINs counted across every call, so that guessing one takes decades, not hours.

`VoiceSession` ends a call after three wrong PINs, but that count dies with the call, and a
call is cheap to make again. Caller ID is spoofable, so the allowlist stops nobody who means
it, and at three guesses a call a 6-digit PIN falls in about 167,000 calls — a day and a
half at twenty in parallel. This is the count that outlives the call, and the process: it
lives in `data_dir/pin-failures.json`, so a restart hands out no fresh budget.

The rule, with all three numbers in `Settings`:

- Every wrong PIN, on any call, is counted for `PIN_FAILURE_WINDOW_HOURS` (24).
- When the count reaches `PIN_FAILURE_LIMIT` (10), PIN entry locks for `PIN_LOCKOUT_MINUTES`
  (60). While it is locked every PIN is refused, the right one included, without being
  compared — so a guess made then learns nothing, and costs nothing to refuse.
- Neither the lock lifting nor a right PIN resets the count. Past the limit, each further
  wrong PIN inside the window locks it again: an attacker who keeps going gets one guess per
  cooldown, about 24 a day, where the per-call limit alone allowed thousands an hour.
- `Lockout.alert` says when to tell the owner: the first time it locks, and again only if it
  is still locking a whole window after he was last told. One message a day, not one an hour.

The price is that whoever can make those calls can also keep the owner's own PIN locked out;
SECURITY.md says why that trade is the right one and what he can do about it.

A missing file is a fresh start. An unreadable one is the one state in which "nobody has
guessed" and "someone has nearly used the budget up" look the same, so it counts as a lock
that began the moment it was found — one cooldown, written back so that a restart does not
extend it, and never longer. A file that cannot be *written* keeps counting in memory: a full
disk must not be a way to guess for free.
"""

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.config import secure_dir, secure_file

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.pin_guard")

#: Where the count lives, under `data_dir`.
STATE_NAME = "pin-failures.json"


@dataclass(frozen=True)
class Lockout:
    """What a wrong PIN that locked PIN entry did: until when, and whether to say so."""

    #: When PIN entry opens again, in seconds since the epoch.
    until: float
    #: Wrong PINs inside the window, this one included.
    failures: int
    #: True when the owner has not been told about this run of lockouts yet.
    alert: bool


@dataclass
class _State:
    failures: list[float] = field(default_factory=list)
    locked_until: float | None = None
    alerted_at: float | None = None


class PinGuard:
    """The count of wrong PINs across calls, and the lock it sets. `clock` is injectable."""

    def __init__(
        self,
        path: Path,
        *,
        limit: int,
        window_seconds: float,
        lockout_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._limit = limit
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._state, unreadable = self._load()
        loaded = asdict(self._state)
        self._settle(clock())
        if unreadable or asdict(self._state) != loaded:
            # Straight back to disk: a lock pulled back here, or one set because the file was
            # unreadable, must not be found afresh (and extended) by the next restart.
            self._save()

    @classmethod
    def for_settings(
        cls, settings: "Settings", *, clock: Callable[[], float] = time.time
    ) -> "PinGuard":
        return cls(
            settings.data_dir / STATE_NAME,
            limit=settings.pin_failure_limit,
            window_seconds=settings.pin_failure_window_hours * 3600,
            lockout_seconds=settings.pin_lockout_minutes * 60,
            clock=clock,
        )

    def locked_until(self) -> float | None:
        """When PIN entry opens again, or None when it is open now."""
        self._settle(self._clock())
        return self._state.locked_until

    def record_failure(self) -> Lockout | None:
        """Count one wrong PIN; the `Lockout` when this one locked PIN entry, else None."""
        now = self._clock()
        self._settle(now)
        state = self._state
        state.failures.append(now)
        lockout = None
        if len(state.failures) >= self._limit:
            state.locked_until = now + self._lockout
            alert = state.alerted_at is None or now - state.alerted_at >= self._window
            if alert:
                state.alerted_at = now
            lockout = Lockout(state.locked_until, len(state.failures), alert)
            log.warning(
                "PIN entry is locked on every call until %s: %d wrong PINs in %.0f hours",
                datetime.fromtimestamp(state.locked_until).isoformat(timespec="minutes"),
                len(state.failures),
                self._window / 3600,
            )
        self._save()
        return lockout

    # --- the state, in memory and on disk ---------------------------------------

    def _settle(self, now: float) -> None:
        """Forget what has aged out, and pull back anything a clock change left ahead of now.

        The pull-back is what keeps a lock bounded whatever the file says: a lock can never
        be further off than one cooldown from the moment it is looked at.
        """
        state = self._state
        state.failures = [min(stamp, now) for stamp in state.failures if stamp > now - self._window]
        if state.locked_until is not None:
            state.locked_until = (
                None if state.locked_until <= now else min(state.locked_until, now + self._lockout)
            )
        if state.alerted_at is not None:
            state.alerted_at = min(state.alerted_at, now)

    def _load(self) -> tuple[_State, bool]:
        """The state on disk (fresh when there is none), and whether it was unreadable."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return (
                _State(
                    failures=[_stamp(stamp) for stamp in _list(data["failures"])],
                    locked_until=_optional_stamp(data.get("locked_until")),
                    alerted_at=_optional_stamp(data.get("alerted_at")),
                ),
                False,
            )
        except FileNotFoundError:
            return _State(), False
        except (OSError, ValueError, TypeError, KeyError) as error:
            until = self._clock() + self._lockout
            log.error(
                "could not read the PIN failure count at %s (%s); PIN entry is locked for one "
                "cooldown, until %s, rather than guess how many wrong PINs it held",
                self._path,
                type(error).__name__,
                datetime.fromtimestamp(until).isoformat(timespec="minutes"),
            )
            return _State(locked_until=until), True

    def _save(self) -> None:
        """Write the state atomically, owner-only. A failure is logged, never raised."""
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            secure_dir(self._path.parent)
            tmp.write_text(json.dumps(asdict(self._state)), encoding="utf-8")
            secure_file(tmp)
            os.replace(tmp, self._path)
        except OSError:
            log.exception("could not write the PIN failure count at %s", self._path)
            with contextlib.suppress(OSError):
                tmp.unlink()


def _list(value: object) -> list:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return value


def _stamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("not a timestamp")
    return float(value)


def _optional_stamp(value: object) -> float | None:
    return None if value is None else _stamp(value)
