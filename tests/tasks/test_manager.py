"""Tests for jarvis.tasks.manager.

Every task runs against `FakeAgentRunner`, so no Agent SDK process is ever started.
Ordering is made deterministic with the fake's `delay_s` plus bounded polling helpers —
no test may wait longer than `WAIT` seconds for anything.
"""

import asyncio
import logging
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis.agents.router import RoutingAgentRunner
from jarvis.config import Settings
from jarvis.events import EventBus, TaskCompleted, TaskFailed, TaskProgress, TaskStarted
from jarvis.tasks.agent_runner import FakeAgentRunner, RunResult, TokenUsage
from jarvis.tasks.manager import (
    CLOSE_TIMEOUT_S,
    AgentUnavailableError,
    TaskLimitError,
    TaskManager,
    UnknownProjectError,
    _duration,
    build_prompt,
    executor_workers,
    install_default_executor,
)
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

WAIT = 2.0  # upper bound (seconds) for every wait in this module
SLOW = 0.2  # fake agent turn long enough to observe a task while it is running


# --- harness -------------------------------------------------------------


class Recorder:
    """Subscribes to every event on a bus and keeps them in order."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[object] = []
        bus.subscribe(object, self.events.append)

    def of(self, *types: type) -> list[object]:
        return [event for event in self.events if isinstance(event, types)]


@dataclass
class Harness:
    manager: TaskManager
    runner: FakeAgentRunner
    events: Recorder
    settings: Settings

    def log_text(self, task_id: int) -> str:
        return (self.settings.data_dir / "tasks" / f"{task_id}.log").read_text()

    def report_text(self, task_id: int) -> str:
        return (self.settings.data_dir / "tasks" / f"{task_id}.md").read_text()


@pytest.fixture
async def store():
    task_store = TaskStore(":memory:")
    yield task_store
    await task_store.close()


@pytest.fixture
async def make_harness(store, settings):
    """Factory for a `TaskManager` + recorder; every manager is shut down afterwards."""
    built: list[TaskManager] = []

    def _build(runner=None, **overrides) -> Harness:
        config = settings.model_copy(update=overrides) if overrides else settings
        bus = EventBus()
        recorder = Recorder(bus)
        agent_runner = FakeAgentRunner() if runner is None else runner
        manager = TaskManager(store, agent_runner, bus, config)
        built.append(manager)
        return Harness(manager, agent_runner, recorder, config)

    yield _build

    for manager in built:
        await manager.shutdown()


async def _opened(harness: Harness, count: int) -> bool:
    return len(harness.runner.opened) >= count


async def dispatch(manager: TaskManager, description="how tall is Everest", **kw):
    kw.setdefault("origin_channel", "local")
    kw.setdefault("origin_caller", None)
    return await manager.dispatch(description, **kw)


async def wait_until(check, *, message: str, timeout: float = WAIT) -> None:
    """Poll `check` (an async predicate) until it is true, or fail after `timeout`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {message}")


async def wait_for_status(manager: TaskManager, task_id: int, status: TaskStatus) -> Task:
    async def _reached() -> bool:
        task = await manager.get(task_id)
        return task is not None and task.status is status

    await wait_until(_reached, message=f"task {task_id} to be {status}")
    return await manager.get(task_id)


def make_projects(tmp_path, *names) -> Path:
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).mkdir(exist_ok=True)
    return root


# --- prompts -------------------------------------------------------------


def test_build_prompt_carries_the_request_and_the_working_directory():
    task = Task(id=1, kind=TaskKind.AGENT, description="when is the next full moon")

    prompt = build_prompt(task)

    assert "when is the next full moon" in prompt
    assert "Working directory: the current directory" in prompt
    assert "(project" not in prompt


def test_build_prompt_names_the_repository_when_there_is_one():
    task = Task(
        id=1,
        kind=TaskKind.AGENT,
        description="add a README",
        project="jarvis",
        cwd="/repos/jarvis",
    )

    prompt = build_prompt(task)

    assert "Working directory: /repos/jarvis (project 'jarvis')" in prompt
    assert "add a README" in prompt


def test_build_prompt_offers_the_whole_machine_not_a_kind():
    """One prompt: the subagent decides whether the work wants a repo, mail or a skill."""
    prompt = build_prompt(Task(id=1, kind=TaskKind.AGENT, description="anything"))

    assert "Gmail" in prompt
    assert "skills" in prompt
    assert "subagents" in prompt
    assert "never push" in prompt


def test_build_prompt_leaves_braces_in_the_description_alone():
    task = Task(id=1, kind=TaskKind.AGENT, description="explain {cwd} and {project}")

    assert build_prompt(task).endswith("explain {cwd} and {project}")


# --- dispatch → done -----------------------------------------------------


async def test_dispatch_returns_a_queued_task_and_publishes_nothing(make_harness):
    harness = make_harness()

    task = await dispatch(
        harness.manager, description="hello", origin_channel="phone", origin_caller="+15550001111"
    )

    assert task.id is not None
    assert task.status is TaskStatus.QUEUED
    assert task.description == "hello"
    assert task.origin_channel == "phone"
    assert task.origin_caller == "+15550001111"
    assert harness.events.events == []


async def test_dispatch_runs_to_done_with_events_and_files(make_harness):
    harness = make_harness(FakeAgentRunner(progress=["[tool] Read foo.py", "thinking hard"]))

    task = await dispatch(harness.manager, description="how tall is Everest")
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.DONE
    assert finished.summary == "I finished the task."
    assert finished.error is None
    assert finished.claude_session_id == "fake-session-1"
    assert finished.started_at is not None and finished.finished_at is not None
    assert harness.events.events == [
        TaskStarted(task.id),
        TaskProgress(task.id, "[tool] Read foo.py"),
        TaskProgress(task.id, "thinking hard"),
        TaskCompleted(task.id, "I finished the task."),
    ]

    report = harness.report_text(task.id)
    assert report.startswith(f"# Task {task.id}\n\nhow tall is Everest\n\n---\n\n")
    assert "SPOKEN_SUMMARY: I finished the task." in report
    assert finished.report_path == str(harness.settings.data_dir / "tasks" / f"{task.id}.md")

    log = harness.log_text(task.id)
    assert "[tool] Read foo.py" in log
    assert "thinking hard" in log

    # The report and the log both hold the work itself; neither is anyone else's business.
    tasks_dir = harness.settings.data_dir / "tasks"
    assert stat.S_IMODE((tasks_dir / f"{task.id}.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((tasks_dir / f"{task.id}.log").stat().st_mode) == 0o600


async def test_dispatch_prompts_the_agent_with_the_standing_preamble(make_harness):
    harness = make_harness()

    task = await dispatch(harness.manager, "battery chemistry")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert harness.runner.sessions[0].prompts == [
        build_prompt(await harness.manager.get(task.id))
    ]
    opened_task, resume = harness.runner.opened[0]
    assert opened_task.id == task.id
    assert resume is None


async def test_dispatch_resolves_the_model_alias(make_harness):
    harness = make_harness()

    aliased = await dispatch(harness.manager, model="sonnet")
    default = await dispatch(harness.manager)

    assert aliased.model == "claude-sonnet-5-5"
    assert default.model == harness.settings.subagent_model


async def test_dispatch_records_the_default_agent_when_none_is_named(make_harness):
    harness = make_harness()

    task = await dispatch(harness.manager)

    assert task.agent == "claude"
    assert (await harness.manager.get(task.id)).agent == "claude"


async def test_dispatch_refuses_an_agent_that_is_not_enabled(make_harness):
    harness = make_harness()

    with pytest.raises(AgentUnavailableError) as raised:
        await dispatch(harness.manager, agent="codex")

    assert raised.value.enabled == ["claude"]
    assert await harness.manager.list() == []


async def test_dispatch_without_a_project_starts_in_the_projects_root(make_harness, tmp_path):
    """Work is handed over as soon as it is recognised; the subagent finds the repo."""
    root = make_projects(tmp_path, "orchard-sensor-net")
    harness = make_harness(projects_root=root)

    task = await dispatch(harness.manager, "add a README")

    assert task.project is None
    assert task.cwd == str(root)


async def test_without_a_projects_root_an_unscoped_task_has_no_directory(make_harness, tmp_path):
    """The runner starts it in `data_dir/workspace`; nothing creates the missing root."""
    root = tmp_path / "projects"
    harness = make_harness(projects_root=root)

    task = await dispatch(harness.manager, "add a README")

    assert task.cwd is None
    assert not root.exists()


async def test_dispatch_sets_cwd_from_the_project(make_harness, tmp_path):
    root = make_projects(tmp_path, "orchard-sensor-net")
    harness = make_harness(projects_root=root)

    task = await dispatch(harness.manager, "add a README", project="orchard")

    assert task.project == "orchard-sensor-net"
    assert task.cwd == str(root / "orchard-sensor-net")


async def test_failed_result_marks_failed_and_publishes_taskfailed(make_harness):
    result = RunResult(
        ok=False,
        final_text="I could not do it.",
        spoken_summary="The task failed: exit code 2",
        session_id="sess-9",
        error="exit code 2",
    )
    harness = make_harness(FakeAgentRunner([result]))

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.FAILED
    assert finished.error == "exit code 2"
    assert finished.summary == "The task failed: exit code 2"
    assert harness.events.of(TaskFailed) == [TaskFailed(task.id, "exit code 2")]
    assert harness.events.of(TaskCompleted) == []
    assert "I could not do it." in harness.report_text(task.id)


async def test_failed_result_without_an_error_message(make_harness):
    harness = make_harness(FakeAgentRunner([RunResult(ok=False)]))

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.FAILED
    assert finished.error == "unknown error"


async def test_an_exception_from_open_fails_the_task(make_harness):
    class BrokenRunner:
        async def open(self, task, *, resume=None):
            raise RuntimeError("no CLI on PATH")

    harness = make_harness(BrokenRunner())

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.FAILED
    assert finished.error == "RuntimeError: no CLI on PATH"
    assert harness.events.of(TaskFailed) == [TaskFailed(task.id, "RuntimeError: no CLI on PATH")]


# --- concurrency ---------------------------------------------------------


async def test_second_task_stays_queued_until_the_first_finishes(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    second = await dispatch(harness.manager, description="two")
    await wait_for_status(harness.manager, first.id, TaskStatus.RUNNING)

    assert (await harness.manager.get(second.id)).status is TaskStatus.QUEUED
    assert len(harness.runner.opened) == 1

    assert (await harness.manager.wait_for(second.id, timeout=WAIT)).status is TaskStatus.DONE
    assert (await harness.manager.get(first.id)).status is TaskStatus.DONE
    assert len(harness.runner.opened) == 2


async def test_wait_for_returns_the_unfinished_task_on_timeout(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW))

    task = await dispatch(harness.manager)
    pending = await harness.manager.wait_for(task.id, timeout=0.01)

    assert pending.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
    assert (await harness.manager.wait_for(task.id, timeout=WAIT)).status is TaskStatus.DONE


async def test_wait_for_on_a_finished_task_returns_immediately(make_harness):
    harness = make_harness()
    loop = asyncio.get_running_loop()

    task = await dispatch(harness.manager)
    await harness.manager.wait_for(task.id, timeout=WAIT)

    before = loop.time()
    again = await harness.manager.wait_for(task.id, timeout=30)

    assert again.status is TaskStatus.DONE
    assert loop.time() - before < 0.5


async def test_wait_for_unknown_task_raises(make_harness):
    harness = make_harness()

    with pytest.raises(KeyError):
        await harness.manager.wait_for(404, timeout=WAIT)


# --- follow-ups ----------------------------------------------------------


async def test_followup_on_a_running_task_is_queued_and_resumed_after_the_turn(make_harness):
    """`send()` mid-turn would be dropped, so the follow-up becomes an immediate re-run."""
    harness = make_harness(FakeAgentRunner(delay_s=SLOW))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)

    returned = await harness.manager.followup(task.id, "also check the weather")

    assert returned.status is TaskStatus.RUNNING
    assert harness.runner.sessions[0].sent == []
    assert "[followup] also check the weather" in harness.log_text(task.id)

    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.DONE
    assert harness.runner.opened[1][1] == "fake-session-1"
    assert harness.runner.sessions[1].prompts == [
        "Follow-up from the user:\n- also check the weather"
    ]
    assert harness.runner.sessions[0].closed is True
    # One task, one announcement: the intermediate result is never published.
    assert harness.events.of(TaskStarted) == [TaskStarted(task.id)]
    assert harness.events.of(TaskCompleted) == [TaskCompleted(task.id, "I finished the task.")]


async def test_a_followup_steers_a_running_turn_that_can_take_it(make_harness, caplog):
    """Codex takes a follow-up into the turn it is running: no re-run, one announcement."""
    caplog.set_level(logging.INFO, logger="jarvis.tasks.manager")
    harness = make_harness(FakeAgentRunner(delay_s=SLOW, steer=True))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    returned = await harness.manager.followup(task.id, "also check the weather")
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert returned.status is TaskStatus.RUNNING
    assert harness.runner.sessions[0].sent == ["also check the weather"]
    assert len(harness.runner.opened) == 1
    assert finished.status is TaskStatus.DONE
    assert harness.events.of(TaskCompleted) == [TaskCompleted(task.id, "I finished the task.")]
    assert "[followup] also check the weather" in harness.log_text(task.id)
    assert f"[followup steered] task {task.id}" in caplog.text


async def test_a_steer_that_fails_for_real_is_the_followups_error_not_a_second_delivery(
    make_harness,
):
    """It may have landed: re-running it as well could do the thing twice."""
    harness = make_harness(FakeAgentRunner(delay_s=SLOW, steer=RuntimeError("pipe broke")))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    with pytest.raises(RuntimeError, match="pipe broke"):
        await harness.manager.followup(task.id, "also check the weather")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert len(harness.runner.opened) == 1


async def test_a_followup_after_the_last_drain_still_runs(make_harness, monkeypatch):
    """The turn is over and the queue was empty, but the row still says running: a
    follow-up now must not be queued into a drain that has already happened."""
    harness = make_harness(FakeAgentRunner(delay_s=0.01))
    drained, gate = asyncio.Event(), asyncio.Event()
    drain = harness.manager._run_live_followups

    async def drain_then_wait(task, result):
        result = await drain(task, result)
        if not drained.is_set():
            drained.set()
            await gate.wait()
        return result

    monkeypatch.setattr(harness.manager, "_run_live_followups", drain_then_wait)
    task = await dispatch(harness.manager)
    await asyncio.wait_for(drained.wait(), WAIT)

    await harness.manager.followup(task.id, "and the tides")
    gate.set()
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.DONE
    assert harness.runner.sessions[1].prompts == ["Follow-up from the user:\n- and the tides"]
    assert harness.events.of(TaskCompleted) == [TaskCompleted(task.id, "I finished the task.")]


async def test_a_followup_just_after_the_row_is_done_waits_for_the_run_to_tidy_up(
    make_harness, monkeypatch
):
    """The row is `done` but the run is still closing its session. Restarting underneath
    it would let its clean-up drop the new run's prompt and wake `wait_for` too early."""
    harness = make_harness(FakeAgentRunner(delay_s=SLOW))
    closing, gate = asyncio.Event(), asyncio.Event()
    close = harness.manager._close_session

    async def slow_first_close(task_id):
        if not closing.is_set():
            closing.set()
            await gate.wait()
        await close(task_id)

    monkeypatch.setattr(harness.manager, "_close_session", slow_first_close)
    task = await dispatch(harness.manager)
    await asyncio.wait_for(closing.wait(), WAIT)
    assert (await harness.manager.get(task.id)).status is TaskStatus.DONE

    followup = asyncio.create_task(harness.manager.followup(task.id, "one more thing"))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(followup, WAIT)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.DONE
    assert harness.runner.sessions[1].prompts == ["one more thing"]
    assert len(harness.events.of(TaskCompleted)) == 2


async def test_several_live_followups_are_joined_into_one_resumed_run(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    await harness.manager.followup(task.id, "one")
    await harness.manager.followup(task.id, "two")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert len(harness.runner.opened) == 2
    assert harness.runner.sessions[1].prompts == ["Follow-up from the user:\n- one\n- two"]


async def test_a_followup_during_the_resumed_run_is_run_in_turn(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    await harness.manager.followup(task.id, "one")
    await wait_until(
        lambda: _opened(harness, 2), message="the resumed run to open", timeout=WAIT
    )
    await harness.manager.followup(task.id, "two")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert [session.prompts for session in harness.runner.sessions[1:]] == [
        ["Follow-up from the user:\n- one"],
        ["Follow-up from the user:\n- two"],
    ]
    assert harness.events.of(TaskCompleted) == [TaskCompleted(task.id, "I finished the task.")]


async def test_a_live_followup_without_a_session_id_re_runs_from_the_top(make_harness):
    harness = make_harness(FakeAgentRunner([RunResult(ok=True, final_text="done")], delay_s=SLOW))

    task = await dispatch(harness.manager, description="original question")
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    await harness.manager.followup(task.id, "and the tides")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert harness.runner.opened[1][1] is None
    assert harness.runner.sessions[1].prompts == [
        f"{build_prompt(task)}\n\nFollow-up from the user:\n- and the tides"
    ]


async def test_followup_on_a_finished_task_resumes_the_claude_session(make_harness):
    harness = make_harness()

    task = await dispatch(harness.manager, description="original question")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    restarted = await harness.manager.followup(task.id, "one more thing")
    assert restarted.status is TaskStatus.QUEUED
    assert restarted.finished_at is None

    finished = await harness.manager.wait_for(task.id, timeout=WAIT)
    assert finished.status is TaskStatus.DONE
    assert harness.runner.opened[1][1] == "fake-session-1"
    assert harness.runner.sessions[1].prompts == ["one more thing"]
    assert harness.events.of(TaskStarted) == [TaskStarted(task.id), TaskStarted(task.id)]


async def test_a_followup_resumes_on_the_agent_that_ran_the_task(make_harness):
    """Not on today's default: a session id belongs to the agent that issued it."""
    claude, codex = FakeAgentRunner(), FakeAgentRunner()
    router = RoutingAgentRunner({"claude": claude, "codex": codex}, default="claude")
    harness = make_harness(router, agents_enabled=["claude", "codex"])

    task = await dispatch(harness.manager, agent="codex")
    await harness.manager.wait_for(task.id, timeout=WAIT)
    harness.settings.agent_backend = "claude"  # the default moves on in the meantime
    await harness.manager.followup(task.id, "one more thing")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert [resume for _, resume in codex.opened] == [None, "fake-session-1"]
    assert claude.opened == []


async def test_a_run_past_the_time_limit_is_stopped_and_says_so(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=5), subagent_timeout_s=0.05)

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.FAILED
    assert finished.error == "timed out after 0s (SUBAGENT_TIMEOUT_S)"
    assert finished.summary == "The task ran for 0 seconds without finishing, so I stopped it."
    assert harness.runner.sessions[0].closed is True


async def test_a_timed_out_agent_is_stopped_before_its_failure_is_reported(make_harness):
    """Timing out the reader does not stop the agent: Jarvis must not say it stopped work
    that is still changing files. Interrupt and close come before the row and the event."""
    harness = make_harness(FakeAgentRunner(delay_s=5), subagent_timeout_s=0.05)
    seen: list[tuple[int, bool]] = []

    def on_failed(event):
        session = harness.runner.sessions[0]
        seen.append((session.interrupts, session.closed))

    harness.manager._bus.subscribe(TaskFailed, on_failed)
    task = await dispatch(harness.manager)
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert seen == [(1, True)]


class AdapterRunner:
    """Real `AdapterSession`s over scripted adapters, for what only the real session does."""

    def __init__(self, *adapters):
        self.adapters = list(adapters)
        self.opened: list[tuple[Task, str | None]] = []

    async def open(self, task, *, resume=None):
        from jarvis.agents.session import AdapterSession

        self.opened.append((task, resume))
        return AdapterSession(self.adapters.pop(0), session_id=resume)


async def test_a_turn_blocked_in_its_reader_is_interrupted_closed_and_unwound(make_harness):
    from agents.fakes import BLOCK, ScriptedAdapter

    from jarvis.agents.session import Text

    adapter = ScriptedAdapter([Text("working"), BLOCK])
    harness = make_harness(AdapterRunner(adapter), subagent_timeout_s=0.05)
    seen: list[tuple[int, int, int]] = []
    harness.manager._bus.subscribe(
        TaskFailed,
        lambda event: seen.append((adapter.turns_closed, adapter.interrupts, adapter.closes)),
    )

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.status is TaskStatus.FAILED
    assert seen == [(1, 1, 1)]


async def test_a_task_records_what_it_spent_across_a_live_follow_up(make_harness):
    """Every turn of one run is summed: the first, and the re-run a follow-up caused."""
    spent = RunResult(
        ok=True,
        final_text="SPOKEN_SUMMARY: done",
        spoken_summary="done",
        session_id="s-1",
        cost_usd=0.25,
        usage=TokenUsage(input_tokens=1000, output_tokens=50, cached_input_tokens=800),
    )
    harness = make_harness(FakeAgentRunner([spent], delay_s=SLOW))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)
    await harness.manager.followup(task.id, "and the tides")
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert len(harness.runner.opened) == 2
    assert (finished.input_tokens, finished.output_tokens) == (2000, 100)
    assert finished.cost_usd == 0.5


async def test_a_re_run_adds_to_what_the_task_had_spent(make_harness):
    spent = RunResult(
        ok=True, spoken_summary="done", session_id="s-1", usage=TokenUsage(300, 20)
    )
    harness = make_harness(FakeAgentRunner([spent]))

    task = await dispatch(harness.manager)
    await harness.manager.wait_for(task.id, timeout=WAIT)
    await harness.manager.followup(task.id, "one more thing")
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert (finished.input_tokens, finished.output_tokens, finished.cost_usd) == (600, 40, None)


async def test_an_agent_that_says_nothing_about_spend_leaves_the_columns_empty(make_harness):
    harness = make_harness(FakeAgentRunner([RunResult(ok=True, spoken_summary="done")]))

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert (finished.input_tokens, finished.output_tokens, finished.cost_usd) == (None, None, None)


async def test_no_time_limit_means_none(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=0.05), subagent_timeout_s=0)

    task = await dispatch(harness.manager)

    assert (await harness.manager.wait_for(task.id, timeout=WAIT)).status is TaskStatus.DONE


@pytest.mark.parametrize(
    ("seconds", "spoken"),
    [
        (10800, "3 hours"),
        (3600, "1 hour"),
        (5400, "90 minutes"),
        (60, "1 minute"),
        (45, "45 seconds"),
    ],
)
def test_the_time_limit_is_said_the_way_a_person_would(seconds, spoken):
    assert _duration(seconds) == spoken


async def test_followup_without_a_session_id_starts_a_fresh_run(make_harness):
    harness = make_harness(FakeAgentRunner([RunResult(ok=True, final_text="done")]))

    task = await dispatch(harness.manager, description="original question")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    await harness.manager.followup(task.id, "one more thing")
    await harness.manager.wait_for(task.id, timeout=WAIT)

    assert harness.runner.opened[1][1] is None
    assert harness.runner.sessions[1].prompts == [
        build_prompt(task) + "\n\nFollow-up: one more thing"
    ]


async def test_followup_on_a_queued_task_extends_the_description(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    queued = await dispatch(harness.manager, description="two")
    await wait_for_status(harness.manager, first.id, TaskStatus.RUNNING)

    updated = await harness.manager.followup(queued.id, "and also three")

    assert updated.status is TaskStatus.QUEUED
    assert updated.description == "two\n\nAdditionally: and also three"
    await harness.manager.wait_for(queued.id, timeout=WAIT)
    assert "and also three" in harness.runner.sessions[1].prompts[0]


async def test_followup_on_a_finished_task_waits_for_the_semaphore_as_queued(make_harness):
    """A re-run still has to queue, so the row must not claim to be running yet."""
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    await harness.manager.wait_for(first.id, timeout=WAIT)
    blocker = await dispatch(harness.manager, description="two")
    await wait_for_status(harness.manager, blocker.id, TaskStatus.RUNNING)

    restarted = await harness.manager.followup(first.id, "more please")

    assert restarted.status is TaskStatus.QUEUED
    assert (await harness.manager.get(first.id)).status is TaskStatus.QUEUED
    assert len(harness.runner.opened) == 2  # nothing opened for the re-run yet

    # A second follow-up while the re-run is still queued joins the pending prompt
    # instead of being appended to the description the agent has already been given.
    again = await harness.manager.followup(first.id, "and this too")
    assert again.description == "one"

    finished = await harness.manager.wait_for(first.id, timeout=WAIT)
    assert finished.status is TaskStatus.DONE
    assert harness.runner.opened[2][1] == "fake-session-1"
    assert harness.runner.sessions[2].prompts == ["more please\n\nAdditionally: and this too"]


async def test_followup_on_a_cancelled_task_is_an_error(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    queued = await dispatch(harness.manager, description="two")
    await wait_for_status(harness.manager, first.id, TaskStatus.RUNNING)
    await harness.manager.cancel(queued.id)

    with pytest.raises(ValueError, match="cancelled"):
        await harness.manager.followup(queued.id, "please continue")


async def test_followup_on_a_running_task_with_no_live_session_is_an_error(make_harness, store):
    """A row left `running` by an earlier process has no session to send anything to."""
    harness = make_harness()
    orphan = await store.create(Task(id=None, kind=TaskKind.AGENT, description="from last boot"))
    await store.update(orphan.id, status=TaskStatus.RUNNING)

    with pytest.raises(ValueError, match="starting up"):
        await harness.manager.followup(orphan.id, "hello")


async def test_followup_on_an_unknown_task_raises(make_harness):
    harness = make_harness()

    with pytest.raises(KeyError):
        await harness.manager.followup(404, "hello")


# --- cancel --------------------------------------------------------------


async def test_cancel_a_running_task_interrupts_and_marks_cancelled(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=30))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)

    cancelled = await harness.manager.cancel(task.id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert harness.runner.sessions[0].interrupts == 1
    assert harness.runner.sessions[0].closed is True
    assert harness.events.of(TaskCompleted, TaskFailed) == []


async def test_cancel_when_the_interrupt_ends_the_turn_still_cancels(make_harness):
    """Interrupting is what ends a real turn, so `run()` returns before the cancel lands."""
    harness = make_harness(FakeAgentRunner(delay_s=30, interrupt_ends_run=True))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)

    cancelled = await harness.manager.cancel(task.id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.error is None
    assert harness.runner.sessions[0].interrupts == 1
    assert harness.runner.sessions[0].closed is True
    assert harness.events.of(TaskCompleted, TaskFailed) == []
    assert not (harness.settings.data_dir / "tasks" / f"{task.id}.md").exists()


async def test_cancel_a_queued_task_never_opens_an_agent(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    queued = await dispatch(harness.manager, description="two")
    await wait_for_status(harness.manager, first.id, TaskStatus.RUNNING)

    cancelled = await harness.manager.cancel(queued.id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert len(harness.runner.opened) == 1
    assert (await harness.manager.wait_for(first.id, timeout=WAIT)).status is TaskStatus.DONE
    assert len(harness.runner.opened) == 1
    assert harness.events.of(TaskCompleted) == [TaskCompleted(first.id, "I finished the task.")]


async def test_cancel_a_finished_task_returns_it_unchanged(make_harness):
    harness = make_harness()

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert (await harness.manager.cancel(task.id)) == finished


async def test_cancel_an_unknown_task_raises(make_harness):
    harness = make_harness()

    with pytest.raises(KeyError):
        await harness.manager.cancel(404)


# --- limits --------------------------------------------------------------


async def test_daily_cap_blocks_further_dispatches(make_harness):
    harness = make_harness(daily_task_cap=1)

    await dispatch(harness.manager)
    with pytest.raises(TaskLimitError):
        await dispatch(harness.manager)


# --- projects ------------------------------------------------------------


async def test_resolve_project_prefers_the_configured_name(make_harness, tmp_path):
    root = make_projects(tmp_path, "orchard-sensor-net")
    checkout = tmp_path / "elsewhere" / "jarvis"
    checkout.mkdir(parents=True)
    harness = make_harness(projects={"jarvis": str(checkout)}, projects_root=root)

    assert harness.manager.resolve_project("jarvis") == ("jarvis", checkout)


async def test_resolve_project_is_fuzzy_about_spaces_and_dashes(make_harness, tmp_path):
    root = make_projects(tmp_path, "orchard-sensor-net")
    harness = make_harness(projects_root=root)

    for spoken in ("orchard sensor net", "Orchard_Sensor_Net", "orchard"):
        assert harness.manager.resolve_project(spoken) == (
            "orchard-sensor-net",
            root / "orchard-sensor-net",
        )


async def test_resolve_project_rejects_an_ambiguous_name(make_harness, tmp_path):
    root = make_projects(tmp_path, "alpha-one", "alpha-two")
    harness = make_harness(projects_root=root)

    with pytest.raises(UnknownProjectError) as excinfo:
        harness.manager.resolve_project("alpha")

    assert "alpha-one" in str(excinfo.value)
    assert "alpha-two" in str(excinfo.value)


async def test_resolve_project_unknown_name_lists_candidates(make_harness, tmp_path):
    root = make_projects(tmp_path, "orchard-sensor-net")
    harness = make_harness(projects_root=root)

    with pytest.raises(UnknownProjectError) as excinfo:
        harness.manager.resolve_project("nonesuch")

    assert excinfo.value.name == "nonesuch"
    assert excinfo.value.candidates == ["orchard-sensor-net"]
    assert "orchard-sensor-net" in str(excinfo.value)


async def test_dispatch_with_an_unknown_project_raises(make_harness, tmp_path):
    harness = make_harness(projects_root=tmp_path / "missing")

    with pytest.raises(UnknownProjectError):
        await dispatch(harness.manager, "add a README", project="nonesuch")


async def test_list_projects_configured_first_then_sorted_subdirs(make_harness, tmp_path):
    root = make_projects(tmp_path, "beta", "alpha", ".hidden")
    (root / "notes.txt").write_text("not a project")
    harness = make_harness(projects={"zeta": str(root / "beta")}, projects_root=root)

    assert harness.manager.list_projects() == [
        ("zeta", root / "beta"),
        ("alpha", root / "alpha"),
    ]


async def test_list_projects_without_a_projects_root(make_harness, tmp_path):
    harness = make_harness(projects_root=tmp_path / "missing")

    assert harness.manager.list_projects() == []


# --- get / list / lifecycle ---------------------------------------------


async def test_get_and_list(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=SLOW), max_concurrent_tasks=1)

    first = await dispatch(harness.manager, description="one")
    second = await dispatch(harness.manager, description="two")

    assert (await harness.manager.get(first.id)).description == "one"
    assert await harness.manager.get(404) is None
    assert [task.id for task in await harness.manager.list()] == [second.id, first.id]
    queued = await harness.manager.list(status=TaskStatus.QUEUED, limit=5)
    assert second.id in [task.id for task in queued]


async def test_start_creates_the_task_directory(make_harness):
    harness = make_harness()

    await harness.manager.start()

    assert (harness.settings.data_dir / "tasks").is_dir()


async def test_shutdown_cancels_running_tasks_and_closes_sessions(make_harness):
    harness = make_harness(FakeAgentRunner(delay_s=30))

    task = await dispatch(harness.manager)
    await wait_for_status(harness.manager, task.id, TaskStatus.RUNNING)

    await harness.manager.shutdown()

    assert (await harness.manager.get(task.id)).status is TaskStatus.CANCELLED
    assert harness.runner.sessions[0].closed is True
    assert harness.events.of(TaskCompleted, TaskFailed) == []


def test_closing_a_session_outlasts_the_sdks_terminate_and_kill_escalation():
    # The SDK's `close()` waits up to 5 s for the write lock, then 5 s for a clean exit,
    # 5 s after SIGTERM and 5 s after SIGKILL. Giving up sooner abandons a CLI that is
    # still mid-turn instead of terminating it.
    assert CLOSE_TIMEOUT_S > 5.0 * 4


# --- a task that changed Jarvis's own code ---------------------------------


async def test_a_subagents_restart_request_lands_on_the_task(make_harness):
    result = RunResult(
        ok=True,
        final_text="Report.",
        spoken_summary="I added the recall tool.",
        restart_reason="registers the new tool at startup",
    )
    harness = make_harness(FakeAgentRunner([result]))

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.needs_restart is True


async def test_a_task_that_asked_for_nothing_needs_no_restart(make_harness):
    harness = make_harness(FakeAgentRunner([RunResult(ok=True, spoken_summary="done")]))

    task = await dispatch(harness.manager)
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.needs_restart is False


async def test_an_internal_task_may_not_take_jarvis_off_the_air(make_harness):
    """The memory update runs after every call; a restart is not its to ask for."""
    result = RunResult(ok=True, spoken_summary="folded it in", restart_reason="why not")
    harness = make_harness(FakeAgentRunner([result]))

    task = await harness.manager.dispatch(
        "update the memory", origin_channel="local", origin_caller=None, internal=True
    )
    finished = await harness.manager.wait_for(task.id, timeout=WAIT)

    assert finished.needs_restart is False


# --- picking up what a killed process never started -------------------------


async def test_a_queued_row_from_a_dead_process_is_started(make_harness):
    """`queued` means it never ran an instruction, so this is its first run, not a re-run."""
    harness = make_harness(FakeAgentRunner([RunResult(ok=True, spoken_summary="done")]))
    orphan = await harness.manager._store.create(
        Task(id=None, kind=TaskKind.AGENT, description="left behind", status=TaskStatus.QUEUED)
    )

    assert await harness.manager.resume_queued() == [orphan.id]

    finished = await harness.manager.wait_for(orphan.id, timeout=WAIT)
    assert finished.status is TaskStatus.DONE


async def test_a_running_row_is_left_alone(make_harness):
    """That subagent had opened; what it got through before the machine died is unknowable."""
    harness = make_harness()
    await harness.manager._store.create(
        Task(id=None, kind=TaskKind.AGENT, description="was mid-flight", status=TaskStatus.RUNNING)
    )

    assert await harness.manager.resume_queued() == []


async def test_the_oldest_thing_they_asked_for_runs_first(make_harness):
    harness = make_harness()
    ids = []
    for minute in (30, 10, 20):
        row = await harness.manager._store.create(
            Task(
                id=None,
                kind=TaskKind.AGENT,
                description=f"queued {minute}",
                status=TaskStatus.QUEUED,
                created_at=datetime(2026, 8, 26, 12, minute, tzinfo=UTC),
            )
        )
        ids.append((minute, row.id))

    resumed = await harness.manager.resume_queued()

    assert resumed == [next(i for m, i in ids if m == minute) for minute in (10, 20, 30)]


async def test_nothing_queued_is_nothing_to_do(make_harness):
    assert await make_harness().manager.resume_queued() == []


async def test_a_store_that_will_not_answer_does_not_stop_the_service_starting(make_harness):
    harness = make_harness()

    async def explode(**kwargs):
        raise RuntimeError("the database is gone")

    harness.manager._store.list = explode

    assert await harness.manager.resume_queued() == []


# --- the default executor --------------------------------------------------


@pytest.mark.parametrize(("tasks", "workers"), [(1, 32), (3, 32), (10, 56)])
def test_the_executor_has_room_for_every_task_and_then_some(settings, tasks, workers):
    assert executor_workers(settings.model_copy(update={"max_concurrent_tasks": tasks})) == workers


async def test_with_every_stream_blocked_a_cancel_and_the_database_still_run(settings, store):
    """Each running Codex turn parks two threads (its stream and its notices) in the default
    executor. With every slot's worth parked, what a cancel and a status write need must
    still get a thread."""
    settings = settings.model_copy(update={"max_concurrent_tasks": 10})
    install_default_executor(settings)  # the loop shuts it down when the test ends
    release = threading.Event()
    parked = [
        asyncio.create_task(asyncio.to_thread(release.wait))
        for _ in range(2 * settings.max_concurrent_tasks)
    ]
    try:
        created = await asyncio.wait_for(
            store.create(Task(id=None, kind=TaskKind.AGENT, description="x")), WAIT
        )
        interrupted = await asyncio.wait_for(asyncio.to_thread(lambda: "interrupt sent"), WAIT)
        closed = await asyncio.wait_for(asyncio.to_thread(lambda: "closed"), WAIT)
    finally:
        release.set()
        await asyncio.gather(*parked)

    assert created.id is not None
    assert (interrupted, closed) == ("interrupt sent", "closed")
