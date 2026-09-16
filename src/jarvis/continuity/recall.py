"""Looking something up in what has already happened (spec §3.3).

The briefing tells Jarvis what it should volunteer at the start of a call. This answers
the other half — "what did we decide about the Orchard sync?" — without spending a
subagent on a question the machine already has the answer to on disk.

Two sources, searched together and ranked as one list:

* **Past calls.** The transcripts `VoiceSession` writes to `data_dir/calls/<id>.log`. A
  hit is the matching line plus the lines around it, so an answer comes back with the
  question it answered.
* **Tasks.** Descriptions and spoken summaries, straight out of the store.

Matching is a deliberately dumb whole-word overlap. The query arrives as speech, already
mangled once by transcription; stemming or fuzzy matching on top of that produces
confident nonsense, and this is read out loud. Stop words are dropped, every remaining
term must appear, and the newest hits win ties.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis.continuity.transcripts import redact_pin
from jarvis.tasks.manager import TaskManager

log = logging.getLogger("jarvis.recall")

#: How many hits come back. This is spoken, so it is a handful, not a page.
DEFAULT_LIMIT = 4
MAX_LIMIT = 8
#: How much of a transcript hit is quoted, and how many lines of context around it.
MAX_SNIPPET_CHARS = 320
CONTEXT_LINES = 1
#: How many call logs are searched, newest first. A year of calls is not worth a scan on
#: the event loop's thread pool while he waits on the line.
MAX_CALL_FILES = 200

_WORD_RE = re.compile(r"[a-z0-9']+")
#: Words that match everything and mean nothing. Speech is full of them.
_STOP_WORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have he her him his
    how i if in into is it its me my of on or our out that the their them then there these they
    this to too us was we were what when where which who why will with would you your about
    said say says tell told get got go going just like know think want need thing things""".split()
)
#: A transcript line as `VoiceSession` writes it: `[<iso timestamp>] user: …`.
_LINE_RE = re.compile(r"^\[(?P<when>[^\]]+)\]\s*(?P<body>.*)$")
_SPOKEN_PREFIXES = ("user:", "assistant:")


@dataclass(frozen=True)
class Hit:
    """One thing found, in the shape the voice model is handed."""

    #: "call" or "task" — what kind of record this came out of.
    source: str
    #: When it happened, as a short spoken date ("22 August"), or "" if unknown.
    when: str
    text: str
    task_id: int | None = None

    def as_dict(self) -> dict:
        entry: dict = {"source": self.source, "text": self.text}
        if self.when:
            entry["when"] = self.when
        if self.task_id is not None:
            entry["task_id"] = self.task_id
        return entry


def terms(query: str) -> list[str]:
    """The words worth matching on, lowercased and de-duplicated in order."""
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(query.lower()):
        if len(word) > 1 and word not in _STOP_WORDS:
            seen.setdefault(word, None)
    return list(seen)


def _spoken_date(value: datetime | None) -> str:
    """A date the way it is said out loud, e.g. "22 August"; "" for nothing usable."""
    if value is None:
        return ""
    return value.astimezone().strftime("%-d %B")


def _parse_line(raw: str) -> tuple[datetime | None, str]:
    """One transcript line as `(timestamp, what was said)`; `("", …)` for a bookkeeping line."""
    match = _LINE_RE.match(raw.strip())
    body = match.group("body").strip() if match else raw.strip()
    if not body.startswith(_SPOKEN_PREFIXES):
        return None, ""
    when: datetime | None = None
    if match:
        try:
            when = datetime.fromisoformat(match.group("when"))
        except ValueError:
            when = None
    return when, body


def _matches(text: str, wanted: list[str]) -> bool:
    """True when every term appears in `text` (case-insensitive, substring)."""
    lowered = text.lower()
    return all(term in lowered for term in wanted)


def _snippet(lines: list[str], index: int) -> str:
    """The matching line with `CONTEXT_LINES` either side, trimmed to a speakable length."""
    start = max(0, index - CONTEXT_LINES)
    window = " ".join(lines[start : index + CONTEXT_LINES + 1])
    collapsed = " ".join(window.split())
    if len(collapsed) <= MAX_SNIPPET_CHARS:
        return collapsed
    return collapsed[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"


def _call_files(data_dir: Path) -> list[Path]:
    """The call transcripts, newest first, capped at `MAX_CALL_FILES`."""
    calls = data_dir / "calls"
    try:
        files = [path for path in calls.iterdir() if path.suffix == ".log"]
    except OSError:
        return []
    files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return files[:MAX_CALL_FILES]


def _file_date(path: Path) -> datetime | None:
    """When a call log was last written — the date of the call, for transcripts written
    before the stamp carried one."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def search_calls(
    data_dir: Path, wanted: list[str], *, limit: int, pin: str | None = None
) -> list[Hit]:
    """Hits from the call transcripts, newest call first. Blocking: run it in a thread.

    `pin` is redacted from every line *before* it is matched, not just from what comes
    back: logs written before redaction still hold the PIN he said aloud, and a search
    that found it would confirm a guess even with the digits blanked out.
    """
    if not wanted:
        return []
    hits: list[Hit] = []
    for path in _call_files(data_dir):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = [_parse_line(redact_pin(line, pin)) for line in raw.splitlines()]
        spoken = [body for _, body in parsed]
        fallback = None  # only paid for by a file that actually has a hit
        for index, (when, body) in enumerate(parsed):
            if not body or not _matches(body, wanted):
                continue
            if when is None:
                fallback = _file_date(path) if fallback is None else fallback
            hits.append(Hit("call", _spoken_date(when or fallback), _snippet(spoken, index)))
            if len(hits) >= limit:
                return hits
    return hits


class Recaller:
    """Answers "what did we say about X" from the transcripts and the task store."""

    def __init__(self, data_dir: Path, manager: TaskManager, *, pin: str | None = None) -> None:
        self._data_dir = data_dir
        self._manager = manager
        #: The configured PIN, redacted from every hit (see `search_calls`).
        self._pin = pin

    async def recall(self, query: str, *, limit: int = DEFAULT_LIMIT) -> list[Hit]:
        """Up to `limit` hits for `query`, tasks first then calls. Never raises."""
        wanted = terms(query)
        if not wanted:
            return []
        limit = min(max(limit, 1), MAX_LIMIT)
        tasks, calls = await asyncio.gather(
            self._tasks(wanted, limit), self._calls(wanted, limit), return_exceptions=True
        )
        hits: list[Hit] = []
        for found in (tasks, calls):
            if isinstance(found, BaseException):
                log.warning("half of a recall failed", exc_info=found)
                continue
            hits += found
        return hits[:limit]

    async def _tasks(self, wanted: list[str], limit: int) -> list[Hit]:
        found = await self._manager.search(wanted, limit=limit)
        return [
            Hit(
                "task",
                _spoken_date(task.finished_at or task.created_at),
                redact_pin(_task_text(task), self._pin),
                task_id=task.id,
            )
            for task in found
        ]

    async def _calls(self, wanted: list[str], limit: int) -> list[Hit]:
        return await asyncio.to_thread(
            search_calls, self._data_dir, wanted, limit=limit, pin=self._pin
        )


def _task_text(task) -> str:
    """What a task hit says: what he asked for, and what came back."""
    text = f"he asked: {task.description}"
    if task.summary:
        text += f" — result: {task.summary}"
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_SNIPPET_CHARS:
        return collapsed
    return collapsed[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"
