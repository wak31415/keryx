"""Tests for the packaged prompt templates and their rendering."""

import pytest

from jarvis.prompts import load_prompt, render_voice_prompt
from jarvis.skills import Skill


def test_voice_prompt_is_packaged_and_loadable():
    text = load_prompt("voice_system.md")

    assert "Jarvis" in text
    assert "{channel}" in text


def test_unknown_prompt_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist.md")


def test_render_fills_every_placeholder(settings):
    rendered = render_voice_prompt(
        settings,
        channel="phone",
        caller="+491555555555",
        authorized=False,
        projects=["jarvis", "garmin"],
        skills=[Skill(name="mermaid", description="Author Mermaid diagrams.")],
        opening_context=None,
    )

    assert "{" not in rendered and "}" not in rendered
    assert "mermaid: Author Mermaid diagrams." in rendered
    assert "phone" in rendered
    assert "+491555555555" in rendered
    assert "jarvis, garmin" in rendered


def test_render_uses_placeholders_for_an_unknown_caller(settings):
    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, projects=[], opening_context=None
    )

    assert "unknown" in rendered
    assert "none configured" in rendered


def test_render_includes_the_opening_context(settings):
    rendered = render_voice_prompt(
        settings,
        channel="phone",
        caller=None,
        authorized=True,
        projects=[],
        opening_context="Task 7 finished: the report is ready.",
    )

    assert "Task 7 finished: the report is ready." in rendered


def test_render_defaults_projects_to_everything_the_manager_can_resolve(settings, tmp_path):
    """Anything under projects_root dispatches, so the model has to know its name."""
    settings.projects = {"jarvis": "/tmp/jarvis"}
    root = tmp_path / "projects"
    (root / "dinov3rse").mkdir(parents=True)
    settings.projects_root = root

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, opening_context=None
    )

    assert "jarvis" in rendered
    assert "dinov3rse" in rendered


def test_render_lists_the_installed_skills(settings, tmp_path):
    skills = tmp_path / "skills"
    (skills / "wandb-query").mkdir(parents=True)
    (skills / "wandb-query" / "SKILL.md").write_text(
        "---\nname: wandb-query\ndescription: Query W&B runs.\n---\n", encoding="utf-8"
    )
    settings.skills_dir = skills

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, opening_context=None
    )

    assert "wandb-query: Query W&B runs." in rendered


def test_render_says_so_when_no_skills_are_installed(settings, tmp_path):
    settings.skills_dir = tmp_path / "nothing-here"

    rendered = render_voice_prompt(
        settings, channel="phone", caller=None, authorized=False, opening_context=None
    )

    assert "none installed" in rendered


def test_render_includes_a_project_brief(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "vidmem").mkdir(parents=True)
    (root / "vidmem" / ".jarvis-brief.md").write_text(
        "A video model with a memory.", encoding="utf-8"
    )
    settings.projects_root = root

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, opening_context=None
    )

    assert "### vidmem" in rendered
    assert "A video model with a memory." in rendered


def test_render_says_so_when_no_project_wrote_a_brief(settings, tmp_path):
    settings.projects_root = tmp_path / "empty"

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, opening_context=None
    )

    assert "nothing written down yet" in rendered
