"""Tests for the Codex backend.

`codex` is never started: the runner takes a `spawner`, and the double below replays the
JSONL fixtures in `fixtures/`, which keep the exact shapes Codex CLI 0.156 printed for real
runs. The one test that spawns a process at all runs a local Python script standing in for
the binary, to exercise the subprocess wrapper; no network, no `codex`.
"""

import asyncio
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.agents import codex as codex_module
from jarvis.agents.codex import (
    INTERRUPTED_ERROR,
    CodexAgentRunner,
    build_command,
    codex_stored_login,
    ensure_token_home,
    mcp_overrides,
    progress_line,
    spawn_codex,
)
from jarvis.tasks.models import Task, TaskKind

FIXTURES = Path(__file__).parent / "fixtures"
THREAD_ID = "01a0d55f-0000-7000-8000-000000000001"


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines(keepends=True)


def make_task(**overrides) -> Task:
    values = {"id": 9, "kind": TaskKind.AGENT, "description": "add a notes file", "agent": "codex"}
    values.update(overrides)
    return Task(**values)


class FakeProcess:
    """Replays `lines`; with `hangs`, then waits for an interrupt, as a turn mid-command does."""

    def __init__(self, lines=(), code=0, stderr="", *, hangs=False):
        self._lines = list(lines)
        self.code = code
        self.stderr = stderr
        self.interrupts = 0
        self.terminated = 0
        self._hangs = hangs
        self._interrupted = asyncio.Event()

    async def lines(self):
        for line in self._lines:
            yield line
        if self._hangs:
            await self._interrupted.wait()

    async def wait(self) -> int:
        return self.code

    def stderr_tail(self) -> str:
        return self.stderr

    def interrupt(self) -> None:
        self.interrupts += 1
        self._interrupted.set()

    async def terminate(self) -> None:
        self.terminated += 1


class FakeSpawner:
    """Hands out scripted processes and records what each would have been started with."""

    def __init__(self, *processes: FakeProcess, fail: Exception | None = None):
        self.processes = list(processes)
        self.fail = fail
        self.calls: list[dict] = []

    async def __call__(self, argv, cwd, env, prompt):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "prompt": prompt})
        if self.fail is not None:
            raise self.fail
        return self.processes.pop(0)


def no_login(*args, **kwargs):  # pragma: no cover - a test that reaches it has failed
    raise AssertionError("no login expected")


@pytest.fixture(autouse=True)
def no_codex_on_this_machine(monkeypatch):
    """Whatever is installed here, the command is spelt with the bare name."""
    monkeypatch.setattr(codex_module, "codex_cli", lambda: None)


async def open_session(settings, spawner, *, task=None, resume=None):
    runner = CodexAgentRunner(settings, spawner=spawner, login=no_login)
    return await runner.open(task or make_task(), resume=resume)


# --------------------------------------------------------------------------- one turn


async def test_a_turn_maps_the_events_into_a_result(settings):
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    session = await open_session(settings, spawner)
    progress: list[str] = []

    result = await session.run("add the notes", on_progress=progress.append)

    assert result.ok is True
    assert result.session_id == THREAD_ID
    assert result.spoken_summary == (
        "I added the notes file and checked the mail; nothing from the landlord."
    )
    assert result.restart_reason == "registers the new tool at startup"
    assert result.final_text.startswith("Added `NOTES.md`")
    assert result.cost_usd is None  # tokens, not dollars: a plan call has no price
    assert progress == [
        "I’ll look at the repository, then add the file.",
        '[tool] shell "/usr/bin/zsh -lc ls"',
        "[edit] add /work/orchard/NOTES.md",
        '[tool] mcp__google__search_gmail_messages {"query": "from:landlord"}',
        result.final_text,
    ]
    assert spawner.calls[0]["prompt"] == "add the notes"


async def test_a_failed_turn_says_what_the_provider_said(settings):
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_failed.jsonl"), code=1))
    session = await open_session(settings, spawner)

    result = await session.run("go", on_progress=lambda text: None)

    assert result.ok is False
    assert result.error == (
        "The 'no-such-model' model is not supported when using Codex with a ChatGPT account."
    )
    assert result.spoken_summary.startswith("The task failed: The 'no-such-model' model")
    assert result.session_id == "01a0d560-0000-7000-8000-000000000002"


async def test_a_refused_key_never_reaches_the_error_even_masked(settings):
    settings.codex_api_key = "sk-jarvis-test-fake"
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_bad_key.jsonl"), code=1))
    session = await open_session(settings, spawner)

    result = await session.run("go", on_progress=lambda text: None)

    assert result.ok is False
    assert "Incorrect API key provided: [redacted]" in result.error
    assert "sk-jarvi" not in result.error and "fake" not in result.error
    assert "sk-jarvi" not in result.spoken_summary


async def test_an_exit_no_event_explains_falls_back_to_stderr(settings):
    """What an unknown resume id looks like: nothing on stdout, one line on stderr."""
    error = "Error: thread/resume: thread/resume failed: no rollout found for thread id x"
    spawner = FakeSpawner(FakeProcess([], code=1, stderr=f"a warning\n{error}\n"))
    session = await open_session(settings, spawner, resume="x")

    result = await session.run("go on", on_progress=lambda text: None)

    assert result.ok is False
    assert result.error == error
    assert result.session_id == "x"


async def test_an_exit_with_nothing_at_all_still_says_something(settings):
    session = await open_session(settings, FakeSpawner(FakeProcess([], code=3)))

    result = await session.run("go", on_progress=lambda text: None)

    assert result.error == "codex exited with 3"


async def test_a_process_that_cannot_start_is_a_failed_turn_not_an_exception(settings):
    spawner = FakeSpawner(fail=FileNotFoundError("codex"))
    session = await open_session(settings, spawner)

    result = await session.run("go", on_progress=lambda text: None)

    assert result.ok is False
    assert result.error == "FileNotFoundError: codex"


async def test_noise_on_stdout_is_skipped(settings):
    lines = ["\n", "not json\n", "[1, 2]\n", *fixture_lines("codex_run.jsonl")]
    session = await open_session(settings, FakeSpawner(FakeProcess(lines)))

    result = await session.run("go", on_progress=lambda text: None)

    assert result.ok is True


async def test_a_failing_progress_callback_does_not_fail_the_turn(settings):
    def explode(text):
        raise RuntimeError("listener gone")

    session = await open_session(
        settings, FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    )

    assert (await session.run("go", on_progress=explode)).ok is True


# ------------------------------------------------------------------- resume, interrupt


async def test_the_second_turn_resumes_the_thread_the_first_one_started(settings):
    spawner = FakeSpawner(
        FakeProcess(fixture_lines("codex_run.jsonl")),
        FakeProcess(fixture_lines("codex_run.jsonl")),
    )
    session = await open_session(settings, spawner)

    await session.run("first", on_progress=lambda text: None)
    await session.run("second", on_progress=lambda text: None)

    first, second = (call["argv"] for call in spawner.calls)
    assert first[:2] == ["codex", "exec"] and "resume" not in first
    assert second[:4] == ["codex", "exec", "resume", THREAD_ID]


async def test_opening_with_a_session_id_resumes_it(settings, tmp_path):
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    session = await open_session(settings, spawner, task=make_task(cwd=str(tmp_path)), resume="t-1")

    await session.run("more", on_progress=lambda text: None)

    call = spawner.calls[0]
    assert call["argv"][:4] == ["codex", "exec", "resume", "t-1"]
    assert "-C" not in call["argv"]  # `exec resume` has no -C; the process cwd is the dir
    assert call["cwd"] == tmp_path


async def test_an_interrupted_turn_is_reported_as_interrupted(settings):
    process = FakeProcess(fixture_lines("codex_interrupted.jsonl"), code=1, hangs=True)
    session = await open_session(settings, FakeSpawner(process))
    await session.interrupt()  # nothing running yet: a no-op
    progress: list[str] = []

    run = asyncio.create_task(session.run("go", on_progress=progress.append))
    while len(progress) < 2:  # the announcement and the command: it is mid-command now
        await asyncio.sleep(0)
    await session.interrupt()
    result = await asyncio.wait_for(run, 1)

    assert process.interrupts == 1
    assert result.ok is False
    assert result.error == INTERRUPTED_ERROR
    # The last message was an announcement of work, not a result: it is never spoken.
    assert result.spoken_summary == "The task failed: interrupted"


async def test_close_terminates_the_process_and_is_safe_twice(settings):
    process = FakeProcess(fixture_lines("codex_run.jsonl"))
    session = await open_session(settings, FakeSpawner(process))
    await session.close()  # before any run

    await session.run("go", on_progress=lambda text: None)
    await session.close()
    await session.close()

    assert process.terminated == 2


async def test_close_swallows_a_terminate_that_fails(settings):
    process = FakeProcess(fixture_lines("codex_run.jsonl"))

    async def broken():
        raise ProcessLookupError

    process.terminate = broken
    session = await open_session(settings, FakeSpawner(process))
    await session.run("go", on_progress=lambda text: None)

    await session.close()


async def test_send_is_not_how_codex_takes_a_follow_up(settings):
    session = await open_session(settings, FakeSpawner())

    with pytest.raises(NotImplementedError):
        await session.send("and also")


# ------------------------------------------------------------------------ the command


def test_the_command_bypasses_approvals_and_reads_the_prompt_from_stdin(settings, tmp_path):
    argv, env, cwd = build_command(make_task(cwd=str(tmp_path)), settings)

    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[-1] == "-"
    assert cwd == tmp_path
    assert env == {}


def test_the_suffix_goes_in_as_developer_instructions(settings):
    settings.owner_name = "Ada"
    argv, _, _ = build_command(make_task(id=31), settings)

    [instructions] = [a for a in argv if a.startswith("developer_instructions=")]
    text = json.loads(instructions.removeprefix("developer_instructions="))
    assert "dispatched on Ada's behalf" in text
    assert "Jarvis-Task: 31" in text


@pytest.mark.parametrize(
    ("task_model", "codex_model", "expected"),
    [("gpt-5.6-terra", None, "gpt-5.6-terra"), ("", "gpt-5.5", "gpt-5.5"), ("", None, None)],
)
def test_the_model_flag_is_the_tasks_else_the_configured_else_none(
    settings, task_model, codex_model, expected
):
    settings.codex_model = codex_model
    argv, _, _ = build_command(make_task(model=task_model), settings)

    assert (argv[argv.index("-m") + 1] if "-m" in argv else None) == expected


def test_google_is_handed_over_as_an_mcp_server_with_its_secrets_by_name(settings):
    settings.google_workspace_mcp = True
    settings.google_oauth_client_id = "client-id"
    settings.google_oauth_client_secret = "client-secret-value"

    argv, env, _ = build_command(make_task(), settings)

    assert 'mcp_servers.google.command="uvx"' in argv
    names = next(a for a in argv if a.startswith("mcp_servers.google.env_vars="))
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in names
    assert env["GOOGLE_OAUTH_CLIENT_SECRET"] == "client-secret-value"
    assert not any("client-secret-value" in a for a in argv)


def test_the_slack_server_is_translated_from_the_claude_config(settings, monkeypatch):
    settings.slack_mcp_server = "team-slack"
    monkeypatch.setattr(
        codex_module,
        "mcp_server_config",
        lambda name: {
            "command": "/bin/slack-mcp",
            "args": ["serve"],
            "env": {"SLACK_BOT_TOKEN": "xoxb-1"},
        },
    )

    argv, env, _ = build_command(make_task(), settings)

    assert 'mcp_servers.team-slack.command="/bin/slack-mcp"' in argv
    assert 'mcp_servers.team-slack.args=["serve"]' in argv
    assert 'mcp_servers.team-slack.env_vars=["SLACK_BOT_TOKEN"]' in argv
    assert env == {"SLACK_BOT_TOKEN": "xoxb-1"}


def test_a_named_slack_server_that_is_not_configured_is_left_out(settings, monkeypatch):
    settings.slack_mcp_server = "team-slack"
    monkeypatch.setattr(codex_module, "mcp_server_config", lambda name: None)

    argv, _, _ = build_command(make_task(), settings)

    assert not any(a.startswith("mcp_servers.") for a in argv)


def test_mcp_servers_codex_cannot_spell_or_run_are_skipped():
    argv, env = mcp_overrides(
        {
            "has space": {"command": "x"},
            "nothing": {"env": {"A": "1"}},
            "remote": {"url": "https://mcp.example/sse"},
        }
    )

    assert argv == ["-c", 'mcp_servers.remote.url="https://mcp.example/sse"']
    assert env == {}


def test_only_new_tool_calls_and_finished_edits_are_progress():
    assert progress_line({"type": "command_execution", "command": "ls"}, started=False) is None
    assert progress_line({"type": "reasoning", "text": "hm"}, started=True) is None
    assert progress_line({"type": "web_search", "query": "moon"}, started=True) == (
        '[tool] web_search "moon"'
    )


# ------------------------------------------------------------------------------- auth


async def test_an_api_key_goes_to_the_child_environment_and_nowhere_else(settings):
    settings.codex_api_key = "sk-codex-secret"
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    session = await open_session(settings, spawner)

    await session.run("go", on_progress=lambda text: None)

    call = spawner.calls[0]
    assert call["env"]["CODEX_API_KEY"] == "sk-codex-secret"
    assert not any("sk-codex-secret" in a for a in call["argv"])


async def test_the_voice_models_key_is_never_borrowed(settings):
    """OPENAI_API_KEY would move a ChatGPT-plan user onto per-token billing unasked."""
    settings.openai_api_key = "sk-voice-model"
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    session = await open_session(settings, spawner)

    await session.run("go", on_progress=lambda text: None)

    assert "sk-voice-model" not in json.dumps(spawner.calls[0], default=str)
    assert "CODEX_API_KEY" not in spawner.calls[0]["env"]


class LoginRecorder:
    def __init__(self, code=0, stderr=""):
        self.code = code
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.code == 0:
            (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").write_text("{}")
        return subprocess.CompletedProcess(argv, self.code, stdout="", stderr=self.stderr)


def test_the_token_logs_in_once_into_a_home_of_jarvis_own(settings, tmp_path):
    owner = tmp_path / "owner-codex"
    (owner / "skills").mkdir(parents=True)
    (owner / "config.toml").write_text('model = "x"\n')
    login = LoginRecorder()

    home = ensure_token_home(settings, "agent-token", run=login, owner_home=owner)
    again = ensure_token_home(settings, "agent-token", run=login, owner_home=owner)

    assert home == again == settings.data_dir / "codex"
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert len(login.calls) == 1
    call = login.calls[0]
    assert call["argv"][1:] == ["login", "--with-access-token"]
    assert call["input"] == "agent-token"  # on stdin, never in argv
    assert not any("agent-token" in a for a in call["argv"])
    assert (home / "config.toml").resolve() == (owner / "config.toml").resolve()
    assert (home / "skills").is_symlink()
    assert not (home / "AGENTS.md").exists()  # the owner has none, so nothing to link


def test_a_rotated_token_logs_in_again(settings, tmp_path):
    login = LoginRecorder()

    ensure_token_home(settings, "first", run=login, owner_home=tmp_path)
    ensure_token_home(settings, "second", run=login, owner_home=tmp_path)

    assert [call["input"] for call in login.calls] == ["first", "second"]


def test_a_refused_token_raises_without_quoting_it(settings, tmp_path):
    login = LoginRecorder(code=1, stderr="Error logging in with access token: bad agent-token\n")

    with pytest.raises(RuntimeError) as raised:
        ensure_token_home(settings, "agent-token", run=login, owner_home=tmp_path)

    assert "agent-token" not in str(raised.value)
    assert "Error logging in with access token" in str(raised.value)


async def test_the_token_tier_points_every_run_at_that_home(settings, tmp_path, monkeypatch):
    settings.codex_access_token = "agent-token"
    monkeypatch.setattr(codex_module, "codex_home", lambda: tmp_path)
    spawner = FakeSpawner(FakeProcess(fixture_lines("codex_run.jsonl")))
    runner = CodexAgentRunner(settings, spawner=spawner, login=LoginRecorder())

    session = await runner.open(make_task())
    await session.run("go", on_progress=lambda text: None)

    env = spawner.calls[0]["env"]
    assert env["CODEX_HOME"] == str(settings.data_dir / "codex")
    assert "CODEX_ACCESS_TOKEN" not in env


@pytest.mark.parametrize(
    ("code", "stdout", "expected"),
    [(0, "Logged in using ChatGPT\n", True), (1, "Not logged in\n", False)],
)
def test_the_stored_login_is_read_from_login_status(monkeypatch, code, stdout, expected):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: "/usr/bin/codex")

    def run(argv, **kwargs):
        assert argv == ["/usr/bin/codex", "login", "status"]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    assert codex_stored_login(run) is expected


def test_no_codex_on_path_is_no_login(monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: None)

    assert codex_stored_login(no_login) is False


def test_a_login_status_that_hangs_is_no_login(monkeypatch):
    monkeypatch.setattr(codex_module, "codex_cli", lambda: "/usr/bin/codex")

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    assert codex_stored_login(hang) is False


# --------------------------------------------------------------- the real subprocess

STAND_IN = """
import json, signal, sys, time
signal.signal(signal.SIGINT, lambda *a: sys.exit(1))
prompt = sys.stdin.read()
sys.stderr.write("stand-in started\\n")
sys.stdout.write(open(sys.argv[1]).read())
message = {"type": "item.completed", "item": {"type": "agent_message", "text": "got: " + prompt}}
sys.stdout.write(json.dumps(message) + "\\n")
sys.stdout.flush()
if len(sys.argv) > 2:
    time.sleep(30)
"""


async def test_the_subprocess_wrapper_streams_stdout_and_keeps_stderr(tmp_path):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    argv = [sys.executable, str(script), str(FIXTURES / "codex_run.jsonl")]

    process = await spawn_codex(argv, tmp_path, {"EXTRA": "1"}, "hello")
    lines = [line async for line in process.lines()]
    code = await process.wait()

    assert code == 0
    assert json.loads(lines[0])["thread_id"] == THREAD_ID
    assert "got: hello" in lines[-1]
    assert process.stderr_tail() == "stand-in started"
    await process.terminate()  # already gone: a no-op


async def test_the_subprocess_wrapper_interrupts_and_terminates_the_group(tmp_path):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    argv = [sys.executable, str(script), str(FIXTURES / "codex_run.jsonl"), "linger"]

    process = await spawn_codex(argv, tmp_path, {}, "hello")
    async for line in process.lines():
        if "got: hello" in line:
            break
    process.interrupt()

    assert await asyncio.wait_for(process.wait(), 10) == 1


async def test_a_process_that_ignores_sigterm_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_module, "TERMINATE_GRACE_S", 0.2)
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdin.read()\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )

    process = await spawn_codex([sys.executable, str(script)], tmp_path, {}, "")
    async for _ in process.lines():
        break
    await asyncio.wait_for(process.terminate(), 10)

    assert await process.wait() != 0
