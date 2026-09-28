"""Reading the service's own logs back, to say whether a restart actually worked.

"Back up" is not the same as "working". A Jarvis that has just loaded a change to its own
code can come up and answer `/health` while a tool failed to register, a credential broke,
or the very import that carries the change threw. The process is alive; the update is not.
The only place that difference is written down is the log.

Scoping the scan is the whole problem. The service's log files are appended to across
every restart, so "are there errors in the log" is always yes. What matters is *since the
restart was asked for* — so the process that asks for one records how long each log file
was at that moment (`marks()`), and the process that comes back reads from there
(`errors_since()`). No timestamps to parse, no journal to query, and it works the same on
a Linux journal-less `append:` file and a macOS launchd one.

Three files, because a failure lands in different ones depending on how bad it is:
`jarvis.err.log` catches what kills the process before logging is even configured (an
import error in new code), `jarvis.out.log` catches what a library prints on its own, and
`jarvis.log` is our own rotated handler, which is where a *running* Jarvis records that
something went wrong.

Nothing here raises: this runs on a machine that may be half-broken, and a log it cannot
read is reported as no errors found rather than as a second failure.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.logscan")

#: The log files a service install writes, in the order a failure escalates through them.
#: `jarvis.err.log`/`jarvis.out.log` come from `ops/systemd/jarvis.service` and
#: `ops/launchd/dev.jarvis.agent.plist`; `jarvis.log` is our own rotating handler.
LOG_NAMES = ("jarvis.err.log", "jarvis.out.log", "jarvis.log")

#: How many error lines are kept. This is read out loud and texted, so it is a handful.
MAX_ERRORS = 3
#: How much of one error line survives. A traceback's last line is the useful part and is
#: rarely longer than this; anything past it is noise in a sentence.
MAX_LINE_CHARS = 160
#: How much of a log file is read. A crash loop can write a lot in a minute, and this runs
#: while somebody is waiting for a phone call.
MAX_SCAN_BYTES = 2_000_000

_TRACEBACK_HEADER = "Traceback (most recent call last):"
#: A log line at our own ERROR/CRITICAL level (`%(levelname)-7s` in `cli.LOG_FORMAT`).
_LEVEL_RE = re.compile(r"\b(?:ERROR|CRITICAL)\b")
#: The line that *ends* a traceback — `ModuleNotFoundError: No module named 'x'`. Anchored
#: at column zero, which is what separates it from the indented frames above it.
_EXCEPTION_RE = re.compile(r"^(?:\w+\.)*\w*(?:Error|Exception|Interrupt|Exit)\b")


@dataclass(frozen=True)
class LogErrors:
    """What the logs said since a mark: how many, and the last few worth repeating."""

    count: int = 0
    #: Up to `MAX_ERRORS` lines, oldest first — the tail, because the last failure in a
    #: crash loop is the one still happening.
    lines: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.count > 0

    def spoken(self) -> str:
        """One clause for the status summary, or "" when the logs were clean."""
        if not self.count:
            return ""
        plural = "s" if self.count != 1 else ""
        return f"{self.count} error{plural} in the log since, the last one: {self.lines[-1]}"

    def written(self) -> str:
        """The same for a text message: the lines themselves, one per line."""
        return "\n".join(self.lines)


def log_dir(state_dir: Path) -> Path:
    """Where the service's log files live."""
    return state_dir / "logs"


def marks(state_dir: Path) -> dict[str, int]:
    """How long each log file is right now — the "everything past here is new" line.

    Every name gets an entry, zero for a file that does not exist yet, so that an empty
    dict means one thing only: nobody took a mark. A file that appears later is read from
    the start, because all of it happened after this.
    """
    directory = log_dir(state_dir)
    found: dict[str, int] = {}
    for name in LOG_NAMES:
        try:
            found[name] = (directory / name).stat().st_size
        except OSError:
            found[name] = 0
    return found


def errors_since(state_dir: Path, recorded: dict[str, int] | None) -> LogErrors:
    """The errors written to the logs since `recorded` was taken by `marks()`.

    `None` or `{}` means nobody took a mark — an older restart record, or a machine with
    no log files at all. There is no way to tell this restart's errors from last month's
    in that case, so the honest answer is "nothing found", never the whole file.
    """
    if not isinstance(recorded, dict) or not recorded:
        return LogErrors()
    directory = log_dir(state_dir)
    lines: list[str] = []
    for name in LOG_NAMES:
        lines += _errors_in(directory / name, _mark(recorded, name))
    return LogErrors(count=len(lines), lines=tuple(lines[-MAX_ERRORS:]))


def _mark(recorded: dict[str, int], name: str) -> int:
    """One file's mark, defaulting to the start of the file for anything unusable.

    The marks come back through JSON, out of a file a person can edit by hand, so a
    string or a negative number is a thing that can happen; reading from the start is the
    safe reading of it (too much scanned, never too little).
    """
    value = recorded.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _errors_in(path: Path, start: int) -> list[str]:
    """The error lines in `path` past byte `start`. Never raises."""
    return _pick_errors(_read_from(path, start))


def _read_from(path: Path, start: int) -> list[str]:
    """The lines of `path` from byte `start`, capped. `[]` for anything unreadable."""
    try:
        size = path.stat().st_size
        # Smaller than the mark means the file was rotated or truncated under us, so the
        # mark points into content that no longer exists: everything here is new.
        offset = start if start <= size else 0
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(MAX_SCAN_BYTES)
    except OSError:
        log.debug("could not read %s while looking for restart errors", path)
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _pick_errors(lines: list[str]) -> list[str]:
    """The lines worth repeating, with each traceback collapsed to the line that ends it.

    A traceback's header and frames say nothing a person wants read out on the phone; the
    exception line at the bottom says all of it. So a header opens a traceback and the
    unindented exception line closes it, and only that line is kept.
    """
    found: list[str] = []
    pending: str | None = None  # the header of a traceback we have not seen the end of
    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue
        if _TRACEBACK_HEADER in line:
            if pending is not None:
                found.append(_trim(pending))  # a traceback that never terminated
            pending = line
            continue
        if pending is not None:
            if _EXCEPTION_RE.match(line):
                found.append(_trim(line))
                pending = None
            continue
        if _LEVEL_RE.search(line):
            found.append(_trim(line))
    if pending is not None:
        found.append(_trim(pending))
    return found


def _trim(line: str) -> str:
    """One log line, collapsed and cut to something that can be said in a sentence."""
    collapsed = " ".join(line.split())
    if len(collapsed) <= MAX_LINE_CHARS:
        return collapsed
    return collapsed[: MAX_LINE_CHARS - 1].rstrip() + "…"
