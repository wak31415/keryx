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
from keryx.integrations.slack import Member, SlackResult, Workspace, new_app_url
from keryx.plugins.ssh_hosts import SshHost
from keryx.setup import plugins as section
from keryx.setup import wizard
from keryx.setup.slack import MEMBER_ID_HINT, TEST_MESSAGE
from keryx.setup.ui import Back, Forward

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
    assert "recommended" in choices["send_to_slack"].hint
    assert "recommended" not in choices["cluster_stats"].hint


def test_a_later_walk_ticks_only_what_is_on(make_ctx):
    ConfigStore().mark_walked("plugins")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    assert not any(c.checked for c in ctx.ui.choices["Which plugins should Keryx have?"])


def test_email_needs_the_claude_extra(make_ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")
    ctx = make_ctx([("Which plugins", [])])

    section.run_section(ctx)

    email = next(c for c in ctx.ui.choices["Which plugins should Keryx have?"]
                 if c.value == "check_email")
    assert "uv sync --extra claude" in email.disabled and not email.checked


# --- slack -------------------------------------------------------------------------------


SLACK = ("Which plugins", ["send_to_slack"])
TOKEN, WHERE, TEST = "Bot token", "Where to send", "Send a test message"
#: A stand-in for whatever the wizard walks next, so Esc and Tab have somewhere to come from.
NEXT = "Next section"


def slack_calls(world) -> list[tuple]:
    return [call for call in world.calls if call[0].startswith("slack")]


def slack_walk(ctx):
    """The Plugins section and one after it, behind the recorder, as `keryx setup` runs them."""
    def after(ctx):
        ctx.ui.confirm(f"{NEXT}?")

    walk = [wizard.Section("plugins", "Plugins", section.run_section),
            wizard.Section("after", "After", after)]
    return wizard.run_walk(ctx, walk, {})


def asked(ctx) -> list[str]:
    """Each question in the order it was put on the terminal, by its first words."""
    names = ("Which plugins", TOKEN, WHERE, TEST, NEXT, "Save it without checking")
    return [next(name for name in names if name in message) for _, message in ctx.ui.asked]


def test_slack_is_a_link_a_pasted_token_and_an_address(make_ctx, world):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, DEFAULT)])

    section.run_section(ctx)

    assert ctx.ui.done()  # no channel id was asked for
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1"}
    assert toml(ctx, "send_to_slack")["channel_id"] == "U0OWNER"
    assert on(ctx, "send_to_slack")
    assert ctx.ui.lines("link") == [new_app_url()]
    assert "Install to Workspace" in ctx.ui.lines("markdown")[0]
    assert "connected to the Acme workspace" in ctx.ui.lines("success")
    assert "found Sam Doe: messages go to you, from the Keryx app" in ctx.ui.lines("success")
    assert "send_to_slack is on, from the next call" in ctx.ui.lines("success")
    assert "sent: it is in Slack, under Apps" in ctx.ui.lines("success")
    assert any("The rest of its settings are in" in line for line in ctx.ui.lines("note"))
    assert not any("keryx restart" in line for line in ctx.ui.lines("note"))
    assert slack_calls(world) == [
        ("slack_auth",), ("slack_member", "sam@x.io"), ("slack_post", "U0OWNER", TEST_MESSAGE)
    ]


def test_the_address_offered_is_the_google_one(make_ctx, world):
    ConfigStore().set({"USER_GOOGLE_EMAIL": "sam@example.com"})
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, DEFAULT), (TEST, False)])

    section.run_section(ctx)

    assert ("slack_member", "sam@example.com") in world.calls
    assert not any(call[0] == "slack_post" for call in world.calls)


def test_a_token_slack_refuses_is_asked_for_again_and_never_shown(make_ctx, world):
    world.slack_workspaces = [Workspace(error="invalid_auth"), Workspace(team="Acme")]
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-wrong"), (TOKEN, "xoxb-right"), (WHERE, "sam@x.io"),
                    (TEST, False)])

    section.run_section(ctx)

    assert ctx.ui.lines("error") == [
        "Slack did not take that token: Slack does not recognise that token (invalid_auth)"
    ]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-right"}
    assert not any("xoxb-wrong" in text or "xoxb-right" in text for _, text in ctx.ui.said)


@pytest.mark.parametrize(
    ("error", "said"),
    [
        ("unreachable", "That token could not be checked: could not reach Slack"),
        ("http_429", "That token could not be checked: Slack answered HTTP 429"),
        ("http_503", "That token could not be checked: Slack answered HTTP 503"),
        ("unknown_error", "That token could not be checked: Slack's answer could not be read"),
    ],
)
def test_a_token_that_could_not_be_checked_is_not_called_refused_and_may_be_saved(
    make_ctx, world, error, said
):
    """Slack never said no: an outage, a rate limit or a proxy must not trap the owner."""
    world.slack_workspaces = [Workspace(error=error)]
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), ("Save it without checking", False),
                    (TOKEN, "xoxb-2"), ("Save it without checking", True),
                    (WHERE, "D123"), (TEST, False)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.ui.lines("error") == [said, said]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-2"}
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"


@pytest.mark.parametrize("code", ["users_not_found", "missing_scope"])
def test_an_address_slack_cannot_find_falls_back_to_an_id_typed_by_hand(make_ctx, world, code):
    """`missing_scope` is an app made the old way, with `chat:write` and nothing else."""
    world.slack_member = Member(error=code)
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (WHERE, "D123"),
                    (TEST, DEFAULT)])

    section.run_section(ctx)

    assert any(code in line for line in ctx.ui.lines("warn"))
    assert MEMBER_ID_HINT in ctx.ui.lines("note")
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"
    assert ("slack_post", "D123", TEST_MESSAGE) in world.calls


def test_an_id_typed_straight_away_asks_slack_nothing_about_an_address(make_ctx, world):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, " D123 "), (TEST, False)])

    section.run_section(ctx)

    assert slack_calls(world) == [("slack_auth",)]
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"


def test_where_to_send_is_required_and_is_an_address_or_a_bare_id(make_ctx):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "")])
    with pytest.raises(AssertionError, match="Required"):
        section.run_section(ctx)

    ctx = make_ctx([SLACK, (TOKEN, DEFAULT), (WHERE, "#general")])
    with pytest.raises(AssertionError, match="bare word"):
        section.run_section(ctx)


def test_a_test_message_slack_refuses_is_said_and_undoes_nothing(make_ctx, world):
    world.slack_sent = SlackResult(error="channel_not_found")
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, True)])

    section.run_section(ctx)

    (warning,) = ctx.ui.lines("warn")
    assert "(channel_not_found)" in warning and "saved" in warning
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1"}
    assert toml(ctx, "send_to_slack")["channel_id"] == "U0OWNER"
    assert on(ctx, "send_to_slack")


def already_set_up(make_ctx, *, channel="D123", review=False):
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-old"})
    ctx = make_ctx([], review=review)
    plugins.write_config(ctx.settings, "send_to_slack", {"channel_id": channel})
    plugins.install(ctx.settings, "send_to_slack")
    return ctx


def test_slack_already_set_up_is_asked_nothing_new_and_slack_is_not_called(make_ctx, world):
    ctx = already_set_up(make_ctx)
    ctx.ui.answers = [SLACK, (TOKEN, DEFAULT), (WHERE, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert slack_calls(world) == [] and not ctx.ui.lines("link")
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-old"}
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"
    # It is on, and it says why nothing was sent to show it.
    assert "Nothing changed, so no test message was sent." in ctx.ui.lines("note")


def test_reviewing_slack_that_is_set_up_offers_a_test_and_sends_none_by_default(make_ctx, world):
    """A review asks again about what is set, as every section does; Enter changes nothing."""
    ctx = already_set_up(make_ctx, review=True)
    ctx.ui.answers = [SLACK, (TOKEN, DEFAULT), (WHERE, DEFAULT), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done() and slack_calls(world) == []


def test_a_token_with_nowhere_to_send_yet_is_kept_and_the_member_found(make_ctx, world):
    """A token saved with `keryx config set` and nothing else: only the address is asked."""
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-old"})
    ctx = make_ctx([SLACK, (TOKEN, DEFAULT), (WHERE, "sam@x.io"), (TEST, False)])

    section.run_section(ctx)

    assert slack_calls(world) == [("slack_member", "sam@x.io")]
    assert toml(ctx, "send_to_slack")["channel_id"] == "U0OWNER"


def test_a_new_channel_for_a_kept_token_offers_the_test(make_ctx, world):
    ctx = already_set_up(make_ctx)
    ctx.ui.answers = [SLACK, (TOKEN, DEFAULT), (WHERE, " C456 "), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert slack_calls(world) == [("slack_post", "C456", TEST_MESSAGE)]


# A direct message's id belongs to the app that opened it, so a token that is not the one
# on file is never offered it: the destination is asked for again.


def test_a_new_token_is_not_offered_the_old_apps_direct_message(make_ctx, world):
    ConfigStore().set({"USER_GOOGLE_EMAIL": "sam@example.com"})
    ctx = already_set_up(make_ctx, channel="D0OLDAPP")
    ctx.ui.answers = [SLACK, (TOKEN, "xoxb-new"), (WHERE, DEFAULT), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert any("D0OLDAPP" in line and "not offered" in line for line in ctx.ui.lines("note"))
    assert toml(ctx, "send_to_slack")["channel_id"] == "U0OWNER"
    assert slack_calls(world) == [
        ("slack_auth",), ("slack_member", "sam@example.com"),
        ("slack_post", "U0OWNER", TEST_MESSAGE),
    ]


def test_a_new_token_keeps_a_member_id_as_what_is_offered(make_ctx, world):
    ConfigStore().set({"USER_GOOGLE_EMAIL": "sam@example.com"})
    ctx = already_set_up(make_ctx, channel="U0MEMBER")
    ctx.ui.answers = [SLACK, (TOKEN, "xoxb-new"), (WHERE, DEFAULT), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert toml(ctx, "send_to_slack")["channel_id"] == "U0MEMBER"
    assert slack_calls(world) == [("slack_auth",), ("slack_post", "U0MEMBER", TEST_MESSAGE)]


def test_a_token_that_replaces_another_says_a_running_keryx_needs_a_restart(make_ctx, world):
    ctx = already_set_up(make_ctx, channel="U0MEMBER")
    ctx.ui.answers = [SLACK, (TOKEN, "xoxb-new"), (WHERE, DEFAULT), (TEST, False)]

    section.run_section(ctx)

    assert ("A Keryx that started with the old token keeps it until `keryx restart`."
            in ctx.ui.lines("note"))


# --- slack, beside an MCP server that carries it ------------------------------------------


@pytest.fixture
def chat_server(make_ctx):
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "send_to_slack", {"mcp_server": "chat"})
    return ctx


def test_an_mcp_server_that_carries_the_route_needs_no_token(chat_server, world):
    ctx = chat_server
    ctx.ui.answers = [SLACK, (TOKEN, ""), (WHERE, "")]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert any("blank" in line and "chat MCP server" in line for line in ctx.ui.lines("note"))
    assert ConfigStore()._secrets() == {} and slack_calls(world) == []
    assert on(ctx, "send_to_slack")
    assert ("No test message was sent: the token and the channel are the chat MCP server's."
            in ctx.ui.lines("note"))


def test_a_token_beside_an_mcp_server_must_bring_its_own_destination(chat_server, world):
    """Blank would pair the new token with the server's channel, which is its app's own."""
    ctx = chat_server
    ctx.ui.answers = [SLACK, (TOKEN, "xoxb-1"), (WHERE, "")]

    with pytest.raises(AssertionError, match="needs somewhere of its own"):
        section.run_section(ctx)

    assert not on(ctx, "send_to_slack")


def test_a_token_beside_an_mcp_server_says_what_that_does_to_the_route(chat_server, world):
    ctx = chat_server
    ctx.ui.answers = [SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert toml(ctx, "send_to_slack") == {"channel_id": "U0OWNER", "mcp_server": "chat"}
    assert any("not with the chat MCP server's" in line and "subagents still" in line
               for line in ctx.ui.lines("note"))


def test_with_no_token_of_its_own_an_address_cannot_be_looked_up(chat_server):
    ctx = chat_server
    ctx.ui.answers = [SLACK, (TOKEN, ""), (WHERE, "sam@x.io")]

    with pytest.raises(AssertionError, match="no token of its own"):
        section.run_section(ctx)


def test_a_kept_token_beside_an_mcp_server_may_still_use_the_servers_channel(make_ctx, world):
    """An install that already ran this way is asked nothing new and changed in nothing."""
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-old"})
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "send_to_slack", {"mcp_server": "chat"})
    ctx.ui.answers = [SLACK, (TOKEN, DEFAULT), (WHERE, "")]

    section.run_section(ctx)

    assert ctx.ui.done() and slack_calls(world) == []
    assert toml(ctx, "send_to_slack")["channel_id"] == ""
    assert ("No test message was sent: the channel is the chat MCP server's."
            in ctx.ui.lines("note"))


def test_a_slack_file_that_cannot_be_written_sends_no_test(make_ctx, world, monkeypatch):
    def refuse(settings, name, values):
        raise plugins.PluginConfigError("send_to_slack: channel_id must be a bare word")

    monkeypatch.setattr(plugins, "write_config", refuse)
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io")])

    section.run_section(ctx)

    assert ctx.ui.done() and ctx.ui.lines("error")
    assert not any(call[0] == "slack_post" for call in world.calls)


# --- slack: going back and forward, before and after the step has saved --------------------
#
# The step saves as it goes, so a second run finds a token and a destination the first did
# not. Its questions must be the same ones all the same, or Esc lands somewhere new.


def test_esc_before_anything_is_written_does_not_ask_slack_about_the_token_twice(make_ctx, world):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, Back()), (TOKEN, DEFAULT),
                    (WHERE, "sam@x.io"), (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert slack_calls(world) == [("slack_auth",), ("slack_member", "sam@x.io")]
    assert toml(ctx, "send_to_slack")["channel_id"] == "U0OWNER"


def test_esc_at_the_test_offer_goes_back_to_where_to_send_and_offers_the_test_again(
    make_ctx, world
):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, Back()),
                    (WHERE, DEFAULT), (TEST, True), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", TOKEN, WHERE, TEST, WHERE, TEST, NEXT]
    assert ("slack_post", "U0OWNER", TEST_MESSAGE) in world.calls
    assert slack_calls(world).count(("slack_auth",)) == 1


@pytest.mark.parametrize("found", [True, False])
def test_esc_from_the_next_section_lands_on_the_test_offer(make_ctx, world, found):
    """Whether the destination was found by address or typed by hand after a miss."""
    world.slack_member = Member(user_id="U0OWNER") if found else Member(error="users_not_found")
    where = [(WHERE, "sam@x.io")] + ([] if found else [(WHERE, "D123")])
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), *where, (TEST, True), (NEXT, Back()),
                    (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx)[-3:] == [NEXT, TEST, NEXT]
    # Nothing was asked of Slack a second time: one check, one lookup, one message.
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_member", "slack_post"]


def test_esc_from_the_next_section_after_an_unchanged_walk_goes_back_a_question(make_ctx, world):
    """To the last one it asked; and going back is a review, which offers the test too."""
    ctx = already_set_up(make_ctx)
    ctx.ui.answers = [SLACK, (TOKEN, DEFAULT), (WHERE, DEFAULT), (NEXT, Back()),
                      (WHERE, DEFAULT), (TEST, DEFAULT), (NEXT, True)]

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", TOKEN, WHERE, NEXT, WHERE, TEST, NEXT]
    assert slack_calls(world) == []
    assert toml(ctx, "send_to_slack")["channel_id"] == "D123"


def test_tab_after_the_step_has_saved_stops_at_nothing_of_slacks(make_ctx, world):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, True), (NEXT, Back()),
                    (TEST, Forward()), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", TOKEN, WHERE, TEST, NEXT, TEST, NEXT]
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_member", "slack_post"]


def test_tab_from_the_first_question_passes_a_slack_step_already_walked(make_ctx, world):
    ctx = make_ctx([SLACK, (TOKEN, "xoxb-1"), (WHERE, "sam@x.io"), (TEST, True), (NEXT, Back()),
                    (TEST, Back()), (WHERE, Back()), (TOKEN, Forward()), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx)[-2:] == [TOKEN, NEXT]
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_member", "slack_post"]


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
