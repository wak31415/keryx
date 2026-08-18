"""Tests for the `jarvis` command line: wiring only, no hardware and no network."""

import wave
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeProvider
from typer.testing import CliRunner

from jarvis.cli import app
from jarvis.config import Settings
from jarvis.realtime.base import AudioDelta, Transcript

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


def test_serve_reports_that_the_phone_server_is_missing(settings_stub):
    result = runner.invoke(app, ["serve", "--no-wakeword"])

    assert result.exit_code == 0
    assert "phone server" in result.output.lower()


def test_serve_starts_the_local_runner(settings_stub, monkeypatch, tmp_path):
    built: dict = {}

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

    monkeypatch.setattr("jarvis.cli.LocalAudioDevice", StubDevice)
    monkeypatch.setattr("jarvis.cli.OpenWakeWordDetector", StubDetector)
    monkeypatch.setattr("jarvis.cli.LocalRunner", StubRunner)

    result = runner.invoke(app, ["serve", "--no-phone"])

    assert result.exit_code == 0, result.output
    assert built["ran"] is True
    assert built["model"] == settings_stub.wakeword_model
    settings, device, _listener, kwargs = built["runner"]
    assert settings is settings_stub
    assert device is built["device"]
    assert kwargs["sessions"] is not None
    assert (settings_stub.data_dir / "calls").is_dir()  # ensure_dirs() ran


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
