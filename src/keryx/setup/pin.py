"""The PIN at the keyboard: what `keryx setup`'s PIN step and `keryx pin` share.

Two keyboard paths and one set of doors. `keryx setup` asks at a terminal; `keryx pin set`
takes the digits twice on stdin from the owner's Mac app. Both find the PIN the same way
(`existing_pin`), judge new digits by the same rule (`pin_problem`), and write through the
same two functions in `keryx.config.pin` (`write_pin`): a first PIN by `O_CREAT | O_EXCL`,
which shuts the enrolment door, and a replacement by an atomic rename, which never leaves a
moment with no PIN.

Neither path is reachable from a call. Both commands are owner-only (`cli._owner_only`), so
anything `keryx serve` started is refused before a digit is read, and nothing under
`keryx.tools` or `keryx.plugins` imports this module; a test says so.

The digits go nowhere but the file: not into a message, an exception or a log line, and not
their length either.
"""

import hmac
import os
from datetime import UTC, datetime
from typing import Any

from keryx.config import Settings
from keryx.config.files import legacy_home
from keryx.config.pin import (
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    PIN_PATTERN,
    PIN_RULE,
    is_trivial_pin,
    pin_file,
    replace_pin_at_keyboard,
    write_enrolled_pin,
)
from keryx.config.settings import env_var_names
from keryx.pin_guard import PinGuard

#: Where the PIN on this machine is (`existing_pin`).
ENVIRONMENT = "environment"
FILE = "file"
#: `KERYX_HOME/pin` is there and holds no usable PIN: no PIN, and still a shut door.
UNUSABLE = "unusable"
#: Only in an unmigrated `~/.jarvis`: the machine has a PIN that has not moved yet.
JARVIS = "jarvis"

#: What `keryx pin set` says, word for word (the Mac app shows them).
DIFFER = "The two PINs differ."
TWICE = "stdin must carry the new PIN twice, one per line, and nothing else."
ALREADY_SET = "A PIN is already set."
IN_ENVIRONMENT = (
    "A PIN is set in the environment ({name}), and it wins over any PIN set here: "
    "remove it from the environment first."
)
UNUSABLE_THERE = "{path} holds no usable PIN; --replace puts the new one in its place."
IN_JARVIS = (
    "The PIN is still in {path}, from when Keryx was Jarvis: run `keryx migrate` to move it "
    "before setting a new one."
)
NOT_WRITTEN = "The PIN could not be written to {path}; `keryx pin` says what is there now."
TERMINAL = "Type the PIN in the Keryx app or `keryx setup`; it is not echoed there."


def environment_pin_variable() -> str | None:
    """The variable setting a PIN in this process's environment, usable or not, or None.

    Read from the environment itself rather than `Settings.pin`: a malformed one leaves the
    field empty in the settings a report loads (`DOCTOR_FALLBACKS`), and yet `keryx serve`
    refuses to start on it, so a PIN written beside it is one nothing would ever read.
    Names match as the settings match them, in any case; the new name first.
    """
    present = {name.upper(): name for name, value in os.environ.items() if value.strip()}
    return next((present[name] for name in env_var_names("pin") if name in present), None)


def existing_pin(settings: Settings) -> str | None:
    """Where this machine's PIN is (`ENVIRONMENT`, `FILE`, `UNUSABLE`, `JARVIS`), or None.

    In the order `Settings.pin` and `pin_enrolment_open` read them: the environment wins,
    usable or not, then `KERYX_HOME/pin`, usable or not, then a PIN `keryx migrate` has yet
    to move.
    """
    if settings.pin_source == PIN_FROM_ENV or environment_pin_variable():
        return ENVIRONMENT
    if settings.pin_source == PIN_FROM_FILE:
        return FILE
    if pin_file(settings.config_dir).exists():
        return UNUSABLE
    if pin_file(legacy_home()).exists():
        return JARVIS
    return None


def pin_problem(digits: str) -> str | None:
    """Why `digits` will not do as a PIN chosen at the keyboard, or None when they will.

    `PIN_PATTERN` takes ASCII digits only: a PIN in another script's digits is one no keypad
    can enter, and an owner locked out.
    """
    if not PIN_PATTERN.fullmatch(digits):
        return f"It {PIN_RULE}."
    if is_trivial_pin(digits):
        return "Pick something less guessable than a run or a repeated digit."
    return None


def write_pin(settings: Settings, digits: str, *, replacing: bool) -> bool:
    """Write a PIN chosen at the keyboard. False when a first PIN found one already there.

    `replacing` swaps whatever `KERYX_HOME/pin` holds by an atomic rename, so a failure leaves
    the old one in place; otherwise this is the enrolment door, `O_CREAT | O_EXCL`. An
    `OSError` from the rename is the caller's to report.
    """
    if replacing:
        replace_pin_at_keyboard(settings.config_dir, digits)
        return True
    return write_enrolled_pin(settings.config_dir, digits)


def read_twice(text: str) -> tuple[str | None, str | None]:
    """The digits typed twice in `text`, and None; or None and why they will not do.

    Exactly two lines, each stripped of its line ending and nothing more. The two are
    compared in constant time *before* either is judged, so a refusal says nothing about
    the digits beyond that they differed.
    """
    lines = text.removesuffix("\n").split("\n") if text else []
    if len(lines) != 2:
        return None, TWICE
    first, second = (line.removesuffix("\r") for line in lines)
    if not first or not second:
        return None, TWICE
    if not hmac.compare_digest(first.encode(), second.encode()):
        return None, DIFFER
    if problem := pin_problem(first):
        return None, problem
    return first, None


def set_refusal(settings: Settings, *, replace: bool) -> str | None:
    """Why `keryx pin set` must leave the PIN alone on this machine, or None when it may.

    Asked before the digits are judged: these are facts about the machine, not the typing.
    """
    where = existing_pin(settings)
    if where == ENVIRONMENT:
        return IN_ENVIRONMENT.format(name=environment_pin_variable() or "KERYX_PIN")
    if where == JARVIS:
        return IN_JARVIS.format(path=legacy_home())
    if refusal := settings.storage_refusal():
        # A PIN written now would meet the old one in `keryx migrate`, which refuses two.
        return f"The PIN waits until the old files have moved: {refusal}"
    if where == FILE and not replace:
        return ALREADY_SET
    if where == UNUSABLE and not replace:
        return UNUSABLE_THERE.format(path=pin_file(settings.config_dir))
    return None


def pin_status(settings: Settings, guard: PinGuard | None = None) -> dict[str, Any]:
    """Whether a PIN is set and where, never what: no digit and no length.

    `locked` is the cross-call wrong-PIN lockout (`keryx.pin_guard`), read and never reset.
    """
    where = existing_pin(settings)
    until = (guard or PinGuard.for_settings(settings)).locked_until()
    return {
        # A malformed one in the environment is no PIN, and still the one that counts.
        "set": where in (FILE, JARVIS) or (where == ENVIRONMENT and bool(settings.pin)),
        "source": {ENVIRONMENT: "environment", FILE: "file", UNUSABLE: "file",
                   JARVIS: "jarvis"}.get(where) if where else None,
        "enrolment_open": settings.pin_enrolment_open and where is None,
        "locked": until is not None,
        "locked_until": (
            None if until is None
            else datetime.fromtimestamp(until, UTC).isoformat(timespec="seconds")
        ),
    }


#: How the text output says where a PIN is, by `pin_status`'s `source`.
SOURCES_SAID = {
    "environment": "set by KERYX_PIN in the environment",
    "file": "set, kept in KERYX_HOME/pin",
    "jarvis": "set, but still in ~/.jarvis until `keryx migrate` moves it",
}


def format_pin_status(found: dict[str, Any]) -> list[str]:
    """`pin_status` as a person reads it."""
    if found["set"]:
        lines = [f"PIN: {SOURCES_SAID[found['source']]}"]
    elif found["source"] == "environment":
        lines = ["PIN: the one in the environment is not 6 to 8 digits, and wins over any "
                 "other; Keryx will not start until it is fixed or removed"]
    elif found["source"] == "file":
        lines = ["PIN: none usable (the file there is not a PIN, and no call can set one)"]
    else:
        lines = ["PIN: not set (the first call may set one)"]
    if found["locked"]:
        lines.append(f"PIN entry is locked after too many wrong PINs, until "
                     f"{found['locked_until']}")
    return lines
