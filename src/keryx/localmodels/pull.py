"""`keryx models pull`: one download, told as a progress bar or as JSON lines.

The steps — the name, a copy already on disk, the free space, the download — are written
once (`pull`), and told to a `Report`: `TextReport` for a terminal, `JsonLines` for the Mac
app, which streams `keryx models pull NAME --json` behind a progress bar and a Cancel button.

`JsonLines` is a contract. Every line is one JSON object; `progress` lines come first, and
exactly one ending closes the stream — `done`, `adopted`, `error` or `cancelled` — on every
path, a refusal before the download included, because the app reads stdout and nothing
else. Cancel is a SIGTERM (or SIGINT): `terminate_as_interrupt` turns the first into the
second for as long as the download runs, and the `.part` file stays where it is, so the
next pull resumes it.
"""

import json
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from typing import Any, Protocol

import rich.progress

from keryx.config import Settings
from keryx.localmodels import catalog, download, hardware

#: The exit code of a pull that was cancelled: a shell's, for a process ended by SIGINT.
CANCELLED = 130
#: The most often a progress line is written, unless a whole percent has passed since.
PROGRESS_EVERY_S = 0.25
#: The endings that say the model is there: an interrupt after one is still a success.
SUCCEEDED = frozenset({"done", "adopted"})
#: What Cancel sends, held off while the ending is written.
CANCEL_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})
#: An `error` event's `message` when what stopped the work is nothing written for a person:
#: the exception itself is the event's `detail`.
UNEXPECTED = "Something unexpected stopped it."


class Report(Protocol):
    """How a pull is told: its progress, and how it ended."""

    def error(self, message: str, *, detail: str | None = None) -> None: ...

    def adopted(self, path: Path, found: Path) -> None: ...

    def done(self, path: Path) -> None: ...

    def downloading(self, title: str, total: int) -> AbstractContextManager[download.Progress]: ...


def pull(settings: Settings, name: str, report: Report) -> int:
    """Download the catalog model `name` into `CACHE_DIR/models`; the exit code.

    2 for a name the catalog does not have, 1 for a disk too small or a download that did
    not arrive whole, 0 when the model is there — downloaded, or linked from a copy a
    Hugging Face tool already has.
    """
    entry = catalog.by_key(name)
    if entry is None:
        report.error(f"There is no model called {name}.", detail="`keryx models list` names them")
        return 2
    dest = download.model_path(settings.cache_dir, entry)
    if not download.is_downloaded(settings.cache_dir, entry) and (
        found := download.already_on_disk(entry)
    ):
        report.adopted(download.adopt(settings.cache_dir, entry, found), found)
        return 0
    need = download.remaining_bytes(settings.cache_dir, entry) + download.DISK_MARGIN_BYTES
    free = hardware.free_bytes(dest.parent)
    if need > download.DISK_MARGIN_BYTES and free < need:
        report.error(f"{entry.title} needs {need / 1e9:.1f} GB free, and there is "
                     f"{free / 1e9:.1f} GB.", detail=f"on {dest.parent}")
        return 1
    download.ensure_model_dir(settings.cache_dir, entry)
    try:
        with report.downloading(entry.title, entry.size) as progress:
            download.download(entry.url, dest, size=entry.size, sha256=entry.sha256,
                              progress=progress)
    except download.DownloadError as error:
        report.error(str(error))
        return 1
    report.done(dest)
    return 0


class TextReport:
    """For a terminal: a progress bar, the path on stdout, and what went wrong on stderr."""

    def __init__(self, echo: Callable[..., None]) -> None:
        self._echo = echo

    def error(self, message: str, *, detail: str | None = None) -> None:
        self._echo(message, err=True)
        if detail:
            self._echo(detail, err=True)

    def adopted(self, path: Path, found: Path) -> None:
        self._echo(f"{path} (already in {found.parent})")

    def done(self, path: Path) -> None:
        self._echo(f"{path}")

    @contextmanager
    def downloading(self, title: str, total: int) -> Iterator[download.Progress]:
        with rich.progress.Progress(
            *rich.progress.Progress.get_default_columns(), rich.progress.DownloadColumn()
        ) as bar:
            task = bar.add_task(title, total=total)
            yield lambda done: bar.update(task, completed=done)


class EventLines:
    """A JSON-lines stream that ends exactly once: one JSON object to a line, written by
    `write` (which flushes). `models pull --json` and `voice use here --json` are each one,
    with events of their own; the ending, its guard and Cancel are this class's."""

    #: The endings that say the work is done: an interrupt after one is still a success.
    succeeded: frozenset[str] = frozenset({"done"})

    def __init__(
        self, write: Callable[[str], None], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._write = write
        self._clock = clock
        #: The one ending told ("done", "error", …), or None; nothing is told after it.
        self.ending: str | None = None
        self._last: tuple[float, float] | None = None  # (when, percent) of the last progress

    def _progressed(self, done: int, total: int | None, **extra: Any) -> None:
        """A `progress` line, `extra` keys in it, written at most every `PROGRESS_EVERY_S`
        unless a whole percent has passed since the last; `total` None when not known."""
        now, percent = self._clock(), done * 100 / total if total else 0.0
        last = self._last
        if last is None or now - last[0] >= PROGRESS_EVERY_S or abs(percent - last[1]) >= 1:
            self._last = (now, percent)
            self._emit({"event": "progress", **extra, "done": done, "total": total})

    @property
    def ended(self) -> bool:
        return self.ending is not None

    def _emit(self, event: dict[str, Any]) -> None:
        self._write(json.dumps(event))

    def _end(self, event: dict[str, Any]) -> None:
        """Tell `event` as the ending, unless one was told. Cancel is held off while it is
        written and marked — a SIGTERM halfway would leave the stream with no ending — and
        lands once it is out, as an interrupt `guard` knows comes after the ending."""
        if self.ended:
            return
        held = signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)
        try:
            self._emit(event)
            self.ending = event["event"]
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)

    def error(self, message: str, *, detail: str | None = None) -> None:
        """The `error` ending: `message` one sentence for a person, and `detail` — always
        there, null when there is nothing more — what a tool said, a path, a log: what the
        person might pass on, never what the app leads with."""
        self._end({"event": "error", "message": message, "detail": detail})

    def cancelled(self) -> None:
        self._end({"event": "cancelled"})

    def guard(self, run: Callable[[], int]) -> int:
        """`run`'s exit code, with every way it can stop told as the ending: an interrupt
        as `cancelled` (130), anything unexpected as an `error` (1). What is raised after
        the ending was told — a refusal's own exit — goes on unchanged, and an interrupt
        that lands after it exits with the code that ending says: 0 for a success, 1 for
        an `error`."""
        try:
            return run()
        except KeyboardInterrupt:
            if self.ending in self.succeeded:
                return 0
            if self.ending == "error":
                return 1
            with suppress(KeyboardInterrupt):  # a second Cancel, held off while it was told
                self.cancelled()
            return CANCELLED
        except Exception as exc:
            if self.ended:
                raise
            with suppress(KeyboardInterrupt):
                self.error(UNEXPECTED, detail=unexpected_detail(exc))
            return 1


def unexpected_detail(exc: BaseException) -> str:
    """An exception nobody wrote a sentence for, as an error's `detail`: its type, then its
    text with every credential Keryx knows of taken out — it can quote a key or a code."""
    from keryx.child_env import redact_known

    said = str(exc)
    return f"{type(exc).__name__}: {redact_known(said)}" if said else type(exc).__name__


class JsonLines(EventLines):
    """For the Mac app: the pull's progress, then how it ended."""

    succeeded = SUCCEEDED

    def adopted(self, path: Path, found: Path) -> None:
        self._end({"event": "adopted", "path": str(path), "from": str(found)})

    def done(self, path: Path) -> None:
        self._end({"event": "done", "path": str(path)})

    @contextmanager
    def downloading(self, title: str, total: int) -> Iterator[download.Progress]:
        self._last = None
        yield lambda done: self._progressed(done, total)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@contextmanager
def terminate_as_interrupt() -> Iterator[None]:
    """SIGTERM raised as `KeyboardInterrupt`, as SIGINT already is, while this lasts; the
    handler before it put back after, or the default when it was not Python's own."""
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
