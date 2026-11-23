"""Reading Slack as the owner: bounded, read-only, and never over a network here."""

import inspect
import json
import logging

import httpx
import pytest

from keryx.integrations import slack_read
from keryx.integrations.slack_read import (
    MAX_CANDIDATES,
    MAX_DAYS,
    MAX_ITEMS,
    MAX_REQUESTS,
    READ_METHODS,
    HttpSlackApi,
    SlackReader,
    SlackReadError,
    Unclear,
    classify,
    conversation_id,
    message_ts,
    window,
)

OWNER, COL, EVE = "U0OWNER", "U0COL", "U0EVE"
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


class FakeSlackApi:
    """The Web API as the reader meets it: each method answers from a table, and every
    call is recorded. A method that is not a read fails the test that asked for it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.searches: dict[str, list] = {}
        self.histories: dict[str, list] = {}
        self.threads: dict[tuple[str, str], list] = {}
        self.names = {COL: "Col League", EVE: "Eve Adams"}
        self.errors: dict[str, SlackReadError] = {}
        self.closed = False

    async def get(self, method: str, params) -> dict:
        assert method in READ_METHODS, method
        self.calls.append((method, dict(params)))
        if method in self.errors:
            raise self.errors[method]
        if method == "search.messages":
            key = next((key for key in self.searches if params["query"].startswith(key)), None)
            found = self.searches.get(key, [])
            return {"ok": True, "messages": {"matches": found, "total": len(found)}}
        if method == "conversations.history":
            return {"ok": True, "messages": self.histories.get(params["channel"], [])}
        if method == "conversations.replies":
            return {"ok": True,
                    "messages": self.threads.get((params["channel"], params["ts"]), [])}
        if method == "users.info":
            return {"ok": True, "user": {"real_name": self.names.get(params["user"], "")}}
        if method == "users.conversations":
            return {"ok": True, "channels": [{"id": "D0COL", "is_im": True, "user": COL}]}
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
        "conversations.info", "search.messages", "users.info",
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


async def test_one_answer_never_makes_more_than_its_budget_of_requests(api):
    one = reader(api, budget=3)

    for _ in range(3):
        await one.history("D0COL", limit=1)
    with pytest.raises(SlackReadError) as over:
        await one.history("D0COL", limit=1)

    assert over.value.code == "over_budget" and len(api.calls) == 3 and one.more
    assert MAX_REQUESTS == 30


async def test_a_list_is_followed_for_a_bounded_number_of_pages(api, monkeypatch):
    async def endless(method, params):
        api.calls.append((method, dict(params)))
        return {"ok": True, "channels": [{"id": "D1"}],
                "response_metadata": {"next_cursor": "more"}}

    monkeypatch.setattr(api, "get", endless)
    one = reader(api)

    found = await one.conversations()

    assert len(api.calls) == slack_read.MAX_PAGES and len(found) == slack_read.MAX_PAGES
    assert one.more


def test_how_far_back_is_held_between_a_day_and_a_month():
    assert window(None) == 7 and window(3) == 3 and window("14") == 14
    assert window(0) == 1 and window(-5) == 1 and window(400) == MAX_DAYS
    assert window("soon") == 7


def test_nothing_but_an_id_or_a_timestamp_is_ever_put_in_a_request():
    assert conversation_id(" C0123ABCD ") == "C0123ABCD" and conversation_id("D0COL") == "D0COL"
    assert message_ts("1700000000.000100") == "1700000000.000100"
    for bad in ("general", "#general", "C1&latest=0", "U0OWNER", ""):
        with pytest.raises(Unclear):
            conversation_id(bad)
    for bad in ("yesterday", "1700000000", "1.1&x=y"):
        with pytest.raises(Unclear):
            message_ts(bad)


async def test_a_name_is_asked_for_once_a_call_and_an_id_will_do_when_slack_will_not_say(api):
    one = reader(api)

    assert [await one.name(COL), await one.name(COL)] == ["Col League", "Col League"]
    assert len(api.made("users.info")) == 1
    assert await one.name(OWNER) == "you" and await one.name("not an id") == "somebody"
    api.errors["users.info"] = SlackReadError("rate_limited")
    assert await one.name(EVE) == EVE


# --- search: one function -------------------------------------------------------------------


async def test_search_is_one_function_over_one_method_with_the_window_as_a_date(api):
    api.searches["budget"] = [match(COL, "the budget is done", ts(1), "C0GEN", kind="channel",
                                    name="general"),
                              match(COL, "an old budget", ts(40), "C0GEN", kind="channel")]
    one = reader(api)

    context = await slack_read.search(one, "  budget  ", days=7)

    [params] = api.made("search.messages")
    assert params["query"] == "budget after:2023-11-06"
    assert (params["sort"], params["sort_dir"]) == ("timestamp", "desc")
    # Only what is inside the window, whatever the day-wide date let through.
    assert [m.text for m in context.messages] == ["the budget is done"]
    assert context.messages[0].where == "#general" and context.messages[0].who == "Col League"
    assert {name for name, _ in api.calls} == {"search.messages", "users.info"}


async def test_search_needs_words(api):
    with pytest.raises(Unclear, match="needs the words"):
        await slack_read.search(reader(api), "   ", days=7)
    assert api.calls == []


# --- unanswered: a best effort ---------------------------------------------------------------


async def test_a_direct_message_whose_latest_word_is_somebody_elses_is_unanswered(api):
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(COL, "can you review my PR?", ts(1))]

    context = await slack_read.unanswered(reader(api), days=7)

    [waiting] = context.messages
    assert (waiting.who, waiting.where, waiting.text) == (
        "Col League", "a direct message", "can you review my PR?"
    )
    assert api.made("conversations.history") == [{"channel": "D0COL", "limit": "1"}]


async def test_one_the_owner_has_answered_since_is_not(api):
    api.searches["to:me"] = [match(COL, "can you review my PR?", ts(1))]
    api.histories["D0COL"] = [message(OWNER, "done", ts(0.5))]

    assert (await slack_read.unanswered(reader(api), days=7)).messages == ()


async def test_a_bots_last_word_is_nobody_waiting(api):
    api.searches["to:me"] = [match(COL, "ping", ts(1))]
    api.histories["D0COL"] = [message("U0BOT", "reminder", ts(0.2), bot_id="B1")]

    assert (await slack_read.unanswered(reader(api), days=7)).messages == ()


async def test_a_conversation_counts_once_however_many_messages_it_holds(api):
    api.searches["to:me"] = [match(COL, "third", ts(1, 3)), match(COL, "second", ts(1, 2)),
                             match(EVE, "hi", ts(2), "G0GROUP", kind="mpim")]
    api.histories["D0COL"] = [message(COL, "third", ts(1, 3))]
    api.histories["G0GROUP"] = [message(EVE, "hi", ts(2))]

    context = await slack_read.unanswered(reader(api), days=7)

    assert [(m.who, m.where, m.text) for m in context.messages] == [
        ("Col League", "a direct message", "third"), ("Eve Adams", "a group message", "hi"),
    ]
    assert len(api.made("conversations.history")) == 2


async def test_a_mention_in_a_channel_the_owner_has_not_written_in_since_is_unanswered(api):
    asked = match(COL, f"<@{OWNER}> can you weigh in?", ts(1), "C0GEN", kind="channel",
                  name="general")
    api.searches[f"<@{OWNER}>"] = [asked]
    api.threads[("C0GEN", asked["ts"])] = [message(COL, asked["text"], asked["ts"])]
    api.histories["C0GEN"] = [message(EVE, "bump", ts(0.5))]

    context = await slack_read.unanswered(reader(api), days=7)

    [waiting] = context.messages
    assert (waiting.who, waiting.where) == ("Col League", "#general")
    assert waiting.text == "@you can you weigh in?"
    assert waiting.link.startswith("https://acme.slack.com/archives/C0GEN/p")


@pytest.mark.parametrize("where", ["thread", "channel"])
async def test_a_mention_the_owner_wrote_after_is_not(api, where):
    asked = match(COL, f"<@{OWNER}> can you weigh in?", ts(1), "C0GEN", kind="channel")
    api.searches[f"<@{OWNER}>"] = [asked]
    answer = message(OWNER, "yes, go ahead", ts(0.5))
    api.threads[("C0GEN", asked["ts"])] = [message(COL, "…", asked["ts"])] + (
        [answer] if where == "thread" else [])
    api.histories["C0GEN"] = [answer] if where == "channel" else []

    assert (await slack_read.unanswered(reader(api), days=7)).messages == ()


async def test_a_mention_inside_a_thread_is_judged_by_that_thread_alone(api):
    asked = match(COL, f"<@{OWNER}> thoughts?", ts(1, 5), "C0GEN", kind="channel",
                  thread=ts(2))
    api.searches[f"<@{OWNER}>"] = [asked]
    api.threads[("C0GEN", ts(2))] = [message(COL, "thoughts?", ts(1, 5))]

    context = await slack_read.unanswered(reader(api), days=7)

    assert len(context.messages) == 1 and api.made("conversations.history") == []
    assert api.made("conversations.replies") == [
        {"channel": "C0GEN", "ts": ts(2), "limit": "60", "oldest": ts(1, 5)}
    ]


async def test_the_owners_own_mentions_and_bots_are_nobody_waiting(api):
    api.searches[f"<@{OWNER}>"] = [
        match(OWNER, f"note to <@{OWNER}>", ts(1), "C0GEN", kind="channel"),
        {**match("U0BOT", f"<@{OWNER}> build failed", ts(1, 2), "C0CI", kind="channel"),
         "bot_id": "B1"},
    ]

    context = await slack_read.unanswered(reader(api), days=7)

    assert context.messages == () and api.made("conversations.replies") == []


async def test_only_so_many_are_looked_into_and_listed_and_the_rest_is_more(api):
    api.searches["to:me"] = [match(COL, f"m{n}", ts(1, n), f"D0{n:03d}") for n in range(20)]
    for n in range(20):
        api.histories[f"D0{n:03d}"] = [message(COL, f"m{n}", ts(1, n))]

    one = reader(api)
    context = await slack_read.unanswered(one, days=7)

    assert len(api.made("conversations.history")) == MAX_CANDIDATES
    assert len(context.messages) == min(MAX_CANDIDATES, MAX_ITEMS) and context.more
    assert len(api.calls) <= MAX_REQUESTS


async def test_one_conversation_that_cannot_be_read_does_not_cost_the_rest(api, monkeypatch):
    api.searches["to:me"] = [match(COL, "a", ts(1)), match(EVE, "b", ts(2), "D0EVE")]
    api.histories["D0EVE"] = [message(EVE, "b", ts(2))]
    real = api.get

    async def flaky(method, params):
        if method == "conversations.history" and params["channel"] == "D0COL":
            raise SlackReadError("not_found")
        return await real(method, params)

    monkeypatch.setattr(api, "get", flaky)

    context = await slack_read.unanswered(reader(api), days=7)

    # What could not be confirmed is not claimed, and the answer says there may be more.
    assert [m.who for m in context.messages] == ["Eve Adams"] and context.more


async def test_slack_refusing_both_searches_is_the_failure_it_is(api):
    api.errors["search.messages"] = SlackReadError("rate_limited")

    with pytest.raises(SlackReadError) as failed:
        await slack_read.unanswered(reader(api), days=7)

    assert failed.value.code == "rate_limited"


# --- mentions, a conversation, a thread ---------------------------------------------------


async def test_mentions_are_listed_newest_first_answered_or_not(api):
    api.searches[f"<@{OWNER}>"] = [
        match(COL, f"<@{OWNER}> and <@{EVE}|eve> look", ts(1), "C0GEN", kind="channel",
              name="general"),
        match(OWNER, f"<@{OWNER}> mine", ts(2), "C0GEN", kind="channel"),
    ]

    context = await slack_read.mentions(reader(api), days=7)

    assert [m.text for m in context.messages] == ["@you and @Eve Adams look"]


async def test_a_conversations_history_is_read_oldest_first_for_a_subagent(api):
    api.histories["C0GEN"] = [message(EVE, "second", ts(1)), message(COL, "first", ts(2))]

    context = await slack_read.history(reader(api), "C0GEN", days=7)

    assert [(m.who, m.text, m.where) for m in context.messages] == [
        ("Col League", "first", "C0GEN"), ("Eve Adams", "second", "C0GEN"),
    ]
    [params] = api.made("conversations.history")
    assert params["oldest"] == f"{NOW - 7 * DAY:.6f}"


async def test_a_thread_is_read_whole_and_in_order(api):
    api.threads[("C0GEN", ts(2))] = [message(COL, "the question", ts(2)),
                                    message(EVE, "the answer", ts(1))]

    context = await slack_read.thread(reader(api), "C0GEN", ts(2))

    assert [m.text for m in context.messages] == ["the question", "the answer"]


async def test_a_conversation_or_a_message_that_is_not_one_asks_slack_nothing(api):
    for call in (slack_read.history(reader(api), "general", days=7),
                 slack_read.thread(reader(api), "C0GEN", "yesterday")):
        with pytest.raises(Unclear):
            await call
    assert api.calls == []


async def test_names_that_cannot_be_looked_up_any_more_do_not_lose_the_messages(api):
    api.histories["C0GEN"] = [message(COL, "first", ts(2)), message(EVE, "second", ts(1))]
    one = reader(api, budget=1)

    context = await slack_read.history(one, "C0GEN", days=7)

    assert [m.text for m in context.messages] == ["second", "first"]
    # A name is a nicety: past the budget each is said by its id, and nothing is lost.
    assert {m.who for m in context.messages} == {COL, EVE} and context.more


async def test_closing_the_reader_closes_its_connections(api):
    await reader(api).aclose()

    assert api.closed
