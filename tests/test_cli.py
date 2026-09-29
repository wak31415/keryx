"""Tests for the `jarvis` command line: wiring only, no hardware and no network."""

import asyncio
import importlib.metadata
import json
import logging
import re
import shutil
import stat
import wave
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeProvider
from pydantic import ValidationError
from typer.testing import CliRunner

from jarvis import plugins
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
from jarvis.config.store import ConfigStore
from jarvis.continuity.memory import memory_path
from jarvis.realtime.base import AudioDelta, Transcript
from jarvis.restart.service import ServiceTarget
from jarvis.restart.store import RECORD_NAME, RestartRecord, RestartStore
from jarvis.tasks.agent_runner import ClaudeAgentRunner, FakeAgentRunner
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

runner = CliRunner()


@pytest.fixture
def settings_stub(monkeypatch, tmp_path, every_agent_installed):
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
        "tasks",
        "doctor",
        "setup",
        "config",
        "auth",
        "memory",
        "restart",
    ):
        assert command in result.output


def test_version_prints_the_installed_package_version():
    """The bug report template asks for it."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"jarvis {importlib.metadata.version('jarvis')}"


def test_help_documents_version():
    assert "--version" in runner.invoke(app, ["--help"]).output


def test_serve_help_documents_its_switches():
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    for option in ("--fake-agents", "--host", "--port"):
        assert option in result.output
    assert "--no-phone" not in result.output and "--no-wakeword" not in result.output


def test_loopback_help_documents_its_switches():
    result = runner.invoke(app, ["loopback", "--help"])

    assert result.exit_code == 0
    for option in ("--wav", "--out", "--tail-seconds"):
        assert option in result.output


# --- serve -----------------------------------------------------------------


def test_serve_still_accepts_no_wakeword_from_an_older_unit(settings_stub, monkeypatch):
    """Units installed before the wake word left `main` pass it; a restart must not fail."""
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0, result.output
    assert built["served"] is True


def test_serve_refuses_to_answer_the_phone_without_signature_checks_behind_a_tunnel(
    settings_stub, monkeypatch
):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    monkeypatch.setattr(settings_stub, "debug_skip_twilio_validation", True)
    monkeypatch.setattr(settings_stub, "public_host", "jarvis.example")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "DEBUG_SKIP_TWILIO_VALIDATION" in result.output
    assert len(result.output.strip().splitlines()) == 1
    assert "config" not in built  # no server was ever built


def test_serve_refuses_a_default_agent_that_is_not_enabled(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    monkeypatch.setattr(settings_stub, "agents_enabled", ["claude"])
    monkeypatch.setattr(settings_stub, "agent_backend", "codex")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "AGENT_BACKEND is codex" in result.output
    assert "config" not in built


def test_serve_refuses_a_default_agent_whose_extra_is_not_installed(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    from jarvis.agents import registry

    monkeypatch.setattr(registry, "installed", lambda agent: agent != "claude")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "AGENT_BACKEND is claude, which is not installed — uv sync --extra claude" in (
        result.output
    )
    assert "config" not in built


def test_serve_refuses_until_the_old_files_are_migrated(settings_stub, monkeypatch):
    """Started now it would find no tasks, no memory, and no PIN: an open enrolment door."""
    built: dict = {}
    stub_uvicorn(monkeypatch, built)
    Path(".env").write_text("OPENAI_API_KEY=sk-old\n")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "jarvis migrate" in result.output
    assert "config" not in built
    assert not (settings_stub.state_dir / "logs" / "jarvis.log").exists()


@pytest.mark.parametrize(
    "command",
    [["tasks", "list"], ["tasks", "show", "1"], ["memory"], ["forget", "--yes"], ["approvals"],
     ["restart", "--status"], ["auth", "status"]],
)
def test_what_reads_the_data_waits_for_the_migration_too(settings_stub, command):
    """It would answer from an empty directory, and `tasks` would make a `tasks.db` there for
    the migration to trip over."""
    legacy = Path.home() / ".jarvis"
    legacy.mkdir(parents=True)
    (legacy / "tasks.db").touch()

    result = runner.invoke(app, command)

    assert result.exit_code == 2, result.output
    assert "jarvis migrate" in result.output
    assert not (settings_stub.data_dir / "tasks.db").exists()


def stub_uvicorn(monkeypatch, built: dict) -> None:
    """Replace `uvicorn.Server` with a stub that records its config instead of listening."""

    class StubServer:
        def __init__(self, config):
            built["config"] = config
            self.should_exit = False
            built["server"] = self

        async def serve(self):
            built["served"] = True
            executor = asyncio.get_running_loop()._default_executor
            built["executor_workers"] = executor._max_workers if executor else 0

    monkeypatch.setattr("jarvis.cli.uvicorn.Server", StubServer)


def test_serve_names_settings_a_plugin_replaced_and_starts_anyway(
    settings_stub, monkeypatch, caplog
):
    store = ConfigStore()
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text('SLACK_MCP_SERVER = "chat"\n')
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    with caplog.at_level("WARNING", logger="jarvis.cli"):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "SLACK_MCP_SERVER" in caplog.text and "--from-settings" in caplog.text


def test_serve_prepares_its_directories_and_executor(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert (settings_stub.data_dir / "calls").is_dir()  # ensure_dirs() ran
    assert built["executor_workers"] >= 32  # room for every running turn's reader


def test_serve_runs_the_phone_server_on_the_configured_address(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert built["served"] is True
    config = built["config"]
    assert (config.host, config.port) == (settings_stub.host, settings_stub.port)
    assert config.log_level == settings_stub.log_level.lower()
    assert "/twilio/media" in {route.path for route in config.app.routes}


def test_serve_takes_the_address_from_the_command_line(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9999"])

    assert result.exit_code == 0, result.output
    config = built["config"]
    assert (config.host, config.port) == ("0.0.0.0", 9999)
    assert (settings_stub.host, settings_stub.port) == ("127.0.0.1", 8080)  # loaded settings intact


def test_serve_wires_the_task_stack_into_the_shared_state(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    state = built["config"].app.state.jarvis
    assert state.manager is not None
    assert isinstance(state.manager._runner.runners["claude"], ClaudeAgentRunner)
    assert "dispatch_task" in {schema["name"] for schema in state.registry.schemas()}
    assert state.store._conn is None  # the store is closed again when serve returns


def test_fake_agents_swaps_the_subagent_runner(settings_stub, monkeypatch):
    built: dict = {}
    stub_uvicorn(monkeypatch, built)

    result = runner.invoke(app, ["serve", "--fake-agents"])

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

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1  # re-running serve replaces the handler, it does not stack
    assert Path(handlers[0].baseFilename) == settings_stub.state_dir / "logs" / "jarvis.log"
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
            input_tokens=55831,
            output_tokens=303,
            cost_usd=0.42,
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
        "55831",
        "303",
        "0.42",
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


# --- how loud the commands are ---------------------------------------------


@pytest.fixture
def log_levels(monkeypatch):
    """The level each command configures logging at, instead of configuring it.

    pytest owns the root logger's handlers, which turns the real `basicConfig` into a
    no-op here — so the level asked for is what there is to assert on.
    """
    asked: list[object] = []
    monkeypatch.setattr(
        "jarvis.cli.logging.basicConfig", lambda **kwargs: asked.append(kwargs["level"])
    )
    return asked


def test_read_only_commands_print_their_answer_without_info_lines(settings_stub, log_levels):
    """`tasks list` on a fresh data dir led with "created tasks schema v4 at …"."""
    for command in (["tasks", "list"], ["memory"], ["approvals"]):
        assert runner.invoke(app, command).exit_code == 0

    assert log_levels == [logging.WARNING] * 3


def test_log_level_debug_still_reaches_a_read_only_command(monkeypatch, tmp_path, log_levels):
    settings = Settings(
        _env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis", log_level="debug"
    )
    monkeypatch.setattr("jarvis.cli.load_settings", lambda **overrides: settings)

    assert runner.invoke(app, ["tasks", "list"]).exit_code == 0

    assert log_levels == [logging.DEBUG]


def test_the_restart_watchdog_keeps_its_info_lines(settings_stub, log_levels, monkeypatch):
    """Its log file is the only place it is ever heard, so it is not a quiet command."""

    async def watch(_settings):
        return "nothing"

    monkeypatch.setattr("jarvis.cli.watch", watch)

    result = runner.invoke(app, ["restart-watch"])

    assert result.exit_code == 0, result.output
    assert log_levels == [logging.INFO]


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
        "jarvis.cli.resolve_target",
        lambda _settings, **_kwargs: ServiceTarget("systemd", "jarvis.service"),
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
    record = RestartStore(restart_settings.state_dir / RECORD_NAME).load()
    assert record.reason == "new code"
    assert record.number == "+15550000001"
    assert record.origin_channel == "cli"
    assert "…0001" in result.output  # the number is masked where it is printed
    # Armed before the hand-over, and on the record, because this command has just
    # promised them a call and nothing else would notice if it never came.
    assert "4321" in record.watchdog
    assert "watchdog:" in result.output


def test_a_quiet_restart_arms_no_watchdog(restart_settings, ran):
    """`--no-callback` promises nothing, so there is nothing to notice the absence of."""
    calls, _ = ran

    runner.invoke(app, ["restart", "--no-callback"])

    record = RestartStore(restart_settings.state_dir / RECORD_NAME).load()
    assert record.watchdog == ""
    assert calls == [["systemctl", "--user", "restart", "jarvis.service"]]


def test_restart_refuses_to_cut_off_a_live_call(restart_settings, ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setattr("jarvis.cli.health_probe", lambda _settings: 1)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert "cuts them off" in result.output
    assert calls == []
    assert not (restart_settings.state_dir / RECORD_NAME).exists()


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
    monkeypatch.setattr("jarvis.cli.resolve_target", lambda _settings, **_kwargs: None)

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    assert "install-systemd.sh" in result.output
    assert calls == []


def test_restart_from_a_terminal_restarts_the_installed_service(
    restart_settings, ran, monkeypatch
):
    """The command is never run *inside* the unit, so it asks for an installed one."""
    asked: list[dict] = []

    def resolve(_settings, **kwargs):
        asked.append(kwargs)
        return ServiceTarget("systemd", "jarvis.service")

    monkeypatch.setattr("jarvis.cli.resolve_target", resolve)

    assert runner.invoke(app, ["restart"]).exit_code == 0
    assert asked == [{"from_outside": True}]


def test_restart_no_callback_leaves_no_number(restart_settings, ran):
    result = runner.invoke(app, ["restart", "--no-callback"])

    assert result.exit_code == 0
    record = RestartStore(restart_settings.state_dir / RECORD_NAME).load()
    assert record.number is None and record.quiet is True
    assert "no call back was asked for" in result.output


def test_a_restart_command_that_fails_says_so_and_keeps_the_record(restart_settings, ran):
    _, done = ran
    done.returncode = 3

    result = runner.invoke(app, ["restart"])

    assert result.exit_code == 1
    record = RestartStore(restart_settings.state_dir / RECORD_NAME).load()
    assert record.state == "failed"
    assert "exited 3" in record.error


def test_restart_status_with_nothing_on_record(restart_settings):
    result = runner.invoke(app, ["restart", "--status"])

    assert result.exit_code == 0
    assert "no restart on record" in result.output


def test_restart_status_reads_back_a_failure(restart_settings):
    store = RestartStore(restart_settings.state_dir / RECORD_NAME)
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


def test_doctor_fails_when_the_install_is_incomplete(settings_stub):
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "❌" in result.output
    assert "Twilio credentials" in result.output


def test_doctor_reports_a_missing_openai_key_instead_of_crashing(
    monkeypatch, tmp_path
):
    optional_key_loader(monkeypatch, tmp_path, [])

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_doctor_passes_on_a_complete_install(
    monkeypatch, tmp_path, every_agent_installed
):
    monkeypatch.chdir(tmp_path)
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

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "❌" not in result.output


def test_doctor_still_runs_and_explains_a_malformed_pin(monkeypatch, tmp_path):
    """`doctor` is the command that has to work when nothing else does.

    A `JARVIS_PIN` that breaks the 6-8 digit rule stops `jarvis serve` from loading at all,
    so if it stopped `doctor` too there would be nothing left to diagnose it with.
    """
    monkeypatch.chdir(tmp_path)
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

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "JARVIS_PIN" in result.output
    assert "6 to 8 digits" in result.output
    pin_line = next(line for line in result.output.splitlines() if "PIN:" in line)
    assert "9876" not in pin_line
    # Everything else was still checked rather than lost to the exception.
    assert "allowed callers" in result.output


def test_doctor_explains_a_malformed_pin_set_the_way_a_person_sets_one(
    monkeypatch, tmp_path
):
    """The same, through the real loader rather than a stub that stands in for it.

    `pydantic` reports a field's *alias* when it has one, so a value refused under
    `JARVIS_PIN` arrives as `JARVIS_PIN` and not as `pin`. A fallback that matched only the
    field name never fired, and `doctor` died with a traceback in the one state it exists
    to explain.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    for name, value in {
        "OPENAI_API_KEY": "sk-test",
        "JARVIS_PIN": "1234",
        "DATA_DIR": str(tmp_path / "jarvis"),
        "PROJECTS_ROOT": str(tmp_path / "projects"),
        "SKILLS_DIR": str(tmp_path / "skills"),
    }.items():
        monkeypatch.setenv(name, value)

    result = runner.invoke(app, ["doctor"])

    assert not isinstance(result.exception, ValidationError), result.exception
    assert result.exit_code == 1, result.output
    assert "JARVIS_PIN is set but unusable" in result.output
    assert "6 to 8 digits" in result.output
    assert "allowed callers" in result.output  # the rest was still checked


def test_serve_refuses_to_start_on_a_malformed_pin(monkeypatch, tmp_path):
    """The strict gate. A bad PIN must stop the thing that answers the phone."""
    monkeypatch.chdir(tmp_path)

    def load(**overrides):
        return Settings(_env_file=None, openai_api_key="sk-test", data_dir=tmp_path, pin="9876")

    monkeypatch.setattr("jarvis.cli.load_settings", load)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "JARVIS_PIN" in result.output
    assert "6 to 8 digits" in result.output
    assert "9876" not in result.output
    assert "doctor" in result.output


# --- approvals -------------------------------------------------------------


def _audit(settings, *entries) -> None:
    path = settings.state_dir / STATE_DIR_NAME / AUDIT_NAME
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
    path = settings_stub.state_dir / STATE_DIR_NAME / AUDIT_NAME
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
    switch = settings_stub.state_dir / STATE_DIR_NAME / KILL_SWITCH_NAME

    assert runner.invoke(app, ["approvals", "--disable"]).exit_code == 0
    assert switch.exists()
    assert stat.S_IMODE(switch.stat().st_mode) == 0o600
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


# --- housekeeping and the memory -------------------------------------------


def test_tasks_list_hides_jarvis_own_housekeeping(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("their work", status=TaskStatus.DONE),
        make_task("update the memory after call abc123", status=TaskStatus.DONE, internal=True),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0, result.output
    assert "their work" in result.output
    assert "update the memory" not in result.output


def test_tasks_list_shows_housekeeping_when_asked(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("update the memory after call abc123", status=TaskStatus.DONE, internal=True),
    )

    result = runner.invoke(app, ["tasks", "list", "--internal"])

    assert result.exit_code == 0, result.output
    assert "update the memory" in result.output


def test_tasks_list_says_whether_they_have_been_told(settings_stub):
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


def test_tasks_list_says_which_agent_ran_each_task(settings_stub):
    seed_tasks(
        settings_stub,
        make_task("on the default", status=TaskStatus.DONE),
        make_task("named out loud", status=TaskStatus.DONE, agent="codex"),
    )

    result = runner.invoke(app, ["tasks", "list"])

    assert "AGENT" in result.output
    lines = result.output.splitlines()
    assert "claude" in next(line for line in lines if "on the default" in line)
    assert "codex" in next(line for line in lines if "named out loud" in line)


def test_tasks_show_names_the_agent(settings_stub):
    seed_tasks(settings_stub, make_task("named out loud", agent="codex"))

    result = runner.invoke(app, ["tasks", "show", "1"])

    assert any(line.split() == ["agent", "codex"] for line in result.output.splitlines())


def test_memory_says_so_when_there_is_nothing_remembered_yet(settings_stub):
    result = runner.invoke(app, ["memory"])

    assert result.exit_code == 0, result.output
    assert "nothing remembered yet" in result.output


def test_memory_prints_what_is_remembered(settings_stub):
    settings_stub.ensure_dirs()
    memory_path(settings_stub.data_dir).write_text("# What Jarvis knows\n\nThey hate jargon.\n")

    result = runner.invoke(app, ["memory"])

    assert result.exit_code == 0, result.output
    assert "They hate jargon." in result.output


def test_memory_path_prints_only_the_path(settings_stub):
    result = runner.invoke(app, ["memory", "--path"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(memory_path(settings_stub.data_dir))


# --- tools -----------------------------------------------------------------


def write_tool(settings: Settings, name: str, source: str) -> None:
    settings.ensure_dirs()
    path = settings.custom_tools_dir / name
    path.write_text(source, encoding="utf-8")
    path.chmod(0o600)


MOON_TOOL = (
    "from jarvis.tools.custom import custom_tool\n\n"
    "@custom_tool(description='The moon tonight.', needs_pin=False)\n"
    "def moon_phase(ctx, args):\n    return {}\n"
)


def test_tools_says_so_when_there_are_none(settings_stub):
    result = runner.invoke(app, ["tools"])

    assert result.exit_code == 0, result.output
    assert str(settings_stub.custom_tools_dir) in result.output
    assert "no custom tools" in result.output


def test_tools_lists_each_tool_and_exits_1_on_anything_refused(settings_stub):
    write_tool(settings_stub, "moon.py", MOON_TOOL)
    write_tool(settings_stub, "pin.py", MOON_TOOL.replace("moon_phase", "submit_pin"))

    result = runner.invoke(app, ["tools"])

    assert result.exit_code == 1, result.output
    assert re.search(r"moon_phase\s+no PIN\s+moon.py", result.output)
    assert "refused pin.py: 'submit_pin' is already a tool's name" in result.output


def test_tools_refuses_a_built_in_name_this_machine_does_not_offer(settings_stub):
    write_tool(settings_stub, "restart.py", MOON_TOOL.replace("moon_phase", "restart_service"))

    result = runner.invoke(app, ["tools"])

    assert result.exit_code == 1
    assert "'restart_service' is already a tool's name" in result.output


# --- plugins -------------------------------------------------------------------------


def test_plugins_lists_all_four_off_on_a_fresh_machine(settings_stub):
    result = runner.invoke(app, ["plugins", "--json"])

    assert result.exit_code == 0, result.output
    found = json.loads(result.output)["plugins"]
    assert [(one["name"], one["on"]) for one in found] == [
        ("send_to_slack", False), ("check_email", False),
        ("check_billing", False), ("cluster_stats", False),
    ]


def test_plugins_install_with_values_turns_one_on(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "check_billing",
                                 "--set", "monthly_budget=40"])

    assert result.exit_code == 0, result.output
    assert "check_billing is on, from the next call" in result.output
    assert plugins.read_config(settings_stub, "check_billing")["monthly_budget"] == 40
    listed = runner.invoke(app, ["plugins"]).output
    assert "check_billing    on" in listed and "monthly_budget=40" in listed


def test_plugins_install_never_takes_a_secret(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "send_to_slack",
                                 "--set", "SLACK_BOT_TOKEN=xoxb-secret"])

    assert result.exit_code == 1
    assert "jarvis config set SLACK_BOT_TOKEN --stdin" in result.output
    assert "xoxb-secret" not in result.output
    assert not plugins.config_path(settings_stub, "send_to_slack").exists()


def test_plugins_install_says_when_it_is_installed_but_refused(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "send_to_slack",
                                 "--set", "channel_id=D123"])

    assert result.exit_code == 1
    assert "send_to_slack is installed but refused" in result.output
    assert "SLACK_BOT_TOKEN --stdin" in result.output


def test_plugins_install_takes_the_clusters_and_the_guard(settings_stub, tmp_path):
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/bin/sh\n")

    result = runner.invoke(app, ["plugins", "install", "cluster_stats",
                                 "--cluster", "Alpha=gpu", "--cluster", "beta=pli",
                                 "--guard", str(guard)])

    assert result.exit_code == 0, result.output
    values = plugins.read_config(settings_stub, "cluster_stats")
    assert values["clusters"] == {"alpha": "gpu", "beta": "pli"}
    assert values["guard"] == str(guard)


def test_the_cluster_flags_are_only_for_cluster_stats(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "check_billing", "--cluster", "a=b"])

    assert result.exit_code == 1 and "for cluster_stats" in result.output


def test_a_template_is_written_off_and_activated_once_edited(settings_stub):
    written = runner.invoke(app, ["plugins", "install", "cluster_stats", "--template"])
    path = plugins.config_path(settings_stub, "cluster_stats")

    assert written.exit_code == 0 and str(path) in written.output
    assert "then `jarvis plugins install cluster_stats`" in written.output
    early = runner.invoke(app, ["plugins", "install", "cluster_stats"])
    assert early.exit_code == 1 and "names no host" in early.output

    path.write_text(path.read_text().replace("[clusters]\n", '[clusters]\nalpha = "gpu"\n'))
    activated = runner.invoke(app, ["plugins", "install", "cluster_stats"])

    assert activated.exit_code == 0, activated.output
    assert not plugins.draft_path(settings_stub, "cluster_stats").exists()


def test_from_settings_moves_what_was_offered_and_drops_the_old_keys(settings_stub):
    store = ConfigStore()
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(
        'OPENAI_VOICE = "cedar"\nBILLING_MONTHLY_BUDGET = 55.0\n\n[CLUSTERS]\nalpha = "gpu"\n'
    )

    result = runner.invoke(app, ["plugins", "install", "--from-settings"])

    assert result.exit_code == 0, result.output
    assert "check_billing: settings in" in result.output
    assert "cluster_stats is on, from the next call" in result.output
    assert plugins.read_config(settings_stub, "check_billing")["monthly_budget"] == 55
    assert plugins.retired_in(store) == {}


def test_from_settings_with_nothing_old_says_so(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "--from-settings"])

    assert result.exit_code == 0
    assert "nothing to move" in result.output


@pytest.mark.parametrize("argv", [["install"], ["install", "send_fax"], ["remove", "send_fax"]])
def test_plugins_needs_a_plugin_it_knows(settings_stub, argv):
    result = runner.invoke(app, ["plugins", *argv, "--yes"] if argv[0] == "remove" else
                           ["plugins", *argv])

    assert result.exit_code in (1, 2)
    assert "which plugin" in result.output or "no plugin called" in result.output


def test_plugins_remove_turns_one_off_and_keeps_its_settings(settings_stub):
    runner.invoke(app, ["plugins", "install", "check_billing", "--set", "monthly_budget=9"])

    result = runner.invoke(app, ["plugins", "remove", "check_billing", "--yes"])
    again = runner.invoke(app, ["plugins", "remove", "check_billing", "--yes"])

    assert "check_billing is off from the next call" in result.output
    assert "was not on" in again.output
    assert plugins.read_config(settings_stub, "check_billing")["monthly_budget"] == 9


@pytest.mark.parametrize("argv", [["install", "check_billing"], ["remove", "check_billing"]])
def test_the_service_may_not_turn_plugins_on_or_off(settings_stub, monkeypatch, argv):
    monkeypatch.setenv("JARVIS_ACTOR", "service")

    result = runner.invoke(app, ["plugins", *argv, "--yes"] if argv[0] == "remove" else
                           ["plugins", *argv])

    assert result.exit_code == 1
    assert "only the owner" in result.output
    assert not plugins.tool_path(settings_stub, "check_billing").exists()


def test_plugins_hosts_lists_what_ssh_says_and_whose_master_is_up(settings_stub, monkeypatch):
    from jarvis.plugins.ssh_hosts import SshHost

    monkeypatch.setattr("jarvis.cli.ssh_hosts.discover", lambda: [
        SshHost("alpha", "login.alpha.example", "me", True, "/tmp/cm"),
        SshHost("beta", "beta.example", "", False, ""),
    ])
    monkeypatch.setattr("jarvis.cli.ssh_hosts.master_alive", lambda alias: alias == "alpha")

    as_json = runner.invoke(app, ["plugins", "hosts", "--json"])
    text = runner.invoke(app, ["plugins", "hosts"])

    hosts = json.loads(as_json.output)["hosts"]
    assert [(h["alias"], h["control_master"], h["master_alive"]) for h in hosts] == [
        ("alpha", True, True), ("beta", False, False),
    ]
    assert "me@login.alpha.example" in text.output and "ControlMaster: up" in text.output
    assert "ControlMaster: none" in text.output


def test_plugins_hosts_with_no_ssh_config_says_so(settings_stub, monkeypatch):
    monkeypatch.setattr("jarvis.cli.ssh_hosts.discover", lambda: [])

    assert "no hosts in" in runner.invoke(app, ["plugins", "hosts"]).output


def test_tools_leaves_a_running_service_alone(settings_stub):
    """A subagent runs it inside the live service's environment: the bridge's socket stays."""
    settings_stub.ensure_dirs()
    socket = settings_stub.state_dir / "approvals.sock"
    socket.write_text("")

    result = runner.invoke(app, ["tools"])

    assert result.exit_code == 0, result.output
    assert socket.exists()


def test_tools_json_is_one_document(settings_stub):
    write_tool(settings_stub, "moon.py", MOON_TOOL)

    result = runner.invoke(app, ["tools", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "directory": str(settings_stub.custom_tools_dir),
        "tools": [
            {"name": "moon_phase", "needs_pin": False, "file": "moon.py",
             "description": "The moon tonight."}
        ],
        "refused": [],
    }

