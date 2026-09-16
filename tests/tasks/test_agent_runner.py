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
    SUBAGENT_MAX_BUFFER_BYTES,
    ClaudeAgentRunner,
    ClaudeAgentSession,
    FakeAgentRunner,
    RunResult,
    build_options,
    extract_restart_request,
    extract_spoken_summary,
    render_subagent_suffix,
    resolve_model,
)
from jarvis.tasks.models import Task, TaskKind


def make_task(**overrides) -> Task:
    values = {
        "id": 7,
        "kind": TaskKind.AGENT,
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
    task = make_task(kind=TaskKind.AGENT, project="jarvis", description="read the spec")

    options = build_options(task, settings)

    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == ["user", "project"]
    assert options.max_turns == 42
    assert options.max_budget_usd == 2.5
    assert options.model == "claude-opus-5"
    assert options.resume is None
    assert options.include_partial_messages is False


def test_build_options_lifts_the_sdk_message_size_limit(settings):
    # One `Read` of a figure echoes back as a single base64 NDJSON line; at the SDK's
    # 1 MiB default that line killed tasks 68-98 mid-turn.
    options = build_options(make_task(), settings)

    assert options.max_buffer_size == SUBAGENT_MAX_BUFFER_BYTES
    assert SUBAGENT_MAX_BUFFER_BYTES > 1024 * 1024


def test_build_options_appends_the_rendered_subagent_suffix(settings):
    task = make_task(project="jarvis", description="add a README")

    options = build_options(task, settings)

    assert options.system_prompt["type"] == "preset"
    assert options.system_prompt["preset"] == "claude_code"
    append = options.system_prompt["append"]
    assert "SPOKEN_SUMMARY:" in append
    assert "add a README" in append
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


def test_build_options_falls_back_to_the_oauth_token(settings):
    settings.anthropic_api_key = None
    settings.claude_code_oauth_token = "tok-1"

    assert build_options(make_task(), settings).env == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-1"}


def test_build_options_prefers_the_api_key_over_the_oauth_token(settings):
    settings.anthropic_api_key = "sk-ant-test"
    settings.claude_code_oauth_token = "tok-1"

    assert build_options(make_task(), settings).env == {"ANTHROPIC_API_KEY": "sk-ant-test"}


def test_build_options_resolves_the_task_model(settings):
    options = build_options(make_task(model="sonnet"), settings)

    assert options.model == "claude-sonnet-5"


def test_build_options_never_restricts_the_built_in_tools(settings):
    """One kind of task, and it keeps every tool: skills and subagents included.

    `tools` is the restricting option (`allowed_tools` only auto-approves), so leaving it
    unset is what gives the subagent the full set.
    """
    options = build_options(make_task(), settings)

    assert options.tools is None


def test_build_options_leaves_the_google_mcp_server_out_by_default(settings):
    """Gmail and Calendar come from the CLI's own claude.ai connectors now."""
    options = build_options(make_task(), settings)

    assert options.mcp_servers == {}
    assert options.allowed_tools == []


def test_build_options_wires_the_google_mcp_server_when_it_is_asked_for(settings):
    settings.google_workspace_mcp = True
    settings.google_oauth_client_id = "client-id"
    settings.google_oauth_client_secret = "client-secret"
    settings.user_google_email = "mail@example.com"

    options = build_options(make_task(), settings)

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


def test_build_options_omits_unconfigured_google_env(settings):
    settings.google_workspace_mcp = True

    options = build_options(make_task(), settings)

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
    session = await runner.open(make_task(), resume="sess-3")

    assert isinstance(session, ClaudeAgentSession)
    assert created[0].connects == 1
    assert created[0].options.resume == "sess-3"
    assert created[0].options.tools is None  # nothing is held back


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


async def test_session_run_survives_a_buffer_overflow():
    # The exact shape the SDK raises: a bare `Exception` re-raised from its query loop.
    client = FakeSdkClient(
        messages=[assistant(TextBlock(text="Opened the contact sheet."))],
        error=Exception(
            "Failed to decode JSON: JSON message exceeded maximum buffer size of "
            "1048576 bytes..."
        ),
    )

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.ok is False
    assert outcome.final_text == "Opened the contact sheet."
    assert outcome.spoken_summary.startswith("The task failed")


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


def test_the_subagent_suffix_makes_slack_opt_in(settings, unwrapped):
    """Subagents reach Slack through the MCP server the owner names; the suffix is the leash."""
    settings.slack_mcp_server = "team-slack"
    options = build_options(make_task(description="review the diff"), settings)
    append = unwrapped(options.system_prompt["append"])

    assert "Do not send him anything on Slack unless he asked for Slack" in append
    assert "If he did not ask, do not send" in append
    assert "`team-slack` MCP server" in append
    assert "offers to send it" in append
    assert "{" not in append and "}" not in append


def test_without_a_slack_server_the_suffix_says_nothing_about_slack(settings, unwrapped):
    """No route configured is no route described: nothing to use, and nothing to offer."""
    options = build_options(make_task(description="review the diff"), settings)
    append = unwrapped(options.system_prompt["append"])

    assert settings.slack_mcp_server is None
    assert "Slack" not in append
    assert "offers to send it" not in append
    assert "{" not in append and "}" not in append


def test_the_subagent_suffix_routes_unasked_output_to_the_report(settings, unwrapped):
    """What he may not be sent still has to land somewhere he can find it, Slack or not."""
    options = build_options(make_task(description="plot the losses"), settings)
    append = unwrapped(options.system_prompt["append"])

    assert "goes in the written report" in append


# --- RESTART_REQUIRED ------------------------------------------------------


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
    """`git log` cannot recover which edits he asked for out loud; the trailer can."""
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
