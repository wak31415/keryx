"""Tests for what every agent backend shares: summaries, restart markers, the suffix, the fake."""

import asyncio
import sys

import pytest

from jarvis.agents.base import (
    DEFAULT_FAKE_RESULT,
    INTERRUPTED_RESULT,
    NO_SUMMARY,
    FakeAgentRunner,
    RunResult,
    SteerUnavailable,
    extract_restart_request,
    extract_spoken_summary,
    render_subagent_suffix,
)
from jarvis.skills import CUSTOM_TOOLS_SKILL
from jarvis.tasks.models import Task, TaskKind


def make_task(**overrides) -> Task:
    values = {
        "id": 7,
        "kind": TaskKind.AGENT,
        "description": "find out when the next full moon is",
    }
    values.update(overrides)
    return Task(**values)


def test_extract_spoken_summary_takes_the_marked_block():
    text = "Long report.\n\nSPOKEN_SUMMARY: I fixed the test. It passes now."

    assert extract_spoken_summary(text) == "I fixed the test. It passes now."


def test_extract_spoken_summary_joins_the_lines_after_the_marker():
    text = "Report.\n\nSPOKEN_SUMMARY:\nI read the repo.\nNothing was broken."

    assert extract_spoken_summary(text) == "I read the repo. Nothing was broken."


def test_extract_spoken_summary_tolerates_markdown_noise_around_the_marker():
    text = "Report.\n\n## **SPOKEN_SUMMARY:** **I sent the email.**"

    assert extract_spoken_summary(text) == "I sent the email."


def test_extract_spoken_summary_uses_the_last_marker():
    text = (
        "SPOKEN_SUMMARY: the format is a line like this.\n\n"
        "Real report here.\n\n"
        "SPOKEN_SUMMARY: I booked the room."
    )

    assert extract_spoken_summary(text) == "I booked the room."


def test_extract_spoken_summary_cleans_bullets_and_backticks():
    text = "SPOKEN_SUMMARY:\n- I ran `pytest`.\n- Everything passed."

    assert extract_spoken_summary(text) == "I ran pytest. Everything passed."


def test_extract_spoken_summary_falls_back_to_the_last_paragraph():
    text = "First paragraph.\n\nThe last thing I did was restart the server.\n\n   \n"

    assert extract_spoken_summary(text) == "The last thing I did was restart the server."


def test_extract_spoken_summary_of_empty_text_is_a_stand_in():
    assert extract_spoken_summary("   \n\n  ") == NO_SUMMARY
    assert extract_spoken_summary("SPOKEN_SUMMARY:   ") == NO_SUMMARY


def test_extract_spoken_summary_of_an_empty_block_falls_back_to_the_report():
    assert extract_spoken_summary("I archived the mail.\n\nSPOKEN_SUMMARY:\n") == (
        "I archived the mail."
    )


def test_extract_spoken_summary_truncates_at_a_word_boundary():
    words = " ".join(["alpha"] * 200)
    summary = extract_spoken_summary(f"SPOKEN_SUMMARY: {words}")

    assert len(summary) <= 400
    assert summary.endswith("…")
    assert not summary.endswith("alph…")
    assert words.startswith(summary[:-1].strip())


async def test_fake_runner_returns_a_default_result_and_records_the_call():
    runner = FakeAgentRunner()
    task = make_task()

    session = await runner.open(task, resume="sess-2")
    outcome = await session.run("do the thing", on_progress=lambda text: None)

    assert runner.opened == [(task, "sess-2")]
    assert runner.sessions == [session]
    assert outcome == DEFAULT_FAKE_RESULT
    assert session.prompts == ["do the thing"]


async def test_fake_runner_emits_the_scripted_progress_lines():
    runner = FakeAgentRunner(progress=["reading", "writing"])
    session = await runner.open(make_task())
    seen: list[str] = []

    await session.run("go", on_progress=seen.append)

    assert seen == ["reading", "writing"]


async def test_fake_runner_pops_scripted_results_and_repeats_the_last():
    first = RunResult(ok=True, final_text="one", spoken_summary="one")
    second = RunResult(ok=False, final_text="two", spoken_summary="two", error="nope")
    runner = FakeAgentRunner([first, second])
    session = await runner.open(make_task())

    outcomes = [await session.run("a", on_progress=lambda t: None) for _ in range(3)]

    assert outcomes == [first, second, second]
    assert session.prompts == ["a", "a", "a"]


async def test_fake_runner_calls_a_script_with_the_task_and_resume():
    calls: list[tuple[Task, str | None]] = []

    def script(task, resume):
        calls.append((task, resume))
        return RunResult(ok=True, final_text="scripted", spoken_summary="scripted")

    runner = FakeAgentRunner(script)
    task = make_task()
    session = await runner.open(task, resume="sess-5")

    outcome = await session.run("go", on_progress=lambda t: None)

    assert calls == [(task, "sess-5")]
    assert outcome.final_text == "scripted"


async def test_fake_session_records_follow_ups_interrupts_and_close():
    runner = FakeAgentRunner(steer=True)
    session = await runner.open(make_task())

    await session.send("and also this")
    await session.interrupt()
    await session.close()

    assert session.sent == ["and also this"]
    assert session.interrupts == 1
    assert session.closed is True


async def test_a_fake_that_cannot_steer_refuses_like_a_real_agent_would():
    session = await FakeAgentRunner().open(make_task())

    with pytest.raises(SteerUnavailable):
        await session.send("and also this")

    assert session.sent == []


async def test_a_fake_steer_can_fail_for_real():
    session = await FakeAgentRunner(steer=RuntimeError("pipe broke")).open(make_task())

    with pytest.raises(RuntimeError, match="pipe broke"):
        await session.send("and also this")


async def test_fake_session_interrupt_can_end_the_turn():
    runner = FakeAgentRunner(delay_s=30, interrupt_ends_run=True)
    session = await runner.open(make_task())
    turn = asyncio.create_task(session.run("go", on_progress=lambda t: None))
    await asyncio.sleep(0)

    await session.interrupt()

    assert await asyncio.wait_for(turn, timeout=1) == INTERRUPTED_RESULT
    assert session.interrupts == 1


async def test_fake_session_run_is_cancellable_mid_delay():
    runner = FakeAgentRunner(delay_s=5)
    session = await runner.open(make_task())
    task = asyncio.create_task(session.run("go", on_progress=lambda t: None))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_no_marker_means_no_restart():
    assert extract_restart_request("I edited a file and ran the tests.") is None


def test_the_marker_is_read_with_its_reason():
    text = (
        "Report.\n\nRESTART_REQUIRED: registers the new tool at startup"
        "\n\nSPOKEN_SUMMARY: done"
    )

    assert extract_restart_request(text) == "registers the new tool at startup"


def test_the_marker_survives_the_markdown_a_model_wraps_it_in():
    """Same tolerance as SPOKEN_SUMMARY:, and for the same reason."""
    wrapped = ("**RESTART_REQUIRED:** why", "- RESTART_REQUIRED: why", "## RESTART_REQUIRED: why")
    for line in wrapped:
        assert extract_restart_request(f"Report.\n{line}\n") == "why"


def test_a_marker_with_no_reason_is_still_asking():
    """Empty is not None: the ask is the line being there, not what it says."""
    assert extract_restart_request("Report.\nRESTART_REQUIRED:\n") == ""


def test_merely_talking_about_a_restart_is_not_asking_for_one():
    """It takes Jarvis off the air, so only the explicit line counts."""
    text = "You will need to restart the service. A restart is required to load this."

    assert extract_restart_request(text) is None


def test_the_reason_is_trimmed_to_something_sayable():
    assert len(extract_restart_request("RESTART_REQUIRED: " + "x " * 400)) <= 120


def test_the_subagent_suffix_carries_the_task_number_for_the_commit_trailer(settings):
    """`git log` cannot recover which edits they asked for out loud; the trailer can."""
    task = Task(id=31, kind=TaskKind.AGENT, description="add a recall tool")

    suffix = render_subagent_suffix(task)

    assert "Jarvis-Task: 31" in suffix
    assert "Task number: 31" in suffix


def test_a_task_with_no_number_yet_still_renders():
    """The suffix is built at open(), after the row exists — but never crash if it is not."""
    suffix = render_subagent_suffix(Task(id=None, kind=TaskKind.AGENT, description="x"))

    assert "Jarvis-Task: unknown" in suffix


def test_the_subagent_suffix_tells_it_not_to_restart_jarvis_itself(settings):
    """It runs inside the service: restarting from there kills it mid-report."""
    suffix = render_subagent_suffix(
        Task(id=1, kind=TaskKind.AGENT, description="change jarvis")
    )

    assert "RESTART_REQUIRED:" in suffix
    assert "Do not restart it yourself" in suffix


def test_the_subagent_suffix_says_whom_the_work_is_for():
    task = Task(id=1, kind=TaskKind.AGENT, description="x")

    assert "dispatched on Ada's behalf" in render_subagent_suffix(task, owner="Ada")
    assert "dispatched on the owner's behalf" in render_subagent_suffix(task)


def test_the_subagent_suffix_says_where_the_owners_tools_go_and_how_to_check_them(
    tmp_path, unwrapped
):
    """Their tools are their data: outside the repo, never committed, live without a restart."""
    task = Task(id=1, kind=TaskKind.AGENT, description="give yourself a tide tool")

    suffix = unwrapped(render_subagent_suffix(task, tools_dir=tmp_path / "tools"))

    assert f"it goes in `{tmp_path / 'tools'}`" in suffix
    assert "never in the Jarvis repository, and it is never committed" in suffix
    assert f"Read `{CUSTOM_TOOLS_SKILL}`" in suffix
    assert CUSTOM_TOOLS_SKILL.is_file()
    assert f"`{sys.executable} -m jarvis tools`" in suffix
    assert "needs no restart and no RESTART_REQUIRED: line" in suffix
    assert "{custom_tools}" not in render_subagent_suffix(task)
    assert "voice tool" not in render_subagent_suffix(task)


def test_the_original_names_are_still_importable_from_tasks_agent_runner():
    """The runner started in `tasks/agent_runner.py`; the move keeps those names."""
    from jarvis.agents import base, claude
    from jarvis.tasks import agent_runner

    assert agent_runner.RunResult is base.RunResult
    assert agent_runner.AgentRunner is base.AgentRunner
    assert agent_runner.AgentSession is base.AgentSession
    assert agent_runner.FakeAgentRunner is base.FakeAgentRunner
    assert agent_runner.extract_spoken_summary is base.extract_spoken_summary
    assert agent_runner.ClaudeAgentRunner is claude.ClaudeAgentRunner
