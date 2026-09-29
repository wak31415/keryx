"""Going back and forward: a section run again answers from its record, runs no probe twice,
offers every answer given before, and Tab lands on the first question not yet answered."""

import asyncio

import pytest

from jarvis.setup.context import Probes
from jarvis.setup.rewind import ASK, PROBE, Entry, Recorder, rewind
from jarvis.setup.ui import Back, Choice, Forward
from jarvis.setup.wizard import Section, run_walk

from .fakes import DEFAULT, ScriptedPrompter


def one(message: str, value="x", kind="text") -> Entry:
    return Entry(ASK, kind, message, value)


# --- the recorder ------------------------------------------------------------------------


def test_a_replay_answers_from_the_record_and_offers_the_next_answer():
    ui = ScriptedPrompter([("Surname", DEFAULT)])
    recorder = Recorder(ui, [one("Name", "Ada")], [one("Surname", "Lovelace")])

    recorder.note("about names")
    assert recorder.text("Name") == "Ada"
    recorder.success("saved NAME")
    recorder.note("and the rest")
    assert recorder.text("Surname", default="") == "Lovelace"

    assert [message for _, message in ui.asked] == ["Surname"]
    # what led up to the replayed answer is not shown; what leads up to the live one is
    assert ui.said == [("success", "saved NAME"), ("note", "and the rest")]
    assert recorder.log == [one("Name", "Ada"), one("Surname", "Lovelace")]


def test_a_replay_passes_over_what_the_section_no_longer_asks():
    ui = ScriptedPrompter()
    recorder = Recorder(ui, [one("Name", "Ada"), one("Numbers", "+1"), one("PIN", "yes")])

    assert recorder.text("PIN") == "yes"  # Name and Numbers are saved, so not asked
    assert ui.asked == []


def test_a_replay_stops_at_a_question_the_record_does_not_have():
    ui = ScriptedPrompter([("New PIN?", "no")])
    recorder = Recorder(ui, [one("Name", "Ada"), one("PIN", "yes")])

    assert recorder.text("Name") == "Ada"
    assert recorder.text("New PIN?") == "no"
    assert not recorder.replaying


def test_every_later_question_offers_its_last_answer():
    ui = ScriptedPrompter([("Pick", DEFAULT), ("Sure?", DEFAULT), ("Which?", DEFAULT),
                           ("Key", DEFAULT)])
    future = [one("Pick", "b", "select"), one("Sure?", False, "confirm"),
              one("Which?", ["b"], "checkbox"), one("Key", "sk-" + "x" * 20, "secret")]
    recorder = Recorder(ui, (), future)

    assert recorder.select("Pick", [Choice("a", "A"), Choice("b", "B")], default="a") == "b"
    assert recorder.confirm("Sure?", default=True) is False
    assert recorder.checkbox("Which?", [Choice("a", "A", checked=True), Choice("b", "B")]) == ["b"]
    assert recorder.secret("Key") == "sk-" + "x" * 20
    assert ui.currents["Key"] == "sk-" + "x" * 20


def test_a_secret_with_nothing_recorded_offers_what_the_section_does():
    ui = ScriptedPrompter([("Key", DEFAULT)])

    assert Recorder(ui).secret("Key", current="sk-saved") == "sk-saved"


def test_tab_leaves_a_question_that_was_answered_before():
    ui = ScriptedPrompter([("Name", Forward())])
    recorder = Recorder(ui, (), [one("Name", "Ada"), one("Numbers", "+1")])

    with pytest.raises(Forward):
        recorder.text("Name")
    assert recorder.record() == [one("Name", "Ada"), one("Numbers", "+1")]


def test_tab_at_a_question_never_answered_stays_there():
    ui = ScriptedPrompter([("Name", Forward()), ("Name", "Ada")])

    assert Recorder(ui).text("Name") == "Ada"


def test_a_probe_replayed_is_not_run_again():
    ran: list[str] = []
    ui = ScriptedPrompter([("Next", "x")])

    def login(argv):
        ran.append("login")
        return 0

    async def task(settings, agent, prompt):
        ran.append("task")
        return "drafts"

    script = [Entry(PROBE, "run_login", value=0),
              Entry(PROBE, "run_task", value="drafts", awaited=True)]
    recorder = Recorder(ui, script)
    probes = recorder.probes(Probes(run_login=login, run_task=task))
    with recorder.spinner("signing in"):
        assert probes.run_login(["claude", "login"]) == 0
    assert asyncio.run(probes.run_task(None, "claude", "p")) == "drafts"
    recorder.text("Next")

    assert ran == []
    assert ("spinner", "signing in") not in ui.said
    assert [entry.name for entry in recorder.log] == ["run_login", "run_task", "text"]


def test_a_probe_that_runs_again_spends_its_old_result():
    recorder = Recorder(ScriptedPrompter(), (), [Entry(PROBE, "headless", value=True), one("Q")])

    assert recorder.probes(Probes(headless=lambda: False)).headless() is False
    assert recorder.future == [one("Q")]


def test_a_probe_that_raised_runs_again():
    calls: list[str] = []

    async def gmail(settings):
        calls.append("gmail")
        if len(calls) == 1:
            raise RuntimeError("offline")
        return "ada@example.com"

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


def test_esc_goes_to_the_question_before_in_the_same_section():
    records = {"b": [one("B1"), Entry(PROBE, "smoke"), one("B2"), one("B3")]}

    assert rewind(["a", "b"], 1, records, before=3) == (1, 2)


def test_esc_at_a_sections_first_question_goes_to_the_last_one_asked_before_it():
    records = {"a": [one("A1"), one("A2")], "quiet": [Entry(PROBE, "smoke")], "c": [one("C1")]}

    assert rewind(["a", "quiet", "c"], 2, records, before=0) == (0, 1)


def test_esc_with_nothing_before_it_leaves_the_walk():
    assert rewind(["a"], 0, {"a": [one("A1")]}, before=0) is None


# --- the walk ----------------------------------------------------------------------------


def asking(*messages: str):
    def run(ctx) -> None:
        for message in messages:
            ctx.ui.text(message, default="")

    return run


WALK = [Section("a", "A", asking("A1", "A2")), Section("b", "B", asking("B1", "B2", "B3"))]


def test_the_walk_goes_back_across_sections_and_offers_every_answer_on_the_way_forward(make_ctx):
    ctx = make_ctx([
        ("A1", "one"),
        ("A2", "two"),
        ("B1", Back()),      # to A2, offered "two"
        ("A2", DEFAULT),
        ("B1", "b"),
        ("B2", "c"),
        ("B3", Back()),      # to B2, offered "c"
        ("B2", Back()),      # to B1, offered "b"
        ("B1", "bee"),
        ("B2", DEFAULT),     # still offered "c"
        ("B3", "done"),
    ])

    assert run_walk(ctx, WALK, records := {}) is True

    assert ctx.ui.done()
    assert ctx.ui.lines("section") == ["A", "B", "A", "B", "B", "B"]
    assert [entry.value for entry in records["b"]] == ["bee", "c", "done"]
    assert set(ctx.store.walked_sections()) == {"a", "b"}


def test_tab_replays_every_answer_and_lands_on_the_first_one_not_given(make_ctx):
    ctx = make_ctx([
        ("A1", "one"), ("A2", "two"), ("B1", "b"), ("B2", "c"),
        ("B3", Back()), ("B2", Back()), ("B1", Back()),   # back into A
        ("A2", Forward()),                                  # and straight back to B3
        ("B3", "done"),
    ])

    run_walk(ctx, WALK, records := {})

    assert ctx.ui.done()
    assert [entry.value for entry in records["a"] + records["b"]] == [
        "one", "two", "b", "c", "done"
    ]


def test_esc_at_the_first_question_leaves_the_walk(make_ctx):
    ctx = make_ctx([("A1", Back())])

    assert run_walk(ctx, WALK, {}) is False


def test_a_section_gone_back_into_is_reviewed(make_ctx):
    seen: list[bool] = []

    def run(ctx) -> None:
        seen.append(ctx.review)
        ctx.ui.text("Q1")
        ctx.ui.text("Q2")

    ctx = make_ctx([("Q1", "a"), ("Q2", Back()), ("Q1", DEFAULT), ("Q2", "b")])

    run_walk(ctx, [Section("s", "S", run)], {})

    assert seen == [False, True] and ctx.review is False


def test_a_section_that_asks_nothing_the_second_time_keeps_its_answers(make_ctx):
    """So Esc from the section after it still has an answer to go back to."""
    runs: list[int] = []

    def once(ctx) -> None:
        if not runs:
            ctx.ui.text("Only once")
        runs.append(1)

    walk = [Section("a", "A", once)]
    records: dict = {}
    run_walk(make_ctx([("Only once", "x")]), walk, records)
    run_walk(make_ctx([]), walk, records)

    assert records["a"] == [one("Only once", "x")]
