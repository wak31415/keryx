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
        projects=["jarvis", "orchard"],
        skills=[Skill(name="mermaid", description="Author Mermaid diagrams.")],
        opening_context=None,
    )

    assert "{" not in rendered and "}" not in rendered
    assert "mermaid: Author Mermaid diagrams." in rendered
    assert "phone" in rendered
    assert "+491555555555" in rendered
    assert "jarvis, orchard" in rendered


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


# --- Slack is opt-in -------------------------------------------------------
#
# Whether a Slack message goes out is the voice model's decision, taken turn by turn, so
# the rule can only live in the prompt. That makes it easy to drop by accident while
# editing the prose around it, and the failure is silent — no test breaks, Jarvis just
# quietly starts messaging him again. These pin the rule to the prompt text instead.


def test_voice_prompt_makes_slack_something_he_has_to_ask_for(unwrapped):
    text = unwrapped(load_prompt("voice_system.md"))

    assert "he has to ask for it first" in text
    assert "Never send unasked" in text


def test_voice_prompt_does_not_send_search_results_unasked(unwrapped):
    """The web_search branch used to end with 'send_to_slack it as well'."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "send_to_slack it as well" not in text
    assert "do not put it on Slack unless he asked for it in writing" in text


def test_voice_prompt_offers_slack_rather_than_sending_it(unwrapped):
    """The escape hatch for something unspeakable is an offer, not a send."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "send it only once he says yes" in text
    assert "never send a written copy of something you have already said" in text


def test_voice_prompt_does_not_promise_slack_as_a_delivery_route(settings, unwrapped):
    """Ending a call used to promise the answer would turn up 'a text, and Slack'."""
    rendered = unwrapped(
        render_voice_prompt(
            settings,
            channel="phone",
            caller=None,
            authorized=True,
            projects=[],
            opening_context=None,
        )
    )

    assert "a text, and Slack" not in rendered


# --- the briefing sections -------------------------------------------------


def _rendered(settings, **kwargs) -> str:
    values = dict(channel="phone", caller=None, authorized=True, projects=[], opening_context=None)
    values.update(kwargs)
    return render_voice_prompt(settings, **values)


def test_a_first_call_has_no_briefing_sections_at_all(settings):
    """An empty heading is something for the model to wonder about; leave it out."""
    rendered = _rendered(settings)

    assert "What he has not heard yet" not in rendered
    assert "What you remember" not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_the_unreported_digest_reaches_the_prompt_under_its_own_heading(settings):
    rendered = _rendered(settings, pending="- task 41 (finished) — he asked for: the ingest script")

    assert "## What he has not heard yet" in rendered
    assert "task 41" in rendered


def test_the_memory_reaches_the_prompt_as_background_not_as_news(settings, unwrapped):
    rendered = _rendered(settings, memory="He is mid-way through the orchard sync.")

    assert "## What you remember" in rendered
    assert "He is mid-way through the orchard sync." in rendered
    assert "Do not read it out" in unwrapped(rendered)


def test_the_prompt_tells_the_model_to_close_the_loop_on_what_it_reported(unwrapped):
    flat = unwrapped(load_prompt("voice_system.md"))

    assert "mark_reported records that you have told him a task finished" in flat
    assert "Call it every time you say a result out loud" in flat


def test_the_prompt_sends_questions_about_the_past_to_recall(unwrapped):
    flat = unwrapped(load_prompt("voice_system.md"))

    assert "recall searches what was said in earlier calls" in flat
    assert "if it comes back empty, say you have nothing on it rather than guessing" in flat


def test_the_memorys_own_headings_are_nested_under_the_section(settings):
    """Otherwise "Standing facts" reads as an instruction to Jarvis, not as what it knows."""
    rendered = _rendered(settings, memory="## Standing facts\n\nHe hates jargon.")

    assert "### Standing facts" in rendered
    assert "\n## Standing facts" not in rendered


def test_the_memory_prompt_is_packaged_and_fully_placeholdered():
    text = load_prompt("memory_update.md")

    assert "{transcript_path}" in text
    assert "{memory_path}" in text
    assert "Never record a PIN" in text
