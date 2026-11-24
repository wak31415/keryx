"""The Plugins section: which optional tools Keryx has, and each one's important settings.

The ssh hosts are made up and answered by `FakeWorld`; nothing here reads a real
`~/.ssh/config` or runs ssh.
"""

import tomllib

import pytest

from keryx import plugins
from keryx.agents import registry
from keryx.config.store import ConfigStore
from keryx.integrations import google as account
from keryx.integrations.gmail import legacy_token_path
from keryx.plugins.ssh_hosts import SshHost
from keryx.setup import plugins as section
from keryx.setup import wizard
from keryx.setup.ui import Back

from .fakes import DEFAULT

HOSTS = [
    SshHost("alpha", "login.alpha.example", "me", True, "/tmp/cm-alpha"),
    SshHost("beta", "login.beta.example", "me", True, "/tmp/cm-beta"),
    SshHost("gamma", "gamma.example", "", False, ""),
]


def on(ctx, name) -> bool:
    return plugins.tool_path(ctx.settings, name).is_file()


def toml(ctx, name) -> dict:
    return tomllib.loads(plugins.config_path(ctx.settings, name).read_text())


# --- the checklist -----------------------------------------------------------------------


def test_a_first_walk_ticks_slack_and_email(make_ctx):
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    choices = {c.value: c for c in ctx.ui.choices["Which plugins should Keryx have?"]}
    assert [name for name, c in choices.items() if c.checked] == ["send_to_slack", "check_email"]
    assert choices["send_to_slack"].hint.endswith(" (recommended)")
    assert "·" not in "".join(choice.hint for choice in choices.values())
    assert "recommended" not in choices["cluster_stats"].hint


def test_a_later_walk_ticks_only_what_is_on(make_ctx):
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    assert not any(c.checked for c in ctx.ui.choices["Which plugins should Keryx have?"])


def test_the_plugins_a_model_answers_need_the_claude_extra(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    choices = {c.value: c for c in ctx.ui.choices["Which plugins should Keryx have?"]}
    for name in ("check_email", "find_document"):
        assert "uv sync --extra claude" in choices[name].disabled and not choices[name].checked
    assert all(c.disabled is None for name, c in choices.items()
               if name not in ("check_email", "find_document"))


def test_one_that_is_on_and_cannot_be_ticked_is_not_turned_off_for_it(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([("Which plugins", [])])
    plugins.install(ctx.settings, "find_document")

    section.run_section(ctx)

    assert ctx.ui.done() and on(ctx, "find_document")


# --- slack -------------------------------------------------------------------------------


def test_slack_saves_its_token_as_a_secret_and_its_channel_in_its_file(make_ctx):
    ctx = make_ctx([("Which plugins", ["send_to_slack"]), ("Bot token", "xoxb-1"),
                    ("DM channel id", "D123")])

    section.run_section(ctx)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1"}
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"
    assert on(ctx, "send_to_slack")
    assert "send_to_slack is on, from the next call" in ctx.ui.lines("success")
    assert any("The rest of its settings are in" in line for line in ctx.ui.lines("note"))
    assert ctx.ui.lines("markdown")  # how to make the app, since there was no token


def test_an_mcp_server_that_carries_the_route_needs_no_token(make_ctx):
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "send_to_slack", {"mcp_server": "chat"})
    ctx.ui.answers = [("Which plugins", ["send_to_slack"]), ("blank uses chat's", ""),
                      ("DM channel id", "")]

    section.run_section(ctx)

    assert ConfigStore()._secrets() == {}
    assert on(ctx, "send_to_slack")


# --- email -------------------------------------------------------------------------------


def google_signed_in(ctx, services=("gmail",)):
    account.write_token(
        ctx.settings,
        account.GoogleToken("rt", "cid", "cs", account.requested_scopes(services), "a@b.c"),
    )


def test_email_with_gmail_connected_asks_nothing_about_signing_in(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["check_email"])])
    google_signed_in(ctx)

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert "Gmail: connected" in ctx.ui.lines("success")
    assert on(ctx, "check_email")


def test_email_is_not_signed_in_again_on_review(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["check_email"])], review=True)
    google_signed_in(ctx)

    section.run_section(ctx)

    assert world.calls == [] and on(ctx, "check_email")


def test_an_older_gmail_only_sign_in_still_turns_email_on(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["check_email"])])
    ctx.settings.ensure_dirs()
    legacy_token_path(ctx.settings).write_text("{}")

    section.run_section(ctx)

    assert world.calls == [] and on(ctx, "check_email")


def test_email_without_a_sign_in_offers_the_google_one(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([("Which plugins", ["check_email"]), ("Sign in to Google?", DEFAULT)])

    section.run_section(ctx)

    assert world.calls[0][0] == "google_browser"
    assert on(ctx, "check_email") and ctx.settings.user_google_email == "sam@example.com"


def test_email_not_signed_in_is_left_off(make_ctx):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([("Which plugins", ["check_email"]), ("Sign in to Google?", "skip")])

    section.run_section(ctx)

    assert not on(ctx, "check_email")
    assert any("keryx auth login google" in line for line in ctx.ui.lines("note"))


def test_email_is_left_off_when_gmail_was_not_granted(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    world.google_services = ("calendar",)
    ctx = make_ctx([("Which plugins", ["check_email"]), ("Sign in to Google?", DEFAULT)])

    section.run_section(ctx)

    assert not on(ctx, "check_email")
    assert any("Gmail is not connected" in line for line in ctx.ui.lines("note"))


# --- the calendar pair, and the sign-in they share with email ----------------------------


def choices_of(ctx) -> dict:
    return {c.value: c for c in ctx.ui.choices["Which plugins should Keryx have?"]}


def test_without_a_sign_in_no_newer_google_plugin_is_ticked(make_ctx):
    """None is worth a sign-in of its own to somebody who has none."""
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    for name in ("check_calendar", "add_calendar_event", "find_document"):
        choice = choices_of(ctx)[name]
        assert not choice.checked and choice.disabled is None
        assert "recommended" not in choice.hint


def test_reading_the_calendar_and_documents_is_ticked_once_their_service_is_connected(make_ctx):
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("calendar", "drive"))

    section.run_section(ctx)

    choices = choices_of(ctx)
    assert [name for name, c in choices.items() if c.checked] == [
        "send_to_slack", "check_email", "check_calendar", "find_document",
    ]
    assert "recommended" in choices["check_calendar"].hint
    assert "recommended" in choices["find_document"].hint


def test_the_plugin_that_writes_is_never_ticked_for_anybody(make_ctx):
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("calendar", "drive"))

    section.run_section(ctx)

    writing = choices_of(ctx)["add_calendar_event"]
    assert not writing.checked and "recommended" not in writing.hint


def test_only_the_connected_service_ticks_its_plugin(make_ctx):
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("calendar",))

    section.run_section(ctx)

    choices = choices_of(ctx)
    assert choices["check_calendar"].checked and not choices["find_document"].checked


def test_a_later_walk_ticks_no_plugin_that_is_off_however_connected(make_ctx):
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("calendar", "drive"))

    section.run_section(ctx)

    choices = choices_of(ctx)
    assert not any(c.checked for c in choices.values())
    assert "recommended" in choices["check_calendar"].hint  # still said, never imposed


def test_documents_connected_without_the_claude_extra_is_not_ticked(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("calendar", "drive"))

    section.run_section(ctx)

    choices = choices_of(ctx)
    assert choices["check_calendar"].checked
    assert not choices["find_document"].checked and choices["find_document"].disabled


def test_a_first_walk_that_takes_the_defaults_turns_the_ticked_ones_on(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["check_calendar", "find_document"])])
    google_signed_in(ctx, ("calendar", "drive"))

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert on(ctx, "check_calendar") and on(ctx, "find_document")
    assert not on(ctx, "add_calendar_event")


def keryx_app_offers(tmp_path, monkeypatch, services):
    """Keryx's own published Google client, approved for `services` only."""
    import json

    client = tmp_path / "published_client.json"
    client.write_text(json.dumps({
        "installed": {"client_id": "keryx.apps", "client_secret": "GOCSPX-published"},
        "keryx_services": list(services),
    }))
    monkeypatch.setattr(account, "PUBLISHED_CLIENT_FILE", client)


def test_a_service_keryxs_app_does_not_offer_is_not_sent_to_a_sign_in_that_cannot_add_it(
    make_ctx, world, tmp_path, monkeypatch
):
    """The note used to say `keryx auth login google`, which asks for what the client
    offers and so could never connect it."""
    keryx_app_offers(tmp_path, monkeypatch, ["gmail"])
    ctx = make_ctx([("Which plugins", ["check_calendar", "find_document"])])
    google_signed_in(ctx, ("gmail",))

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert not on(ctx, "check_calendar") and not on(ctx, "find_document")
    notes = [line for line in ctx.ui.lines("note") if "left off" in line]
    assert len(notes) == 2
    for note, label, name in zip(notes, ("Calendar", "Drive"),
                                 ("check_calendar", "find_document"), strict=True):
        assert f"{label} is not offered by Keryx's Google app yet" in note
        assert "keryx auth login google --client-file FILE" in note
        assert f"`keryx plugins install {name}`" in note
        assert "`keryx setup` again" not in note


def test_a_service_keryxs_app_does_offer_is_still_offered_the_sign_in(
    make_ctx, world, tmp_path, monkeypatch
):
    keryx_app_offers(tmp_path, monkeypatch, ["gmail", "calendar"])
    ctx = make_ctx([("Which plugins", ["check_calendar"]), ("Sign in to Google?", DEFAULT)])

    section.run_section(ctx)

    assert [call[0] for call in world.calls] == ["google_browser"]
    assert on(ctx, "check_calendar")


def test_calendar_connected_turns_both_on_and_asks_nothing(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["check_calendar", "add_calendar_event"])])
    google_signed_in(ctx, ("calendar",))

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert ctx.ui.lines("success").count("Calendar: connected") == 2
    assert on(ctx, "check_calendar") and on(ctx, "add_calendar_event")
    assert toml(ctx, "check_calendar") == {"calendars": "selected", "max_events": 25}
    assert toml(ctx, "add_calendar_event") == {"default_minutes": 60}


def test_documents_with_drive_connected_asks_nothing(make_ctx, world):
    ctx = make_ctx([("Which plugins", ["find_document"])])
    google_signed_in(ctx, ("drive",))

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert "Drive: connected" in ctx.ui.lines("success")
    assert on(ctx, "find_document")
    assert toml(ctx, "find_document") == {"model": "claude-opus-5-5", "effort": "low"}


def test_documents_without_drive_in_the_sign_in_is_left_off(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    world.google_services = ("gmail", "calendar")
    ctx = make_ctx([("Which plugins", ["find_document"]), ("Sign in to Google?", DEFAULT)])

    section.run_section(ctx)

    assert not on(ctx, "find_document")
    assert any("Drive is not connected, so find_document is left off" in line
               for line in ctx.ui.lines("note"))


def test_calendar_needs_no_claude(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: False)
    ctx = make_ctx([("Which plugins", ["check_calendar"])])
    google_signed_in(ctx, ("calendar",))

    section.run_section(ctx)

    assert on(ctx, "check_calendar")


def test_several_plugins_waiting_on_the_sign_in_ask_for_it_once(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([
        ("Which plugins", ["check_email", "check_calendar", "add_calendar_event"]),
        ("Sign in to Google?", DEFAULT),
    ])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert [call[0] for call in world.calls] == ["google_browser"]
    assert all(on(ctx, name) for name in ("check_email", "check_calendar",
                                          "add_calendar_event"))


def test_going_back_after_the_sign_in_never_signs_in_a_second_time(make_ctx, world):
    """Esc runs the section again from its record. By then the sign-in is kept, so the
    section no longer asks for one and the probe that ran it is not run again."""
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})

    def after(ctx) -> None:
        ctx.ui.text("And then?", default="")

    walk = [wizard.Section("plugins", "Plugins", section.run_section),
            wizard.Section("after", "After", after)]
    ctx = make_ctx([
        ("Which plugins", ["check_email", "check_calendar"]),
        ("Sign in to Google?", DEFAULT),
        ("And then?", Back()),
        ("And then?", "done"),
    ])

    assert wizard.run_walk(ctx, walk, {}) is True

    assert ctx.ui.done()
    assert [call[0] for call in world.calls] == ["google_browser"]
    assert on(ctx, "check_email") and on(ctx, "check_calendar")


def test_a_sign_in_turned_down_is_not_offered_again_for_the_next_plugin(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([
        ("Which plugins", ["check_email", "check_calendar"]),
        ("Sign in to Google?", "skip"),
    ])

    section.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert not on(ctx, "check_email") and not on(ctx, "check_calendar")
    notes = ctx.ui.lines("note")
    assert any("keryx plugins install check_email" in line for line in notes)
    assert any("Calendar is not connected, so check_calendar is left off" in line
               for line in notes)


def test_a_sign_in_that_left_calendar_out_offers_to_sign_in_again(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([("Which plugins", ["check_calendar"]), ("Sign in again?", DEFAULT)])
    google_signed_in(ctx, ("gmail",))

    section.run_section(ctx)

    assert [call[0] for call in world.calls] == ["google_browser"]
    assert on(ctx, "check_calendar")


def test_calendar_still_not_granted_is_left_off(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    world.google_services = ("gmail",)
    ctx = make_ctx([("Which plugins", ["check_calendar"]), ("Sign in to Google?", DEFAULT)])

    section.run_section(ctx)

    assert not on(ctx, "check_calendar")
    assert any("Calendar is not connected" in line for line in ctx.ui.lines("note"))


def test_turning_a_calendar_plugin_off_leaves_the_other_and_the_sign_in(make_ctx):
    ctx = make_ctx([])
    google_signed_in(ctx, ("calendar",))
    plugins.install(ctx.settings, "check_calendar")
    plugins.install(ctx.settings, "add_calendar_event")
    ctx.ui.answers = [("Which plugins", ["check_calendar"]), ("Turn off Add to calendar", True)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert on(ctx, "check_calendar") and not on(ctx, "add_calendar_event")
    assert account.read_token(ctx.settings) is not None


# --- billing -----------------------------------------------------------------------------


def test_billing_saves_the_admin_key_and_writes_the_provider_and_budget(make_ctx):
    ctx = make_ctx([("Which plugins", ["check_billing"]), ("Whose bill", "anthropic"),
                    ("Anthropic admin key", "sk-ant-admin"), ("monthly budget", "50")])

    section.run_section(ctx)

    assert ConfigStore()._secrets() == {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin"}
    assert toml(ctx, "check_billing")["provider"] == "anthropic"
    assert toml(ctx, "check_billing")["monthly_budget"] == 50
    assert on(ctx, "check_billing")


def test_editing_a_plugin_that_is_on_opens_on_its_values(make_ctx):
    ConfigStore().set({"OPENAI_ADMIN_KEY": "sk-admin-kept-0000"})
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "check_billing", {"monthly_budget": 75})
    plugins.install(ctx.settings, "check_billing")
    ctx.ui.answers = [("Which plugins", DEFAULT), ("Whose bill", DEFAULT),
                      ("OpenAI admin key", DEFAULT), ("monthly budget", DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.currents["OpenAI admin key (platform.openai.com/settings/organization/"
                           "admin-keys)"] == "sk-admin-kept-0000"
    assert toml(ctx, "check_billing")["monthly_budget"] == 75


# --- turning off -------------------------------------------------------------------------


def test_a_plugin_left_unticked_is_turned_off_after_a_yes(make_ctx):
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "check_billing", {})
    plugins.install(ctx.settings, "check_billing")
    ctx.ui.answers = [("Which plugins", []), ("Turn off Billing", True)]

    section.run_section(ctx)

    assert not on(ctx, "check_billing")
    assert plugins.off_path(ctx.settings, "check_billing").exists()


def test_turning_email_off_leaves_the_google_sign_in_alone(make_ctx):
    """The sign-in is everything's now: the agents' tools run on the same token."""
    ctx = make_ctx([])
    google_signed_in(ctx)
    plugins.install(ctx.settings, "check_email")
    ctx.ui.answers = [("Which plugins", []), ("Turn off Email answers", True)]

    section.run_section(ctx)

    assert ctx.ui.done() and not on(ctx, "check_email")
    assert account.read_token(ctx.settings) is not None


def test_turning_email_off_can_take_an_older_gmail_only_sign_in_with_it(make_ctx):
    ctx = make_ctx([])
    ctx.settings.ensure_dirs()
    legacy_token_path(ctx.settings).write_text("{}")
    plugins.install(ctx.settings, "check_email")
    ctx.ui.answers = [("Which plugins", []), ("Turn off Email answers", True),
                      ("delete the older Gmail-only sign-in", True)]

    section.run_section(ctx)

    assert not on(ctx, "check_email") and not legacy_token_path(ctx.settings).exists()


def test_a_no_leaves_it_on(make_ctx):
    ctx = make_ctx([])
    plugins.install(ctx.settings, "check_billing")
    ctx.ui.answers = [("Which plugins", []), ("Turn off Billing", DEFAULT)]

    section.run_section(ctx)

    assert on(ctx, "check_billing")


# --- cluster_stats -----------------------------------------------------------------------


def cluster_ctx(make_ctx, world, answers):
    world.ssh_hosts = list(HOSTS)
    world.masters_up = {"alpha"}
    world.partitions = {"alpha": ["gpu", "pli"]}
    return make_ctx([("Which plugins", ["cluster_stats"]), *answers])


def test_the_hosts_are_shown_and_one_without_a_master_is_never_offered(make_ctx, world):
    ctx = cluster_ctx(make_ctx, world, [("Which hosts", []), ("What now", "cancel")])

    section.run_section(ctx)

    assert ctx.ui.lines("table") == [
        "alpha | login.alpha.example | me | yes | yes",
        "beta | login.beta.example | me | yes | no",
        "gamma | gamma.example | — | no | —",
    ]
    choices = {c.value: c for c in ctx.ui.choices["Which hosts should cluster_stats ask?"]}
    assert choices["gamma"].disabled == "no ControlMaster in ~/.ssh/config"
    assert choices["alpha"].disabled is None
    assert ("master", "gamma") not in world.calls


def test_install_asks_each_partition_and_turns_it_on(make_ctx, world):
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", ["alpha", "beta"]),
        ("Which partition on alpha", "pli"),
        ("GPU partition on beta", "gpu"),
        ("Install this?", "install"),
    ])

    section.run_section(ctx)

    assert toml(ctx, "cluster_stats")["clusters"] == {"alpha": "pli", "beta": "gpu"}
    assert on(ctx, "cluster_stats")
    assert 'alpha = "pli"' in ctx.ui.lines("panel")[0]
    assert ("partitions", "beta") not in world.calls  # its master is down: typed instead


def test_a_partition_slurm_did_not_list_can_be_typed(make_ctx, world):
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", ["alpha"]),
        ("Which partition on alpha", section.TYPE_ONE),
        ("GPU partition on alpha", "special"),
        ("Install this?", "install"),
    ])

    section.run_section(ctx)

    assert toml(ctx, "cluster_stats")["clusters"] == {"alpha": "special"}


def test_template_writes_the_file_and_a_draft_and_says_where(make_ctx, world):
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", ["alpha"]), ("Which partition", "gpu"), ("Install this?", "template"),
    ])

    section.run_section(ctx)

    path = plugins.config_path(ctx.settings, "cluster_stats")
    assert toml(ctx, "cluster_stats")["clusters"] == {"alpha": "gpu"}
    assert plugins.draft_path(ctx.settings, "cluster_stats").exists()
    assert not on(ctx, "cluster_stats")
    assert f"written to {path}" in ctx.ui.lines("success")
    assert any("keryx plugins install cluster_stats" in line for line in ctx.ui.lines("note"))


def test_cancel_writes_nothing(make_ctx, world):
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", ["alpha"]), ("Which partition", "gpu"), ("Install this?", "cancel"),
    ])

    section.run_section(ctx)

    assert list(ctx.settings.custom_tools_dir.glob("cluster_stats*")) == []


def test_with_no_control_master_only_a_template_or_nothing_is_offered(make_ctx, world):
    world.ssh_hosts = [HOSTS[2]]
    ctx = make_ctx([("Which plugins", ["cluster_stats"]), ("What now", "template")])

    section.run_section(ctx)

    assert [c.value for c in ctx.ui.choices["What now?"]] == ["template", "cancel"]
    assert any("No host in ~/.ssh/config has a ControlMaster" in line
               for line in ctx.ui.lines("warn"))
    assert "# One line per host" in plugins.config_path(ctx.settings, "cluster_stats").read_text()


def test_going_back_replays_the_ssh_probes_rather_than_running_them_again(make_ctx, world):
    """Esc at the last question goes back to the partition, not back to reading ssh."""
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", ["alpha"]),
        ("Which partition on alpha", "gpu"),
        ("Install this?", Back()),
        ("Which partition on alpha", "pli"),
        ("Install this?", "install"),
    ])
    walk = [s for s in wizard.SECTIONS if s.key == "plugins"]

    assert wizard.run_walk(ctx, walk, {})

    assert world.calls.count(("ssh_hosts",)) == 1
    assert world.calls.count(("partitions", "alpha")) == 1
    assert toml(ctx, "cluster_stats")["clusters"] == {"alpha": "pli"}


@pytest.mark.parametrize("bad", ["has space", "a;b"])
def test_a_typed_partition_must_be_a_bare_word(make_ctx, world, bad):
    ctx = cluster_ctx(make_ctx, world, [("Which hosts", ["beta"]), ("GPU partition", bad)])

    with pytest.raises(AssertionError, match="refused"):
        section.run_section(ctx)


def test_a_file_that_does_not_parse_is_said_and_left_for_the_owner(make_ctx):
    ctx = make_ctx([])
    ctx.settings.ensure_dirs()
    path = plugins.config_path(ctx.settings, "check_billing")
    path.write_text("provider = \n")
    ctx.ui.answers = [("Which plugins", ["check_billing"]), ("Whose bill", DEFAULT),
                      ("OpenAI admin key", "sk-admin"), ("monthly budget", DEFAULT)]

    section.run_section(ctx)

    assert any("does not parse" in line for line in ctx.ui.lines("warn"))
    assert any("does not parse" in line for line in ctx.ui.lines("error"))
    assert path.read_text() == "provider = \n" and not on(ctx, "check_billing")


def test_an_old_setting_that_would_not_validate_is_not_a_default(make_ctx, world):
    ConfigStore().config_path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore().config_path.write_text('[CLUSTERS]\n"beta; x" = "gpu"\n')
    ctx = cluster_ctx(make_ctx, world, [("Which hosts", DEFAULT), ("What now", "cancel")])

    section.run_section(ctx)

    assert not any(c.checked for c in ctx.ui.choices["Which hosts should cluster_stats ask?"])


@pytest.mark.parametrize(("typed", "problem"), [("lots", "A number"), ("-1", "0 or more")])
def test_the_budget_must_be_a_number_of_dollars(make_ctx, typed, problem):
    ctx = make_ctx([("Which plugins", ["check_billing"]), ("Whose bill", DEFAULT),
                    ("OpenAI admin key", "sk-admin"), ("monthly budget", typed)])

    with pytest.raises(AssertionError, match=problem):
        section.run_section(ctx)
