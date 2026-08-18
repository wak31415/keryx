"""Tests for the tools the voice model calls (spec §3.2 `tools/registry.py`, §3.3).

Everything runs against a real `TaskManager` with a `FakeAgentRunner` and an in-memory
`TaskStore`, so the dicts asserted on here are the dicts the model would really see.
The session is a `StubSession`: the tools only need the duck-typed slice of it, and the
real `VoiceSession` PIN machinery has its own module (`tests/test_session_pin.py`).
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.tasks.agent_runner import FakeAgentRunner, RunResult
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.models import TaskStatus
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolContext, ToolRegistry
from jarvis.tools.builtin import register_builtin_tools

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

    async def call(self, name: str, arguments: dict | None = None, **overrides) -> dict:
        """Invoke a tool through the registry, exactly as the session would."""
        session = self.session
        for name_, value in overrides.items():
            setattr(session, name_, value)
        ctx = ToolContext(session=session, channel=session.channel, caller=session.caller)
        return await self.registry.call(name, arguments or {}, ctx)

    async def dispatch(self, kind: str = "chat", description: str = "how tall is Everest", **kw):
        return await self.manager.dispatch(
            kind, description, origin_channel="local", origin_caller=None, **kw
        )


@pytest.fixture
async def make_tools(tmp_path):
    """Factory for a registry wired to a real manager; every manager is shut down after."""
    built: list[Harness] = []
    repo = tmp_path / "repo"
    repo.mkdir()

    def _build(runner: FakeAgentRunner | None = None, **overrides) -> Harness:
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
        register_builtin_tools(registry, manager=manager, settings=settings)
        harness = Harness(registry, manager, settings, store, agent_runner, StubSession())
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


def test_the_dispatch_schema_spells_out_the_kinds_and_the_wait(tools):
    schema = next(s for s in tools.registry.schemas() if s["name"] == "dispatch_task")

    properties = schema["parameters"]["properties"]
    assert properties["kind"]["enum"] == ["chat", "research", "coding", "cowork"]
    assert schema["parameters"]["required"] == ["kind", "description"]
    assert properties["wait_seconds"]["maximum"] == 25
    for kind in ("chat", "research", "coding", "cowork"):
        assert kind in properties["kind"]["description"]


# --- dispatch_task: the PIN gate (spec §3.3) -------------------------------


async def test_local_sessions_dispatch_destructive_work_without_a_pin(make_tools):
    tools = make_tools(pin="4242")

    result = await tools.call(
        "dispatch_task",
        {"kind": "coding", "description": "add a README", "project": "jarvis"},
        channel="local",
        authorized=False,
    )

    assert result["task_id"] == 1
    assert "status" in result


async def test_an_unauthorized_phone_caller_is_asked_for_the_pin(make_tools):
    tools = make_tools(pin="4242")

    result = await tools.call(
        "dispatch_task",
        {"kind": "coding", "description": "add a README", "project": "jarvis"},
        channel="phone",
        caller="+491555555555",
        authorized=False,
    )

    assert result["status"] == "pin_required"
    assert "PIN" in result["message"]
    assert await tools.manager.list() == []  # nothing was dispatched


async def test_an_authorized_phone_caller_dispatches_destructive_work(make_tools):
    tools = make_tools(pin="4242")

    result = await tools.call(
        "dispatch_task",
        {"kind": "cowork", "description": "draft a reply to Anna"},
        channel="phone",
        caller="+491555555555",
        authorized=True,
    )

    assert result["task_id"] == 1


async def test_a_phone_caller_needs_no_pin_for_harmless_kinds(make_tools):
    tools = make_tools(pin="4242")

    result = await tools.call(
        "dispatch_task",
        {"kind": "research", "description": "what happened at CES"},
        channel="phone",
        caller="+491555555555",
        authorized=False,
    )

    assert result["task_id"] == 1


async def test_destructive_work_is_refused_when_no_pin_is_configured(make_tools):
    tools = make_tools()  # settings.pin is None

    result = await tools.call(
        "dispatch_task",
        {"kind": "coding", "description": "add a README", "project": "jarvis"},
        channel="phone",
        caller="+491555555555",
        authorized=False,
    )

    assert result["status"] == "refused"
    assert "none is configured" in result["message"]


# --- dispatch_task: dispatching -------------------------------------------


async def test_an_unknown_kind_is_reported_before_anything_is_dispatched(tools):
    result = await tools.call("dispatch_task", {"kind": "hacking", "description": "the mainframe"})

    assert "unknown kind" in result["error"]
    assert await tools.manager.list() == []


async def test_waiting_returns_the_summary_inline(tools):
    result = await tools.call(
        "dispatch_task",
        {"kind": "chat", "description": "how tall is Everest", "wait_seconds": 20},
    )

    assert result == {"task_id": 1, "status": "done", "summary": "I finished the task."}


async def test_a_long_task_comes_back_running_with_a_promise(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))

    result = await tools.call(
        "dispatch_task", {"kind": "chat", "description": "read all of Wikipedia"}
    )

    assert result["task_id"] == 1
    assert result["status"] in {"queued", "running"}
    assert "summary" not in result
    assert result["message"] == "still running; you will be told when it finishes"


async def test_the_wait_is_clamped_to_the_configured_maximum(tools):
    seen: list[float] = []
    original = tools.manager.wait_for

    async def spy(task_id: int, timeout: float):
        seen.append(timeout)
        return await original(task_id, timeout)

    tools.manager.wait_for = spy

    await tools.call(
        "dispatch_task",
        {"kind": "chat", "description": "how tall is Everest", "wait_seconds": 999},
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
        {"kind": "chat", "description": "how tall is Everest", "wait_seconds": -5},
    )

    assert called is False
    assert result["task_id"] == 1


async def test_an_unknown_project_comes_back_with_the_candidates(tools):
    result = await tools.call(
        "dispatch_task",
        {"kind": "coding", "description": "add a README", "project": "wat"},
    )

    assert "unknown project" in result["error"]
    assert result["candidates"] == ["jarvis"]


async def test_a_coding_task_without_a_project_is_an_error(tools):
    result = await tools.call("dispatch_task", {"kind": "coding", "description": "add a README"})

    assert result == {"error": "coding tasks need a project"}


async def test_the_daily_cap_comes_back_as_an_error(make_tools):
    tools = make_tools(daily_task_cap=0)

    result = await tools.call("dispatch_task", {"kind": "chat", "description": "anything"})

    assert "daily task cap" in result["error"]


async def test_a_missing_description_is_refused(tools):
    result = await tools.call("dispatch_task", {"kind": "chat", "description": "  "})

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
    assert result["tasks"][0]["kind"] == "chat"


async def test_list_tasks_rejects_a_status_it_does_not_know(tools):
    result = await tools.call("list_tasks", {"status": "sideways"})

    assert "unknown status" in result["error"]


# --- get_task_status / get_task_result ------------------------------------


async def test_get_task_status_describes_one_task(tools):
    await tools.dispatch(description="how tall is Everest")
    await tools.manager.wait_for(1, WAIT)

    result = await tools.call("get_task_status", {"task_id": 1})

    assert result["task_id"] == 1
    assert result["status"] == "done"
    assert result["kind"] == "chat"
    assert result["description"] == "how tall is Everest"
    assert result["summary"] == "I finished the task."
    assert result["error"] is None
    assert result["line"].startswith("task 1 (chat, done)")


async def test_get_task_status_for_a_task_that_does_not_exist(tools):
    assert await tools.call("get_task_status", {"task_id": 99}) == {"error": "no task 99"}


async def test_a_task_id_that_is_not_a_number_is_an_error(tools):
    result = await tools.call("get_task_status", {"task_id": "the blue one"})

    assert "task_id" in result["error"]


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


async def test_get_task_result_for_a_task_that_does_not_exist(tools):
    assert await tools.call("get_task_result", {"task_id": 7}) == {"error": "no task 7"}


# --- send_followup / cancel_task ------------------------------------------


async def test_send_followup_reaches_a_running_task(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="research the moon")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call("send_followup", {"task_id": 1, "message": "and the tides"})

    assert result == {"task_id": 1, "status": "running"}
    assert tools.runner.sessions[0].sent == ["and the tides"]


async def test_send_followup_needs_something_to_say(tools):
    await tools.dispatch()

    result = await tools.call("send_followup", {"task_id": 1, "message": ""})

    assert "message" in result["error"]


async def test_send_followup_to_a_cancelled_task_explains_itself(tools):
    await tools.dispatch()
    await tools.manager.cancel(1)

    result = await tools.call("send_followup", {"task_id": 1, "message": "one more thing"})

    assert result == {"error": "task is cancelled"}


async def test_send_followup_to_a_task_that_does_not_exist(tools):
    assert await tools.call("send_followup", {"task_id": 4, "message": "hi"}) == {
        "error": "no task 4"
    }


async def test_cancel_task_stops_a_running_task(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="never mind")
    await wait_for_status(tools, 1, TaskStatus.RUNNING)

    result = await tools.call("cancel_task", {"task_id": 1})

    assert result == {"task_id": 1, "status": "cancelled"}


async def test_cancel_task_for_a_task_that_does_not_exist(tools):
    assert await tools.call("cancel_task", {"task_id": 12}) == {"error": "no task 12"}


# --- list_projects ---------------------------------------------------------


async def test_list_projects_returns_names_only(tools):
    assert await tools.call("list_projects", {}) == {"projects": ["jarvis"]}


# --- request_callback ------------------------------------------------------


async def test_request_callback_uses_an_explicit_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    result = await tools.call(
        "request_callback", {"task_id": 1, "number": "+491777777777"}, channel="local"
    )

    assert result["task_id"] == 1
    task = await tools.manager.get(1)
    assert (task.callback_requested, task.callback_number) == (True, "+491777777777")


async def test_request_callback_falls_back_to_the_caller(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW))
    await tools.dispatch(description="a long one")

    await tools.call("request_callback", {"task_id": 1}, channel="phone", caller="+491555555555")

    task = await tools.manager.get(1)
    assert task.callback_number == "+491555555555"


async def test_request_callback_falls_back_to_the_owner_number(make_tools):
    tools = make_tools(FakeAgentRunner(delay_s=SLOW), owner_number_explicit="+491666666666")
    await tools.dispatch(description="a long one")

    await tools.call("request_callback", {"task_id": 1}, channel="local", caller=None)

    task = await tools.manager.get(1)
    assert task.callback_number == "+491666666666"


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

    result = await tools.call("request_callback", {"task_id": 1, "number": "+491777777777"})

    assert result["status"] == "already_finished"
    assert result["summary"] == "I finished the task."
    assert (await tools.manager.get(1)).callback_requested is False


async def test_request_callback_for_a_task_that_does_not_exist(tools):
    assert await tools.call("request_callback", {"task_id": 3}) == {"error": "no task 3"}


# --- submit_pin / end_session ---------------------------------------------


async def test_submit_pin_hands_the_digits_to_the_session(tools):
    tools.session.pin_result = {"status": "invalid", "attempts_left": 2}

    result = await tools.call("submit_pin", {"pin": "1234"})

    assert result == {"status": "invalid", "attempts_left": 2}
    assert tools.session.pins == ["1234"]


async def test_end_session_asks_the_session_to_end(tools):
    result = await tools.call("end_session", {})

    assert result["status"] == "ending"
    assert tools.session.ends == ["user"]
