"""Tests for the packaged prompt templates and their rendering."""

import re
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from jarvis.prompts import load_prompt, render_voice_prompt
from jarvis.skills import Skill
from jarvis.trust import TrustLevel


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
        caller="+15555555555",
        trust=TrustLevel.FULL,
        projects=["jarvis", "orchard"],
        skills=[Skill(name="mermaid", description="Author Mermaid diagrams.")],
        opening_context=None,
    )

    assert "{" not in rendered and "}" not in rendered
    assert "mermaid: Author Mermaid diagrams." in rendered
    assert "phone" in rendered
    assert "+15555555555" in rendered
    assert "jarvis, orchard" in rendered


def test_render_uses_placeholders_for_an_unknown_caller(settings):
    rendered = render_voice_prompt(
        settings,
        channel="local",
        caller=None,
        trust=TrustLevel.FULL,
        projects=[],
        opening_context=None,
    )

    assert "unknown" in rendered
    assert "none configured" in rendered


def test_render_includes_the_opening_context(settings):
    rendered = render_voice_prompt(
        settings,
        channel="phone",
        caller=None,
        trust=TrustLevel.FULL,
        projects=[],
        opening_context="Task 7 finished: the report is ready.",
    )

    assert "Task 7 finished: the report is ready." in rendered


def test_render_defaults_projects_to_everything_the_manager_can_resolve(settings, tmp_path):
    """Anything under projects_root dispatches, so the model has to know its name."""
    settings.projects = {"jarvis": "/tmp/jarvis"}
    root = tmp_path / "projects"
    (root / "weather-station").mkdir(parents=True)
    settings.projects_root = root

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, trust=TrustLevel.FULL, opening_context=None
    )

    assert "jarvis" in rendered
    assert "weather-station" in rendered


def test_render_lists_the_installed_skills(settings, tmp_path):
    skills = tmp_path / "skills"
    (skills / "wandb-query").mkdir(parents=True)
    (skills / "wandb-query" / "SKILL.md").write_text(
        "---\nname: wandb-query\ndescription: Query W&B runs.\n---\n", encoding="utf-8"
    )
    settings.skills_dir = skills

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, trust=TrustLevel.FULL, opening_context=None
    )

    assert "wandb-query: Query W&B runs." in rendered


def test_render_says_so_when_no_skills_are_installed(settings, tmp_path):
    settings.skills_dir = tmp_path / "nothing-here"

    rendered = render_voice_prompt(
        settings, channel="phone", caller=None, trust=TrustLevel.FULL, opening_context=None
    )

    assert "none installed" in rendered


def test_render_includes_a_project_brief(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "orchard-sensor-net").mkdir(parents=True)
    (root / "orchard-sensor-net" / ".jarvis-brief.md").write_text(
        "Soil sensors in an orchard.", encoding="utf-8"
    )
    settings.projects_root = root

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, trust=TrustLevel.FULL, opening_context=None
    )

    assert "### orchard-sensor-net" in rendered
    assert "Soil sensors in an orchard." in rendered


def test_render_says_so_when_no_project_wrote_a_brief(settings, tmp_path):
    settings.projects_root = tmp_path / "empty"

    rendered = render_voice_prompt(
        settings, channel="local", caller=None, trust=TrustLevel.FULL, opening_context=None
    )

    assert "nothing written down yet" in rendered


@pytest.fixture
def owners_world(settings, tmp_path):
    """A machine with projects, briefs and skills on it — the map of the owner's world."""
    root = tmp_path / "projects"
    (root / "weather-station").mkdir(parents=True)
    (root / "weather-station" / ".jarvis-brief.md").write_text("A rain gauge on the roof.")
    skills = tmp_path / "skills"
    (skills / "wandb-query").mkdir(parents=True)
    (skills / "wandb-query" / "SKILL.md").write_text(
        "---\nname: wandb-query\ndescription: Query W&B runs.\n---\n", encoding="utf-8"
    )
    settings.projects, settings.projects_root, settings.skills_dir = {"orchard": "/x"}, root, skills
    return settings


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_below_full_the_prompt_carries_no_map_of_their_world(owners_world, trust):
    """The projects, the briefs, the skills and the memory are what the PIN still buys."""
    rendered = render_voice_prompt(
        owners_world,
        channel="phone",
        caller="+15550001111",
        trust=trust,
        pending="- task 41 (finished) — their bank balance",
        memory="They are waiting on the letter from the lawyer.",
    )

    for secret in ("weather-station", "rain gauge", "orchard", "wandb-query", "lawyer"):
        assert secret not in rendered, secret
    assert "held back until the PIN" in rendered
    assert "{" not in rendered and "}" not in rendered


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_the_digest_is_not_part_of_what_is_held_back(owners_world, trust):
    """News they have not heard is not the map of their world; it is what they asked for."""
    rendered = render_voice_prompt(
        owners_world,
        channel="phone",
        caller="+15550001111",
        trust=trust,
        pending="- task 41 (finished) — their bank balance",
    )

    assert "## What the owner has not heard yet" in rendered
    assert "task 41" in rendered


# --- Slack is opt-in -------------------------------------------------------
#
# Whether a Slack message goes out is the voice model's decision, taken turn by turn, so
# the rule can only live in the prompt. That makes it easy to drop by accident while
# editing the prose around it, and the failure is silent — no test breaks, Jarvis just
# quietly starts messaging them again. These pin the rule to the prompt text instead.


def test_voice_prompt_makes_slack_something_they_have_to_ask_for(unwrapped):
    text = unwrapped(load_prompt("voice_system.md"))

    assert "they have to ask for it first" in text
    assert "Never send unasked" in text


def test_voice_prompt_does_not_send_search_results_unasked(unwrapped):
    """The web_search branch used to end with 'send_to_slack it as well'."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "send_to_slack it as well" not in text
    assert "do not put it on Slack unless they asked for it in writing" in text


def test_voice_prompt_offers_slack_rather_than_sending_it(unwrapped):
    """The escape hatch for something unspeakable is an offer, not a send."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "send it only once they say yes" in text
    assert "never send a written copy of something you have already said" in text


def test_voice_prompt_does_not_promise_slack_as_a_delivery_route(settings, unwrapped):
    """Ending a call used to promise the answer would turn up 'a text, and Slack'."""
    rendered = unwrapped(
        render_voice_prompt(
            settings,
            channel="phone",
            caller=None,
            trust=TrustLevel.FULL,
            projects=[],
            opening_context=None,
        )
    )

    assert "a text, and Slack" not in rendered


# --- the briefing sections -------------------------------------------------


def _rendered(settings, **kwargs) -> str:
    values = dict(
        channel="phone", caller=None, trust=TrustLevel.FULL, projects=[], opening_context=None
    )
    values.update(kwargs)
    return render_voice_prompt(settings, **values)


def test_a_first_call_has_no_digest_and_says_it_remembers_nothing(settings, unwrapped):
    """An empty heading is something for the model to wonder about; leave the digest out.

    The memory is the exception. With nothing in it the model used to be told nothing at
    all, and a model told nothing about whom it is talking to fills the gap with a warm,
    invented familiarity. So a trusted session with no memory is told so, in one line.
    """
    rendered = _rendered(settings)

    assert "What the owner has not heard yet" not in rendered
    assert "## What you remember" in rendered
    assert "You know nothing about the owner beyond what this call tells you" in unwrapped(
        rendered
    )
    assert "{" not in rendered and "}" not in rendered


def test_an_empty_memory_is_only_admitted_to_a_trusted_session(settings):
    """Before the PIN, "nothing remembered" would be a claim about the owner's memory."""
    none = _rendered(settings, trust=TrustLevel.NONE)
    possession = _rendered(settings, trust=TrustLevel.POSSESSION)
    remembered = _rendered(settings, memory="Works nights.")

    for rendered in (none, possession, remembered):
        assert "You know nothing about" not in rendered


def test_the_unreported_digest_reaches_the_prompt_under_its_own_heading(settings):
    rendered = _rendered(
        settings, pending="- task 41 (finished) — they asked for: the ingest script"
    )

    assert "## What the owner has not heard yet" in rendered
    assert "task 41" in rendered


def test_the_memory_reaches_the_prompt_as_background_not_as_news(settings, unwrapped):
    rendered = _rendered(settings, memory="They are mid-way through the orchard sync.")

    assert "## What you remember" in rendered
    assert "They are mid-way through the orchard sync." in rendered
    assert "Do not read it out" in unwrapped(rendered)


def test_the_prompt_tells_the_model_to_close_the_loop_on_what_it_reported(unwrapped):
    flat = unwrapped(load_prompt("voice_system.md"))

    assert "mark_reported records that you have told them a task finished" in flat
    assert "Call it every time you say a result out loud" in flat


def test_the_prompt_sends_questions_about_the_past_to_recall(unwrapped):
    flat = unwrapped(load_prompt("voice_system.md"))

    assert "recall searches what was said in earlier calls" in flat
    assert "if it comes back empty, say you have nothing on it rather than guessing" in flat


def test_the_cluster_paragraph_is_only_there_when_the_tool_is(settings):
    """A machine with no clusters configured has no `cluster_stats`; describing one anyway is
    an invitation to call a tool that does not exist."""
    without = _rendered(settings)
    other_tools = _rendered(settings, tool_names=["web_search", "dispatch_task"])
    with_it = _rendered(settings, tool_names=["web_search", "cluster_stats"])

    assert "cluster_stats" not in without
    assert "cluster_stats" not in other_tools
    assert "- cluster_stats is what" in with_it
    assert "{" not in with_it and "}" not in with_it


def test_the_memorys_own_headings_are_nested_under_the_section(settings):
    """Otherwise "Standing facts" reads as an instruction to Jarvis, not as what it knows."""
    rendered = _rendered(settings, memory="## Standing facts\n\nThey hate jargon.")

    assert "### Standing facts" in rendered
    assert "\n## Standing facts" not in rendered


def test_the_memory_prompt_is_packaged_and_fully_placeholdered():
    text = load_prompt("memory_update.md")

    assert "{transcript_path}" in text
    assert "{memory_path}" in text
    assert "{structure}" in text
    assert "## Standing facts" not in text  # continuity.memory.memory_skeleton owns those
    assert "Never record a PIN" in text


# --- saying it once --------------------------------------------------------
#
# A call on 2026-09-16 spent four spoken turns on one PIN and two more on a call-back that
# takes milliseconds to arrange: "let me set that up for you", then "all set". They noticed,
# on the phone, and asked for it to stop. None of these rules can live anywhere but the
# prompt, and all of them are a sentence somebody could tidy away while editing the prose
# around them.


def test_the_prompt_forbids_announcing_and_then_confirming(unwrapped):
    """One action, one sentence — the rule the call-back kept breaking."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "Say a thing once" in text
    assert "never both" in text


def test_the_prompt_names_the_tools_that_are_too_fast_to_announce(unwrapped):
    """"One moment" before a millisecond call is latency they pay for nothing."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert 'Say "one moment" only before something that will really keep them waiting' in text
    for name in ("request_callback", "mark_reported", "submit_pin", "end_session"):
        assert name in text


def test_the_prompt_does_not_have_them_justify_what_they_already_agreed_to(unwrapped):
    """"so you don't have to wait on the line" was said back to them three calls running."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "Do not justify what they have just agreed to" in text


def test_the_prompt_never_predicts_the_pin(unwrapped):
    """"That'll need your PIN" spends a turn on something the tool will say itself."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "Never predict it" in text
    assert "Do not tell them in advance that something will need the PIN" in text


def test_the_prompt_treats_an_accepted_pin_as_nothing_to_say(unwrapped):
    """The task number is the proof it worked; "you're authorized now" is a spare turn."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "When it is accepted, say nothing about it" in text


def test_the_prompt_asks_for_the_callback_to_be_confirmed_once(unwrapped):
    text = unwrapped(load_prompt("voice_system.md"))

    assert 'not "let me set that up" and then "all set"' in text


def test_the_prompt_does_not_let_it_promise_what_a_result_will_contain(unwrapped):
    """"It'll include a short summary and where to find the deck" was invented whole."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "because you do not know yet" in text
    assert "Never invent facts, results or progress" in text


# --- nobody's name is built in ---------------------------------------------
#
# Jarvis was written for one person, and their name was in the first line of the voice prompt,
# the subagent suffix, the memory's title and a tool description. Anyone else who installed
# it got an assistant that believed it worked for them. The name is `OWNER_NAME` now, and
# these keep it from coming back by the easy route of an edit to the prose.

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "jarvis"


def _author_first_name() -> str:
    """The first name of the package's author: the name most likely to creep back in."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["authors"][0]["name"].split()[0]


def test_the_voice_prompt_says_whom_jarvis_works_for(settings):
    settings.owner_name = "Ada"

    rendered = _rendered(settings)

    assert "You are Jarvis, Ada's personal assistant." in rendered


def test_without_a_name_the_voice_prompt_works_for_the_owner(settings):
    rendered = _rendered(settings)

    assert "You are Jarvis, the owner's personal assistant." in rendered
    assert "{" not in rendered and "}" not in rendered


def test_no_packaged_prompt_or_source_names_the_author():
    """Not in a prompt, a string constant, a docstring or a comment under `src/jarvis`."""
    name = re.compile(rf"\b{re.escape(_author_first_name())}\b", re.IGNORECASE)
    files = sorted([*PACKAGE.rglob("*.py"), *PACKAGE.rglob("*.md")])

    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if name.search(line)
    ]

    assert files and not offenders, offenders


# --- what the prompt promises has to be true on this machine ----------------


def test_the_clock_carries_the_time_zone(settings):
    """On a server running in UTC, "14:00" alone is an hour the owner is not living in."""
    zone = datetime.now().astimezone().strftime("%Z")

    time_line = next(line for line in _rendered(settings).splitlines() if "- Time:" in line)

    assert zone and time_line.endswith(f" {zone}")


def test_the_cluster_is_only_a_slow_thing_when_there_is_a_cluster_tool(settings, unwrapped):
    """Naming the cluster as a wait, or as PIN-free, invites a call to a tool that is not
    there."""
    without = unwrapped(_rendered(settings))
    with_it = unwrapped(_rendered(settings, tool_names=["cluster_stats"]))

    assert "the cluster" not in without
    assert "a dispatch, a search, the bill" in without
    assert "Only the bill, a web search and hanging up do not" in without
    assert "a dispatch, a search, the cluster, the bill" in with_it
    assert "Only the bill, the cluster, a web search and hanging up do not" in with_it


def test_without_texting_the_prompt_promises_no_text(settings, unwrapped):
    """`SMS_ENABLED` is off by default, and a promised text that never comes is a result
    the owner never hears about."""
    rendered = unwrapped(_rendered(settings))

    assert "a text" not in rendered
    assert "get a call saying so instead" in rendered
    assert "will turn up instead — at the top of the next call —" in rendered


def test_with_texting_on_the_prompt_says_a_text(settings, unwrapped):
    settings.twilio_account_sid, settings.twilio_auth_token = "AC1", "token"
    settings.twilio_number, settings.sms_enabled = "+15550000000", True

    rendered = unwrapped(_rendered(settings))

    assert "get a text saying so instead" in rendered
    assert "will turn up instead — a text —" in rendered


# --- what the prompt says about this call's level ---------------------------
#
# The model cannot read `jarvis/trust.py`, so the only thing that tells it what it may do
# is these three paragraphs. A level whose note goes missing is a model guessing, and it
# guesses generously.


def test_every_level_says_what_it_is_and_what_it_may_do(settings, unwrapped):
    for trust in TrustLevel:
        rendered = unwrapped(_rendered(settings, trust=trust))

        assert "How much this call has proved:" in rendered
        assert "{" not in rendered and "}" not in rendered


def test_an_inbound_call_is_told_it_has_proved_nothing(settings, unwrapped):
    rendered = unwrapped(_rendered(settings, trust=TrustLevel.NONE))

    assert "caller id can be faked" in rendered
    assert "asking for the PIN" in rendered


def test_a_call_jarvis_placed_is_told_what_the_key_is_for(settings, unwrapped):
    """The voicemail rule only works if the model knows to ask for the key."""
    rendered = unwrapped(_rendered(settings, trust=TrustLevel.POSSESSION))

    assert "you rang them, on their own number" in rendered
    assert "ask them to press one key" in rendered
    assert "not as a greeting" in rendered
    assert "still need the PIN" in rendered


def test_a_full_call_is_told_to_say_nothing_about_the_pin(settings, unwrapped):
    rendered = unwrapped(_rendered(settings, trust=TrustLevel.FULL))

    assert "Everything is open to you" in rendered
    assert "Say nothing about the PIN" in rendered
