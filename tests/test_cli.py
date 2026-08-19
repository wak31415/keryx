"""Tests for the `jarvis` command line: wiring only, no hardware and no network."""

import asyncio
import logging
import wave
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeProvider
from typer.testing import CliRunner

from jarvis.app import TASK_DB_NAME
from jarvis.cli import LOG_BACKUP_COUNT, LOG_MAX_BYTES, MAX_REPORT_CHARS, app
from jarvis.config import PLACEHOLDER_KEY, Settings
from jarvis.realtime.base import AudioDelta, Transcript
from jarvis.tasks.agent_runner import ClaudeAgentRunner, FakeAgentRunner
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

runner = CliRunner()


@pytest.fixture
def settings_stub(monkeypatch, tmp_path):
    """Make every command see a hermetic Settings instead of the ambient environment."""
    settings = Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis")
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)
    return settings


def write_wav(path: Path, milliseconds: int = 40) -> Path:
    samples = np.zeros(24000 * milliseconds // 1000, dtype="<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(samples.tobytes())
    return path


# --- help ------------------------------------------------------------------


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("serve", "loopback", "download-models", "tasks", "doctor", "setup-google"):
        assert command in result.output


def test_serve_help_documents_its_switches():
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    for option in ("--no-phone", "--no-wakeword", "--fake-agents", "--host", "--port"):
        assert option in result.output


def test_loopback_help_documents_its_switches():
    result = runner.invoke(app, ["loopback", "--help"])

    assert result.exit_code == 0
    for option in ("--wav", "--out", "--tail-seconds"):
        assert option in result.output


# --- serve -----------------------------------------------------------------


def test_serve_with_nothing_to_run_says_so(settings_stub):
    result = runner.invoke(app, ["serve", "--no-phone", "--no-wakeword"])

    assert result.exit_code == 0
    assert "nothing to run" in result.output.lower()


def stub_local_runner(monkeypatch, built: dict, *, run=None) -> None:
    """Replace the mic, the wake-word model and the runner with recording stubs."""

    class StubDevice:
        def __init__(self, **kwargs):
            built["device"] = self

    class StubDetector:
        def __init__(self, model_name):
            built["model"] = model_name

    class StubRunner:
        def __init__(self, settings, device, listener, **kwargs):
            built["runner"] = (settings, device, listener, kwargs)

        async def run(self):
            built["ran"] = True
            if run is not None:
                await run()

    monkeypatch.setattr("jarvis.cli.LocalAudioDevice", StubDevice)
    monkeypatch.setattr("jarvis.cli.OpenWakeWordDetector", StubDetector)
    monkeypatch.setattr("jarvis.cli.LocalRunner", StubRunner)


def stub_uvicorn(monkeypatch, built: dict) -> None:
    """Replace `uvicorn.Server` with a stub that records its config instead of listening."""

    class StubServer:
        def __init__(self, config):
            built["config"] = config
            self.should_exit = False
            built["server"] = self

        async def serve(self):
            built["served"] = True

    monkeypatch.setattr("jarvis.cli.uvicorn.Server", StubServer)


def test_serve_starts_the_local_runner(settings_stub, monkeypatch, tmp_path):
    built: dict = {}
    stub_local_runner(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-phone"])

    assert result.exit_code == 0, result.output
    assert built["ran"] is True
    assert built["model"] == settings_stub.wakeword_model
    settings, device, _listener, kwargs = built["runner"]
    assert settings is settings_stub
    assert device is built["device"]
    assert kwargs["sessions"] is not None
    assert (settings_stub.data_dir / "calls").is_dir()  # ensure_dirs() ran


def test_serve_runs_the_phone_server_on_the_configured_address(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0, result.output
    assert built["served"] is True
    config = built["config"]
    assert (config.host, config.port) == (settings_stub.host, settings_stub.port)
    assert config.log_level == settings_stub.log_level.lower()
    assert "/twilio/media" in {route.path for route in config.app.routes}


def test_serve_takes_the_address_from_the_command_line(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword", "--host", "0.0.0.0", "--port", "9999"])

    assert result.exit_code == 0, result.output
    config = built["config"]
    assert (config.host, config.port) == ("0.0.0.0", 9999)
    assert (settings_stub.host, settings_stub.port) == ("127.0.0.1", 8080)  # loaded settings intact


def test_serve_runs_the_phone_server_and_the_wake_word_on_one_shared_state(
    settings_stub, monkeypatch
):
    built: dict = {}

    async def run_forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            built["cancelled"] = True
            raise

    stub_local_runner(monkeypatch, built, run=run_forever)
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve"])  # the stub server returns straight away

    assert result.exit_code == 0, result.output
    assert built["served"] is True
    assert built["cancelled"] is True  # the runner is stopped when the server stops
    _settings, _device, _listener, kwargs = built["runner"]
    state = built["config"].app.state.jarvis
    assert kwargs["sessions"] is state.sessions
    assert kwargs["registry"] is state.registry
    assert kwargs["bus"] is state.bus
    assert kwargs["provider_factory"] is state.provider_factory


def test_serve_wires_the_task_stack_into_the_shared_state(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0, result.output
    state = built["config"].app.state.jarvis
    assert state.manager is not None
    assert isinstance(state.manager._runner, ClaudeAgentRunner)
    assert "dispatch_task" in {schema["name"] for schema in state.registry.schemas()}
    assert state.store._conn is None  # the store is closed again when serve returns


def test_fake_agents_swaps_the_subagent_runner(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword", "--fake-agents"])

    assert result.exit_code == 0, result.output
    state = built["config"].app.state.jarvis
    assert isinstance(state.manager._runner, FakeAgentRunner)
    assert settings_stub.fake_agents is False  # the loaded settings are left alone


# --- loopback --------------------------------------------------------------


def test_loopback_runs_a_session_and_writes_the_reply(settings_stub, monkeypatch, tmp_path):
    provider = FakeProvider()
    provider.feed(AudioDelta(item_id="item_1", audio=b"\x01\x02" * 240))
    provider.feed(Transcript(role="assistant", text="hello there", item_id="item_1"))
    monkeypatch.setattr("jarvis.cli.OpenAIRealtimeClient", lambda *args, **kwargs: provider)
    wav_in = write_wav(tmp_path / "in.wav")
    out = tmp_path / "reply.wav"

    result = runner.invoke(
        app,
        ["loopback", "--wav", str(wav_in), "--out", str(out), "--tail-seconds", "0.04"],
    )

    assert result.exit_code == 0, result.output
    assert provider.sent_audio  # the wav reached the model
    with wave.open(str(out), "rb") as reply:
        assert reply.getframerate() == 24000
        assert reply.readframes(reply.getnframes()) == b"\x01\x02" * 240
    assert "assistant: hello there" in result.output


def test_loopback_rejects_a_missing_wav(settings_stub, tmp_path):
    result = runner.invoke(app, ["loopback", "--wav", str(tmp_path / "nope.wav")])

    assert result.exit_code != 0


# --- logging ---------------------------------------------------------------


def test_serve_writes_a_rotating_log_file(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0, result.output
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1  # re-running serve replaces the handler, it does not stack
    assert Path(handlers[0].baseFilename) == settings_stub.data_dir / "logs" / "jarvis.log"
    assert handlers[0].maxBytes == LOG_MAX_BYTES
    assert handlers[0].backupCount == LOG_BACKUP_COUNT


# --- tasks -----------------------------------------------------------------


def seed_tasks(settings: Settings, *tasks: Task) -> list[Task]:
    """Write `tasks` into the store the CLI will open, and hand back their stored copies."""
    settings.ensure_dirs()

    async def store_them() -> list[Task]:
        store = TaskStore(settings.data_dir / TASK_DB_NAME)
        created = [await store.create(task) for task in tasks]
        await store.close()
        return created

    return asyncio.run(store_them())


def make_task(description: str = "look something up", **overrides) -> Task:
    fields = {"id": None, "kind": TaskKind.RESEARCH, "description": description, **overrides}
    return Task(**fields)


def test_tasks_list_prints_a_row_per_task(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("summarise the inbox", status=TaskStatus.DONE),
        make_task("add a README", kind=TaskKind.CODING, status=TaskStatus.RUNNING),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "summarise the inbox" in result.output
    assert "add a README" in result.output
    running_row = next(line for line in lines if "add a README" in line)
    assert running_row.split()[:3] == ["2", "running", "coding"]


def test_tasks_list_filters_by_status(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("done thing", status=TaskStatus.DONE),
        make_task("running thing", status=TaskStatus.RUNNING),
    )

    result = runner.invoke(app, ["tasks", "list", "--status", "running"])

    assert result.exit_code == 0, result.output
    assert "running thing" in result.output
    assert "done thing" not in result.output


def test_tasks_list_honours_the_limit(settings_stub):
    seed_tasks(settings_stub, make_task("first"), make_task("second"), make_task("third"))

    result = runner.invoke(app, ["tasks", "list", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "third" in result.output  # newest first
    assert "first" not in result.output


def test_tasks_list_shortens_a_long_description(settings_stub):
    seed_tasks(settings_stub, make_task("x" * 200))

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    assert "x" * 200 not in result.output
    assert "x" * 40 in result.output


def test_tasks_list_says_when_there_is_nothing(settings_stub):
    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    assert "no tasks" in result.output.lower()


def test_tasks_list_rejects_an_unknown_status(settings_stub):
    result = runner.invoke(app, ["tasks", "list", "--status", "sideways"])

    assert result.exit_code != 0


def test_tasks_show_prints_every_field_and_the_report(settings_stub, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Findings\n\nEverything is fine.\n")
    seed_tasks(
        settings_stub,
        make_task(
            "check the logs",
            status=TaskStatus.DONE,
            project="jarvis",
            summary="Nothing on fire.",
            report_path=str(report),
            claude_session_id="sess-42",
        ),
    )

    result = runner.invoke(app, ["tasks", "show", "1"])

    assert result.exit_code == 0, result.output
    for expected in (
        "check the logs",
        "done",
        "jarvis",
        "Nothing on fire.",
        "sess-42",
        str(report),
        "--- report ---",
        "Everything is fine.",
    ):
        assert expected in result.output


def test_tasks_show_truncates_a_huge_report(settings_stub, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("y" * (MAX_REPORT_CHARS + 500))
    seed_tasks(settings_stub, make_task("big one", report_path=str(report)))

    result = runner.invoke(app, ["tasks", "show", "1"])

    assert result.exit_code == 0, result.output
    assert "truncated" in result.output
    assert "y" * MAX_REPORT_CHARS in result.output
    assert "y" * (MAX_REPORT_CHARS + 1) not in result.output


def test_tasks_show_tolerates_a_report_that_is_gone(settings_stub, tmp_path):
    seed_tasks(settings_stub, make_task("stale", report_path=str(tmp_path / "gone.md")))

    result = runner.invoke(app, ["tasks", "show", "1"])

    assert result.exit_code == 0, result.output
    assert "--- report ---" not in result.output


def test_tasks_show_rejects_an_unknown_id(settings_stub):
    result = runner.invoke(app, ["tasks", "show", "77"])

    assert result.exit_code == 1
    assert "77" in result.output


def test_tasks_help_documents_the_subcommands(settings_stub):
    result = runner.invoke(app, ["tasks", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "show" in result.output


def test_tasks_list_help_documents_its_switches(settings_stub):
    result = runner.invoke(app, ["tasks", "list", "--help"])

    assert result.exit_code == 0
    assert "--status" in result.output
    assert "--limit" in result.output


# --- settings for the read-only commands -----------------------------------


def optional_key_loader(monkeypatch, tmp_path, calls: list[dict]) -> Settings:
    """Stub `load_settings` the way a machine with no `OPENAI_API_KEY` behaves."""

    def load(**overrides):
        calls.append(overrides)
        return Settings(_env_file=None, data_dir=tmp_path / "jarvis", **overrides)

    monkeypatch.setattr("jarvis.cli.load_settings", load)


def test_read_only_commands_run_without_an_openai_key(monkeypatch, tmp_path):
    calls: list[dict] = []
    optional_key_loader(monkeypatch, tmp_path, calls)

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    assert calls == [{}, {"openai_api_key": PLACEHOLDER_KEY}]


# --- doctor ----------------------------------------------------------------


@pytest.fixture
def wakeword_models(monkeypatch, tmp_path):
    """Point the wake-word check at a directory instead of importing openwakeword."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr("jarvis.doctor._wakeword_models_dir", lambda: models)
    return models


def test_doctor_fails_when_the_install_is_incomplete(settings_stub, wakeword_models):
    result = runner.invoke(app, ["doctor", "--no-mic"])

    assert result.exit_code == 1
    assert "❌" in result.output
    assert "ANTHROPIC_API_KEY" in result.output


def test_doctor_reports_a_missing_openai_key_instead_of_crashing(
    monkeypatch, tmp_path, wakeword_models
):
    optional_key_loader(monkeypatch, tmp_path, [])

    result = runner.invoke(app, ["doctor", "--no-mic"])

    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_doctor_passes_on_a_complete_install(monkeypatch, tmp_path, wakeword_models):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    (wakeword_models / "hey_jarvis_v0.1.onnx").write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant",
        twilio_account_sid="AC1",
        twilio_auth_token="token",
        twilio_number="+15550000000",
        allowed_callers=["+15551234567"],
        pin="1234",
        public_host="jarvis.ngrok.app",
        data_dir=tmp_path / "jarvis",
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)

    result = runner.invoke(app, ["doctor", "--no-mic"])

    assert result.exit_code == 0, result.output
    assert "❌" not in result.output


def test_doctor_help_documents_no_mic():
    result = runner.invoke(app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "--no-mic" in result.output


# --- setup-google ----------------------------------------------------------


def test_setup_google_refuses_without_an_oauth_client(settings_stub, monkeypatch):
    def no_subprocesses(*args, **kwargs):  # pragma: no cover - the point is it never runs
        raise AssertionError("setup-google must not start a server without credentials")

    monkeypatch.setattr("jarvis.google_setup.subprocess.Popen", no_subprocesses)

    result = runner.invoke(app, ["setup-google"])

    assert result.exit_code == 1
    assert "GOOGLE_OAUTH_CLIENT_ID" in result.output


def test_setup_google_runs_the_flow_and_prints_what_it_says(settings_stub, monkeypatch):
    seen: list[Settings] = []

    def fake_setup(settings, *, echo, **kwargs):
        seen.append(settings)
        echo("open this: https://accounts.google.com/o/oauth2/auth")
        return True

    monkeypatch.setattr("jarvis.cli.run_google_setup", fake_setup)

    result = runner.invoke(app, ["setup-google"])

    assert result.exit_code == 0, result.output
    assert seen == [settings_stub]
    assert "https://accounts.google.com/o/oauth2/auth" in result.output


def test_setup_google_help():
    result = runner.invoke(app, ["setup-google", "--help"])

    assert result.exit_code == 0


def test_download_models_help():
    result = runner.invoke(app, ["download-models", "--help"])

    assert result.exit_code == 0
