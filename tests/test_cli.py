"""Tests for the `jarvis` command line: wiring only, no hardware and no network."""

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeProvider
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config import Settings
from jarvis.realtime.base import AudioDelta, Transcript
from jarvis.tasks.agent_runner import ClaudeAgentRunner, FakeAgentRunner

runner = CliRunner()


@pytest.fixture
def settings_stub(monkeypatch, tmp_path):
    """Make every command see a hermetic Settings instead of the ambient environment."""
    settings = Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis")
    monkeypatch.setattr("jarvis.cli.load_settings", lambda: settings)
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
    for command in ("serve", "loopback", "download-models"):
        assert command in result.output


def test_serve_help_documents_its_switches():
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "--no-phone" in result.output
    assert "--no-wakeword" in result.output
    assert "--fake-agents" in result.output


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
