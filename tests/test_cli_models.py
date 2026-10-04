"""`keryx models`: the catalog against this machine, a download, and the servers."""

import json

import pytest
from typer.testing import CliRunner

from keryx.cli import app
from keryx.config import Settings
from keryx.localmodels import catalog, download, hardware, servers
from keryx.localmodels.hardware import Hardware

runner = CliRunner()
RTX = Hardware("cuda", "RTX 5090", 31.8, 62)
CODER = catalog.by_key("qwen3-coder-30b-a3b")


@pytest.fixture
def models_settings(monkeypatch, tmp_path, every_agent_installed):
    settings = Settings(
        _env_file=None, openai_api_key="test", data_dir=tmp_path / "keryx",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr("keryx.cli.load_settings", lambda **overrides: settings)
    monkeypatch.setattr(hardware, "detect", lambda: RTX)
    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**12)
    return settings


def test_list_marks_what_fits_and_recommends_one(models_settings):
    result = runner.invoke(app, ["models", "list"])

    assert result.exit_code == 0, result.output
    assert "RTX 5090, 32 GB of video memory" in result.output
    coder = next(line for line in result.output.splitlines() if line.startswith(CODER.key))
    assert "✓ fits" in coder and "recommended" in coder and "tested" in coder
    assert "✗ too big" in next(line for line in result.output.splitlines()
                               if line.startswith("gpt-oss-120b"))
    assert "bm_george" in result.output


def test_list_json_carries_the_hardware_the_models_and_the_voices(models_settings):
    models_settings.voice_base_url = "http://127.0.0.1:8765/v1"  # the voice shares the GPU

    document = json.loads(runner.invoke(app, ["models", "list", "--json"]).output)

    assert document["hardware"]["model_memory_gb"] == 31.8
    rows = {row["name"]: row for row in document["models"]}
    assert rows[CODER.key]["recommended"] and rows[CODER.key]["fits"]
    assert not rows["gpt-oss-120b"]["fits"]
    assert {"name": "af_heart", "sounds": "warm, American, female"} in document["voices"]


def test_pull_downloads_with_a_progress_bar(models_settings, monkeypatch):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    fetched: list = []

    def fake(url, dest, *, size, sha256, progress):
        fetched.append((url, sha256))
        progress(size)
        return dest

    monkeypatch.setattr(download, "download", fake)

    result = runner.invoke(app, ["models", "pull", CODER.key])

    assert result.exit_code == 0, result.output
    assert fetched == [(CODER.url, CODER.sha256)]
    assert str(download.model_path(models_settings.cache_dir, CODER)) in result.output


def test_pull_links_a_copy_already_on_disk(models_settings, monkeypatch, tmp_path):
    found = tmp_path / CODER.file
    found.write_bytes(b"x")
    monkeypatch.setattr(download, "already_on_disk", lambda entry: found)

    result = runner.invoke(app, ["models", "pull", CODER.key])

    assert result.exit_code == 0 and "already in" in result.output
    assert download.model_path(models_settings.cache_dir, CODER).read_bytes() == b"x"


def test_pull_refuses_an_unknown_name_a_full_disk_and_a_bad_download(models_settings, monkeypatch):
    assert runner.invoke(app, ["models", "pull", "nope"]).exit_code == 2

    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**9)
    full = runner.invoke(app, ["models", "pull", CODER.key])
    assert full.exit_code == 1 and "needs 19.6 GB free" in full.output

    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**12)

    def broken(*args, **kwargs):
        raise download.DownloadError("did not arrive whole")

    monkeypatch.setattr(download, "download", broken)
    bad = runner.invoke(app, ["models", "pull", CODER.key])
    assert bad.exit_code == 1 and "did not arrive whole" in bad.output


def test_serve_becomes_the_server_or_says_why_not(models_settings, monkeypatch):
    served: list = []
    monkeypatch.setattr(servers, "serve", lambda kind, settings: served.append(kind))
    assert runner.invoke(app, ["models", "serve", "llm"]).exit_code == 0
    assert served == ["llm"]

    def refuse(kind, settings):
        raise servers.ServerConfigError("LLM_SERVER_MODEL is not set")

    monkeypatch.setattr(servers, "serve", refuse)
    result = runner.invoke(app, ["models", "serve", "llm"])
    assert result.exit_code == 2
    assert "cannot start the llm server: LLM_SERVER_MODEL is not set" in result.output
