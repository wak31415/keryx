"""Tests for the `jarvis` command line: wiring only, no hardware and no network."""

import asyncio
import json
import logging
import shutil
import wave
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeProvider
from typer.testing import CliRunner

from jarvis.app import TASK_DB_NAME
from jarvis.approvals.broker import AUDIT_NAME, KILL_SWITCH_NAME, STATE_DIR_NAME
from jarvis.cli import (
    APPROVALS_EMPTY,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    MAX_REPORT_CHARS,
    app,
)
from jarvis.config import PLACEHOLDER_KEY, Settings
from jarvis.continuity.memory import memory_path
from jarvis.realtime.base import AudioDelta, Transcript
from jarvis.restart.service import ServiceTarget
from jarvis.restart.store import RECORD_NAME, RestartRecord, RestartStore
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
    for command in (
        "serve",
        "loopback",
        "download-models",
        "tasks",
        "doctor",
        "setup-google",
        "restart",
    ):
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
    # As if on a Mac with its packages installed, whatever host runs the suite.
    monkeypatch.setattr("jarvis.cli.wakeword_unavailable", lambda: None)


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


def test_serve_where_the_wake_word_cannot_run_serves_the_phone_alone(
    settings_stub, monkeypatch
):
    """`jarvis serve` on Linux: one line saying so, not a traceback after the server is up."""
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    monkeypatch.setattr("jarvis.cli.wakeword_unavailable", lambda: "the wake word needs macOS")
    monkeypatch.setattr(
        "jarvis.cli.OpenWakeWordDetector",
        lambda *a, **k: pytest.fail("the wake word was started anyway"),
    )

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "the wake word needs macOS; serving the phone channel only" in result.output
    assert built["served"] is True
    assert "Traceback" not in result.output


def test_serve_with_no_phone_where_the_wake_word_cannot_run_has_nothing_to_run(
    settings_stub, monkeypatch
):
    monkeypatch.setattr("jarvis.cli.wakeword_unavailable", lambda: "the wake word needs macOS")

    result = runner.invoke(app, ["serve", "--no-phone"])

    assert result.exit_code == 1
    assert "nothing to run" in result.output
    assert "the wake word needs macOS" in result.output


def test_serve_no_wakeword_does_not_mention_the_platform(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    monkeypatch.setattr("jarvis.cli.wakeword_unavailable", lambda: "the wake word needs macOS")

    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0, result.output
    assert "macOS" not in result.output


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
    fields = {"id": None, "kind": TaskKind.AGENT, "description": description, **overrides}
    return Task(**fields)


def test_tasks_list_prints_a_row_per_task(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("summarise the inbox", status=TaskStatus.DONE),
        make_task("add a README", status=TaskStatus.RUNNING),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "summarise the inbox" in result.output
    assert "add a README" in result.output
    running_row = next(line for line in lines if "add a README" in line)
    assert running_row.split()[:2] == ["2", "running"]


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


# --- restart ---------------------------------------------------------------


@pytest.fixture
def restart_settings(monkeypatch, tmp_path):
    """Settings with somebody to call back, and no real service manager anywhere near."""
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        owner_number_explicit="+15550000001",
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)
    monkeypatch.setattr("jarvis.cli.loaded_version", lambda data_dir, repo=None: "v-test")
    monkeypatch.setattr(
        "jarvis.cli.resolve_target", lambda _settings: ServiceTarget("systemd", "jarvis.service")
    )
    monkeypatch.setattr("jarvis.cli.health_probe", lambda _settings: 0)
    # `resolve_target` is stubbed above, but `watch_command` still looks for `systemd-run`
    # on PATH — and on a macOS runner it is not there, so the watchdog silently did not arm
    # and this fixture failed on the host rather than on anything it was testing.
    real_which = shutil.which

    def which(name, *args, **kwargs):
        if name == "systemd-run":
            return "/usr/bin/systemd-run"
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    return settings


@pytest.fixture
def ran(monkeypatch):
    """Records the restart command instead of running it; `returncode` is settable."""
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return Done

    monkeypatch.setattr("jarvis.cli.subprocess.run", fake_run)
    # The watchdog deliberately starts a process that outlives its parent, which is the
    # one thing a test suite must never do (see the testing rule in CLAUDE.md).
    monkeypatch.setattr("jarvis.cli.spawn_watchdog", lambda plan, settings: 4321)
    return calls, Done


def test_restart_asks_the_service_manager_and_records_the_call_back(restart_settings, ran):
    calls, _ = ran

    result = runner.invoke(app, ["restart", "--reason", "new code"])

    assert result.exit_code == 0
    assert calls == [["systemctl", "--user", "restart", "jarvis.service"]]
    record = RestartStore(restart_settings.data_dir / RECORD_NAME).load()
    assert record.reason == "new code"
    assert record.number == "+15550000001"
    assert record.origin_channel == "cli"
    assert "…0001" in result.output  # the number is masked where it is printed
    # Armed before the hand-over, and on the record, because this command has just
    # promised him a call and nothing else would notice if it never came.
    assert "4321" in record.watchdog
    assert "watchdog:" in result.output


def test_a_quiet_restart_arms_no_watchdog(restart_settings, ran):
    """`--no-callback` promises nothing, so there is nothing to notice the absence of."""
    calls, _ = ran

    runner.invoke(app, ["restart", "--no-callback"])

    record = RestartStore(restart_settings.data_dir / RECORD_NAME).load()
    assert record.watchdog == ""
    assert calls == [["systemctl", "--user", "restart", "jarvis.service"]]


def test_restart_refuses_to_cut_off_a_live_call(restart_settings, ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setattr("jarvis.cli.health_probe", lambda _settings: 1)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert "cuts them off" in result.output
    assert calls == []
    assert not (restart_settings.data_dir / RECORD_NAME).exists()


def test_restart_force_goes_ahead_anyway(restart_settings, ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setattr("jarvis.cli.health_probe", lambda _settings: 1)

    result = runner.invoke(app, ["restart", "--force"])

    assert result.exit_code == 0
    assert calls


def test_restart_without_a_service_manager_says_how_to_install_one(
    restart_settings, ran, monkeypatch
):
    calls, _ = ran
    monkeypatch.setattr("jarvis.cli.resolve_target", lambda _settings: None)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert "install-systemd.sh" in result.output
    assert calls == []


def test_restart_no_callback_leaves_no_number(restart_settings, ran):
    result = runner.invoke(app, ["restart", "--no-callback"])

    assert result.exit_code == 0
    assert RestartStore(restart_settings.data_dir / RECORD_NAME).load().number is None
    assert "no call back was asked for" in result.output


def test_a_restart_command_that_fails_says_so_and_keeps_the_record(restart_settings, ran):
    _, done = ran
    done.returncode = 3

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    record = RestartStore(restart_settings.data_dir / RECORD_NAME).load()
    assert record.state == "failed"
    assert "exited 3" in record.error


def test_restart_status_with_nothing_on_record(restart_settings):
    result = runner.invoke(app, ["restart", "--status"])

    assert result.exit_code == 0
    assert "no restart on record" in result.output


def test_restart_status_reads_back_a_failure(restart_settings):
    store = RestartStore(restart_settings.data_dir / RECORD_NAME)
    store.save(
        RestartRecord(
            requested_at="2026-08-24T10:00:00+00:00",
            reason="new code",
            number="+15550000001",
            state="failed",
            error="twilio would not take the call",
        )
    )

    result = runner.invoke(app, ["restart", "--status"])

    assert result.exit_code == 0
    assert "failed" in result.output
    assert "twilio would not take the call" in result.output
    assert "+15550000001" not in result.output  # masked, even here


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
    assert "Twilio credentials" in result.output


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
        pin="123456",
        public_host="jarvis.example.com",
        data_dir=tmp_path / "jarvis",
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)

    result = runner.invoke(app, ["doctor", "--no-mic"])

    assert result.exit_code == 0, result.output
    assert "❌" not in result.output


def test_doctor_still_runs_and_explains_a_malformed_pin(monkeypatch, tmp_path, wakeword_models):
    """`doctor` is the command that has to work when nothing else does.

    A `JARVIS_PIN` that breaks the 6-8 digit rule stops `jarvis serve` from loading at all,
    so if it stopped `doctor` too there would be nothing left to diagnose it with.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    (wakeword_models / "hey_jarvis_v0.1.onnx").write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")

    def load(**overrides):
        # What the real loader does with JARVIS_PIN=1234 in the environment: refuse, until
        # the caller says what to put there instead.
        values = {
            "openai_api_key": "sk-test",
            "anthropic_api_key": "sk-ant",
            "twilio_account_sid": "AC1",
            "twilio_auth_token": "token",
            "twilio_number": "+15550000000",
            "allowed_callers": ["+15551234567"],
            "public_host": "jarvis.example.com",
            "data_dir": tmp_path / "jarvis",
            "pin": "9876",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    monkeypatch.setattr("jarvis.cli.load_settings", load)

    result = runner.invoke(app, ["doctor", "--no-mic"])

    assert result.exit_code == 1, result.output
    assert "JARVIS_PIN" in result.output
    assert "6 to 8 digits" in result.output
    pin_line = next(line for line in result.output.splitlines() if "PIN:" in line)
    assert "9876" not in pin_line
    # Everything else was still checked rather than lost to the exception.
    assert "allowed callers" in result.output


def test_serve_refuses_to_start_on_a_malformed_pin(monkeypatch, tmp_path):
    """The strict gate. A bad PIN must stop the thing that answers the phone."""
    monkeypatch.chdir(tmp_path)

    def load(**overrides):
        return Settings(_env_file=None, openai_api_key="sk-test", data_dir=tmp_path, pin="9876")

    monkeypatch.setattr("jarvis.cli.load_settings", load)

    result = runner.invoke(app, ["serve", "--no-phone", "--no-wakeword"])

    assert result.exit_code == 2, result.output
    assert "JARVIS_PIN" in result.output
    assert "6 to 8 digits" in result.output
    assert "9876" not in result.output
    assert "doctor" in result.output


# --- approvals -------------------------------------------------------------


def _audit(settings, *entries) -> None:
    path = settings.data_dir / STATE_DIR_NAME / AUDIT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")


def test_approvals_says_when_there_is_nothing_yet(settings_stub):
    result = runner.invoke(app, ["approvals"])

    assert result.exit_code == 0, result.output
    assert APPROVALS_EMPTY in result.output
    assert "escalation: on" in result.output


def test_approvals_prints_the_audit_trail(settings_stub):
    _audit(
        settings_stub,
        {"ts": "2026-09-02T14:05:00Z", "event": "raised", "request_id": 7,
         "summary": "Claude is asking to run pytest"},
        {"ts": "2026-09-02T14:11:00Z", "event": "settled", "request_id": 7, "answer": "approve"},
    )

    result = runner.invoke(app, ["approvals"])

    assert result.exit_code == 0, result.output
    assert "2026-09-02 14:05" in result.output
    assert "raised" in result.output and "settled" in result.output
    assert "run pytest" in result.output


def test_approvals_ignores_a_corrupt_audit_line_rather_than_failing(settings_stub):
    path = settings_stub.data_dir / STATE_DIR_NAME / AUDIT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not json\n{"ts": "2026-09-02T14:05:00Z", "event": "raised"}\n')

    result = runner.invoke(app, ["approvals"])

    assert result.exit_code == 0, result.output
    assert "raised" in result.output


def test_approvals_limit_shows_only_the_tail(settings_stub):
    _audit(settings_stub, *[
        {"ts": f"2026-09-02T14:{index:02d}:00Z", "event": "raised", "request_id": index}
        for index in range(10)
    ])

    result = runner.invoke(app, ["approvals", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "14:09" in result.output
    assert "14:00" not in result.output


def test_the_kill_switch_is_a_file_so_it_needs_no_restart(settings_stub):
    switch = settings_stub.data_dir / STATE_DIR_NAME / KILL_SWITCH_NAME

    assert runner.invoke(app, ["approvals", "--disable"]).exit_code == 0
    assert switch.exists()
    assert "OFF (kill switch)" in runner.invoke(app, ["approvals"]).output

    assert runner.invoke(app, ["approvals", "--enable"]).exit_code == 0
    assert not switch.exists()
    assert "escalation: on" in runner.invoke(app, ["approvals"]).output


def test_approvals_refuses_both_switches_at_once(settings_stub):
    result = runner.invoke(app, ["approvals", "--disable", "--enable"])

    assert result.exit_code == 2


# --- forget ----------------------------------------------------------------


def test_forget_asks_before_deleting_anything(settings_stub, monkeypatch):
    settings_stub.ensure_dirs()
    (settings_stub.data_dir / "calls" / "abc.log").write_text("user: hello")

    result = runner.invoke(app, ["forget"], input="n\n")

    assert result.exit_code == 1  # typer.confirm(abort=True)
    assert (settings_stub.data_dir / "calls" / "abc.log").exists()


def test_forget_deletes_transcripts_when_confirmed(settings_stub):
    settings_stub.ensure_dirs()
    (settings_stub.data_dir / "calls" / "abc.log").write_text("user: hello")

    result = runner.invoke(app, ["forget", "--yes"])

    assert result.exit_code == 0, result.output
    assert not (settings_stub.data_dir / "calls" / "abc.log").exists()
    assert "removed 1 transcript" in result.output


def test_forget_leaves_task_rows_alone_when_asked(settings_stub):
    settings_stub.ensure_dirs()
    (settings_stub.data_dir / "calls" / "abc.log").write_text("user: hello")

    result = runner.invoke(app, ["forget", "--transcripts-only", "--yes"])

    assert result.exit_code == 0, result.output
    assert "transcript" in result.output


def test_forget_refuses_both_only_flags(settings_stub):
    result = runner.invoke(app, ["forget", "--transcripts-only", "--tasks-only", "--yes"])

    assert result.exit_code == 2


def test_forget_keeps_a_window_when_one_is_given(settings_stub):
    """`--older-than 30` must not take a transcript written this morning."""
    settings_stub.ensure_dirs()
    (settings_stub.data_dir / "calls" / "today.log").write_text("user: hello")

    result = runner.invoke(app, ["forget", "--older-than", "30", "--yes"])

    assert result.exit_code == 0, result.output
    assert (settings_stub.data_dir / "calls" / "today.log").exists()


def test_doctor_help_documents_no_mic():
    result = runner.invoke(app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "--no-mic" in result.output


# --- setup-google ----------------------------------------------------------


def test_setup_google_refuses_without_an_oauth_client(settings_stub, monkeypatch):
    spawned: list[object] = []
    # Recorded, not raised: an exception here would *also* exit 1 and hide the difference
    # between "refused" and "started a server and then blew up".
    monkeypatch.setattr(
        "jarvis.google_setup.subprocess.Popen",
        lambda argv, **kwargs: spawned.append(argv),
    )

    result = runner.invoke(app, ["setup-google"])

    assert result.exit_code == 1
    assert "GOOGLE_OAUTH_CLIENT_ID" in result.output
    assert spawned == []  # no workspace-mcp server was ever started


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


def test_download_models_where_the_wake_word_cannot_run_says_so_in_one_line(
    settings_stub, monkeypatch
):
    monkeypatch.setattr("jarvis.cli.wakeword_unavailable", lambda: "the wake word needs macOS")

    result = runner.invoke(app, ["download-models"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # a clean exit, not a traceback
    assert result.output.strip().count("\n") == 0
    assert "the wake word needs macOS" in result.output


# --- housekeeping and the memory -------------------------------------------


def test_tasks_list_hides_jarvis_own_housekeeping(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("his work", status=TaskStatus.DONE),
        make_task("update the memory after call abc123", status=TaskStatus.DONE, internal=True),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    assert "his work" in result.output
    assert "update the memory" not in result.output


def test_tasks_list_shows_housekeeping_when_asked(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("update the memory after call abc123", status=TaskStatus.DONE, internal=True),
    )

    result = runner.invoke(app, ["tasks", "list", "--internal"])

    assert result.exit_code == 0, result.output
    assert "update the memory" in result.output


def test_tasks_list_says_whether_he_has_been_told(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("not yet said", status=TaskStatus.DONE),
        make_task(
            "already said",
            status=TaskStatus.DONE,
            reported_at=datetime(2026, 8, 25, 14, 0, tzinfo=UTC),
        ),
        make_task("still going", status=TaskStatus.RUNNING),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert "TOLD" in result.output
    assert "NO" in next(line for line in result.output.splitlines() if "not yet said" in line)
    assert "yes" in next(line for line in result.output.splitlines() if "already said" in line)


def test_memory_says_so_when_there_is_nothing_remembered_yet(settings_stub):
    result = runner.invoke(app, ["memory"])

    assert result.exit_code == 0, result.output
    assert "nothing remembered yet" in result.output


def test_memory_prints_what_is_remembered(settings_stub):
    settings_stub.ensure_dirs()
    memory_path(settings_stub.data_dir).write_text("# What Jarvis knows\n\nHe hates jargon.\n")

    result = runner.invoke(app, ["memory"])

    assert result.exit_code == 0, result.output
    assert "He hates jargon." in result.output


def test_memory_path_prints_only_the_path(settings_stub):
    result = runner.invoke(app, ["memory", "--path"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(memory_path(settings_stub.data_dir))
