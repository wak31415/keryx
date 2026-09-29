"""The Plugins section: which optional tools Jarvis has, and each one's important settings.

The ssh hosts are made up and answered by `FakeWorld`; nothing here reads a real
`~/.ssh/config` or runs ssh.
"""

import tomllib

import pytest

from jarvis import plugins
from jarvis.agents import registry
from jarvis.config.store import ConfigStore
from jarvis.integrations.gmail import token_path
from jarvis.plugins.ssh_hosts import SshHost
from jarvis.setup import plugins as section
from jarvis.setup import wizard
from jarvis.setup.ui import Back

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

    choices = {c.value: c for c in ctx.ui.choices["Which plugins should Jarvis have?"]}
    assert [name for name, c in choices.items() if c.checked] == ["send_to_slack", "check_email"]
    assert "recommended" in choices["send_to_slack"].hint
    assert "recommended" not in choices["cluster_stats"].hint


def test_a_later_walk_ticks_only_what_is_on(make_ctx):
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    assert not any(c.checked for c in ctx.ui.choices["Which plugins should Jarvis have?"])


def test_email_needs_the_claude_extra(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    email = next(c for c in ctx.ui.choices["Which plugins should Jarvis have?"]
                 if c.value == "check_email")
    assert "uv sync --extra claude" in email.disabled and not email.checked


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


def test_email_signed_in_already_asks_nothing_and_turns_it_on(make_ctx):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([("Which plugins", ["check_email"])])
    ctx.settings.ensure_dirs()
    token_path(ctx.settings).write_text("{}")

    section.run_section(ctx)

    assert ctx.ui.done()
    assert "Gmail: signed in" in ctx.ui.lines("success")
    assert on(ctx, "check_email")


def test_email_not_signed_in_is_left_off(make_ctx):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([("Which plugins", ["check_email"]), ("address you landed on", "")])

    section.run_section(ctx)

    assert not on(ctx, "check_email")
    assert any("jarvis auth login gmail" in line for line in ctx.ui.lines("note"))


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


def test_turning_email_off_can_take_the_sign_in_with_it(make_ctx):
    ctx = make_ctx([])
    ctx.settings.ensure_dirs()
    token_path(ctx.settings).write_text("{}")
    plugins.install(ctx.settings, "check_email")
    ctx.ui.answers = [("Which plugins", []), ("Turn off Email answers", True),
                      ("delete the Gmail sign-in", True)]

    section.run_section(ctx)

    assert not on(ctx, "check_email") and not token_path(ctx.settings).exists()


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
    assert any("jarvis plugins install cluster_stats" in line for line in ctx.ui.lines("note"))


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


def test_the_old_clusters_are_the_defaults_and_leave_once_it_is_on(make_ctx, world):
    ConfigStore().config_path.parent.mkdir(parents=True, exist_ok=True)
    ConfigStore().config_path.write_text('[CLUSTERS]\nbeta = "gpu"\n')
    ctx = cluster_ctx(make_ctx, world, [
        ("Which hosts", DEFAULT), ("GPU partition on beta", DEFAULT), ("Install this?", "install"),
    ])

    section.run_section(ctx)

    [beta] = [c for c in ctx.ui.choices["Which hosts should cluster_stats ask?"] if c.checked]
    assert beta.value == "beta"
    assert toml(ctx, "cluster_stats")["clusters"] == {"beta": "gpu"}
    assert plugins.retired_in(ConfigStore()) == {}


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
