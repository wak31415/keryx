"""Reading Slack as the owner: bounded, read-only, and never over a network here."""

import inspect
import json
import logging
import time

import httpx
import pytest

from keryx.integrations import slack_read
from keryx.integrations.slack_read import (
    DIRECTORY_PAGES,
    MAX_DAYS,
    MAX_NAMED,
    MAX_PAGES,
    MAX_REQUESTS,
    NAME_REQUESTS,
    READ_METHODS,
    SYSTEM_SUBTYPES,
    HttpSlackApi,
    Read,
    SlackReader,
    SlackReadError,
    classify,
    window,
)

OWNER, COL, EVE, SAM = "U0OWNER", "U0COL", "U0EVE", "U0SAM"
NOW = 1_700_000_000.0
DAY = 86400


def ts(days_ago: float, n: int = 0) -> str:
    return f"{NOW - days_ago * DAY:.0f}.{n:06d}"


def match(user, text, when, channel="D0COL", *, kind="im", name="", thread=None) -> dict:
    """One `search.messages` match."""
    where = {"id": channel, "name": name, "is_im": kind == "im", "is_mpim": kind == "mpim"}
    made = {"type": "message", "user": user, "text": text, "ts": when, "channel": where,
            "permalink": f"https://acme.slack.com/archives/{channel}/p{when.replace('.', '')}"}
    if thread:
        made["thread_ts"] = thread
    return made


def message(user, text, when, **more) -> dict:
    return {"type": "message", "user": user, "text": text, "ts": when, **more}


def member(user_id, real_name, display="", handle="", **more) -> dict:
    return {"id": user_id, "name": handle or real_name.lower().replace(" ", "."),
            "real_name": real_name, "profile": {"display_name": display}, **more}


class FakeSlackApi:
    """The Web API as the reader meets it: each method answers from a table, and every
    call is recorded. A method that is not a read fails the test that asked for it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.searches: dict[str, list] = {}
        self.histories: dict[str, list] = {}
        self.threads: dict[tuple[str, str], list] = {}
        #: Conversations that say there is more than they returned.
        self.has_more: set[str] = set()
        self.names = {COL: "Col League", EVE: "Eve Adams"}
        self.members = [member(OWNER, "Owner Person"), member(COL, "Col League"),
                        member(EVE, "Eve Adams"), member(SAM, "Sam Lee")]
        self.channels = [
            {"id": "D0COL", "is_im": True, "user": COL},
            {"id": "D0EVE", "is_im": True, "user": EVE},
            {"id": "C0GEN", "name": "general"},
            {"id": "C0BUD", "name": "budget-2027"},
            {"id": "G0PRIV", "name": "leads", "is_private": True},
        ]
        #: A method, or `method:key` (a conversation, or the start of a query), that fails.
        self.errors: dict[str, Exception] = {}
        self.closed = False

    def _fail(self, method: str, key: str = "") -> None:
        for name in (method, f"{method}:{key}"):
            if name in self.errors:
                raise self.errors[name]

    async def get(self, method: str, params) -> dict:
        assert method in READ_METHODS, method
        self.calls.append((method, dict(params)))
        if method == "search.messages":
            key = next((key for key in sorted(self.searches, key=len, reverse=True)
                        if params["query"].startswith(key)), "")
            self._fail(method, key or params["query"].split(" ")[0])
            found = self.searches.get(key, [])
            return {"ok": True, "messages": {"matches": found, "total": len(found)}}
        if method == "conversations.history":
            self._fail(method, params["channel"])
            return {"ok": True, "messages": self.histories.get(params["channel"], []),
                    "has_more": params["channel"] in self.has_more}
        if method == "conversations.replies":
            self._fail(method, params["channel"])
            return {"ok": True,
                    "messages": self.threads.get((params["channel"], params["ts"]), []),
                    "has_more": params["channel"] in self.has_more}
        self._fail(method, params.get("user", ""))
        if method == "users.info":
            return {"ok": True, "user": {"id": params["user"],
                                         "real_name": self.names.get(params["user"], "")}}
        if method == "users.list":
            return {"ok": True, "members": self.members}
        if method == "users.conversations":
            return {"ok": True, "channels": self.channels}
        raise AssertionError(method)

    async def aclose(self) -> None:
        self.closed = True

    def made(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]


@pytest.fixture
def api():
    return FakeSlackApi()


def reader(api, **more) -> SlackReader:
    return SlackReader(api, OWNER, clock=lambda: NOW, **more)


# --- read-only by construction ------------------------------------------------------------


def test_every_method_it_may_call_only_reads():
    """The list is the whole of what this module can ask Slack, and none of it writes."""
    assert READ_METHODS == {
        "users.conversations", "conversations.history", "conversations.replies",
        "search.messages", "users.info", "users.list",
    }
    writes = ("post", "update", "delete", "add", "remove", "join", "leave", "mark", "open",
              "close", "invite", "kick", "set", "upload", "schedule", "create", "rename")
    assert not [name for name in READ_METHODS if any(
        part in name.split(".")[-1].lower() for part in writes)]


def test_the_one_request_there_is_cannot_post_or_take_a_body():
    """Read-only is a matter of construction: there is no method to write with."""
    public = [name for name in vars(HttpSlackApi) if not name.startswith("_")]
    assert sorted(public) == ["aclose", "get"]
    assert list(inspect.signature(HttpSlackApi.get).parameters) == ["self", "method", "params"]
    source = inspect.getsource(slack_read)
    assert "_client.get(" in source
    for verb in (".post(", ".put(", ".patch(", ".delete(", ".request(", "json=", "data="):
        assert verb not in source


async def test_a_method_that_is_not_a_read_is_refused_before_anything_is_sent():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    api = HttpSlackApi("xoxp-secret", transport=httpx.MockTransport(handler))

    for method in ("chat.postMessage", "reactions.add", "conversations.mark", "files.upload"):
        with pytest.raises(SlackReadError) as refused:
            await api.get(method, {"channel": "D1"})
        assert refused.value.code == "not_a_read"
    assert sent == []
    await api.aclose()


async def test_a_read_is_one_get_with_the_token_in_a_header_and_never_in_the_address():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "messages": []})

    api = HttpSlackApi("xoxp-secret", transport=httpx.MockTransport(handler))

    assert await api.get("conversations.history", {"channel": "D0COL", "limit": "1"}) == {
        "ok": True, "messages": [],
    }
    [request] = seen
    assert request.method == "GET" and request.content == b""
    assert str(request.url) == "https://slack.com/api/conversations.history?channel=D0COL&limit=1"
    assert request.headers["Authorization"] == "Bearer xoxp-secret"
    assert "xoxp" not in str(request.url)
    await api.aclose()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429, headers={"Retry-After": "30"}, json={"ok": False}), "rate_limited"),
        (httpx.Response(200, json={"ok": False, "error": "ratelimited"}), "rate_limited"),
        (httpx.Response(200, json={"ok": False, "error": "invalid_auth"}), "signed_out"),
        (httpx.Response(200, json={"ok": False, "error": "token_revoked"}), "signed_out"),
        (httpx.Response(200, json={"ok": False, "error": "missing_scope"}), "missing_scope"),
        (httpx.Response(200, json={"ok": False, "error": "channel_not_found"}), "not_found"),
        (httpx.Response(200, json={"ok": False, "error": "fatal_error"}), "fatal_error"),
        (httpx.Response(200, json={"ok": False, "error": "xoxp-secret said no"}), "unreachable"),
        (httpx.Response(200, text="<html>proxy</html>"), "unreachable"),
        (httpx.Response(200, json=["not", "an", "object"]), "unreachable"),
    ],
)
async def test_a_failure_is_a_status_and_a_sentence_and_never_what_slack_said(response, code):
    api = HttpSlackApi("xoxp-secret", transport=httpx.MockTransport(lambda request: response))

    with pytest.raises(SlackReadError) as failed:
        await api.get("search.messages", {"query": "x"})

    assert failed.value.code == code
    said = failed.value.as_dict()
    assert said["status"] == code and said["message"].endswith(".")
    assert "xoxp" not in json.dumps(said)
    await api.aclose()


async def test_a_rate_limit_is_said_at_once_and_never_slept_on(monkeypatch):
    """On a live call a wait is silence: the status comes back and the caller says it."""
    async def never(*a, **k):
        pytest.fail("slept on a rate limit")

    monkeypatch.setattr("asyncio.sleep", never)
    api = HttpSlackApi("xoxp-secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "120"})))

    with pytest.raises(SlackReadError) as limited:
        await api.get("search.messages", {"query": "x"})

    assert limited.value.code == "rate_limited" and "minute" in limited.value.sentence
    await api.aclose()


async def test_slack_not_answering_is_a_status_too():
    def down(request):
        raise httpx.ConnectTimeout("timed out")

    api = HttpSlackApi("xoxp-secret", transport=httpx.MockTransport(down))

    with pytest.raises(SlackReadError) as failed:
        await api.get("users.info", {"user": COL})

    assert failed.value.code == "unreachable"
    await api.aclose()


def test_the_http_clients_own_log_is_held_quiet_because_an_address_is_what_was_searched_for():
    logging.getLogger("httpx").setLevel(logging.DEBUG)

    HttpSlackApi("xoxp-secret")

    assert logging.getLogger("httpx").level == logging.WARNING


def test_slacks_errors_become_a_few_statuses():
    assert classify("ratelimited") == "rate_limited"
    assert classify("not_authed") == "signed_out" and classify("not_in_channel") == "not_found"
    assert classify("not_allowed_token_type") == "missing_scope"
    assert classify("") == "unreachable" and classify("Some Words") == "unreachable"


# --- bounded --------------------------------------------------------------------------------


async def test_one_call_never_makes_more_than_its_budget_of_requests(api):
    one = reader(api, budget=3)

    for _ in range(3):
        await one.history("D0COL", limit=1)
    with pytest.raises(SlackReadError) as spent:
        await one.history("D0COL", limit=1)

    assert spent.value.code == "over_budget" and one.more
    assert len(api.calls) == 3 <= MAX_REQUESTS


async def test_a_read_never_spends_the_requests_kept_for_names(api):
    """Whatever the reads cost, there is still somebody to attribute a message to."""
    one = reader(api)

    for _ in range(MAX_REQUESTS - NAME_REQUESTS):
        await one.history("D0COL", limit=1)
    with pytest.raises(SlackReadError) as spent:
        await one.history("D0COL", limit=1)

    assert spent.value.code == "over_budget"
    assert await one.name(COL) == "Col League" and len(api.calls) <= MAX_REQUESTS
    for n in range(NAME_REQUESTS + 3):
        await one.name(f"U0N{n:03d}")
    assert len(api.calls) == MAX_REQUESTS, "and names stop at the budget too"


async def test_the_owners_conversations_are_asked_once_and_followed_for_a_few_pages(api):
    pages = iter([{"ok": True, "channels": [{"id": f"C0PAGE{n}", "name": f"page-{n}"}],
                   "response_metadata": {"next_cursor": f"cursor-{n}"}} for n in range(9)])

    async def get(method, params):
        api.calls.append((method, dict(params)))
        return next(pages)

    api.get = get
    one = reader(api)

    first, again = await one.places(), await one.places()

    assert [place.name for place in first] == ["page-0", "page-1"] and again is first
    assert len(api.calls) == MAX_PAGES and one.more
    assert api.calls[0][1]["types"] == "public_channel,private_channel,mpim,im"
    assert api.calls[1][1]["cursor"] == "cursor-0"


async def test_the_member_list_is_followed_for_a_few_pages_and_leaves_out_who_is_not_a_person(
    api,
):
    api.members += [member("U0BOT", "Deploy Bot", is_bot=True),
                    member("U0GONE", "Left Long Ago", deleted=True),
                    {"id": "USLACKBOT", "real_name": "Slackbot"}, "not a member", {"id": 7}]
    one = reader(api)

    people = await one.people()

    assert [person.id for person in people] == [OWNER, COL, EVE, SAM]
    assert people[1].names == ("Col League", "col.league") and people[1].label == "Col League"
    assert len(api.made("users.list")) == 1 <= DIRECTORY_PAGES
    # And what it learned names the authors of messages, with no request each.
    assert await one.name(SAM) == "Sam Lee" and api.made("users.info") == []


def test_how_far_back_is_held_between_a_day_and_a_month():
    assert [window(days) for days in (None, 0, -3, 1, 7, 400, "nonsense", "12")] == [
        7, 1, 1, 1, 7, MAX_DAYS, 7, 12
    ]


async def test_a_conversation_is_called_what_the_owners_list_calls_it_or_goes_by_its_id(api):
    one = reader(api)

    assert (await one.place("C0GEN")).name == "general"
    assert (await one.place("G0PRIV")).kind == "private"
    assert (await one.place("D0COL")).user == COL
    unlisted = await one.place("D0NEW")
    assert (unlisted.id, unlisted.kind, unlisted.name) == ("D0NEW", "im", "")


async def test_a_list_that_cannot_be_read_still_leaves_the_conversation_readable_by_id(api):
    api.errors["users.conversations"] = SlackReadError("rate_limited")

    assert (await reader(api).place("C0GEN")).name == ""


# --- one read at a time ----------------------------------------------------------------------


async def test_search_is_one_function_over_one_method_with_the_window_as_dates(api):
    api.searches["budget"] = [match(COL, "the budget is done", ts(1)),
                              match(COL, "an old budget", ts(9))]

    page = await reader(api).search("budget", days=7)

    [params] = api.made("search.messages")
    assert params["query"] == "budget after:2023-11-06"
    assert (params["sort"], params["sort_dir"]) == ("timestamp", "desc")
    # The date is a day and the window a moment: what is older is dropped here.
    assert [read.text for read in page.reads] == ["the budget is done"] and not page.more


async def test_a_search_can_be_asked_for_what_is_older_than_a_message(api):
    api.searches["budget"] = [match(COL, "newer", ts(1)), match(COL, "older", ts(3))]

    page = await reader(api).search("budget", days=7, before=ts(2))

    assert api.made("search.messages")[0]["query"].endswith("before:2023-11-13")
    assert [read.text for read in page.reads] == ["older"]


async def test_a_history_says_whether_slack_had_more_and_never_decides_what_that_means(api):
    api.histories["D0COL"] = [message(COL, "hi", ts(1))]
    api.has_more.add("D0COL")
    one = reader(api)

    page = await one.history("D0COL", limit=1)

    assert page.more and not one.more, "the latest message alone is not a bound reached"


async def test_a_thread_is_followed_for_a_bounded_number_of_pages(api):
    answers = iter([
        {"ok": True, "messages": [message(COL, f"reply {n}", ts(1, n))], "has_more": True,
         "response_metadata": {"next_cursor": f"c{n}"}} for n in range(9)
    ])

    async def get(method, params):
        api.calls.append((method, dict(params)))
        return next(answers)

    api.get = get

    page = await reader(api).replies("C0GEN", ts(1))

    assert [read.text for read in page.reads] == ["reply 0", "reply 1"] and page.more
    assert len(api.calls) == MAX_PAGES


def test_only_a_bot_and_slacks_own_notices_are_not_a_person_writing():
    def automated(**fields):
        return slack_read._read(message(COL, "x", ts(1), **fields)).automated

    assert automated(bot_id="B1") and automated(subtype="channel_join")
    assert automated(subtype="bot_message")
    # A file shared, a reply also sent to the channel, a /me: a person wrote each.
    for subtype in ("file_share", "thread_broadcast", "me_message"):
        assert subtype not in SYSTEM_SUBTYPES and not automated(subtype=subtype)


def test_a_message_knows_who_replied_under_it_and_when_that_list_is_short():
    listed = slack_read._read(message(COL, "x", ts(1), reply_users=[OWNER, COL],
                                      reply_users_count=2))
    short = slack_read._read(message(COL, "x", ts(1), reply_users=[COL], reply_users_count=7))

    assert listed.reply_users == (OWNER, COL) and not listed.repliers_unlisted
    assert short.repliers_unlisted


def test_a_messages_ref_names_its_thread():
    top = Read("C0GEN", ts(1), COL, "x")
    reply = Read("C0GEN", ts(1, 5), COL, "y", thread_ts=ts(1))

    assert top.ref == reply.ref == f"C0GEN:{ts(1)}"


# --- names: who wrote it ---------------------------------------------------------------------


async def test_a_name_is_asked_for_once_a_call_and_an_id_will_do_when_slack_will_not_say(api):
    one = reader(api)

    assert [await one.name(COL), await one.name(COL)] == ["Col League", "Col League"]
    assert len(api.made("users.info")) == 1
    api.errors["users.info"] = SlackReadError("user_not_found")
    assert await one.name("U0NOBODY") == "U0NOBODY"
    assert await one.name("not a member") == "somebody"


async def test_only_the_owner_is_ever_called_you(api):
    """A member who shows the name `you` would otherwise be read as the owner."""
    api.names[EVE] = "You"
    one = reader(api)

    assert await one.name(OWNER) == "you"
    assert await one.name(EVE) == 'a member named "You"'
    said = await one.attributed(Read("C0GEN", ts(1), COL, f"<@{EVE}> and <@{OWNER}> agreed"))
    assert said.text == '@a member named "You" and @you agreed'


@pytest.mark.parametrize("name", ["you,", "You (the owner)", "YOU", "you - the owner", " you"])
async def test_a_name_that_starts_with_the_word_kept_for_the_owner_is_quoted(api, name):
    """`you,` said that, or `You (the owner)` approved it, must not read as the owner."""
    api.names[EVE] = name

    said = await reader(api).name(EVE)

    assert said.startswith('a member named "') and said != "you"


async def test_a_name_that_only_holds_the_word_later_is_left_as_it_is(api):
    api.names[EVE] = "Thank You Cards"

    assert await reader(api).name(EVE) == "Thank You Cards"


async def test_a_name_that_holds_a_backslash_or_a_group_reference_is_put_in_as_it_is(api):
    """Names are substituted by a function: nothing in one is read as a replacement."""
    api.names[EVE] = r"Eve \1 \g<0> \\"
    one = reader(api)

    said = await one.attributed(Read("C0GEN", ts(1), COL, f"ask <@{EVE}|eve> about it"))

    assert said.text == r"ask @Eve \1 \g<0> \\ about it"


async def test_slack_saying_it_is_asked_too_much_ends_the_asking_for_names(api):
    api.errors["users.info"] = SlackReadError("rate_limited")
    one = reader(api)

    names = [await one.name(member) for member in ("U0AAA", "U0BBB", "U0CCC")]

    assert names == ["U0AAA", "U0BBB", "U0CCC"]
    assert len(api.made("users.info")) == 1


async def test_only_the_first_few_members_a_message_mentions_are_named(api):
    members = [f"U0M{n:03d}" for n in range(40)]
    one = reader(api)

    said = await one.attributed(
        Read("C0GEN", ts(1), COL, " ".join(f"<@{each}>" for each in members))
    )

    assert len(api.made("users.info")) == 1 + MAX_NAMED  # the author, then the first few
    assert said.text.count("@someone") >= 1 and "<@" not in said.text


async def test_no_message_can_cost_much_to_attribute_by_its_shape(api):
    """Its text is cut to a line before anything is looked for in it."""
    worst = ["<@U0AAAAAAA>" * 40000, "<@" * 200000, ("<@U0AAAAAAA|" + "x" * 190) * 3000,
             "&lt;" * 100000, "<https://a|" * 50000, " \t\n" * 100000]
    one = reader(api)

    started = time.perf_counter()
    for text in worst:
        said = await one.attributed(Read("C0GEN", ts(1), COL, text))
        assert len(said.as_dict()["text"]) <= 1500
    assert time.perf_counter() - started < 1.0
    assert len(api.made("users.info")) <= 1 + MAX_NAMED


async def test_many_authors_are_named_from_the_member_list_not_one_request_each(api):
    """A channel has more authors than there are requests kept for names: past them a
    summary named people by their ids."""
    authors = [f"U0A{n:03d}" for n in range(12)]
    api.members = [member(each, f"Author {n}") for n, each in enumerate(authors)]
    one = reader(api)

    context = await one.context(
        [Read("C0GEN", ts(1, n), each, "hello") for n, each in enumerate(authors)], limit=12)

    assert [said.who for said in context.messages] == [f"Author {n}" for n in range(12)]
    assert len(api.made("users.list")) == 1 and api.made("users.info") == []
    assert not context.more, "a member list is not more messages"


async def test_a_few_authors_are_still_asked_about_one_by_one(api):
    one = reader(api)

    context = await one.context([Read("D0COL", ts(1), COL, "hi"),
                                 Read("D0EVE", ts(1), EVE, "hello")])

    assert [said.who for said in context.messages] == ["Col League", "Eve Adams"]
    assert api.made("users.list") == [] and len(api.made("users.info")) == 2


async def test_the_member_list_is_not_asked_for_names_when_the_reads_left_no_room(api):
    authors = [f"U0A{n:03d}" for n in range(12)]
    one = reader(api)
    for _ in range(MAX_REQUESTS - NAME_REQUESTS - DIRECTORY_PAGES + 1):
        await one.history("D0COL", limit=1)

    context = await one.context(
        [Read("C0GEN", ts(1, n), each, "hello") for n, each in enumerate(authors)], limit=12)

    asked = len(api.made("users.info"))
    assert api.made("users.list") == [] and asked == NAME_REQUESTS + DIRECTORY_PAGES - 1
    assert len(context.messages) == 12 and len(api.calls) == MAX_REQUESTS


async def test_a_member_list_that_fails_costs_only_the_names(api):
    authors = [f"U0A{n:03d}" for n in range(12)]
    api.errors["users.list"] = SlackReadError("missing_scope")
    one = reader(api)

    context = await one.context(
        [Read("C0GEN", ts(1, n), each, "hello") for n, each in enumerate(authors)], limit=12)

    assert len(context.messages) == 12 and not context.incomplete and not context.more


async def test_what_a_read_becomes_says_who_when_and_where_and_carries_its_ref(api):
    one = reader(api)

    direct = await one.attributed(Read("D0COL", ts(1), COL, "hi", kind="im"))
    channel = await one.attributed(Read("C0GEN", ts(1, 3), "", "deployed",
                                        channel_name="general", thread_ts=ts(1)))

    assert (direct.who, direct.where, direct.ref) == (
        "Col League", "a direct message", f"D0COL:{ts(1)}")
    assert (channel.who, channel.where, channel.ref) == ("an app", "#general", f"C0GEN:{ts(1)}")


async def test_a_context_says_what_was_left_out_and_what_could_not_be_read(api):
    one = reader(api)
    one.incomplete = True

    context = await one.context([Read("D0COL", ts(1, n), COL, "x") for n in range(3)], limit=2,
                                before=ts(1, 1))

    assert len(context.messages) == 2 and context.more and context.incomplete
    assert context.before == ts(1, 1)


async def test_closing_the_reader_closes_its_connections(api):
    one = reader(api)

    await one.aclose()

    assert api.closed
