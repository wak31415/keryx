"""Going back and forward in `keryx setup`: run the section again, answering from a record.

A section is ordinary code — it asks, looks at the answer, saves, reaches the network — so
there is no list of questions to step through. Instead a `Recorder` stands between the
section and the terminal and writes down every answer and every probe's result as it runs,
and the wizard keeps each section's record for the whole walk.

- **Esc** (`Back`) runs the section again in review mode, answering from its record up to
  the question before (`script`), and asks that one on the terminal. Nothing after it is
  thrown away: the rest of the record (`future`) offers each later question its last
  answer as the default, and a secret as `***` and its last four characters.
- **Tab** (`Forward`) runs the section again from its whole record, and every section
  after it from theirs, until a question comes up that the record has no answer for —
  the first one not yet answered — which is asked on the terminal.

A replayed probe hands back its recorded result without running, so no sign-in, no agent
task and no key check happens twice. A replay passes over what the record has that the
section no longer asks (an earlier save made it unnecessary), and stops at the first
question or probe the record does not have. Nothing said while replaying is shown, except
what leads up to where it stops.
"""

import contextlib
import dataclasses
import inspect
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from keryx.setup.context import Probes
from keryx.setup.ui import Choice, Forward, Prompter, Validator

ASK, PROBE = "ask", "probe"


@dataclass(frozen=True)
class Entry:
    """One answer (`ASK`, its kind and question) or one probe's result (`PROBE`, its name)."""

    what: str
    name: str
    message: str = ""
    value: Any = None
    #: A probe that returned an awaitable: its replay must return one too.
    awaited: bool = False


def _take(entries: list[Entry], what: str, name: str, message: str = "") -> Entry | None:
    """The first entry in `entries` that is this one, and everything before it, taken out."""
    for index, entry in enumerate(entries):
        if (entry.what, entry.name, entry.message) == (what, name, message):
            del entries[: index + 1]
            return entry
    return None


class Recorder:
    """A `Prompter` that answers from `script` while it lasts, and records everything.

    Once it asks on the terminal, `future` is what the section answered before, beyond this
    run: each question found there offers that answer as its default.
    """

    def __init__(
        self,
        ui: Prompter,
        script: Sequence[Entry] = (),
        future: Sequence[Entry] = (),
        *,
        on_live: Callable[[], None] | None = None,
    ):
        self.ui = ui
        self.script = list(script)
        self.future = list(future)
        self.log: list[Entry] = []
        self.on_live = on_live
        #: The recorded answer the question on the terminal was offered, until answered.
        self._offered: Entry | None = None
        #: What was said since the last replayed answer, shown if the replay stops here.
        self._held: list[tuple[Callable[..., None], tuple, dict]] = []

    @property
    def replaying(self) -> bool:
        return bool(self.script)

    def record(self) -> list[Entry]:
        """Everything this section has answered: this run, and what lay beyond it."""
        offered = [self._offered] if self._offered is not None else []
        return [*self.log, *offered, *self.future]

    def stop_replaying(self) -> None:
        """Show what was held, and ask everything from here on the terminal."""
        self.script.clear()
        for say, args, kwargs in self._held:
            say(*args, **kwargs)
        self._held.clear()

    # --- saying: held while replaying ----------------------------------------------------

    def _say(self, name: str, *args: Any, **kwargs: Any) -> None:
        say = getattr(self.ui, name)
        if self.script:
            self._held.append((say, args, kwargs))
        else:
            say(*args, **kwargs)

    def intro(self, title: str, subtitle: str = "") -> None:
        self._say("intro", title, subtitle)

    def section(self, title: str, step: tuple[int, int] | None = None) -> None:
        self._say("section", title, step)

    def note(self, text: str) -> None:
        self._say("note", text)

    def success(self, text: str) -> None:
        self._say("success", text)

    def warn(self, text: str) -> None:
        self._say("warn", text)

    def error(self, text: str) -> None:
        self._say("error", text)

    def markdown(self, text: str) -> None:
        self._say("markdown", text)

    def panel(self, title: str, body: str) -> None:
        self._say("panel", title, body)

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self._say("table", headers, rows)

    def outro(self, text: str) -> None:
        self._say("outro", text)

    @contextlib.contextmanager
    def spinner(self, message: str) -> Iterator[None]:
        if self.script:
            yield  # a replayed probe returns at once
            return
        with self.ui.spinner(message):
            yield

    # --- asking: from the script, else from the terminal ---------------------------------

    def _ask(self, kind: str, message: str, ask: Callable[[Entry | None], Any]) -> Any:
        if self.script and (step := _take(self.script, ASK, kind, message)) is not None:
            self._held.clear()
            answer = step.value
        else:
            self.stop_replaying()
            if self.on_live is not None:
                self.on_live()
            self._offered = _take(self.future, ASK, kind, message)
            while True:
                try:
                    answer = ask(self._offered)
                    break
                except Forward:
                    if self._offered is not None:
                        raise
                    # Not answered before: already where Tab would have gone.
            self._offered = None
        self.log.append(Entry(ASK, kind, message, answer))
        return answer

    def select(self, message: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        return self._ask("select", message, lambda offered: self.ui.select(
            message, choices, default=default if offered is None else offered.value
        ))

    def checkbox(self, message: str, choices: Sequence[Choice]) -> list[str]:
        def ask(offered: Entry | None) -> list[str]:
            if offered is not None:
                choices_ = [
                    dataclasses.replace(c, checked=c.value in offered.value) for c in choices
                ]
                return self.ui.checkbox(message, choices_)
            return self.ui.checkbox(message, choices)

        return self._ask("checkbox", message, ask)

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str:
        return self._ask("text", message, lambda offered: self.ui.text(
            message,
            default=default if offered is None else offered.value,
            validate=validate,
            multiline=multiline,
        ))

    def secret(self, message: str, *, validate: Validator | None = None, current: str = "") -> str:
        return self._ask("secret", message, lambda offered: self.ui.secret(
            message, validate=validate, current=current if offered is None else offered.value
        ))

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return self._ask("confirm", message, lambda offered: self.ui.confirm(
            message, default=default if offered is None else offered.value
        ))

    # --- the outside world: recorded, and replayed without running -----------------------

    def probes(self, real: Probes) -> Probes:
        """`real`, with every probe recorded — and, while replaying, not run."""
        return Probes(**{
            field.name: self._probe(field.name, getattr(real, field.name))
            for field in dataclasses.fields(real)
        })

    def _probe(self, name: str, call: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            if self.script and (step := _take(self.script, PROBE, name)) is not None:
                self.log.append(step)
                return _resolved(step.value) if step.awaited else step.value
            # A probe that runs is seen running, never hidden in a replay; and what it
            # answered last time is spent.
            self.stop_replaying()
            _take(self.future, PROBE, name)
            result = call(*args, **kwargs)
            if inspect.isawaitable(result):
                return self._recorded(name, result)
            self.log.append(Entry(PROBE, name, value=result))
            return result

        return run

    async def _recorded(self, name: str, pending: Any) -> Any:
        # A probe that raised is not recorded, so a replay reaches it and runs it again.
        result = await pending
        self.log.append(Entry(PROBE, name, value=result, awaited=True))
        return result


async def _resolved(value: Any) -> Any:
    return value


def rewind(
    keys: Sequence[str], index: int, records: dict[str, list[Entry]], before: int
) -> tuple[int, int] | None:
    """Where Esc goes from section `keys[index]`, whose first `before` entries were answered
    ahead of the question it was pressed at: the section to run again, and how much of its
    record to replay. A section that asked nothing is passed over; None when nothing before
    was asked at all.
    """
    for back in range(index, -1, -1):
        entries = records.get(keys[back], [])
        limit = before if back == index else len(entries)
        asked = [number for number, entry in enumerate(entries[:limit]) if entry.what == ASK]
        if asked:
            return back, asked[-1]
    return None
