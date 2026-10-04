"""`keryx setup` as a whole: what it says is left, what it walks, and what it asks a second
time (nothing, unless reviewing)."""

import pytest

from keryx.agents import registry
from keryx.agents.registry import BACKENDS
from keryx.config.store import ConfigStore
from keryx.setup import wizard
from keryx.setup.ui import Aborted, Back
from keryx.setup.wizard import DONE, FAILED, MISSING, pending, run_wizard, statuses

from .fakes import DEFAULT


@pytest.fixture
def claude_signed_in(monkeypatch):
    import dataclasses

    spec = BACKENDS["claude"]
    monkeypatch.setitem(
        BACKENDS,
        "claude",
        dataclasses.replace(spec, auth=dataclasses.replace(spec.auth, stored_login=lambda: True)),
    )
    monkeypatch.setattr("shutil.which", lambda name: None)


FIRST_RUN = [
    ("What next?", "left"),
    # Voice
    ("How should calls be voiced?", "openai"),
    ("OpenAI API key", "sk-live"),
    # Coding agents: Claude is installed and signed in already, so nothing is asked
    # Local models: offered once, and left alone
    ("local agent's model run", "none"),
    ("voice on a call", "openai"),
    # Settings
    ("sensible default", "recommended"),
    # Owner and PIN
    ("assistant be called", "Jarvis"),
    ("call you", "Ada"),
    ("mobile numbers", "+15551234567"),
    ("Set the PIN", "now"),
    ("New PIN", "482915"),
    ("same PIN again", "482915"),
    # Phone and plugins left for later (Google is done: Claude's connectors carry it)
    ("Set up phone calls", "skip"),
    ("Which plugins", []),
    # Issue reports: on, and gh (the fake) is already signed in
    ("bug reports and feature requests", True),
    # About you
    ("mostly use Jarvis for", ["coding"]),
    ("Anything else", ""),
    ("fact", "Works nights."),
    ("fact", ""),
    ("Write it?", True),
    # Project context: not now
    ("explore your projects", "no"),
]


def test_a_first_run_walks_everything_and_a_second_asks_nothing(make_ctx, claude_signed_in):
    ctx = make_ctx(FIRST_RUN)

    code = run_wizard(ctx)

    assert ctx.ui.done(), ctx.ui.answers
    assert code == 0, ctx.ui.lines("panel")[-1]
    settings = ctx.refresh()
    assert settings.openai_api_key == "sk-live"
    assert settings.issue_reporting is True
    assert settings.owner_name == "Ada"
    assert settings.assistant_name == "Jarvis"
    assert settings.pin_source == "enrolled"
    [outro] = ctx.ui.lines("outro")
    assert "the phone is how you talk to Jarvis" in outro

    again = make_ctx([("Everything is set up", "exit")])
    assert run_wizard(again) == 0
    assert again.ui.done()
    assert again.ui.lines("section") == ["Overview"]  # and no section walked


def test_esc_goes_back_a_question_and_the_key_is_not_asked_for_twice(
    make_ctx, claude_signed_in, world
):
    ctx = make_ctx(
        [
            ("What next?", Back()),  # nothing before it: asked again
            ("What next?", "left"),
            ("How should calls be voiced?", "openai"),
            ("OpenAI API key", "sk-live"),
            ("local agent's model run", Back()),  # back into Voice: the key is typed again
            ("OpenAI API key", "sk-other"),
            ("local agent's model run", "none"),
            ("voice on a call", "openai"),
            ("sensible default", "recommended"),
            ("assistant be called", "Lyra"),
            ("call you", "Ada"),
            ("mobile numbers", Back()),  # the name again, offered as it was answered
            ("call you", DEFAULT),
            ("mobile numbers", "+15551234567"),
            ("Set the PIN", Aborted()),
        ]
    )

    with pytest.raises(Aborted):
        run_wizard(ctx)

    assert ctx.ui.done()
    settings = ctx.refresh()
    assert settings.owner_name == "Ada" and settings.openai_api_key == "sk-other"
    assert [call for call in world.calls if call[0] == "openai"] == [
        ("openai", "sk-live"), ("openai", "sk-other")
    ]


def test_the_overview_puts_what_is_not_configured_first(make_ctx, claude_signed_in):
    ctx = make_ctx([("What next?", "exit")])

    run_wizard(ctx)

    labels = [choice.label for choice in ctx.ui.choices["What next?"]]
    assert labels[0].startswith("Set up what is left")
    todo, done = labels.index("Not yet configured"), labels.index("Configured")
    assert todo < labels.index("○  Voice") < done < labels.index("✓  Coding agents")
    assert labels[-2:] == ["Review everything", "Exit"]
    assert not any("XDG" in label for label in labels)


def test_a_section_picked_from_the_overview_is_walked_alone_and_comes_back(
    make_ctx, claude_signed_in, world
):
    ConfigStore().set({"OPENAI_API_KEY": "sk-saved-key-0123456789"})
    ctx = make_ctx([
        ("What next?", "voice"),
        ("OpenAI API key", DEFAULT),        # configured: offered, and kept with Enter
        ("What next?", "owner"),
        ("assistant be called", Back()),    # Esc at the first question: the overview
        ("What next?", "exit"),
    ])

    run_wizard(ctx)

    assert ctx.ui.done()
    assert ctx.ui.currents["OpenAI API key"] == "sk-saved-key-0123456789"
    assert not [call for call in world.calls if call[0] == "openai"]  # nothing to check
    assert ctx.ui.lines("section") == ["Overview", "Voice", "Overview", "Owner and PIN",
                                       "Overview"]
    assert ctx.ui.lines("panel")  # something was walked, so the summary closes it


def test_a_section_that_failed_is_walked_even_after_it_was_walked(make_ctx, claude_signed_in):
    # A default the enabled list leaves out: configured, and broken.
    ConfigStore().set(
        {"OPENAI_API_KEY": "sk", "AGENTS_ENABLED": "claude", "AGENT_BACKEND": "codex"}
    )
    ConfigStore().mark_walked("agents")
    ctx = make_ctx([])

    found = statuses(ctx, wizard.run_doctor_checks(ctx.settings))

    assert found["agents"] == FAILED
    assert "agents" in [section.key for section in pending(ctx, found)]


def test_a_missing_optional_section_is_walked_once(make_ctx, claude_signed_in):
    ctx = make_ctx([])
    found = statuses(ctx, wizard.run_doctor_checks(ctx.settings))
    assert found["plugins"] == MISSING and "plugins" in [s.key for s in pending(ctx, found)]

    ConfigStore().mark_walked("plugins")

    assert "plugins" not in [s.key for s in pending(ctx, found)]


def test_the_voice_key_is_asked_until_it_is_there(make_ctx, claude_signed_in):
    ConfigStore().mark_walked("voice")
    ctx = make_ctx([])

    found = statuses(ctx, wizard.run_doctor_checks(ctx.settings))

    assert found["voice"] == MISSING
    assert "voice" in [section.key for section in pending(ctx, found)]


def test_the_import_section_appears_only_with_something_to_migrate(make_ctx, tmp_path):
    ctx = make_ctx([])
    assert "import" not in statuses(ctx, [])

    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")

    assert statuses(ctx, [])["import"] == MISSING


def test_nothing_is_set_up_until_the_old_files_are_migrated(make_ctx, tmp_path):
    """What setup saves would land where `keryx migrate` is about to move things."""
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")
    ctx = make_ctx([])

    assert run_wizard(ctx) == 1

    assert ctx.ui.asked == [] and ConfigStore().stored() == {}
    assert ".env" in ctx.ui.lines("error")[0]
    assert "keryx migrate" in ctx.ui.lines("outro")[0]


def test_review_walks_every_section_and_asks_again(make_ctx, claude_signed_in, monkeypatch):
    walked = []

    def recorder(key):
        return lambda ctx: walked.append((key, ctx.review))

    monkeypatch.setattr(
        wizard,
        "SECTIONS",
        tuple(wizard.Section(s.key, s.title, recorder(s.key)) for s in wizard.SECTIONS),
    )
    ctx = make_ctx([])

    run_wizard(ctx, review_all=True)

    assert [key for key, _ in walked] == [
        "voice", "agents", "local", "settings", "owner", "phone", "google", "plugins", "issues",
        "profile", "projects", "service",
    ]
    assert all(review for _, review in walked)


def test_nothing_left_offers_a_review(make_ctx, claude_signed_in, monkeypatch):
    for section in wizard.SECTIONS:
        ConfigStore().mark_walked(section.key)
    ConfigStore().set({"OPENAI_API_KEY": "sk"})
    ctx = make_ctx([("Everything is set up", "exit")])

    assert run_wizard(ctx) == 0
    [options] = ctx.ui.choices.values()
    values = [choice.value for choice in options if not choice.separator]
    assert "left" not in values and values[-2:] == ["review", "exit"]


def test_ctrl_c_propagates_and_what_was_saved_stays(make_ctx, claude_signed_in):
    ctx = make_ctx(
        [("What next?", "left"), ("How should calls be voiced?", "openai"),
         ("OpenAI API key", "sk-1"), ("local agent's model run", Aborted())]
    )

    with pytest.raises(Aborted):
        run_wizard(ctx)

    assert ConfigStore()._secrets() == {"OPENAI_API_KEY": "sk-1"}
    assert "voice" in ConfigStore().walked_sections()


def test_half_a_phone_counts_against_the_summary(make_ctx, claude_signed_in):
    ConfigStore().set({"OPENAI_API_KEY": "sk", "PUBLIC_HOST": "keryx.example.com"})

    assert wizard.summary(make_ctx([])) == 1


def test_a_hard_failure_left_at_the_end_is_exit_1(make_ctx, claude_signed_in, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent, settings=None: False)
    ctx = make_ctx([("What next?", "exit")])
    assert run_wizard(ctx) == 0  # exiting at once changes nothing and fails nothing

    assert wizard.summary(make_ctx([])) == 1


def test_the_agent_instructions_name_this_machines_paths(make_ctx):
    ctx = make_ctx([])

    text = wizard.agent_instructions(ctx.settings, ctx.store)

    assert str(ctx.store.home) in text
    assert str(ctx.settings.data_dir / "projects") in text
    assert "--from-env" in text and "never set it yourself" in text
    assert "keryx auth login gmail --callback-url" in text
    assert "{" not in text


def test_every_status_is_one_the_table_can_mark():
    assert set(wizard.MARKS) == {DONE, MISSING, FAILED}
