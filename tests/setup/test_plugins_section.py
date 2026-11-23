"""The Plugins section: which optional tools Keryx has, and each one's important settings.

The ssh hosts are made up and answered by `FakeWorld`; nothing here reads a real
`~/.ssh/config` or runs ssh.
"""

import json
import tomllib

import pytest

from keryx import plugins
from keryx.agents import registry
from keryx.config.store import ConfigStore
from keryx.integrations import google as account
from keryx.integrations import slack as slack_api
from keryx.integrations.gmail import legacy_token_path
from keryx.integrations.slack import (
    Access,
    Connection,
    SlackResult,
    Workspace,
    app_manifest,
    new_app_url,
)
from keryx.plugins.ssh_hosts import SshHost
from keryx.setup import plugins as section
from keryx.setup import wizard
from keryx.setup.slack import (
    LISTEN_NOTE,
    OWNER_LINE,
    OWNER_NOTED,
    READ_NOTE,
    meaning,
)
from keryx.setup.slack import test_message as said_to_test
from keryx.setup.ui import Back, Forward

from .fakes import DEFAULT

#: What the app is called when nobody has named the assistant: Lyra, the default.
LYRA = "Lyra"
#: The one message that shows a connection arriving, at `announce`.
TEST_MESSAGE = said_to_test(Access.ANNOUNCE, LYRA)

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
    assert [name for name, c in choices.items() if c.checked] == ["slack_report", "check_email"]
    assert choices["slack_report"].hint.endswith(" (recommended)")
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


SLACK = ("Which plugins", ["slack_report"])
TOKEN, TEST = "Bot token", "Send a test message"
LEVEL, APP_TOKEN, USER_TOKEN = "What may Keryx see", "App-level token", "User token"
#: The offer of `slack_read`, made once when the level has just been raised to read.
ANSWERS = "Turn on reading Slack"
#: The level every install had before there were levels, taken as it is offered.
ANNOUNCE = (LEVEL, DEFAULT)
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
    names = ("Which plugins", LEVEL, TOKEN, APP_TOKEN, USER_TOKEN, TEST, NEXT,
             "Save it without checking")
    return [next(name for name in names if name in message) for _, message in ctx.ui.asked]


def test_slack_is_a_link_and_two_pasted_tokens_and_nothing_is_asked_about_where(
    make_ctx, world
):
    """The user token says which member they are: reports go to them, and that is all."""
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, DEFAULT)])

    section.run_section(ctx)

    assert ctx.ui.done() and asked(ctx) == ["Which plugins", LEVEL, TOKEN, USER_TOKEN, TEST]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1",
                                        "SLACK_USER_TOKEN": "xoxp-1"}
    saved = toml(ctx, "slack_report")
    assert (saved["channel_id"], saved["owner_id"], saved["access"]) == (
        "U0OWNER", "U0OWNER", "announce")
    assert on(ctx, "slack_report")
    assert ctx.ui.lines("link") == [new_app_url(assistant="Lyra")]
    assert "Install to Workspace" in ctx.ui.lines("markdown")[0]
    assert "the same page as the bot token" in ctx.ui.lines("markdown")[0]
    assert "connected to the Acme workspace" in ctx.ui.lines("success")
    assert "it is yours: you are U0OWNER there" in ctx.ui.lines("success")
    assert "slack_report is on, from the next call" in ctx.ui.lines("success")
    assert "sent: it is in Slack, under Apps" in ctx.ui.lines("success")
    notes = ctx.ui.lines("note")
    assert any(line.startswith("Reports go to you (U0OWNER). To send reports somewhere "
                               "else: `keryx plugins install slack_report --set channel_id=ID`")
               for line in notes)
    assert OWNER_NOTED.format(who="U0OWNER") in notes
    assert not any("keryx restart" in line for line in notes)
    assert slack_calls(world) == [
        ("slack_auth",), ("slack_auth",), ("slack_post", "U0OWNER", TEST_MESSAGE)
    ]


def test_a_token_slack_refuses_is_asked_for_again_and_never_shown(make_ctx, world):
    world.slack_workspaces = [Workspace(error="invalid_auth"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER")]
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-wrong"), (TOKEN, "xoxb-right"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False)])

    section.run_section(ctx)

    assert ctx.ui.lines("error") == [
        "Slack did not take that token: Slack does not recognise that token (invalid_auth)"
    ]
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == "xoxb-right"
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
    """Slack never said no: an outage, a rate limit or a proxy must not trap the owner
    who is replacing a token. Where reports go is on file and stays; who they are is
    asked another time, because only Slack can say."""
    ctx = already_set_up(make_ctx, channel="U0MEMBER")
    world.slack_workspaces = [Workspace(error=error)]
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), ("Save it without checking", False),
                      (TOKEN, "xoxb-2"), ("Save it without checking", True),
                      (USER_TOKEN, "xoxp-1"), (TEST, False)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.ui.lines("error") == [said, said]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-2"}
    saved = toml(ctx, "slack_report")
    assert (saved["channel_id"], saved["owner_id"]) == ("U0MEMBER", "")
    assert any("is asked for another time" in line for line in ctx.ui.lines("note"))


def test_with_slack_unreachable_and_nowhere_on_file_nothing_is_saved(make_ctx, world):
    world.slack_workspaces = [Workspace(error="unreachable")]
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), ("Save it without checking", True),
                    (USER_TOKEN, "xoxp-1")])

    section.run_section(ctx)

    assert ctx.ui.done() and ConfigStore()._secrets() == {}
    assert any("Nothing says where reports would go" in line for line in ctx.ui.lines("error"))


def test_a_test_message_slack_refuses_is_said_and_undoes_nothing(make_ctx, world):
    world.slack_sent = SlackResult(error="channel_not_found")
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True)])

    section.run_section(ctx)

    (warning,) = ctx.ui.lines("warn")
    assert "(channel_not_found)" in warning and "saved" in warning
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] == "xoxb-1"
    assert toml(ctx, "slack_report")["channel_id"] == "U0OWNER"
    assert on(ctx, "slack_report")


def already_set_up(make_ctx, *, channel="D123", review=False):
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-old"})
    ctx = make_ctx([], review=review)
    plugins.write_config(ctx.settings, "slack_report", {"channel_id": channel})
    plugins.install(ctx.settings, "slack_report")
    return ctx


def test_an_install_from_before_user_tokens_walks_through_untouched(make_ctx, world):
    """Bot token and a destination, no user token, no owner: the user token may be left
    blank, Slack is asked nothing, and everything stays exactly as it was."""
    ctx = already_set_up(make_ctx)
    before = toml(ctx, "slack_report")
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, DEFAULT), (USER_TOKEN, "")]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert slack_calls(world) == [] and not ctx.ui.lines("link")
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-old"}
    assert toml(ctx, "slack_report") == before
    assert toml(ctx, "slack_report")["channel_id"] == "D123"
    assert any("goes on working without one" in line for line in ctx.ui.lines("note"))
    # It is on, and it says why nothing was sent to show it.
    assert "Nothing changed, so no test message was sent." in ctx.ui.lines("note")
    # It is shown how to update the app it has, for when it wants an owner on file.
    assert any("from before Keryx asked for a user token" in line
               for line in ctx.ui.lines("note"))
    title, _, body = ctx.ui.lines("panel")[0].partition("\n")
    assert title == "The manifest"
    assert json.loads(body) == app_manifest(Access.ANNOUNCE, assistant="Lyra")
    # Saving it is also what renames the app after the assistant.
    assert ("Saving it also names the app Lyra, after your assistant: Slack shows the name "
            "it was made with until then.") in ctx.ui.lines("note")
    assert "Do not create a second one." in ctx.ui.lines("markdown")[0]
    assert "**User OAuth Token**" in ctx.ui.lines("markdown")[0]
    assert "Bot User OAuth Token" not in ctx.ui.lines("markdown")[0]


def test_that_install_given_its_user_token_gains_an_owner_and_keeps_its_destination(
    make_ctx, world
):
    """The kept destination stays: a direct message's id still goes with the same token."""
    ctx = already_set_up(make_ctx)
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, DEFAULT), (USER_TOKEN, "xoxp-1"), (TEST, False)]

    section.run_section(ctx)

    saved = toml(ctx, "slack_report")
    assert ctx.ui.done()
    assert (saved["channel_id"], saved["owner_id"], saved["team_id"]) == (
        "D123", "U0OWNER", "T0ACME")
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-old",
                                        "SLACK_USER_TOKEN": "xoxp-1"}
    assert any(line.startswith("It keeps sending to D123.") for line in ctx.ui.lines("note"))
    # Asked of Slack only now: which workspace the kept bot token is, then whose the
    # user token is.
    assert slack_calls(world) == [("slack_auth",), ("slack_auth",)]


def test_reviewing_slack_that_is_set_up_offers_a_test_and_sends_none_by_default(make_ctx, world):
    """A review asks again about what is set, as every section does; Enter changes nothing."""
    ctx = already_set_up(make_ctx, review=True)
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, DEFAULT), (USER_TOKEN, ""), (TEST, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done() and slack_calls(world) == []


def test_a_new_token_does_not_keep_the_old_apps_direct_message(make_ctx, world):
    ctx = already_set_up(make_ctx, channel="D0OLDAPP")
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "xoxb-new"), (USER_TOKEN, "xoxp-1"),
                      (TEST, DEFAULT)]

    section.run_section(ctx)

    assert any("D0OLDAPP" in line and "not kept" in line for line in ctx.ui.lines("note"))
    assert toml(ctx, "slack_report")["channel_id"] == "U0OWNER"
    assert slack_calls(world) == [
        ("slack_auth",), ("slack_auth",), ("slack_post", "U0OWNER", TEST_MESSAGE),
    ]


def test_a_new_token_keeps_a_member_id_as_where_reports_go(make_ctx, world):
    ctx = already_set_up(make_ctx, channel="U0MEMBER")
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "xoxb-new"), (USER_TOKEN, "xoxp-1"),
                      (TEST, DEFAULT)]

    section.run_section(ctx)

    saved = toml(ctx, "slack_report")
    assert (saved["channel_id"], saved["owner_id"]) == ("U0MEMBER", "U0OWNER"), (
        "where reports go stays; who the owner is comes from the user token")
    assert slack_calls(world) == [("slack_auth",), ("slack_auth",),
                                  ("slack_post", "U0MEMBER", TEST_MESSAGE)]


def test_a_token_that_replaces_another_says_a_running_keryx_needs_a_restart(make_ctx, world):
    ctx = already_set_up(make_ctx, channel="U0MEMBER")
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "xoxb-new"), (USER_TOKEN, "xoxp-1"), (TEST, False)]

    section.run_section(ctx)

    assert ("A Keryx that started with the old token keeps it until `keryx restart`."
            in ctx.ui.lines("note"))


# --- slack: the access level comes first ---------------------------------------------------


def test_the_level_is_asked_first_and_says_what_keryx_will_see_before_the_link(make_ctx, world):
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"),
                    (TEST, True)])

    section.run_section(ctx)

    assert ctx.ui.done() and asked(ctx)[:2] == ["Which plugins", LEVEL]
    choices = ctx.ui.choices["What may Keryx see in your Slack?"]
    assert [c.value for c in choices] == ["announce", "converse", "read", "write"]
    assert "reads nothing" in choices[0].hint
    assert "without the PIN" in choices[1].hint and "reads Slack as you" in choices[2].hint
    meant = ctx.ui.said.index(("note", meaning(Access.CONVERSE, LYRA)))
    assert meant < [kind for kind, _ in ctx.ui.said].index("link")
    assert ctx.ui.lines("link") == [new_app_url(Access.CONVERSE, assistant="Lyra")]
    assert "4. Under **Basic Information**, **App-Level Tokens**" in ctx.ui.lines("markdown")[0]


def test_the_wizards_slack_step_follows_the_assistants_name(make_ctx, world):
    """The link's manifest, the options, what the level means, the steps, the owner line
    and the test message: an assistant called Jarvis has a Jarvis app, mentioned @Jarvis."""
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, True)])
    ctx.save({"ASSISTANT_NAME": "Jarvis"})

    section.run_section(ctx)

    assert ctx.ui.lines("link") == [new_app_url(Access.CONVERSE, assistant="Jarvis")]
    choices = ctx.ui.choices["What may Keryx see in your Slack?"]
    assert "every message you send the Jarvis app itself" in choices[1].hint
    assert meaning(Access.CONVERSE, "Jarvis") in ctx.ui.lines("note")
    assert "named Jarvis, after your assistant" in ctx.ui.lines("markdown")[0]
    assert OWNER_LINE.format(who="U0OWNER", app="Jarvis") in ctx.ui.lines("note")
    assert LISTEN_NOTE.format(app="Jarvis") in ctx.ui.lines("note")
    assert slack_calls(world)[-1] == (
        "slack_post", "U0OWNER", said_to_test(Access.CONVERSE, "Jarvis"))
    assert not any("@Keryx" in text or "Keryx app" in text for _, text in ctx.ui.said)


def test_converse_is_three_tokens_each_checked_and_the_owner_is_whose_the_user_token_is(
    make_ctx, world
):
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, True)])

    section.run_section(ctx)

    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1",
                                        "SLACK_USER_TOKEN": "xoxp-1"}
    saved = toml(ctx, "slack_report")
    assert (saved["access"], saved["owner_id"], saved["channel_id"]) == (
        "converse", "U0OWNER", "U0OWNER"
    )
    assert (saved["team"], saved["team_id"]) == ("Acme", "T0ACME")
    assert slack_calls(world) == [
        ("slack_auth",), ("slack_open",), ("slack_auth",),
        ("slack_post", "U0OWNER", said_to_test(Access.CONVERSE, LYRA)),
    ]
    assert LISTEN_NOTE.format(app=LYRA) in ctx.ui.lines("note")
    assert OWNER_LINE.format(who="U0OWNER", app=LYRA) in ctx.ui.lines("note")
    assert not any(secret in text for secret in ("xapp-1", "xoxb-1", "xoxp-1")
                   for _, text in ctx.ui.said)


def test_an_app_token_slack_refuses_is_asked_for_again_and_says_what_it_needs(make_ctx, world):
    world.slack_connections = [Connection(error="invalid_auth"), Connection(url="wss://x/?t=1")]
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-wrong"),
                    (APP_TOKEN, "xapp-right"), (USER_TOKEN, "xoxp-1"), (TEST, False)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert any("connections:write" in line and "(invalid_auth)" in line
               for line in ctx.ui.lines("error"))
    assert ConfigStore()._secrets()["SLACK_APP_TOKEN"] == "xapp-right"


def test_above_announce_a_token_that_could_not_be_checked_is_never_saved_unchecked(
    make_ctx, world
):
    """Every message is held to the workspace Slack names, so Slack has to have named it."""
    world.slack_connections = [Connection(error="http_503"), Connection(url="wss://x/?t=1")]
    world.slack_workspaces = [Workspace(error="http_503"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER")]
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (TOKEN, "xoxb-1"),
                    (APP_TOKEN, "xapp-1"), (APP_TOKEN, "xapp-1"), (USER_TOKEN, "xoxp-1"),
                    (TEST, False)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert "Save it without checking" not in " ".join(message for _, message in ctx.ui.asked)
    assert "At this level it has to be checked: try again when Slack answers." in (
        ctx.ui.lines("note")
    )
    assert any("At this level it has to be" in line for line in ctx.ui.lines("error"))
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1",
                                        "SLACK_USER_TOKEN": "xoxp-1"}


def test_a_level_abandoned_half_way_has_saved_none_of_it(make_ctx, world):
    """All of its tokens or none: the bot token is not saved while the app token is not."""
    world.slack_connections = [Connection(error="invalid_auth")]
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-wrong")])

    with pytest.raises(AssertionError, match="unscripted secret"):
        section.run_section(ctx)

    assert ConfigStore()._secrets() == {} and not on(ctx, "slack_report")
    assert not plugins.config_path(ctx.settings, "slack_report").exists()


def test_a_slack_file_that_cannot_be_written_has_still_saved_the_tokens_and_says_so(
    make_ctx, world, monkeypatch
):
    """As on the command line: the tokens go first, in one write, and the error is shown."""
    def refuse(settings, name, values):
        raise plugins.PluginConfigError("slack_report: channel_id must be a bare word")

    monkeypatch.setattr(plugins, "write_config", refuse)
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1")])

    section.run_section(ctx)

    assert ctx.ui.done() and ctx.ui.lines("error")
    assert "saved SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_USER_TOKEN" in ctx.ui.lines("success")


def test_an_app_token_must_be_one(make_ctx):
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xoxb-again")])

    with pytest.raises(AssertionError, match="An app-level token starts xapp-"):
        section.run_section(ctx)


def test_read_is_three_tokens_and_the_user_token_says_who_and_where(make_ctx, world):
    world.slack_workspaces = [Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM")]
    ctx = make_ctx([SLACK, (LEVEL, "read"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False), (ANSWERS, False)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ConfigStore()._secrets() == {
        "SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1", "SLACK_USER_TOKEN": "xoxp-1",
    }
    saved = toml(ctx, "slack_report")
    assert (saved["access"], saved["owner_id"], saved["channel_id"]) == ("read", "U0SAM", "U0SAM")
    assert READ_NOTE in ctx.ui.lines("note")
    assert "it is yours: you are U0SAM there" in ctx.ui.lines("success")
    assert slack_calls(world) == [("slack_auth",), ("slack_open",), ("slack_auth",)]
    assert ("5. Under **OAuth & Permissions**, the same page as the bot token, copy the "
            "**User OAuth Token**") in ctx.ui.lines("markdown")[0]


def test_a_user_token_from_another_workspace_is_asked_for_again_and_never_saved(make_ctx, world):
    world.slack_workspaces = [
        Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
        Workspace(team="Other", team_id="T0OTHER", user_id="U0ELSE"),
        Workspace(error="http_503"),
        Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"),
    ]
    ctx = make_ctx([SLACK, (LEVEL, "read"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-other"), (USER_TOKEN, "xoxp-down"),
                    (USER_TOKEN, "xoxp-mine"), (TEST, False), (ANSWERS, False)])

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.ui.lines("error") == [
        "That user token cannot be used: it belongs to another workspace than the bot "
        "token (Other)",
        "That user token cannot be used: it could not be checked (Slack answered HTTP 503)",
    ]
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-mine"
    assert toml(ctx, "slack_report")["owner_id"] == "U0SAM"


def test_a_channel_on_file_as_where_reports_go_stays_and_never_says_who_the_owner_is(
    make_ctx, world
):
    """Set with `keryx plugins install slack_report --set channel_id=…`: the one way
    reports go anywhere but to the owner."""
    ctx = conversing(make_ctx)
    plugins.write_config(ctx.settings, "slack_report", {"channel_id": "C0CHANNEL"})
    ctx.ui.answers = [SLACK, (LEVEL, DEFAULT), (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT),
                      (USER_TOKEN, "xoxp-1"), (TEST, False)]

    section.run_section(ctx)

    saved = toml(ctx, "slack_report")
    assert ctx.ui.done() and (saved["channel_id"], saved["owner_id"]) == ("C0CHANNEL", "U0OWNER")
    assert any(line.startswith("Reports go to C0CHANNEL, which is not your direct messages")
               for line in ctx.ui.lines("note"))


def test_raising_the_level_shows_the_manifest_for_the_app_there_is_and_no_new_app_link(
    make_ctx, world
):
    ctx = already_set_up(make_ctx, channel="U0COLLEAGUE")
    ctx.ui.answers = [SLACK, (LEVEL, "converse"), (TOKEN, DEFAULT), (APP_TOKEN, "xapp-1"),
                      (USER_TOKEN, "xoxp-1"), (TEST, False)]

    section.run_section(ctx)

    assert ctx.ui.done() and not ctx.ui.lines("link")
    assert ("Slack is connected at the announce level, so the app you have is changed, not "
            "made again.") in ctx.ui.lines("note")
    steps = ctx.ui.lines("markdown")[0]
    assert "Do not create a second one." in steps and "**Reinstall to Workspace**" in steps
    assert "Bot User OAuth Token" not in steps and "App-Level Tokens" in steps
    assert "**User OAuth Token**" in steps
    title, _, body = ctx.ui.lines("panel")[0].partition("\n")
    assert title == "The manifest"
    assert json.loads(body) == app_manifest(Access.CONVERSE, assistant="Lyra")
    # The install is from before levels, so Slack is asked once which workspace it is;
    # and the user token says who they are, which where reports go never does.
    assert slack_calls(world) == [("slack_auth",), ("slack_open",), ("slack_auth",)]
    saved = toml(ctx, "slack_report")
    assert (saved["access"], saved["owner_id"], saved["channel_id"]) == (
        "converse", "U0OWNER", "U0COLLEAGUE")
    assert saved["team"] == "Acme"
    assert LISTEN_NOTE.format(app=LYRA) in ctx.ui.lines("note")


def test_a_kept_bot_token_with_no_workspace_on_file_still_holds_a_user_token_to_its_own(
    make_ctx, world
):
    """An install from before levels: its workspace is asked for, so the check has teeth."""
    world.slack_workspaces = [
        Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
        Workspace(team="Other", team_id="T0OTHER", user_id="U0ELSE"),
        Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM"),
    ]
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    ctx.ui.answers = [SLACK, (LEVEL, "read"), (TOKEN, DEFAULT), (APP_TOKEN, "xapp-1"),
                      (USER_TOKEN, "xoxp-other"), (USER_TOKEN, "xoxp-mine"),
                      (TEST, False), (ANSWERS, False)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.ui.lines("error") == [
        "That user token cannot be used: it belongs to another workspace than the bot "
        "token (Other)",
    ]
    saved = toml(ctx, "slack_report")
    assert (saved["team_id"], saved["owner_id"]) == ("T0ACME", "U0SAM")
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-mine"


def test_a_workspace_slack_cannot_name_right_now_is_not_raised_above_announce(make_ctx, world):
    world.slack_workspaces = [Workspace(error="http_503")]
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    ctx.ui.answers = [SLACK, (LEVEL, "converse"), (TOKEN, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert any("did not say which workspace this is" in line and "Nothing was saved" in line
               for line in ctx.ui.lines("error"))
    assert toml(ctx, "slack_report").get("access", "announce") == "announce"
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-old"}


# --- slack: where to send is never whose messages are acted on ----------------------------


def conversing(make_ctx, **more):
    """An install at converse whose owner is U0OWNER, in the workspace the fakes name."""
    ConfigStore().set({"SLACK_APP_TOKEN": "xapp-old", **more})
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    plugins.write_config(ctx.settings, "slack_report", {
        "access": "read" if more else "converse", "owner_id": "U0OWNER", "team": "Acme",
        "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    return ctx


def test_the_wizard_refuses_another_workspaces_bot_token_as_the_command_line_does(
    make_ctx, world
):
    """One owner, fixed at connect: a token from another workspace would bring another
    owner with it, so it is refused in the command line's own sentence and nothing more
    is asked or saved."""
    ctx = conversing(make_ctx)
    before = toml(ctx, "slack_report")
    world.slack_workspaces = [Workspace(team="Other Co", team_id="T0OTHER", user_id="U0OTHERBOT")]
    ctx.ui.answers = [SLACK, (LEVEL, DEFAULT), (TOKEN, "xoxb-other-workspace")]

    section.run_section(ctx)

    assert ctx.ui.done(), "no further question: not the app token, not the user token"
    assert ctx.ui.lines("error") == [
        "That bot token is for Other Co, and Acme is connected here: `keryx auth logout "
        "slack` first, then log in again. Nothing was saved."]
    assert toml(ctx, "slack_report") == before
    assert ConfigStore()._secrets()["SLACK_BOT_TOKEN"] != "xoxb-other-workspace"


def test_at_read_another_workspaces_tokens_never_name_a_new_owner(make_ctx, world):
    ctx = conversing(make_ctx, SLACK_USER_TOKEN="xoxp-old")
    world.slack_workspaces = [
        Workspace(team="Other Co", team_id="T0OTHER", user_id="U0OTHERBOT"),
        Workspace(team="Other Co", team_id="T0OTHER", user_id="U0EVE"),
    ]
    ctx.ui.answers = [SLACK, (LEVEL, DEFAULT), (TOKEN, "xoxb-other-workspace")]

    section.run_section(ctx)

    after = toml(ctx, "slack_report")
    assert ctx.ui.done() and (after["owner_id"], after["team_id"]) == ("U0OWNER", "T0ACME")
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-old"


def test_the_wizard_and_the_command_line_ask_one_function_about_another_workspace():
    """So that neither can let through what the other refuses."""
    import inspect

    from keryx.setup import plugins as wizard
    from keryx.setup import slack as command_line

    assert "another_workspace(" in inspect.getsource(wizard.run_slack)
    assert "another_workspace(" in inspect.getsource(command_line.connect)


def test_the_first_owner_is_named_in_a_line_of_its_own(make_ctx, world):
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False)])

    section.run_section(ctx)

    assert OWNER_LINE.format(who="U0OWNER", app=LYRA) in ctx.ui.lines("note")


def test_the_owner_is_recorded_at_announce_too_so_raising_the_level_changes_nothing(
    make_ctx, world
):
    """Whoever the user token belongs to, at every level: nothing typed is acted on at
    announce, and the owner is already who they will be."""
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, False)])
    section.run_section(ctx)
    assert toml(ctx, "slack_report")["owner_id"] == "U0OWNER"
    assert not any("are acted on there" in line for line in ctx.ui.lines("note"))
    assert OWNER_NOTED.format(who="U0OWNER") in ctx.ui.lines("note")

    world.slack_user = Workspace(team="Acme", team_id="T0ACME", user_id="U0ELSE")
    ctx.ui.answers = [SLACK, (LEVEL, "converse"), (TOKEN, DEFAULT), (APP_TOKEN, "xapp-1"),
                      (USER_TOKEN, "xoxp-theirs")]
    with pytest.raises(AssertionError, match="unscripted secret"):
        section.run_section(ctx)  # somebody else's token is asked for again, never taken

    assert toml(ctx, "slack_report")["owner_id"] == "U0OWNER"
    assert any("it belongs to U0ELSE" in line for line in ctx.ui.lines("error"))


def test_a_user_token_that_is_not_the_owners_on_file_is_asked_for_again(make_ctx, world):
    """A token is never how the owner changes: only logout and a new login is."""
    world.slack_workspaces = [
        Workspace(team="Acme", team_id="T0ACME", user_id="U0ELSE"),
        Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER"),
    ]
    ctx = conversing(make_ctx)
    ctx.ui.answers = [SLACK, (LEVEL, "read"), (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT),
                      (USER_TOKEN, "xoxp-theirs"), (USER_TOKEN, "xoxp-mine"),
                      (TEST, False), (ANSWERS, False)]

    section.run_section(ctx)

    assert ctx.ui.done()
    assert ctx.ui.lines("error") == [
        "That user token cannot be used: it belongs to U0ELSE, and the member whose "
        "messages Keryx acts on is U0OWNER: paste your own (`keryx auth logout slack` and a "
        "new login connects as somebody else)"
    ]
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-mine"
    assert toml(ctx, "slack_report")["owner_id"] == "U0OWNER"


def test_a_token_typed_again_after_going_back_is_not_shown_to_slack_twice(make_ctx, world):
    """Nothing is on file to recognise it by, since nothing is saved until the end."""
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, Back()), (USER_TOKEN, Back()),
                    (APP_TOKEN, Back()), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert slack_calls(world) == [("slack_auth",), ("slack_open",), ("slack_auth",)]
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1",
                                        "SLACK_USER_TOKEN": "xoxp-1"}


def test_a_lower_level_forgets_the_tokens_it_no_longer_uses_and_asks_slack_nothing(
    make_ctx, world
):
    ConfigStore().set({"SLACK_APP_TOKEN": "xapp-old", "SLACK_USER_TOKEN": "xoxp-old"})
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    plugins.write_config(ctx.settings, "slack_report", {"access": "read", "owner_id": "U0OWNER"})
    ctx.ui.answers = [SLACK, (LEVEL, "announce"), (TOKEN, DEFAULT), (TEST, False)]

    section.run_section(ctx)

    # The user token that could read Slack as them is not kept below read, and is not
    # asked for: the owner on file stands.
    assert ctx.ui.done() and slack_calls(world) == []
    assert toml(ctx, "slack_report")["owner_id"] == "U0OWNER"
    assert ConfigStore()._secrets() == {"SLACK_BOT_TOKEN": "xoxb-old"}
    assert toml(ctx, "slack_report")["access"] == "announce"
    assert ("SLACK_APP_TOKEN and SLACK_USER_TOKEN are forgotten: the announce level does not "
            "use them.") in ctx.ui.lines("note")
    assert not ctx.ui.lines("link") and not ctx.ui.lines("panel")


def test_a_level_already_set_up_keeps_every_token_and_asks_slack_nothing(make_ctx, world):
    ConfigStore().set({"SLACK_APP_TOKEN": "xapp-old", "SLACK_USER_TOKEN": "xoxp-old"})
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    plugins.write_config(ctx.settings, "slack_report", {
        "access": "read", "owner_id": "U0OWNER", "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    ctx.ui.answers = [SLACK, (LEVEL, DEFAULT), (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT),
                      (USER_TOKEN, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done() and slack_calls(world) == []
    assert "Nothing changed, so no test message was sent." in ctx.ui.lines("note")
    assert toml(ctx, "slack_report")["access"] == "read"


def test_a_kept_user_token_with_no_member_on_file_is_asked_of_slack_once(make_ctx, world):
    world.slack_user = Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM")
    ConfigStore().set({"SLACK_APP_TOKEN": "xapp-old", "SLACK_USER_TOKEN": "xoxp-old"})
    ctx = already_set_up(make_ctx, channel="C0CHANNEL")
    plugins.write_config(ctx.settings, "slack_report", {
        "access": "read", "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    ctx.ui.answers = [SLACK, (LEVEL, DEFAULT), (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT),
                      (USER_TOKEN, DEFAULT), (TEST, False)]

    section.run_section(ctx)

    assert slack_calls(world) == [("slack_auth",)]
    assert toml(ctx, "slack_report")["owner_id"] == "U0SAM"
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-old"


def test_esc_past_the_app_token_does_not_ask_slack_about_either_token_twice(make_ctx, world):
    ctx = make_ctx([SLACK, (LEVEL, "converse"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, Back()), (APP_TOKEN, DEFAULT), (USER_TOKEN, "xoxp-1"),
                    (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", LEVEL, TOKEN, APP_TOKEN, USER_TOKEN, APP_TOKEN,
                          USER_TOKEN, TEST, NEXT]
    assert slack_calls(world) == [("slack_auth",), ("slack_open",), ("slack_auth",)]
    assert toml(ctx, "slack_report")["access"] == "converse"


def test_the_first_time_it_says_how_a_second_workspace_is_added(make_ctx, world):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, False)])

    section.run_section(ctx)

    assert any("keryx auth login slack --workspace NAME" in line for line in ctx.ui.lines("note"))


def test_raising_to_read_offers_slack_answers_once_and_turns_them_on(make_ctx, world):
    world.slack_workspaces = [Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM")]
    ctx = make_ctx([SLACK, (LEVEL, "read"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False), (ANSWERS, DEFAULT)])

    section.run_section(ctx)

    assert ctx.ui.done() and on(ctx, "slack_read")
    assert "slack_read is on, from the next call" in ctx.ui.lines("success")
    assert any("Keryx reads Slack as you only when you ask about messages" in line
               for line in ctx.ui.lines("note"))


SENDING = "Turn on sending as you"


def test_raising_to_write_offers_what_that_level_unlocks_and_sending_is_not_a_yes_by_default(
    make_ctx, world
):
    world.slack_workspaces = [
        Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
        Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM",
                  scopes=(*slack_api.USER_SCOPES, *slack_api.WRITE_USER_SCOPES)),
    ]
    ctx = make_ctx([SLACK, (LEVEL, "write"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False),
                    (ANSWERS, DEFAULT), (SENDING, DEFAULT)])

    section.run_section(ctx)

    assert ctx.ui.done() and toml(ctx, "slack_report")["access"] == "write"
    assert on(ctx, "slack_read") and not on(ctx, "slack_message"), "reading yes, sending no"
    notes = ctx.ui.lines("note")
    assert any("send messages as you, in your name, to other people" in line for line in notes)
    assert any("other people read it as written by you" in line for line in notes)


def test_saying_yes_to_sending_turns_it_on(make_ctx, world):
    world.slack_workspaces = [Workspace(team="Acme", team_id="T0ACME", user_id="U0KERYX"),
                              Workspace(team="Acme", team_id="T0ACME", user_id="U0SAM")]
    ctx = make_ctx([SLACK, (LEVEL, "write"), (TOKEN, "xoxb-1"), (APP_TOKEN, "xapp-1"),
                    (USER_TOKEN, "xoxp-1"), (TEST, False),
                    (ANSWERS, False), (SENDING, True)])

    section.run_section(ctx)

    assert ctx.ui.done() and on(ctx, "slack_message") and not on(ctx, "slack_read")
    assert "slack_message is on, from the next call" in ctx.ui.lines("success")


def test_raising_read_to_write_checks_the_kept_user_token_again_for_what_it_may_do(
    make_ctx, world
):
    """The app has to be reinstalled first: a token that may not post yet is asked for
    again, and nothing is saved at write until one may."""
    world.slack_workspaces = [
        Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                  scopes=slack_api.USER_SCOPES),
        Workspace(team="Acme", team_id="T0ACME", user_id="U0OWNER",
                  scopes=(*slack_api.USER_SCOPES, *slack_api.WRITE_USER_SCOPES)),
    ]
    ctx = conversing(make_ctx, SLACK_USER_TOKEN="xoxp-old")
    ctx.ui.answers = [SLACK, (LEVEL, "write"), (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT),
                      (USER_TOKEN, DEFAULT), (USER_TOKEN, "xoxp-new"),
                      (TEST, False), (SENDING, False)]

    section.run_section(ctx)

    assert ctx.ui.done() and toml(ctx, "slack_report")["access"] == "write"
    assert any("it may not yet do what the write level needs (it lacks chat:write, im:write)"
               in line for line in ctx.ui.lines("error"))
    assert ConfigStore()._secrets()["SLACK_USER_TOKEN"] == "xoxp-new"
    assert not any(ANSWERS in message for _, message in ctx.ui.asked), "read was on before"


def test_sending_ticked_without_a_workspace_at_write_is_left_off_with_what_to_do(
    make_ctx, world
):
    ctx = make_ctx([("Which plugins", ["slack_message"])])

    section.run_section(ctx)

    assert ctx.ui.done() and not on(ctx, "slack_message")
    assert any("needs a workspace at the write level" in line
               and "keryx auth login slack --access write" in line
               for line in ctx.ui.lines("note"))


def test_slack_answers_ticked_without_a_workspace_at_read_are_left_off_with_what_to_do(
    make_ctx, world
):
    ctx = make_ctx([("Which plugins", ["slack_read"])])

    section.run_section(ctx)

    assert ctx.ui.done() and not on(ctx, "slack_read")
    assert any("needs a workspace at the read level" in line
               and "keryx auth login slack --access read" in line
               for line in ctx.ui.lines("note"))


def test_slack_answers_ticked_with_a_workspace_at_read_are_turned_on_and_asked_nothing(
    make_ctx, world
):
    ConfigStore().set({"SLACK_APP_TOKEN": "xapp-old", "SLACK_USER_TOKEN": "xoxp-old"})
    ctx = already_set_up(make_ctx, channel="U0OWNER")
    plugins.write_config(ctx.settings, "slack_report", {
        "access": "read", "owner_id": "U0OWNER", "team_id": "T0ACME", "bot_user_id": "U0KERYX",
    })
    ConfigStore().mark_walked("plugins")
    ctx.ui.answers = [("Which plugins", ["slack_report", "slack_read"]), (LEVEL, DEFAULT),
                      (TOKEN, DEFAULT), (APP_TOKEN, DEFAULT), (USER_TOKEN, DEFAULT)]

    section.run_section(ctx)

    assert ctx.ui.done() and on(ctx, "slack_read") and slack_calls(world) == []


# --- slack, beside an MCP server that carries it ------------------------------------------


@pytest.fixture
def chat_server(make_ctx):
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "slack_report", {"mcp_server": "chat"})
    return ctx


def test_above_announce_an_mcp_servers_token_will_not_do(chat_server):
    """Listening needs the app's own tokens: the server's token is another app's."""
    ctx = chat_server
    ctx.ui.answers = [SLACK, (LEVEL, "converse"), (TOKEN, "")]

    with pytest.raises(AssertionError, match="needs the token of its own app"):
        section.run_section(ctx)


def test_an_mcp_server_that_carries_the_route_needs_no_token(chat_server, world):
    ctx = chat_server
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "")]

    section.run_section(ctx)

    # No token of its own, so no user token is asked for either: there is no app of
    # Keryx's to have issued one.
    assert ctx.ui.done()
    assert any("blank" in line and "chat MCP server" in line for line in ctx.ui.lines("note"))
    assert ConfigStore()._secrets() == {} and slack_calls(world) == []
    assert on(ctx, "slack_report")
    assert ("No test message was sent: the token and the channel are the chat MCP server's."
            in ctx.ui.lines("note"))


def test_a_token_beside_an_mcp_server_says_what_that_does_to_the_route(chat_server, world):
    ctx = chat_server
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, DEFAULT)]

    section.run_section(ctx)

    saved = toml(ctx, "slack_report")
    assert (saved["channel_id"], saved["mcp_server"]) == ("U0OWNER", "chat")
    assert any("not with the chat MCP server's" in line and "subagents still" in line
               for line in ctx.ui.lines("note"))


def test_a_kept_token_beside_an_mcp_server_may_still_use_the_servers_channel(make_ctx, world):
    """An install that already ran this way is asked nothing new and changed in nothing."""
    ConfigStore().set({"SLACK_BOT_TOKEN": "xoxb-old"})
    ctx = make_ctx([])
    plugins.write_config(ctx.settings, "slack_report", {"mcp_server": "chat"})
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, DEFAULT), (USER_TOKEN, "")]

    section.run_section(ctx)

    assert ctx.ui.done() and slack_calls(world) == []
    assert toml(ctx, "slack_report")["channel_id"] == ""
    assert ("No test message was sent: the channel is the chat MCP server's."
            in ctx.ui.lines("note"))


def test_a_slack_file_that_cannot_be_written_sends_no_test(make_ctx, world, monkeypatch):
    def refuse(settings, name, values):
        raise plugins.PluginConfigError("slack_report: channel_id must be a bare word")

    monkeypatch.setattr(plugins, "write_config", refuse)
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1")])

    section.run_section(ctx)

    assert ctx.ui.done() and ctx.ui.lines("error")
    assert not any(call[0] == "slack_post" for call in world.calls)


# --- slack: going back and forward, before and after the step has saved --------------------
#
# The step saves as it goes, so a second run finds a token and a destination the first did
# not. Its questions must be the same ones all the same, or Esc lands somewhere new.


def test_esc_before_anything_is_written_does_not_ask_slack_about_the_token_twice(make_ctx, world):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, Back()), (TOKEN, DEFAULT),
                    (USER_TOKEN, "xoxp-1"), (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert slack_calls(world) == [("slack_auth",), ("slack_auth",)]
    assert toml(ctx, "slack_report")["channel_id"] == "U0OWNER"


def test_esc_at_the_test_offer_goes_back_to_the_user_token_and_offers_the_test_again(
    make_ctx, world
):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, Back()),
                    (USER_TOKEN, DEFAULT), (TEST, True), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", LEVEL, TOKEN, USER_TOKEN, TEST, USER_TOKEN, TEST,
                          NEXT]
    assert ("slack_post", "U0OWNER", TEST_MESSAGE) in world.calls
    assert slack_calls(world).count(("slack_auth",)) == 2, "each token shown to Slack once"


def test_esc_from_the_next_section_lands_on_the_test_offer(make_ctx, world):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (NEXT, Back()), (TEST, False), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx)[-3:] == [NEXT, TEST, NEXT]
    # Nothing was asked of Slack a second time: each token once, one message.
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_auth", "slack_post"]


def test_esc_from_the_next_section_after_an_unchanged_walk_goes_back_a_question(make_ctx, world):
    """To the last one it asked; and going back is a review, which offers the test too."""
    ctx = already_set_up(make_ctx)
    ctx.ui.answers = [SLACK, ANNOUNCE, (TOKEN, DEFAULT), (USER_TOKEN, ""), (NEXT, Back()),
                      (USER_TOKEN, ""), (TEST, DEFAULT), (NEXT, True)]

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", LEVEL, TOKEN, USER_TOKEN, NEXT, USER_TOKEN, TEST,
                          NEXT]
    assert slack_calls(world) == []
    assert toml(ctx, "slack_report")["channel_id"] == "D123"


def test_tab_after_the_step_has_saved_stops_at_nothing_of_slacks(make_ctx, world):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (NEXT, Back()), (TEST, Forward()), (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx) == ["Which plugins", LEVEL, TOKEN, USER_TOKEN, TEST, NEXT, TEST, NEXT]
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_auth", "slack_post"]


def test_tab_from_the_first_question_passes_a_slack_step_already_walked(make_ctx, world):
    ctx = make_ctx([SLACK, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (NEXT, Back()), (TEST, Back()), (USER_TOKEN, Back()), (TOKEN, Forward()),
                    (NEXT, True)])

    assert slack_walk(ctx)

    assert asked(ctx)[-2:] == [TOKEN, NEXT]
    assert [call[0] for call in slack_calls(world)] == ["slack_auth", "slack_auth", "slack_post"]


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
        "slack_report", "check_email", "check_calendar", "find_document",
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
        ("Sign in to Google?", DEFAULT)])

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
        ("And then?", "done")])

    assert wizard.run_walk(ctx, walk, {}) is True

    assert ctx.ui.done()
    assert [call[0] for call in world.calls] == ["google_browser"]
    assert on(ctx, "check_email") and on(ctx, "check_calendar")


# --- Slack's step and the Google plugins in one walk -------------------------------------
#
# The two came from two branches. Slack's step asks the same three questions whatever it
# has saved, so that a replay finds them; the Google step asks one question once and none
# after the sign-in is kept. Walked together, going back must reach neither Slack nor
# Google a second time.

BOTH = ("Which plugins", ["slack_report", "check_email", "check_calendar", "find_document"])
SIGN_IN = "Sign in to Google?"


def outside_calls(world) -> list[str]:
    return [call[0] for call in world.calls if call[0].startswith(("slack", "google"))]


def test_slack_and_the_google_plugins_walk_together_each_asked_its_own_questions_once(
    make_ctx, world
):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([BOTH, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (SIGN_IN, DEFAULT), (NEXT, True)])

    assert slack_walk(ctx)

    assert ctx.ui.done()
    assert outside_calls(world) == ["slack_auth", "slack_auth", "slack_post", "google_browser"]
    assert all(on(ctx, name) for name in BOTH[1])
    assert toml(ctx, "slack_report")["channel_id"] == "U0OWNER"


def test_esc_after_both_reaches_neither_slack_nor_google_a_second_time(make_ctx, world):
    """Esc lands on the last question the section asked, the Google sign-in. By then the
    sign-in is kept, so that question is no longer asked: the replay answers Slack's three
    from the record, runs none of its calls again, and the walk goes on."""
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([BOTH, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (SIGN_IN, DEFAULT), (NEXT, Back()), (NEXT, True)])

    assert slack_walk(ctx)

    assert ctx.ui.done()
    assert outside_calls(world) == ["slack_auth", "slack_auth", "slack_post", "google_browser"]
    assert [message.split("?")[0] for _, message in ctx.ui.asked][-2:] == [NEXT, NEXT]
    assert all(on(ctx, name) for name in BOTH[1])


def test_esc_into_slacks_step_with_google_behind_it_asks_slack_again_and_google_nothing(
    make_ctx, world
):
    """Back twice from the next section: past the sign-in, which is gone, to Slack's test
    offer. Slack's questions are still the ones recorded; Google is not signed in again."""
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([BOTH, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (SIGN_IN, Back()), (TEST, False), (SIGN_IN, DEFAULT), (NEXT, True)])

    assert slack_walk(ctx)

    assert ctx.ui.done()
    assert outside_calls(world).count("google_browser") == 1
    assert outside_calls(world).count("slack_auth") == 2, "each of the two tokens, once"
    assert all(on(ctx, name) for name in BOTH[1])


def test_tab_through_both_once_walked_stops_at_nothing_of_either(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([BOTH, ANNOUNCE, (TOKEN, "xoxb-1"), (USER_TOKEN, "xoxp-1"), (TEST, True),
                    (SIGN_IN, DEFAULT), (NEXT, Back()), (NEXT, Back()), (TEST, Forward()),
                    (NEXT, True)])

    assert slack_walk(ctx)

    assert ctx.ui.done()
    assert outside_calls(world) == ["slack_auth", "slack_auth", "slack_post", "google_browser"]


def test_the_checklist_ticks_slack_email_and_what_google_has_connected(make_ctx):
    ctx = make_ctx([("Which plugins", [])])
    google_signed_in(ctx, ("gmail", "calendar", "drive"))

    section.run_section(ctx)

    choices = {c.value: c for c in ctx.ui.choices["Which plugins should Keryx have?"]}
    assert [name for name, c in choices.items() if c.checked] == [
        "slack_report", "check_email", "check_calendar", "find_document",
    ]
    assert all("·" not in c.hint for c in choices.values())


def test_a_sign_in_turned_down_is_not_offered_again_for_the_next_plugin(make_ctx, world):
    ConfigStore().set({"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "cs"})
    ctx = make_ctx([
        ("Which plugins", ["check_email", "check_calendar"]),
        ("Sign in to Google?", "skip")])

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
