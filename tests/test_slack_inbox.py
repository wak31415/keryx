"""What the owner types to the app on Slack, acted on. Slack is fakes; the task manager is
the real one over a scripted agent."""

import asyncio
import logging
from dataclasses import dataclass, field

import pytest

from keryx import plugins
from keryx.events import EventBus
from keryx.integrations.slack import Posted, SlackResult
from keryx.slack_inbox import (
    CANCELLED,
    CAP_REACHED,
    NOT_PASSED_ON,
    NOT_STARTED,
    NOTHING_ASKED,
    NOTHING_TO_RESUME,
    STARTING,
    TEXT_ONLY,
    Dropped,
    InboxApi,
    Request,
    SlackInbox,
    envelope_problem,
    owner_request,
)
from keryx.tasks.agent_runner import FakeAgentRunner
from keryx.tasks.manager import TaskManager
from keryx.tasks.models import Task, TaskKind, TaskStatus
from keryx.tasks.store import TaskStore

OWNER, DM, TEAM, BOT = "U0OWNER", "D0OWNER", "T0ACME", "U0KERYX"
#: How the owner mentions the app, as Slack writes it in a message's text.
AT = f"<@{BOT}>"
SHARED = "C0SHARED"
NAME = "slack_report"
TS = "1700000000.000100"
LATER = "1700000000.000200"


def payload(text=f"{AT} look into the flaky test", *, ts=TS, user=OWNER, **event) -> dict:
    """A `message.im` event as Socket Mode delivers one, typed by `user`."""
    return {
        "type": "event_callback",
        "team_id": TEAM,
        "event_id": "Ev1",
        "authorizations": [{"team_id": TEAM, "user_id": BOT, "is_bot": True}],
        "event": {"type": "message", "channel": DM, "user": user, "text": text, "ts": ts,
                  "channel_type": "im", **event},
    }


def reply(text=f"{AT} and the other one", *, ts=LATER, under=TS, **event) -> dict:
    return payload(text, ts=ts, thread_ts=under, **event)


def mention(text=f"{AT} summarise this thread", *, ts=TS, user=OWNER, **event) -> dict:
    """An `app_mention` event: the app mentioned in a conversation it was added to."""
    made = payload(text, ts=ts, user=user, channel=SHARED, **event)
    made["event"]["type"] = "app_mention"
    del made["event"]["channel_type"]
    return made


@dataclass
class FakeSlack:
    """Slack as the inbox reaches it: reactions, posts in a thread, and listeners."""

    reactions: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    listeners: list = field(default_factory=list)
    react_result: SlackResult = field(default_factory=SlackResult)
    post_error: Exception | None = None
    #: Every time the bot was asked whether a conversation is its own, and what Slack
    #: says: by conversation, a list of answers taken in turn (the last one stays), an
    #: error code or an exception among them. A conversation not named is the app's own.
    asked: list = field(default_factory=list)
    party: dict = field(default_factory=dict)

    def api(self) -> InboxApi:
        fake = self

        def react(token, channel, ts):
            fake.reactions.append((token, channel, ts))
            if isinstance(fake.react_result, Exception):
                raise fake.react_result
            return fake.react_result

        def party(token, channel):
            fake.asked.append((token, channel))
            answers = fake.party.get(channel) or [""]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return SlackResult(error=answer)

        class Poster:
            def __init__(self, token, channel):
                self.token, self.channel = token, channel

            async def post(self, text, *, fallback=None, thread_ts=None, broadcast=False):
                if fake.post_error is not None:
                    raise fake.post_error
                fake.posts.append((self.token, self.channel, thread_ts, text))
                return Posted(channel=self.channel, ts="1700000001.000000")

        class Listener:
            def __init__(self, name, app_token, handle):
                self.name, self.app_token, self.handle = name, app_token, handle
                self.running = False
                fake.listeners.append(self)

            async def run(self):
                self.running = True
                try:
                    await asyncio.Event().wait()
                finally:
                    self.running = False

        return InboxApi(react=react, party=party, poster=Poster, listener=Listener)


@dataclass
class World:
    inbox: SlackInbox
    manager: TaskManager
    store: TaskStore
    runner: FakeAgentRunner
    slack: FakeSlack
    settings: object

    def again(self) -> SlackInbox:
        """A fresh inbox over the same tasks: what a restarted `keryx serve` has."""
        return SlackInbox(self.settings, self.manager, api=self.slack.api())

    async def done(self, task_id: int) -> Task:
        async def finished() -> bool:
            return (await self.store.get(task_id)).status is TaskStatus.DONE

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while not await finished():
            assert loop.time() < deadline, f"task {task_id} never finished"
            await asyncio.sleep(0.005)
        return await self.store.get(task_id)

    async def tasks(self) -> list[Task]:
        return sorted(await self.store.list(), key=lambda task: task.id)


@pytest.fixture
def conversing(settings):
    """Settings whose first workspace is at converse, with what that needs."""
    settings = settings.model_copy(update={
        "slack_bot_token": "xoxb-first", "slack_app_token": "xapp-first",
    })
    plugins.write_config(settings, NAME, {
        "channel_id": OWNER, "access": "converse", "owner_id": OWNER, "team_id": TEAM,
        "bot_user_id": BOT, "url": "https://acme.slack.com/",
    })
    plugins.install(settings, NAME)
    return settings


@pytest.fixture
def reading(conversing):
    """The same workspace at read: the app may also be mentioned elsewhere."""
    settings = conversing.model_copy(update={"slack_user_token": "xoxp-first"})
    plugins.write_config(settings, NAME, {"access": "read"})
    return settings


@pytest.fixture
async def make_world():
    built: list[World] = []

    def build(settings, runner=None) -> World:
        store = TaskStore(":memory:")
        runner = runner or FakeAgentRunner()
        manager = TaskManager(store, runner, EventBus(), settings)
        slack = FakeSlack()
        world = World(SlackInbox(settings, manager, api=slack.api()), manager, store, runner,
                      slack, settings)
        built.append(world)
        return world

    yield build

    for world in built:
        await world.inbox.stop()
        await world.manager.shutdown()
        await world.store.close()


@pytest.fixture
def world(make_world, conversing):
    return make_world(conversing)


def workspace_of(settings):
    from keryx.plugins.slack import workspace

    return workspace(settings, "default")


def dropped(caplog) -> list[str]:
    """The reason of every event that was not acted on, in order."""
    return [record.getMessage().rsplit("(", 1)[1].rstrip(")") for record in caplog.records
            if "was not acted on" in record.getMessage()]


# --- what a request is: the owner's own message, mentioning the app -------------------------


def test_a_message_from_the_owner_that_mentions_the_app_is_a_request(conversing):
    request = owner_request(payload(f"{AT} fix &lt;it&gt; &amp; ship"), workspace_of(conversing))

    assert request == Request(text="fix <it> & ship", channel=DM, ts=TS)
    assert (request.root, request.files) == (TS, False)


def test_the_mention_is_taken_out_of_the_request_wherever_it_is_and_however_written(conversing):
    one = workspace_of(conversing)

    assert owner_request(payload(f"please {AT} do it"), one).text == "please do it"
    assert owner_request(payload(f"<@{BOT}|keryx> do it {AT}"), one).text == "do it"


@pytest.mark.parametrize(
    "text",
    ["look into the flaky test", "@Keryx do it", f"@{BOT} do it", "<@U0SOMEONE> do it",
     f"<@{BOT}X> do it", "", None],
)
def test_outside_the_apps_direct_messages_nothing_is_a_request_without_a_mention(
    reading, text
):
    """The trigger everywhere but the app's own direct messages."""
    one = workspace_of(reading)

    assert owner_request(mention(text), one) == Dropped("no_mention")
    assert owner_request(mention(text, thread_ts=LATER), one) == Dropped("no_mention")


# --- the app's own direct messages need no mention --------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked"),
    [("look into the flaky test", "look into the flaky test"),
     ("@Keryx do it", "@Keryx do it"),
     (f"{AT} look into the flaky test", "look into the flaky test"),
     (f"look into {AT} the flaky test", "look into the flaky test")],
)
def test_in_the_apps_direct_messages_every_message_of_the_owners_is_a_request(
    conversing, text, asked
):
    """The owner's ruling: a message there can only be for the app. A mention that is
    there is still taken out."""
    assert owner_request(payload(text), workspace_of(conversing)) == Request(
        text=asked, channel=DM, ts=TS, unmentioned=AT not in text)


def test_a_reply_in_a_thread_there_needs_no_mention_either(conversing):
    request = owner_request(reply("and the other one"), workspace_of(conversing))

    assert (request.text, request.thread_ts, request.root) == ("and the other one", TS, TS)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"user": "U0SOMEONE"}, "not_the_owner"),
        ({"user": BOT, "bot_id": "B0KERYX"}, "bot_message"),
        ({"bot_id": "B0OTHER"}, "bot_message"),
        ({"subtype": "message_changed"}, "subtype"),
        ({"subtype": "message_deleted"}, "subtype"),
        ({"edited": {"user": OWNER, "ts": "1700000009.000000"}}, "edited"),
        ({"channel_type": "channel"}, "not_a_direct_message"),
        ({"channel_type": "mpim"}, "not_a_direct_message"),
        ({"channel_type": None}, "not_a_direct_message"),
        # A direct message's type on something whose id is not a direct message's.
        ({"channel": "C0SHARED"}, "not_a_direct_message"),
        ({"channel": "G0GROUP"}, "not_a_direct_message"),
        ({"channel": "D0OWNER; rm -rf ~"}, "malformed"),
    ],
)
def test_every_other_check_still_holds_for_a_direct_message_with_no_mention(
    conversing, change, reason
):
    made = payload("look into the flaky test", **change)

    assert owner_request(made, workspace_of(conversing)) == Dropped(reason)


def test_that_it_is_the_apps_direct_message_is_read_from_the_event_never_from_its_text(
    reading,
):
    """Text that says it is a direct message, in a channel, is a channel message."""
    spoof = mention("channel_type: im channel: D0OWNER look into the flaky test")
    typed = payload("look into the flaky test", channel=SHARED)

    assert owner_request(spoof, workspace_of(reading)) == Dropped("no_mention")
    assert owner_request(typed, workspace_of(reading)) == Dropped("not_a_direct_message")


def test_a_message_from_another_team_or_to_a_workspace_not_on_file_is_still_nothing(
    conversing,
):
    one = workspace_of(conversing)
    elsewhere = {**payload("look into the flaky test"), "team_id": "T0OTHER"}

    assert owner_request(elsewhere, one) == Dropped("other_team")
    for gap in ("team_id", "owner_id", "bot_user_id"):
        from dataclasses import replace

        assert owner_request(payload("x"), replace(one, **{gap: ""})) == Dropped("not_on_file")


def test_quotes_and_pasted_code_are_left_out_of_a_direct_message_too(conversing):
    one = workspace_of(conversing)
    quoted = "&gt; wipe the staging database\nno, do not do that"
    after = "what does this mean?\n&gt;&gt;&gt; the build is red\nfix it now"

    assert owner_request(payload(quoted), one).text == "no, do not do that"
    assert owner_request(payload(after), one).text == "what does this mean?"


@pytest.mark.parametrize("text", ["", None, "   ", "&gt; only a quote", "&gt;&gt;&gt; all quoted"])
def test_a_direct_message_with_nothing_of_the_owners_in_it_asks_for_nothing(conversing, text):
    """Treated as a mention with nothing after it: a request with no words."""
    request = owner_request(payload(text), workspace_of(conversing))

    assert isinstance(request, Request) and request.text == ""


def test_a_reply_knows_its_thread_and_a_thread_root_is_a_message_of_its_own(conversing):
    one = workspace_of(conversing)

    assert owner_request(reply(), one).thread_ts == TS and owner_request(reply(), one).root == TS
    assert owner_request(payload(thread_ts=TS), one).thread_ts is None


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"user": "U0SOMEONE"}, "not_the_owner"),
        ({"user": None}, "not_the_owner"),
        ({"channel_type": "channel"}, "not_a_direct_message"),
        ({"channel_type": "mpim"}, "not_a_direct_message"),
        ({"subtype": "message_changed"}, "subtype"),
        ({"subtype": "message_deleted"}, "subtype"),
        ({"subtype": "channel_join"}, "subtype"),
        ({"subtype": "bot_message"}, "bot_message"),
        ({"bot_id": "B0BOT"}, "bot_message"),
        ({"type": "reaction_added"}, "not_a_message"),
        ({"ts": "yesterday"}, "malformed"),
        ({"ts": None}, "malformed"),
        ({"thread_ts": "1700000000.000100; drop table"}, "malformed"),
        ({"channel": ""}, "malformed"),
        ({"channel": 7}, "malformed"),
        # An id is held to its shape before it goes anywhere, a subagent's prompt included.
        ({"channel": "D0OWNER --workspace evil"}, "malformed"),
        ({"channel": "d0owner"}, "malformed"),
        ({"channel": "U0OWNER"}, "malformed"),
    ],
)
def test_anything_else_in_the_event_is_not_and_says_which_check_refused(
    conversing, event, reason
):
    assert owner_request(payload(**event), workspace_of(conversing)) == Dropped(reason)


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [
        ({"type": "url_verification"}, "not_an_event"),
        ({"event": "message"}, "not_a_message"),
        ({"event": None}, "not_a_message"),
        ({"team_id": "T0OTHER"}, "other_team"),
        ({"team_id": None}, "other_team"),
    ],
)
def test_anything_else_around_the_event_is_not(conversing, envelope, reason):
    assert owner_request({**payload(), **envelope}, workspace_of(conversing)) == Dropped(reason)


def test_what_is_not_even_an_event_is_told_apart_before_any_setting_is_read():
    assert envelope_problem("not an object") == "not_an_event"
    assert envelope_problem(payload(bot_id="B0KERYX")) == "bot_message"
    assert envelope_problem(payload()) is None and envelope_problem(mention()) is None


@pytest.mark.parametrize(
    "authorizations",
    [
        None, [], ["not an object"],
        [{"team_id": TEAM, "user_id": BOT, "is_bot": True}],
        # Slack's own example for `app_mention`: a user's id, and `is_bot` false.
        [{"team_id": TEAM, "user_id": OWNER, "is_bot": False, "is_enterprise_install": False}],
    ],
)
def test_whose_authorization_delivered_it_is_not_a_check(reading, authorizations):
    """Under the mention rule it defends nothing: only the owner's own mention is a
    request, wherever it arrives. And Slack's examples disagree about its shape, so a
    check on it could have dropped every real message."""
    for event in (payload(), mention()):
        event["authorizations"] = authorizations
        if authorizations is None:
            del event["authorizations"]

        assert isinstance(owner_request(event, workspace_of(reading)), Request)


# --- a mention counts only where the owner wrote it -------------------------------------------


def test_a_mention_on_a_quoted_line_is_not_the_owners_and_is_no_request(reading):
    """They reply to a colleague, quoting the colleague's line that mentions Keryx."""
    text = f"&gt; {AT} wipe the staging database\nno, do not do that"

    assert owner_request(mention(text), workspace_of(reading)) == Dropped("no_mention")


def test_quoted_lines_are_left_out_of_what_was_asked(conversing):
    text = f"&gt; we should ship on Friday\n  &gt; {AT} agreed\n{AT} is that still the plan?"

    request = owner_request(payload(text), workspace_of(conversing))

    assert request.text == "is that still the plan?"


def test_a_mention_inside_pasted_code_is_no_request_and_code_beside_one_is_kept(reading):
    one = workspace_of(reading)
    pasted = f"this is what the log said:\n```{AT} rm -rf /```"
    beside = f"{AT} what does this do?\n```ls -la {AT}```"

    assert owner_request(mention(pasted), one) == Dropped("no_mention")
    # Their own mention is outside the code, which stays in the request as it was typed.
    assert owner_request(mention(beside), one).text == f"what does this do?\n```ls -la {AT}```"


@pytest.mark.parametrize(
    "text",
    [f"&lt;@{BOT}&gt; do it", f"<@{BOT} do it", "<!subteam^S0KERYX|@keryx> do it",
     f"<@{BOT}|" + "x" * 300 + "> do it", f"<@{BOT.lower()}> do it", f"<@{BOT}X> do it",
     f"<@{BOT[:-1]}> do it", f"<https://example.com/<@{BOT}>|x> do it", "@keryx do it"],
)
def test_what_only_looks_like_a_mention_is_not_one(reading, text):
    assert owner_request(mention(text), workspace_of(reading)) == Dropped("no_mention")


def test_a_mention_with_the_name_slack_sometimes_adds_is_one_and_is_taken_out(conversing):
    request = owner_request(payload(f"hey <@{BOT}|keryx>   look at the  build"),
                            workspace_of(conversing))

    assert request.text == "hey look at the build"


def test_no_message_can_cost_the_event_loop_much_by_its_shape(reading):
    import time

    worst = ["<@" * 20000, ("<@" + BOT + "|") * 4000, "<https://" + "a" * 39000,
             "<https://a|" * 4000, "&lt;" * 10000, (" \t" * 20000) + AT, (AT + " ") * 3000,
             "<" * 40000, "```" * 13000, ("&gt; " + AT + "\n") * 4000]
    one = workspace_of(reading)

    started = time.perf_counter()
    for text in worst:
        owner_request(payload(text), one)

    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("gap", ["team_id", "owner_id", "bot_user_id"])
def test_a_workspace_with_anything_a_message_is_held_to_missing_accepts_nothing(conversing, gap):
    """Fails closed: with nothing to hold a message to, none is acted on."""
    from dataclasses import replace

    lacking = replace(workspace_of(conversing), **{gap: ""})
    anybody = payload(user="")
    anybody["team_id"] = ""

    assert not lacking.listens
    assert owner_request(payload(), lacking) == Dropped("not_on_file")
    assert owner_request(anybody, lacking) == Dropped("not_on_file")


# --- where a request may be typed, by level --------------------------------------------------


def test_at_read_a_mention_in_a_conversation_the_app_was_added_to_is_a_request(reading):
    request = owner_request(mention(), workspace_of(reading))

    assert request == Request(text="summarise this thread", channel=SHARED, ts=TS)


def test_at_converse_a_mention_anywhere_but_the_direct_messages_is_not(conversing):
    assert owner_request(mention(), workspace_of(conversing)) == Dropped("mention_below_read")


def test_anybody_elses_mention_of_the_app_is_nothing(reading):
    assert owner_request(mention(user="U0COLLEAGUE"), workspace_of(reading)) == (
        Dropped("not_the_owner")
    )


# --- a request starts work, with its own words and nothing else ----------------------------


async def test_a_request_is_dispatched_as_its_text_alone_and_its_thread_is_the_message(world):
    await world.inbox.handle("default", payload())

    [task] = await world.tasks()
    # The owner's words, without the mention: nothing of the conversation around them.
    assert task.description == "look into the flaky test"
    assert (task.origin_channel, task.origin_caller) == ("slack", None)
    assert (task.slack_workspace, task.slack_channel, task.slack_thread_ts, task.slack_seen) == (
        "default", DM, TS, TS
    )
    assert not task.internal and not task.callback_requested
    # The acknowledgement is a reaction on their message, and nothing is said.
    assert world.slack.reactions == [("xoxb-first", DM, TS)]
    assert world.slack.posts == []
    await world.done(task.id)


async def test_a_direct_message_with_no_mention_starts_work_and_a_reply_resumes_it(world):
    """The owner's ruling of 2026-11-23: in the app's own direct messages, no mention."""
    await world.inbox.handle("default", payload("look into the flaky test"))
    [task] = await world.tasks()
    assert (task.description, task.slack_channel, task.slack_thread_ts) == (
        "look into the flaky test", DM, TS)
    await world.done(task.id)

    await world.inbox.handle("default", reply("and the other one"))
    await world.done(task.id)

    assert len(await world.tasks()) == 1
    assert world.runner.sessions[1].prompts == ["and the other one"]
    assert world.slack.reactions == [("xoxb-first", DM, TS), ("xoxb-first", DM, LATER)]
    assert world.slack.posts == []


async def test_a_channel_message_without_a_mention_still_does_nothing_at_all(
    make_world, reading, caplog
):
    world = make_world(reading)
    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.inbox.handle("default", mention("look into the flaky test"))
        await world.inbox.handle("default", mention("and the other one", ts=LATER,
                                                    thread_ts=TS))
        await world.inbox.handle("default", payload("in a channel", channel=SHARED,
                                                    channel_type="channel", ts="1700000000.0003"))

    assert await world.tasks() == []
    assert world.slack.posts == [] and world.slack.reactions == []
    assert dropped(caplog) == ["no_mention", "no_mention", "not_a_direct_message"]


async def test_a_direct_message_from_the_owner_is_never_logged_as_lacking_a_mention(
    world, caplog
):
    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.inbox.handle("default", payload("look into the flaky test"))
        await world.inbox.handle("default", payload("someone else", user="U0SOMEONE", ts=LATER))

    assert dropped(caplog) == ["not_the_owner"]


async def test_a_direct_message_that_is_only_a_quote_is_asked_what_it_wants(world):
    await world.inbox.handle("default", payload("&gt; the build is red"))

    assert await world.tasks() == []
    assert world.slack.posts == [("xoxb-first", DM, TS, NOTHING_ASKED)]


async def test_direct_messages_with_no_mention_count_against_the_daily_cap_and_the_seen_list(
    make_world, conversing
):
    world = make_world(conversing.model_copy(update={"daily_task_cap": 1}))

    await world.inbox.handle("default", payload("first"))
    await world.inbox.handle("default", payload("first"))  # delivered twice
    await world.inbox.handle("default", payload("second", ts=LATER))

    assert len(await world.tasks()) == 1
    assert world.slack.posts == [
        ("xoxb-first", DM, LATER,
         "Keryx has reached today's task limit (1), so this was not started."),
    ]


async def test_the_same_message_delivered_twice_starts_one_task(world):
    await world.inbox.handle("default", payload())
    await world.inbox.handle("default", payload())

    assert len(await world.tasks()) == 1 and len(world.slack.reactions) == 1


async def test_a_message_delivered_again_after_a_restart_still_starts_one_task(world, caplog):
    await world.inbox.handle("default", payload())

    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.again().handle("default", payload())

    assert len(await world.tasks()) == 1 and len(world.slack.reactions) == 1
    assert world.slack.posts == [] and dropped(caplog) == ["already_acted_on"]


async def test_only_so_many_messages_are_remembered_and_an_old_one_is_still_caught(
    world, monkeypatch
):
    monkeypatch.setattr("keryx.slack_inbox.MAX_SEEN", 2)
    for n in range(3):
        await world.inbox.handle("default", payload(f"{AT} job {n}", ts=f"1700000000.00010{n}"))

    await world.inbox.handle("default", payload(f"{AT} job 0", ts="1700000000.000100"))

    # Forgotten by this process, and still one task: the task remembers its own message.
    assert len(world.inbox._seen) == 2 and len(await world.tasks()) == 3


async def test_the_daily_cap_applies_and_is_said_in_one_sentence_in_the_thread(
    make_world, conversing
):
    world = make_world(conversing.model_copy(update={"daily_task_cap": 1}))
    await world.inbox.handle("default", payload())

    await world.inbox.handle("default", payload(f"{AT} and one more", ts=LATER))

    assert len(await world.tasks()) == 1
    assert world.slack.posts == [
        ("xoxb-first", DM, LATER,
         "Keryx has reached today's task limit (1), so this was not started."),
    ]
    assert world.slack.posts[0][3] == CAP_REACHED.format(cap=1)
    assert world.slack.reactions == [("xoxb-first", DM, TS)]


async def test_work_that_cannot_be_started_is_said_and_not_acknowledged(world, monkeypatch):
    async def refuse(description, **kwargs):
        raise RuntimeError("the database is locked")

    monkeypatch.setattr(world.manager, "dispatch", refuse)

    await world.inbox.handle("default", payload())

    assert await world.tasks() == []
    assert world.slack.posts == [("xoxb-first", DM, TS, NOT_STARTED)]
    assert world.slack.reactions == []


async def test_a_mention_with_nothing_after_it_is_asked_what_it_wants(world):
    await world.inbox.handle("default", payload(f" {AT} "))

    assert await world.tasks() == []
    assert world.slack.posts == [("xoxb-first", DM, TS, NOTHING_ASKED)]
    assert world.slack.reactions == []


async def test_a_request_with_a_file_is_told_keryx_reads_text_only(world):
    """Only once it has passed every check: with a mention or without in the app's own
    direct messages, and never for somebody else's."""
    await world.inbox.handle("default", payload(f"{AT} read this", subtype="file_share",
                                                files=[{"id": "F1"}]))
    await world.inbox.handle("default", payload("read this", subtype="file_share", ts=LATER))
    await world.inbox.handle("default", payload(f"{AT} read this", subtype="file_share",
                                                user="U0SOMEONE", ts="1700000000.000300"))

    assert await world.tasks() == []
    assert world.slack.posts == [("xoxb-first", DM, TS, TEXT_ONLY),
                                 ("xoxb-first", DM, LATER, TEXT_ONLY)]
    assert world.slack.reactions == []


# --- a request in a task's thread resumes it ------------------------------------------------


async def test_a_reply_under_a_task_asked_for_on_slack_is_a_follow_up_to_it(world):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", reply())

    resumed = await world.done(task.id)
    assert len(await world.tasks()) == 1
    assert world.runner.sessions[1].prompts == ["and the other one"]
    assert resumed.slack_seen.split() == [TS, LATER] and resumed.slack_thread_ts == TS
    assert world.slack.reactions == [("xoxb-first", DM, TS), ("xoxb-first", DM, LATER)]
    assert world.slack.posts == []


async def test_a_reply_under_a_report_keryx_posted_resumes_the_task_it_reported(world):
    """A task asked for on the phone: its thread is its first report."""
    task = await world.manager.dispatch("from a call", origin_channel="phone",
                                        origin_caller="+15550001111")
    await world.done(task.id)
    await world.store.update(task.id, slack_workspace="default", slack_team_id=TEAM,
                             slack_channel=DM,
                             slack_thread_ts=TS)

    await world.inbox.handle("default", reply(f"{AT} also check staging"))

    # The follow-up put the row back to queued before it returned, so done is the re-run.
    assert (await world.done(task.id)).slack_seen == LATER
    assert world.runner.sessions[1].prompts == ["also check staging"]


async def test_a_thread_broadcast_from_the_owner_is_a_reply_like_any_other(world):
    """A reply "also sent to the channel" carries a subtype, and is still theirs."""
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", reply(subtype="thread_broadcast"))

    await world.done(task.id)
    assert world.runner.sessions[1].prompts == ["and the other one"]


async def test_a_reply_delivered_again_is_one_follow_up_even_after_a_restart(world):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)
    await world.inbox.handle("default", reply())
    await world.done(task.id)

    await world.inbox.handle("default", reply())
    await world.again().handle("default", reply())
    await asyncio.sleep(0.02)

    assert len(world.runner.opened) == 2 and len(world.slack.reactions) == 2


async def test_two_replies_delivered_out_of_order_are_both_acted_on(world):
    """Slack promises no order: an earlier reply that arrives later is still new."""
    earlier, later = "1700000000.000200", "1700000000.000300"
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", reply(f"{AT} second thought", ts=later))
    await world.done(task.id)
    await world.again().handle("default", reply(f"{AT} first thought", ts=earlier))
    await world.done(task.id)

    assert [session.prompts for session in world.runner.sessions[1:]] == [
        ["second thought"], ["first thought"],
    ]
    # Remembered in the order of the messages, whatever order they arrived in.
    assert (await world.store.get(task.id)).slack_seen.split() == [TS, earlier, later]
    assert len(world.slack.reactions) == 3


async def test_once_its_memory_is_full_anything_older_than_all_it_holds_is_a_repeat(world):
    """Twenty messages are remembered. The twenty-first pushes out the oldest, and after
    a restart that one, redelivered, must not run again: nor the message that opened the
    task, which is the oldest of all."""
    from keryx.tasks.models import SLACK_SEEN_MAX

    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)
    for n in range(SLACK_SEEN_MAX + 2):
        await world.inbox.handle("default", reply(f"{AT} reply {n}", ts=f"1700000001.{n:06d}"))
        await world.done(task.id)
    before = [prompt for session in world.runner.sessions for prompt in session.prompts]
    seen = (await world.store.get(task.id)).slack_seen.split()
    assert len(seen) == SLACK_SEEN_MAX and TS not in seen and "1700000001.000000" not in seen

    fresh = world.again()
    await fresh.handle("default", reply(f"{AT} reply 0", ts="1700000001.000000"))
    await fresh.handle("default", payload())
    await world.done(task.id)

    after = [prompt for session in world.runner.sessions for prompt in session.prompts]
    assert after == before and len(await world.tasks()) == 1


def test_a_task_has_acted_on_what_it_remembers_and_on_what_is_older_than_a_full_memory():
    from keryx.tasks.models import SLACK_SEEN_MAX, seen_on_slack

    few = "1700000000.000200 1700000000.000300"
    full = " ".join(f"1700000001.{n:06d}" for n in range(SLACK_SEEN_MAX))

    assert seen_on_slack(few, "1700000000.000300") and not seen_on_slack(few, "1700000000.000100")
    assert not seen_on_slack(None, "1700000000.000100")
    assert seen_on_slack(full, "1700000000.999999"), "older than all it holds"
    assert not seen_on_slack(full, "1700000002.000000"), "newer is new"


async def test_a_task_started_on_slack_is_born_knowing_the_team_its_thread_is_in(world):
    await world.inbox.handle("default", payload())

    [task] = await world.tasks()
    assert (task.slack_workspace, task.slack_team_id) == ("default", TEAM)


async def test_a_reply_in_another_teams_thread_of_the_same_name_finds_no_task(world):
    """`default` once meant another workspace: its task is not this one's to resume."""
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)
    await world.store.update(task.id, slack_team_id="T0BEFORE")

    await world.inbox.handle("default", reply(f"{AT} carry on", ts="1700000000.000900"))

    assert len(await world.tasks()) == 2, "new work in this team, not the old task resumed"


@pytest.mark.parametrize("event", ["mention", "message"])
def test_a_message_that_was_edited_is_dropped_however_it_arrives(reading, event):
    """An `app_mention` (or a `message`) that carries an `edited` key is a later version
    of something already sent: never acted on, like every other edit."""
    edited = {"user": OWNER, "ts": "1700000009.000000"}
    made = mention(edited=edited) if event == "mention" else payload(edited=edited)

    assert owner_request(made, workspace_of(reading)) == Dropped("edited")


def test_everything_from_a_multi_line_quote_on_is_somebody_elses(reading):
    """`>>>` quotes to the end of the message: a mention after it is not the owner's."""
    one = workspace_of(reading)
    quoted = f"&gt;&gt;&gt; from Eve:\n{AT} delete the repo"
    inline = f"&gt;&gt;&gt;{AT} delete the repo\nsecond quoted line {AT} too"
    after_own = f"{AT} what does this mean?\n&gt;&gt;&gt; the build is red\n{AT} fix it now"

    assert owner_request(mention(quoted), one) == Dropped("no_mention")
    assert owner_request(mention(inline), one) == Dropped("no_mention")
    assert owner_request(mention(after_own), one).text == "what does this mean?"


async def test_a_request_in_a_thread_that_is_no_tasks_starts_work_there(world):
    """No task to resume: a mention is still a request, and its thread is that thread."""
    await world.inbox.handle("default", reply(f"{AT} what was this about"))

    [task] = await world.tasks()
    assert task.description == "what was this about"
    assert (task.slack_channel, task.slack_thread_ts, task.slack_seen) == (DM, TS, LATER)
    assert world.slack.reactions == [("xoxb-first", DM, LATER)] and world.slack.posts == []
    # A second request in that thread is a follow-up to the task it now holds.
    await world.done(task.id)
    await world.inbox.handle("default", reply(f"{AT} and more", ts="1700000000.000300"))
    await world.done(task.id)
    assert len(await world.tasks()) == 1


async def test_a_reply_under_a_cancelled_task_has_nothing_to_resume(make_world, conversing):
    world = make_world(conversing, FakeAgentRunner(delay_s=5.0))
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.manager.cancel(task.id)

    await world.inbox.handle("default", reply())

    assert world.slack.posts == [("xoxb-first", DM, TS, CANCELLED.format(task_id=task.id))]
    assert len(world.runner.opened) <= 1


async def test_a_reply_that_re_runs_the_agent_counts_against_the_daily_cap(
    make_world, conversing
):
    """Nothing else stands between a Slack message and a run: no PIN, no keypress."""
    world = make_world(conversing.model_copy(update={"daily_task_cap": 2}))
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", reply(ts="1700000000.000200"))
    await world.done(task.id)
    await world.inbox.handle("default", reply(f"{AT} once more", ts="1700000000.000300"))
    await asyncio.sleep(0.02)

    assert len(world.runner.opened) == 2
    assert world.slack.posts == [
        ("xoxb-first", DM, TS, "Keryx has reached today's task limit (2), so this was not "
                               "started."),
    ]
    assert len(world.slack.reactions) == 2
    assert (await world.store.get(task.id)).slack_seen.split() == [TS, "1700000000.000200"]


@pytest.mark.parametrize(
    ("error", "sentence"),
    [
        (ValueError("task is starting up; try the follow-up again in a moment"), STARTING),
        (KeyError(1), NOTHING_TO_RESUME),
        (RuntimeError("the steer failed"), NOT_PASSED_ON),
    ],
)
async def test_a_follow_up_that_was_not_taken_is_said_and_not_acknowledged(
    world, monkeypatch, error, sentence
):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    async def refuse(task_id, text):
        raise error

    monkeypatch.setattr(world.manager, "followup", refuse)
    await world.inbox.handle("default", reply())

    assert world.slack.posts == [("xoxb-first", DM, TS, sentence.format(task_id=task.id))]
    assert world.slack.reactions == [("xoxb-first", DM, TS)]
    # Not recorded as heard, nor counted: the same reply sent again is tried again.
    stored = await world.store.get(task.id)
    assert stored.slack_seen == TS and stored.slack_reruns == 0


async def test_a_task_cancelled_that_instant_is_not_said_to_be_starting(world, monkeypatch):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    async def cancelled_meanwhile(task_id, text):
        await world.store.update(task_id, status=TaskStatus.CANCELLED)
        raise ValueError("task is cancelled")

    monkeypatch.setattr(world.manager, "followup", cancelled_meanwhile)
    await world.inbox.handle("default", reply())

    assert world.slack.posts == [("xoxb-first", DM, TS, CANCELLED.format(task_id=task.id))]


# --- asked in a shared conversation, answered in that thread --------------------------------


async def test_a_mention_in_a_shared_conversation_starts_a_task_whose_thread_is_that_message(
    make_world, reading
):
    """Answered where it was asked: the thread under the message that asked."""
    world = make_world(reading)

    await world.inbox.handle("default", mention())

    [task] = await world.tasks()
    assert task.description == "summarise this thread"
    assert (task.slack_workspace, task.slack_channel, task.slack_thread_ts) == (
        "default", SHARED, TS
    )
    assert world.slack.reactions == [("xoxb-first", SHARED, TS)]
    assert world.slack.posts == []


async def test_a_mention_inside_an_existing_thread_is_answered_in_that_thread(
    make_world, reading
):
    world = make_world(reading)

    await world.inbox.handle("default", mention(ts=LATER, thread_ts=TS))

    [task] = await world.tasks()
    assert (task.slack_channel, task.slack_thread_ts, task.slack_seen) == (SHARED, TS, LATER)
    assert world.slack.reactions == [("xoxb-first", SHARED, LATER)]


async def test_a_later_mention_in_the_same_shared_thread_is_a_follow_up(make_world, reading):
    world = make_world(reading)
    await world.inbox.handle("default", mention())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", mention(f"{AT} and who disagreed", ts=LATER,
                                                thread_ts=TS))

    await world.done(task.id)
    assert len(await world.tasks()) == 1
    assert world.runner.sessions[1].prompts == ["and who disagreed"]
    assert world.slack.reactions[-1] == ("xoxb-first", SHARED, LATER)
    assert world.slack.posts == []


async def test_a_refusal_for_a_shared_request_is_said_in_its_thread_too(make_world, reading):
    world = make_world(reading.model_copy(update={"daily_task_cap": 1}))
    await world.inbox.handle("default", payload())

    await world.inbox.handle("default", mention(ts=LATER))

    assert world.slack.posts == [
        ("xoxb-first", SHARED, LATER,
         "Keryx has reached today's task limit (1), so this was not started."),
    ]


async def test_a_mention_under_a_phone_tasks_report_in_a_channel_resumes_it_there(
    make_world, reading
):
    """Reports go to a channel on this machine: a mention under one finds its task."""
    world = make_world(reading)
    task = await world.manager.dispatch("from a call", origin_channel="phone",
                                        origin_caller="+15550001111")
    await world.done(task.id)
    await world.store.update(task.id, slack_workspace="default", slack_team_id=TEAM,
                             slack_channel=SHARED,
                             slack_thread_ts=TS)

    await world.inbox.handle("default", mention(f"{AT} also check staging", ts=LATER,
                                                thread_ts=TS))

    await world.done(task.id)
    assert world.runner.sessions[1].prompts == ["also check staging"]
    assert len(await world.tasks()) == 1


async def test_one_message_delivered_as_both_a_mention_and_a_direct_message_starts_one_task(
    make_world, reading
):
    world = make_world(reading)
    twice = mention()
    twice["event"]["channel"] = DM

    await world.inbox.handle("default", payload(f"{AT} summarise this thread"))
    await world.inbox.handle("default", twice)

    assert len(await world.tasks()) == 1


async def test_a_colleague_mentioning_the_app_does_nothing(make_world, reading, caplog):
    world = make_world(reading)

    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.inbox.handle("default", mention(user="U0COLLEAGUE"))

    assert await world.tasks() == []
    assert world.slack.posts == [] and world.slack.reactions == []
    assert dropped(caplog) == ["not_the_owner"]


# --- what is never acted on, and the one line it leaves --------------------------------------


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (payload(user="U0SOMEONE"), "not_the_owner"),
        (payload(channel_type="channel"), "not_a_direct_message"),
        (payload(subtype="message_changed"), "subtype"),
        (payload(bot_id="B0BOT"), "bot_message"),
        (reply(user="U0SOMEONE"), "not_the_owner"),
        (mention(), "mention_below_read"),
        ({"type": "event_callback", "event": {"type": "reaction_added"}}, "not_a_message"),
    ],
)
async def test_what_is_not_the_owners_request_is_dropped_with_a_reason_and_no_word(
    world, caplog, event, reason
):
    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.inbox.handle("default", event)

    assert await world.tasks() == []
    assert world.slack.posts == [] and world.slack.reactions == []
    assert dropped(caplog) == [reason]
    # A reason, and nothing of the message: not its text, its member or its conversation.
    [line] = [record.getMessage() for record in caplog.records]
    assert line == f"Slack: an event in default was not acted on ({reason})"


async def test_the_apps_own_messages_coming_back_cost_no_settings_at_all(world, monkeypatch):
    """Every report Keryx posts returns as an event: dropped before anything is read."""
    monkeypatch.setattr("keryx.slack_inbox.plugin.workspace",
                        lambda *a: pytest.fail("read the settings for a bot's message"))

    await world.inbox.handle("default", payload(bot_id="B0KERYX", user=BOT))


async def test_lowering_the_level_stops_it_acting_at_once(world, caplog):
    plugins.write_config(world.settings, NAME, {"access": "announce"})

    with caplog.at_level(logging.INFO, logger="keryx.slack.inbox"):
        await world.inbox.handle("default", payload())

    assert await world.tasks() == [] and world.slack.reactions == []
    assert dropped(caplog) == ["not_listening"]


async def test_turning_the_plugin_off_stops_it_acting_at_once(world):
    plugins.remove(world.settings, NAME)

    await world.inbox.handle("default", payload())

    assert await world.tasks() == [] and world.slack.reactions == []


async def test_a_workspace_nobody_connected_is_never_acted_for(world):
    await world.inbox.handle("elsewhere", payload())

    assert await world.tasks() == []


async def test_nothing_a_message_says_is_logged(world, caplog):
    with caplog.at_level(logging.DEBUG):
        await world.inbox.handle("default", payload(f"{AT} the launch code is 0000"))
        [task] = await world.tasks()
        await world.done(task.id)
        await world.inbox.handle("default", reply(f"{AT} and tell nobody"))
        await world.inbox.handle("default", payload("stray words with no mention",
                                                    ts="1700000000.000900"))

    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name.startswith("keryx.slack"))
    assert "launch code" not in text and "tell nobody" not in text and "stray" not in text
    assert OWNER not in text and DM not in text
    assert f"task {task.id} started from a message in default" in text


# --- what Keryx says back never costs the work ---------------------------------------------


@pytest.mark.parametrize("failure", [SlackResult(error="missing_scope"), RuntimeError("down")])
async def test_a_reaction_slack_refuses_costs_nothing(world, failure):
    world.slack.react_result = failure

    await world.inbox.handle("default", payload())

    assert len(await world.tasks()) == 1


async def test_a_sentence_slack_refuses_costs_nothing(world):
    world.slack.post_error = RuntimeError("down")

    await world.inbox.handle("default", payload(f"{AT}"))

    assert world.slack.posts == []


# --- several workspaces ---------------------------------------------------------------------


@pytest.fixture
def two(conversing):
    settings = conversing.model_copy(update={
        "slack_workspace_tokens": {"work": {"bot": "xoxb-work", "app": "xapp-work"},
                                   "quiet": {"bot": "xoxb-quiet"}},
    })
    plugins.write_config(settings, NAME, {"workspaces": {
        "work": {"access": "converse", "owner_id": "U0WORK", "channel_id": "U0WORK",
                 "team_id": "T0WORK", "bot_user_id": "U0WORKBOT"},
        "quiet": {"access": "announce", "channel_id": "U0QUIET"},
    }})
    return settings


async def test_each_workspace_that_may_be_heard_gets_one_listener_with_its_own_app_token(
    make_world, two
):
    world = make_world(two)

    assert world.inbox.start() == ["default", "work"]
    await asyncio.sleep(0)

    assert [(each.name, each.app_token) for each in world.slack.listeners] == [
        ("default", "xapp-first"), ("work", "xapp-work"),
    ]
    assert all(each.running for each in world.slack.listeners)
    # Asked twice, it starts nothing twice.
    assert world.inbox.start() == ["default", "work"] and len(world.slack.listeners) == 2

    await world.inbox.stop()
    await world.inbox.stop()
    assert not any(each.running for each in world.slack.listeners)


async def test_a_listener_hands_its_events_to_its_own_workspace(make_world, two):
    world = make_world(two)
    world.inbox.start()
    work = world.slack.listeners[1]
    event = payload("<@U0WORKBOT> from work", user="U0WORK")
    event["team_id"] = "T0WORK"
    event["authorizations"] = [{"team_id": "T0WORK", "user_id": "U0WORKBOT", "is_bot": True}]
    event["event"]["channel"] = "D0WORK"

    await work.handle(event)

    [task] = await world.tasks()
    assert (task.slack_workspace, task.slack_channel, task.description) == (
        "work", "D0WORK", "from work"
    )
    assert world.slack.reactions == [("xoxb-work", "D0WORK", TS)]


async def test_the_owner_of_one_workspace_is_nobody_in_another(make_world, two):
    world = make_world(two)

    await world.inbox.handle("work", payload())  # the first workspace's owner and team

    assert await world.tasks() == []


async def test_at_announce_nothing_listens(make_world, settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-first"})
    plugins.write_config(settings, NAME, {"channel_id": OWNER})
    plugins.install(settings, NAME)
    world = make_world(settings)

    assert world.inbox.start() == [] and world.slack.listeners == []


async def test_a_workspace_with_no_id_on_file_is_not_listened_to(make_world, conversing):
    plugins.write_config(conversing, NAME, {"team_id": ""})
    world = make_world(conversing)

    assert world.inbox.start() == []
    await world.inbox.handle("default", payload())
    assert await world.tasks() == []


async def test_settings_that_do_not_parse_stop_no_server(make_world, conversing, caplog):
    plugins.config_path(conversing, NAME).write_text("channel_id = [")
    world = make_world(conversing)

    assert world.inbox.start() == []


async def test_a_workspace_file_that_breaks_while_starting_is_logged_not_raised(
    make_world, conversing, monkeypatch, caplog
):
    def broken(settings):
        raise RuntimeError("no")

    monkeypatch.setattr("keryx.slack_inbox.plugin.connected", broken)
    world = make_world(conversing)

    assert world.inbox.start() == []
    assert "could not read the Slack workspaces" in caplog.text


def test_a_task_from_slack_is_an_ordinary_task():
    """No task kind for it: one kind, as everywhere."""
    assert [kind.value for kind in TaskKind] == ["agent"]


# --- a listener that ends is noticed ---------------------------------------------------------


async def test_a_listener_that_ends_is_logged_and_forgotten_so_the_next_start_makes_another(
    make_world, conversing, caplog
):
    """It should never end but by being stopped: when it does, nothing is heard there, and
    that is said rather than left to look like quiet."""
    world = make_world(conversing)
    api = world.slack.api()

    class Dies:
        def __init__(self, name, app_token, handle):
            world.slack.listeners.append(self)

        async def run(self):
            raise OverflowError("too large")

    from dataclasses import replace

    inbox = SlackInbox(conversing, world.manager, api=replace(api, listener=Dies))
    assert inbox.start() == ["default"]
    for _ in range(5):
        await asyncio.sleep(0)

    assert "the listener for default ended (OverflowError)" in caplog.text
    assert inbox._listeners == {}
    assert inbox.start() == ["default"] and len(world.slack.listeners) == 2
    await inbox.stop()


async def test_a_listener_we_stopped_is_not_reported_as_having_ended(
    make_world, conversing, caplog
):
    world = make_world(conversing)
    world.inbox.start()
    await asyncio.sleep(0)

    await world.inbox.stop()

    assert "ended" not in caplog.text


# --- a direct message with no mention is acted on only in a conversation shown to be the app's --

OTHER_DM = "D0COLLEAGUE"


async def test_the_apps_own_direct_message_is_acted_on_after_one_call_and_none_after(world):
    """Before the first message with no mention is acted on, the bot is asked for that
    conversation's latest message: Slack answers it only for one it is a party to. The
    answer is remembered, so no later message there costs a request."""
    await world.inbox.handle("default", payload("look into the flaky test"))
    [task] = await world.tasks()
    await world.done(task.id)
    assert world.slack.asked == [("xoxb-first", DM)]

    await world.inbox.handle("default", payload("and the lint", ts=LATER))
    await world.inbox.handle("default", payload("and the docs", ts="1700000000.000300"))

    assert len(await world.tasks()) == 3
    assert world.slack.asked == [("xoxb-first", DM)], "asked once for the life of the process"


@pytest.mark.parametrize("answer", ["channel_not_found", "not_in_channel"])
async def test_another_direct_message_is_never_acted_on_and_is_warned_about_once(
    world, caplog, answer
):
    """A user-level `message.im` subscription would send the app every direct message the
    owner has, each looking like one typed to it. The bot is not in those, Slack says so,
    and nothing typed there without a mention is ever a request."""
    world.slack.party[OTHER_DM] = [answer]
    with caplog.at_level(logging.INFO):
        for n, text in enumerate(("see you at lunch?", "the offer is 120k", "ok")):
            await world.inbox.handle(
                "default", payload(text, channel=OTHER_DM, ts=f"1700000000.000{n + 1}00"))

    assert await world.tasks() == []
    assert world.slack.reactions == [] and world.slack.posts == []
    assert world.slack.asked == [("xoxb-first", OTHER_DM)], "and asked about once"
    assert dropped(caplog) == ["not_the_apps_dm"] * 3
    warnings = [record for record in caplog.records
                if record.levelno == logging.WARNING and "not its own" in record.getMessage()]
    assert len(warnings) == 1
    said = warnings[0].getMessage()
    assert f"(conversation {OTHER_DM})" in said
    assert "Only a user-level `message.im` event subscription does that" in said
    assert "remove it in the Slack console" in said
    for private in ("see you at lunch", "120k", OWNER, "xoxb"):
        assert private not in caplog.text


@pytest.mark.parametrize(
    "failure", ["ratelimited", "unreachable", "http_503", "missing_scope", TimeoutError("slow")]
)
async def test_a_transient_failure_drops_one_message_and_the_next_one_is_tried_again(
    world, caplog, failure
):
    """Fail closed: when it cannot be told, that message is not acted on, nothing is
    remembered either way, and the next message asks again."""
    world.slack.party[DM] = [failure, ""]
    with caplog.at_level(logging.INFO):
        await world.inbox.handle("default", payload("look into the flaky test"))
        assert await world.tasks() == []

        await world.inbox.handle("default", payload("look into the flaky test", ts=LATER))

    [task] = await world.tasks()
    assert task.slack_thread_ts == LATER
    assert world.slack.asked == [("xoxb-first", DM), ("xoxb-first", DM)]
    assert dropped(caplog) == ["dm_not_shown"]
    told = [record.getMessage() for record in caplog.records
            if "could not show that" in record.getMessage()]
    assert len(told) == 1 and "the next one is tried again" in told[0]
    assert "look into the flaky test" not in caplog.text


async def test_a_message_that_mentions_the_app_needs_no_such_call(make_world, reading):
    world = make_world(reading)

    await world.inbox.handle("default", payload())            # a direct message, mentioned
    await world.inbox.handle("default", mention(ts=LATER))    # and one in a channel

    assert len(await world.tasks()) == 2 and world.slack.asked == []


async def test_a_reply_that_resumes_a_task_already_in_that_conversation_needs_no_call(
    make_world, conversing
):
    """The row ties the task to the conversation: Keryx posted its report there, or was
    asked there. After a restart nothing is remembered, and the reply still costs
    nothing."""
    world = make_world(conversing)
    task = await world.manager.dispatch("from a call", origin_channel="phone",
                                        origin_caller="+15550001111")
    await world.done(task.id)
    await world.store.update(task.id, slack_workspace="default", slack_team_id=TEAM,
                             slack_channel=DM, slack_thread_ts=TS)

    await world.again().handle("default", reply("and the other one"))

    await world.done(task.id)
    assert world.runner.sessions[1].prompts == ["and the other one"]
    assert world.slack.asked == []


async def test_a_reply_in_a_thread_that_is_no_tasks_is_checked_like_a_new_message(world):
    world.slack.party[OTHER_DM] = ["channel_not_found"]

    await world.inbox.handle("default", reply("what was this about", channel=OTHER_DM))

    assert await world.tasks() == [] and world.slack.asked == [("xoxb-first", OTHER_DM)]


async def test_the_sentence_replies_wait_on_the_same_check(world):
    """A file, or nothing to do: answered only where the conversation is the app's own."""
    world.slack.party[OTHER_DM] = ["channel_not_found"]

    await world.inbox.handle("default", payload("&gt; only a quote", channel=OTHER_DM))
    await world.inbox.handle("default", payload("read this", channel=OTHER_DM, ts=LATER,
                                                subtype="file_share"))

    assert world.slack.posts == []


async def test_what_was_shown_is_remembered_by_the_workspaces_own_id(world):
    """Not by the name it is connected under, which can come to mean another team."""
    await world.inbox.handle("default", payload("first"))

    assert list(world.inbox._own) == [(TEAM, DM)] and world.inbox._own[(TEAM, DM)] is True


def test_the_check_is_one_read_of_one_message_with_the_bots_token():
    from keryx.integrations import slack as client

    calls = []

    def get(url, params, token):
        calls.append((url, params, token))
        return {"ok": True, "messages": [{"text": "something private"}]}

    assert client.in_conversation("xoxb-1", "D0OWNER", get=get).ok
    assert calls == [("https://slack.com/api/conversations.history",
                      {"channel": "D0OWNER", "limit": 1}, "xoxb-1")]
    refused = client.in_conversation(
        "xoxb-1", "D0X", get=lambda *a: {"ok": False, "error": "channel_not_found"})
    assert refused.error in client.NOT_A_PARTY == {"channel_not_found", "not_in_channel"}
    assert "im:history" in client.bot_scopes(client.Access.CONVERSE)


def test_owner_request_stays_a_function_of_the_payload_and_says_what_rests_on_the_place(
    conversing,
):
    one = workspace_of(conversing)

    assert owner_request(payload("no mention"), one).unmentioned is True
    assert owner_request(payload(), one).unmentioned is False
