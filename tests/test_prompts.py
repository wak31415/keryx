"""Tests for the packaged prompt templates and their rendering."""

import pytest

from jarvis.prompts import load_prompt, render_voice_prompt


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
        opening_context=None,
    )

    assert "{" not in rendered and "}" not in rendered
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


def test_render_defaults_projects_to_the_configured_ones(settings):
    settings.projects = {"jarvis": "/tmp/jarvis"}

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, authorized=True, opening_context=None
    )

    assert "jarvis" in rendered
