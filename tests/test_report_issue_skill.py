"""The `jarvis-report-issue` skill is prose a subagent follows, so a test keeps it true.

Nothing that runs would notice if the commands it names went away, the templates it points
at moved, or the rules that keep a public issue free of the owner's life were edited out.
"""

import re

import pytest
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config.settings import SOURCE_ROOT
from jarvis.issues import SKILL
from jarvis.skills import BUNDLED_SKILLS, discover_skills

TEXT = (SOURCE_ROOT / SKILL).read_text(encoding="utf-8")
FLAT = " ".join(TEXT.split())


def test_the_skill_parses_the_way_jarvis_parses_every_other_skill():
    found = {skill.name: skill.description for skill in discover_skills(BUNDLED_SKILLS)}

    assert "bug report or a feature request" in found["jarvis-report-issue"].lower()


def test_every_command_it_names_exists():
    for command in sorted(set(re.findall(r"-m jarvis ([a-z-]+)", TEXT))):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0, f"`jarvis {command}` is named in the skill but is gone"


def test_the_templates_it_follows_are_in_the_repository():
    templates = re.findall(r"`(\.github/ISSUE_TEMPLATE/[\w.]+)`", TEXT)

    assert len(templates) == 2
    for template in templates:
        assert (SOURCE_ROOT / template).is_file(), f"{template} is named in the skill but is gone"


def test_the_look_is_short_and_fixes_nothing():
    assert "About **ten tool calls** of looking" in FLAT
    assert "running the tests, reproducing it" in FLAT
    assert "any edit to the checkout" in FLAT


@pytest.mark.parametrize(
    "rule",
    [
        "**No phone numbers**, whole or masked.",
        "**No PIN**",
        "**No key, token, password or secret**",
        "**No names**",
        "**Nothing quoted** from a call",
        "read the whole body once more",
    ],
)
def test_nothing_of_the_owners_goes_in_a_public_issue(rule):
    assert rule in FLAT


def test_a_security_problem_is_never_filed_in_public():
    assert "**Never file one publicly.**" in FLAT
    assert "/security/advisories/new" in FLAT


def test_a_failed_filing_keeps_the_draft_and_posts_nowhere_else():
    assert "do not retry in a loop and do not post any other way" in FLAT
    assert "Put the whole draft in your written report" in FLAT
