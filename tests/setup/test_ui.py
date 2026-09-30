"""`RichPrompter`: what it draws, and how it asks — questionary is stubbed, so no terminal."""

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarvis.setup import ui
from jarvis.setup.ui import KEYS_HINT, Aborted, Back, Choice, Forward, RichPrompter, heading


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
    monkeypatch.setattr(ui, "_enter_ticks", lambda application: None)
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


@pytest.mark.parametrize("key, leaves", [("\x1b", Back), ("\t", Forward)])
@pytest.mark.parametrize("kind", ["select", "checkbox", "text", "password", "confirm"])
def test_esc_and_tab_leave_every_kind_of_question(kind, key, leaves):
    """The real questionary, on a pipe: Esc leaves with `Back`, Tab with `Forward`."""
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as keys:
        options = {"choices": ["a", "b"]} if kind in ("select", "checkbox") else {}
        question = getattr(questionary, kind)("m", input=keys, output=DummyOutput(), **options)
        ui._bind_back(question.application)
        keys.send_text(key)
        with pytest.raises(leaves):
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


def test_a_secret_given_before_is_masked_and_enter_keeps_it(prompter, questionary):
    answers, recorded = questionary
    answers.update(password="")
    key = "sk-proj-" + "a" * 20 + "WXYZ"

    required = lambda v: None if v.strip() else "Required."  # noqa: E731
    assert prompter.secret("OpenAI API key", validate=required, current=key) == key

    [(_, (message,), kwargs)] = recorded
    assert message == "OpenAI API key (***WXYZ — Enter keeps it)"
    assert key not in message
    assert kwargs["validate"]("") is True  # blank is keeping it, not refused


def test_typing_over_a_secret_replaces_it_and_is_still_checked(prompter, questionary):
    answers, recorded = questionary
    answers.update(password=" new ")

    assert prompter.secret("Key", validate=lambda v: "bad" if v == "x" else None,
                           current="old") == "new"
    assert recorded[0][2]["validate"]("x") == "bad"


def test_a_short_secret_shows_none_of_itself():
    assert ui.mask("482915") == "***"
    assert ui.mask("xoxb-1234567890-abcd") == "***abcd"


def test_a_heading_is_a_line_that_cannot_be_picked():
    import questionary

    line = ui._choice(heading("Configured"))
    assert isinstance(line, questionary.Separator) and "Configured" in line.title
    assert isinstance(ui._choice(heading("")), questionary.Separator)


def ask_checkbox(keys: str, options=("a", "b")):
    """The real questionary checkbox, as `RichPrompter` builds it, answering `keys`."""
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        question = questionary.checkbox(
            "m",
            choices=[*options, questionary.Separator(" "),
                     questionary.Choice(title=ui.CONTINUE_TITLE, value=ui.CONTINUE)],
            input=pipe,
            output=DummyOutput(),
        )
        ui._enter_ticks(question.application)
        pipe.send_text(keys)
        return question.unsafe_ask(), question


DOWN = "\x1b[B"


@pytest.mark.parametrize(
    "keys, ticked",
    [
        ("\r" + DOWN + DOWN + "\r", ["a"]),               # enter ticks, Continue moves on
        (DOWN + "\r\r" + DOWN + "\r", []),               # enter again unticks
        (" " + DOWN + " " + DOWN + "\r", ["a", "b"]),      # space ticks as well
        ("ai" + DOWN + DOWN + "\r", []),                   # no select-all, no invert
    ],
)
def test_enter_ticks_and_continue_moves_on(keys, ticked):
    answer, _ = ask_checkbox(keys)

    assert answer == ticked


def test_continue_has_no_box_to_tick():
    from questionary.prompts.common import InquirerControl

    _, question = ask_checkbox("\r" + DOWN + "\r", options=("a",))
    [control] = [window.content for window in question.application.layout.find_all_windows()
                 if isinstance(window.content, InquirerControl)]
    drawn = control.create_content(80, 10)  # what reaches the screen
    tokens = [token for line in range(drawn.line_count) for token in drawn.get_line(line)]
    before = tokens[tokens.index(ui.CONTINUE_TITLE[0]) - 1]
    assert before[1].strip() == ""


def test_continue_is_not_an_answer(prompter, questionary):
    answers, recorded = questionary
    answers.update(checkbox=["a", ui.CONTINUE])

    assert prompter.checkbox("Which?", [Choice("a", "A")]) == ["a"]
    labels = [choice.title for choice in recorded[0][2]["choices"]]
    assert labels[-1] == ui.CONTINUE_TITLE
