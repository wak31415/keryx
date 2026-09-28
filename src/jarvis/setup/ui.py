"""The terminal `jarvis setup` draws on, behind a `Prompter` a test can script.

Everything the wizard says or asks goes through a `Prompter`, so no section needs a
terminal to test: `tests/setup/fakes.py` answers from a script and keeps what was said.
`RichPrompter` is the real one, in the clack style — a `◆` header per section, a `│` gutter
for what is said inside it, `✓`/`▲`/`✗` for how a step went — drawn with rich and asked with
questionary. Both are imported here and nowhere else.

It shows one question at a time: each clears the screen and draws the section's header,
then only what was said since the last answer — that answer's outcome, and the notes that
lead up to this question — so the history of the walk is in the scrollback, not in the way.

A person pressing ctrl-c at any question raises `Aborted`. Every section saves as it goes,
so stopping part way loses only the question that was being asked. Esc raises `Back`,
which the wizard turns into the question before (`jarvis.setup.rewind`).
"""

import contextlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

#: Returns why an answer is not acceptable, or None when it is.
Validator = Callable[[str], str | None]


class Aborted(Exception):
    """The person stopped the wizard (ctrl-c, or the end of input)."""


class Back(Exception):
    """The person asked for the question before this one (Esc)."""


@dataclass(frozen=True)
class Choice:
    """One option in a `select` or `checkbox`."""

    value: str
    label: str
    hint: str = ""
    checked: bool = False
    #: Why it cannot be picked, shown beside it; None when it can.
    disabled: str | None = None


class Prompter(Protocol):
    """What a section may say, and what it may ask."""

    def intro(self, title: str, subtitle: str = "") -> None: ...
    def section(self, title: str, step: tuple[int, int] | None = None) -> None: ...
    def note(self, text: str) -> None: ...
    def success(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def markdown(self, text: str) -> None: ...
    def panel(self, title: str, body: str) -> None: ...
    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None: ...
    def outro(self, text: str) -> None: ...

    def select(
        self, message: str, choices: Sequence[Choice], *, default: str | None = None
    ) -> str: ...
    def checkbox(self, message: str, choices: Sequence[Choice]) -> list[str]: ...
    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str: ...
    def secret(self, message: str, *, validate: Validator | None = None) -> str: ...
    def confirm(self, message: str, *, default: bool = True) -> bool: ...
    def spinner(self, message: str) -> contextlib.AbstractContextManager[None]: ...


#: Under each section's header: the two keys that are not answers.
KEYS_HINT = "esc goes back a question · ctrl-c stops (what is answered is saved)"


class RichPrompter:
    """The real terminal: rich for what is said, questionary for what is asked."""

    def __init__(self) -> None:
        from rich.console import Console

        self.console = Console(highlight=False)
        self._title = ""
        self._header = ""
        #: What was said since the last answer, with the section it was said in, redrawn
        #: under the header of the next question.
        self._screen: list[tuple[str, Callable[[], None]]] = []

    def _show(self, draw: Callable[[], None]) -> None:
        draw()
        self._screen.append((self._title, draw))

    def _redraw(self) -> None:
        """A fresh screen for a question: the header, then what led up to it."""
        self.console.clear()
        if self._header:
            self.console.print(self._header)
            self.console.print(f"[dim]   {KEYS_HINT}[/]")
        shown = self._title
        for title, draw in self._screen:
            if title != shown:
                # Said before this section began: the end of the one before, under its name.
                current = title == self._title or not title
                self.console.print("" if current else f"\n[dim]◇  {_escape(title)}[/]")
                shown = title
            draw()
        self.console.print()

    # --- saying ------------------------------------------------------------------------

    def intro(self, title: str, subtitle: str = "") -> None:
        from rich.panel import Panel

        body = f"[bold]{title}[/]" + (f"\n[dim]{subtitle}[/]" if subtitle else "")
        self._show(lambda: self.console.print(
            Panel(body, border_style="cyan", expand=False, padding=(0, 2))
        ))

    def section(self, title: str, step: tuple[int, int] | None = None) -> None:
        progress = f"  [dim]{step[0]} of {step[1]}[/]" if step else ""
        self._title = title
        self._header = f"[bold cyan]◆[/]  [bold]{_escape(title)}[/]{progress}"
        self.console.print("\n" + self._header)

    def _gutter(self, text: str, style: str = "", mark: str = "│") -> None:
        lines = []
        for number, line in enumerate(text.splitlines() or [""]):
            prefix = mark if number == 0 else "│"
            lines.append(f"[dim]{prefix}[/]  " + (f"[{style}]{_escape(line)}[/]"
                         if style else _escape(line)))

        def draw() -> None:
            for line in lines:
                self.console.print(line)

        self._show(draw)

    def note(self, text: str) -> None:
        self._gutter(text, "dim")

    def success(self, text: str) -> None:
        self._gutter(text, "green", "[green]✓[/]")

    def warn(self, text: str) -> None:
        self._gutter(text, "yellow", "[yellow]▲[/]")

    def error(self, text: str) -> None:
        self._gutter(text, "red", "[red]✗[/]")

    def markdown(self, text: str) -> None:
        from rich.markdown import Markdown
        from rich.padding import Padding

        self._show(lambda: self.console.print(Padding(Markdown(text), (0, 0, 0, 3))))

    def panel(self, title: str, body: str) -> None:
        from rich.panel import Panel

        panel = Panel(_escape(body), title=_escape(title), title_align="left", border_style="dim")
        self._show(lambda: self.console.print(panel))

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        from rich.table import Table

        table = Table(box=None, padding=(0, 2), show_edge=False)
        for header in headers:
            table.add_column(header, style="bold" if header == headers[0] else "")
        for row in rows:
            table.add_row(*(_escape(cell) for cell in row))
        self._show(lambda: self.console.print(table))

    def outro(self, text: str) -> None:
        self._show(lambda: self.console.print(f"[dim]└[/]  {_escape(text)}\n"))

    # --- asking ------------------------------------------------------------------------

    def _ask(self, question, *, back: bool = True) -> object:
        """Ask on a fresh screen; Esc is `Back` (not in a multiline box, where it submits)."""
        self._redraw()
        if back:
            _bind_back(question.application)
        try:
            answer = question.ask()
        finally:
            self._screen = []
        if answer is None:
            raise Aborted
        return answer

    def select(self, message: str, choices: Sequence[Choice], *, default: str | None = None) -> str:
        import questionary

        options = [_choice(choice) for choice in choices]
        start = next((option for option in options if option.value == default), None)
        return str(
            self._ask(
                questionary.select(
                    message, choices=options, default=start, qmark="◇", style=_style()
                )
            )
        )

    def checkbox(self, message: str, choices: Sequence[Choice]) -> list[str]:
        import questionary

        answer = self._ask(
            questionary.checkbox(
                message,
                choices=[_choice(choice) for choice in choices],
                qmark="◇",
                style=_style(),
            )
        )
        return [str(value) for value in answer]  # type: ignore[union-attr]

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str:
        import questionary

        return str(
            self._ask(
                questionary.text(
                    message,
                    default=default,
                    validate=_validator(validate),
                    multiline=multiline,
                    qmark="◇",
                    style=_style(),
                ),
                back=not multiline,
            )
        ).strip()

    def secret(self, message: str, *, validate: Validator | None = None) -> str:
        import questionary

        return str(
            self._ask(
                questionary.password(
                    message, validate=_validator(validate), qmark="◇", style=_style()
                )
            )
        ).strip()

    def confirm(self, message: str, *, default: bool = True) -> bool:
        import questionary

        return bool(
            self._ask(questionary.confirm(message, default=default, qmark="◇", style=_style()))
        )

    @contextlib.contextmanager
    def spinner(self, message: str) -> Iterator[None]:
        with self.console.status(f"[cyan]{_escape(message)}[/]", spinner="dots"):
            yield


def _bind_back(application) -> None:
    """Esc leaves the question with `Back` instead of an answer."""
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    keys = KeyBindings()

    @keys.add("escape", eager=True)
    def _back(event) -> None:
        event.app.exit(exception=Back())

    own = application.key_bindings
    application.key_bindings = merge_key_bindings([own, keys]) if own is not None else keys
    # A lone Esc is told from the start of an arrow key by a pause; keep it short.
    application.ttimeoutlen = 0.1


def _escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


def _validator(validate: Validator | None):
    if validate is None:
        return None
    return lambda value: validate(value) or True


def _choice(choice: Choice):
    import questionary

    title = [("", choice.label)]
    if choice.hint:
        title.append(("class:hint", f"  {choice.hint}"))
    return questionary.Choice(
        title=title, value=choice.value, checked=choice.checked, disabled=choice.disabled
    )


def _style():
    import questionary

    return questionary.Style(
        [
            ("qmark", "fg:ansicyan bold"),
            ("question", "bold"),
            ("pointer", "fg:ansicyan bold"),
            ("highlighted", "fg:ansicyan"),
            ("selected", "fg:ansigreen"),
            ("answer", "fg:ansicyan"),
            ("hint", "fg:ansibrightblack"),
            ("disabled", "fg:ansibrightblack italic"),
        ]
    )
