"""Going back a question in `jarvis setup`: run the section again, answering from the record.

A section is ordinary code — it asks, looks at the answer, saves, reaches the network — so
there is no list of questions to step back through. Instead a `Recorder` stands between
the section and the terminal and writes down every answer and every probe's result as it
runs. Esc (`Back`) drops the last answer, and the wizard runs the section again in review
mode behind a `Recorder` handed the rest: it answers each question from the record, and
hands back each probe's recorded result without running it — so no sign-in, no agent task
and no key check happens twice — until the record runs out. The question after it is asked
on the terminal again, with the dropped answer as its default.

A replay stops at the first question that is not the one recorded (an earlier save changed
what the section asks) and asks from there, so going back may land a question early, never
late, and never skips one. Nothing said while replaying is shown, except what leads up to
the question the replay stops at.
"""

import contextlib
import dataclasses
import inspect
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.setup.context import Probes
from jarvis.setup.ui import Choice, Prompter, Validator

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


_NO_HINT = object()


class Recorder:
    """A `Prompter` that answers from `script` while it lasts, and records everything."""

    def __init__(self, ui: Prompter, script: Sequence[Entry] = (), hint: Entry | None = None):
        self.ui = ui
        self.script = list(script)
        self.log: list[Entry] = []
        self.hint = hint
        #: What was said since the last replayed answer, shown if the replay stops here.
        self._held: list[tuple[Callable[..., None], tuple, dict]] = []

    @property
    def replaying(self) -> bool:
        return bool(self.script)

    def _next(self, what: str, name: str, message: str = "") -> Entry | None:
        """The next recorded entry, taken, when it is this one; else None."""
        if self.script and (self.script[0].what, self.script[0].name,
                            self.script[0].message) == (what, name, message):
            return self.script.pop(0)
        return None

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

    def _ask(self, kind: str, message: str, ask: Callable[[Any], Any]) -> Any:
        if (step := self._next(ASK, kind, message)) is not None:
            self._held.clear()
            answer = step.value
        else:
            self.stop_replaying()
            previous = _NO_HINT
            if self.hint is not None and (self.hint.name, self.hint.message) == (kind, message):
                previous, self.hint = self.hint.value, None
            answer = ask(previous)
        self.log.append(Entry(ASK, kind, message, answer))
        return answer

    def select(self, message: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        return self._ask("select", message, lambda previous: self.ui.select(
            message, choices, default=default if previous is _NO_HINT else previous
        ))

    def checkbox(self, message: str, choices: Sequence[Choice]) -> list[str]:
        def ask(previous: Any) -> list[str]:
            if previous is not _NO_HINT:
                choices_ = [dataclasses.replace(c, checked=c.value in previous) for c in choices]
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
        return self._ask("text", message, lambda previous: self.ui.text(
            message,
            default=default if previous is _NO_HINT else previous,
            validate=validate,
            multiline=multiline,
        ))

    def secret(self, message: str, *, validate: Validator | None = None) -> str:
        # Never prefilled: a key typed again is typed again.
        return self._ask("secret", message, lambda _: self.ui.secret(message, validate=validate))

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return self._ask("confirm", message, lambda previous: self.ui.confirm(
            message, default=default if previous is _NO_HINT else previous
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
            if (step := self._next(PROBE, name)) is not None:
                self.log.append(step)
                return _resolved(step.value) if step.awaited else step.value
            self.stop_replaying()
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


def rewind(keys: Sequence[str], index: int, records: dict[str, list[Entry]],
           hints: dict[str, Entry]) -> int:
    """Where Esc at section `keys[index]` goes: the index of the section to run again.

    `records` holds what each section answered (this one's up to the Esc); the last answer
    before the Esc is dropped from its section's record and becomes its hint. A section
    that asked nothing is passed over. With nothing answered at all, this section runs
    again from its start.
    """
    for back in range(index, -1, -1):
        entries = records.get(keys[back], [])
        asked = [number for number, entry in enumerate(entries) if entry.what == ASK]
        if asked:
            records[keys[back]] = entries[: asked[-1]]
            hints[keys[back]] = entries[asked[-1]]
            return back
        records.pop(keys[back], None)
    return index
