"""Going back a question: a section run again answers from its record, runs no probe twice,
and asks again from the question that was answered last."""

import asyncio

import pytest

from jarvis.setup.rewind import ASK, PROBE, Entry, Recorder, rewind
from jarvis.setup.ui import Back, Choice
from jarvis.setup.wizard import Section, run_walk

from .fakes import DEFAULT, ScriptedPrompter


def asks(recorder: Recorder) -> list[tuple[str, object]]:
    return [(entry.message, entry.value) for entry in recorder.log if entry.what == ASK]


# --- the recorder ------------------------------------------------------------------------


def test_a_replay_answers_from_the_record_and_asks_the_last_again_with_its_answer():
    ui = ScriptedPrompter([("Surname", DEFAULT)])
    script = [Entry(ASK, "text", "Name", "Ada")]
    recorder = Recorder(ui, script, hint=Entry(ASK, "text", "Surname", "Lovelace"))

    recorder.note("about names")
    assert recorder.text("Name") == "Ada"
    recorder.success("saved NAME")
    recorder.note("and the rest")
    assert recorder.text("Surname", default="") == "Lovelace"

    assert [message for _, message in ui.asked] == ["Surname"]
    # what led up to the replayed answer is not shown; what leads up to the live one is
    assert ui.said == [("success", "saved NAME"), ("note", "and the rest")]
    assert asks(recorder) == [("Name", "Ada"), ("Surname", "Lovelace")]


def test_a_replay_stops_at_the_first_question_that_is_not_the_one_recorded():
    ui = ScriptedPrompter([("Numbers", "+1555"), ("Name", DEFAULT)])
    script = [Entry(ASK, "text", "Name", "Ada"), Entry(ASK, "text", "Numbers", "+1")]
    recorder = Recorder(ui, script, hint=Entry(ASK, "text", "Name", "Ada"))

    recorder.text("Numbers")
    assert not recorder.replaying
    assert recorder.text("Name") == "Ada"  # the hint still lands where it belongs


@pytest.mark.parametrize(
    "kind, ask, answer",
    [
        ("select", lambda r: r.select("Pick", [Choice("a", "A"), Choice("b", "B")], default="a"),
         "b"),
        ("confirm", lambda r: r.confirm("Sure?", default=True), False),
        ("checkbox", lambda r: r.checkbox("Which?", [Choice("a", "A", checked=True),
                                                     Choice("b", "B")]), ["b"]),
    ],
)
def test_the_question_gone_back_to_offers_its_last_answer(kind, ask, answer):
    message = {"select": "Pick", "confirm": "Sure?", "checkbox": "Which?"}[kind]
    ui = ScriptedPrompter([(message, DEFAULT)])

    assert ask(Recorder(ui, hint=Entry(ASK, kind, message, answer))) == answer


def test_a_secret_is_never_offered_back():
    ui = ScriptedPrompter([("Key", "sk-new")])
    recorder = Recorder(ui, hint=Entry(ASK, "secret", "Key", "sk-old"))

    assert recorder.secret("Key") == "sk-new"


def test_a_probe_replayed_is_not_run_again():
    ran: list[str] = []
    ui = ScriptedPrompter([("Next", "x")])

    def login(argv):
        ran.append("login")
        return 0

    async def task(settings, agent, prompt):
        ran.append("task")
        return "drafts"

    script = [Entry(PROBE, "run_login", value=0), Entry(PROBE, "run_task", value="drafts",
                                                         awaited=True)]
    recorder = Recorder(ui, script)
    from jarvis.setup.context import Probes

    probes = recorder.probes(Probes(run_login=login, run_task=task))
    with recorder.spinner("signing in"):
        assert probes.run_login(["claude", "login"]) == 0
    assert asyncio.run(probes.run_task(None, "claude", "p")) == "drafts"
    recorder.text("Next")

    assert ran == []
    assert ("spinner", "signing in") not in ui.said
    assert [entry.name for entry in recorder.log] == ["run_login", "run_task", "text"]


def test_a_probe_is_recorded_live_and_one_that_raised_runs_again():
    calls: list[str] = []

    async def gmail(settings):
        calls.append("gmail")
        if len(calls) == 1:
            raise RuntimeError("offline")
        return "ada@example.com"

    from jarvis.setup.context import Probes

    first = Recorder(ScriptedPrompter())
    with pytest.raises(RuntimeError):
        asyncio.run(first.probes(Probes(gmail_address=gmail)).gmail_address(None))
    assert first.log == []

    second = Recorder(ScriptedPrompter(), first.log)
    assert asyncio.run(second.probes(Probes(gmail_address=gmail)).gmail_address(None)) == (
        "ada@example.com")
    assert second.log == [Entry(PROBE, "gmail_address", value="ada@example.com", awaited=True)]


def test_everything_said_goes_through_when_not_replaying():
    ui = ScriptedPrompter()
    recorder = Recorder(ui)
    recorder.intro("t", "s")
    recorder.section("S", (1, 2))
    recorder.warn("w")
    recorder.error("e")
    recorder.markdown("m")
    recorder.panel("p", "b")
    recorder.table(("h",), [("r",)])
    recorder.outro("o")
    with recorder.spinner("busy"):
        pass

    assert [kind for kind, _ in ui.said] == [
        "intro", "section", "warn", "error", "markdown", "panel", "table", "outro", "spinner"
    ]


# --- where Esc goes --------------------------------------------------------------------------


def one(message: str, value="x") -> Entry:
    return Entry(ASK, "text", message, value)


def test_esc_goes_to_the_question_before_in_the_same_section():
    records = {"b": [one("B1"), Entry(PROBE, "smoke"), one("B2")]}
    hints: dict = {}

    assert rewind(["a", "b"], 1, records, hints) == 1
    assert records["b"] == [one("B1"), Entry(PROBE, "smoke")] and hints["b"] == one("B2")


def test_esc_at_a_sections_first_question_goes_to_the_last_one_asked_before_it():
    records = {"a": [one("A1"), one("A2")], "quiet": [Entry(PROBE, "smoke")], "c": []}
    hints: dict = {}

    assert rewind(["a", "quiet", "c"], 2, records, hints) == 0
    assert records == {"a": [one("A1")]} and hints == {"a": one("A2")}


def test_esc_with_nothing_before_it_asks_the_same_question_again():
    records: dict = {"a": []}

    assert rewind(["a"], 0, records, {}) == 0
    assert records == {}


# --- the walk ----------------------------------------------------------------------------


def asking(*messages: str):
    def run(ctx) -> None:
        for message in messages:
            ctx.ui.text(message, default="")

    return run


def test_the_walk_goes_back_across_sections_and_forward_again(make_ctx):
    ctx = make_ctx([
        ("A1", "one"),
        ("A2", "two"),
        ("B1", Back()),      # to A2, offered "two"
        ("A2", DEFAULT),
        ("B1", "b"),
        ("B2", Back()),      # to B1, offered "b"
        ("B1", "bee"),
        ("B2", "done"),
    ])
    walk = [Section("a", "A", asking("A1", "A2")), Section("b", "B", asking("B1", "B2"))]

    run_walk(ctx, walk)

    assert ctx.ui.done()
    asked = [message for _, message in ctx.ui.asked]
    assert asked == ["A1", "A2", "B1", "A2", "B1", "B2", "B1", "B2"]  # A1 was replayed
    assert ctx.ui.lines("section") == ["A", "B", "A", "B", "B"]
    assert set(ctx.store.walked_sections()) == {"a", "b"}


def test_a_section_gone_back_into_is_reviewed(make_ctx):
    seen: list[bool] = []

    def run(ctx) -> None:
        seen.append(ctx.review)
        ctx.ui.text("Q1")
        ctx.ui.text("Q2")

    ctx = make_ctx([("Q1", "a"), ("Q2", Back()), ("Q1", DEFAULT), ("Q2", "b")])

    run_walk(ctx, [Section("s", "S", run)])

    assert seen == [False, True] and ctx.review is False
