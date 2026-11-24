"""Tests for sending Slack messages. No network: the transport is injected."""

import http.client
import inspect
import io
import json
import re
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
from slack_sets import READ, READS, REQUESTS, SELECTIONS, SEND, named

from keryx.integrations import slack
from keryx.integrations.slack import (
    BASE,
    EVERYTHING,
    FileRefused,
    LocalFile,
    Permission,
    Posted,
    SlackFiles,
    SlackResult,
    SlackWebApi,
    Uploaded,
    add_reaction,
    app_manifest,
    auth_test,
    is_dm,
    is_ts,
    local_file,
    local_files,
    manifest_json,
    markdown_pieces,
    new_app_url,
    open_connection,
    plain_text,
    post_text,
    slack_credentials,
    token_kind,
    token_kinds,
    ts_key,
)

#: A made-up server name: which MCP server carries Slack is the owner's setting, never ours.
SERVER = "team-slack"


def write_config(path, *, token="xoxb-test", channel="D0TEST", server=SERVER):
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    server: {"env": {"SLACK_BOT_TOKEN": token, "SLACK_CHANNEL_ID": channel}}
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_an_explicit_pair_wins(tmp_path):
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(
        "xoxb-mine", "D0MINE", server=SERVER, config_path=config
    ) == ("xoxb-mine", "D0MINE")


def test_an_explicit_pair_needs_no_server_at_all(tmp_path):
    assert slack_credentials("xoxb-mine", "D0MINE", config_path=tmp_path / "missing.json") == (
        "xoxb-mine",
        "D0MINE",
    )


def test_credentials_fall_back_to_the_named_mcp_server(tmp_path):
    """One Slack app, configured once, wherever it is used from."""
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(None, None, server=SERVER, config_path=config) == (
        "xoxb-test",
        "D0TEST",
    )


def test_half_a_pair_is_completed_from_the_config(tmp_path):
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials("xoxb-mine", None, server=SERVER, config_path=config) == (
        "xoxb-mine",
        "D0TEST",
    )


def test_without_a_named_server_nothing_is_read_from_the_config(tmp_path):
    """No server name is built in: an unset `SLACK_MCP_SERVER` means no fallback at all."""
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(None, None, config_path=config) is None
    assert slack_credentials("xoxb-mine", None, server=None, config_path=config) is None


def test_without_a_path_the_config_is_the_claude_clis_own(monkeypatch, tmp_path):
    """`~/.claude.json`, or inside `CLAUDE_CONFIG_DIR` when that moves it."""
    write_config(tmp_path / ".claude.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert slack_credentials(None, None, server=SERVER) == ("xoxb-test", "D0TEST")

    moved = tmp_path / "moved"
    moved.mkdir()
    write_config(moved / ".claude.json", token="xoxb-moved")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    assert slack_credentials(None, None, server=SERVER) == ("xoxb-moved", "D0TEST")


def test_no_slack_anywhere_is_not_an_error(tmp_path):
    missing = tmp_path / "missing.json"
    assert slack_credentials(None, None, server=SERVER, config_path=missing) is None

    unrelated = tmp_path / "other.json"
    write_config(unrelated, server="something-else")
    assert slack_credentials(None, None, server=SERVER, config_path=unrelated) is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert slack_credentials(None, None, server=SERVER, config_path=broken) is None


async def test_send_posts_the_message_to_the_channel():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, token: str) -> dict:
        posted.append({"url": url, "payload": payload, "token": token})
        return {"ok": True}

    sender = SlackWebApi("xoxb-test", "D0TEST", post=fake_post)

    assert await sender.send("the **tests** pass") is True
    assert posted[0]["payload"] == {
        "channel": "D0TEST",
        "text": "the **tests** pass",
        "blocks": [{"type": "markdown", "text": "the **tests** pass"}],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert posted[0]["token"] == "xoxb-test"


async def test_a_long_message_goes_as_several_with_the_same_notification_line():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, token: str) -> dict:
        posted.append(payload)
        return {"ok": True}

    long = "\n\n".join(["x" * 7000, "y" * 7000])

    assert await SlackWebApi("t", "D", post=fake_post).send(long, fallback="Task 3") is True
    assert [p["blocks"][0]["text"] for p in posted] == ["x" * 7000, "y" * 7000]
    assert {p["text"] for p in posted} == {"Task 3"}


async def test_a_refused_piece_stops_the_rest():
    posted: list[dict] = []

    def refuse(url: str, payload: dict, token: str) -> dict:
        posted.append(payload)
        return {"ok": False, "error": "msg_blocks_too_long"}

    long = "\n\n".join(["x" * 7000, "y" * 7000])

    assert await SlackWebApi("t", "D", post=refuse).send(long) is False
    assert len(posted) == 1


async def test_a_refused_markdown_block_is_posted_again_as_plain_text():
    """A workspace that refuses the block still gets the report, unrendered."""
    posted: list[dict] = []

    def no_blocks(url: str, payload: dict, token: str) -> dict:
        posted.append(payload)
        return {"ok": "blocks" not in payload, "error": "invalid_blocks"}

    assert await SlackWebApi("t", "D", post=no_blocks).send("the **report**", fallback="Task 3")
    assert posted[1] == {
        "channel": "D",
        "text": "the **report**",
        "unfurl_links": False,
        "unfurl_media": False,
    }


def test_markdown_is_cut_between_sections_and_never_inside_a_list_or_table():
    results = "## Results\n\n" + "\n".join(["| a | b |", "|---|---|"] + ["| 1 | 2 |"] * 40)
    steps = "## Steps\n\n" + "\n".join(f"{n}. step {n}" for n in range(1, 30))

    pieces = markdown_pieces(f"{results}\n\n{steps}", limit=len(results) + 10)

    assert pieces == [results, steps]


def test_markdown_is_not_cut_inside_a_code_block():
    code = "```\nfirst\n\nsecond\n```"

    assert markdown_pieces(f"intro\n\n{code}\n\nafter", limit=25) == [
        "intro",
        code,
        "after",
    ]


def test_only_a_paragraph_longer_than_a_message_is_cut_mid_way():
    assert markdown_pieces("z" * 25, limit=10) == ["z" * 10, "z" * 10, "z" * 5]


async def test_slack_refusing_the_message_is_a_failure_not_a_crash():
    """Slack reports its own errors in the body, with a 200."""

    def refuse(url: str, payload: dict, token: str) -> dict:
        return {"ok": False, "error": "channel_not_found"}

    assert await SlackWebApi("xoxb-test", "D0TEST", post=refuse).send("hello") is False


async def test_a_transport_failure_is_a_failure_not_a_crash():
    def explode(url: str, payload: dict, token: str) -> dict:
        raise OSError("no route to host")

    assert await SlackWebApi("xoxb-test", "D0TEST", post=explode).send("hello") is False


# --- connecting: the app's manifest, and the three calls setup makes ----------------------


def test_the_manifest_is_a_bot_that_can_message_and_a_user_token_that_sees_nothing():
    manifest = app_manifest()

    assert manifest["display_information"]["name"] == "Keryx"
    assert manifest["features"]["bot_user"]["display_name"] == "Keryx"
    assert manifest["features"]["app_home"]["messages_tab_enabled"] is True
    assert manifest["oauth_config"] == {
        "scopes": {"bot": ["chat:write", "files:write"], "user": ["emoji:read"]}
    }
    # No events, no redirect URLs, no socket mode: nothing of Keryx's listens to Slack.
    assert set(manifest) == {"display_information", "features", "oauth_config"}
    assert set(manifest["features"]) == {"app_home", "bot_user"}


# --- the app carries the assistant's name --------------------------------------------------


@pytest.mark.parametrize("assistant", ["Lyra", "Jarvis", "Mary Ann", "O'Neil", "Лира"])
def test_the_app_carries_the_assistants_name_as_it_stands(assistant):
    assert slack.app_name(assistant) == assistant
    manifest = app_manifest(assistant=assistant)

    assert manifest["display_information"]["name"] == assistant
    assert manifest["features"]["bot_user"]["display_name"] == assistant


def test_a_name_too_long_for_slack_is_cut_to_the_apps_limit():
    """`display_information.name` takes 35 characters, the least of the two limits."""
    long = "Bartholomew Maximilian Featherstonehaugh the Third"

    fitted = slack.app_name(long)

    assert fitted == "Bartholomew Maximilian Featherstone"
    assert len(fitted) == slack.APP_NAME_MAX == 35
    # Never a space left hanging where the cut fell.
    assert slack.app_name("A" * 34 + " B") == "A" * 34
    assert app_manifest(assistant=long)["features"]["bot_user"]["display_name"] == fitted


@pytest.mark.parametrize(("assistant", "fitted"), [
    ("<!channel> Lyra", "channel Lyra"),
    ("@Lyra & *co*", "Lyra co"),
    ("Ly`ra` <@U0OWNER|x>", "Lyra U0OWNERx"),
    ("  Lyra\n\tthe   second ", "Lyra the second"),
    ("Lyra 🎙 2.0", "Lyra 2.0"),
])
def test_characters_the_bot_name_may_not_carry_are_dropped(assistant, fitted):
    """What is kept is letters, digits, spaces, and - ' . _: nothing Slack reads as markup,
    since the name is also written into what the owner is shown and what the app posts."""
    assert slack.app_name(assistant) == fitted
    assert app_manifest(assistant=assistant)["display_information"]["name"] == fitted


@pytest.mark.parametrize("assistant", ["", "   ", "!!!", "<@>", "-.-", "🎙", None])
def test_a_name_with_nothing_usable_left_is_keryx(assistant):
    assert slack.app_name(assistant) == "Keryx"
    assert app_manifest(assistant=assistant or "")["display_information"]["name"] == "Keryx"


@pytest.mark.parametrize("assistant", ["Slackbot", "here", "Channel", "Every-one", "s.l.a.c.k.bot"])
def test_a_name_whose_username_slack_keeps_for_itself_is_keryx(assistant):
    """Slack makes the bot's username from the display name and refuses a reserved one."""
    assert slack.app_name(assistant) == "Keryx"


def test_with_no_name_given_the_app_is_keryx():
    assert app_manifest() == app_manifest(assistant="")
    assert app_manifest()["display_information"]["name"] == "Keryx"
    assert json.loads(manifest_json(READS, assistant="Lyra")) == app_manifest(
        READS, assistant="Lyra")
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(new_app_url(REQUESTS, assistant="Lyra")).query)
    assert json.loads(query["manifest_json"][0]) == app_manifest(
        REQUESTS, assistant="Lyra")


# --- the app's colour ------------------------------------------------------------------------


def _luminance(colour: str) -> float:
    """Relative luminance of `#rrggbb`, as WCAG defines it."""
    def linear(part: int) -> float:
        value = part / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(colour[index:index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_the_manifest_carries_a_background_colour_from_the_mark(allowed):
    """Slack takes a hex colour with its hash, three or six digits. It writes white text
    over it, so of the mark's three colours it is the one white reads best on."""
    colour = app_manifest(allowed, assistant="Lyra")["display_information"]["background_color"]
    mark = (Path(__file__).parents[2] / "docs" / "assets" / "keryx-mark.svg").read_text()

    assert colour == slack.BACKGROUND_COLOR == "#7c3aed"
    assert re.fullmatch(r"#[0-9a-f]{6}", colour)
    assert f"stroke: {colour};" in mark
    # At least 4.5 to 1 against white, which is what small text needs.
    assert 1.05 / (_luminance(colour) + 0.05) >= 4.5
    for other in ("#f97316", "#db2777"):
        assert 1.05 / (_luminance(other) + 0.05) < 1.05 / (_luminance(colour) + 0.05)


def test_the_new_app_link_carries_the_whole_manifest_url_encoded():
    link = new_app_url()

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query, strict_parsing=True)
    assert link.startswith("https://api.slack.com/apps?new_app=1&manifest_json=%7B")
    assert query["new_app"] == ["1"]
    assert json.loads(query["manifest_json"][0]) == app_manifest()
    assert " " not in link and '"' not in link


def test_auth_test_names_the_workspace_the_token_belongs_to():
    calls = []

    def post(url, payload, token):
        calls.append((url, payload, token))
        return {"ok": True, "team": "Acme", "url": "https://acme.slack.com/", "user": "keryx"}

    workspace = auth_test("xoxb-test", post=post)

    assert workspace.ok and workspace.team == "Acme"
    assert not hasattr(workspace, "url"), "nothing reads the workspace's address any more"
    assert calls == [("https://slack.com/api/auth.test", {}, "xoxb-test")]


def test_a_name_slack_gives_is_kept_to_one_line():
    reply = {"ok": True, "team": "Acme\n  Corp", "user": {"id": "U0OWNER", "real_name": "Sam\tDoe"}}

    assert auth_test("xoxb-test", post=lambda *a: reply).team == "Acme Corp"


def test_a_refused_token_is_slacks_own_code_and_a_sentence_without_the_token():
    workspace = auth_test("xoxb-secret", post=lambda *a: {"ok": False, "error": "invalid_auth"})

    assert not workspace.ok and workspace.error == "invalid_auth"
    assert workspace.problem == "Slack does not recognise that token (invalid_auth)"
    assert "xoxb-secret" not in repr(workspace)


def test_an_error_that_is_not_a_code_is_never_repeated():
    """Whatever answered, only a bare code is ever printed: nothing else can carry a token."""
    reply = {"ok": False, "error": "bad token xoxb-secret was given"}

    workspace = auth_test("xoxb-secret", post=lambda *a: reply)

    assert workspace.error == "unknown_error"
    assert workspace.problem == "Slack's answer could not be read"


@pytest.mark.parametrize(
    ("failure", "code", "sentence"),
    [
        (urllib.error.URLError("no route to xoxb-secret"), "unreachable", "could not reach Slack"),
        (TimeoutError(), "unreachable", "could not reach Slack"),
        (ValueError("not JSON"), "unreachable", "could not reach Slack"),
        (http.client.IncompleteRead(b"half of xoxb-secret"), "unreachable",
         "could not reach Slack"),
        (http.client.BadStatusLine("xoxb-secret"), "unreachable", "could not reach Slack"),
        (
            urllib.error.HTTPError("https://slack.com", 429, "Too Many", None, None),
            "http_429",
            "Slack answered HTTP 429",
        ),
    ],
)
def test_a_call_that_never_arrives_is_a_failure_not_a_crash(failure, code, sentence):
    def post(url, payload, token):
        raise failure

    workspace = auth_test("xoxb-secret", post=post)

    assert workspace.error == code
    assert workspace.problem == sentence
    assert not workspace.refused  # Slack was never properly asked, so it refused nothing


@pytest.mark.parametrize(
    ("error", "refused"),
    [("", False), ("invalid_auth", True), ("users_not_found", True), ("unreachable", False),
     ("http_503", False), ("unknown_error", False)],
)
def test_only_a_code_of_slacks_own_is_a_refusal(error, refused):
    assert SlackResult(error=error).refused is refused


@pytest.mark.parametrize(
    ("channel", "dm"), [("D0123", True), ("U0OWNER", False), ("W0OWNER", False),
                        ("C0123", False), ("", False)],
)
def test_only_a_direct_message_id_belongs_to_one_app(channel, dm):
    assert is_dm(channel) is dm


def test_an_answer_that_is_not_an_object_is_a_failure():
    assert auth_test("xoxb-test", post=lambda *a: ["ok"]).error == "unknown_error"


def test_a_test_message_is_plain_text_to_the_member_or_channel():
    calls = []

    def post(url, payload, token):
        calls.append((url, payload, token))
        return {"ok": True}

    result = post_text("xoxb-test", "U0OWNER", "hello", post=post)

    assert result.ok and result.problem == ""
    assert calls == [
        ("https://slack.com/api/chat.postMessage", {"channel": "U0OWNER", "text": "hello"},
         "xoxb-test")
    ]


def test_a_test_message_slack_refuses_says_its_code():
    result = post_text(
        "xoxb-test", "U0OWNER", "hello", post=lambda *a: {"ok": False, "error": "channel_not_found"}
    )

    assert result.problem == "Slack does not know that channel or member (channel_not_found)"


def test_the_bot_reads_nothing_about_members_and_no_address_is_ever_looked_up():
    """Who the owner is comes from their own user token: the lookup by address, and the
    two bot scopes it needed, are gone."""
    for allowed in SELECTIONS:
        assert not any(scope.startswith("users:") for scope in slack.bot_scopes(allowed))
    assert not hasattr(slack, "lookup_by_email") and not hasattr(slack, "Member")
    assert "lookupByEmail" not in inspect.getsource(slack)


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_whatever_is_ticked_the_install_issues_the_owner_a_user_token(allowed):
    """An install issues one only when the manifest asks for a user scope, and the token
    is how Slack says who installed the app."""
    assert app_manifest(allowed)["oauth_config"]["scopes"]["user"]
    assert "user" in token_kinds(allowed)


@pytest.mark.parametrize("allowed", [BASE, REQUESTS], ids=named)
def test_with_neither_read_nor_send_the_user_token_has_one_scope_that_sees_nothing(allowed):
    """`emoji:read`: "View custom emoji in a workspace", granted to user tokens, not a
    legacy scope, and its one method is `emoji.list`, which Keryx never calls."""
    assert slack.user_scopes(allowed) == slack.IDENTITY_USER_SCOPES == ("emoji:read",)
    assert app_manifest(allowed)["oauth_config"]["scopes"]["user"] == ["emoji:read"]
    for module in ("slack", "slack_read", "slack_write", "slack_query"):
        source = Path(slack.__file__).with_name(f"{module}.py").read_text()
        assert "emoji.list" not in source.replace("`emoji.list`", "")


def test_what_may_be_ticked_is_three_independent_things_and_no_ladder():
    assert slack.PERMISSIONS == (Permission.REQUESTS, Permission.READ, Permission.SEND)
    assert [str(each) for each in slack.PERMISSIONS] == ["requests", "read", "send"]
    assert len(SELECTIONS) == 8  # every combination is an app
    assert not hasattr(slack, "Access") and not hasattr(slack, "LEVELS")
    assert slack.permissions(["Read ", "requests"]) == READS
    assert slack.ordered({Permission.SEND, Permission.REQUESTS}) == (
        Permission.REQUESTS, Permission.SEND)
    with pytest.raises(ValueError, match="converse"):
        slack.permissions(["converse"])


def test_nothing_ticked_is_exactly_the_app_there_was():
    assert app_manifest(BASE) == app_manifest()
    assert new_app_url(BASE) == new_app_url()
    assert "settings" not in app_manifest()
    assert app_manifest()["features"]["app_home"]["messages_tab_read_only_enabled"] is True
    assert app_manifest()["oauth_config"] == {"scopes": {
        "bot": ["chat:write", "files:write"], "user": ["emoji:read"]}}


def test_requests_hears_its_own_direct_messages_over_a_socket_and_nothing_else():
    manifest = app_manifest(REQUESTS)

    assert manifest["oauth_config"] == {"scopes": {
        "bot": ["chat:write", "files:write", "im:history", "reactions:write"],
        "user": ["emoji:read"]}}
    assert manifest["settings"] == {
        "event_subscriptions": {"bot_events": ["message.im"]},
        "socket_mode_enabled": True,
    }
    # The tab takes what the owner types, and nothing of Keryx's has an address at Slack.
    assert manifest["features"]["app_home"]["messages_tab_read_only_enabled"] is False
    assert "request_url" not in json.dumps(manifest)
    assert "redirect_urls" not in json.dumps(manifest)


def test_read_alone_changes_only_what_the_owners_own_token_may_do():
    """No socket, no event, no app-level token: reading is the user token's, and the bot
    stays a bot that posts."""
    manifest = app_manifest(READ)

    assert manifest["oauth_config"]["scopes"]["bot"] == ["chat:write", "files:write"]
    assert manifest["oauth_config"]["scopes"]["user"] == [
        "channels:read", "groups:read", "im:read", "mpim:read", "users:read",
        "channels:history", "groups:history", "im:history", "mpim:history",
        "search:read",
    ]
    assert "settings" not in manifest
    assert manifest["features"]["app_home"]["messages_tab_read_only_enabled"] is True
    assert token_kinds(READ) == ("user", "bot")
    assert slack.used_kinds(READ) == ("bot", "user")


def test_send_alone_can_find_who_is_meant_and_post_and_can_read_no_message():
    """The lookups the recipient is resolved with, the two scopes that post as the
    owner, and neither a history nor search: what it sees is names."""
    manifest = app_manifest(SEND)
    user = manifest["oauth_config"]["scopes"]["user"]

    assert user == ["channels:read", "groups:read", "im:read", "mpim:read", "users:read",
                    "chat:write", "im:write"]
    assert not any(scope.endswith(":history") or scope.startswith("search") for scope in user)
    assert manifest["oauth_config"]["scopes"]["bot"] == ["chat:write", "files:write"]
    assert "settings" not in manifest
    assert slack.used_kinds(SEND) == ("bot", "user")


def test_what_send_looks_a_recipient_up_with_is_all_it_is_given_to_look_with():
    """`slack_write.recipient` resolves through the reader's lists of conversations and
    members and nothing else of it, so those scopes are the whole of what `send` adds to
    see with."""
    from keryx.integrations import slack_read, slack_write

    assert slack_write.WRITE_METHODS == {"chat.postMessage", "conversations.open"}
    looked_up = {"users.conversations", "users.list", "users.info"}
    assert looked_up <= slack_read.READ_METHODS
    assert slack.LOOKUP_USER_SCOPES == (
        "channels:read", "groups:read", "im:read", "mpim:read", "users:read")
    assert slack.SEND_USER_SCOPES == ("chat:write", "im:write")


def test_requests_and_read_together_are_also_sent_mentions_of_the_app():
    manifest = app_manifest(READS)

    # The bot may hear a mention of itself, and still read no conversation at all.
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "chat:write", "files:write", "im:history", "reactions:write", "app_mentions:read",
    ]
    assert not any(scope.split(":")[0] in ("channels", "groups", "mpim")
                   for scope in manifest["oauth_config"]["scopes"]["bot"])
    assert set(manifest["oauth_config"]["scopes"]["user"]) == {
        "channels:history", "groups:history", "im:history", "mpim:history",
        "channels:read", "groups:read", "im:read", "mpim:read",
        "users:read", "search:read",
    }
    assert manifest["settings"] == {
        "event_subscriptions": {"bot_events": ["message.im", "app_mention"]},
        "socket_mode_enabled": True,
    }


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_a_mention_is_a_request_only_with_requests_and_read_both(allowed):
    """Something has to listen, and the thread around a mention can only be read as the
    owner: neither box alone subscribes the app to mentions."""
    both = Permission.REQUESTS in allowed and Permission.READ in allowed
    manifest = app_manifest(allowed)
    events = manifest.get("settings", {}).get("event_subscriptions", {}).get("bot_events", [])

    assert slack.mentions(allowed) is both
    assert ("app_mention" in events) is both
    assert ("app_mentions:read" in manifest["oauth_config"]["scopes"]["bot"]) is both


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_each_box_adds_its_own_scopes_whatever_else_is_ticked(allowed):
    scopes = app_manifest(allowed)["oauth_config"]["scopes"]
    bot, user = set(scopes["bot"]), set(scopes["user"])

    assert ({"im:history", "reactions:write"} <= bot) is (Permission.REQUESTS in allowed)
    assert ("search:read" in user) is ("channels:history" in user) is (
        Permission.READ in allowed)
    assert ({"chat:write", "im:write"} <= user) is (Permission.SEND in allowed)
    assert ("users:read" in user) is slack.as_owner(allowed)
    assert ("emoji:read" in user) is not slack.as_owner(allowed)
    assert ("settings" in app_manifest(allowed)) is (Permission.REQUESTS in allowed)


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_nothing_ticked_is_ever_sent_a_conversation_it_was_not_addressed_in(allowed):
    """Never `message.channels`, `.groups` or `.mpim`, and never a user-level event, which
    would be every direct message the owner has with anybody."""
    text = json.dumps(app_manifest(allowed))

    assert "user_events" not in text
    for event in ("message.channels", "message.groups", "message.mpim"):
        assert event not in text


def test_a_conversations_id_is_held_to_its_shape():
    assert slack.is_conversation("C0123ABCD") and slack.is_conversation("D0OWNER")
    for bad in ("U0OWNER", "c0123abcd", "C1 --workspace x", "C1`id`", "", None, 7):
        assert not slack.is_conversation(bad)


def test_search_is_one_constant_and_not_the_api_whose_results_may_not_be_stored():
    assert slack.SEARCH_SCOPES == ("search:read",)
    assert set(slack.SEARCH_SCOPES) <= set(slack.READ_USER_SCOPES)
    assert not any(scope.startswith("search:read.") for scope in slack.user_scopes(EVERYTHING))


@pytest.mark.parametrize("allowed", SELECTIONS, ids=named)
def test_every_selection_has_a_link_and_a_manifest_to_paste(allowed):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(new_app_url(allowed)).query)

    assert json.loads(query["manifest_json"][0]) == app_manifest(allowed)
    assert json.loads(manifest_json(allowed)) == app_manifest(allowed)
    assert "\n" in manifest_json(allowed)


def test_connecting_always_takes_the_user_token_and_tokens_are_told_apart_by_prefix():
    # In the order Slack's console shows them: the app-level token on a page of its own,
    # then the user token and, below it on the same page, the bot token.
    assert token_kinds(BASE) == token_kinds(READ | SEND) == ("user", "bot")
    assert token_kinds(REQUESTS) == ("app", "user", "bot")
    assert token_kinds(READS) == token_kinds(EVERYTHING) == ("app", "user", "bot")
    # What it runs on once connected is another matter: the user token only to read or
    # send as the owner, the app-level one only to listen.
    assert slack.used_kinds(BASE) == ("bot",)
    assert slack.used_kinds(REQUESTS) == ("bot", "app")
    assert slack.used_kinds(READS) == ("bot", "app", "user")
    assert slack.used_kinds(REQUESTS | SEND) == ("bot", "app", "user")
    assert [token_kind(t) for t in ("xoxb-1", "xapp-1-A", "xoxp-1", "xoxe-1", "")] == [
        "bot", "app", "user", None, None,
    ]


def test_auth_test_says_which_team_and_whom_the_token_acts_as():
    reply = {"ok": True, "team": "Acme", "team_id": "T0ACME", "user_id": "U0OWNER"}

    workspace = auth_test("xoxp-test", post=lambda *a: reply)

    assert (workspace.team_id, workspace.user_id) == ("T0ACME", "U0OWNER")
    odd = auth_test("xoxp-test", post=lambda *a: {"ok": True, "team_id": "t 1", "user_id": 7})
    assert (odd.team_id, odd.user_id) == ("", "")


def test_an_app_token_is_checked_by_opening_a_connection_whose_address_is_never_shown():
    calls = []

    def post(url, payload, token):
        calls.append((url, payload, token))
        return {"ok": True, "url": "wss://wss.slack.example/link/?ticket=very-secret"}

    connection = open_connection("xapp-1", post=post)

    assert connection.ok and connection.url.endswith("ticket=very-secret")
    assert calls == [("https://slack.com/api/apps.connections.open", {}, "xapp-1")]
    assert "very-secret" not in repr(connection) and "wss" not in repr(connection)


@pytest.mark.parametrize("reply", [
    {"ok": True}, {"ok": True, "url": "https://x.example/"}, {"ok": True, "url": 7},
])
def test_an_answer_with_no_socket_address_is_a_failure(reply):
    assert open_connection("xapp-1", post=lambda *a: reply).error == "unknown_error"


def test_an_app_token_slack_refuses_says_its_code():
    refused = open_connection("xapp-1", post=lambda *a: {"ok": False, "error": "invalid_auth"})

    assert refused.refused and refused.url == ""


def test_the_connection_is_asked_for_as_a_form_with_the_token_in_a_header(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()),
                    data=request.data, method=request.get_method())
        return io.BytesIO(b'{"ok": true, "url": "wss://x.example/?ticket=t"}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert open_connection("xapp-secret").ok
    assert seen["method"] == "POST" and seen["data"] == b""
    assert seen["headers"]["Authorization"] == "Bearer xapp-secret"
    assert seen["headers"]["Content-type"] == "application/x-www-form-urlencoded"
    assert "xapp-secret" not in seen["url"]


def test_a_reaction_is_the_acknowledgement_and_one_already_there_is_fine():
    calls = []

    def post(url, payload, token):
        calls.append((url, payload, token))
        return {"ok": True}

    assert add_reaction("xoxb-1", "D1", "17.000100", post=post).ok
    assert calls == [("https://slack.com/api/reactions.add",
                      {"channel": "D1", "timestamp": "17.000100", "name": "eyes"}, "xoxb-1")]
    again = {"ok": False, "error": "already_reacted"}
    assert add_reaction("xoxb-1", "D1", "17.000100", post=lambda *a: again).ok
    missing = {"ok": False, "error": "missing_scope"}
    assert add_reaction("xoxb-1", "D1", "17.000100", post=lambda *a: missing).error == (
        "missing_scope"
    )


# --- posting into a thread, and where a message landed ------------------------------------


async def test_post_says_where_the_message_landed_which_is_slacks_answer():
    """A member's id as the channel lands in their direct message with the app."""
    def post(url, payload, token):
        return {"ok": True, "channel": "D0REAL", "ts": "1700000000.000100"}

    posted = await SlackWebApi("t", "U0OWNER", post=post).post("report")

    assert posted == Posted(channel="D0REAL", ts="1700000000.000100")


async def test_a_reply_goes_in_its_thread_and_only_its_first_piece_is_broadcast():
    payloads: list[dict] = []

    def post(url, payload, token):
        payloads.append(payload)
        return {"ok": True, "channel": "D1", "ts": f"1700000000.00000{len(payloads)}"}

    long = "\n\n".join(["x" * 7000, "y" * 7000])
    posted = await SlackWebApi("t", "D1", post=post).post(
        long, thread_ts="1699999999.000001", broadcast=True
    )

    assert posted == Posted(channel="D1", ts="1700000000.000001")
    assert [p["thread_ts"] for p in payloads] == ["1699999999.000001"] * 2
    assert [p.get("reply_broadcast") for p in payloads] == [True, None]


async def test_a_reply_that_is_not_broadcast_says_nothing_about_it():
    payloads: list[dict] = []

    def post(url, payload, token):
        payloads.append(payload)
        return {"ok": True}

    posted = await SlackWebApi("t", "D1", post=post).post("x", thread_ts="1.1")

    assert "reply_broadcast" not in payloads[0] and payloads[0]["thread_ts"] == "1.1"
    # An answer without the two fields still counts as sent, with what is known.
    assert posted == Posted(channel="D1", ts="")


async def test_a_long_message_whose_second_piece_fails_still_says_where_the_first_landed():
    """The thread it started is there, whatever became of the rest."""
    answers = [{"ok": True, "channel": "D0REAL", "ts": "1700000000.000001"},
               {"ok": False, "error": "msg_too_long"}]

    result = await SlackWebApi("t", "U0OWNER", post=lambda *a: answers.pop(0)).post(
        "\n\n".join(["x" * 7000, "y" * 7000])
    )

    assert not result.ok and result.error == "msg_too_long"
    assert (result.channel, result.ts) == ("D0REAL", "1700000000.000001")


async def test_a_post_slack_refuses_landed_nowhere():
    refuse = lambda *a: {"ok": False, "error": "channel_not_found"}  # noqa: E731

    refused = await SlackWebApi("t", "D1", post=refuse).post("x")

    assert not refused.ok and refused.error == "channel_not_found" and refused.gone
    assert (refused.channel, refused.ts) == ("", "")


@pytest.mark.parametrize(
    ("answer", "error", "gone"),
    [
        ({"ok": False, "error": "ratelimited"}, "ratelimited", False),
        ({"ok": False, "error": "thread_not_found"}, "thread_not_found", True),
        ({"ok": False, "error": "not_in_channel"}, "not_in_channel", True),
        ({"ok": False, "error": "invalid_auth"}, "invalid_auth", True),
        ({"ok": False, "error": "see https://x.example"}, "unknown_error", False),
        (TimeoutError("timed out"), "unreachable", False),
    ],
)
async def test_a_post_says_whether_its_thread_is_gone_or_only_not_reached(answer, error, gone):
    """What may pass (a timeout, a rate limit) is never taken for a thread that is gone."""
    def post(url, payload, token):
        if isinstance(answer, Exception):
            raise answer
        return answer

    result = await SlackWebApi("t", "D1", post=post).post("x", thread_ts="1.1")

    assert (result.error, result.gone, result.ok) == (error, gone, False)


async def test_a_long_message_posted_on_its_own_is_one_thread_under_its_first_piece():
    """A reply under any piece of a long report is then in the one thread."""
    payloads: list[dict] = []

    def post(url, payload, token):
        payloads.append(payload)
        return {"ok": True, "channel": "D0REAL", "ts": f"1700000000.00000{len(payloads)}"}

    long = "\n\n".join(["x" * 7000, "y" * 7000, "z" * 7000])

    posted = await SlackWebApi("t", "U0OWNER", post=post).post(long, fallback="Task 3")

    assert posted == Posted(channel="D0REAL", ts="1700000000.000001")
    assert [p.get("thread_ts") for p in payloads] == [
        None, "1700000000.000001", "1700000000.000001",
    ]
    # In the conversation Slack put the first one in, which is where its thread is.
    assert [p["channel"] for p in payloads] == ["U0OWNER", "D0REAL", "D0REAL"]


async def test_a_long_message_sent_by_the_tool_is_still_several_messages():
    payloads: list[dict] = []

    def post(url, payload, token):
        payloads.append(payload)
        return {"ok": True, "channel": "D0REAL", "ts": "1700000000.000001"}

    assert await SlackWebApi("t", "U0OWNER", post=post).send("\n\n".join(["x" * 7000] * 2))
    assert [p.get("thread_ts") for p in payloads] == [None, None]


def test_a_timestamp_is_recognised_and_ordered():
    assert is_ts("1700000000.000100") and not is_ts("1700000000") and not is_ts(17.1)
    assert not is_ts("1700000000.000100; drop") and not is_ts(None)
    assert ts_key("1700000000.000100") < ts_key("1700000000.000200") < ts_key("1700000001.0")
    assert ts_key("9.5") < ts_key("10.1")


def test_a_messages_text_is_read_back_as_it_was_typed():
    assert plain_text("  fix &lt;b&gt; &amp;amp; ship  ") == "fix <b> &amp; ship"
    assert plain_text("see <https://x.example/a?b=1|the page> and <https://y.example>") == (
        "see https://x.example/a?b=1 and https://y.example"
    )
    assert plain_text("mail <mailto:sam@x.io|sam@x.io>") == "mail mailto:sam@x.io"
    assert plain_text("<@U0OWNER> hi") == "<@U0OWNER> hi"


def test_send_adds_to_read_the_two_user_scopes_that_send_as_the_owner():
    """Nothing the bot may do changes, and nothing is added that edits, deletes or joins."""
    read, write = app_manifest(READS), app_manifest(EVERYTHING)

    assert set(write["oauth_config"]["scopes"]["user"]) - set(
        read["oauth_config"]["scopes"]["user"]) == {"chat:write", "im:write"}
    assert write["oauth_config"]["scopes"]["bot"] == read["oauth_config"]["scopes"]["bot"]
    assert write["settings"] == read["settings"]
    assert slack.token_kinds(EVERYTHING) == slack.token_kinds(READS)
    assert slack.user_scopes(REQUESTS) == ("emoji:read",)


def test_checking_a_token_also_says_what_it_may_do_when_slack_says():
    said = slack.auth_test("xoxp-1", post=lambda *a: {
        "ok": True, "team": "Acme", "team_id": "T0ACME", "user_id": "U0OWNER",
        slack.SCOPES: "search:read, chat:write,im:write"})
    silent = slack.auth_test("xoxp-1", post=lambda *a: {
        "ok": True, "team": "Acme", "team_id": "T0ACME", "user_id": "U0OWNER"})

    assert said.scopes == ("search:read", "chat:write", "im:write")
    assert silent.scopes is None, "not said is never taken to mean nothing"


# --- files: three steps, and nothing of Slack's answer repeated ----------------------------

UPLOAD_ADDRESS = "https://files.slack.example/upload/v1/very-secret-ticket"


class FakeUploads:
    """Slack's three steps, each recorded: the two methods and the address between them."""

    def __init__(self, *, url=UPLOAD_ADDRESS, file_id="F0PLOT", start=None, complete=None,
                 send=None) -> None:
        self.calls: list = []
        self._start = start or {"ok": True, "upload_url": url, "file_id": file_id}
        self._complete = complete or {"ok": True, "files": [{"id": file_id}]}
        self._send = send

    def form(self, url, params, token):
        self.calls.append(("form", url, params, token))
        return self._start

    def post(self, url, payload, token):
        self.calls.append(("json", url, payload, token))
        return self._complete

    def send(self, url, path, size):
        self.calls.append(("bytes", url, path.read_bytes(), size))
        if self._send is not None:
            raise self._send

    def files(self, token="xoxb-very-secret") -> SlackFiles:
        return SlackFiles(token, form=self.form, post=self.post, send=self.send)


@pytest.fixture
def plot(tmp_path) -> LocalFile:
    path = tmp_path / "plot.png"
    path.write_bytes(b"\x89PNG and the rest")
    return local_file(path)


def test_whatever_is_ticked_can_send_a_file_and_with_the_one_scope_that_takes():
    """`files:write` is the scope both upload methods ask for, and the only one added."""
    assert slack.FILE_BOT_SCOPES == ("files:write",)
    for allowed in SELECTIONS:
        scopes = app_manifest(allowed)["oauth_config"]["scopes"]
        assert "files:write" in scopes["bot"] and "files:write" not in scopes["user"]
        assert not any(scope.startswith(("files:read", "remote_files"))
                       for scope in scopes["bot"] + scopes["user"])


async def test_a_file_is_uploaded_in_slacks_two_steps_and_the_token_never_goes_with_the_bytes(
    plot,
):
    slack_api = FakeUploads()

    uploaded = await slack_api.files().upload(plot)

    assert uploaded == Uploaded(file_id="F0PLOT", title="plot.png")
    assert slack_api.calls == [
        ("form", "https://slack.com/api/files.getUploadURLExternal",
         {"filename": "plot.png", "length": plot.size}, "xoxb-very-secret"),
        ("bytes", UPLOAD_ADDRESS, b"\x89PNG and the rest", plot.size),
    ]


async def test_the_address_slack_gives_is_never_kept_or_shown(plot, caplog):
    caplog.set_level("DEBUG")

    uploaded = await FakeUploads().files().upload(plot)
    failed = await FakeUploads(send=OSError(UPLOAD_ADDRESS)).files().upload(plot)

    for result in (uploaded, failed):
        assert "very-secret" not in repr(result) + result.problem
    assert "very-secret" not in caplog.text and "xoxb" not in caplog.text


async def test_sharing_is_one_call_that_names_a_conversation_and_may_name_a_thread(plot):
    slack_api = FakeUploads()
    files = slack_api.files()
    uploaded = [await files.upload(plot)]
    slack_api.calls.clear()

    assert (await files.share(uploaded, channel="D0REAL")).ok
    assert (await files.share(uploaded, channel="C0SHARED", thread_ts="1700000000.000100")).ok

    url = "https://slack.com/api/files.completeUploadExternal"
    listed = [{"id": "F0PLOT", "title": "plot.png"}]
    assert slack_api.calls == [
        ("json", url, {"files": listed, "channel_id": "D0REAL"}, "xoxb-very-secret"),
        ("json", url, {"files": listed, "channel_id": "C0SHARED",
                       "thread_ts": "1700000000.000100"}, "xoxb-very-secret"),
    ]


async def test_an_app_without_the_scope_is_refused_before_any_byte_is_sent(plot):
    slack_api = FakeUploads(start={"ok": False, "error": "missing_scope",
                                   "needed": "files:write", "provided": "chat:write"})

    uploaded = await slack_api.files().upload(plot)

    assert uploaded.error == "missing_scope" and uploaded.file_id == ""
    assert [call[0] for call in slack_api.calls] == ["form"]


@pytest.mark.parametrize("start", [
    {"ok": True, "file_id": "F0PLOT"},
    {"ok": True, "upload_url": "http://files.slack.example/x", "file_id": "F0PLOT"},
    {"ok": True, "upload_url": UPLOAD_ADDRESS},
    {"ok": True, "upload_url": UPLOAD_ADDRESS, "file_id": "not an id"},
    {"ok": True, "upload_url": 7, "file_id": "F0PLOT"},
])
async def test_an_answer_without_a_secure_address_and_a_file_id_sends_no_bytes(plot, start):
    slack_api = FakeUploads(start=start)

    assert (await slack_api.files().upload(plot)).error == "unknown_error"
    assert [call[0] for call in slack_api.calls] == ["form"]


@pytest.mark.parametrize(("failure", "code"), [
    (urllib.error.HTTPError("", 413, "too large", None, None), "http_413"),
    (urllib.error.URLError("no route"), "unreachable"),
    (TimeoutError(), "unreachable"),
    (http.client.RemoteDisconnected("cut"), "unreachable"),
])
async def test_bytes_that_do_not_arrive_are_a_code_and_nothing_is_shared(plot, failure, code):
    uploaded = await FakeUploads(send=failure).files().upload(plot)

    assert uploaded.error == code and not uploaded.ok and uploaded.file_id == ""


async def test_a_share_slack_refuses_says_its_code(plot):
    slack_api = FakeUploads(complete={"ok": False, "error": "not_in_channel"})
    files = slack_api.files()

    shared = await files.share([await files.upload(plot)], channel="C0SHARED")

    assert shared.error == "not_in_channel" and shared.refused


def test_the_bytes_go_as_they_are_to_the_address_with_no_token_and_are_read_as_sent(
    plot, monkeypatch
):
    seen = {}

    class Answer(io.BytesIO):
        status = 200

    def urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()),
                    body=request.data.read(), method=request.get_method(), timeout=timeout)
        return Answer(b"OK - 17")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    slack._send_file(UPLOAD_ADDRESS, plot.path, plot.size)

    assert seen["url"] == UPLOAD_ADDRESS and seen["method"] == "POST"
    assert seen["body"] == b"\x89PNG and the rest"
    assert seen["headers"] == {"Content-type": "application/octet-stream",
                               "Content-length": str(plot.size)}
    assert "Authorization" not in seen["headers"]


def test_anything_but_a_200_from_the_address_is_a_failure_that_does_not_carry_it(
    plot, monkeypatch
):
    class Answer(io.BytesIO):
        status = 202
        headers = None

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Answer(b""))

    with pytest.raises(urllib.error.HTTPError) as failed:
        slack._send_file(UPLOAD_ADDRESS, plot.path, plot.size)

    assert failed.value.code == 202
    assert "very-secret" not in str(failed.value) + str(failed.value.url)


async def test_the_upload_url_is_asked_for_as_a_form_because_a_json_body_is_ignored(
    monkeypatch, plot
):
    seen = {}

    def urlopen(request, timeout):
        seen.update(headers=dict(request.header_items()), data=request.data)
        return io.BytesIO(json.dumps(
            {"ok": True, "upload_url": UPLOAD_ADDRESS, "file_id": "F0PLOT"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    sent = []
    files = SlackFiles("xoxb-very-secret", send=lambda *a: sent.append(a))

    uploaded = await files.upload(plot)

    assert uploaded.ok and len(sent) == 1
    assert seen["headers"]["Content-type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(seen["data"].decode()) == {
        "filename": ["plot.png"], "length": [str(plot.size)]}


# --- which paths may be sent ---------------------------------------------------------------


def test_a_regular_readable_file_may_be_sent_under_its_own_name(tmp_path):
    path = tmp_path / "results" / "loss curve.png"
    path.parent.mkdir()
    path.write_bytes(b"12345")
    link = tmp_path / "latest.png"
    link.symlink_to(path)

    assert local_file(path) == LocalFile(path=path, name="loss curve.png", size=5)
    # A link to a file is sent as the file it leads to.
    assert local_file(str(link)).name == "loss curve.png"


def test_what_is_not_a_file_that_can_be_sent_is_refused_in_one_sentence(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.touch()
    locked = tmp_path / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)

    for path, says in [
        (tmp_path / "missing.png", "there is no file at"),
        (tmp_path, "is not a regular file"),
        (loop, "there is no file at"),
        (empty, "is empty, and Slack takes no empty file"),
        (locked, "cannot be read"),
        ("\0", "there is no file at"),
    ]:
        with pytest.raises(FileRefused, match=says):
            local_file(path)


def test_a_file_larger_than_slack_takes_is_refused_without_being_read(tmp_path, monkeypatch):
    path = tmp_path / "weights.bin"
    path.write_bytes(b"x" * 11)
    monkeypatch.setattr(slack, "MAX_FILE_BYTES", 10)

    with pytest.raises(FileRefused, match="larger than the 1 GB Slack takes in one file"):
        local_file(path)
    assert slack.MAX_FILE_BYTES == 10


def test_nothing_in_a_private_directory_is_sent_whatever_leads_to_it(tmp_path):
    home = tmp_path / "keryx-home"
    home.mkdir()
    (home / "secrets.toml").write_text('SLACK_BOT_TOKEN = "xoxb-very-secret"')
    link = tmp_path / "notes.txt"
    link.symlink_to(home / "secrets.toml")

    for path in (home / "secrets.toml", link, home / ".." / "keryx-home" / "secrets.toml"):
        with pytest.raises(FileRefused, match="Keryx's own configuration directory"):
            local_file(path, private=[home])
    # Beside it is not in it.
    (tmp_path / "keryx-home-notes.txt").write_text("x")
    assert local_file(tmp_path / "keryx-home-notes.txt", private=[home]).size == 1


def test_more_files_than_one_message_takes_are_refused_together(tmp_path):
    paths = []
    for number in range(slack.MAX_FILES + 1):
        paths.append(tmp_path / f"{number}.txt")
        paths[-1].write_text("x")

    assert [each.name for each in local_files(paths[:2])] == ["0.txt", "1.txt"]
    with pytest.raises(FileRefused, match="that is 11 files, and Slack takes 10 in one message"):
        local_files(paths)
    assert slack.MAX_FILE_BYTES == 1_000_000_000 and slack.MAX_FILES == 10
