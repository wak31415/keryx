"""Tests for the one-off Google OAuth bootstrap: a scripted stdio server, no subprocess."""

import json
import threading

import pytest

from jarvis.config import Settings
from jarvis.google_setup import (
    PROBE_TOOL,
    GoogleSetupError,
    run_google_setup,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        google_oauth_client_id="client-id",
        google_oauth_client_secret="client-secret",
        user_google_email="me@example.com",
    )


class FakeStdin:
    """The server's stdin: records every line the setup flow writes."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in "".join(self.lines).splitlines() if line.strip()]


class FakeStdout:
    """The server's stdout: hands out scripted lines, then EOF (or a stall, when blocking)."""

    #: How long a "blocking" fake server stalls before giving up, so no thread lingers.
    STALL_SECONDS = 2.0

    def __init__(self, lines: list[str], *, block: bool = False) -> None:
        self._lines = list(lines)
        self._block = block
        self._released = threading.Event()

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        if self._block:
            self._released.wait(self.STALL_SECONDS)
        return ""

    def close(self) -> None:
        self._released.set()


class FakeProcess:
    def __init__(self, stdout_lines: list[str], *, block: bool = False) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines, block=block)
        self.terminated = 0
        self.killed = 0
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def kill(self) -> None:  # pragma: no cover - only a hung server gets here
        self.killed += 1

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return self.returncode


def rpc(id_: int, result: object) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n"


def make_popen(process: FakeProcess, calls: list[dict]):
    def popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return process

    return popen


def scripted_process() -> FakeProcess:
    """A server that answers `initialize` and the probe tool call."""
    return FakeProcess(
        [
            "warming up\n",  # non-JSON server chatter is ignored
            rpc(1, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "workspace-mcp"}}),
            rpc(2, {"content": [{"type": "text", "text": "Calendar: primary"}]}),
        ]
    )


# --- refusals --------------------------------------------------------------


def test_missing_oauth_client_settings_refuse_to_start_the_server(settings):
    incomplete = settings.model_copy(update={"google_oauth_client_secret": None})
    calls: list[dict] = []

    with pytest.raises(GoogleSetupError) as excinfo:
        run_google_setup(incomplete, popen=make_popen(FakeProcess([]), calls))

    assert "GOOGLE_OAUTH_CLIENT_SECRET" in str(excinfo.value)
    assert calls == []


# --- the flow --------------------------------------------------------------


def test_the_server_is_started_with_the_workspace_mcp_command_and_env(settings):
    calls: list[dict] = []

    run_google_setup(settings, popen=make_popen(scripted_process(), calls))

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "uvx"
    assert argv[1:] == [
        "workspace-mcp",
        "--tools",
        "gmail",
        "calendar",
        "--transport",
        "stdio",
        "--single-user",
    ]
    env = calls[0]["env"]
    assert env["GOOGLE_OAUTH_CLIENT_ID"] == "client-id"
    assert env["GOOGLE_OAUTH_CLIENT_SECRET"] == "client-secret"
    assert env["GOOGLE_MCP_CREDENTIALS_DIR"] == str(settings.data_dir / "google")
    assert env["PATH"]  # the ambient environment is kept, not replaced


def test_the_handshake_is_initialize_then_initialized_then_the_probe_tool(settings):
    process = scripted_process()

    assert run_google_setup(settings, popen=make_popen(process, [])) is True

    methods = [message.get("method") for message in process.stdin.messages()]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    call = process.stdin.messages()[-1]
    assert call["params"]["name"] == PROBE_TOOL


def test_the_server_is_always_stopped_again(settings):
    process = scripted_process()

    run_google_setup(settings, popen=make_popen(process, []))

    assert process.terminated == 1


def test_a_server_that_dies_before_answering_fails_cleanly(settings):
    process = FakeProcess([])  # immediate EOF

    with pytest.raises(GoogleSetupError) as excinfo:
        run_google_setup(settings, popen=make_popen(process, []))

    assert "exited" in str(excinfo.value)
    assert process.terminated == 1


def test_a_slow_server_gives_up_at_the_timeout(settings):
    # Answers `initialize`, then stalls instead of answering the tool call.
    process = FakeProcess([rpc(1, {"protocolVersion": "2025-06-18"})], block=True)

    with pytest.raises(GoogleSetupError) as excinfo:
        run_google_setup(settings, popen=make_popen(process, []), timeout_s=0.2)

    assert "timed out" in str(excinfo.value).lower()
    assert process.terminated == 1


def test_a_tool_error_is_reported_as_a_failure(settings):
    process = FakeProcess(
        [
            rpc(1, {"protocolVersion": "2025-06-18"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "nope"}})
            + "\n",
        ]
    )

    with pytest.raises(GoogleSetupError) as excinfo:
        run_google_setup(settings, popen=make_popen(process, []))

    assert "nope" in str(excinfo.value)


def test_the_instructions_and_the_server_output_are_echoed(settings):
    printed: list[str] = []

    run_google_setup(settings, popen=make_popen(scripted_process(), []), echo=printed.append)

    output = "\n".join(printed)
    assert "browser" in output.lower()
    assert "Calendar: primary" in output
