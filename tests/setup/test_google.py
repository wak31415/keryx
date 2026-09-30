"""Tests for the workspace-mcp sign-in: a scripted stdio server, no subprocess."""

import json
import threading
import time

import pytest

from keryx.config import Settings
from keryx.setup.google import (
    MCP_PROTOCOL_VERSION,
    PROBE_TOOL,
    SIGN_IN_INSTRUCTIONS,
    GoogleSetupError,
    run_google_setup,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "keryx",
        google_client_secrets_file=tmp_path / "no-client.json",
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

    assert "no Google OAuth client" in str(excinfo.value)
    assert calls == []


def test_a_client_file_is_as_good_as_the_pair(settings, tmp_path):
    """The old `setup-google` refused unless the id/secret pair was set, though everything
    else in Keryx took the downloaded JSON: one loader now, for both sign-ins."""
    client = tmp_path / "client.json"
    client.write_text('{"installed": {"client_id": "file-id", "client_secret": "file-secret"}}')
    from_file = settings.model_copy(
        update={
            "google_oauth_client_id": None,
            "google_oauth_client_secret": None,
            "google_client_secrets_file": client,
        }
    )
    calls: list[dict] = []

    run_google_setup(from_file, popen=make_popen(scripted_process(), calls))

    env = calls[0]["env"]
    assert (env["GOOGLE_OAUTH_CLIENT_ID"], env["GOOGLE_OAUTH_CLIENT_SECRET"]) == (
        "file-id",
        "file-secret",
    )


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


# --- waiting out the browser sign-in ---------------------------------------


AUTH_REQUIRED = "Authorization required. Visit https://accounts.google.com/o/oauth2/auth?x=1"


def auth_required_process(*, confirmed: bool = True) -> FakeProcess:
    """A server that answers the first probe with "authorize first", then (maybe) succeeds."""
    lines = [rpc(1, {"protocolVersion": MCP_PROTOCOL_VERSION}),
             rpc(2, {"content": [{"type": "text", "text": AUTH_REQUIRED}], "isError": True})]
    if confirmed:
        lines.append(rpc(3, {"content": [{"type": "text", "text": "Calendar: primary"}]}))
    return FakeProcess(lines, block=True)


def test_an_authorization_prompt_waits_for_the_browser_instead_of_giving_up(settings, tmp_path):
    """The consent URL is not a failure: the server has to stay alive for the sign-in."""
    credentials_dir = settings.data_dir / "google"
    credentials_dir.mkdir(parents=True)
    process = auth_required_process()
    printed: list[str] = []

    def sign_in(_seconds: float) -> None:  # the browser flow, finishing on the first poll
        (credentials_dir / "credentials.json").write_text("{}")

    assert run_google_setup(
        settings, popen=make_popen(process, []), echo=printed.append, sleep=sign_in
    ) is True

    output = "\n".join(printed)
    assert "https://accounts.google.com/o/oauth2/auth?x=1" in output  # the URL is shown
    assert SIGN_IN_INSTRUCTIONS in output  # ... with the "finish it in the browser" nudge
    methods = [message.get("method") for message in process.stdin.messages()]
    assert methods.count("tools/call") == 2  # probed again once credentials appeared
    assert process.terminated == 1  # ... and only stopped at the end


def test_a_sign_in_that_never_happens_fails_at_the_deadline(settings, tmp_path):
    (settings.data_dir / "google").mkdir(parents=True)
    process = auth_required_process(confirmed=False)

    with pytest.raises(GoogleSetupError) as excinfo:
        run_google_setup(
            settings,
            popen=make_popen(process, []),
            echo=lambda _text: None,
            timeout_s=0.2,
            sleep=lambda _seconds: time.sleep(0.01),
        )

    assert "credentials" in str(excinfo.value)
    assert process.terminated == 1


def test_stale_credentials_do_not_count_as_a_completed_sign_in(settings):
    """A file left by an earlier run is not proof that *this* sign-in finished."""
    credentials_dir = settings.data_dir / "google"
    credentials_dir.mkdir(parents=True)
    (credentials_dir / "credentials.json").write_text("{}")  # from last time
    process = auth_required_process(confirmed=False)

    with pytest.raises(GoogleSetupError):
        run_google_setup(
            settings,
            popen=make_popen(process, []),
            echo=lambda _text: None,
            timeout_s=0.2,
            sleep=lambda _seconds: time.sleep(0.01),
        )


def test_a_working_server_never_waits_for_a_sign_in(settings):
    """Nothing to authorize: no polling, no second probe."""
    process = scripted_process()
    slept: list[float] = []

    run_google_setup(
        settings, popen=make_popen(process, []), echo=lambda _text: None, sleep=slept.append
    )

    assert slept == []
    assert [m.get("method") for m in process.stdin.messages()].count("tools/call") == 1


def test_the_default_popen_is_resolved_when_it_is_called(settings, monkeypatch):
    """So that monkeypatching `subprocess.Popen` really does stop a subprocess starting."""
    spawned: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return scripted_process()

    monkeypatch.setattr("keryx.setup.google.subprocess.Popen", fake_popen)

    run_google_setup(settings, echo=lambda _text: None)

    assert spawned and spawned[0][0] == "uvx"
