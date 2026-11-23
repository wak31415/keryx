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
    NOTHING_TO_RESUME,
    STARTING,
    InboxApi,
    SlackInbox,
    owner_message,
)
from keryx.tasks.agent_runner import FakeAgentRunner
from keryx.tasks.manager import TaskManager
from keryx.tasks.models import Task, TaskKind, TaskStatus
from keryx.tasks.store import TaskStore

OWNER, DM, TEAM = "U0OWNER", "D0OWNER", "T0ACME"
NAME = "send_to_slack"
TS = "1700000000.000100"
LATER = "1700000000.000200"


def payload(text="look into the flaky test", *, ts=TS, user=OWNER, **event) -> dict:
    """A `message.im` event as Socket Mode delivers one, typed by `user`."""
    return {
        "type": "event_callback",
        "team_id": TEAM,
        "event_id": "Ev1",
        "authorizations": [{"team_id": TEAM, "user_id": "U0BOT", "is_bot": True}],
        "event": {"type": "message", "channel": DM, "user": user, "text": text, "ts": ts,
                  "channel_type": "im", **event},
    }


def reply(text="and the other one", *, ts=LATER, under=TS, **event) -> dict:
    return payload(text, ts=ts, thread_ts=under, **event)


@dataclass
class FakeSlack:
    """Slack as the inbox reaches it: reactions, posts in a thread, and listeners."""

    reactions: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    listeners: list = field(default_factory=list)
    react_result: SlackResult = field(default_factory=SlackResult)
    post_error: Exception | None = None

    def api(self) -> InboxApi:
        fake = self

        def react(token, channel, ts):
            fake.reactions.append((token, channel, ts))
            if isinstance(fake.react_result, Exception):
                raise fake.react_result
            return fake.react_result

        class Poster:
            def __init__(self, token, channel):
                self.token, self.channel = token, channel

            async def post(self, text, *, fallback=None, thread_ts=None, broadcast=False):
                if fake.post_error is not None:
                    raise fake.post_error
                fake.posts.append((self.token, self.channel, thread_ts, text))
                return Posted(self.channel, "1700000001.000000")

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

        return InboxApi(react=react, poster=Poster, listener=Listener)


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
    })
    plugins.install(settings, NAME)
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


# --- which messages are the owner's -------------------------------------------------------


def test_a_message_the_owner_typed_to_the_app_is_one(conversing):
    message = owner_message(payload("fix &lt;it&gt; &amp; ship"), workspace_of(conversing))

    assert (message.channel, message.ts, message.thread_ts) == (DM, TS, None)
    assert message.text == "fix <it> & ship"


def test_a_reply_knows_the_thread_it_is_in_and_a_thread_root_is_a_message_of_its_own(conversing):
    one = workspace_of(conversing)

    assert owner_message(reply(), one).thread_ts == TS
    assert owner_message(payload(thread_ts=TS), one).thread_ts is None


@pytest.mark.parametrize(
    "event",
    [
        {"user": "U0SOMEONE"},
        {"user": None},
        {"channel_type": "channel"},
        {"channel_type": "mpim"},
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"subtype": "file_share"},
        {"subtype": "bot_message"},
        {"subtype": "thread_broadcast"},
        {"bot_id": "B0BOT"},
        {"type": "reaction_added"},
        {"ts": "yesterday"},
        {"ts": None},
        {"thread_ts": "1700000000.000100; drop table"},
        {"channel": ""},
        {"channel": 7},
        {"text": ""},
        {"text": "   "},
        {"text": None},
    ],
)
def test_anything_else_in_the_event_is_not(conversing, event):
    assert owner_message(payload(**event), workspace_of(conversing)) is None


@pytest.mark.parametrize(
    "envelope",
    [
        {"type": "url_verification"},
        {"team_id": "T0OTHER"},
        {"event": "message"},
        {"event": None},
        # Delivered for the owner's own authorization: a user-level subscription, which
        # would be every direct message they have with anybody.
        {"authorizations": [{"team_id": TEAM, "user_id": OWNER, "is_bot": False}]},
        {"authorizations": ["not an object"]},
    ],
)
def test_anything_else_around_the_event_is_not(conversing, envelope):
    assert owner_message({**payload(), **envelope}, workspace_of(conversing)) is None
    assert owner_message("not an object", workspace_of(conversing)) is None


def test_with_no_owner_on_file_nobody_is_the_owner(conversing):
    plugins.write_config(conversing, NAME, {"owner_id": ""})
    nobody = payload(user="")

    assert owner_message(nobody, workspace_of(conversing)) is None
    assert owner_message(payload(), workspace_of(conversing)) is None


def test_an_event_that_does_not_say_whose_authorization_it_is_for_is_still_read(conversing):
    quiet = payload()
    del quiet["authorizations"]

    assert owner_message(quiet, workspace_of(conversing)) is not None


# --- a message of its own starts work -----------------------------------------------------


async def test_a_new_message_is_dispatched_and_its_thread_is_the_message_itself(world):
    await world.inbox.handle("default", payload())

    [task] = await world.tasks()
    assert task.description == "look into the flaky test"
    assert (task.origin_channel, task.origin_caller) == ("slack", None)
    assert (task.slack_workspace, task.slack_channel, task.slack_thread_ts, task.slack_last_ts) == (
        "default", DM, TS, TS
    )
    assert not task.internal and not task.callback_requested
    # The acknowledgement is a reaction on their message, and nothing is said.
    assert world.slack.reactions == [("xoxb-first", DM, TS)]
    assert world.slack.posts == []
    await world.done(task.id)


async def test_the_same_message_delivered_twice_starts_one_task(world):
    await world.inbox.handle("default", payload())
    await world.inbox.handle("default", payload())

    assert len(await world.tasks()) == 1 and len(world.slack.reactions) == 1


async def test_a_message_delivered_again_after_a_restart_still_starts_one_task(world):
    await world.inbox.handle("default", payload())

    await world.again().handle("default", payload())

    assert len(await world.tasks()) == 1 and len(world.slack.reactions) == 1
    assert world.slack.posts == []


async def test_the_daily_cap_applies_and_is_said_in_one_sentence_in_the_thread(
    make_world, conversing
):
    world = make_world(conversing.model_copy(update={"daily_task_cap": 1}))
    await world.inbox.handle("default", payload())

    await world.inbox.handle("default", payload("and one more", ts=LATER))

    assert len(await world.tasks()) == 1
    assert world.slack.posts == [
        ("xoxb-first", DM, LATER,
         "Keryx has reached today's limit of 1 tasks, so this was not started."),
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


# --- a reply in a task's thread resumes it ------------------------------------------------


async def test_a_reply_under_a_task_asked_for_on_slack_is_a_follow_up_to_it(world):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    await world.inbox.handle("default", reply())

    resumed = await world.done(task.id)
    assert len(await world.tasks()) == 1
    assert world.runner.sessions[1].prompts == ["and the other one"]
    assert resumed.slack_last_ts == LATER and resumed.slack_thread_ts == TS
    assert world.slack.reactions == [("xoxb-first", DM, TS), ("xoxb-first", DM, LATER)]
    assert world.slack.posts == []


async def test_a_reply_under_a_report_keryx_posted_resumes_the_task_it_reported(world):
    """A task asked for on the phone: its thread is its first report."""
    task = await world.manager.dispatch("from a call", origin_channel="phone",
                                        origin_caller="+15550001111")
    await world.done(task.id)
    await world.store.update(task.id, slack_workspace="default", slack_channel=DM,
                             slack_thread_ts=TS)

    await world.inbox.handle("default", reply("also check staging"))

    # The follow-up put the row back to queued before it returned, so done is the re-run.
    assert (await world.done(task.id)).slack_last_ts == LATER
    assert world.runner.sessions[1].prompts == ["also check staging"]


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


async def test_a_reply_in_a_thread_that_is_no_tasks_is_told_so_in_one_sentence(world):
    await world.inbox.handle("default", reply())

    assert await world.tasks() == []
    assert world.slack.posts == [("xoxb-first", DM, TS, NOTHING_TO_RESUME)]
    assert world.slack.reactions == []


async def test_a_reply_under_a_cancelled_task_has_nothing_to_resume(make_world, conversing):
    world = make_world(conversing, FakeAgentRunner(delay_s=5.0))
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.manager.cancel(task.id)

    await world.inbox.handle("default", reply())

    assert world.slack.posts == [("xoxb-first", DM, TS, CANCELLED.format(task_id=task.id))]
    assert len(world.runner.opened) <= 1


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
    # Not recorded as heard: the same reply sent again is tried again.
    assert (await world.store.get(task.id)).slack_last_ts == TS


async def test_a_reply_that_cannot_be_recorded_is_still_acknowledged(world, monkeypatch):
    await world.inbox.handle("default", payload())
    [task] = await world.tasks()
    await world.done(task.id)

    async def refuse(task_id, ts):
        raise RuntimeError("the database is locked")

    monkeypatch.setattr(world.manager, "heard_on_slack", refuse)
    await world.inbox.handle("default", reply())

    assert world.slack.reactions[-1] == ("xoxb-first", DM, LATER)


# --- what is never acted on ---------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        payload(user="U0SOMEONE"),
        payload(channel_type="channel"),
        payload(subtype="message_changed"),
        payload(bot_id="B0BOT"),
        reply(user="U0SOMEONE"),
        {"type": "event_callback", "event": {"type": "app_mention"}},
    ],
)
async def test_what_is_not_the_owners_message_is_dropped_without_a_word(world, event):
    await world.inbox.handle("default", event)

    assert await world.tasks() == []
    assert world.slack.posts == [] and world.slack.reactions == []


async def test_lowering_the_level_stops_it_acting_at_once(world):
    plugins.write_config(world.settings, NAME, {"access": "announce"})

    await world.inbox.handle("default", payload())

    assert await world.tasks() == [] and world.slack.reactions == []


async def test_turning_the_plugin_off_stops_it_acting_at_once(world):
    plugins.remove(world.settings, NAME)

    await world.inbox.handle("default", payload())

    assert await world.tasks() == [] and world.slack.reactions == []


async def test_a_workspace_nobody_connected_is_never_acted_for(world):
    await world.inbox.handle("elsewhere", payload())

    assert await world.tasks() == []


async def test_nothing_a_message_says_is_logged(world, caplog):
    with caplog.at_level(logging.DEBUG):
        await world.inbox.handle("default", payload("the launch code is 0000"))
        [task] = await world.tasks()
        await world.done(task.id)
        await world.inbox.handle("default", reply("and tell nobody"))
        await world.inbox.handle("default", reply("stray", under="1600000000.000001",
                                                  ts="1700000000.000900"))

    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name.startswith("keryx.slack"))
    assert "launch code" not in text and "tell nobody" not in text and "stray" not in text
    assert f"task {task.id} started from a message in default" in text


# --- what Keryx says back never costs the work ---------------------------------------------


@pytest.mark.parametrize("failure", [SlackResult(error="missing_scope"), RuntimeError("down")])
async def test_a_reaction_slack_refuses_costs_nothing(world, failure):
    world.slack.react_result = failure

    await world.inbox.handle("default", payload())

    assert len(await world.tasks()) == 1


async def test_a_sentence_slack_refuses_costs_nothing(world):
    world.slack.post_error = RuntimeError("down")

    await world.inbox.handle("default", reply())

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
                 "team_id": "T0WORK"},
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
    event = payload("from work", user="U0WORK")
    event["team_id"] = "T0WORK"
    event["event"]["channel"] = "D0WORK"

    await work.handle(event)

    [task] = await world.tasks()
    assert (task.slack_workspace, task.slack_channel) == ("work", "D0WORK")
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
