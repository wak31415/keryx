"""Sending one message as the owner: two methods, nothing guessed, never over a network."""

import inspect

import httpx
import pytest

from integrations.test_slack_read import COL, OWNER, SAM, FakeSlackApi, member, reader, ts
from keryx.integrations import slack_read, slack_write
from keryx.integrations.slack_read import READ_METHODS, Unclear
from keryx.integrations.slack_write import (
    MESSAGE_CHARS,
    NO_BROADCAST,
    NO_MESSAGE,
    TOO_LONG,
    WRITE_METHODS,
    HttpSlackWriter,
    Recipient,
    SlackWriteError,
    escaped,
    message_problem,
    recipient,
    send,
)


class FakeWriter:
    """The two calls there are, recorded; a method that is neither fails the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, Exception] = {}
        self.closed = False

    async def call(self, method: str, payload) -> dict:
        assert method in WRITE_METHODS, method
        self.calls.append((method, dict(payload)))
        if method in self.errors:
            raise self.errors[method]
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D0NEW" + payload["users"][-3:]}}
        return {"ok": True, "channel": payload["channel"], "ts": "1700000500.000100"}

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def api():
    return FakeSlackApi()


@pytest.fixture
def writer():
    return FakeWriter()


# --- two methods, and no other ----------------------------------------------------------------


def test_the_write_client_can_post_one_message_and_open_a_direct_conversation_and_nothing_else():
    """The whole of what Keryx can do with the owner's token to anybody else."""
    assert WRITE_METHODS == {"chat.postMessage", "conversations.open"}
    public = [name for name in vars(HttpSlackWriter) if not name.startswith("_")]
    assert sorted(public) == ["aclose", "call"]
    source = inspect.getsource(slack_write)
    for never in ("chat.update", "chat.delete", "files.", "reactions.", "conversations.join",
                  "chat.scheduleMessage", "reply_broadcast", "conversations.invite"):
        assert never not in source


def test_the_read_client_still_cannot_post():
    """Two clients, so that this stays true by construction."""
    assert not READ_METHODS & WRITE_METHODS
    assert not hasattr(slack_read.HttpSlackApi, "call")
    assert not hasattr(HttpSlackWriter, "get")
    assert ".post(" not in inspect.getsource(slack_read)


async def test_a_method_that_is_not_one_of_the_two_is_refused_before_anything_is_sent():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    client = HttpSlackWriter("xoxp-secret", transport=httpx.MockTransport(handler))

    for method in ("chat.update", "chat.delete", "files.upload", "conversations.join",
                   "reactions.add", "conversations.history", "chat.scheduleMessage"):
        with pytest.raises(SlackWriteError) as refused:
            await client.call(method, {"channel": "C1"})
        assert refused.value.code == "not_a_write"
    assert sent == []
    await client.aclose()


async def test_a_post_is_json_with_the_token_in_a_header_and_never_in_the_address():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "channel": "C0GEN", "ts": "1.1"})

    client = HttpSlackWriter("xoxp-secret", transport=httpx.MockTransport(handler))

    await client.call("chat.postMessage", {"channel": "C0GEN", "text": "hi"})

    [request] = seen
    assert request.method == "POST" and str(request.url) == (
        "https://slack.com/api/chat.postMessage")
    assert request.headers["Authorization"] == "Bearer xoxp-secret"
    assert b"xoxp" not in request.content and b'"text":"hi"' in request.content.replace(
        b" ", b"")
    await client.aclose()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json={"ok": False, "error": "missing_scope"}), "missing_scope"),
        (httpx.Response(200, json={"ok": False, "error": "token_revoked"}), "signed_out"),
        (httpx.Response(200, json={"ok": False, "error": "channel_not_found"}), "not_found"),
        (httpx.Response(200, json={"ok": False, "error": "restricted_action"}),
         "restricted_action"),
        (httpx.Response(429, json={"ok": False}), "rate_limited"),
        (httpx.Response(200, text="<html>"), "unreachable"),
        (httpx.Response(200, json=["not", "an", "object"]), "unreachable"),
    ],
)
async def test_a_failure_is_a_status_and_a_sentence_that_says_whether_it_was_sent(
    response, code
):
    client = HttpSlackWriter("xoxp-secret", transport=httpx.MockTransport(lambda r: response))

    with pytest.raises(SlackWriteError) as failed:
        await client.call("chat.postMessage", {"channel": "C0GEN", "text": "hi"})

    assert failed.value.code == code and "xoxp" not in failed.value.sentence
    said = failed.value.as_dict()["message"]
    assert "not sent" in said or "not known whether" in said
    await client.aclose()


async def test_slack_not_answering_leaves_it_unknown_whether_it_was_sent():
    def handler(request):
        raise httpx.ConnectTimeout("no")

    client = HttpSlackWriter("xoxp-secret", transport=httpx.MockTransport(handler))

    with pytest.raises(SlackWriteError) as failed:
        await client.call("chat.postMessage", {"channel": "C0GEN", "text": "hi"})

    assert failed.value.code == "unreachable"
    assert "look before sending it again" in failed.value.sentence
    await client.aclose()


# --- the message --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["@channel the build is fixed", "heads up @here", "@Everyone lunch", "<!channel> hi",
     "<!here|here> hi", "<!everyone>", "<!subteam^S0ENG|@eng> deploy", "hi @HERE."],
)
def test_a_message_that_would_notify_a_whole_channel_is_refused_in_one_sentence(text):
    assert message_problem(text) == NO_BROADCAST
    assert "never sent" in NO_BROADCAST and NO_BROADCAST.count(". ") == 1


def test_an_empty_message_and_one_too_long_are_refused_and_an_ordinary_one_is_not():
    assert message_problem("  \n") == NO_MESSAGE
    assert message_problem("x" * (MESSAGE_CHARS + 1)) == TOO_LONG
    for fine in ("I am running late", "see you there, channel 4 is free", "email me@here.com",
                 "x" * MESSAGE_CHARS):
        assert message_problem(fine) is None


def test_the_text_is_posted_as_it_was_said_and_nothing_in_it_is_markup():
    assert escaped("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert escaped("<@U0EVE> <https://evil.example|click> <#C0GEN>") == (
        "&lt;@U0EVE&gt; &lt;https://evil.example|click&gt; &lt;#C0GEN&gt;")


# --- who it goes to: resolved, never guessed --------------------------------------------------


async def test_a_person_is_any_member_and_their_conversation_is_opened_only_on_sending(api):
    found = await recipient(reader(api), "Sam Lee")

    assert found == Recipient("person", "Sam Lee", member=SAM)
    assert found.said == "a direct message to Sam Lee"
    assert {name for name, _ in api.calls} <= READ_METHODS, "resolving only ever reads"


async def test_a_channel_is_one_of_the_owners_own_by_the_name_slack_gives_it(api):
    found = await recipient(reader(api), "the general channel")

    assert found == Recipient("channel", "#general", channel="C0GEN")
    assert found.said == "#general"


async def test_a_ref_with_a_message_in_it_is_a_reply_in_that_thread(api):
    found = await recipient(reader(api), f"C0GEN:{ts(1)}")
    direct = await recipient(reader(api), f"D0COL:{ts(1)}")

    assert found == Recipient("thread", "#general", channel="C0GEN", thread_ts=ts(1))
    assert found.said == "a reply in a thread in #general"
    assert direct.said == "a reply in a thread in Col League"


async def test_a_ref_without_one_is_that_conversation(api):
    found = await recipient(reader(api), "D0COL")

    assert found == Recipient("person", "Col League", channel="D0COL", member=COL)


async def test_an_ambiguous_name_is_unclear_with_the_candidates_and_nothing_is_chosen(api):
    api.members.append(member("U0SAM2", "Sam Wu"))

    with pytest.raises(Unclear) as several:
        await recipient(reader(api), "Sam")

    assert several.value.status == "unclear"
    assert [each.label for each in several.value.candidates] == ["Sam Lee", "Sam Wu"]


@pytest.mark.parametrize(
    ("to", "status", "says"),
    [
        ("the moon committee", "no_match", "Nothing on their Slack is called"),
        ("C0SECRET", "no_match", "not one of theirs"),
        (f"C0SECRET:{ts(1)}", "no_match", "not one of theirs"),
        ("me", "unclear", "a message to themselves"),
        ("", "unclear", "say who it goes to"),
        ("x" * 200, "unclear", "say who it goes to"),
    ],
)
async def test_who_it_cannot_go_to_is_said_and_never_worked_around(api, to, status, says):
    with pytest.raises(Unclear, match=says) as refused:
        await recipient(reader(api), to)

    assert refused.value.status == status


# --- sending -----------------------------------------------------------------------------------


async def test_a_message_to_a_person_opens_their_conversation_and_posts_once(writer):
    sent = await send(writer, Recipient("person", "Sam Lee", member=SAM), "running late")

    assert [name for name, _ in writer.calls] == ["conversations.open", "chat.postMessage"]
    assert writer.calls[0][1] == {"users": SAM}
    assert writer.calls[1][1] == {
        "channel": "D0NEWSAM", "text": "running late", "link_names": False,
        "unfurl_links": False, "unfurl_media": False,
    }
    assert (sent.channel, sent.ts) == ("D0NEWSAM", "1700000500.000100")


async def test_a_message_to_a_channel_is_one_post_and_a_reply_carries_its_thread(writer):
    await send(writer, Recipient("channel", "#general", channel="C0GEN"), "it is fixed")
    await send(writer, Recipient("thread", "#general", channel="C0GEN", thread_ts=ts(1)),
               "a < b")

    first, second = (payload for _, payload in writer.calls)
    assert [name for name, _ in writer.calls] == ["chat.postMessage"] * 2
    assert "thread_ts" not in first
    assert (second["thread_ts"], second["text"]) == (ts(1), "a &lt; b")
    assert "reply_broadcast" not in second, "a reply is never also sent to the channel"


async def test_nothing_is_posted_that_the_check_refuses_whatever_the_caller_did(writer):
    for text in ("", "@channel hi", "x" * (MESSAGE_CHARS + 1)):
        with pytest.raises(SlackWriteError) as refused:
            await send(writer, Recipient("channel", "#general", channel="C0GEN"), text)
        assert refused.value.code == "refused_message"

    assert writer.calls == []


async def test_a_conversation_or_a_thread_that_is_not_an_id_is_never_posted_to(writer):
    writer.errors["conversations.open"] = None

    async def opened(method, payload):
        writer.calls.append((method, dict(payload)))
        return {"ok": True, "channel": {"id": "not an id"}}

    writer.call = opened
    with pytest.raises(SlackWriteError):
        await send(writer, Recipient("person", "Sam Lee", member=SAM), "hi")
    with pytest.raises(SlackWriteError):
        await send(writer, Recipient("thread", "#general", channel="C0GEN",
                                     thread_ts="yesterday"), "hi")

    assert [name for name, _ in writer.calls] == ["conversations.open"]


async def test_a_refusal_from_slack_is_the_failure_it_is(writer):
    writer.errors["chat.postMessage"] = SlackWriteError("missing_scope")

    with pytest.raises(SlackWriteError) as failed:
        await send(writer, Recipient("channel", "#general", channel="C0GEN"), "hi")

    assert failed.value.code == "missing_scope"
    assert "reinstalled at the write level" in failed.value.sentence
    assert OWNER not in failed.value.sentence
