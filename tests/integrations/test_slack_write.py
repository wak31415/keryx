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


@pytest.mark.parametrize("text", ["_@channel_ hi", "hi.@here", "(@everyone)", "*@channel*",
                                  "see\n@here", "@channel"])
def test_a_channel_wide_mention_is_refused_whatever_punctuation_is_before_it(text):
    assert message_problem(text) == NO_BROADCAST


@pytest.mark.parametrize("text", ["mail me@here.com", "ask x@channel.example", "sam2@everyone.io"])
def test_an_address_is_not_a_channel_wide_mention(text):
    """Only a letter or a digit right before the `@` makes it part of a word."""
    assert message_problem(text) is None


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
    assert [(each.ref, each.label) for each in several.value.candidates] == [
        (SAM, "Sam Lee"), ("U0SAM2", "Sam Wu")]


async def test_one_match_in_a_member_list_that_was_cut_short_is_asked_about_not_sent_to(api):
    """Any other Sam is on a page that was never read: this one is offered, not chosen."""
    one = reader(api)
    await one.people()
    one.people_cut = True

    with pytest.raises(Unclear, match="than could be read") as cut_short:
        await recipient(one, "Sam")

    [offered] = cut_short.value.candidates
    assert (await recipient(one, offered.ref)) == Recipient("person", "Sam Lee", member=SAM)


def test_a_recipient_is_named_exactly_by_its_id_with_nothing_looked_up():
    """What the confirming run of the command sends to: never a name resolved afresh."""
    from keryx.integrations.slack_write import exact

    assert exact(SAM) == Recipient("person", SAM, member=SAM)
    assert exact("C0GEN") == Recipient("channel", "C0GEN", channel="C0GEN")
    assert exact(f"C0GEN:{ts(1)}") == Recipient("thread", "C0GEN", channel="C0GEN",
                                                thread_ts=ts(1))
    for name in ("Sam", "general", "#general", "DAN", "me", ""):
        assert exact(name) is None
    assert [each.target for each in (exact(SAM), exact("C0GEN"), exact(f"C0GEN:{ts(1)}"))] == [
        SAM, "C0GEN", f"C0GEN:{ts(1)}"]


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


# --- never to Keryx itself: the loop a direct message with no mention would make ------------

APP = "U0KERYX"


@pytest.fixture
def with_app(api):
    """A workspace whose listings know the Keryx app: its name, and the owner's direct
    conversation with it."""
    api.names[APP] = "Keryx"
    api.channels.append({"id": "D0APP", "is_im": True, "user": APP})
    return api


@pytest.mark.parametrize("to", ["Keryx", "@keryx", "the keryx", APP, "D0APP", f"D0APP:{ts(1)}"])
async def test_the_app_itself_is_never_a_recipient_however_it_is_named(with_app, to):
    """By its name, its member id, the conversation with it, or a thread there."""
    with pytest.raises(Unclear) as refused:
        await recipient(reader(with_app), to, app=APP)

    assert refused.value.status == "refused"
    assert "a message to Keryx itself" in str(refused.value)


async def test_somebody_whose_name_is_not_the_apps_is_still_found(with_app):
    found = await recipient(reader(with_app), "Sam Lee", app=APP)

    assert found == Recipient("person", "Sam Lee", member=SAM)


async def test_a_message_sent_as_the_owner_can_never_set_keryx_to_work_on_itself(writer):
    """In the app's own direct messages every message of the owner's is a request, with
    no mention asked for: one Keryx sent there in their name would be a request from
    nobody, answered with work, whose report could ask for more. Refused in the path
    every send goes through, whatever resolved the recipient."""
    for to in (Recipient("person", "Keryx", member=APP),
               Recipient("person", "Keryx", channel="D0APP", member=APP),
               Recipient("thread", "Keryx", channel="D0APP", member=APP, thread_ts=ts(1)),
               Recipient("person", APP, member=APP)):
        with pytest.raises(SlackWriteError) as refused:
            await send(writer, to, "look into the flaky test", app=APP)
        assert refused.value.code == "to_the_app"

    assert writer.calls == [], "not even the conversation was opened"
    assert "never sent" in SlackWriteError("to_the_app").sentence


async def test_a_direct_conversation_named_only_by_its_id_is_checked_against_the_apps(writer):
    """The confirming run of the command is handed an id and looks no name up: which
    conversation the owner's with the app is, is asked of Slack before anything is
    posted to a direct message whose other member is not known."""
    own = "D0NEWRYX"  # what the fake opens for the app's member id

    with pytest.raises(SlackWriteError) as refused:
        await send(writer, Recipient("channel", own, channel=own), "hi", app=APP)
    with pytest.raises(SlackWriteError) as threaded:
        await send(writer, Recipient("thread", own, channel=own, thread_ts=ts(1)), "hi",
                   app=APP)
    await send(writer, Recipient("channel", "D0COL", channel="D0COL"), "hi", app=APP)

    assert refused.value.code == threaded.value.code == "to_the_app"
    posts = [payload["channel"] for name, payload in writer.calls if name == "chat.postMessage"]
    assert posts == ["D0COL"]


async def test_when_it_cannot_be_told_whether_it_is_the_apps_conversation_nothing_is_sent(
    writer,
):
    writer.errors["conversations.open"] = SlackWriteError("missing_scope")

    with pytest.raises(SlackWriteError) as refused:
        await send(writer, Recipient("channel", "D0COL", channel="D0COL"), "hi", app=APP)

    assert refused.value.code == "cannot_tell" and "was not sent" in refused.value.sentence
    assert [name for name, _ in writer.calls] == ["conversations.open"]


async def test_a_channel_and_a_known_member_cost_no_extra_request(writer):
    await send(writer, Recipient("channel", "#general", channel="C0GEN"), "hi", app=APP)
    await send(writer, Recipient("person", "Col League", channel="D0COL", member=COL), "hi",
               app=APP)

    assert [name for name, _ in writer.calls] == ["chat.postMessage", "chat.postMessage"]


def test_a_person_is_named_to_the_second_run_by_their_member_id():
    """So that who it is stays plain to the run that sends, and to its check."""
    assert Recipient("person", "Col League", channel="D0COL", member=COL).target == COL
    assert Recipient("person", "Sam Lee", member=SAM).target == SAM
    assert Recipient("channel", "#general", channel="C0GEN").target == "C0GEN"
    assert Recipient("thread", "Col League", channel="D0COL", member=COL,
                     thread_ts=ts(1)).target == f"D0COL:{ts(1)}"
