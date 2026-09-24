"""The `jarvis-onboard` skill ships in this repository, so a test keeps it true.

It is prose that a Claude Code session at the keyboard follows, which means nothing that
runs would notice if the flag it tells that session to use were renamed, or if the cap it
quotes drifted from the one `projects.py` enforces. These assert the load-bearing half:
that Jarvis's own parser can read it, that it goes through `jarvis init` rather than
writing `memory.md` itself, and that it shows the owner everything before it happens.
"""

import tomllib
from pathlib import Path

import pytest

from jarvis.projects import BRIEF_FILE, MAX_BRIEF_CHARS
from jarvis.skills import discover_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL = SKILLS / "jarvis-onboard" / "SKILL.md"


@pytest.fixture
def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture
def flat(skill_text, unwrapped) -> str:
    return unwrapped(skill_text)


def test_the_skill_parses_the_way_jarvis_parses_every_other_skill():
    """It is installed into `SKILLS_DIR`, where the voice prompt lists what it finds."""
    found = {skill.name: skill.description for skill in discover_skills(SKILLS)}

    assert "jarvis-onboard" in found
    assert "onboard" in found["jarvis-onboard"].lower()


def test_the_skill_goes_through_init_and_never_writes_the_memory_itself(flat):
    """`continuity/memory.py` owns `memory.md`; a skill with a Write tool must not."""
    assert "jarvis init --from - --yes" in flat
    assert "Never write `memory.md` yourself" in flat


def test_the_skill_picks_the_projects_after_the_owner_has_seen_the_list(flat):
    """Scanning every repository under PROJECTS_ROOT is the thing issue #48 forbids."""
    assert "after they have seen the list" in flat
    assert "Never scan a project they did not pick" in flat


def test_the_skill_says_where_a_brief_and_the_memory_end_up(flat):
    """It has to say, in the words it shows them, that this is sent to a third party."""
    assert "sent to the realtime provider" in flat
    assert "on every call" in flat


def test_the_skill_quotes_this_repositorys_own_brief_file_and_cap(flat):
    assert BRIEF_FILE in flat
    assert f"{MAX_BRIEF_CHARS:,} characters" in flat


def test_the_skill_proposes_the_subagents_own_memory_rather_than_editing_it(flat):
    assert "~/.claude/CLAUDE.md" in flat
    assert "Show every edit before you make it" in flat


def test_the_skill_never_touches_the_env_file(flat):
    assert "never edits .env" in flat
    assert "OWNER_NAME=" in flat


def test_the_skill_ships_in_the_sdist():
    """`only-include` is an explicit list: a directory left out of it is not in the tarball."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    included = project["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]

    assert "skills" in included


def test_the_readme_says_how_to_install_the_skill():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "skills/jarvis-onboard" in readme


def test_the_skill_leaves_the_pin_to_them_and_never_invents_one(flat):
    """The one setting the phone can also set, so the skill has to say both ways in.

    A PIN a Claude Code session picked is a PIN in a transcript, and the file it would be
    written to is under `~/.jarvis/`, which this skill may not read at all.
    """
    assert "Never choose a PIN for them" in flat
    assert "JARVIS_PIN=" in flat
    assert "first call" in flat
