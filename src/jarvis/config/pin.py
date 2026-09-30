"""The PIN's own store: `JARVIS_HOME/pin`, written once and never replaced.

`JARVIS_PIN` in the process environment still wins over it (`Settings.pin`); without it,
this file is the PIN.

It sits in the configuration directory, beside `secrets.toml`, and not in `DATA_DIR`,
because where the PIN is must not depend on a setting. Were it `DATA_DIR/pin`, pointing
`DATA_DIR` at an empty directory would find no PIN — and no PIN is an open enrolment door.
`JARVIS_HOME` is an environment variable only, never a setting. The PIN is still not a
*setting* either: a file of its own, written once, and never a key in `config.toml`.

It is written in exactly two places — a first call keying one in
(`session._enrol_keypad_pin`), and the owner choosing one at the keyboard in `jarvis setup`
— and both go through `enrol`, so both go through the same `O_CREAT | O_EXCL`.
"""

import logging
import os
import re
from pathlib import Path

from jarvis.config.files import DATA_FILE_MODE, secure_dir, secure_file, write_private

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

#: Each digit as a caller may say it and a model or a transcriber may write it: the numeral,
#: or the word ("oh" and a bare "o" for zero too, which is how a phone number is read out).
DIGIT_WORDS = {
    "0": ("zero", "oh", "o", "nought"),
    "1": ("one",),
    "2": ("two",),
    "3": ("three",),
    "4": ("four",),
    "5": ("five",),
    "6": ("six",),
    "7": ("seven",),
    "8": ("eight",),
    "9": ("nine",),
}
_WORD_DIGITS = {word: digit for digit, words in DIGIT_WORDS.items() for word in words}
#: What may sit between two digits of a PIN said or typed: spaces, commas, stops and dashes
#: (the typographic ones too: a model tends to write a grouped number with an en dash).
_SEPARATOR = r"[\s,.\-\u2010-\u2015]"
BETWEEN_DIGITS = _SEPARATOR + "*"
_SEPARATORS = re.compile(_SEPARATOR + "+")


def spoken_digits(said: str | None) -> str | None:
    """The digits of a PIN as it was said, or None when `said` is not only digits.

    A PIN is said in groups — "four two four, two four two" — and whoever writes it down
    keeps the grouping: a dash, a space, a word per digit. None of that is the PIN being
    wrong, and comparing it as written refused the right PIN. So the separators go and the
    words become numerals; anything else at all (a stray word, a non-ASCII numeral) makes
    it no PIN, rather than a string to pick digits out of.
    """
    digits = []
    for token in _SEPARATORS.split((said or "").strip().lower()):
        if token.isascii() and token.isdigit():
            digits.append(token)
        elif token in _WORD_DIGITS:
            digits.append(_WORD_DIGITS[token])
        elif token:
            return None
    return "".join(digits) or None

#: Where a PIN came from, for `jarvis doctor` and `jarvis memory seed --json`. Never the
#: digits.
PIN_FROM_ENV = "environment"
PIN_FROM_FILE = "enrolled"
#: The enrolled PIN's file name in `JARVIS_HOME` (`read_enrolled_pin` / `write_enrolled_pin`).
PIN_FILE_NAME = "pin"


def pin_file(home: Path) -> Path:
    """Where the PIN lives, in the configuration directory `home`. `JARVIS_PIN` outranks it."""
    return home / PIN_FILE_NAME


def read_enrolled_pin(home: Path) -> str | None:
    """The PIN in `home/pin`, or None when there is none worth having.

    **The digits, not a hash, and that is deliberate.** Six digits fall to any hash in
    microseconds, so hashing would buy nothing and imply a protection that is not there.
    The protection is the file mode: 0600 inside a 0700 `JARVIS_HOME`, beside
    `secrets.toml`, which is no less private. Do not "improve" this into a hash.

    A file that is there and does not hold 6-8 digits is *not* the same as no file: it is
    no PIN, and it still seals the door, because `write_enrolled_pin` cannot replace it.
    `jarvis doctor` says so, and the way out is the owner's — delete it, or run
    `jarvis setup`.
    """
    try:
        value = pin_file(home).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if PIN_PATTERN.fullmatch(value) else None


def write_enrolled_pin(home: Path, digits: str) -> bool:
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
    path = pin_file(home)
    try:
        secure_dir(home)
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


def replace_pin_at_keyboard(home: Path, digits: str) -> None:
    """The owner, at a terminal, putting a new PIN in place of the one there. `jarvis setup`
    only, after two yeses — never a tool, never a command a subagent could run for itself.

    A rename rather than delete-then-create, so there is no moment with no PIN and an open
    door, and a write that fails leaves the old PIN exactly where it was. The running
    service keeps the PIN it read at startup until it restarts.
    """
    if not PIN_PATTERN.fullmatch(digits):
        raise ValueError(PIN_RULE)
    write_private(pin_file(home), f"{digits}\n")
    log.info("the PIN at %s was replaced at the keyboard", pin_file(home))


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
