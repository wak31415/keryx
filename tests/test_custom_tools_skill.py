"""The `jarvis-custom-tools` skill is prose a subagent follows, so a test keeps it true.

Nothing that runs would notice if the loader's contract drifted from what the skill tells a
subagent to write. These assert the load-bearing half: its example loads as a tool, the
gates and the timeout it quotes are the loader's, and every command it names exists.
"""

import re

from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.skills import BUNDLED_SKILLS, CUSTOM_TOOLS_SKILL, discover_skills
from jarvis.tools.custom import DEFAULT_TIMEOUT_S, CustomTool, load_custom_tools

TEXT = CUSTOM_TOOLS_SKILL.read_text(encoding="utf-8")


def test_the_skill_parses_the_way_jarvis_parses_every_other_skill():
    found = {skill.name: skill.description for skill in discover_skills(BUNDLED_SKILLS)}

    assert "voice tools" in found["jarvis-custom-tools"].lower()


def test_its_example_is_a_tool_the_loader_accepts(tmp_path):
    (example,) = re.findall(r"```python\n(.*?)```", TEXT, re.S)
    path = tmp_path / "tides.py"
    path.write_text(example, encoding="utf-8")
    path.chmod(0o600)
    tmp_path.chmod(0o700)

    result = load_custom_tools(tmp_path)

    assert result.errors == []
    ((_, tool),) = result.tools
    assert tool.name == "tides" and tool.needs_pin is False


def test_the_gate_and_the_timeout_it_quotes_are_the_loaders():
    assert "`needs_pin=True`** (the default)" in TEXT
    assert CustomTool.__dataclass_fields__["needs_pin"].default is True
    assert f"default {DEFAULT_TIMEOUT_S:g}" in TEXT


def test_every_command_it_names_exists():
    for command in sorted(set(re.findall(r"-m jarvis ([a-z-]+)", TEXT))):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0, f"`jarvis {command}` is named in the skill but is gone"


def test_it_keeps_the_tools_out_of_the_repository_and_off_restarts():
    assert "Never put one in the Jarvis repository, and never commit one." in TEXT
    assert "Do not write a `RESTART_REQUIRED:` line for it." in TEXT
