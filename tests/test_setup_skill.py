"""The `jarvis-setup` skill ships in this repository, so a test keeps it true.

It is prose that a coding-agent session follows, which means nothing that runs would notice
if a command it names were renamed, or if the cap it quotes drifted from the one
`projects.py` enforces. These assert the load-bearing half: that Jarvis's own parser can read
it, that every `jarvis` command it names exists, that secrets stay off the command line, and
that it goes through `jarvis memory seed` rather than writing `memory.md` itself.
"""

import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS
from jarvis.skills import discover_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL = SKILLS / "jarvis-setup" / "SKILL.md"


@pytest.fixture
def flat(unwrapped) -> str:
    return unwrapped(SKILL.read_text(encoding="utf-8"))


def test_the_skill_parses_the_way_jarvis_parses_every_other_skill():
    found = {skill.name: skill.description for skill in discover_skills(SKILLS)}

    assert "jarvis-setup" in found
    assert "set up a new jarvis install" in found["jarvis-setup"].lower()


def test_every_command_the_skill_names_exists():
    commands = set(re.findall(r"jarvis ((?:[a-z-]+)(?: [a-z-]+)?)", SKILL.read_text()))
    for command in sorted(commands):
        words = command.split()
        result = CliRunner().invoke(app, [*words, "--help"])
        if result.exit_code != 0:  # a trailing word that is an argument, not a command
            result = CliRunner().invoke(app, [words[0], "--help"])
        assert result.exit_code == 0, command


def test_the_skill_starts_from_the_machines_own_instructions(flat):
    assert "uv run jarvis setup --agent-instructions" in flat


def test_the_skill_keeps_secrets_off_the_command_line_and_out_of_its_hands(flat):
    assert "A secret never goes on the command line" in flat
    assert "--stdin" in flat and "--from-env VAR" in flat
    assert "do not ask them to paste it to you" in flat
    assert "Never read `~/.jarvis/`" in flat


def test_the_skill_goes_through_memory_seed_and_never_writes_the_memory_itself(flat):
    assert "jarvis memory seed --file - --json" in flat
    assert "Never write `memory.md` yourself" in flat


def test_the_skill_picks_the_projects_after_the_owner_has_seen_the_list(flat):
    assert "only after they have seen the list" in flat
    assert "Never scan a project they did not pick" in flat


def test_the_skill_says_where_the_summaries_and_the_memory_end_up(flat):
    assert "sent to the realtime provider on every call" in flat
    assert "~/.jarvis/projects/<name>.md" in flat
    assert "`.jarvis-brief.md` wins" in flat


def test_the_skill_quotes_this_repositorys_caps(flat):
    assert f"{MAX_BRIEF_CHARS:,} characters" in flat
    assert f"{MAX_BRIEFS_CHARS:,}" in flat


def test_the_skill_leaves_the_pin_to_them_and_hands_over_to_setup(flat):
    assert "Never choose a PIN for them" in flat
    assert "Run `uv run jarvis setup`" in flat
    assert "first call" in flat


def test_the_skill_proposes_each_enabled_agents_own_file(flat):
    assert "~/.claude/CLAUDE.md" in flat and "~/.codex/AGENTS.md" in flat
    assert "Show every edit before you make it" in flat


def test_the_skill_ships_in_the_sdist():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    included = project["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]

    assert "skills" in included


def test_the_readme_says_how_to_hand_setup_to_an_agent():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "jarvis setup --agent-instructions" in readme
    assert "skills/jarvis-setup" in readme
