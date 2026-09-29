"""Tests for the Codex backend, on the `openai-codex` SDK.

`codex` is never started: the runner takes a `client_factory`, and the doubles below replay
the fixtures in `fixtures/codex_app_*.jsonl` — notifications the bundled app-server 0.157.1
sent for real turns, rebuilt here into the SDK's own typed models, so the adapter is read
against the exact shapes it will meet. The two tests that go as far as the SDK's `Popen`
replace it, so nothing is spawned there either.
"""

import asyncio
import json
import logging
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex import InternalRpcError, InvalidRequestError
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from openai_codex.models import Notification

from jarvis import plugins
from jarvis.agents import codex as codex_module
from jarvis.agents.base import AgentOpenError, SteerUnavailable, TokenUsage
from jarvis.agents.codex import (
    CODEX_MODELS,
    CodexAgentRunner,
    _error_text,
    _tool_call,
    codex_cli,
    codex_cli_version,
    codex_stored_login,
    ensure_login_home,
    mcp_config,
)
from jarvis.agents.session import AdapterSession
from jarvis.tasks.models import Task, TaskKind


def turn_on_slack(settings, server: str) -> None:
    """The `send_to_slack` plugin on, naming `server` as the subagents' Slack."""
    settings.ensure_dirs()
    plugins.write_config(settings, "send_to_slack", {"mcp_server": server})
    plugins.install(settings, "send_to_slack")


FIXTURES = Path(__file__).parent / "fixtures"
THREAD_ID = "01a0d55f-0000-7000-8000-000000000001"
KEY = "sk-codex-test-0123456789"


def notifications(name: str) -> list[Notification]:
    """A recorded turn, as the typed notifications the SDK would have handed us."""
    lines = (FIXTURES / f"codex_app_{name}.jsonl").read_text().splitlines()
    return [
        Notification(d["method"], NOTIFICATION_MODELS[d["method"]].model_validate(d["params"]))
        for d in map(json.loads, lines)
    ]


def notice(method: str, **params) -> Notification:
    return Notification(method, NOTIFICATION_MODELS[method].model_validate(params))


def make_task(**overrides) -> Task:
    values = {"id": 9, "kind": TaskKind.AGENT, "description": "add a notes file", "agent": "codex"}
    values.update(overrides)
    return Task(**values)


class FakeTurn:
    """Replays a turn; with `pause_at`, waits there for an interrupt, as a command does."""

    def __init__(self, notes, *, pause_at=None, steer_error=None):
        self.notes = list(notes)
        self.pause_at = pause_at
        self.steer_error = steer_error
        self.steered: list[str] = []
        self.interrupts = 0
        self.stream_closed = False
        self.paused = asyncio.Event()
        self._interrupted = asyncio.Event()

    async def stream(self):
        try:
            for index, note in enumerate(self.notes):
                if index == self.pause_at:
                    self.paused.set()
                    await self._interrupted.wait()
                yield note
        finally:
            self.stream_closed = True

    async def steer(self, text):
        if self.steer_error is not None:
            raise self.steer_error
        self.steered.append(text)

    async def interrupt(self):
        self.interrupts += 1
        self._interrupted.set()


class FakeThread:
    def __init__(self, turns, thread_id=THREAD_ID):
        self.id = thread_id
        self.turns = list(turns)
        self.prompts: list[str] = []

    async def turn(self, prompt):
        self.prompts.append(prompt)
        return self.turns.pop(0)


class FakeCodex:
    """One app-server's worth of recorded calls."""

    def __init__(self, *turns, fail=None, notes=()):
        self.thread = FakeThread(turns)
        self.fail = fail
        self.notes = list(notes)
        self.env: dict[str, str] = {}
        self.cwd: Path | None = None
        self.started: list[dict] = []
        self.resumed: list[tuple[str, dict]] = []
        self.closes = 0
        self._gone = asyncio.Event()

    async def thread_start(self, **options):
        self.started.append(options)
        if self.fail is not None:
            raise self.fail
        return self.thread

    async def thread_resume(self, thread_id, **options):
        self.resumed.append((thread_id, options))
        if self.fail is not None:
            raise self.fail
        return self.thread

    async def notices(self):
        for note in self.notes:
            yield note
        await self._gone.wait()

    async def close(self):
        self.closes += 1
        self._gone.set()


class Factory:
    def __init__(self, codex: FakeCodex):
        self.codex = codex
        self.calls = 0

    def __call__(self, env, cwd):
        self.calls += 1
        self.codex.env, self.codex.cwd = env, cwd
        return self.codex


def no_login(*args, **kwargs):  # pragma: no cover - a test that reaches it has failed
    raise AssertionError("no login expected")


async def open_session(settings, codex, *, task=None, resume=None, login=no_login):
    runner = CodexAgentRunner(settings, client_factory=Factory(codex), login=login)
    return await runner.open(task or make_task(), resume=resume)


def quiet(text):
    return None


# --------------------------------------------------------------------------- one turn


async def test_a_turn_maps_the_notifications_into_a_result(settings):
    codex = FakeCodex(FakeTurn(notifications("run")))
    session = await open_session(settings, codex)
    progress: list[str] = []

    result = await session.run("add the notes", on_progress=progress.append)

    assert result.ok is True
    assert result.session_id == THREAD_ID
    assert result.final_text.startswith("Command output: `hello`")
    assert result.spoken_summary == "Completed the command, file creation, and secret word lookup."
    assert result.restart_reason == "registers the new tool"
    assert result.cost_usd is None  # tokens, not dollars: a plan call has no price
    assert result.usage == TokenUsage(
        input_tokens=55831, output_tokens=303, cached_input_tokens=47744
    )
    assert progress == [
        "I’ll run the command, create the requested note file, then locate and call the "
        "probe server’s `secret_word` tool.",
        "[tool] shell \"/usr/bin/zsh -lc 'echo hello'\"",
        "[edit] add /work/orchard/NOTES.md",
        "[tool] mcp__probe__secret_word {}",
        result.final_text,
    ]
    assert codex.thread.prompts == ["add the notes"]


async def test_a_failed_turn_says_what_went_wrong_without_the_url(settings, caplog):
    caplog.set_level(logging.INFO, logger="jarvis.agents.session")
    session = await open_session(settings, FakeCodex(FakeTurn(notifications("failed"))))

    result = await session.run("go", on_progress=quiet)

    reason = "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    assert result.ok is False
    assert result.error == reason
    assert result.spoken_summary == f"The task failed: {reason}"
    assert "cf-ray" not in result.error and "request id" not in result.error
    retries = [r for r in caplog.records if "Reconnecting" in r.getMessage()]
    assert retries and all(r.levelno == logging.INFO for r in retries)
    [terminal] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert terminal.getMessage() == f"subagent: {reason}"


async def test_a_turn_the_runtime_ended_on_its_budget_is_an_ordinary_failure(settings):
    session = await open_session(settings, FakeCodex(FakeTurn(notifications("budget"))))

    result = await session.run("go", on_progress=quiet)

    assert result.ok is False
    assert result.error == "This session has used its budget."


async def test_an_interrupted_turn_is_reported_as_interrupted(settings):
    notes = notifications("interrupted")
    turn = FakeTurn(notes, pause_at=len(notes) - 1)  # mid-command, before turn/completed
    session = await open_session(settings, FakeCodex(turn))
    await session.interrupt()  # nothing running yet: a no-op

    running = asyncio.create_task(session.run("go", on_progress=quiet))
    await turn.paused.wait()
    await session.interrupt()
    result = await asyncio.wait_for(running, 1)

    assert turn.interrupts == 1
    assert result.ok is False
    assert result.error == "interrupted"
    # The last message was an announcement of work, not a result: it is never spoken.
    assert result.spoken_summary == "The task failed: interrupted"
    assert result.usage == TokenUsage(18316, 116, 11136)


async def test_cancelling_a_turn_blocked_in_its_reader_closes_the_stream(settings):
    notes = notifications("interrupted")
    turn = FakeTurn(notes, pause_at=len(notes) - 1)
    session = await open_session(settings, FakeCodex(turn))

    running = asyncio.create_task(session.run("go", on_progress=quiet))
    await turn.paused.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert turn.stream_closed is True


async def test_the_second_turn_of_a_session_runs_on_the_same_thread(settings):
    codex = FakeCodex(FakeTurn(notifications("run")), FakeTurn(notifications("steered")))
    session = await open_session(settings, codex)

    await session.run("first", on_progress=quiet)
    second = await session.run("second", on_progress=quiet)

    assert codex.thread.prompts == ["first", "second"]
    assert len(codex.started) == 1
    assert second.session_id == THREAD_ID


# --------------------------------------------------------------------------- steering


async def test_a_follow_up_goes_into_the_running_turn(settings):
    notes = notifications("steered")
    turn = FakeTurn(notes, pause_at=9)  # after the command, before the steer lands
    session = await open_session(settings, FakeCodex(turn))

    running = asyncio.create_task(session.run("go", on_progress=quiet))
    await turn.paused.wait()
    await session.send("Also include the word KIWI in your answer.")
    turn._interrupted.set()  # let the recorded turn play on
    result = await running

    assert turn.steered == ["Also include the word KIWI in your answer."]
    assert "KIWI" in result.final_text


async def test_there_is_nothing_to_steer_before_or_after_a_turn(settings):
    session = await open_session(settings, FakeCodex(FakeTurn(notifications("run"))))

    with pytest.raises(SteerUnavailable):
        await session.send("too early")
    await session.run("go", on_progress=quiet)
    with pytest.raises(SteerUnavailable):
        await session.send("too late")


async def test_a_steer_the_moment_the_turn_is_done_is_refused_not_sent(settings):
    """`Done` is yielded with the turn already forgotten: nothing is sent into a turn the
    server has closed, so the manager re-runs with the text instead."""
    turn = FakeTurn(notifications("run"))
    codex = FakeCodex(turn)
    runner = CodexAgentRunner(settings, client_factory=Factory(codex), login=no_login)
    adapter = await runner.connect(runner.context(make_task()), None)

    events = adapter.turn("go")
    async for event in events:
        if type(event).__name__ == "Done":
            with pytest.raises(SteerUnavailable):
                await adapter.steer("too late")
            break
    await events.aclose()
    await adapter.close()

    assert turn.steered == []


@pytest.mark.parametrize(
    "error",
    [
        InvalidRequestError(-32600, "no active turn to steer"),
        InternalRpcError(-32603, "busy", {"codexErrorInfo": "activeTurnNotSteerable"}),
    ],
)
async def test_a_steer_the_server_refused_was_never_delivered(settings, error):
    notes = notifications("steered")
    turn = FakeTurn(notes, pause_at=9, steer_error=error)
    session = await open_session(settings, FakeCodex(turn))

    running = asyncio.create_task(session.run("go", on_progress=quiet))
    await turn.paused.wait()
    with pytest.raises(SteerUnavailable):
        await session.send("more")
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_a_steer_that_failed_otherwise_is_not_unavailable(settings):
    notes = notifications("steered")
    turn = FakeTurn(notes, pause_at=9, steer_error=InternalRpcError(-32603, "pipe broke"))
    session = await open_session(settings, FakeCodex(turn))

    running = asyncio.create_task(session.run("go", on_progress=quiet))
    await turn.paused.wait()
    with pytest.raises(RuntimeError) as raised:
        await session.send("more")
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    assert not isinstance(raised.value, SteerUnavailable)


# ------------------------------------------------------------------ open, resume, close


async def test_a_new_task_starts_a_thread_with_the_instructions_and_the_model(settings, tmp_path):
    settings.owner_name = "Ada"
    codex = FakeCodex()

    await open_session(settings, codex, task=make_task(id=31, cwd=str(tmp_path), model="gpt-6-sol"))

    [options] = codex.started
    assert options["cwd"] == str(tmp_path)
    assert codex.cwd == tmp_path
    assert options["model"] == "gpt-6-sol"
    assert "dispatched on Ada's behalf" in options["developer_instructions"]
    assert "Jarvis-Task: 31" in options["developer_instructions"]
    assert options["config"] is None
    assert codex.resumed == []


@pytest.mark.parametrize(
    ("task_model", "codex_model", "expected"),
    [("gpt-5.6-terra", None, "gpt-5.6-terra"), ("", "gpt-5.5", "gpt-5.5"), ("", None, None)],
)
async def test_the_model_is_the_tasks_else_the_configured_else_codexs_own(
    settings, task_model, codex_model, expected
):
    settings.codex_model = codex_model
    codex = FakeCodex()

    await open_session(settings, codex, task=make_task(model=task_model))

    assert codex.started[0]["model"] == expected


async def test_a_resume_resumes_the_thread_with_the_same_options(settings, tmp_path):
    started, resumed = FakeCodex(), FakeCodex()
    task = make_task(cwd=str(tmp_path))

    await open_session(settings, started, task=task)
    session = await open_session(settings, resumed, task=task, resume="t-1")

    assert resumed.started == []
    assert resumed.resumed == [("t-1", started.started[0])]
    assert session._session_id == "t-1"


async def test_an_unknown_resume_id_fails_the_open_and_closes_the_app_server(settings):
    codex = FakeCodex(fail=InvalidRequestError(-32600, "no rollout found for thread id t-x"))

    with pytest.raises(AgentOpenError, match="no rollout found for thread id t-x"):
        await open_session(settings, codex, resume="t-x")

    assert codex.closes == 1


async def test_close_stops_the_app_server_and_is_safe_twice(settings):
    codex = FakeCodex(FakeTurn(notifications("run")))
    session = await open_session(settings, codex)

    await session.run("go", on_progress=quiet)
    await session.close()
    await session.close()

    assert codex.closes == 1


# ------------------------------------------------------------------------------ notices


async def test_warnings_that_reach_no_turn_are_logged_redacted(settings, caplog):
    settings.codex_access_token = "agent-access-token-0001"
    codex = FakeCodex(
        notes=[
            notice("configWarning", summary="unknown key", details="agent-access-token-0001"),
            notice("deprecationNotice", summary="old flag"),
            notice("warning", message="slow network"),
            notice("mcpServer/startupStatus/updated", name="google", status="failed",
                   error="uvx not found"),
            notice("mcpServer/startupStatus/updated", name="probe", status="ready"),
            notice("thread/status/changed", threadId=THREAD_ID, status={"type": "idle"}),
            # A shape this build does not know is skipped, not a crash.
            Notification("mcpServer/startupStatus/updated", SimpleNamespace()),
        ]
    )
    session = await open_session(settings, codex)
    for _ in range(20):
        await asyncio.sleep(0)
    await session.close()

    said = [r.getMessage() for r in caplog.records if r.name == "jarvis.agents.codex"]
    assert said == [
        "codex: unknown key ([redacted])",
        "codex: old flag",
        "codex: slow network",
        "codex: MCP server google did not start: uvx not found",
    ]


# ---------------------------------------------------------------------------------- MCP


async def test_google_is_handed_over_with_its_secrets_by_name(settings):
    settings.google_workspace_mcp = True
    settings.google_oauth_client_id = "client-id"
    settings.google_oauth_client_secret = "client-secret-value"
    codex = FakeCodex()

    await open_session(settings, codex)

    google = codex.started[0]["config"]["mcp_servers"]["google"]
    assert google["command"] == "uvx"
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in google["env_vars"]
    assert codex.env["GOOGLE_OAUTH_CLIENT_SECRET"] == "client-secret-value"
    assert "client-secret-value" not in json.dumps(codex.started[0])


async def test_the_slack_server_is_translated_from_the_claude_config(settings, monkeypatch):
    turn_on_slack(settings, "team-slack")
    monkeypatch.setattr(
        codex_module,
        "mcp_server_config",
        lambda name: {
            "command": "/bin/slack-mcp",
            "args": ["serve"],
            "env": {"SLACK_BOT_TOKEN": "xoxb-1"},
        },
    )
    codex = FakeCodex()

    await open_session(settings, codex)

    assert codex.started[0]["config"] == {
        "mcp_servers": {
            "team-slack": {
                "command": "/bin/slack-mcp",
                "args": ["serve"],
                "env_vars": ["SLACK_BOT_TOKEN"],
            }
        }
    }
    assert codex.env["SLACK_BOT_TOKEN"] == "xoxb-1"


async def test_a_named_slack_server_that_is_not_configured_is_left_out(settings, monkeypatch):
    turn_on_slack(settings, "team-slack")
    monkeypatch.setattr(codex_module, "mcp_server_config", lambda name: None)
    codex = FakeCodex()

    await open_session(settings, codex)

    assert codex.started[0]["config"] is None


def test_mcp_servers_codex_cannot_spell_or_run_are_skipped():
    config, env = mcp_config(
        {
            "has space": {"command": "x", "env": {"A": "1"}},
            "nothing": {"env": {"B": "2"}},
            "remote": {"url": "https://mcp.example/sse"},
        }
    )

    assert config == {"mcp_servers": {"remote": {"url": "https://mcp.example/sse"}}}
    assert env == {}


async def test_an_mcp_secret_is_kept_out_of_a_failed_turn(settings, monkeypatch):
    turn_on_slack(settings, "team-slack")
    token = "xoxb-0000000000-slack-bot-token"
    monkeypatch.setattr(
        codex_module,
        "mcp_server_config",
        lambda name: {"command": "slack-mcp", "env": {"SLACK_BOT_TOKEN": token}},
    )
    turn = FakeTurn([RuntimeError(f"stderr_tail=token {token} refused")])

    async def broken_stream():
        raise turn.notes[0]
        yield  # pragma: no cover

    turn.stream = broken_stream
    session = await open_session(settings, FakeCodex(turn))

    result = await session.run("go", on_progress=quiet)

    assert token not in result.error
    assert "[redacted]" in result.error


def test_a_provider_body_quoted_as_json_is_unwrapped_to_its_message():
    """A 400 arrives as the provider's JSON body in `message` (seen live, 2026-09-26)."""
    body = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'x\' model is not supported when using Codex with a ChatGPT account."}}'
    )
    error = SimpleNamespace(message=body, additional_details=None)

    assert _error_text(error) == (
        "The 'x' model is not supported when using Codex with a ChatGPT account."
    )
    assert _error_text(SimpleNamespace(message="[1, 2]", additional_details=None)) == "[1, 2]"


def test_every_kind_of_tool_call_is_a_bounded_progress_line():
    assert _tool_call(SimpleNamespace(type="webSearch", query="moon")).arguments == "moon"
    dynamic = SimpleNamespace(type="dynamicToolCall", namespace="fs", tool="read", arguments={})
    assert _tool_call(dynamic).name == "fs.read"
    bare = SimpleNamespace(type="dynamicToolCall", namespace=None, tool="read", arguments={})
    assert _tool_call(bare).name == "read"
    collab = SimpleNamespace(
        type="collabAgentToolCall", tool=SimpleNamespace(value="spawnAgent"), prompt="look"
    )
    assert _tool_call(collab).name == "agent.spawnAgent"
    assert _tool_call(SimpleNamespace(type="reasoning")) is None


# ------------------------------------------------------------------------------- auth


async def test_the_stored_login_blanks_every_credential_it_did_not_choose(settings):
    """OPENAI_API_KEY would move a ChatGPT-plan user onto per-token billing unasked."""
    settings.openai_api_key = "sk-voice-model"
    codex = FakeCodex()

    await open_session(settings, codex)

    assert codex.env == {"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": ""}


async def test_the_token_goes_in_the_environment_and_is_never_persisted(settings):
    settings.codex_access_token = "agent-access-token-0001"
    codex = FakeCodex()

    await open_session(settings, codex)

    assert codex.env["CODEX_ACCESS_TOKEN"] == "agent-access-token-0001"
    assert codex.env["CODEX_API_KEY"] == "" and codex.env["OPENAI_API_KEY"] == ""
    assert "CODEX_HOME" not in codex.env
    assert not (settings.data_dir / "codex").exists()


class LoginRecorder:
    def __init__(self, code=0, stderr="", *, delay=0.0, write=True):
        self.code = code
        self.stderr = stderr
        self.delay = delay
        self.write = write
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        time.sleep(self.delay)
        if self.code == 0 and self.write:
            (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").write_text("{}")
        return subprocess.CompletedProcess(argv, self.code, stdout="", stderr=self.stderr)


@pytest.fixture
def bundled(monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: "/venv/codex_cli_bin/bin/codex")


async def test_an_api_key_logs_in_once_into_a_home_of_jarvis_own(
    settings, tmp_path, monkeypatch, bundled
):
    settings.codex_api_key = KEY
    owner = tmp_path / "owner-codex"
    (owner / "skills").mkdir(parents=True)
    (owner / "config.toml").write_text('model = "x"\n')
    (owner / "hooks.json").write_text("{}")
    monkeypatch.setattr(codex_module, "codex_home", lambda: owner)
    login = LoginRecorder()
    first, second = FakeCodex(), FakeCodex()

    await open_session(settings, first, login=login)
    await open_session(settings, second, login=login)

    home = settings.data_dir / "codex"
    assert first.env["CODEX_HOME"] == second.env["CODEX_HOME"] == str(home)
    assert first.env["CODEX_API_KEY"] == ""  # the app-server ignores it; the login is it
    assert KEY not in json.dumps(first.env)
    assert len(login.calls) == 1
    call = login.calls[0]
    assert call["argv"] == ["/venv/codex_cli_bin/bin/codex", "login", "--with-api-key"]
    assert call["input"] == KEY  # on stdin, never argv
    assert call["env"]["CODEX_HOME"] == str(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".jarvis-login-sha256").stat().st_mode) == 0o600
    assert (home / "config.toml").resolve() == (owner / "config.toml").resolve()
    assert (home / "skills").is_symlink()
    assert not (home / "AGENTS.md").exists()  # the owner has none, so nothing to link
    assert not (home / "hooks.json").exists()  # the owner's automation stays theirs


def test_a_rotated_key_logs_in_again(settings, tmp_path, bundled):
    login = LoginRecorder()

    ensure_login_home(settings, "sk-first-0000", run=login, owner_home=tmp_path)
    ensure_login_home(settings, "sk-second-000", run=login, owner_home=tmp_path)
    ensure_login_home(settings, "sk-second-000", run=login, owner_home=tmp_path)

    assert [call["input"] for call in login.calls] == ["sk-first-0000", "sk-second-000"]


def test_two_tasks_opening_at_once_log_in_once(settings, tmp_path, bundled):
    login = LoginRecorder(delay=0.05)
    homes: list[Path] = []

    def open_one():
        homes.append(ensure_login_home(settings, KEY, run=login, owner_home=tmp_path))

    threads = [threading.Thread(target=open_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(login.calls) == 1
    assert homes[0] == homes[1]


@pytest.mark.parametrize(
    "login", [LoginRecorder(code=1, stderr=f"Error: bad key {KEY}\n"), LoginRecorder(write=False)]
)
async def test_a_refused_key_fails_the_open_without_quoting_it(settings, bundled, login):
    settings.codex_api_key = KEY

    with pytest.raises(AgentOpenError) as raised:
        await open_session(settings, FakeCodex(), login=login)

    assert KEY not in str(raised.value)
    assert "codex refused CODEX_API_KEY" in str(raised.value)
    assert not (settings.data_dir / "codex" / ".jarvis-login-sha256").exists()


def test_no_bundled_cli_is_a_clear_refusal(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: None)

    with pytest.raises(RuntimeError, match="bundled codex CLI is missing"):
        ensure_login_home(settings, KEY, run=no_login, owner_home=tmp_path)


# ------------------------------------------------------------------ the real SDK, no process


class PopenRecorder:
    """Stands in for `subprocess.Popen` inside the SDK: records the child's env and refuses."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        raise OSError("not in a test")


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("subscription", {"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": ""}),
        ("token", {"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": "agent-tok-1"}),
    ],
)
async def test_the_app_server_never_inherits_a_credential_jarvis_did_not_choose(
    settings, monkeypatch, tier, expected
):
    from openai_codex import client as sdk_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-voice-model-inherited")
    monkeypatch.setenv("CODEX_API_KEY", "sk-inherited-codex-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "inherited-token")
    if tier == "token":
        settings.codex_access_token = "agent-tok-1"
    popen = PopenRecorder()
    monkeypatch.setattr(sdk_client.subprocess, "Popen", popen)

    with pytest.raises(AgentOpenError, match="not in a test"):
        await CodexAgentRunner(settings).open(make_task())

    [call] = popen.calls
    env = call["env"]
    assert {name: env[name] for name in expected} == expected
    assert call["args"][1:] == ["app-server", "--listen", "stdio://"]
    assert call["args"][0] == codex_cli()


async def test_every_thread_runs_unattended_with_full_access(settings, monkeypatch, tmp_path):
    import openai_codex
    from openai_codex import ApprovalMode, Sandbox

    made: list[SimpleNamespace] = []

    class RecordingCodex:
        def __init__(self, config):
            self.config = config
            self.calls: list[tuple[str, tuple, dict]] = []
            made.append(self)

        async def thread_start(self, **kwargs):
            self.calls.append(("start", (), kwargs))
            return SimpleNamespace(id="t-1")

        async def thread_resume(self, thread_id, **kwargs):
            self.calls.append(("resume", (thread_id,), kwargs))
            return SimpleNamespace(id=thread_id)

        async def close(self):
            self.calls.append(("close", (), {}))

    monkeypatch.setattr(openai_codex, "AsyncCodex", RecordingCodex)
    client = codex_module.open_codex({"CODEX_API_KEY": ""}, tmp_path)

    await client.thread_start(cwd="/w")
    await client.thread_resume("t-9", cwd="/w")
    await client.close()

    [codex] = made
    assert codex.calls[1][2]["include_turns"] is False  # no deprecated full-history reply
    assert codex.config.env == {"CODEX_API_KEY": ""}
    assert codex.config.cwd == str(tmp_path)
    assert codex.config.codex_bin is None  # the bundled binary, never one on PATH
    for _, _, kwargs in codex.calls[:2]:
        assert kwargs["sandbox"] is Sandbox.full_access
        assert kwargs["approval_mode"] is ApprovalMode.deny_all
    assert codex.calls[1][1] == ("t-9",)


async def test_the_sdk_notices_end_when_the_app_server_is_gone(settings, tmp_path):
    client = codex_module.open_codex({}, tmp_path)
    queue = [notice("warning", message="hi"), RuntimeError("transport closed")]

    async def next_notification():
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    client._codex._client.next_notification = next_notification

    assert [n.method async for n in client.notices()] == ["warning"]


# ------------------------------------------------------------------ install and login


def test_the_codex_cli_is_the_one_the_sdk_bundles(monkeypatch):
    import codex_cli_bin

    assert codex_cli() == str(codex_cli_bin.bundled_codex_path())
    assert codex_cli_version() == "codex-cli 0.157.1"

    def missing():
        raise FileNotFoundError("no binary")

    monkeypatch.setattr(codex_cli_bin, "bundled_codex_path", missing)
    assert codex_cli() is None
    monkeypatch.setitem(sys.modules, "codex_cli_bin", None)
    assert codex_cli() is None


def test_no_cli_package_is_no_version(monkeypatch):
    def missing(name):
        raise codex_module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(codex_module.metadata, "version", missing)

    assert codex_cli_version() is None


def test_the_spoken_aliases_are_the_models_codex_offers():
    assert CODEX_MODELS == {
        "astra": "gpt-6-astra",
        "sol": "gpt-6-sol",
        "luna": "gpt-6-luna",
        "terra": "gpt-5.6-terra",
    }


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "expected"),
    [
        # Codex answers on stderr; stdout is checked too, in case that moves.
        (0, "", "Logged in using ChatGPT\n", True),
        (0, "Logged in using an API key - sk-…\n", "", True),
        (1, "", "Not logged in\n", False),
        (0, "", "Not logged in\n", False),
    ],
)
def test_the_stored_login_is_read_from_login_status(monkeypatch, code, stdout, stderr, expected):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: "/venv/bin/codex")

    def run(argv, **kwargs):
        assert argv == ["/venv/bin/codex", "login", "status"]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    assert codex_stored_login(run) is expected


def test_no_codex_is_no_login(monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: None)

    assert codex_stored_login(no_login) is False


def test_a_login_status_that_hangs_is_no_login(monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: "/venv/bin/codex")

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    assert codex_stored_login(hang) is False


async def test_the_session_is_the_shared_one(settings):
    assert isinstance(await open_session(settings, FakeCodex()), AdapterSession)
