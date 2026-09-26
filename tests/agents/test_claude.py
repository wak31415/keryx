"""Tests for the Claude backend: the Agent SDK options, runner and session.

The real SDK is never started: `ClaudeAgentRunner` takes a `client_factory`, and the
double below yields real `claude_agent_sdk` message dataclasses so the parsing code is
exercised against the actual shapes the SDK emits.
"""

import stat
import subprocess

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

from jarvis.agents.base import AgentOpenError, RunResult, SteerUnavailable, TokenUsage
from jarvis.agents.claude import (
    SUBAGENT_MAX_BUFFER_BYTES,
    ClaudeAgentRunner,
    ClaudeAgentSession,
    build_options,
    claude_usage,
    resolve_model,
)
from jarvis.agents.session import AdapterSession
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
    assert stat.S_IMODE(expected.stat().st_mode) == 0o700  # it is under data_dir


def test_a_task_directory_that_is_gone_is_never_created(settings, tmp_path):
    """A projects root that was never there, or a checkout since deleted: Jarvis does not
    invent a folder in somebody's home directory, it starts in its own workspace."""
    gone = tmp_path / "projects"

    options = build_options(make_task(cwd=str(gone)), settings)

    assert options.cwd == str(settings.data_dir / "workspace")
    assert not gone.exists()


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


async def test_runner_open_connects_a_client_built_from_the_task(settings):
    created: list[FakeSdkClient] = []

    def factory(options):
        client = FakeSdkClient(options)
        created.append(client)
        return client

    runner = ClaudeAgentRunner(settings, client_factory=factory)
    session = await runner.open(make_task(), resume="sess-3")

    assert isinstance(session, AdapterSession)
    assert created[0].connects == 1
    assert created[0].options.resume == "sess-3"
    assert created[0].options.tools is None  # nothing is held back


def test_the_default_client_is_the_sdks_own(settings):
    from claude_agent_sdk import ClaudeSDKClient

    from jarvis.agents.claude import _default_client_factory

    client = _default_client_factory(build_options(make_task(), settings))

    assert isinstance(client, ClaudeSDKClient)


async def test_runner_open_propagates_a_connect_failure(settings):
    def factory(options):
        return FakeSdkClient(options, fail_on=["connect"])

    runner = ClaudeAgentRunner(settings, client_factory=factory)

    with pytest.raises(AgentOpenError, match="RuntimeError: no CLI on PATH"):
        await runner.open(make_task())


async def test_a_connect_failure_never_quotes_the_key(settings):
    settings.anthropic_api_key = "sk-ant-api03-secret-key"

    class Refusing(FakeSdkClient):
        async def connect(self):
            raise PermissionError("bad key sk-ant-api03-secret-key")

    runner = ClaudeAgentRunner(settings, client_factory=Refusing)

    with pytest.raises(AgentOpenError) as raised:
        await runner.open(make_task())

    assert "sk-ant-api03-secret-key" not in str(raised.value)


async def test_the_google_client_secret_is_kept_out_of_a_failed_turn(settings):
    settings.google_workspace_mcp = True
    settings.google_oauth_client_id = "client-id"
    settings.google_oauth_client_secret = "GOCSPX-a-real-looking-secret"
    client = FakeSdkClient(error=RuntimeError("workspace-mcp: GOCSPX-a-real-looking-secret"))
    session = await ClaudeAgentRunner(settings, client_factory=lambda options: client).open(
        make_task()
    )

    outcome = await session.run("go", on_progress=lambda text: None)

    assert "GOCSPX" not in outcome.error
    assert "[redacted]" in outcome.error


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


async def test_the_usage_and_the_cost_come_from_the_result():
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 3000,
        "output_tokens": 45,
    }
    client = FakeSdkClient(messages=[result_message(usage=usage, total_cost_usd=0.07)])

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.cost_usd == 0.07
    assert outcome.usage == TokenUsage(
        input_tokens=3210, output_tokens=45, cached_input_tokens=3000
    )


def test_no_usage_reported_is_none():
    assert claude_usage(None) is None
    assert claude_usage({}) is None


async def test_an_error_result_speaks_the_failure_not_a_summary():
    """The unified rule: a failed turn is spoken as the failure, whatever text it carries."""
    failed = result_message(is_error=True, subtype="error_during_execution", result="API down")
    client = FakeSdkClient(messages=[failed])

    outcome = await ClaudeAgentSession(client).run("go", on_progress=lambda text: None)

    assert outcome.ok is False
    assert outcome.error == "API down"
    assert outcome.final_text == "API down"
    assert outcome.spoken_summary == "The task failed: API down"


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
    assert outcome.spoken_summary == "The task failed: RuntimeError: stream closed"


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


async def test_a_follow_up_is_never_put_into_a_running_claude_turn():
    """A `query()` after the final text starts a turn `receive_response()` never reads."""
    client = FakeSdkClient(messages=[result_message()])

    with pytest.raises(SteerUnavailable):
        await ClaudeAgentSession(client).send("also check Wednesday")

    assert client.queries == []


async def test_session_interrupt_and_close_reach_the_client():
    client = FakeSdkClient(messages=[result_message()])
    session = ClaudeAgentSession(client)

    await session.interrupt()
    await session.close()

    assert client.interrupts == 1
    assert client.disconnects == 1


async def test_session_interrupt_and_close_swallow_client_errors():
    client = FakeSdkClient(fail_on=["interrupt", "disconnect"])
    session = ClaudeAgentSession(client)

    await session.interrupt()
    await session.close()
    await session.close()

    assert client.disconnects == 1


def test_the_subagent_suffix_makes_slack_opt_in(settings, unwrapped):
    """Subagents reach Slack through the MCP server the owner names; the suffix is the leash."""
    settings.slack_mcp_server = "team-slack"
    options = build_options(make_task(description="review the diff"), settings)
    append = unwrapped(options.system_prompt["append"])

    assert "Do not send them anything on Slack unless they asked for Slack" in append
    assert "If they did not ask, do not send" in append
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
    """What they may not be sent still has to land somewhere they can find it, Slack or not."""
    options = build_options(make_task(description="plot the losses"), settings)
    append = unwrapped(options.system_prompt["append"])

    assert "goes in the written report" in append


def test_build_options_hands_the_subagent_the_owners_name(settings):
    settings.owner_name = "Ada"

    options = build_options(make_task(), settings)

    assert "dispatched on Ada's behalf" in options.system_prompt["append"]


# ------------------------------------------------------------------ install and login


def test_the_claude_cli_is_the_one_the_sdk_bundles(monkeypatch, tmp_path):
    from jarvis.agents import claude as claude_module

    bundled = tmp_path / "_bundled" / "claude"
    bundled.parent.mkdir()
    bundled.write_text("")
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "__file__", str(tmp_path / "x.py"))
    assert claude_module.claude_cli() == str(bundled)

    bundled.unlink()
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert claude_module.claude_cli() == "/usr/bin/claude"


def test_a_stored_login_is_the_credentials_file_or_the_keychain(monkeypatch, tmp_path):
    from jarvis.agents import claude as claude_module

    monkeypatch.setattr(claude_module.Path, "home", lambda: tmp_path)
    calls: list[list[str]] = []

    def keychain(code):
        def run(argv, **kwargs):
            calls.append(argv)
            if code is None:
                raise OSError("no security binary")
            return subprocess.CompletedProcess(argv, code)

        return run

    monkeypatch.setattr(claude_module.subprocess, "run", keychain(0))
    assert claude_module.claude_stored_login() is True
    monkeypatch.setattr(claude_module.subprocess, "run", keychain(44))
    assert claude_module.claude_stored_login() is False
    monkeypatch.setattr(claude_module.subprocess, "run", keychain(None))
    assert claude_module.claude_stored_login() is False

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    calls.clear()
    assert claude_module.claude_stored_login() is True
    assert calls == []  # the file answers; the keychain is never asked
