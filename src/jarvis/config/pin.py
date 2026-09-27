"""The PIN's own store: `data_dir/pin`, written once and never replaced.

`JARVIS_PIN` in the process environment still wins over it (`Settings.pin`), and so does a
legacy `.env`; with neither, this file is the PIN. It is written in exactly two places —
a first call keying one in (`session._enrol_keypad_pin`), and the owner choosing one at
the keyboard in `jarvis setup` — and both go through `enrol`, so both go through the same
`O_CREAT | O_EXCL`.
"""

import logging
import os
import re
from pathlib import Path

from jarvis.config.files import DATA_FILE_MODE, secure_dir, secure_file

log = logging.getLogger("jarvis.config")

#: What a configured PIN has to be. Digits, because it is keyed into a phone: a PIN with
#: a letter in it cannot be entered at all today, so accepting one only ever produced a
#: caller who could not authorize. Six of them at minimum because this is the single thing
#: between someone who has spoofed a caller ID and a subagent running with
#: `bypassPermissions`, and eight at most because it is said or keyed under time pressure.
PIN_PATTERN = re.compile(r"\d{6,8}")
PIN_RULE = "must be 6 to 8 digits, and nothing but digits"
#: The longest `PIN_PATTERN` allows. A keypad entry with no configured PIN to measure
#: itself against runs to here, since no further digit could be part of one.
PIN_MAX_DIGITS = 8

#: Where a PIN came from, for `jarvis doctor` and `jarvis memory seed --json`. Never the
#: digits.
PIN_FROM_ENV = "environment"
PIN_FROM_FILE = "enrolled"
#: The enrolled PIN's file name under `data_dir` (`read_enrolled_pin` / `write_enrolled_pin`).
PIN_FILE_NAME = "pin"


def pin_file(data_dir: Path) -> Path:
    """Where the PIN lives. `JARVIS_PIN` outranks it (`Settings.pin`)."""
    return data_dir / PIN_FILE_NAME


def read_enrolled_pin(data_dir: Path) -> str | None:
    """The PIN in `data_dir/pin`, or None when there is none worth having.

    **The digits, not a hash, and that is deliberate.** Six digits fall to any hash in
    microseconds, so hashing would buy nothing and imply a protection that is not there.
    The protection is the file mode: 0600 inside a 0700 `data_dir`, beside `memory.md` and
    every call transcript, which are no less private. Do not "improve" this into a hash.

    A file that is there and does not hold 6-8 digits is *not* the same as no file: it is
    no PIN, and it still seals the door, because `write_enrolled_pin` cannot replace it.
    `jarvis doctor` says so, and the way out is the owner's — delete it, or run
    `jarvis setup`.
    """
    try:
        value = pin_file(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if PIN_PATTERN.fullmatch(value) else None


def write_enrolled_pin(data_dir: Path, digits: str) -> bool:
    """Write the first PIN this machine has had. True when it was written, False when
    one was already there.

    **This is the one-way door, and the kernel is what holds it shut.** `O_CREAT | O_EXCL`
    makes the write fail on an existing file in the syscall, so no path in this code —
    no voice tool, no CLI command, no retry, no later refactor — can replace a PIN that
    has been enrolled. A policy check would not be that guarantee. There is deliberately
    no setter anywhere: changing a PIN means the owner removing this file at the keyboard
    (`jarvis setup` asks them to, in so many words) and enrolling afresh.

    A write that fails part way leaves a file that is not a usable PIN, and it is left
    exactly there rather than cleaned up: "delete the enrolled PIN" is the one operation
    this module must not know how to do.
    """
    if not PIN_PATTERN.fullmatch(digits):
        raise ValueError(PIN_RULE)
    path = pin_file(data_dir)
    try:
        secure_dir(data_dir)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, DATA_FILE_MODE)
    except FileExistsError:
        log.warning("a PIN is already enrolled at %s, and an enrolled PIN is never replaced", path)
        return False
    except OSError:
        log.exception("could not enrol a PIN at %s", path)
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(f"{digits}\n")
    except OSError:
        log.exception("could not write the enrolled PIN at %s", path)
        return False
    finally:
        # The mode above is masked by the umask; this is the belt to that pair of braces.
        secure_file(path)
    log.info("a PIN was enrolled at %s", path)
    return True


def is_trivial_pin(digits: str) -> bool:
    """One digit repeated, or a straight run up or down (123456, 987654).

    A PIN only in the sense that it is digits. Refused by `jarvis setup`, which is choosing
    one on purpose; a first call is not second-guessed, because the person keying it in is
    standing in a hallway and would only key the next worst.
    """
    if len(set(digits)) == 1:
        return True
    steps = {int(b) - int(a) for a, b in zip(digits, digits[1:], strict=False)}
    return steps in ({1}, {-1})
