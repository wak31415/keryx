"""`RichPrompter`: what it draws, and how it asks — questionary is stubbed, so no terminal."""

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarvis.setup import ui
from jarvis.setup.ui import KEYS_HINT, Aborted, Back, Choice, RichPrompter


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
        self.application = SimpleNamespace(key_bindings=None, ttimeoutlen=0.5)
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


# --- one question to a screen ----------------------------------------------------------------


@pytest.fixture
def screens(prompter, monkeypatch):
    """What each cleared screen showed, the last one last."""
    monkeypatch.setattr(prompter.console, "clear", lambda home=True: prompter.console.file.write(
        "<CLEAR>"
    ))
    return lambda: drawn(prompter).split("<CLEAR>")[1:]


def test_each_question_gets_a_screen_of_its_own(prompter, questionary, screens):
    answers, _ = questionary
    answers.update(text="Ada", confirm=True)
    prompter.section("Owner and PIN", step=(4, 9))
    prompter.note("Who you are.")
    prompter.text("What should Jarvis call you?")
    prompter.success("saved OWNER_NAME")
    prompter.note("Your numbers.")
    prompter.confirm("Numbers?")

    first, second = screens()
    assert "◆  Owner and PIN  4 of 9" in first and KEYS_HINT in first
    assert "Who you are." in first
    assert "Owner and PIN" in second and KEYS_HINT in second
    # the last answer's outcome, and what leads up to this question — nothing earlier
    assert "saved OWNER_NAME" in second and "Your numbers." in second
    assert "Who you are." not in second


def test_what_a_section_before_said_is_shown_under_its_name(prompter, questionary, screens):
    answers, _ = questionary
    answers.update(confirm=True)
    prompter.section("Voice", step=(1, 2))
    prompter.success("OPENAI_API_KEY is set")
    prompter.section("Slack", step=(2, 2))
    prompter.note("Slack DMs.")
    prompter.confirm("Set up Slack?")

    [screen] = screens()
    assert screen.index("◇  Voice") < screen.index("OPENAI_API_KEY is set")
    assert screen.index("OPENAI_API_KEY is set") < screen.index("Slack DMs.")
    assert "◇  Slack" not in screen


def test_a_question_left_with_back_starts_the_next_screen_empty(prompter, monkeypatch, screens):
    import questionary as real

    def back(*args, **kwargs):
        question = Question(None, [], "confirm", args, kwargs)
        question.ask = lambda: (_ for _ in ()).throw(Back())
        return question

    monkeypatch.setattr(real, "confirm", back)
    prompter.note("before")
    with pytest.raises(Back):
        prompter.confirm("sure?")
    monkeypatch.setattr(real, "confirm", lambda *a, **k: Question(True, [], "confirm", a, k))
    prompter.confirm("again?")

    assert "before" not in screens()[-1]


@pytest.mark.parametrize("kind", ["select", "checkbox", "text", "password", "confirm"])
def test_escape_is_back_on_every_kind_of_question(kind):
    """The real questionary, on a pipe: Esc leaves with `Back`, not an answer."""
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as keys:
        options = {"choices": ["a", "b"]} if kind in ("select", "checkbox") else {}
        question = getattr(questionary, kind)("m", input=keys, output=DummyOutput(), **options)
        ui._bind_back(question.application)
        keys.send_text("\x1b")
        with pytest.raises(Back):
            question.unsafe_ask()


def test_a_multiline_box_keeps_escape_for_submitting(prompter, questionary, monkeypatch):
    """questionary submits a multiline answer with Esc then Enter."""
    answers, _ = questionary
    answers.update(text="typed")
    bound: list = []
    monkeypatch.setattr(ui, "_bind_back", bound.append)

    prompter.text("Summary", multiline=True)
    assert bound == []
    prompter.text("Name")
    assert len(bound) == 1
