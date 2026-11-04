"""Tests for the Notifier: announce, then SMS, then call back.

Everything outbound is a fake: no REST client, no socket, no real session. The store is a
real in-memory `TaskStore` and the token store is the real one, because what the tests
care about is the flags that end up on the row and whether the minted token can be
redeemed by the media socket afterwards.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import pytest
from fakes import FakeVoiceSession

from keryx.config import Settings
from keryx.events import EventBus, SessionEnded, TaskCompleted, TaskFailed
from keryx.inline_waits import InlineWaits
from keryx.notify.notifier import SLACK_PREAMBLE, SMS_BODY_LIMIT, Notifier
from keryx.notify.reports import report_token
from keryx.session import SessionRegistry
from keryx.stream_tokens import StreamTokenStore, confers_possession
from keryx.tasks.models import Task, TaskKind, TaskStatus
from keryx.tasks.store import TaskStore
from keryx.trust import TrustLevel

OWNER = "+15550000001"
CALLER = "+15551234567"
HOST = "keryx.example"
SECRET = "a-report-secret"


class FakeTwilioOut:
    """Records what would have gone to Twilio; `configured` and the errors are settable."""

    def __init__(self) -> None:
        self.configured = True
        #: What `SMS_ENABLED` decides on the real one: texting off, calling unaffected.
        self.sms_enabled = True
        self.sms: list[tuple[str, str]] = []
        self.calls: list[dict] = []
        self.sms_error: Exception | None = None
        self.call_error: Exception | None = None

    @property
    def can_text(self) -> bool:
        """Mirrors the real one: credentials *and* `SMS_ENABLED`, derived not snapshotted,
        so a test that drops `configured` afterwards stops texting the way Keryx would."""
        return self.configured and self.sms_enabled

    async def send_sms(self, to: str, body: str) -> str:
        if self.sms_error is not None:
            raise self.sms_error
        self.sms.append((to, body))
        return "SM1"

    async def place_call(self, to: str, *, twiml: str, status_callback: str | None = None) -> str:
        if self.call_error is not None:
            raise self.call_error
        self.calls.append({"to": to, "twiml": twiml, "status_callback": status_callback})
        return "CA1"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "openai_api_key": "test",
        "data_dir": tmp_path / "keryx",
        "owner_number_explicit": OWNER,
        "public_host": HOST,
        "report_secret": SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def stream_parameters(twiml: str) -> dict[str, str]:
    """The `<Parameter>` name/value pairs inside a `<Connect><Stream>` document."""
    stream = ElementTree.fromstring(twiml).find("./Connect/Stream")
    assert stream is not None, twiml
    return {p.get("name"): p.get("value") for p in stream.findall("Parameter")}


@dataclass
class Harness:
    notifier: Notifier
    bus: EventBus
    store: TaskStore
    sessions: SessionRegistry
    twilio: FakeTwilioOut
    tokens: StreamTokenStore
    settings: Settings
    inline_waits: InlineWaits

    async def task(self, **fields) -> Task:
        """A row in the store, defaulting to a finished local-origin chat task."""
        values: dict = {
            "kind": TaskKind.AGENT,
            "description": "look something up",
            "report_path": str(self.settings.data_dir / "tasks" / "1.md"),
        }
        values.update(fields)
        return await self.store.create(Task(id=None, **values))

    def session(self, **kwargs) -> FakeVoiceSession:
        session = FakeVoiceSession(**kwargs)
        self.sessions.add(session)
        return session

    async def finished(self, task: Task, summary: str = "all done") -> None:
        await self.bus.publish(TaskCompleted(task.id, summary))

    async def failed(self, task: Task, error: str = "it went wrong") -> None:
        await self.bus.publish(TaskFailed(task.id, error))

    async def row(self, task: Task) -> Task:
        return await self.store.get(task.id)

    async def hang_up(self, session: FakeVoiceSession, *, authorized: bool = False) -> None:
        """End `session` the way `_teardown` does, and wait for what that set off."""
        self.sessions.remove(session)
        await self.bus.publish(
            SessionEnded(session.session_id, "phone", session.caller, "hangup", authorized)
        )
        await asyncio.gather(*self.notifier._later)

    @property
    def sms_body(self) -> str:
        return self.twilio.sms[0][1]


class FakeSlack:
    """A `SlackSender` that records what it posted; `on` is the plugin, `error` a failure."""

    def __init__(self) -> None:
        self.on = True
        self.error: Exception | None = None
        self.sent: list[str] = []
        self.fallbacks: list[str | None] = []

    async def send(self, text: str, *, fallback: str | None = None) -> bool:
        if self.error is not None:
            raise self.error
        self.sent.append(text)
        self.fallbacks.append(fallback)
        return True


class FakeRestarter:
    """A `RestartCoordinator` that records what it was asked for and answers as told."""

    def __init__(self, status: str = "deferred") -> None:
        self.requests: list[dict] = []
        self.status = status
        self.error: Exception | None = None

    async def request(self, **kwargs) -> dict:
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"status": self.status, "message": "…"}


@pytest.fixture
async def harnesses(tmp_path) -> Callable[..., Harness]:
    """Factory for started Notifiers; every one built is stopped and closed afterwards."""
    made: list[Harness] = []

    def build(**overrides) -> Harness:
        store = TaskStore(":memory:")
        settings = make_settings(tmp_path, **overrides)
        bus = EventBus()
        sessions = SessionRegistry()
        twilio = FakeTwilioOut()
        tokens = StreamTokenStore()
        inline_waits = InlineWaits()
        restarter = FakeRestarter()
        slack = FakeSlack()
        notifier = Notifier(
            bus,
            store,
            sessions,
            twilio,
            settings,
            tokens,
            inline_waits,
            restarter,
            slack=lambda: slack if slack.on else None,
        )
        notifier.start()
        harness = Harness(
            notifier, bus, store, sessions, twilio, tokens, settings, inline_waits
        )
        harness.restarter = restarter
        harness.slack = slack
        made.append(harness)
        return harness

    yield build

    for harness in made:
        harness.notifier.stop()
        await harness.store.close()


@pytest.fixture
async def harness(harnesses) -> Harness:
    return harnesses()


# --- (1) announcing into live sessions -------------------------------------


async def test_a_live_phone_session_hears_the_result_and_nothing_else_goes_out(harness):
    session = harness.session(channel="phone")
    task = await harness.task(origin_channel="phone", origin_caller=CALLER)

    await harness.finished(task, "the tests pass now")

    assert session.announced == [f"Task {task.id} finished: the tests pass now"]
    assert session.announced_tasks == [task.id]  # so the call is asked to stamp it (#82)
    assert harness.twilio.sms == []
    assert harness.twilio.calls == []
    row = await harness.row(task)
    assert (row.announced, row.sms_sent) == (True, False)


async def test_every_live_session_hears_it(harness):
    phone = harness.session(channel="phone")
    local = harness.session(channel="local")
    task = await harness.task()

    await harness.finished(task, "done")

    assert phone.announced == local.announced == [f"Task {task.id} finished: done"]


async def test_a_session_that_is_no_longer_live_is_skipped(harness):
    ended = harness.session(channel="phone", is_live=False)
    task = await harness.task()

    await harness.finished(task)

    assert ended.announced == []
    assert (await harness.row(task)).announced is False
    assert harness.twilio.sms != []  # nobody heard it, so it still goes out by text


async def test_a_phone_session_that_refuses_the_announcement_still_gets_the_sms(harness):
    harness.session(channel="phone", accepts=False)
    task = await harness.task()

    await harness.finished(task)

    assert (await harness.row(task)).announced is False
    assert harness.twilio.sms[0][0] == OWNER


async def test_a_call_that_has_not_given_the_pin_hears_it_and_is_still_not_a_delivery(harness):
    """News is not gated on the PIN, but whoever heard it may not be the owner — so the
    call-back they asked for still goes out, rather than a spoofer absorbing the result."""
    stranger = harness.session(channel="phone", trust=TrustLevel.NONE)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "the balance is 1,234 pounds")

    assert stranger.announced == [f"Task {task.id} finished: the balance is 1,234 pounds"]
    assert (await harness.row(task)).announced is True  # a delivery was attempted
    assert [call["to"] for call in harness.twilio.calls] == [CALLER]


async def test_a_call_keryx_placed_hearing_it_is_a_delivery(harness):
    """Whoever answered is holding the owner's phone: ringing them again would be the
    duplicate every other rule here exists to avoid."""
    owner = harness.session(channel="phone", trust=TrustLevel.POSSESSION)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "the balance is 1,234 pounds")

    assert owner.announced != []
    assert harness.twilio.calls == []
    assert harness.twilio.sms == []


async def test_only_the_local_channel_hearing_it_does_not_replace_the_sms(harness):
    session = harness.session(channel="local")
    task = await harness.task()

    await harness.finished(task, "I looked it up")

    assert session.announced == [f"Task {task.id} finished: I looked it up"]
    row = await harness.row(task)
    assert (row.announced, row.sms_sent) == (True, True)
    assert harness.twilio.sms[0][0] == OWNER


async def test_a_session_that_blows_up_does_not_stop_the_sms(harness):
    harness.session(channel="phone", error=RuntimeError("socket is gone"))
    task = await harness.task()

    await harness.finished(task)

    assert harness.twilio.sms[0][0] == OWNER


async def test_a_session_holding_the_line_for_the_task_is_not_told_twice(harness):
    """`dispatch_task` hands the result back as the tool result; announcing repeats it."""
    waiting = harness.session(channel="phone", session_id="sess-a")
    other = harness.session(channel="phone", session_id="sess-b")
    task = await harness.task(origin_channel="phone", origin_caller=CALLER)

    with harness.inline_waits.holding("sess-a", task.id):
        await harness.finished(task, "done")

    assert waiting.announced == []
    assert other.announced == [f"Task {task.id} finished: done"]
    assert harness.twilio.sms == []
    assert (await harness.row(task)).announced is True


async def test_a_local_session_holding_the_line_gets_no_text_either(harness):
    """They asked and waited for the answer at the Mac; a text about it is noise."""
    waiting = harness.session(channel="local", session_id="sess-a")
    task = await harness.task(origin_channel="local")

    with harness.inline_waits.holding("sess-a", task.id):
        await harness.finished(task, "done")

    assert waiting.announced == []
    assert harness.twilio.sms == []


async def test_waiting_on_another_task_does_not_silence_this_one(harness):
    session = harness.session(channel="phone", session_id="sess-a")
    task = await harness.task()

    with harness.inline_waits.holding("sess-a", task.id + 1):
        await harness.finished(task, "done")

    assert session.announced == [f"Task {task.id} finished: done"]


async def test_the_wait_is_over_once_the_hold_is_released(harness):
    session = harness.session(channel="phone", session_id="sess-a")
    task = await harness.task()

    with harness.inline_waits.holding("sess-a", task.id):
        pass
    await harness.finished(task, "done")

    assert session.announced == [f"Task {task.id} finished: done"]


async def test_nobody_is_called_back_while_they_are_holding_the_line(harness):
    harness.session(channel="phone", session_id="sess-a")
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    with harness.inline_waits.holding("sess-a", task.id):
        await harness.finished(task)

    assert harness.twilio.calls == []


# --- (2) the SMS -----------------------------------------------------------


async def test_a_phone_task_texts_the_caller_a_summary_and_a_report_link(harness):
    task = await harness.task(origin_channel="phone", origin_caller=CALLER)

    await harness.finished(task, "I read the docs")

    to, body = harness.twilio.sms[0]
    assert to == CALLER
    summary_line, url = body.split("\n")
    assert summary_line == f"Task {task.id} finished: I read the docs"
    assert url.startswith(f"https://{HOST}/reports/{task.id}?t=")
    assert parse_qs(urlparse(url).query)["t"] == [report_token(task.id, SECRET)]
    assert (await harness.row(task)).sms_sent is True


async def test_a_local_task_texts_the_owner(harness):
    task = await harness.task(origin_channel="local")

    await harness.finished(task)

    assert harness.twilio.sms[0][0] == OWNER


async def test_a_phone_task_with_no_caller_on_the_row_falls_back_to_the_owner(harness):
    task = await harness.task(origin_channel="phone", origin_caller=None)

    await harness.finished(task)

    assert harness.twilio.sms[0][0] == OWNER


async def test_a_failed_task_says_so(harness):
    task = await harness.task(kind=TaskKind.AGENT)

    await harness.failed(task, "the build never went green")

    assert harness.sms_body.startswith(
        f"Task {task.id} failed: the build never went green"
    )


async def test_a_very_long_summary_is_cut_down_to_a_sendable_body(harness):
    task = await harness.task()

    await harness.finished(task, "x" * 5000)

    summary_line, url = harness.sms_body.split("\n")
    assert len(summary_line) == SMS_BODY_LIMIT
    assert url.startswith(f"https://{HOST}/reports/{task.id}?t=")


async def test_without_a_public_host_the_sms_is_just_the_summary(harnesses):
    harness = harnesses(public_host=None)
    task = await harness.task()

    await harness.finished(task, "done")

    assert harness.sms_body == f"Task {task.id} finished: done"


async def test_a_task_with_no_report_is_texted_without_a_link(harness):
    task = await harness.task(report_path=None)

    await harness.finished(task, "done")

    assert harness.sms_body == f"Task {task.id} finished: done"


async def test_nothing_is_sent_when_twilio_is_not_configured(harness):
    harness.twilio.configured = False
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert (harness.twilio.sms, harness.twilio.calls) == ([], [])
    row = await harness.row(task)
    assert (row.sms_sent, row.callback_requested) == (False, True)


async def test_with_no_number_to_text_nothing_is_sent(harnesses):
    harness = harnesses(owner_number_explicit=None)
    task = await harness.task(origin_channel="local")

    await harness.finished(task)

    assert harness.twilio.sms == []
    assert (await harness.row(task)).sms_sent is False


# --- (3) the call-back -----------------------------------------------------


async def test_a_requested_call_back_dials_out_with_a_redeemable_stream_token(harness):
    task = await harness.task(
        kind=TaskKind.AGENT,
        origin_channel="phone",
        origin_caller=CALLER,
        callback_requested=True,
        callback_number=CALLER,
    )

    await harness.finished(task, "I found the answer")

    call = harness.twilio.calls[0]
    assert call["to"] == CALLER
    assert call["status_callback"] == f"https://{HOST}/twilio/status"
    parameters = stream_parameters(call["twiml"])
    assert parameters["caller"] == CALLER
    assert parameters["task_id"] == str(task.id)
    info = harness.tokens.redeem(parameters["token"])
    assert info is not None
    assert info.caller == CALLER
    assert info.extra["task_id"] == task.id
    context = info.extra["opening_context"]
    assert f"task {task.id}" in context
    # A call-back is a new call, so it has to say what this was about, not just the answer.
    assert "look something up" in context

    # And that Keryx placed it, so the session it opens knows it is not a stranger.
    assert confers_possession(info, CALLER) is True
    assert "Result: I found the answer." in context
    assert "PIN" in context


async def test_a_call_back_is_only_dialled_once(harness):
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "first")
    await harness.finished(task, "second")  # a follow-up on the same task

    assert len(harness.twilio.calls) == 1
    assert (await harness.row(task)).callback_requested is False


async def test_a_failed_task_is_called_back_too(harness):
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.failed(task, "the build never went green")

    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    context = info.extra["opening_context"]
    assert "has failed" in context
    assert "look something up" in context
    assert "Error: the build never went green." in context


@pytest.mark.parametrize("failed", [False, True])
async def test_a_call_back_gives_its_news_in_the_first_turn(harness, failed):
    """Told only to greet and then say it, a model greets, promises news and waits (#63)."""
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    if failed:
        await harness.failed(task, "it broke")
    else:
        await harness.finished(task, "it worked")

    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    context = info.extra["opening_context"]
    assert "In your first turn" in context
    assert "do not stop after the greeting to wait for them" in context


async def test_nobody_is_called_back_while_they_are_already_on_the_phone(harness):
    harness.session(channel="phone")
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert harness.twilio.calls == []


async def test_a_call_back_needs_a_number_and_a_public_host(harnesses):
    without_number = harnesses()
    task = await without_number.task(callback_requested=True, callback_number=None)
    await without_number.finished(task)

    without_host = harnesses(public_host=None)
    other = await without_host.task(callback_requested=True, callback_number=CALLER)
    await without_host.finished(other)

    assert without_number.twilio.calls == []
    assert without_host.twilio.calls == []


async def test_a_task_nobody_asked_to_be_called_back_about_is_not_dialled(harness):
    task = await harness.task()

    await harness.finished(task)

    assert harness.twilio.calls == []


async def test_a_failed_sms_does_not_cost_the_call_back(harness):
    harness.twilio.sms_error = RuntimeError("twilio is down")
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert (await harness.row(task)).sms_sent is False
    assert harness.twilio.calls[0]["to"] == CALLER


async def test_a_failed_call_leaves_the_request_standing_and_raises_nothing(harness):
    harness.twilio.call_error = RuntimeError("twilio is down")
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert (await harness.row(task)).callback_requested is True


# --- lifecycle -------------------------------------------------------------


async def test_an_event_about_an_unknown_task_is_harmless(harness):
    await harness.bus.publish(TaskCompleted(404, "who?"))

    assert (harness.twilio.sms, harness.twilio.calls) == ([], [])


async def test_stopping_takes_the_notifier_off_the_bus(harness):
    session = harness.session(channel="local")
    task = await harness.task()

    harness.notifier.stop()
    await harness.finished(task)

    assert session.announced == []
    assert harness.twilio.sms == []


async def test_a_long_request_is_trimmed_in_the_call_back_context(harness):
    """The context is spoken from, not read; it carries the gist of the ask, not an essay."""
    task = await harness.task(
        callback_requested=True, callback_number=CALLER, description="x" * 500
    )

    await harness.finished(task, "done")

    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    assert "…" in info.extra["opening_context"]
    assert "x" * 300 not in info.extra["opening_context"]


async def test_the_call_back_carries_the_previous_call(harness):
    """A call-back is a new session, so the last one has to be handed to it."""
    task = await harness.task(
        callback_requested=True,
        callback_number=CALLER,
        origin_session_id="sess-42",
        callback_note="they want the tests run on the branch",
    )
    calls = harness.settings.data_dir / "calls"
    calls.mkdir(parents=True, exist_ok=True)
    (calls / "sess-42.log").write_text(
        "[10:00:00] --- session sess-42 channel=phone caller=+15550000000\n"
        "[10:00:02] user: look at the retry logic\n"
        "[10:00:05] assistant: On it — I'll call you back.\n",
        encoding="utf-8",
    )

    await harness.finished(task, "the retry is in")

    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    context = info.extra["opening_context"]
    assert "they want the tests run on the branch" in context
    assert "user: look at the retry logic" in context
    assert "do not read it back to them" in context


async def test_a_call_back_without_a_previous_session_still_goes_out(harness):
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "done")

    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    assert "Result: done." in info.extra["opening_context"]
    assert len(harness.twilio.calls) == 1


# --- housekeeping is not news ----------------------------------------------


async def test_an_internal_task_is_never_announced_texted_or_called_about(harness):
    """The per-call memory update is Keryx talking to itself; they never asked for it."""
    session = harness.session(channel="phone")
    task = await harness.task(
        description="update the memory after call abc123",
        internal=True,
        callback_requested=True,
        callback_number=CALLER,
    )

    await harness.finished(task)

    assert session.announced == []
    assert harness.twilio.sms == []
    assert harness.twilio.calls == []
    assert harness.slack.sent == []


async def test_an_internal_task_that_fails_is_just_as_quiet(harness):
    harness.session(channel="phone")
    task = await harness.task(internal=True)

    await harness.failed(task)

    assert harness.twilio.sms == []
    row = await harness.row(task)
    assert row.announced is False and row.sms_sent is False


# --- (3) the report on Slack ------------------------------------------------


#: A subagent's report in the shapes they come in, with the lines it addresses to Keryx.
REPORT = (
    "## What I found\n\n"
    "Two **open** issues, both [on the tracker](https://example.com/issues).\n\n"
    "| Issue | State |\n|---|---|\n| 1 | open |\n| 2 | open |\n\n"
    "1. the first\n2. the second"
)
MARKERS = "\n\nRESTART_REQUIRED: loads the new tool\n\nSPOKEN_SUMMARY: Two issues are open.\n"


async def test_slack_gets_the_report_alone_even_when_they_heard_it(harness):
    """Not the request above it, nor the spoken summary that is the report again."""
    harness.session(channel="phone")
    task = await harness.task(
        origin_channel="phone",
        origin_caller=CALLER,
        description="Follow the steps in /home/someone/steps.md\n\n---\n\nthen stop.",
    )
    report = harness.settings.data_dir / "tasks" / "1.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"# Task 1\n\n{task.description}\n\n---\n\n{REPORT}{MARKERS}")

    await harness.finished(task, "Two issues are open.")

    assert harness.slack.sent == [f"**Task {task.id} finished**\n\n{REPORT}"]
    assert harness.slack.fallbacks == [f"Task {task.id} finished"]


async def test_with_no_report_to_read_the_summary_is_posted(harness):
    task = await harness.task()

    await harness.finished(task, "Two issues are open.")

    assert harness.slack.sent == [f"**Task {task.id} finished**\n\nTwo issues are open."]


async def test_a_failure_leads_with_its_error_above_what_the_run_wrote(harness):
    task = await harness.task()
    report = harness.settings.data_dir / "tasks" / "1.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"# Task 1\n\n{task.description}\n\n---\n\nI'll start with the logs.")

    await harness.failed(task, "it broke")

    assert harness.slack.sent == [
        f"**Task {task.id} failed: it broke**\n\nI'll start with the logs."
    ]
    assert harness.slack.fallbacks == [f"Task {task.id} failed: it broke"]


async def test_a_failure_with_nothing_written_is_its_error_alone(harness):
    task = await harness.task()

    await harness.failed(task, "it broke")

    assert harness.slack.sent == [f"**Task {task.id} failed: it broke**"]


@pytest.mark.parametrize("slack_on", [True, False])
async def test_the_call_back_points_at_the_report_and_says_where_it_went(harness, slack_on):
    harness.slack.on = slack_on
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "found it")

    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    context = harness.tokens.redeem(parameters["token"]).extra["opening_context"]
    assert f"get_task_result for task {task.id}" in context
    assert (SLACK_PREAMBLE in context) is slack_on


async def test_a_slack_failure_costs_nothing_else(harness):
    harness.slack.error = RuntimeError("slack is down")
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "found it")

    assert len(harness.twilio.sms) == 1
    parameters = stream_parameters(harness.twilio.calls[0]["twiml"])
    context = harness.tokens.redeem(parameters["token"]).extra["opening_context"]
    assert SLACK_PREAMBLE not in context


# --- (4) a task that changed Keryx's own code ------------------------------


async def test_a_task_that_needs_a_restart_asks_for_one(harness):
    task = await harness.task(
        needs_restart=True, callback_requested=True, callback_number=CALLER,
        origin_channel="phone", origin_caller=CALLER, origin_session_id="sess-1",
    )

    await harness.finished(task, "added the recall tool")

    assert harness.restarter.requests == [
        {
            "reason": "to load what task 1 changed",
            "number": CALLER,
            "origin_channel": "phone",
            "origin_session_id": "sess-1",
            "task_id": task.id,
        }
    ]


async def test_the_restart_confirmation_becomes_the_call_back(harness):
    """Two calls a minute apart about the same work is what this exists to avoid."""
    task = await harness.task(
        needs_restart=True, callback_requested=True, callback_number=CALLER,
        origin_channel="phone", origin_caller=CALLER,
    )

    await harness.finished(task)

    assert harness.twilio.calls == []
    row = await harness.row(task)
    assert row.callback_requested is False  # nothing else will dial about this task
    assert row.needs_restart is False  # and a follow-up will not restart a second time


async def test_the_result_is_still_announced_and_texted_before_the_restart(harness):
    """Both cost nothing and both survive a restart that does not come back."""
    task = await harness.task(needs_restart=True, origin_channel="phone", origin_caller=CALLER)

    await harness.finished(task, "added the recall tool")

    assert "added the recall tool" in harness.sms_body
    assert harness.restarter.requests


async def test_a_refused_restart_does_not_swallow_the_call_back(harness):
    """No service manager on the machine: the restart cannot happen, the result still must."""
    harness.restarter.status = "unsupported"
    task = await harness.task(
        needs_restart=True, callback_requested=True, callback_number=CALLER,
    )

    await harness.finished(task)

    assert harness.twilio.calls, "the result went nowhere because the restart was refused"


async def test_a_restarter_that_raises_does_not_swallow_the_call_back(harness):
    harness.restarter.error = RuntimeError("systemd is unhappy")
    task = await harness.task(
        needs_restart=True, callback_requested=True, callback_number=CALLER,
    )

    await harness.finished(task)

    assert harness.twilio.calls


async def test_a_failed_task_never_restarts_anything(harness):
    """If the edit did not work, loading it is the last thing anybody wants."""
    task = await harness.task(needs_restart=True)

    await harness.failed(task)

    assert harness.restarter.requests == []


async def test_an_ordinary_task_asks_for_no_restart(harness):
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert harness.restarter.requests == []
    assert harness.twilio.calls


def test_a_summary_that_ends_in_a_stop_does_not_get_a_second_one():
    """The template supplies its own, and "the tests pass.." is what a voice reads out."""
    from keryx.notify.callback import no_trailing_stop

    assert no_trailing_stop("I added the recall tool and the tests pass.") == (
        "I added the recall tool and the tests pass"
    )
    assert no_trailing_stop("no stop here") == "no stop here"
    assert no_trailing_stop("trailing space. ") == "trailing space"


# --- texting turned off ----------------------------------------------------


async def test_with_texting_off_no_text_goes_out(harness):
    """The result still reaches them: the call-back, or the digest at the top of their next
    call, which is exactly what `reported_at` exists to keep honest."""
    harness.twilio.sms_enabled = False
    task = await harness.task(origin_channel="phone", origin_caller=CALLER)

    await harness.finished(task, "all done")

    assert harness.twilio.sms == []
    assert (await harness.row(task)).sms_sent is False


async def test_texting_off_does_not_cost_them_the_call_back(harness):
    """Calling and texting are separate capabilities, and only one of them is off."""
    harness.twilio.sms_enabled = False
    task = await harness.task(
        callback_requested=True, callback_number=CALLER, origin_channel="phone",
        origin_caller=CALLER,
    )

    await harness.finished(task)

    assert harness.twilio.sms == []
    assert harness.twilio.calls, "the call-back is not an SMS and must still happen"


async def test_an_unreported_task_still_rides_the_next_call(harness):
    """With no text and nobody listening, the digest is the only route left — so the task
    must stay unreported rather than be quietly marked delivered."""
    harness.twilio.sms_enabled = False
    task = await harness.task(status=TaskStatus.DONE, finished_at=datetime.now(UTC))

    await harness.finished(task)

    row = await harness.row(task)
    assert row.reported_at is None
    assert await harness.store.list_unreported() == [row]


# --- a call-back while they are on the line already (#100) --------------------


async def test_a_call_back_waits_while_their_phone_is_on_a_call_with_keryx(harness):
    """They rang in before the PIN and heard it: their phone is busy, and the call that
    heard it proved nothing, so the call-back goes out once it ends rather than on top."""
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "the build is green")

    assert on_the_line.announced == [f"Task {task.id} finished: the build is green"]
    assert harness.twilio.calls == []

    await harness.hang_up(on_the_line)

    assert [call["to"] for call in harness.twilio.calls] == [CALLER]
    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    assert "Result: the build is green." in info.extra["opening_context"]
    assert (await harness.row(task)).callback_requested is False


async def test_a_pin_given_after_the_announcement_makes_that_call_the_delivery(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    first = await harness.task(callback_requested=True, callback_number=CALLER)
    second = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(first)
    await harness.finished(second)
    await harness.hang_up(on_the_line, authorized=True)

    assert harness.twilio.calls == []


async def test_without_the_pin_every_waiting_call_back_rings(harness):
    """As when several finish at once with nobody on the line: one call each."""
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    tasks = [await harness.task(callback_requested=True, callback_number=CALLER) for _ in "ab"]
    for task in tasks:
        await harness.finished(task)

    await harness.hang_up(on_the_line)

    assert [stream_parameters(c["twiml"])["task_id"] for c in harness.twilio.calls] == [
        str(task.id) for task in tasks
    ]


async def test_a_result_marked_reported_on_that_call_is_not_rung_through(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)
    await harness.store.update(task.id, reported_at=datetime.now(UTC))
    await harness.hang_up(on_the_line)

    assert harness.twilio.calls == []


@pytest.mark.parametrize(
    "deaf",
    [
        {"is_live": False},  # hanging up already: still on the line, past hearing anything
        {"accepts": False},  # refused it: `BRIEFING_BEFORE_PIN` off, before the PIN
    ],
    ids=["hanging-up", "refused"],
)
async def test_a_pin_on_a_call_that_never_heard_it_is_not_a_delivery(harness, deaf):
    """The PIN says who they are, not that they heard this result: it rings after."""
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER, **deaf)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)
    assert (on_the_line.announced, harness.twilio.calls) == ([], [])

    await harness.hang_up(on_the_line, authorized=True)

    assert [call["to"] for call in harness.twilio.calls] == [CALLER]


async def test_a_waiting_call_back_waits_again_for_the_next_call_on_that_phone(harness):
    dropped = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    redialled = harness.session(
        channel="phone", trust=TrustLevel.NONE, caller=CALLER, session_id="sess-2"
    )
    await harness.hang_up(dropped)
    assert harness.twilio.calls == []

    await harness.hang_up(redialled)
    assert len(harness.twilio.calls) == 1


async def test_waiting_on_the_next_call_still_knows_that_call_heard_it(harness):
    """Two calls on the number, the one hanging up found first: the PIN on the one that
    heard it still counts once the wait has moved on to it."""
    leaving = harness.session(channel="phone", is_live=False, caller=CALLER)
    listening = harness.session(
        channel="phone", trust=TrustLevel.NONE, caller=CALLER, session_id="sess-2"
    )
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    await harness.hang_up(leaving)
    await harness.hang_up(listening, authorized=True)

    assert listening.announced != []
    assert harness.twilio.calls == []


async def test_a_follow_up_finishing_on_the_same_call_still_rings_once(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task, "first")
    await harness.finished(task, "second")
    await harness.hang_up(on_the_line)

    assert len(harness.twilio.calls) == 1
    info = harness.tokens.redeem(stream_parameters(harness.twilio.calls[0]["twiml"])["token"])
    assert "Result: second." in info.extra["opening_context"]


async def test_a_follow_up_told_elsewhere_drops_the_call_back_still_waiting(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task, "first")

    harness.session(channel="phone", caller=OWNER, session_id="sess-2")  # the PIN given
    await harness.finished(task, "second")
    await harness.hang_up(on_the_line)

    assert harness.twilio.calls == []


async def test_a_call_from_another_number_does_not_hold_the_call_back(harness):
    harness.session(channel="phone", trust=TrustLevel.NONE, caller=OWNER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)

    await harness.finished(task)

    assert [call["to"] for call in harness.twilio.calls] == [CALLER]


async def test_each_waiting_call_back_waits_for_its_own_phone(harness):
    mine = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    theirs = harness.session(
        channel="phone", trust=TrustLevel.NONE, caller=OWNER, session_id="sess-2"
    )
    for number in (CALLER, OWNER):
        await harness.finished(await harness.task(callback_requested=True, callback_number=number))

    await harness.hang_up(mine)
    assert [call["to"] for call in harness.twilio.calls] == [CALLER]

    await harness.hang_up(theirs)
    assert [call["to"] for call in harness.twilio.calls] == [CALLER, OWNER]


async def test_stopping_drops_a_call_back_waiting_on_a_call(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    harness.notifier.stop()
    await harness.hang_up(on_the_line)

    assert harness.twilio.calls == []


async def test_stopping_drops_a_call_back_the_end_of_a_call_set_off(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    harness.sessions.remove(on_the_line)
    await harness.bus.publish(SessionEnded(on_the_line.session_id, "phone", CALLER, "hangup"))
    harness.notifier.stop()
    await asyncio.gather(*harness.notifier._later, return_exceptions=True)

    assert harness.twilio.calls == []


async def test_a_call_back_after_the_call_that_blows_up_raises_nothing(harness):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    await harness.store.close()  # the row can no longer be read
    await harness.hang_up(on_the_line)

    assert harness.twilio.calls == []


async def test_a_task_deleted_while_waiting_on_a_call_is_dropped(harness, monkeypatch):
    on_the_line = harness.session(channel="phone", trust=TrustLevel.NONE, caller=CALLER)
    task = await harness.task(callback_requested=True, callback_number=CALLER)
    await harness.finished(task)

    async def gone(task_id):
        return None

    monkeypatch.setattr(harness.store, "get", gone)
    await harness.hang_up(on_the_line)

    assert harness.twilio.calls == []
