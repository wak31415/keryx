"""Tests for the tools the voice model calls (spec §3.2 `tools/registry.py`, §3.3).

Everything runs against a real `TaskManager` with a `FakeAgentRunner` and an in-memory
`TaskStore`, so the dicts asserted on here are the dicts the model would really see.
The session is a `StubSession`: the tools only need the duck-typed slice of it, and the
real `VoiceSession` PIN machinery has its own module (`tests/test_session_pin.py`).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from jarvis.config import Settings
from jarvis.continuity.recall import DEFAULT_LIMIT as DEFAULT_RECALL_LIMIT
from jarvis.continuity.recall import MAX_LIMIT as MAX_RECALL_LIMIT
from jarvis.continuity.recall import Hit
from jarvis.events import EventBus
from jarvis.inline_waits import InlineWaits
from jarvis.integrations.billing import BillingError, BillingReport
from jarvis.integrations.cluster import MESSAGES, ClusterError, ClusterReport, GpuCounts, MyJobs
from jarvis.tasks.agent_runner import FakeAgentRunner, RunResult
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import TaskStatus
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolContext, ToolRegistry
from jarvis.tools.builtin import register_builtin_tools
from jarvis.tools.builtin_common import (
    CALLBACK_SET_MESSAGE,
    PIN_INVALID_MESSAGE,
    PIN_OK_MESSAGE,
    REPORTED_MESSAGE,
    STILL_RUNNING_MESSAGE,
)

WAIT = 2.0  # upper bound (seconds) for every wait in this module
SLOW = 0.3  # a fake agent turn long enough to observe a task while it is still running

TOOL_NAMES = {
    "dispatch_task",
    "list_tasks",
    "get_task_status",
    "get_task_result",
    "send_followup",
    "cancel_task",
    "list_projects",
    "request_callback",
    "mark_reported",
    "submit_pin",
    "end_session",
}


# --- harness ---------------------------------------------------------------


@dataclass
class StubSession:
    """The duck-typed slice of `VoiceSession` the tools actually touch."""

    authorized: bool = True
    channel: str = "local"
    caller: str | None = None
    session_id: str = "sess1234"
    pin_result: dict = field(default_factory=lambda: {"status": "authorized"})
    pins: list[str] = field(default_factory=list)
    ends: list[str] = field(default_factory=list)

    async def submit_pin(self, pin: str) -> dict:
        self.pins.append(pin)
        return self.pin_result

    def request_end(self, reason: str = "user") -> None:
        self.ends.append(reason)


@dataclass
class Harness:
    registry: ToolRegistry
    manager: TaskManager
    settings: Settings
    store: TaskStore
    runner: FakeAgentRunner
    session: StubSession
    inline_waits: InlineWaits

    async def call(self, name: str, arguments: dict | None = None, **overrides) -> dict:
        """Invoke a tool through the registry, exactly as the session would."""
        session = self.session
        for name_, value in overrides.items():
            setattr(session, name_, value)
        ctx = ToolContext(session=session, channel=session.channel, caller=session.caller)
        return await self.registry.call(name, arguments or {}, ctx)

    async def dispatch(self, description: str = "how tall is Everest", **kw):
        return await self.manager.dispatch(
            description, origin_channel="local", origin_caller=None, **kw
        )


@pytest.fixture
async def make_tools(tmp_path):
    """Factory for a registry wired to a real manager; every manager is shut down after."""
    built: list[Harness] = []
    repo = tmp_path / "repo"
    repo.mkdir()

    def _build(
        runner: FakeAgentRunner | None = None,
        *,
        searcher=None,
        slack=None,
        restarter=None,
        recaller=None,
        billing=None,
        cluster=None,
        **overrides,
    ) -> Harness:
        settings = Settings(
            _env_file=None,
            openai_api_key="test",
            data_dir=tmp_path / "jarvis",
            projects={"jarvis": str(repo)},
            projects_root=tmp_path / "no-such-root",
            **overrides,
        )
        store = TaskStore(":memory:")
        agent_runner = runner or FakeAgentRunner()
        manager = TaskManager(store, agent_runner, EventBus(), settings)
        registry = ToolRegistry()
        inline_waits = InlineWaits()
        register_builtin_tools(
            registry,
            manager=manager,
            settings=settings,
            inline_waits=inline_waits,
            searcher=searcher,
            slack=slack,
            restarter=restarter,
            recaller=recaller,
            billing=billing,
            cluster=cluster,
        )
        harness = Harness(
            registry, manager, settings, store, agent_runner, StubSession(), inline_waits
        )
        built.append(harness)
        return harness

    yield _build

    for harness in built:
        await harness.manager.shutdown()
        await harness.store.close()


@pytest.fixture
async def tools(make_tools):
    return make_tools()


async def wait_for_status(harness: Harness, task_id: int, status: TaskStatus) -> None:
    """Poll until `task_id` reaches `status`, or fail the test after `WAIT` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        task = await harness.manager.get(task_id)
        if task is not None and task.status is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {status}")


# --- registration ----------------------------------------------------------


def test_every_tool_is_registered_with_a_usable_schema(tools):
    schemas = {schema["name"]: schema for schema in tools.registry.schemas()}

    assert set(schemas) == TOOL_NAMES
    for name, schema in schemas.items():
        assert schema["type"] == "function"
        assert schema["description"].strip(), f"{name} has no description"
        assert schema["parameters"]["type"] == "object"
        assert isinstance(schema["parameters"]["required"], list)


def test_the_dispatch_schema_asks_only_for_the_work_and_the_wait(tools):
    """No kind to classify: the model decides to hand over, not what sort of task it is."""
    schema = next(s for s in tools.registry.schemas() if s["name"] == "dispatch_task")

    properties = schema["parameters"]["properties"]
    assert "kind" not in properties
    assert schema["parameters"]["required"] == ["description"]
    assert properties["wait_seconds"]["maximum"] == 25


# --- dispatch_task: the PIN gate (spec §3.3) -------------------------------


async def test_local_sessions_dispatch_destructive_work_without_a_pin(make_tools):
    tools = make_tools(pin="424242")

    result = await tools.call(
        "dispatch_task",
        {"description": "add a README", "project": "jarvis"},
        channel="local",
        authorized=False,
    )

    assert result["task_id"] == 1
    assert "status" in result


async def test_an_unauthorized_phone_caller_is_asked_for_the_pin(make_tools):
    tools = make_tools(pin="424242")

    result = await tools.call(
        "dispatch_task",
        {"description": "add a README", "project": "jarvis"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "pin_required"
    assert "PIN" in result["message"]
    assert await tools.manager.list() == []  # nothing was dispatched


async def test_an_authorized_phone_caller_dispatches_destructive_work(make_tools):
    tools = make_tools(pin="424242")

    result = await tools.call(
        "dispatch_task",
        {"description": "draft a reply to Anna"},
        channel="phone",
        caller="+15555555555",
        authorized=True,
    )

    assert result["task_id"] == 1


async def test_every_phone_dispatch_needs_the_pin_now(make_tools):
    """There is one kind of task and it has the machine and the mailbox: all of it is gated."""
    tools = make_tools(pin="424242")

    result = await tools.call(
        "dispatch_task",
        {"description": "what happened at CES"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "pin_required"


async def test_destructive_work_is_refused_when_no_pin_is_configured(make_tools):
    tools = make_tools()  # settings.pin is None

    result = await tools.call(
        "dispatch_task",
        {"description": "add a README", "project": "jarvis"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "refused"
    assert "none is configured" in result["message"]


async def test_a_blank_pin_is_no_pin_at_all(make_tools):
    """Defence in depth: an empty PIN that slipped past `Settings` unlocks nothing."""
    tools = make_tools(pin="424242")
    tools.settings.pin = ""

    result = await tools.call(
        "dispatch_task",
        {"description": "add a README", "project": "jarvis"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "refused"


# --- dispatch_task: dispatching -------------------------------------------


    assert await tools.manager.list() == []


async def test_waiting_returns_the_summary_inline(tools):
    result = await tools.call(
        "dispatch_task",
        {"description": "how tall is Everest", "wait_seconds": 20},
    )

    assert result == {"task_id": 1, "status": "done", "summary": "I finished the task."}


async def test_a_long_task_comes_back_running_with_a_promise(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))

    result = await tools.call(
        "dispatch_task", {"description": "read all of Wikipedia"}
    )

    assert result["task_id"] == 1
    assert result["status"] in {"queued", "running"}
    assert "summary" not in result
    assert result["message"] == STILL_RUNNING_MESSAGE
    # He is told it is running and that he will hear; what the answer will *say* is not
    # knowable yet, and a promise about it is the model inventing a result.
    assert "still running" in result["message"]


async def test_the_waiting_session_is_marked_so_it_is_not_told_the_result_twice(tools):
    """The notifier must skip a session that is getting the result as a tool result."""
    waiting: list[bool] = []
    original = tools.manager.wait_for

    async def spy(task_id: int, timeout: float):
        waiting.append((tools.session.session_id, task_id) in tools.inline_waits)
        return await original(task_id, timeout)

    tools.manager.wait_for = spy

    await tools.call(
        "dispatch_task",
        {"description": "how tall is Everest", "wait_seconds": 20},
    )

    assert waiting == [True]
    assert ("sess1234", 1) not in tools.inline_waits  # and the mark is gone afterwards


async def test_a_task_that_failed_within_the_wait_says_what_went_wrong(make_tools):
    tools = make_tools(
        FakeAgentRunner([RunResult(ok=False, spoken_summary="it broke", error="exit code 2")])
    )

    result = await tools.call(
        "dispatch_task", {"description": "build it", "wait_seconds": 20}
    )

    assert result == {
        "task_id": 1,
        "status": "failed",
        "summary": "it broke",
        "error": "exit code 2",
    }


async def test_the_wait_is_clamped_to_the_configured_maximum(tools):
    seen: list[float] = []
    original = tools.manager.wait_for

    async def spy(task_id: int, timeout: float):
        seen.append(timeout)
        return await original(task_id, timeout)

    tools.manager.wait_for = spy

    await tools.call(
        "dispatch_task",
        {"description": "how tall is Everest", "wait_seconds": 999},
    )

    assert seen == [float(tools.settings.dispatch_wait_max_seconds)]


async def test_a_negative_wait_never_reaches_the_manager(tools):
    called = False

    async def spy(task_id: int, timeout: float):
        nonlocal called
        called = True

    tools.manager.wait_for = spy

    result = await tools.call(
        "dispatch_task",
        {"description": "how tall is Everest", "wait_seconds": -5},
    )

    assert called is False
    assert result["task_id"] == 1


async def test_an_unknown_project_comes_back_with_the_candidates(tools):
    result = await tools.call(
        "dispatch_task",
        {"description": "add a README", "project": "wat"},
    )

    assert "unknown project" in result["error"]
    assert result["candidates"] == ["jarvis"]


async def test_a_coding_task_without_a_project_is_dispatched_anyway(tools):
    """No interrogation over the voice channel: dispatch, and let the subagent work it out."""
    result = await tools.call("dispatch_task", {"description": "add a README"})

    assert "error" not in result
    assert result["task_id"] == 1


async def test_the_daily_cap_comes_back_as_an_error(make_tools):
    tools = make_tools(daily_task_cap=0)

    result = await tools.call("dispatch_task", {"description": "anything"})

    assert "daily task cap" in result["error"]


async def test_a_missing_description_is_refused(tools):
    result = await tools.call("dispatch_task", {"description": "  "})

    assert "description" in result["error"]


# --- list_tasks ------------------------------------------------------------


async def test_list_tasks_returns_the_newest_first_and_shortens_descriptions(tools):
    await tools.dispatch(description="first")
    await tools.dispatch(description="x" * 300)

    result = await tools.call("list_tasks", {})

    ids = [entry["id"] for entry in result["tasks"]]
    assert ids == [2, 1]
    longest = result["tasks"][0]["description"]
    assert len(longest) == 120
    assert longest.endswith("…")


async def test_list_tasks_defaults_to_five_and_caps_at_twenty(tools):
    for index in range(25):
        await tools.dispatch(description=f"task {index}")

    assert len((await tools.call("list_tasks", {}))["tasks"]) == 5
    assert len((await tools.call("list_tasks", {"limit": 100}))["tasks"]) == 20


async def test_list_tasks_running_includes_queued_work(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)
    await tools.dispatch(description="the running one")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)
    await tools.dispatch(description="the queued one")

    result = await tools.call("list_tasks", {"status": "running"})

    assert [entry["status"] for entry in result["tasks"]] == ["queued", "running"]
    assert [entry["id"] for entry in result["tasks"]] == [2, 1]


async def test_list_tasks_can_filter_to_finished_work(tools):
    await tools.dispatch(description="finished")
    await tools.manager.wait_for(1, WAIT)
    await tools.dispatch(description="not asked for")
    await tools.manager.cancel(2)

    result = await tools.call("list_tasks", {"status": "done"})

    assert [entry["id"] for entry in result["tasks"]] == [1]
    assert result["tasks"][0]["summary"] == "I finished the task."


async def test_list_tasks_rejects_a_status_it_does_not_know(tools):
    result = await tools.call("list_tasks", {"status": "sideways"})

    assert "unknown status" in result["error"]


# --- the task_id every task tool takes -------------------------------------


@pytest.mark.parametrize(
    "tool, extra",
    [
        ("get_task_status", {}),
        ("get_task_result", {}),
        ("send_followup", {"message": "hi"}),
        ("cancel_task", {}),
        ("request_callback", {}),
    ],
)
async def test_a_task_id_that_names_nothing_is_an_error(tools, tool, extra):
    assert await tools.call(tool, {"task_id": 99, **extra}) == {"error": "no task 99"}


async def test_a_task_id_that_is_not_a_number_is_an_error(tools):
    result = await tools.call("get_task_status", {"task_id": "the blue one"})

    assert "task_id" in result["error"]


# --- get_task_status / get_task_result ------------------------------------


async def test_get_task_status_describes_one_task(tools):
    await tools.dispatch(description="how tall is Everest")
    await tools.manager.wait_for(1, WAIT)

    result = await tools.call("get_task_status", {"task_id": 1})

    assert result["task_id"] == 1
    assert result["status"] == "done"
    assert result["description"] == "how tall is Everest"
    assert result["summary"] == "I finished the task."
    assert result["error"] is None
    assert result["line"].startswith("task 1 (done)")


async def test_get_task_result_carries_an_excerpt_of_the_report(tools):
    await tools.dispatch(description="how tall is Everest")
    await tools.manager.wait_for(1, WAIT)

    result = await tools.call("get_task_result", {"task_id": 1})

    assert result["status"] == "done"
    assert result["summary"] == "I finished the task."
    assert "how tall is Everest" in result["report_excerpt"]


async def test_a_long_report_is_cut_down_for_the_voice_model(make_tools):
    long_report = RunResult(ok=True, final_text="x" * 5000, spoken_summary="done")
    tools = make_tools(FakeAgentRunner([long_report]))
    await tools.dispatch(description="write a book")
    await tools.manager.wait_for(1, WAIT)

    result = await tools.call("get_task_result", {"task_id": 1})

    assert len(result["report_excerpt"]) == 1500
    assert result["report_excerpt"].endswith("…")


async def test_get_task_result_without_a_report_says_nothing_about_one(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="still going")

    result = await tools.call("get_task_result", {"task_id": 1})

    assert "report_excerpt" not in result
    assert result["status"] in {"queued", "running"}


# --- send_followup / cancel_task ------------------------------------------


async def test_send_followup_reaches_a_running_task(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="research the moon")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call("send_followup", {"task_id": 1, "message": "and the tides"})

    assert result == {"task_id": 1, "status": "running"}
    await tools.manager.wait_for(1, WAIT)
    assert tools.runner.sessions[1].prompts == ["Follow-up from the user:\n- and the tides"]


async def test_send_followup_needs_something_to_say(tools):
    await tools.dispatch()

    result = await tools.call("send_followup", {"task_id": 1, "message": ""})

    assert "message" in result["error"]


async def test_send_followup_to_a_cancelled_task_explains_itself(tools):
    await tools.dispatch()
    await tools.manager.cancel(1)

    result = await tools.call("send_followup", {"task_id": 1, "message": "one more thing"})

    assert result == {"error": "task is cancelled"}


async def test_cancel_task_stops_a_running_task(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="never mind")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call("cancel_task", {"task_id": 1})

    assert result == {"task_id": 1, "status": "cancelled"}


# --- send_followup / cancel_task: the PIN gate (spec §3.3) -----------------


@pytest.mark.parametrize("tool", ["send_followup", "cancel_task"])
async def test_an_unauthorized_phone_caller_cannot_touch_a_destructive_task(make_tools, tool):
    """`list_tasks` shows every task; reaching into a coding one still needs the PIN."""
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), pin="424242")
    await tools.dispatch("add a README", project="jarvis")

    result = await tools.call(
        tool,
        {"task_id": 1, "message": "and push it"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "pin_required"
    assert (await tools.manager.get(1)).status is not TaskStatus.CANCELLED


@pytest.mark.parametrize("tool", ["send_followup", "cancel_task"])
async def test_an_authorized_phone_caller_may_touch_a_destructive_task(make_tools, tool):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), pin="424242")
    await tools.dispatch("add a README", project="jarvis")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call(
        tool,
        {"task_id": 1, "message": "and push it"},
        channel="phone",
        caller="+15555555555",
        authorized=True,
    )

    assert result["task_id"] == 1
    assert "status" in result


@pytest.mark.parametrize("tool", ["send_followup", "cancel_task"])
async def test_reaching_into_a_running_task_needs_the_pin_too(make_tools, tool):
    """Following up opens the same bypassPermissions subagent that dispatching does."""
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), pin="424242")
    await tools.dispatch("how tall is Everest")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call(
        tool,
        {"task_id": 1, "message": "and the tides"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "pin_required"


@pytest.mark.parametrize("tool", ["send_followup", "cancel_task"])
async def test_a_local_session_needs_no_pin_to_follow_up_or_cancel(make_tools, tool):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), pin="424242")
    await tools.dispatch("add a README", project="jarvis")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call(
        tool, {"task_id": 1, "message": "and push it"}, channel="local", authorized=False
    )

    assert result["task_id"] == 1


# --- list_projects ---------------------------------------------------------


async def test_list_projects_returns_names_only(tools):
    assert await tools.call("list_projects", {}) == {"projects": ["jarvis"]}


# --- request_callback ------------------------------------------------------


async def test_request_callback_uses_an_explicit_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call(
        "request_callback", {"task_id": 1, "number": "+15557777777"}, channel="local"
    )

    assert result["task_id"] == 1
    task = await tools.manager.get(1)
    assert (task.callback_requested, task.callback_number) == (True, "+15557777777")


async def test_request_callback_falls_back_to_the_caller(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    await tools.call("request_callback", {"task_id": 1}, channel="phone", caller="+15555555555")

    task = await tools.manager.get(1)
    assert task.callback_number == "+15555555555"


async def test_request_callback_falls_back_to_the_owner_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), owner_number_explicit="+15556666666")
    await tools.dispatch(description="a long one")

    await tools.call("request_callback", {"task_id": 1}, channel="local", caller=None)

    task = await tools.manager.get(1)
    assert task.callback_number == "+15556666666"


async def test_request_callback_without_any_number_asks_for_one(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call("request_callback", {"task_id": 1}, channel="local", caller=None)

    assert "number" in result["error"]
    assert (await tools.manager.get(1)).callback_requested is False


async def test_request_callback_rejects_something_that_is_not_a_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call("request_callback", {"task_id": 1, "number": "call the office"})

    assert "phone number" in result["error"]
    assert (await tools.manager.get(1)).callback_requested is False


async def test_request_callback_on_a_finished_task_just_reports_it(tools):
    await tools.dispatch()
    await tools.manager.wait_for(1, WAIT)

    result = await tools.call("request_callback", {"task_id": 1, "number": "+15557777777"})

    assert result["status"] == "already_finished"
    assert result["summary"] == "I finished the task."
    assert (await tools.manager.get(1)).callback_requested is False


async def test_an_unauthorized_phone_caller_cannot_be_called_back_anywhere(make_tools):
    """Dialling out is the one tool an unauthorized caller could aim at a stranger."""
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call(
        "request_callback",
        {"task_id": 1, "number": "+15559999999"},
        channel="phone",
        caller="+15555555555",
        authorized=False,
    )

    assert result["status"] == "refused"
    assert (await tools.manager.get(1)).callback_requested is False


async def test_an_unauthorized_phone_caller_may_ask_for_their_own_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), allowed_callers=["+15556666666"])
    await tools.dispatch(description="a long one")

    for number in (None, "+15555555555", "+15556666666"):
        result = await tools.call(
            "request_callback",
            {"task_id": 1} if number is None else {"task_id": 1, "number": number},
            channel="phone",
            caller="+15555555555",
            authorized=False,
        )
        assert result["status"] == "callback_requested", number


async def test_an_authorized_phone_caller_may_name_any_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call(
        "request_callback",
        {"task_id": 1, "number": "+15559999999"},
        channel="phone",
        caller="+15555555555",
        authorized=True,
    )

    assert result["status"] == "callback_requested"


async def test_a_requested_callback_is_one_fact_the_model_states_once(make_tools):
    """Real calls had "let me set that up" and then "all set" around a millisecond call.

    Arranging a call-back and having arranged it are the same fact, so the result carries
    the instruction to say it once and stop, and the tool's own description tells the
    model not to announce it beforehand.
    """
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    task = await tools.dispatch("something slow")

    result = await tools.call(
        "request_callback", {"task_id": task.id}, channel="phone", caller="+15555555555"
    )

    assert result["message"] == CALLBACK_SET_MESSAGE
    assert "once" in CALLBACK_SET_MESSAGE
    schema = next(s for s in tools.registry.schemas() if s["name"] == "request_callback")
    assert "do not say you are setting it up first" in schema["description"]


async def test_a_callback_on_a_finished_task_is_answered_not_arranged(tools):
    """Nothing is going to land, so the model has the result and should just say it."""
    task = await _finish(tools)

    result = await tools.call(
        "request_callback", {"task_id": task.id}, channel="phone", caller="+15555555555"
    )

    assert result["status"] == "already_finished"
    assert "mark_reported" in result["message"]


async def test_a_running_task_promises_nothing_about_what_the_answer_will_say(make_tools):
    """"It'll include a short summary and where to find the deck" was invented on a call."""
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))

    result = await tools.call("dispatch_task", {"description": "read all of Wikipedia"})

    assert result["message"] == STILL_RUNNING_MESSAGE
    assert "you do not know yet" in STILL_RUNNING_MESSAGE


# --- submit_pin / end_session ---------------------------------------------


async def test_submit_pin_hands_the_digits_to_the_session(tools):
    tools.session.pin_result = {"status": "invalid", "attempts_left": 2}

    result = await tools.call("submit_pin", {"pin": "123456"})

    assert result["status"] == "invalid"
    assert result["attempts_left"] == 2
    assert result["message"] == PIN_INVALID_MESSAGE
    assert tools.session.pins == ["123456"]


async def test_an_accepted_pin_is_not_something_to_announce(tools):
    """Four spoken turns for one PIN is what this wording exists to stop."""
    tools.session.pin_result = {"status": "authorized"}

    result = await tools.call("submit_pin", {"pin": "123456"})

    assert result["status"] == "authorized"
    assert result["message"] == PIN_OK_MESSAGE
    assert "Say nothing about the PIN" in result["message"]


async def test_the_pin_ask_itself_is_one_sentence_with_no_preamble(make_tools):
    """The refusal a gated tool returns is where the asking gets long-winded."""
    tools = make_tools(pin="123456")

    result = await tools.call(
        "dispatch_task", {"description": "anything"}, channel="phone", authorized=False
    )

    assert result["status"] == "pin_required"
    assert "one short sentence" in result["message"]
    assert "do not announce that you are checking it" in result["message"]


async def test_a_message_the_session_wrote_wins_over_the_stock_wording(tools):
    """Wording here may never talk over something the session had a reason to say."""
    tools.session.pin_result = {"status": "invalid", "message": "session knows better"}

    result = await tools.call("submit_pin", {"pin": "000000"})

    assert result["message"] == "session knows better"


async def test_end_session_asks_the_session_to_end(tools):
    """The goodbye comes *before* this call, so the result must not ask for another."""
    result = await tools.call("end_session", {})

    assert result["status"] == "ending"
    assert "goodbye" not in result["message"].lower()
    assert tools.session.ends == ["user"]


# --- web_search ------------------------------------------------------------


class FakeSearcher:
    """A `WebSearcher` that answers from a script."""

    def __init__(self, answer: str = "It is seventeen degrees and clear.") -> None:
        self.answer = answer
        self.queries: list[str] = []

    async def search(self, query: str) -> str:
        self.queries.append(query)
        return self.answer


async def test_web_search_hands_back_a_spoken_answer(make_tools):
    searcher = FakeSearcher()
    tools = make_tools(searcher=searcher)

    result = await tools.call("web_search", {"query": "weather tomorrow"})

    assert result == {"answer": "It is seventeen degrees and clear."}
    assert searcher.queries == ["weather tomorrow"]


async def test_web_search_needs_a_query(make_tools):
    tools = make_tools(searcher=FakeSearcher())

    result = await tools.call("web_search", {})

    assert "query is required" in result["error"]


async def test_an_empty_search_result_is_an_error_the_model_can_speak_to(make_tools):
    tools = make_tools(searcher=FakeSearcher(answer=""))

    result = await tools.call("web_search", {"query": "something obscure"})

    assert "empty" in result["error"]


async def test_there_is_no_web_search_tool_without_a_searcher(tools):
    """A process wired without one simply does not offer it."""
    assert "web_search" not in {schema["name"] for schema in tools.registry.schemas()}


# --- check_billing ---------------------------------------------------------


class FakeBilling:
    """A `BillingReader` that answers from a script, or raises the given `BillingError`."""

    provider = "openai"

    def __init__(self, report: BillingReport | BillingError) -> None:
        self.report = report
        self.asked = 0

    async def month_to_date(self, *, now=None) -> BillingReport:
        self.asked += 1
        if isinstance(self.report, BillingError):
            raise self.report
        return self.report


def billing_factory(answer, *, for_provider: BillingError | None = None):
    """A `BillingFactory` recording which provider the model asked for."""
    asked: list[str | None] = []

    def factory(provider: str | None):
        asked.append(provider)
        if provider is not None and for_provider is not None:
            raise for_provider
        return FakeBilling(answer)

    factory.asked = asked  # type: ignore[attr-defined]
    return factory


def a_report(**overrides) -> BillingReport:
    defaults = dict(
        provider="openai",
        currency="USD",
        spend=31.0,
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
        usage={"input_tokens": 5, "requests": 2},
    )
    return BillingReport(**{**defaults, **overrides})


async def test_check_billing_hands_back_the_month_with_a_sentence_to_say(make_tools):
    tools = make_tools(billing=billing_factory(a_report()))

    result = await tools.call("check_billing", {})

    assert result["status"] == "ok"
    assert result["provider"] == "openai"
    assert result["spend_to_date"] == 31.0
    assert result["period_start"] == "2026-08-01T00:00:00+00:00"
    assert result["as_of"] == "2026-08-11T00:00:00+00:00"
    assert result["estimate"] is True
    assert result["usage"] == {"input_tokens": 5, "requests": 2}
    assert "OpenAI so far this month" in result["spoken"]


async def test_the_model_may_name_the_provider_and_the_default_is_none(make_tools):
    factory = billing_factory(a_report())
    tools = make_tools(billing=factory)

    await tools.call("check_billing", {})
    await tools.call("check_billing", {"provider": "Anthropic"})

    assert factory.asked == [None, "anthropic"]


@pytest.mark.parametrize(
    "code",
    ["not_configured", "auth", "rate_limited", "unavailable"],
)
async def test_every_billing_failure_is_a_status_with_a_sentence(make_tools, code):
    """Never a raised exception and never an `error` the model has to invent words for."""
    tools = make_tools(billing=billing_factory(BillingError(code, "HTTP 401 sk-admin-SECRET")))

    result = await tools.call("check_billing", {})

    assert result["status"] == code
    assert result["message"]
    assert "sk-admin" not in str(result)


async def test_a_provider_with_no_credential_is_refused_at_the_factory(make_tools):
    tools = make_tools(
        billing=billing_factory(
            a_report(), for_provider=BillingError("not_configured", "ANTHROPIC_ADMIN_KEY is unset")
        )
    )

    result = await tools.call("check_billing", {"provider": "anthropic"})

    assert result["status"] == "not_configured"
    assert "ANTHROPIC_ADMIN_KEY" not in result["message"]


async def test_check_billing_is_not_pin_gated_because_it_only_reads(make_tools):
    """An unauthorized phone caller asking what the bill is changes nothing by asking."""
    tools = make_tools(billing=billing_factory(a_report()))

    result = await tools.call("check_billing", {}, channel="phone", authorized=False)

    assert result["status"] == "ok"


async def test_there_is_no_check_billing_tool_without_a_billing_factory(tools):
    assert "check_billing" not in {schema["name"] for schema in tools.registry.schemas()}


async def test_the_billing_schema_offers_exactly_the_two_providers(make_tools):
    tools = make_tools(billing=billing_factory(a_report()))

    schema = next(s for s in tools.registry.schemas() if s["name"] == "check_billing")

    assert schema["parameters"]["properties"]["provider"]["enum"] == ["openai", "anthropic"]
    assert schema["parameters"]["required"] == []


# --- cluster_stats ---------------------------------------------------------


class FakeClusters:
    """A `ClusterQuerier` answering from a script: a report, or a `ClusterError` to raise."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def known(self) -> list[str]:
        return list(self.answers)

    async def stats(self, cluster: str):
        self.asked.append(cluster)
        answer = self.answers.get(cluster)
        if answer is None:
            raise ClusterError("unknown_cluster", f"no cluster {cluster!r}")
        if isinstance(answer, ClusterError):
            raise answer
        return answer


def a_cluster_report(name: str = "alpha", **overrides) -> ClusterReport:
    defaults = dict(
        cluster=name,
        spoken_name=name.capitalize(),
        partition="shared",
        gpus=GpuCounts(total=30, busy=4, free=26, nodes=3),
        jobs=MyJobs(running=1, gpus=2, soonest_end_s=3600, ids=[100042]),
        queue_competing=0,
        queue_pending=1,
    )
    return ClusterReport(**{**defaults, **overrides})


def both_clusters(**overrides):
    answers = {"alpha": a_cluster_report("alpha"), "beta": a_cluster_report("beta")}
    answers.update(overrides)
    return FakeClusters(answers)


async def test_cluster_stats_asks_every_cluster_when_he_names_none(make_tools):
    clusters = both_clusters()
    tools = make_tools(cluster=clusters)

    result = await tools.call("cluster_stats", {})

    assert clusters.asked == ["alpha", "beta"]
    assert result["status"] == "ok"
    assert [entry["cluster"] for entry in result["clusters"]] == ["alpha", "beta"]
    assert "Alpha" in result["spoken"] and "Beta" in result["spoken"]


async def test_cluster_stats_answers_for_one_cluster_when_he_names_it(make_tools):
    clusters = both_clusters()
    tools = make_tools(cluster=clusters)

    result = await tools.call("cluster_stats", {"cluster": "Beta"})

    assert clusters.asked == ["beta"]
    assert [entry["cluster"] for entry in result["clusters"]] == ["beta"]


async def test_the_payload_carries_counts_and_job_ids_but_never_a_job_name(make_tools):
    """Anyone past the caller allowlist learns how busy a machine is, and nothing else."""
    tools = make_tools(cluster=both_clusters())

    result = await tools.call("cluster_stats", {"cluster": "alpha"})

    entry = result["clusters"][0]
    assert entry["gpus_free"] == 26 and entry["gpus_total"] == 30
    assert entry["my_job_ids"] == [100042]
    assert "name" not in entry and "cwd" not in entry


async def test_one_cluster_failing_never_costs_the_other(make_tools):
    tools = make_tools(cluster=both_clusters(beta=ClusterError("auth_expired", "login expired")))

    result = await tools.call("cluster_stats", {})

    assert result["status"] == "ok"
    assert [entry["cluster"] for entry in result["clusters"]] == ["alpha"]
    assert result["unavailable"] == [
        {"cluster": "beta", "status": "auth_expired", "message": MESSAGES["auth_expired"]}
    ]


@pytest.mark.parametrize(
    "code", ["not_configured", "unknown_cluster", "auth_expired", "timeout", "unavailable"]
)
async def test_every_cluster_failure_is_a_status_with_a_sentence(make_tools, code):
    """Never a raised exception, and never a path or a host for the model to read out."""
    detail = "no guard at /home/someone/.claude/skills/x/cluster_ssh.sh"
    answers = {name: ClusterError(code, detail) for name in ("alpha", "beta")}
    tools = make_tools(cluster=FakeClusters(answers))

    result = await tools.call("cluster_stats", {})

    assert result["status"] == code
    assert result["message"] == MESSAGES[code]
    assert ".claude" not in str(result)


async def test_a_cluster_it_does_not_know_is_refused_rather_than_looked_up(make_tools):
    clusters = both_clusters()
    tools = make_tools(cluster=clusters)

    result = await tools.call("cluster_stats", {"cluster": "gamma"})

    assert result["status"] == "unknown_cluster"
    assert clusters.asked == ["gamma"]  # asked the querier, which refused it by name


async def test_cluster_stats_is_not_pin_gated_because_it_only_reads(make_tools):
    """An unauthorized phone caller asking how busy a machine is changes nothing by asking."""
    tools = make_tools(cluster=both_clusters())

    result = await tools.call("cluster_stats", {}, channel="phone", authorized=False)

    assert result["status"] == "ok"


async def test_there_is_no_cluster_stats_tool_without_a_querier(tools):
    assert "cluster_stats" not in {schema["name"] for schema in tools.registry.schemas()}


async def test_the_cluster_schema_offers_exactly_the_configured_clusters(make_tools):
    """The names come from the querier, which got them from settings — none are built in."""
    tools = make_tools(cluster=both_clusters())

    schema = next(s for s in tools.registry.schemas() if s["name"] == "cluster_stats")

    assert schema["parameters"]["properties"]["cluster"]["enum"] == ["alpha", "beta", "all"]
    assert schema["parameters"]["required"] == []
    assert "alpha and beta" in schema["description"]


async def test_one_configured_cluster_is_described_as_one(make_tools):
    tools = make_tools(cluster=FakeClusters({"alpha": a_cluster_report("alpha")}))

    schema = next(s for s in tools.registry.schemas() if s["name"] == "cluster_stats")

    assert "the Slurm cluster alpha is doing" in schema["description"]
    assert schema["parameters"]["properties"]["cluster"]["enum"] == ["alpha", "all"]


async def test_a_querier_that_knows_no_cluster_offers_no_tool(make_tools):
    tools = make_tools(cluster=FakeClusters({}))

    assert "cluster_stats" not in {schema["name"] for schema in tools.registry.schemas()}


# --- send_to_slack ---------------------------------------------------------


class FakeSlack:
    """A `SlackSender` that records what it was asked to send."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


async def test_send_to_slack_sends_the_message(make_tools):
    slack = FakeSlack()
    tools = make_tools(slack=slack)

    result = await tools.call("send_to_slack", {"message": "task 3 is done"})

    assert result == {"status": "sent"}
    assert slack.sent == ["task 3 is done"]


async def test_send_to_slack_needs_a_message(make_tools):
    tools = make_tools(slack=FakeSlack())

    assert "message is required" in (await tools.call("send_to_slack", {}))["error"]


async def test_a_refused_slack_message_comes_back_as_an_error(make_tools):
    tools = make_tools(slack=FakeSlack(ok=False))

    result = await tools.call("send_to_slack", {"message": "anything"})

    assert "did not go through" in result["error"]


async def test_there_is_no_slack_tool_without_credentials(tools):
    assert "send_to_slack" not in {schema["name"] for schema in tools.registry.schemas()}


def _slack_description(tools) -> str:
    """The `send_to_slack` description, as the model is shown it, on one line."""
    schema = next(s for s in tools.registry.schemas() if s["name"] == "send_to_slack")
    return " ".join(schema["description"].split())


async def test_the_slack_tool_tells_the_model_to_wait_to_be_asked(make_tools):
    """The description is half the guardrail: the model reads it on every turn."""
    description = _slack_description(make_tools(slack=FakeSlack()))

    assert "Only call it when he has explicitly asked" in description
    assert "Never call it unasked" in description


async def test_the_slack_tool_does_not_invite_a_written_copy_of_the_answer(make_tools):
    """Repeating in writing what was just said out loud is the commonest unasked send."""
    description = _slack_description(make_tools(slack=FakeSlack()))

    assert "never to repeat in writing something you have already said" in description


async def test_the_slack_tool_still_sends_when_it_is_called(make_tools):
    """The rule is about when the model calls it, not about crippling the tool itself."""
    slack = FakeSlack()
    tools = make_tools(slack=slack)

    assert await tools.call("send_to_slack", {"message": "the link he asked for"}) == {
        "status": "sent"
    }
    assert slack.sent == ["the link he asked for"]


# --- restart_service -------------------------------------------------------


class FakeRestarter:
    """The slice of `RestartCoordinator` the tool touches."""

    def __init__(self, result: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.result = result or {"status": "deferred", "message": "say so"}

    async def request(self, **kwargs) -> dict:
        self.requests.append(kwargs)
        return self.result


def test_restart_service_is_not_offered_without_a_coordinator(tools):
    assert "restart_service" not in {schema["name"] for schema in tools.registry.schemas()}


def test_restart_service_is_offered_with_one(make_tools):
    harness = make_tools(restarter=FakeRestarter())

    assert "restart_service" in {schema["name"] for schema in harness.registry.schemas()}


async def test_restart_service_hands_the_reason_and_the_caller_over(make_tools):
    restarter = FakeRestarter()
    harness = make_tools(restarter=restarter)

    result = await harness.call(
        "restart_service", {"reason": "new code"}, channel="phone", caller="+15551234567"
    )

    assert result["status"] == "deferred"
    assert restarter.requests == [
        {
            "reason": "new code",
            "number": "+15551234567",
            "origin_channel": "phone",
            "origin_session_id": harness.session.session_id,
            "task_id": None,
        }
    ]


async def test_restart_service_names_the_task_whose_change_it_is_loading(make_tools):
    """The id is what turns the confirmation into "your change is running"."""
    restarter = FakeRestarter()
    harness = make_tools(restarter=restarter)

    await harness.call("restart_service", {"reason": "new tool", "task_id": 42}, channel="phone")

    assert restarter.requests[0]["task_id"] == 42


async def test_a_made_up_task_id_on_a_restart_is_dropped(make_tools):
    """Better an unlinked restart than one that claims to be loading a task that is not."""
    restarter = FakeRestarter()
    harness = make_tools(restarter=restarter)

    await harness.call("restart_service", {"task_id": "the last one"}, channel="phone")

    assert restarter.requests[0]["task_id"] is None


async def test_a_local_restart_names_no_number(make_tools):
    """The wake word has no caller: the coordinator falls back to the owner's number."""
    restarter = FakeRestarter()
    harness = make_tools(restarter=restarter)

    await harness.call("restart_service", {}, channel="local", caller=None)

    assert restarter.requests[0]["number"] is None


async def test_restart_service_needs_the_pin_on_the_phone(make_tools):
    """Taking the phone channel off the air is at least as serious as dispatching work."""
    restarter = FakeRestarter()
    harness = make_tools(restarter=restarter, pin="654321")

    result = await harness.call(
        "restart_service", {}, channel="phone", caller="+15551234567", authorized=False
    )

    assert result["status"] == "pin_required"
    assert restarter.requests == []


# --- mark_reported ---------------------------------------------------------


async def _finish(harness, description: str = "rewrite the ingest script"):
    """Dispatch a task and wait for it to land, so it is something to report."""
    task = await harness.dispatch(description)
    await wait_for_status(harness, task.id, TaskStatus.DONE)
    return task


async def test_mark_reported_records_the_ids_the_model_said_out_loud(tools):
    task = await _finish(tools)

    result = await tools.call("mark_reported", {"task_ids": [task.id]})

    assert result["reported"] == [task.id]
    assert (await tools.manager.get(task.id)).reported_at is not None


async def test_mark_reported_is_silent_because_he_has_already_heard_the_result(tools):
    """A turn generated over its answer is the result said a second time.

    Session 54d90826 is the case: the model greeted him, gave the result, called
    `mark_reported`, and the forced response made it say the whole greeting again.
    """
    assert tools.registry.is_silent("mark_reported")

    result = await tools.call("mark_reported", {"task_ids": [(await _finish(tools)).id]})

    assert result["message"] == REPORTED_MESSAGE


async def test_end_session_is_silent_because_the_goodbye_came_first(tools):
    """Nothing said after `end_session` is heard, so nothing should be generated."""
    assert tools.registry.is_silent("end_session")


async def test_the_tools_he_is_waiting_on_still_get_their_turn(tools):
    """Silence is for bookkeeping only: an answer he asked for has to be spoken."""
    for name in ("dispatch_task", "list_tasks", "get_task_result", "request_callback"):
        assert not tools.registry.is_silent(name), name


async def test_a_reported_task_stops_coming_back_in_the_briefing(tools):
    task = await _finish(tools)
    assert [one.id for one in await tools.manager.unreported()] == [task.id]

    await tools.call("mark_reported", {"task_ids": [task.id]})

    assert await tools.manager.unreported() == []


async def test_mark_reported_shrugs_off_ids_that_do_not_exist(tools):
    """The model is guessing at numbers from a spoken conversation."""
    task = await _finish(tools)

    result = await tools.call("mark_reported", {"task_ids": [task.id, 999]})

    assert result["reported"] == [task.id]


async def test_mark_reported_needs_at_least_one_usable_id(tools):
    assert "error" in await tools.call("mark_reported", {"task_ids": []})
    assert "error" in await tools.call("mark_reported", {})
    assert "error" in await tools.call("mark_reported", {"task_ids": ["nonsense"]})


async def test_mark_reported_accepts_a_bare_number_as_well_as_a_list(tools):
    task = await _finish(tools)

    result = await tools.call("mark_reported", {"task_ids": task.id})
    assert result["reported"] == [task.id]


async def test_mark_reported_needs_no_pin_because_it_starts_no_work(tools):
    task = await _finish(tools)

    result = await tools.call(
        "mark_reported", {"task_ids": [task.id]}, channel="phone", authorized=False
    )

    assert result["reported"] == [task.id]


# --- recall ----------------------------------------------------------------


class FakeRecaller:
    """Records the query and replays scripted hits."""

    def __init__(self, hits=None) -> None:
        self.hits = list(hits or ())
        self.queries: list[tuple[str, int]] = []

    async def recall(self, query: str, *, limit: int):
        self.queries.append((query, limit))
        return self.hits


def test_recall_is_not_offered_when_there_is_nothing_to_search(tools):
    assert "recall" not in {schema["name"] for schema in tools.registry.schemas()}


async def test_recall_hands_back_what_it_found(make_tools):
    recaller = FakeRecaller([Hit("call", "22 August", "we said poll every fifteen minutes")])
    harness = make_tools(recaller=recaller)

    result = await harness.call("recall", {"query": "orchard sync"})

    assert recaller.queries == [("orchard sync", DEFAULT_RECALL_LIMIT)]
    assert result["hits"] == [
        {"source": "call", "text": "we said poll every fifteen minutes", "when": "22 August"}
    ]


async def test_recall_that_finds_nothing_tells_the_model_to_say_so(make_tools):
    harness = make_tools(recaller=FakeRecaller([]))

    result = await harness.call("recall", {"query": "submarine"})

    assert result["hits"] == []
    assert "nothing on record" in result["message"]


async def test_recall_needs_something_to_look_for(make_tools):
    harness = make_tools(recaller=FakeRecaller([]))

    assert "error" in await harness.call("recall", {"query": "  "})


async def test_the_recall_limit_is_clamped_to_something_speakable(make_tools):
    recaller = FakeRecaller([])
    harness = make_tools(recaller=recaller)

    await harness.call("recall", {"query": "orchard", "limit": 99})

    assert recaller.queries[0][1] == MAX_RECALL_LIMIT
