"""The smaller `jarvis setup` sections, each against a scripted terminal and a fake world."""

import stat

import pytest

from jarvis.config import Settings, pin_file, read_enrolled_pin, write_enrolled_pin
from jarvis.config.store import ConfigStore
from jarvis.setup import sections
from jarvis.setup.sections import pin_problem

from .fakes import DEFAULT

# --- voice ------------------------------------------------------------------------------


def test_the_key_is_checked_with_openai_and_kept_as_a_secret(make_ctx, world):
    ctx = make_ctx([("OpenAI API key", "sk-live")])

    sections.run_voice(ctx)

    assert world.calls == [("openai", "sk-live")]
    assert ConfigStore()._secrets() == {"OPENAI_API_KEY": "sk-live"}
    assert ctx.settings.openai_api_key == "sk-live"
    assert "sk-live" not in " ".join(ctx.ui.lines())


def test_a_refused_key_is_asked_again(make_ctx, world):
    world.openai_problem = "OpenAI does not recognise that key"
    ctx = make_ctx(
        [("OpenAI API key", "sk-bad"), ("Keep it anyway", False), ("OpenAI API key", "sk-bad2"),
         ("Keep it anyway", True)]
    )

    sections.run_voice(ctx)

    assert ConfigStore()._secrets() == {"OPENAI_API_KEY": "sk-bad2"}
    assert ctx.ui.lines("error") == ["OpenAI does not recognise that key"] * 2


def test_a_key_already_set_is_not_asked_for(make_ctx):
    ConfigStore().set({"OPENAI_API_KEY": "sk-there"})
    ctx = make_ctx([])

    sections.run_voice(ctx)

    assert ctx.ui.done() and ctx.ui.asked == []


def test_a_key_in_the_environment_wins_and_is_said_to(make_ctx, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    ctx = make_ctx([])

    sections.run_voice(ctx)

    assert any("environment" in line for line in ctx.ui.lines("warn"))
    assert ConfigStore().stored() == {}


# --- owner and PIN ------------------------------------------------------------------------


def test_owner_name_numbers_and_a_pin_chosen_now(make_ctx):
    ctx = make_ctx(
        [
            ("What should Jarvis call you", "Ada"),
            ("mobile numbers", "+15551234567, +15557654321"),
            ("Set the PIN", "now"),
            ("New PIN", "482915"),
            ("same PIN again", "482915"),
        ]
    )

    sections.run_owner(ctx)

    stored = ConfigStore().stored()
    assert stored["OWNER_NAME"] == "Ada"
    assert stored["ALLOWED_CALLERS"] == ["+15551234567", "+15557654321"]
    assert stored["OWNER_NUMBER"] == "+15551234567"
    path = pin_file(ctx.settings.data_dir)
    assert read_enrolled_pin(ctx.settings.data_dir) == "482915"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "JARVIS_PIN" not in stored
    assert "482915" not in " ".join(ctx.ui.lines())


def test_the_first_call_may_be_left_to_set_the_pin(make_ctx):
    ctx = make_ctx(
        [("call you", ""), ("mobile numbers", ""), ("Set the PIN", "call")]
    )

    sections.run_owner(ctx)

    assert not pin_file(ctx.settings.data_dir).exists()
    assert ctx.settings.pin_enrolment_open


@pytest.mark.parametrize("digits", ["12345", "123456789", "12a456", "111111", "123456", "876543"])
def test_a_pin_that_is_not_worth_having_is_refused(digits):
    assert pin_problem(digits) is not None


def test_a_mistyped_second_pin_asks_again(make_ctx):
    ctx = make_ctx(
        [("call you", ""), ("mobile", ""), ("Set the PIN", "now"), ("New PIN", "482915"),
         ("same PIN", "482916"), ("New PIN", "482915"), ("same PIN", "482915")]
    )

    sections.run_owner(ctx)

    assert ctx.ui.lines("error") == ["Those were not the same."]
    assert read_enrolled_pin(ctx.settings.data_dir) == "482915"


def test_a_pin_in_its_file_is_left_alone_outside_a_review(make_ctx):
    ctx = make_ctx([])
    write_enrolled_pin(ctx.settings.data_dir, "482915")
    ctx.refresh()

    sections.run_pin(ctx)

    assert ctx.ui.asked == []
    assert read_enrolled_pin(ctx.settings.data_dir) == "482915"


def test_replacing_a_pin_takes_the_new_one_twice_and_two_yeses(make_ctx):
    ctx = make_ctx(
        [("Choose a new PIN", True), ("New PIN", "739104"), ("same PIN", "739104"),
         ("Delete", True)],
        review=True,
    )
    write_enrolled_pin(ctx.settings.data_dir, "482915")
    ctx.refresh()

    sections.run_pin(ctx)

    assert read_enrolled_pin(ctx.settings.data_dir) == "739104"


def test_stopping_before_the_last_yes_leaves_the_old_pin(make_ctx):
    """Never a moment with no PIN and an open door: the file goes only once the new digits
    are in hand, and only on the last yes."""
    ctx = make_ctx(
        [("Choose a new PIN", True), ("New PIN", "739104"), ("same PIN", "739104"),
         ("Delete", False)],
        review=True,
    )
    write_enrolled_pin(ctx.settings.data_dir, "482915")
    ctx.refresh()

    sections.run_pin(ctx)

    assert read_enrolled_pin(ctx.settings.data_dir) == "482915"


def test_a_pin_in_the_environment_is_never_touched(make_ctx, monkeypatch):
    monkeypatch.setenv("JARVIS_PIN", "482915")
    ctx = make_ctx([], review=True)

    sections.run_pin(ctx)

    assert ctx.ui.asked == []
    assert not pin_file(ctx.settings.data_dir).exists()


def test_an_unusable_pin_file_is_replaced_after_asking(make_ctx):
    ctx = make_ctx([("New PIN", "739104"), ("same PIN", "739104"), ("Delete", True)])
    ctx.settings.data_dir.mkdir(parents=True)
    pin_file(ctx.settings.data_dir).write_text("oops\n")
    ctx.refresh()

    sections.run_pin(ctx)

    assert read_enrolled_pin(ctx.settings.data_dir) == "739104"


def test_a_pin_that_appears_meanwhile_is_never_overwritten(make_ctx, monkeypatch):
    ctx = make_ctx([("Set the PIN", "now"), ("New PIN", "739104"), ("same PIN", "739104")])

    def raced(data_dir, digits):
        return False

    monkeypatch.setattr(sections, "write_enrolled_pin", raced)

    sections.run_pin(ctx)

    assert any("appeared" in line for line in ctx.ui.lines("error"))


# --- settings ------------------------------------------------------------------------------


def test_recommended_keeps_every_default_and_says_what_the_service_may_change(make_ctx):
    ctx = make_ctx([("sensible default", "recommended")])

    sections.run_settings(ctx)

    assert ConfigStore().stored() == {}
    assert any("OPENAI_VOICE" in line for line in ctx.ui.lines("note"))


def test_manual_asks_each_setting_by_type_and_saves_only_changes(make_ctx):
    answers = [("sensible default", "manual"), ("Which groups", ["voice"])]
    for name, info in Settings.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("group") != "voice" or not info.repr:
            continue
        key = name.upper()
        answer = {"OPENAI_VOICE": "marin", "VAD_MODE": "server", "VAD_SILENCE_MS": "900"}
        answers.append((key, answer.get(key, DEFAULT)))
    answers.append(("may the running service change", DEFAULT))
    ctx = make_ctx(answers)

    sections.run_settings(ctx)

    assert ConfigStore().stored() == {
        "OPENAI_VOICE": "marin",
        "VAD_MODE": "server",
        "VAD_SILENCE_MS": 900,
    }
    assert ctx.ui.done()


def test_the_checklist_locks_and_unlocks_what_changed(make_ctx):
    ctx = make_ctx([])
    store = ctx.store
    ctx.ui.answers = [("may the running service change", ["PROJECTS_ROOT", "VAD_MODE"])]

    sections._service_permissions(ctx)

    overrides = store.overrides()
    assert overrides["PROJECTS_ROOT"] is True
    assert overrides["OPENAI_VOICE"] is False
    assert "VAD_MODE" not in overrides
    choices = [c.value for c in ctx.ui.choices[next(iter(ctx.ui.choices))]]
    assert "ALLOWED_CALLERS" not in choices and "OPENAI_API_KEY" not in choices


# --- import --------------------------------------------------------------------------------


def test_a_legacy_env_is_imported_on_a_yes(make_ctx, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-old\nOWNER_NAME=Ada\n")
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    ctx = make_ctx([("Import it now", True)])

    sections.run_import(ctx)

    assert ConfigStore().stored() == {"OPENAI_API_KEY": "sk-old", "OWNER_NAME": "Ada"}
    assert not (tmp_path / ".env").exists()
    assert ctx.settings.owner_name == "Ada"


def test_an_import_that_is_refused_says_why_and_keeps_the_file(make_ctx, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PORT=eighty\n")
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    ctx = make_ctx([("Import it now", True)])

    sections.run_import(ctx)

    assert (tmp_path / ".env").exists()
    assert any("PORT" in line for line in ctx.ui.lines("error"))


def test_no_legacy_env_asks_nothing(make_ctx):
    ctx = make_ctx([])
    sections.run_import(ctx)
    assert ctx.ui.asked == []


# --- slack and billing ----------------------------------------------------------------------


def test_slack_is_skipped_by_default_and_set_up_on_request(make_ctx):
    skipped = make_ctx([("Set up Slack", DEFAULT)])
    sections.run_slack(skipped)
    assert ConfigStore().stored() == {}

    ctx = make_ctx([("Set up Slack", "setup"), ("Bot token", "xoxb-1"), ("channel id", "D123")])
    sections.run_slack(ctx)

    assert ConfigStore().stored() == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL_ID": "D123"}
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1"}


def test_billing_stores_an_admin_key_and_a_budget(make_ctx):
    ctx = make_ctx(
        [("spending questions", "openai"), ("admin key", "sk-admin"), ("monthly budget", "50")]
    )

    sections.run_billing(ctx)

    stored = ConfigStore().stored()
    assert stored["OPENAI_ADMIN_KEY"] == "sk-admin"
    assert stored["BILLING_MONTHLY_BUDGET"] == 50.0
    assert stored["BILLING_PROVIDER"] == "openai"


# --- the service -----------------------------------------------------------------------------


def test_the_service_waits_for_the_phone(make_ctx):
    ctx = make_ctx([])

    sections.run_service(ctx)

    assert any("phone set up first" in line for line in ctx.ui.lines("note"))


def test_the_service_installer_runs_on_a_yes(make_ctx, world, tmp_path, monkeypatch):
    script_dir = tmp_path / "repo" / "scripts"
    script_dir.mkdir(parents=True)
    for name in ("install-systemd.sh", "install-launchd.sh"):
        (script_dir / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(sections, "repo_root", lambda: tmp_path / "repo")
    ConfigStore().set({"PUBLIC_HOST": "jarvis.example.com"})
    ctx = make_ctx([("Install it now", True)])

    sections.run_service(ctx)

    [(kind, argv)] = [call for call in world.calls if call[0] == "script"]
    assert argv[0].startswith(str(script_dir))
    assert ctx.ui.lines("success") == ["the service is installed and running"]


# --- the paths the walks above do not take ------------------------------------------------


def test_the_import_reports_the_pin_the_client_and_what_it_left(make_ctx, tmp_path, monkeypatch):
    data = tmp_path / "jarvis"
    (tmp_path / ".env").write_text(f"JARVIS_PIN=482915\nDATA_DIR={data}\nWHO=me\n")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "client_secret.json").write_text(
        '{"installed": {"client_id": "i", "client_secret": "s"}}'
    )
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    ctx = make_ctx([("Import it now", True)])

    sections.run_import(ctx)

    said = " ".join(ctx.ui.lines())
    assert "the PIN: moved to DATA_DIR/pin" in said
    assert "the Google client file" in said and "WHO" in said


def test_an_import_declined_changes_nothing(make_ctx, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OWNER_NAME=Ada\n")
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")

    sections.run_import(make_ctx([("Import it now", False)]))

    assert (tmp_path / ".env").exists()


def test_three_refused_keys_and_no_keep_leave_nothing_saved(make_ctx, world):
    world.openai_problem = "no"
    ctx = make_ctx([("OpenAI API key", "a"), ("Keep", False)] * 3)

    sections.run_voice(ctx)

    assert ConfigStore().stored() == {}


def test_manual_settings_ask_a_yes_no_a_list_and_a_map(make_ctx):
    groups = {"phone": {"SMS_ENABLED": True}, "projects": {"PROJECTS": '{"a": "/b"}'}}
    answers = [("sensible default", "manual"), ("Which groups", ["phone", "projects"])]
    for name, info in Settings.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("group") in groups and info.repr:
            answers.append((name.upper() if name != "owner_number_explicit" else "OWNER_NUMBER",
                            groups[extra["group"]].get(name.upper(), DEFAULT)))
    answers.append(("may the running service change", DEFAULT))
    ctx = make_ctx(answers)

    sections.run_settings(ctx)

    stored = ConfigStore().stored()
    assert stored["SMS_ENABLED"] is True and stored["PROJECTS"] == {"a": "/b"}


def test_a_manual_value_that_does_not_validate_is_refused_where_it_is_typed(make_ctx):
    ctx = make_ctx([])
    ctx.ui.answers = [("PORT", "eighty")]

    with pytest.raises(AssertionError, match="refused 'eighty'"):
        sections._ask_setting(ctx, "port")


def test_reviewing_the_owner_asks_again_and_can_clear_the_numbers(make_ctx):
    ConfigStore().set({"OWNER_NAME": "Ada", "ALLOWED_CALLERS": "+15551234567"})
    ctx = make_ctx(
        [("call you", "Ada"), ("mobile numbers", ""), ("Set the PIN", "call")], review=True
    )

    sections.run_owner(ctx)

    stored = ConfigStore().stored()
    assert stored["ALLOWED_CALLERS"] == [] and "OWNER_NUMBER" not in stored


def test_three_mismatched_pins_give_up_and_set_nothing(make_ctx):
    ctx = make_ctx([("Set the PIN", "now")] + [("New PIN", "482915"), ("same", "000000")] * 3)

    sections.run_pin(ctx)

    assert not pin_file(ctx.settings.data_dir).exists()


def test_a_replacement_declined_at_the_first_question_changes_nothing(make_ctx):
    ctx = make_ctx([("Choose a new PIN", False)], review=True)
    write_enrolled_pin(ctx.settings.data_dir, "482915")
    ctx.refresh()

    sections.run_pin(ctx)

    assert read_enrolled_pin(ctx.settings.data_dir) == "482915"


def test_slack_and_billing_already_set_ask_nothing(make_ctx):
    ConfigStore().set(
        {"SLACK_BOT_TOKEN": "xoxb", "SLACK_CHANNEL_ID": "D1", "ANTHROPIC_ADMIN_KEY": "k"}
    )
    ctx = make_ctx([])

    sections.run_slack(ctx)
    sections.run_billing(ctx)

    assert ctx.ui.asked == []


def test_billing_without_a_budget_stores_no_budget(make_ctx):
    ctx = make_ctx([("spending", "anthropic"), ("admin key", "k"), ("monthly budget", "")])

    sections.run_billing(ctx)

    assert "BILLING_MONTHLY_BUDGET" not in ConfigStore().stored()


def test_billing_skipped_stores_nothing(make_ctx):
    sections.run_billing(make_ctx([("spending", DEFAULT)]))
    assert ConfigStore().stored() == {}


def test_an_installed_service_is_said_and_nothing_asked(make_ctx, monkeypatch):
    from jarvis.doctor import Check

    monkeypatch.setattr(sections, "service_manager_check", lambda s: Check("s", True, "unit"))
    ctx = make_ctx([])

    sections.run_service(ctx)

    assert ctx.ui.lines("success") == ["installed: unit"]


def test_the_service_outside_a_clone_says_where_the_installer_is(make_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(sections, "repo_root", lambda: tmp_path / "no-clone")
    ctx = make_ctx([])

    sections.run_service(ctx)

    assert any("from a clone" in line for line in ctx.ui.lines("note"))


def test_a_failing_installer_or_a_no_is_said(make_ctx, world, tmp_path, monkeypatch):
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("install-systemd.sh", "install-launchd.sh"):
        (scripts / name).write_text("")
    monkeypatch.setattr(sections, "repo_root", lambda: tmp_path / "repo")
    ConfigStore().set({"PUBLIC_HOST": "j.example.com"})
    world.script_code = 1

    declined = make_ctx([("Install it now", False)])
    sections.run_service(declined)
    failed = make_ctx([("Install it now", True)])
    sections.run_service(failed)

    assert declined.ui.lines("error") == []
    assert any("exited with 1" in line for line in failed.ui.lines("error"))
