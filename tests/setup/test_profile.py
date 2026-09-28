"""A first memory: `jarvis memory seed` for an agent, the "About you" section for a person,
and the report both end with — what every call will carry."""

import json
import stat

import pytest
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config import Settings, write_enrolled_pin
from jarvis.continuity.memory import MAX_MEMORY_CHARS, memory_path, read_memory, seed_memory
from jarvis.projects import MAX_BRIEFS_CHARS
from jarvis.setup import profile
from jarvis.setup.profile import PIN_NOTES, facts_from_text, pin_note, setup_report, setup_summary

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
        data_dir=tmp_path / "jarvis",
        projects_root=tmp_path / "projects",
        skills_dir=tmp_path / "skills",
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)
    return settings


def memory_text(settings) -> str:
    return memory_path(settings.data_dir).read_text()


# --- jarvis memory seed ---------------------------------------------------------------


def test_seed_takes_the_facts_on_stdin_and_writes_them_owner_only(home):
    result = runner.invoke(
        app, ["memory", "seed", "--file", "-"], input="# about me\nWorks nights.\n\nLikes brevity\n"
    )

    assert result.exit_code == 0, result.output
    text = memory_text(home)
    assert "# What Jarvis knows about Ada" in text
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
    seed_memory(home.data_dir, owner="Ada", facts=["Old fact."])

    refused = runner.invoke(app, ["memory", "seed", "--file", "-"], input="New fact.\n")
    forced = runner.invoke(app, ["memory", "seed", "--file", "-", "--force"], input="New fact.\n")

    assert refused.exit_code == 1 and "`jarvis memory seed --force` replaces it" in refused.output
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
    (home.projects_root / "orchard" / ".jarvis-brief.md").write_text("Soil sensors.")
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
    assert "jarvis setup" in pin_note(home)
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
    (home.projects_root / "orchard" / ".jarvis-brief.md").write_text("Soil sensors.")
    (home.skills_dir / "mermaid").mkdir(parents=True)
    (home.skills_dir / "mermaid" / "SKILL.md").write_text(
        "---\nname: mermaid\ndescription: Diagrams.\n---\n"
    )
    seed_memory(home.data_dir, owner="Ada", facts=["Works nights."])
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
            ("mostly use Jarvis for", ["email", "coding"]),
            ("Anything else", "reading papers"),
            ("fact", "Works nights."),
            ("fact", ""),
            ("Write it?", True),
        ]
    )

    profile.run_section(ctx)

    text = read_memory(ctx.settings.data_dir)
    assert (
        "Mostly uses Jarvis for email triage, coding tasks in their repositories, reading papers."
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
    seed_memory(ctx.settings.data_dir, owner="Ada", facts=["Old."])

    profile.run_section(ctx)

    assert "- Old." in read_memory(ctx.settings.data_dir)


def test_nothing_said_writes_nothing_and_says_the_first_call_will_introduce_itself(make_ctx):
    ctx = make_ctx([("mostly use", []), ("Anything else", ""), ("fact", "")])

    profile.run_section(ctx)

    assert not memory_path(ctx.settings.data_dir).exists()
    assert any("introduce itself" in line for line in ctx.ui.lines("note"))
