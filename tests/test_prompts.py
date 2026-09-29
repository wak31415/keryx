"""Tests for the packaged prompt templates and their rendering."""

import re
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from jarvis.prompts import load_prompt, render_voice_prompt
from jarvis.skills import Skill
from jarvis.trust import TrustLevel

PIN = "123456"


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


@pytest.mark.parametrize(
    ("clock", "pattern"),
    [("24h", r"- Time: \w+ \d{2} \w+ \d{4}, \d{2}:\d{2} "),
     ("12h", r"- Time: \w+ \d{2} \w+ \d{4}, \d{1,2}:\d{2} [AP]M ")],
)
def test_the_clock_is_written_the_way_the_owner_reads_one(settings, clock, pattern):
    settings.clock_format = clock
    rendered = render_voice_prompt(
        settings, channel="phone", caller=None, trust=TrustLevel.FULL, projects=[],
        opening_context=None,
    )

    assert re.search(pattern, rendered)


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
    settings.pin = PIN  # a machine past its first call; the PIN-less one is its own test
    return settings


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_below_full_the_prompt_still_carries_the_map_of_their_world(owners_world, trust):
    """The owner's ruling (2026-09-19): the PIN is the line between reading and acting.

    Whoever has the machine has `.env` and so has the PIN, so gating reads defended only
    against a phone-side spoofer — and charged a keypad to every ordinary call.
    """
    rendered = render_voice_prompt(
        owners_world,
        channel="phone",
        caller="+15550001111",
        trust=trust,
        pending="- task 41 (finished) — their bank balance",
        memory="They are waiting on the letter from the lawyer.",
    )

    for known in ("weather-station", "rain gauge", "orchard", "wandb-query", "lawyer"):
        assert known in rendered, known
    assert "held back until the PIN" not in rendered
    assert "{" not in rendered and "}" not in rendered


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_with_the_briefing_held_back_the_prompt_carries_none_of_it(owners_world, trust):
    """`BRIEFING_BEFORE_PIN=false` restores the older silence, and must keep working."""
    owners_world.briefing_before_pin = False

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
def test_with_no_pin_on_the_machine_nothing_of_theirs_is_rendered(owners_world, trust):
    """The hole `BRIEFING_BEFORE_PIN` leaves on a machine that has never had a PIN.

    The setting trades reads for a keypad entry, which presumes there is a keypad entry to
    make. With no PIN anywhere a call cannot authenticate at all, so a PIN-less install
    would read the memory out to any allowed caller for ever. Withheld until one exists.
    """
    owners_world.pin = None
    assert owners_world.briefing_before_pin is True

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
def test_a_call_that_can_set_the_first_pin_is_told_so_and_told_whose_digits_they_are(
    owners_world, trust, unwrapped
):
    """The model has to know the state exists, or it waits for a PIN nobody has set.

    And it must not fill the silence: suggesting digits, or reading back what it thought
    it heard, would put on the transcript the one thing the keypad path exists to keep off.
    """
    owners_world.pin = None

    rendered = unwrapped(
        render_voice_prompt(owners_world, channel="phone", caller=None, trust=trust)
    )

    assert "There is no PIN on this machine yet, and this call can set one" in rendered
    assert "six to eight digits then hash" in rendered
    assert "key it a second time to confirm" in rendered
    assert "never suggest one, never say one out loud" in rendered
    assert "you have been told almost nothing of theirs" not in rendered


def test_the_full_prompt_is_unmoved_by_there_being_no_pin_yet(owners_world):
    """The local microphone is `FULL` by construction and reads its own machine."""
    owners_world.pin = None

    rendered = render_voice_prompt(
        owners_world, channel="local", caller=None, trust=TrustLevel.FULL, memory="The lawyer."
    )

    for known in ("weather-station", "orchard", "wandb-query", "lawyer"):
        assert known in rendered, known


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_a_withheld_prompt_is_told_it_is_withheld(owners_world, trust):
    """Whatever the model is told has to match what it was handed, in both directions.

    A model told its instructions are complete when they are not says "there is nothing on
    record"; a model told they are incomplete when they are not apologizes for what it is
    already holding.
    """
    owners_world.briefing_before_pin = False
    withheld = render_voice_prompt(owners_world, channel="phone", caller=None, trust=trust)

    owners_world.briefing_before_pin = True
    handed = render_voice_prompt(owners_world, channel="phone", caller=None, trust=trust)

    assert "you have been told almost nothing of theirs" in withheld
    assert "you have been told almost nothing of theirs" not in handed


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
def test_the_digest_is_not_part_of_what_is_held_back(owners_world, trust):
    """News they have not heard is not the map of their world; it is what they asked for."""
    owners_world.briefing_before_pin = False
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


def test_voice_prompt_does_not_send_search_results_unasked(unwrapped):
    """The web_search branch used to end with 'send_to_slack it as well'."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "send_to_slack it as well" not in text
    assert "do not send it anywhere in writing unless they asked for that" in text


def test_the_prompt_names_no_plugin_because_each_describes_itself(settings, unwrapped):
    """A plugin is on some machines and not others; the prompt describing one that is not
    there invites a call to a tool that does not exist. Their descriptions carry it all."""
    from jarvis.plugins import PLUGINS

    text = unwrapped(_rendered(settings))

    assert not [name for name in PLUGINS if name in text]
    assert "that description is all there is" in text
    assert "{" not in text and "}" not in text


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
    # A machine with a PIN, which is every machine past its first call: with none at all
    # the prompt is withheld whatever `BRIEFING_BEFORE_PIN` says, and the tests for that
    # state name it (`test_with_no_pin_yet_...`).
    settings.pin = settings.pin or PIN
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


# --- a follow-up has to earn its turn --------------------------------------
#
# The routing bullets used to forbid a question outright ("do not confirm first", "dispatch
# anyway", "the questions worth asking are the ones Claude works out"). That was written
# against a model that interviewed instead of acting, and it is still right about the
# ordinary dispatch turn — but a ban is not the same thing as a threshold, and the one
# question that changes where the work lands is worth ten seconds. These keep it a
# threshold in both directions: high, and not zero.


def test_the_prompt_keeps_dispatch_as_the_default(unwrapped):
    """The threshold below is not a licence to interview."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "When in doubt, dispatch" in text
    assert "Dispatch first" in text
    assert "They asked for the work, not a conversation about the work" in text
    assert "If they did not name a project, dispatch anyway" in text


def test_the_prompt_makes_a_follow_up_rare_rather_than_forbidden(unwrapped):
    """A question that changes what happens is worth a turn; the rest are not."""
    text = unwrapped(load_prompt("voice_system.md"))

    assert "A follow-up has to earn its turn" in text
    assert "only when the answer changes what actually happens" in text
    assert "roughly one dispatch in ten, not one in two" in text
    assert "If you cannot say what you would do differently with each answer" in text


# --- the first call --------------------------------------------------------
#
# A machine with nothing in `memory.md` has never had a call worth remembering, so the
# first authorized one opens as an introduction instead: what Jarvis is, and a handful of
# questions about them. The absence of the memory is the whole marker — there is no second
# record of "has been onboarded" — so these check both edges of it.


def test_a_first_call_opens_as_an_introduction(settings, unwrapped):
    flat = unwrapped(_rendered(settings))

    assert "## This is the first call" in _rendered(settings)
    assert "Open by saying what you are and what you can do" in flat
    assert "What to call them" in flat
    assert "Which projects matter" in flat
    assert "How they like to be answered" in flat
    assert "What is worth ringing them about" in flat


def test_the_first_call_ends_by_saying_the_shape_of_it_once(unwrapped):
    """The last turn is also the record: what is said out loud is what the memory gets."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert "Say back the shape of it at the end, in one turn" in flat
    assert "Not the list item by item" in flat
    assert "what is said out loud in this call is all that survives it" in flat


def test_the_first_call_spells_nothing_and_confirms_a_name_it_is_unsure_of(unwrapped):
    """Transcription mangles names; a wrong one recorded silently rides every later call."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert "Spell nothing" in flat
    assert "ask if you have it right" in flat


def test_the_first_call_is_dropped_the_moment_it_is_not_wanted(unwrapped):
    """Realtime minutes are paid, and an interview nobody asked for is the worst of them."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert "If they say not now" in flat
    assert "Do not come back to it later in the call" in flat
    assert "that comes first, every time" in flat
    assert "Never make it a condition" in flat


def test_the_first_call_waits_for_a_call_they_made(unwrapped):
    """A call-back about a task is not the moment to ask what they work on."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert 'see "Why this session opened"' in flat
    assert "The introduction waits for a call they made" in flat


def test_anything_remembered_at_all_ends_the_interview(settings):
    """The memory is the only marker: one line of it and the next call is ordinary."""
    rendered = _rendered(settings, memory="They are mid-way through the orchard sync.")

    assert "This is the first call" not in rendered
    assert "## What you remember" in rendered


def test_the_first_call_never_reaches_a_session_that_has_not_given_the_pin(settings):
    """Before the PIN there is no telling whose first call it is.

    Not even on a call Jarvis placed: possession says whose phone answered, not that the
    interview is wanted, and the questions are about them. Unmoved by the 2026-09-19
    widening, which this runs under: being handed the briefing is being told what Jarvis
    knows, and an interview is asking the owner for more.
    """
    assert settings.briefing_before_pin is True
    for trust in (TrustLevel.NONE, TrustLevel.POSSESSION):
        assert "This is the first call" not in _rendered(settings, trust=trust)


def test_the_first_call_prompt_is_packaged_and_renders_whole(settings):
    """It is a prompt file like the others, so editing it needs no restart."""
    text = load_prompt("first_call.md")
    settings.owner_name = "Ada"

    rendered = _rendered(settings)

    assert "{owner}" in text
    assert "You know nothing about Ada beyond what this call tells you" in rendered
    assert "{" not in rendered and "}" not in rendered


def test_a_declined_first_call_does_not_come_back_unless_nothing_was_kept(unwrapped):
    """Declining is written down like anything else, and that is what retires the offer."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert "this will not come back" in flat
    assert "nothing at all was written down, you may offer it once more" in flat


def test_the_first_call_starts_where_the_conversation_already_is(unwrapped):
    """On the phone none of this arrives until the PIN does, which is rarely turn one."""
    flat = unwrapped(load_prompt("first_call.md"))

    assert "told none of this until the PIN is in" in flat
    assert "do not greet them again" in flat
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


# --- the coding agents ------------------------------------------------------


def _render(settings, trust=TrustLevel.FULL, **kw):
    kw.setdefault("skills", [])
    return render_voice_prompt(settings, channel="phone", caller=None, trust=trust, **kw)


def test_one_agent_is_told_nothing_about_a_choice(settings):
    assert "pass agent on the dispatch" not in _render(settings, agents=["claude"])
    assert "pass agent on the dispatch" not in _render(settings)


def test_two_agents_are_named_with_the_default_first(settings, unwrapped):
    rendered = unwrapped(_render(settings, agents=["claude", "codex"]))

    expected = "You can hand work to Claude or Codex; Claude takes it unless they name another."
    assert expected in rendered
    assert "never name an agent for send_followup" in rendered


def test_a_codex_default_is_what_the_prompt_calls_the_back_office(settings, unwrapped):
    settings.agent_backend = "codex"
    memory = "## Standing facts\n\n- They like Claude for writing."

    rendered = unwrapped(_render(settings, memory=memory))

    assert "hand real work to Codex, which runs" in rendered
    assert "Okay, let me check with Codex" in rendered
    assert "hand real work to Claude" not in rendered
    # The approval bridge is Claude Code's, whoever does the dispatched work...
    assert "Sometimes Claude Code, working on their own screen" in rendered
    # ...and what the owner wrote is theirs, never rewritten.
    assert "They like Claude for writing." in rendered


def test_a_codex_default_renames_the_first_call_and_the_trust_note_too(settings, unwrapped):
    settings.agent_backend = "codex"

    first_call = unwrapped(_render(settings))
    before_the_pin = unwrapped(_render(settings, trust=TrustLevel.NONE))

    assert "anything for Codex" in first_call
    assert "handing work to Codex" in before_the_pin


def test_an_older_build_renders_the_template_whole():
    """Prompts are live under whatever build runs, and it blanks a placeholder it does not know.

    So the only placeholder the agents feature adds is a paragraph on a line of its own, and
    the agent's name is substituted in code rather than templated.
    """
    text = load_prompt("voice_system.md")

    assert "{agent}" not in text and "{agent_name}" not in text
    assert [line for line in text.splitlines() if "{agents}" in line] == ["{agents}"]


def test_the_skills_of_every_enabled_agent_are_listed(settings, tmp_path, monkeypatch):
    claude_skills, codex_home = tmp_path / "claude-skills", tmp_path / "codex"
    for root, name in ((claude_skills, "mermaid"), (codex_home / "skills", "review")):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Does {name}.\n---\n", encoding="utf-8"
        )
    settings.skills_dir = claude_skills
    monkeypatch.setattr("jarvis.agents.registry.codex_home", lambda: codex_home)

    alone = render_voice_prompt(settings, channel="phone", caller=None)
    settings.agents_enabled = ["claude", "codex"]
    both = render_voice_prompt(settings, channel="phone", caller=None)

    assert "mermaid: Does mermaid." in alone and "review" not in alone
    assert "mermaid: Does mermaid." in both and "review: Does review." in both


