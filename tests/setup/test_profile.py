"""A first memory: `keryx memory seed` for an agent, the "About you" section for a person,
and the report both end with — what every call will carry."""

import json
import stat
import sys

import pytest
import typer
from typer.testing import CliRunner
from wording import for_the_app

from keryx import cli
from keryx.cli import app
from keryx.config import Settings, write_enrolled_pin
from keryx.config.store import ConfigStore
from keryx.continuity.memory import MAX_MEMORY_CHARS, memory_path, read_memory, seed_memory
from keryx.projects import MAX_BRIEFS_CHARS
from keryx.setup import profile
from keryx.setup.profile import PIN_NOTES, facts_from_text, pin_note, setup_report, setup_summary

from .fakes import DEFAULT

runner = CliRunner()


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A hermetic install, as every command will load it."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        owner_name="Ada",
        data_dir=tmp_path / "keryx",
        projects_root=tmp_path / "projects",
        skills_dir=tmp_path / "skills",
    )
    monkeypatch.setattr("keryx.cli.load_settings", lambda **overrides: settings)
    return settings


def memory_text(settings) -> str:
    return memory_path(settings.data_dir).read_text()


# --- keryx memory seed ---------------------------------------------------------------


def test_seed_takes_the_facts_on_stdin_and_writes_them_owner_only(home):
    result = runner.invoke(
        app, ["memory", "seed", "--file", "-"], input="# about me\nWorks nights.\n\nLikes brevity\n"
    )

    assert result.exit_code == 0, result.output
    text = memory_text(home)
    assert "# What Lyra knows about Ada" in text
    assert "- Works nights." in text and "- Likes brevity" in text
    assert stat.S_IMODE(memory_path(home.data_dir).stat().st_mode) == 0o600
    assert "sent to the realtime provider on every call" in result.output


def test_seed_reads_a_file(home, tmp_path):
    facts = tmp_path / "facts.txt"
    facts.write_text("Runs the orchard project.\n")

    result = runner.invoke(app, ["memory", "seed", "--file", str(facts)])

    assert result.exit_code == 0
    assert "- Runs the orchard project." in memory_text(home)


def test_an_unreadable_file_is_a_wrong_command_line(home, tmp_path):
    result = runner.invoke(app, ["memory", "seed", "--file", str(tmp_path / "missing")])

    assert result.exit_code == 2
    assert "could not read" in result.output


def test_seed_never_replaces_a_memory_without_force(home):
    seed_memory(home.data_dir, owner="Ada", assistant="Lyra", facts=["Old fact."])

    refused = runner.invoke(app, ["memory", "seed", "--file", "-"], input="New fact.\n")
    forced = runner.invoke(app, ["memory", "seed", "--file", "-", "--force"], input="New fact.\n")

    assert refused.exit_code == 1 and "`keryx memory seed --force` replaces it" in refused.output
    assert forced.exit_code == 0 and "- New fact." in memory_text(home)


def test_nothing_given_is_unchanged_and_not_a_failure(home):
    result = runner.invoke(app, ["memory", "seed", "--file", "-", "--json"], input="\n")

    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "unchanged"
    assert not memory_path(home.data_dir).exists()


def test_a_memory_too_long_for_a_call_is_refused(home):
    result = runner.invoke(
        app, ["memory", "seed", "--file", "-", "--json"], input="x" * (MAX_MEMORY_CHARS + 1)
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "too_long"
    assert not memory_path(home.data_dir).exists()


def test_json_reports_everything_a_call_will_carry_and_nothing_else(home):
    for name in ("orchard", "weather"):
        (home.projects_root / name).mkdir(parents=True)
    (home.projects_root / "orchard" / ".keryx-brief.md").write_text("Soil sensors.")
    (home.data_dir / "projects").mkdir(parents=True)
    (home.data_dir / "projects" / "weather.md").write_text("Forecasts.")

    result = runner.invoke(app, ["memory", "seed", "--file", "-", "--json"], input="Nights.\n")

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["status"] == "written"
    assert summary["memory"]["written"] is True
    assert summary["projects"] == [
        {"name": "orchard", "brief_chars": len("Soil sensors.")},
        {"name": "weather", "brief_chars": len("Forecasts.")},
    ]
    assert summary["briefs"]["count"] == 2
    assert summary["briefs"]["max_chars"] == MAX_BRIEFS_CHARS
    assert summary["per_call_chars"] == 23 + summary["memory"]["chars"]


def test_json_says_whether_a_pin_exists_and_where_from_never_the_digits(home):
    write_enrolled_pin(home.config_dir, "482915")
    enrolled = Settings(_env_file=None, openai_api_key="x", data_dir=home.data_dir)

    pin = setup_summary(enrolled)["pin"]

    assert pin["set"] is True and pin["source"] == "enrolled"
    assert "482915" not in json.dumps(setup_summary(enrolled))


# --- what the PIN note says -----------------------------------------------------------


def test_the_pin_note_names_each_of_the_four_states(home):
    assert pin_note(home) == PIN_NOTES[None]
    assert "keryx setup" in pin_note(home)
    assert pin_note(home.model_copy(update={"pin": "482915"})) == PIN_NOTES["environment"]

    home.config_dir.mkdir(parents=True, exist_ok=True)
    (home.config_dir / "pin").write_text("nope\n")
    sealed = Settings(_env_file=None, openai_api_key="x", data_dir=home.data_dir)
    assert "no call can set one" in pin_note(sealed)


# --- the report ------------------------------------------------------------------------


def test_the_report_says_the_projects_root_is_missing(home):
    lines = setup_report(home)

    assert f"projects root: {home.projects_root} does not exist" in lines[0]


def test_the_report_counts_projects_briefs_skills_and_what_every_call_carries(home):
    for name in ("orchard", "weather"):
        (home.projects_root / name).mkdir(parents=True)
    (home.projects_root / "orchard" / ".keryx-brief.md").write_text("Soil sensors.")
    (home.skills_dir / "mermaid").mkdir(parents=True)
    (home.skills_dir / "mermaid" / "SKILL.md").write_text(
        "---\nname: mermaid\ndescription: Diagrams.\n---\n"
    )
    seed_memory(home.data_dir, owner="Ada", assistant="Lyra", facts=["Works nights."])
    memory_chars = len(memory_text(home).strip())

    report = "\n".join(setup_report(home))

    assert "projects: 2, and 1 with a brief: orchard" in report
    assert f"{len('Soil sensors.') + memory_chars} characters" in report
    assert "skills: 1" in report
    assert "~/.claude/CLAUDE.md" in report


def test_every_enabled_agents_instructions_file_is_named(home, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home.agents_enabled = ["claude", "codex"]

    summary = setup_summary(home)

    assert summary["subagent_memories"] == {
        "claude": "~/.claude/CLAUDE.md",
        "codex": "~/.codex/AGENTS.md",
    }


def test_facts_skip_blank_lines_and_headings():
    assert facts_from_text("# Me\n\n  one  \n\n## More\ntwo\n") == ["one", "two"]


# --- the "About you" section ---------------------------------------------------------------


def test_the_section_writes_what_they_use_it_for_and_their_facts(make_ctx):
    ctx = make_ctx(
        [
            ("mostly use Lyra for", ["email", "coding"]),
            ("Anything else", "reading papers"),
            ("fact", "Works nights."),
            ("fact", ""),
            ("Write it?", True),
        ]
    )

    profile.run_section(ctx)

    text = read_memory(ctx.settings.data_dir)
    assert (
        "Mostly uses Lyra for email triage, coding tasks in their repositories, reading papers."
        in text
    )
    assert "- Works nights." in text
    path = str(memory_path(ctx.settings.data_dir))
    assert any(line.startswith(path) for line in ctx.ui.lines("panel"))


def test_saying_no_writes_nothing(make_ctx):
    ctx = make_ctx(
        [("mostly use", []), ("Anything else", ""), ("fact", "Hi."), ("fact", ""), ("Write", False)]
    )

    profile.run_section(ctx)

    assert not memory_path(ctx.settings.data_dir).exists()


def test_a_memory_there_is_kept_unless_they_want_it_replaced(make_ctx):
    ctx = make_ctx([("Replace that memory", DEFAULT)])
    seed_memory(ctx.settings.data_dir, owner="Ada", assistant="Lyra", facts=["Old."])

    profile.run_section(ctx)

    assert "- Old." in read_memory(ctx.settings.data_dir)


def test_nothing_said_writes_nothing_and_says_the_first_call_will_introduce_itself(make_ctx):
    ctx = make_ctx([("mostly use", []), ("Anything else", ""), ("fact", "")])

    profile.run_section(ctx)

    assert not memory_path(ctx.settings.data_dir).exists()
    assert any("introduce itself" in line for line in ctx.ui.lines("note"))


# --- the section, for the Mac app: keryx setup profile ------------------------------------


def profile_json(*args: str, answers=None):
    stdin = None if answers is None else json.dumps(answers)
    result = runner.invoke(app, ["setup", "profile", *args, "--json"], input=stdin)
    return result, (json.loads(result.stdout) if result.stdout.strip() else None)


def test_the_questions_are_the_wizards_in_its_order(home):
    result, document = profile_json()

    assert result.exit_code == 0, result.output
    assert [question["id"] for question in document["questions"]] == ["uses", "other", "facts"]
    uses = document["questions"][0]
    assert uses["prompt"] == "What will you mostly use Lyra for?"
    assert [choice["value"] for choice in uses["choices"]] == list(profile.USES)
    assert uses["choices"][1]["label"] == "coding tasks in your repositories"
    assert document["note"] == profile.PRIVACY_NOTE
    assert document["memory"] == {"exists": False, "chars": 0, "max_chars": MAX_MEMORY_CHARS}
    assert all(question["key"] is None and question["current"] is None
               for question in document["questions"])


def test_a_memory_there_is_asked_about_first(home):
    seed_memory(home.data_dir, owner="Ada", assistant="Lyra", facts=["Old."])

    _, document = profile_json()

    first = document["questions"][0]
    assert first["id"] == "replace" and first["input"] == "boolean"
    assert first["default"] is False and not first["optional"]
    assert document["memory"]["exists"] is True


def test_every_question_is_worded_for_the_app():
    settings = Settings(_env_file=None, openai_api_key="test")
    document = profile.questions(settings)
    for question in document["questions"]:
        assert for_the_app(question["prompt"]) and for_the_app(question["title"])
    assert for_the_app(document["note"])


def test_applied_answers_write_what_the_wizard_writes(home):
    result, outcome = profile_json("--apply", answers={
        "uses": ["coding", "email"], "other": "reading papers",
        "facts": ["Works nights.", "", "Likes it short."],
    })

    assert result.exit_code == 0, result.output
    assert outcome["status"] == "written" and outcome["written"] and outcome["walked"]
    text = memory_text(home)
    assert outcome["text"] == text and outcome["chars"] == len(text)
    assert ("Mostly uses Lyra for email triage, coding tasks in their repositories, "
            "reading papers.") in text
    assert "- Works nights." in text and "- Likes it short." in text
    assert stat.S_IMODE(memory_path(home.data_dir).stat().st_mode) == 0o600
    assert "profile" in ConfigStore().walked_sections()


def test_a_preview_shows_the_document_and_writes_nothing(home):
    result, outcome = profile_json("--preview", answers={"facts": ["Works nights."]})

    assert result.exit_code == 0
    assert outcome["status"] == "written" and not outcome["written"] and not outcome["walked"]
    assert "invalid" not in outcome
    assert "- Works nights." in outcome["text"]
    assert not memory_path(home.data_dir).exists()
    assert "profile" not in ConfigStore().walked_sections()


def test_a_memory_there_is_kept_unless_the_answers_replace_it(home):
    seed_memory(home.data_dir, owner="Ada", assistant="Lyra", facts=["Old."])

    _, kept = profile_json("--apply", answers={"facts": ["New."]})
    assert kept["status"] == "kept" and not kept["written"] and kept["walked"]
    assert "- Old." in memory_text(home)

    _, replaced = profile_json("--apply", answers={"replace": True, "facts": ["New."]})
    assert replaced["status"] == "written"
    assert "- New." in memory_text(home) and "- Old." not in memory_text(home)


def test_nothing_said_writes_nothing_and_is_walked(home):
    result, outcome = profile_json("--apply", answers={"uses": [], "other": "", "facts": []})

    assert result.exit_code == 0
    assert outcome["status"] == "unchanged" and outcome["text"] is None
    assert not memory_path(home.data_dir).exists()
    assert outcome["walked"]


def test_a_memory_too_long_is_a_problem_and_nothing_is_written(home):
    result, outcome = profile_json("--apply", answers={"facts": ["x" * MAX_MEMORY_CHARS]})

    assert result.exit_code == 1
    # The wizard walks the section after its own length refusal, and so does this.
    assert outcome["status"] == "refused" and outcome["walked"]
    assert "profile" in ConfigStore().walked_sections()
    [problem] = outcome["problems"]
    assert problem["id"] == "facts" and "a call reads" in problem["message"]
    assert not memory_path(home.data_dir).exists()


@pytest.mark.parametrize(
    ("answers", "question"),
    [({"uses": ["gardening"]}, "uses"), ({"uses": "email"}, "uses"),
     ({"replace": "yes"}, "replace"), ({"other": 3}, "other"),
     ({"facts": "Works nights."}, "facts"), ({"facts": [1]}, "facts"),
     ({"shoe_size": 9}, "shoe_size")],
)
def test_an_answer_that_is_not_one_is_named_and_nothing_is_written(home, answers, question):
    result, outcome = profile_json("--apply", answers={**answers, "other": "x"}
                                   if question != "other" else answers)

    assert result.exit_code == 1
    assert [problem["id"] for problem in outcome["problems"]] == [question]
    assert not memory_path(home.data_dir).exists()
    assert not outcome["walked"] and "profile" not in ConfigStore().walked_sections()


@pytest.mark.parametrize("stdin", ["", "[1, 2]", "{not json"])
def test_answers_that_are_not_one_json_object_are_a_wrong_command_line(home, stdin):
    result = runner.invoke(app, ["setup", "profile", "--apply", "--json"], input=stdin)

    assert result.exit_code == 2 and not result.stdout.strip()
    assert "one JSON object on stdin" in result.stderr


def test_answers_are_never_typed_into_a_terminal(home, monkeypatch):
    class Terminal:
        def isatty(self):
            return True

        def read(self, *args):
            raise AssertionError("answers were read from a terminal")

    monkeypatch.setattr(sys, "stdin", Terminal())

    with pytest.raises(typer.Exit) as refused:
        cli.setup_profile(apply=True, preview=False, as_json=True)

    assert refused.value.exit_code == 2
    assert not memory_path(home.data_dir).exists()


def test_apply_and_preview_together_are_a_wrong_command_line(home):
    result = runner.invoke(app, ["setup", "profile", "--apply", "--preview"], input="{}")

    assert result.exit_code == 2


def test_the_service_may_not_write_the_memory(home, monkeypatch):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result, _ = profile_json("--apply", answers={"facts": ["Hi."]})

    assert result.exit_code == 1 and "only the owner" in result.stderr
    assert not memory_path(home.data_dir).exists()


def test_without_json_it_is_said_in_lines(home):
    asked = runner.invoke(app, ["setup", "profile"])
    applied = runner.invoke(app, ["setup", "profile", "--apply"],
                            input=json.dumps({"facts": ["Hi."], "uses": ["x"]}))
    written = runner.invoke(app, ["setup", "profile", "--apply"], input='{"facts": ["Hi."]}')

    assert "uses: What will you mostly use Lyra for?" in asked.output
    assert applied.exit_code == 1 and "uses: Choose from the uses offered." in applied.stderr
    assert written.exit_code == 0 and "- Hi." in written.stdout
