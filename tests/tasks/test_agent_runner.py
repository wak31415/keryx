"""Tests for the Claude Agent SDK runner, the fake runner and summary extraction.

The real SDK is never started: `ClaudeAgentRunner` takes a `client_factory`, and the
double below yields real `claude_agent_sdk` message dataclasses so the parsing code is
exercised against the actual shapes the SDK emits.
"""

import asyncio

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

from jarvis.tasks.agent_runner import (
    DEFAULT_FAKE_RESULT,
    INTERRUPTED_RESULT,
    NO_SUMMARY,
    ClaudeAgentRunner,
    ClaudeAgentSession,
    FakeAgentRunner,
    RunResult,
    build_options,
    extract_spoken_summary,
    resolve_model,
)
from jarvis.tasks.models import Task, TaskKind


def make_task(**overrides) -> Task:
    values = {
        "id": 7,
        "kind": TaskKind.CHAT,
        "description": "find out when the next full moon is",
    }
    values.update(overrides)
    return Task(**values)


def assistant(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-opus-5")


def result_message(**overrides) -> ResultMessage:
    values = {
        "subtype": "success",
        "duration_ms": 1200,
        "duration_api_ms": 900,
        "is_error": False,
        "num_turns": 3,
        "session_id": "sess-1",
        "total_cost_usd": 0.42,
        "result": "All done.\n\nSPOKEN_SUMMARY: The moon is full on Tuesday.",
    }
    values.update(overrides)
    return ResultMessage(**values)


class FakeSdkClient:
    """Stand-in for `ClaudeSDKClient`: scripted messages in, recorded calls out."""

    def __init__(self, options=None, messages=(), *, fail_on=None, error=None):
        self.options = options
        self.messages = list(messages)
        self.error = error
        self.fail_on = set(fail_on or ())
        self.connects = 0
        self.queries: list[str] = []
        self.interrupts = 0
        self.disconnects = 0

    async def connect(self) -> None:
        if "connect" in self.fail_on:
            raise RuntimeError("no CLI on PATH")
        self.connects += 1

    async def query(self, prompt: str, session_id: str = "default") -> None:
        if "query" in self.fail_on:
            raise RuntimeError("query failed")
        self.queries.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error

    async def interrupt(self) -> None:
        self.interrupts += 1
        if "interrupt" in self.fail_on:
            raise RuntimeError("cannot interrupt")

    async def disconnect(self) -> None:
        self.disconnects += 1
        if "disconnect" in self.fail_on:
            raise RuntimeError("cannot disconnect")


# --------------------------------------------------------------------------- summaries


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


# ------------------------------------------------------------------------ model names


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("opus", "claude-opus-5"),
        ("sonnet", "claude-sonnet-5"),
        ("fable", "claude-fable-5"),
        ("haiku", "claude-haiku-4-5-20251001"),
        (" Opus ", "claude-opus-5"),
        ("claude-3-5-haiku-20241022", "claude-3-5-haiku-20241022"),
    ],
)
def test_resolve_model_maps_aliases_and_passes_ids_through(settings, name, expected):
    assert resolve_model(name, settings) == expected


@pytest.mark.parametrize("name", [None, "", "   "])
def test_resolve_model_defaults_to_the_configured_model(settings, name):
    settings.subagent_model = "claude-sonnet-5"

    assert resolve_model(name, settings) == "claude-sonnet-5"


# ------------------------------------------------------------------------ build_options


def test_build_options_sets_the_shared_agent_configuration(settings):
    settings.subagent_max_turns = 42
    settings.subagent_max_budget_usd = 2.5
    task = make_task(kind=TaskKind.RESEARCH, project="jarvis", description="read the spec")

    options = build_options(task, settings)

    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == ["user", "project"]
    assert options.max_turns == 42
    assert options.max_budget_usd == 2.5
    assert options.model == "claude-opus-5"
    assert options.resume is None
    assert options.include_partial_messages is False


def test_build_options_appends_the_rendered_subagent_suffix(settings):
    task = make_task(kind=TaskKind.CODING, project="jarvis", description="add a README")

    options = build_options(task, settings)

    assert options.system_prompt["type"] == "preset"
    assert options.system_prompt["preset"] == "claude_code"
    append = options.system_prompt["append"]
    assert "SPOKEN_SUMMARY:" in append
    assert "add a README" in append
    assert "coding" in append
    assert "jarvis" in append
    assert "{" not in append and "}" not in append


def test_build_options_defaults_cwd_to_a_created_workspace(settings):
    options = build_options(make_task(), settings)

    expected = settings.data_dir / "workspace"
    assert options.cwd == str(expected)
    assert expected.is_dir()


def test_build_options_uses_the_task_cwd_when_set(settings, tmp_path):
    project_dir = tmp_path / "repo"
    project_dir.mkdir()

    options = build_options(make_task(cwd=str(project_dir)), settings)

    assert options.cwd == str(project_dir)


def test_build_options_passes_the_resume_session_id(settings):
    options = build_options(make_task(), settings, resume="sess-9")

    assert options.resume == "sess-9"


def test_build_options_omits_the_env_without_an_api_key(settings):
    settings.anthropic_api_key = None

    assert build_options(make_task(), settings).env == {}


def test_build_options_passes_the_anthropic_key_in_the_env(settings):
    settings.anthropic_api_key = "sk-ant-test"

    assert build_options(make_task(), settings).env == {"ANTHROPIC_API_KEY": "sk-ant-test"}


def test_build_options_resolves_the_task_model(settings):
    options = build_options(make_task(model="sonnet"), settings)

    assert options.model == "claude-sonnet-5"


def test_build_options_for_chat_gives_only_read_only_built_ins(settings):
    options = build_options(make_task(kind=TaskKind.CHAT), settings)

    # `tools` is the restricting option; `allowed_tools` only auto-approves.
    assert options.tools == ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]
    assert options.allowed_tools == []
    assert options.mcp_servers == {}


def test_build_options_for_research_also_gives_write(settings):
    options = build_options(make_task(kind=TaskKind.RESEARCH), settings)

    assert options.tools == ["WebSearch", "WebFetch", "Read", "Glob", "Grep", "Write"]
    assert options.allowed_tools == []


def test_build_options_for_coding_does_not_restrict_tools(settings):
    options = build_options(make_task(kind=TaskKind.CODING), settings)

    assert options.tools is None
    assert options.allowed_tools == []
    assert options.mcp_servers == {}


def test_build_options_for_cowork_wires_the_google_mcp_server(settings):
    settings.google_oauth_client_id = "client-id"
    settings.google_oauth_client_secret = "client-secret"
    settings.user_google_email = "mail@example.com"

    options = build_options(make_task(kind=TaskKind.COWORK), settings)

    assert options.tools == ["WebSearch", "WebFetch", "Read"]
    assert options.allowed_tools == ["mcp__google__*"]
    google = options.mcp_servers["google"]
    assert google["type"] == "stdio"
    assert google["command"] == "uvx"
    assert google["args"] == [
        "workspace-mcp",
        "--tools",
        "gmail",
        "calendar",
        "--transport",
        "stdio",
        "--single-user",
    ]
    assert google["env"] == {
        "GOOGLE_OAUTH_CLIENT_ID": "client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
        "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost:8000/oauth2callback",
        "USER_GOOGLE_EMAIL": "mail@example.com",
        "GOOGLE_MCP_CREDENTIALS_DIR": str(settings.data_dir / "google"),
        "OAUTHLIB_INSECURE_TRANSPORT": "1",
    }


def test_build_options_for_cowork_omits_unconfigured_google_env(settings):
    options = build_options(make_task(kind=TaskKind.COWORK), settings)

    env = options.mcp_servers["google"]["env"]
    assert "GOOGLE_OAUTH_CLIENT_ID" not in env
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env
    assert "USER_GOOGLE_EMAIL" not in env
    assert env["OAUTHLIB_INSECURE_TRANSPORT"] == "1"


# ---------------------------------------------------------------------------- runner


async def test_runner_open_connects_a_client_built_from_the_task(settings):
    created: list[FakeSdkClient] = []

    def factory(options):
        client = FakeSdkClient(options)
        created.append(client)
        return client

    runner = ClaudeAgentRunner(settings, client_factory=factory)
    session = await runner.open(make_task(kind=TaskKind.RESEARCH), resume="sess-3")

    assert isinstance(session, ClaudeAgentSession)
    assert created[0].connects == 1
    assert created[0].options.resume == "sess-3"
    assert created[0].options.tools[-1] == "Write"


async def test_runner_open_propagates_a_connect_failure(settings):
    def factory(options):
        return FakeSdkClient(options, fail_on=["connect"])

    runner = ClaudeAgentRunner(settings, client_factory=factory)

    with pytest.raises(RuntimeError, match="no CLI on PATH"):
        await runner.open(make_task())


# --------------------------------------------------------------------------- session


async def test_session_run_returns_the_result_and_reports_progress():
    client = FakeSdkClient(
        messages=[
            SystemMessage(subtype="init", data={}),
            assistant(TextBlock(text="Looking it up.")),
            assistant(ToolUseBlock(id="t1", name="WebSearch", input={"query": "full moon"})),
            UserMessage(content="tool result"),
            assistant(TextBlock(text="It is on Tuesday.")),
            result_message(),
        ]
    )
    progress: list[str] = []
    session = ClaudeAgentSession(client)

    outcome = await session.run("when is the full moon?", on_progress=progress.append)

    assert client.queries == ["when is the full moon?"]
    assert outcome == RunResult(
        ok=True,
        final_text="All done.\n\nSPOKEN_SUMMARY: The moon is full on Tuesday.",
        spoken_summary="The moon is full on Tuesday.",
        session_id="sess-1",
        cost_usd=0.42,
        error=None,
    )
    assert progress[0] == "Looking it up."
    assert progress[1].startswith("[tool] WebSearch ")
    assert "full moon" in progress[1]
    assert progress[2] == "It is on Tuesday."


async def test_session_run_falls_back_to_the_last_assistant_text():
    client = FakeSdkClient(
        messages=[
            assistant(TextBlock(text="First pass.")),
            assistant(TextBlock(text="Report.\n\nSPOKEN_SUMMARY: I checked the calendar.")),
            result_message(result=None),
        ]
    )

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.ok is True
    assert outcome.final_text == "Report.\n\nSPOKEN_SUMMARY: I checked the calendar."
    assert outcome.spoken_summary == "I checked the calendar."


async def test_session_run_awaits_an_async_progress_callback():
    client = FakeSdkClient(messages=[assistant(TextBlock(text="hi")), result_message()])
    seen: list[str] = []

    async def on_progress(text: str) -> None:
        seen.append(text)

    await ClaudeAgentSession(client).run("go", on_progress=on_progress)

    assert seen == ["hi"]


async def test_session_run_ignores_a_failing_progress_callback():
    client = FakeSdkClient(messages=[assistant(TextBlock(text="hi")), result_message()])

    def on_progress(text: str) -> None:
        raise ValueError("boom")

    outcome = await ClaudeAgentSession(client).run("go", on_progress=on_progress)

    assert outcome.ok is True


async def test_session_run_reports_an_error_result():
    client = FakeSdkClient(
        messages=[result_message(is_error=True, subtype="error_max_turns", result=None)]
    )

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.ok is False
    assert outcome.error == "error_max_turns"
    assert outcome.session_id == "sess-1"
    assert outcome.spoken_summary.startswith("The task failed")


async def test_session_run_survives_a_transport_exception():
    client = FakeSdkClient(
        messages=[assistant(TextBlock(text="Partial work."))],
        error=RuntimeError("stream closed"),
    )

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.ok is False
    assert outcome.error == "RuntimeError: stream closed"
    assert outcome.final_text == "Partial work."
    assert outcome.spoken_summary == "The task failed: stream closed"


async def test_session_send_interrupt_and_close_reach_the_client():
    client = FakeSdkClient(messages=[result_message()])
    session = ClaudeAgentSession(client)

    await session.send("also check Wednesday")
    await session.interrupt()
    await session.close()

    assert client.queries == ["also check Wednesday"]
    assert client.interrupts == 1
    assert client.disconnects == 1


async def test_session_interrupt_and_close_swallow_client_errors():
    client = FakeSdkClient(fail_on=["interrupt", "disconnect"])
    session = ClaudeAgentSession(client)

    await session.interrupt()
    await session.close()
    await session.close()

    assert client.disconnects == 2


# ------------------------------------------------------------------------ fake runner


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
    runner = FakeAgentRunner()
    session = await runner.open(make_task())

    await session.send("and also this")
    await session.interrupt()
    await session.close()

    assert session.sent == ["and also this"]
    assert session.interrupts == 1
    assert session.closed is True


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
