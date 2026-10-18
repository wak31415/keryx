"""Reading back what was said in an earlier call.

A call-back is a new realtime session: the provider keeps no history across sockets, so
the only record of the conversation that asked for the call is the transcript the session
wrote to `data_dir/calls/<session_id>.log`. This reads the end of one back, so the
call-back can open knowing how the last call went instead of only what the task returned.

The tail, not the whole thing: it goes into a system prompt that is spoken from, and the
last few exchanges are where the callback-worthy part of a call always is.

**A spoken PIN is never written down.** Saying the PIN aloud is supported, and the
transcription faithfully writes it into the call log — from where `recall`, the memory
subagent and every call-back's context would read it back to a model. `redact_pin` is
applied to every line `VoiceSession` writes, and again by everything that reads a
transcript back to a model, because logs written before it still hold the digits.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

from keryx.config.pin import BETWEEN_DIGITS, DIGIT_WORDS
from keryx.logging_util import mask_number

#: The transcripts' directory under `data_dir`.
CALLS_DIR = "calls"

log = logging.getLogger("keryx.transcripts")

#: How much of the previous call the context carries.
MAX_TRANSCRIPT_CHARS = 1200
#: Lines that are session bookkeeping (`--- session … ended`), not conversation.
_MARKER_PREFIX = "---"

#: Written when a call gives the PIN part-way through (see `was_authorized`).
AUTHORIZED_MARKER = "--- authorized"
_UNAUTHORIZED_FLAG = " authorized=no"

#: What a PIN is written down as, wherever it was said.
PIN_REDACTED = "[PIN]"


#: Two digits said as one number: "ten" to "nineteen", by their second digit, and the tens.
_TEENS = "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = dict(
    zip("23456789", "twenty thirty forty fifty sixty seventy eighty ninety".split(), strict=True)
)


def _digit(digit: str) -> str:
    """One digit, as a numeral or a whole word."""
    return rf"(?:{digit}|\b(?:{'|'.join(DIGIT_WORDS[digit])})\b)"


def _pair(first: str, second: str) -> str | None:
    """Two digits said as one: "forty-two", "twelve", "forty", or "double four"."""
    said = []
    if first == "1":
        said.append(rf"\b{_TEENS[int(second)]}\b")
    elif first in _TENS:
        unit = "" if second == "0" else BETWEEN_DIGITS + _digit(second)
        said.append(rf"\b{_TENS[first]}\b{unit}")
    if first == second:
        said.append(r"\bdouble" + BETWEEN_DIGITS + _digit(first))
    return "|".join(said) or None


def _said(digits: str) -> str:
    """`digits` in order, each one alone or two of them said as one number."""
    ways = [(_digit(digits[0]), digits[1:])]
    if len(digits) > 1 and (pair := _pair(digits[0], digits[1])):
        ways.append((f"(?:{pair})", digits[2:]))
    said = (head + (BETWEEN_DIGITS + _said(rest) if rest else "") for head, rest in ways)
    return f"(?:{'|'.join(said)})"


@lru_cache(maxsize=4)
def _pin_pattern(pin: str) -> re.Pattern[str]:
    """The PIN's digits in order — numerals, words, or pairs said as one ("forty-two",
    "double four") — loosely separated.

    Not anchored to digit boundaries on purpose: the PIN inside a longer run is still
    redacted, because the other direction lets a stray digit smuggle the rest out. A pair
    is redacted wherever it could be the PIN, too: replacing too much is the safe direction.
    """
    return re.compile(_said(pin), re.IGNORECASE)


def redact_pin(text: str, pin: str | None) -> str:
    """`text` with every occurrence of `pin`, typed or spoken, replaced by `PIN_REDACTED`.

    Unchanged when there is no PIN to look for, or one that is not plain ASCII digits
    (which `Settings` refuses anyway).
    """
    if not pin or not (pin.isascii() and pin.isdigit()):
        return text
    return _pin_pattern(pin).sub(PIN_REDACTED, text)


def session_header(session_id: str, channel: str, caller: str | None, *, authorized: bool) -> str:
    """The line a transcript opens with, saying whether the call started authorized.

    The caller is masked like every other record Keryx keeps of a number.
    """
    flag = "yes" if authorized else "no"
    who = mask_number(caller) if caller else "none"
    return f"--- session {session_id} channel={channel} caller={who} authorized={flag}"


def was_authorized(raw: str) -> bool:
    """Whether the call in transcript `raw` was ever authorized.

    False only for a call that opened `authorized=no` and never wrote `AUTHORIZED_MARKER`:
    a phone call that did not give the PIN, whose words are nobody's history — reading them
    back later would let a caller who proved nothing speak as them. A transcript from before
    the header carried the flag is their own, and counts as authorized.
    """
    lines = [line.split("] ", 1)[-1].strip() for line in raw.splitlines()]
    header = next((line for line in lines if line.startswith("--- session ")), "")
    return not header.endswith(_UNAUTHORIZED_FLAG) or AUTHORIZED_MARKER in lines


def calls_dir(data_dir: Path) -> Path:
    """Where the transcripts are, one `<session id>.log` per session. The one definition:
    the session writes here, and `recall` and `retention` read and prune it."""
    return data_dir / CALLS_DIR


def transcript_path(data_dir: Path, session_id: str) -> Path:
    """Where the session with this id wrote its transcript."""
    return calls_dir(data_dir) / f"{session_id}.log"


def read_tail(
    data_dir: Path,
    session_id: str,
    *,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
    pin: str | None = None,
) -> str:
    """The last of what was said in `session_id`, as `user:`/`assistant:` lines.

    Empty when there is no readable transcript — a call-back is still worth placing with
    only the task result, so nothing here is allowed to be fatal. `pin` is redacted from
    every line: this is read into the opening of a call that has not given the PIN yet.
    """
    if not session_id:
        return ""
    path = transcript_path(data_dir, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.info("no readable transcript for session %s at %s", session_id, path)
        return ""

    spoken: list[str] = []
    for line in raw.splitlines():
        # Drop the timestamp the session writes, and keep only what someone said.
        text = line.split("] ", 1)[-1].strip() if line.startswith("[") else line.strip()
        if not text or text.startswith(_MARKER_PREFIX):
            continue
        if text.startswith(("user:", "assistant:")):
            spoken.append(redact_pin(text, pin))

    transcript = "\n".join(spoken)
    if len(transcript) <= max_chars:
        return transcript
    # Keep the end, and start at a line boundary so it does not open mid-word.
    clipped = transcript[-max_chars:]
    _, newline, rest = clipped.partition("\n")
    return rest if newline else clipped
