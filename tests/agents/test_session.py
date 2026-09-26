"""Tests for the one agent session every backend runs through."""

import asyncio
import logging
from pathlib import Path

import pytest

from agents.fakes import BLOCK, ScriptedAdapter
from jarvis.agents.auth import AuthMode, AuthSource, AuthStatus
from jarvis.agents.base import AgentOpenError, RunResult, SteerUnavailable, TokenUsage
from jarvis.agents.session import (
    NO_DONE_ERROR,
    AdapterRunner,
    AdapterSession,
    AgentContext,
    Done,
    FileEdit,
    Notice,
    SessionId,
    Text,
    ToolCall,
)
from jarvis.tasks.models import Task, TaskKind

KEY = "sk-live-0123456789abcdef"
SLACK = "xoxb-000000000-slack-token"


def make_task(**overrides) -> Task:
    values = {"id": 5, "kind": TaskKind.AGENT, "description": "tidy the notes"}
    values.update(overrides)
    return Task(**values)


async def run(adapter, prompt="go", *, session_id=None, progress=None):
    session = AdapterSession(adapter, session_id=session_id)
    return await session.run(prompt, on_progress=(progress if progress is not None else []).append)


# ------------------------------------------------------------------------------ a turn


async def test_events_become_progress_lines_and_a_result():
    usage = TokenUsage(input_tokens=120, output_tokens=30, cached_input_tokens=100)
    adapter = ScriptedAdapter(
        [
            SessionId("thread-1"),
            Text("Looking at the repo."),
            Text("   "),
            ToolCall("shell", "/bin/zsh -lc ls"),
            FileEdit((("add", "NOTES.md"), ("update", "README.md"))),
            Text("Done.\n\nRESTART_REQUIRED: new tool\nSPOKEN_SUMMARY: I added the notes."),
            Done(ok=True, usage=usage, cost_usd=0.02),
        ]
    )
    progress: list[str] = []

    result = await run(adapter, "add notes", progress=progress)

    assert adapter.prompts == ["add notes"]
    assert result == RunResult(
        ok=True,
        final_text="Done.\n\nRESTART_REQUIRED: new tool\nSPOKEN_SUMMARY: I added the notes.",
        spoken_summary="I added the notes.",
        session_id="thread-1",
        cost_usd=0.02,
        restart_reason="new tool",
        usage=usage,
    )
    assert progress == [
        "Looking at the repo.",
        '[tool] shell "/bin/zsh -lc ls"',
        "[edit] add NOTES.md, update README.md",
        result.final_text,
    ]


async def test_the_result_text_wins_over_the_last_message():
    adapter = ScriptedAdapter(
        [Text("thinking out loud"), Done(ok=True, result_text="SPOKEN_SUMMARY: all good")]
    )

    result = await run(adapter)

    assert result.final_text == "SPOKEN_SUMMARY: all good"
    assert result.spoken_summary == "all good"


async def test_a_long_tool_call_is_cut_short():
    progress: list[str] = []
    await run(
        ScriptedAdapter([ToolCall("Write", {"content": "x" * 1000}), Done(ok=True)]),
        progress=progress,
    )

    assert len(progress[0]) < 220


async def test_a_session_id_given_at_open_is_kept_until_the_agent_names_another():
    result = await run(ScriptedAdapter([Done(ok=True)]), session_id="sess-0")

    assert result.session_id == "sess-0"


async def test_a_failed_turn_speaks_the_failure_and_asks_for_no_restart():
    adapter = ScriptedAdapter(
        [
            Text("I will now run the migration."),
            Text("RESTART_REQUIRED: changed jarvis"),
            Done(ok=False, error="usage limit reached", usage=TokenUsage(5, 1)),
        ]
    )

    result = await run(adapter)

    assert result.ok is False
    assert result.error == "usage limit reached"
    assert result.spoken_summary == "The task failed: usage limit reached"
    assert result.restart_reason is None
    assert result.final_text == "RESTART_REQUIRED: changed jarvis"
    assert result.usage == TokenUsage(5, 1)


@pytest.mark.parametrize(
    ("done", "error"),
    [(Done(ok=False, interrupted=True), "interrupted"), (Done(ok=False), "the turn failed")],
)
async def test_a_failure_with_no_message_still_says_something(done, error):
    result = await run(ScriptedAdapter([done]))

    assert result.error == error
    assert result.spoken_summary == f"The task failed: {error}"


async def test_a_turn_that_never_finishes_is_a_failure():
    result = await run(ScriptedAdapter([Text("half")]))

    assert result.ok is False
    assert result.error == NO_DONE_ERROR
    assert result.final_text == "half"


async def test_an_exception_mid_turn_keeps_what_was_said_and_the_session_id():
    adapter = ScriptedAdapter(
        [SessionId("t-9"), Text("one"), Text("two"), ConnectionError("stream closed")]
    )

    result = await run(adapter)

    assert result == RunResult(
        ok=False,
        final_text="one\n\ntwo",
        spoken_summary="The task failed: ConnectionError: stream closed",
        session_id="t-9",
        error="ConnectionError: stream closed",
    )
    assert adapter.turns_closed == 1


async def test_a_failing_or_async_progress_callback_is_harmless():
    seen: list[str] = []

    async def collect(text: str) -> None:
        seen.append(text)

    def explode(text: str) -> None:
        raise ValueError("listener gone")

    adapter = ScriptedAdapter([Text("a"), Done(ok=True)], [Text("b"), Done(ok=True)])
    session = AdapterSession(adapter)

    assert (await session.run("x", on_progress=collect)).ok
    assert (await session.run("y", on_progress=explode)).ok
    assert seen == ["a"]


async def test_cancelling_a_blocked_turn_closes_the_adapters_stream():
    adapter = ScriptedAdapter([Text("working"), BLOCK])
    session = AdapterSession(adapter)
    progress: list[str] = []

    running = asyncio.create_task(session.run("go", on_progress=progress.append))
    while not progress:
        await asyncio.sleep(0)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert adapter.turns_closed == 1


# ----------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "secret",
    [KEY, "agent-access-token-0001", "GOCSPX-google-client-secret", SLACK],
)
async def test_no_credential_reaches_an_error_a_notice_or_the_log(secret, caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis.agents.session")
    adapter = ScriptedAdapter(
        [Notice(f"server said {secret}"), Done(ok=False, error=f"401 for {secret}")],
        [RuntimeError(f"stream died quoting {secret}")],
        secrets=[secret],
        fail_on=["interrupt", "close"],
    )
    session = AdapterSession(adapter)

    failed = await session.run("go", on_progress=lambda text: None)
    crashed = await session.run("go", on_progress=lambda text: None)
    await session.interrupt()
    await session.close()

    for said in (failed.error, failed.spoken_summary, crashed.error, crashed.spoken_summary):
        assert secret not in said
        assert "[redacted]" in said
    assert secret not in caplog.text
    assert "[redacted]" in caplog.text


async def test_a_masked_key_is_redacted_too():
    result = await run(ScriptedAdapter([Done(ok=False, error="Incorrect key sk-abcd****wxyz")]))

    assert result.error == "Incorrect key [redacted]"


async def test_a_notice_is_logged_at_its_own_level(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.agents.session")
    await run(
        ScriptedAdapter(
            [Notice("retrying", level=logging.INFO), Notice("gave up"), Done(ok=True)]
        )
    )

    levels = {record.getMessage(): record.levelno for record in caplog.records}
    assert levels["subagent: retrying"] == logging.INFO
    assert levels["subagent: gave up"] == logging.WARNING


# ------------------------------------------------------------ steer, interrupt, close


async def test_send_steers_the_running_turn():
    adapter = ScriptedAdapter(steer=None)

    await AdapterSession(adapter).send("and the tests")

    assert adapter.steered == ["and the tests"]


async def test_an_agent_with_no_steer_says_so():
    with pytest.raises(SteerUnavailable):
        await AdapterSession(ScriptedAdapter()).send("and the tests")


async def test_a_steer_that_fails_otherwise_is_a_redacted_error():
    adapter = ScriptedAdapter(steer=ConnectionError(f"pipe broke near {KEY}"), secrets=[KEY])

    with pytest.raises(RuntimeError) as raised:
        await AdapterSession(adapter).send("more")

    assert not isinstance(raised.value, SteerUnavailable)
    assert KEY not in str(raised.value)


async def test_interrupt_and_close_reach_the_adapter_and_close_is_idempotent():
    adapter = ScriptedAdapter()
    session = AdapterSession(adapter)

    await session.interrupt()
    await session.close()
    await session.close()

    assert adapter.interrupts == 1
    assert adapter.closes == 1


# ---------------------------------------------------------------------- context, runner

SOURCE = AuthSource(
    api_key_setting="codex_api_key",
    api_key_env="CODEX_API_KEY",
    token_setting="codex_access_token",
    token_env="CODEX_ACCESS_TOKEN",
    stored_login=lambda: True,
    login_hint="log in",
)


def test_the_context_is_resolved_once_from_the_task_and_settings(settings, tmp_path):
    settings.codex_api_key = KEY
    settings.owner_name = "Ada"
    servers = {
        "slack": {"command": "slack-mcp", "env": {"SLACK_BOT_TOKEN": SLACK, "DEBUG": "1"}},
        "google": {
            "command": "uvx",
            "env": {
                "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-google-client-secret",
                "GOOGLE_MCP_CREDENTIALS_DIR": "/home/ada/.jarvis/google",
                "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost:8000/oauth2callback",
            },
        },
    }

    context = AgentContext.build(
        make_task(cwd=str(tmp_path), id=12), settings, auth=SOURCE, model="", mcp_servers=servers
    )

    assert context.cwd == tmp_path
    assert "Jarvis-Task: 12" in context.instructions
    assert "Ada" in context.instructions
    assert context.model is None
    assert context.auth.mode is AuthMode.API_KEY
    assert context.mcp_env["SLACK_BOT_TOKEN"] == SLACK
    assert context.secrets == (KEY, SLACK, "GOCSPX-google-client-secret")


class ScriptedRunner(AdapterRunner):
    name = "scripted"

    def __init__(self, settings, adapter=None, fail=None):
        super().__init__(settings)
        self.adapter = adapter or ScriptedAdapter([Done(ok=True)])
        self.fail = fail
        self.connected: list[tuple[AgentContext, str | None]] = []

    def context(self, task):
        return AgentContext(
            cwd=Path("/work"),
            instructions="",
            model=None,
            auth=AuthStatus(AuthMode.API_KEY, "CODEX_API_KEY", KEY),
        )

    async def connect(self, context, resume):
        self.connected.append((context, resume))
        if self.fail is not None:
            raise self.fail
        return self.adapter


async def test_open_connects_and_resumes_the_session_it_was_given(settings):
    runner = ScriptedRunner(settings)

    session = await runner.open(make_task(), resume="sess-4")
    result = await session.run("go", on_progress=lambda text: None)

    assert runner.connected[0][1] == "sess-4"
    assert result.session_id == "sess-4"


async def test_a_connection_that_fails_is_an_open_error_with_the_key_taken_out(settings):
    runner = ScriptedRunner(settings, fail=PermissionError(f"refused {KEY}"))

    with pytest.raises(AgentOpenError) as raised:
        await runner.open(make_task())

    assert str(raised.value) == "PermissionError: refused [redacted]"
    assert raised.value.__cause__ is None
