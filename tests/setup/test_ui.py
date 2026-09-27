"""`RichPrompter`: what it draws, and how it asks — questionary is stubbed, so no terminal."""

import io

import pytest
from rich.console import Console

from jarvis.setup import ui
from jarvis.setup.ui import Aborted, Choice, RichPrompter


@pytest.fixture
def prompter():
    screen = RichPrompter()
    screen.console = Console(file=io.StringIO(), width=100, color_system=None)
    return screen


def drawn(prompter) -> str:
    return prompter.console.file.getvalue()


def test_every_kind_of_line_is_drawn_in_the_gutter(prompter):
    prompter.intro("Jarvis setup", "saved as you go")
    prompter.section("Voice")
    prompter.note("a note\nover two lines")
    prompter.success("saved")
    prompter.warn("careful")
    prompter.error("broken [not markup]")
    prompter.markdown("1. **one**")
    prompter.panel("Title", "body [x]")
    prompter.table(("A", "B"), [("1", "2")])
    prompter.outro("done")

    text = drawn(prompter)
    for fragment in ("Jarvis setup", "◆  Voice", "│  a note", "│  over two lines", "✓  saved",
                     "▲  careful", "✗  broken [not markup]", "one", "body [x]", "└  done"):
        assert fragment in text, fragment


def test_a_spinner_wraps_the_block(prompter):
    with prompter.spinner("working"):
        pass


class Question:
    def __init__(self, answer, recorded, kind, args, kwargs):
        self.answer = answer
        recorded.append((kind, args, kwargs))

    def ask(self):
        return self.answer


@pytest.fixture
def questionary(monkeypatch):
    """questionary's prompt functions, answering from `answers[kind]`."""
    import questionary as real

    recorded: list = []
    answers: dict = {}
    for kind in ("select", "checkbox", "text", "password", "confirm"):
        monkeypatch.setattr(
            real,
            kind,
            lambda *args, kind=kind, **kwargs: Question(
                answers.get(kind), recorded, kind, args, kwargs
            ),
        )
    return answers, recorded


def test_each_question_is_asked_and_answered(prompter, questionary):
    answers, recorded = questionary
    answers.update(select="b", checkbox=["a"], text="  typed  ", password=" sk ", confirm=True)
    choices = [Choice("a", "A", hint="first", checked=True), Choice("b", "B", disabled="no")]

    assert prompter.select("pick", choices, default="b") == "b"
    assert prompter.checkbox("pick some", choices) == ["a"]
    assert prompter.text("say", default="x", validate=lambda v: None, multiline=True) == "typed"
    assert prompter.secret("key", validate=lambda v: "bad" if not v else None) == "sk"
    assert prompter.confirm("sure?", default=False) is True

    kinds = [kind for kind, _, _ in recorded]
    assert kinds == ["select", "checkbox", "text", "password", "confirm"]
    select = recorded[0][2]
    assert select["default"].value == "b"
    assert [choice.value for choice in select["choices"]] == ["a", "b"]
    validate = recorded[3][2]["validate"]
    assert validate("") == "bad" and validate("x") is True


def test_ctrl_c_is_aborted(prompter, questionary):
    with pytest.raises(Aborted):
        prompter.confirm("sure?")


def test_no_validator_is_none():
    assert ui._validator(None) is None
