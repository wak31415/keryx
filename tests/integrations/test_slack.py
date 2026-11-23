"""Tests for sending Slack messages. No network: the transport is injected."""

import http.client
import io
import json
import urllib.error
import urllib.parse

import pytest

from keryx.integrations import slack
from keryx.integrations.slack import (
    Access,
    Posted,
    SlackResult,
    SlackWebApi,
    add_reaction,
    app_manifest,
    auth_test,
    is_dm,
    is_ts,
    lookup_by_email,
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


def test_the_manifest_is_a_bot_that_can_message_and_look_a_member_up_and_nothing_more():
    manifest = app_manifest()

    assert manifest["display_information"]["name"] == "Keryx"
    assert manifest["features"]["bot_user"]["display_name"] == "Keryx"
    assert manifest["features"]["app_home"]["messages_tab_enabled"] is True
    assert manifest["oauth_config"] == {
        "scopes": {"bot": ["chat:write", "users:read", "users:read.email"]}
    }
    # No events, no redirect URLs, no socket mode: nothing of Keryx's listens to Slack.
    assert set(manifest) == {"display_information", "features", "oauth_config"}
    assert set(manifest["features"]) == {"app_home", "bot_user"}


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
    assert workspace.url == "https://acme.slack.com/"
    assert calls == [("https://slack.com/api/auth.test", {}, "xoxb-test")]


def test_a_name_slack_gives_is_kept_to_one_line():
    reply = {"ok": True, "team": "Acme\n  Corp", "user": {"id": "U0OWNER", "real_name": "Sam\tDoe"}}

    assert auth_test("xoxb-test", post=lambda *a: reply).team == "Acme Corp"
    assert lookup_by_email("xoxb-test", "sam@x.io", get=lambda *a: reply).name == "Sam Doe"


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


def test_a_member_is_looked_up_by_the_address_they_use_in_slack():
    calls = []

    def get(url, params, token):
        calls.append((url, params, token))
        return {"ok": True, "user": {"id": "U0OWNER", "name": "sam", "real_name": "Sam Doe"}}

    member = lookup_by_email("xoxb-test", " sam@example.com ", get=get)

    assert member.ok and member.user_id == "U0OWNER" and member.name == "Sam Doe"
    assert calls == [
        ("https://slack.com/api/users.lookupByEmail", {"email": "sam@example.com"}, "xoxb-test")
    ]


def test_a_member_with_no_real_name_goes_by_their_handle():
    reply = {"ok": True, "user": {"id": "U0OWNER", "name": "sam"}}

    assert lookup_by_email("xoxb-test", "sam@example.com", get=lambda *a: reply).name == "sam"


@pytest.mark.parametrize(
    ("code", "sentence"),
    [
        ("users_not_found", "nobody in that workspace has that address (users_not_found)"),
        ("missing_scope", "the app lacks a permission this needs (missing_scope)"),
    ],
)
def test_a_member_slack_will_not_give_says_why(code, sentence):
    member = lookup_by_email(
        "xoxb-test", "sam@example.com", get=lambda *a: {"ok": False, "error": code}
    )

    assert not member.ok and member.user_id == ""
    assert member.problem == sentence


@pytest.mark.parametrize("user", [None, {}, {"id": "not an id"}, "U0OWNER"])
def test_a_member_without_a_usable_id_is_a_failure(user):
    member = lookup_by_email(
        "xoxb-test", "sam@example.com", get=lambda *a: {"ok": True, "user": user}
    )

    assert member.error == "unknown_error" and member.user_id == ""


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


def test_the_lookup_is_one_get_with_the_token_in_a_header_and_never_in_the_address(monkeypatch):
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen.append(request)
        return Response(b'{"ok": false, "error": "users_not_found"}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    member = lookup_by_email("xoxb-secret", "sam+slack@example.com")

    (request,) = seen
    assert member.error == "users_not_found"
    assert request.get_method() == "GET"
    assert request.full_url == (
        "https://slack.com/api/users.lookupByEmail?email=sam%2Bslack%40example.com"
    )
    assert request.get_header("Authorization") == "Bearer xoxb-secret"
    assert "xoxb-secret" not in request.full_url


# --- the three access levels --------------------------------------------------------------


def test_the_levels_are_a_ladder():
    assert slack.LEVELS == (Access.ANNOUNCE, Access.CONVERSE, Access.READ)
    assert Access.READ.reaches(Access.CONVERSE) and Access.CONVERSE.reaches(Access.CONVERSE)
    assert not Access.ANNOUNCE.reaches(Access.CONVERSE)
    assert not Access.CONVERSE.reaches(Access.READ)


def test_announce_is_exactly_the_app_there_was():
    assert app_manifest(Access.ANNOUNCE) == app_manifest()
    assert new_app_url(Access.ANNOUNCE) == new_app_url()
    assert "settings" not in app_manifest()
    assert app_manifest()["features"]["app_home"]["messages_tab_read_only_enabled"] is True


def test_converse_hears_its_own_direct_messages_over_a_socket_and_nothing_else():
    manifest = app_manifest(Access.CONVERSE)

    assert manifest["oauth_config"] == {"scopes": {"bot": [
        "chat:write", "users:read", "users:read.email", "im:history", "reactions:write",
    ]}}
    assert manifest["settings"] == {
        "event_subscriptions": {"bot_events": ["message.im"]},
        "socket_mode_enabled": True,
    }
    # The tab takes what the owner types, and nothing of Keryx's has an address at Slack.
    assert manifest["features"]["app_home"]["messages_tab_read_only_enabled"] is False
    assert "request_url" not in json.dumps(manifest)
    assert "redirect_urls" not in json.dumps(manifest)


def test_read_is_also_sent_mentions_of_the_app_and_asks_the_owner_for_a_user_token():
    manifest = app_manifest(Access.READ)

    # The bot may hear a mention of itself, and still read no conversation at all.
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "chat:write", "users:read", "users:read.email", "im:history", "reactions:write",
        "app_mentions:read",
    ]
    assert not any(scope.split(":")[0] in ("channels", "groups", "mpim")
                   for scope in manifest["oauth_config"]["scopes"]["bot"])
    assert manifest["oauth_config"]["scopes"]["user"] == [
        "channels:history", "groups:history", "im:history", "mpim:history",
        "channels:read", "groups:read", "im:read", "mpim:read",
        "users:read", "search:read",
    ]
    assert manifest["settings"] == {
        "event_subscriptions": {"bot_events": ["message.im", "app_mention"]},
        "socket_mode_enabled": True,
    }


@pytest.mark.parametrize("access", list(Access))
def test_no_level_is_ever_sent_a_conversation_it_was_not_addressed_in(access):
    """Never `message.channels`, `.groups` or `.mpim`, and never a user-level event, which
    would be every direct message the owner has with anybody."""
    text = json.dumps(app_manifest(access))

    assert "user_events" not in text
    for event in ("message.channels", "message.groups", "message.mpim"):
        assert event not in text


def test_a_mention_is_found_however_slack_writes_it_and_a_link_to_a_message_is_built():
    found = slack.mention("U0KERYX")

    assert found.sub("", "<@U0KERYX> do it <@U0KERYX|keryx>").strip() == "do it"
    assert not found.search("<@U0KERYXX> hi") and not found.search("@U0KERYX hi")
    assert not found.search("<@U0OTHER> hi")
    assert slack.permalink("https://acme.slack.com/", "C123", "1700000000.000100") == (
        "https://acme.slack.com/archives/C123/p1700000000000100"
    )
    assert slack.permalink("", "C123", "1700000000.000100") == ""
    assert slack.permalink("http://acme.example/", "C123", "1700000000.000100") == ""


def test_search_is_one_constant_and_not_the_api_whose_results_may_not_be_stored():
    assert slack.SEARCH_SCOPES == ("search:read",)
    assert set(slack.SEARCH_SCOPES) <= set(slack.USER_SCOPES)
    assert not any(scope.startswith("search:read.") for scope in slack.USER_SCOPES)


@pytest.mark.parametrize("access", list(Access))
def test_each_level_has_a_link_and_a_manifest_to_paste(access):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(new_app_url(access)).query)

    assert json.loads(query["manifest_json"][0]) == app_manifest(access)
    assert json.loads(manifest_json(access)) == app_manifest(access)
    assert "\n" in manifest_json(access)


def test_each_level_needs_one_more_token_told_apart_by_prefix():
    assert token_kinds(Access.ANNOUNCE) == ("bot",)
    assert token_kinds(Access.CONVERSE) == ("bot", "app")
    assert token_kinds(Access.READ) == ("bot", "app", "user")
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
