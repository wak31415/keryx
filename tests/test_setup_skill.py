"""The `keryx-setup` skill ships in this repository, so a test keeps it true.

It is prose that a coding-agent session follows, which means nothing that runs would notice
if a command it names were renamed, or if the cap it quotes drifted from the one
`projects.py` enforces. These assert the load-bearing half: that Keryx's own parser can read
it, that every `keryx` command it names exists, that secrets stay off the command line, and
that it goes through `keryx memory seed` rather than writing `memory.md` itself.
"""

import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keryx.cli import app
from keryx.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS
from keryx.skills import discover_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL = SKILLS / "keryx-setup" / "SKILL.md"


@pytest.fixture
def flat(unwrapped) -> str:
    return unwrapped(SKILL.read_text(encoding="utf-8"))


def test_the_skill_parses_the_way_keryx_parses_every_other_skill():
    found = {skill.name: skill.description for skill in discover_skills(SKILLS)}

    assert "keryx-setup" in found
    assert "set up a new keryx install" in found["keryx-setup"].lower()


def test_every_command_the_skill_names_exists():
    commands = set(re.findall(r"keryx ((?:[a-z-]+)(?: [a-z-]+)?)", SKILL.read_text()))
    for command in sorted(commands):
        words = command.split()
        result = CliRunner().invoke(app, [*words, "--help"])
        if result.exit_code != 0:  # a trailing word that is an argument, not a command
            result = CliRunner().invoke(app, [words[0], "--help"])
        assert result.exit_code == 0, command


def test_the_skill_starts_from_the_machines_own_instructions(flat):
    assert "keryx setup --agent-instructions" in flat


def test_the_skill_keeps_secrets_off_the_command_line_and_out_of_its_hands(flat):
    assert "A secret never goes on the command line" in flat
    assert "--stdin" in flat and "--from-env VAR" in flat
    assert "do not ask them to paste it to you" in flat
    assert "Never read Keryx's own directories" in flat
    assert "`~/.config/keryx/`" in flat and "`~/.local/share/keryx/`" in flat


def test_the_skill_goes_through_memory_seed_and_never_writes_the_memory_itself(flat):
    assert "keryx memory seed --file - --json" in flat
    assert "Never write `memory.md` yourself" in flat


def test_the_skill_picks_the_projects_after_the_owner_has_seen_the_list(flat):
    assert "only after they have seen the list" in flat
    assert "Never scan a project they did not pick" in flat


def test_the_skill_says_where_the_summaries_and_the_memory_end_up(flat):
    assert "sent to the realtime provider on every call" in flat
    assert "~/.local/share/keryx/projects/<name>.md" in flat
    assert "`.keryx-brief.md` wins" in flat


def test_the_skill_quotes_this_repositorys_caps(flat):
    assert f"{MAX_BRIEF_CHARS:,} characters" in flat
    assert f"{MAX_BRIEFS_CHARS:,}" in flat


def test_the_skill_connects_slack_without_ever_holding_its_token(flat):
    assert "keryx auth login slack --stdin --allow none # theirs to run" in flat
    assert "--email" not in flat, "no address is asked for: the user token says who they are"
    assert "their own user token and the bot token" in flat
    # The places are said as Slack's console lays them out, and in the order it shows them.
    assert "`Basic Information > App-Level Tokens > Generate Token and Scopes`" in flat
    assert ("`OAuth & Permissions > OAuth Tokens` then lists the User OAuth Token "
            "(`xoxp-…`) and, below it, the Bot User OAuth Token (`xoxb-…`)") in flat
    assert "Do not ask them to paste a token to you" in flat
    assert "ask them whether it arrived" in flat


def test_the_skill_leaves_what_keryx_may_do_in_slack_to_them(flat):
    """With requests a Slack message can start work with no PIN, with read their messages
    are read and with send Keryx posts in their name: never the agent's call."""
    assert "What Keryx may do in their Slack is theirs to choose, and it comes first" in flat
    assert "Never choose `requests`, `read` or `send` for them" in flat
    assert "without the phone PIN" in flat
    assert "`--allow none` is reports and files alone" in flat
    assert "Nothing is guessed" in flat
    # The level it replaced is named nowhere, and neither is its option.
    for old in ("--access", "`announce`", "`converse`", "`write`", "access level"):
        assert old not in flat


def test_neither_tells_an_agent_to_pass_allow_none_to_a_workspace_that_is_connected(flat):
    """`--allow` says everything that stays on, and what it leaves out is switched off at
    once: `none` as the answer to no answer is for a workspace that is not connected.
    On one that is, an agent passes what is already on, read from `auth status`."""
    source = (ROOT / "src" / "keryx" / "setup" / "agent_instructions.md").read_text("utf-8")
    instructions = " ".join(source.split())

    assert ("what to use when they give no answer **on a workspace that is not connected "
            "yet**") in flat
    assert "never pass `--allow none` by default" in flat
    assert ("what to use when they give no answer, **but only where Slack is not connected "
            "yet**") in instructions
    assert "never `none` by default" in instructions
    for text in (flat, instructions):
        assert text.count("no answer") == 1  # said once, and only with that condition
        assert "`keryx auth status --json` (`slack.workspaces[].allow`)" in text
        assert "unless they asked you for a change" in text


def test_what_auth_status_says_is_ticked_is_where_the_instructions_send_an_agent(settings):
    """`slack.workspaces[].allow`, in the words `--allow` takes."""
    from keryx import plugins
    from keryx.config.store import ConfigStore
    from keryx.setup import auth

    settings = settings.model_copy(update={"slack_bot_token": "xoxb-1",
                                           "slack_app_token": "xapp-1"})
    plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "owner_id": "U0OWNER", "allow": ["requests"],
        "team_id": "T0ACME", "bot_user_id": "U0KERYX"})
    plugins.install(settings, "slack_report")

    [workspace] = auth.status(settings, ConfigStore())["slack"]["workspaces"]

    assert workspace["allow"] == ["requests"] and workspace["missing"] == []


def test_the_agent_instructions_say_the_same_and_name_no_level():
    source = (ROOT / "src" / "keryx" / "setup" / "agent_instructions.md").read_text("utf-8")
    flat = " ".join(source.split())
    assert "Never choose one for them" in flat and "--allow none" in flat
    for old in ("--access", "`announce`", "`converse`", "`write`", "access level"):
        assert old not in flat


def test_the_skill_leaves_the_pin_to_them_and_hands_over_to_setup(flat):
    assert "Never choose a PIN for them" in flat
    assert "Run `keryx setup`" in flat
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

    assert "keryx setup --agent-instructions" in readme
    assert "skills/keryx-setup" in readme
