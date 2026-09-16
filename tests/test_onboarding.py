"""Tests for `jarvis init`, driven through the command line with typed input.

Every test runs in a directory with a `.env` in it, because the one thing `init` must never
do is edit that file: it prints the `OWNER_NAME=` line and leaves the writing to a person.
"""

import stat

import pytest
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config import Settings
from jarvis.continuity.memory import MAX_MEMORY_CHARS, memory_path, seed_memory
from jarvis.onboarding import facts_from_text, setup_report

runner = CliRunner()

ENV_TEXT = "OPENAI_API_KEY=sk-test\n# nothing else\n"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A hermetic install: a `.env` in the working directory, and nothing of the machine's."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(ENV_TEXT)
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        projects_root=tmp_path / "projects",
        skills_dir=tmp_path / "skills",
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)
    return settings


def memory_text(settings: Settings) -> str:
    return memory_path(settings.data_dir).read_text(encoding="utf-8")


def env_untouched(tmp_path) -> bool:
    return (tmp_path / ".env").read_text() == ENV_TEXT


# --- asked at the keyboard ---------------------------------------------------


def test_init_asks_for_a_name_and_facts_shows_the_memory_and_writes_it(home, tmp_path):
    typed = "Ada\nPrefers short answers.\nWorks nights.\n\ny\n"

    result = runner.invoke(app, ["init"], input=typed)

    assert result.exit_code == 0, result.output
    path = memory_path(home.data_dir)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert memory_text(home).startswith("# What Jarvis knows about Ada\n\n## Standing facts\n")
    assert "- Prefers short answers.\n- Works nights.\n" in memory_text(home)
    assert "# What Jarvis knows about Ada" in result.output  # shown before it was written
    assert "sent to the realtime provider" in result.output
    assert "OWNER_NAME=Ada" in result.output
    assert env_untouched(tmp_path)


def test_saying_no_writes_nothing(home, tmp_path):
    result = runner.invoke(app, ["init"], input="Ada\nPrefers short answers.\n\nn\n")

    assert result.exit_code == 0, result.output
    assert "nothing written" in result.output
    assert not memory_path(home.data_dir).exists()
    assert env_untouched(tmp_path)


def test_a_blank_name_is_the_owner_and_asks_for_no_env_line(home):
    result = runner.invoke(app, ["init"], input="\nPrefers short answers.\n\ny\n")

    assert result.exit_code == 0, result.output
    assert memory_text(home).startswith("# What Jarvis knows about the owner\n")
    assert "OWNER_NAME=" not in result.output


def test_a_configured_owner_name_is_not_asked_for_again(home):
    home.owner_name = "Ada"

    result = runner.invoke(app, ["init"], input="Prefers short answers.\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "what should Jarvis call you" not in result.output
    assert "OWNER_NAME=" not in result.output
    assert memory_text(home).startswith("# What Jarvis knows about Ada\n")


def test_no_facts_writes_nothing(home):
    result = runner.invoke(app, ["init"], input="Ada\n\n")

    assert result.exit_code == 0, result.output
    assert "nothing to write down" in result.output
    assert not memory_path(home.data_dir).exists()


# --- given on the command line ----------------------------------------------


def test_init_takes_everything_from_flags_and_asks_nothing(home, tmp_path):
    result = runner.invoke(
        app,
        [
            "init",
            *("--name", "Ada Quill"),
            *("--fact", "Prefers short answers.", "--fact", "Works nights."),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "- Works nights." in memory_text(home)
    assert "OWNER_NAME='Ada Quill'" in result.output  # quoted, so the space survives
    assert env_untouched(tmp_path)


def test_init_reads_the_facts_from_a_file(home, tmp_path):
    facts = tmp_path / "about-me.md"
    facts.write_text("# About me\n\n- Prefers short answers.\n\n* Works nights.\n")

    result = runner.invoke(app, ["init", "--name", "Ada", "--from", str(facts), "--yes"])

    assert result.exit_code == 0, result.output
    assert "- Prefers short answers.\n- Works nights.\n" in memory_text(home)
    assert "About me" not in memory_text(home)


def test_init_reads_the_facts_from_stdin(home):
    result = runner.invoke(
        app, ["init", "--name", "Ada", "--from", "-", "--yes"], input="Works nights.\n"
    )

    assert result.exit_code == 0, result.output
    assert "- Works nights." in memory_text(home)


def test_facts_on_stdin_need_yes_because_nothing_is_left_to_answer_with(home):
    result = runner.invoke(app, ["init", "--from", "-"], input="Works nights.\n")

    assert result.exit_code == 2
    assert "add --yes" in result.output
    assert not memory_path(home.data_dir).exists()


def test_an_unreadable_facts_file_says_so(home, tmp_path):
    result = runner.invoke(app, ["init", "--from", str(tmp_path / "missing.md"), "--yes"])

    assert result.exit_code == 2
    assert "could not read" in result.output


def test_yes_with_nothing_given_writes_nothing(home):
    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    assert not memory_path(home.data_dir).exists()


# --- what is already there --------------------------------------------------


def test_init_never_replaces_a_memory_without_force(home):
    seed_memory(home.data_dir, owner="Ada", facts=["written by a call"])

    result = runner.invoke(app, ["init", "--name", "Ada", "--fact", "a first draft", "--yes"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert "written by a call" in memory_text(home)
    assert "projects root:" in result.output  # the report still comes


def test_force_replaces_it(home):
    seed_memory(home.data_dir, owner="Ada", facts=["written by a call"])

    result = runner.invoke(
        app, ["init", "--name", "Ada", "--fact", "a first draft", "--force", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "a first draft" in memory_text(home)
    assert "written by a call" not in memory_text(home)


def test_a_memory_too_long_for_a_call_is_refused_before_it_is_shown(home):
    result = runner.invoke(
        app, ["init", "--name", "Ada", "--fact", "x" * MAX_MEMORY_CHARS, "--yes"]
    )

    assert result.exit_code == 1
    assert str(MAX_MEMORY_CHARS) in result.output
    assert not memory_path(home.data_dir).exists()


def test_init_is_listed_in_the_help():
    result = runner.invoke(app, ["init", "--help"])

    assert result.exit_code == 0
    for switch in ("--name", "--fact", "--from", "--force", "--yes"):
        assert switch in result.output


# --- the report -------------------------------------------------------------


def test_the_report_says_the_projects_root_is_missing(home):
    lines = setup_report(home)

    assert f"projects root: {home.projects_root} does not exist" in lines[0]
    assert "PROJECTS_ROOT" in lines[0]


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

    assert f"projects root: {home.projects_root}\n" in report
    assert "projects: 2, and 1 with a .jarvis-brief.md: orchard" in report
    assert f"{len('Soil sensors.') + memory_chars} characters" in report
    assert "skills: 1" in report
    assert "~/.claude/CLAUDE.md" in report


def test_facts_skip_blank_lines_and_headings():
    assert facts_from_text("# Me\n\n  one  \n\n## More\ntwo\n") == ["one", "two"]
