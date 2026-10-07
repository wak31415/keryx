"""`keryx models`: the catalog against this machine, a download, and the servers."""

import json
import os
import signal

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.localmodels import catalog, download, hardware, servers, use
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
    monkeypatch.setattr(use, "unit_installed", lambda kind: False)
    monkeypatch.setattr(use, "probe", lambda url: (False, None))
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


# --- models list --json: the servers ------------------------------------------------------


def test_list_json_says_which_servers_are_set_up_and_answering(models_settings, monkeypatch):
    models_settings.llm_server_model = CODER.key
    models_settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    models_settings.local_agent_model = CODER.key
    models_settings.agents_enabled = ["claude", "local"]
    monkeypatch.setattr(use, "unit_installed", lambda kind: kind == "llm")
    monkeypatch.setattr(use, "probe", lambda url: (url == "http://127.0.0.1:8090/v1",
                                                   "qwen3-coder"))

    servers_part = json.loads(runner.invoke(app, ["models", "list", "--json"]).output)["servers"]

    assert servers_part["llm"]["url"] == "http://127.0.0.1:8090/v1"
    assert servers_part["llm"]["unit_installed"] and servers_part["llm"]["reachable"]
    assert servers_part["llm"]["model"] == CODER.key  # the setting
    assert servers_part["llm"]["served_model"] == "qwen3-coder"  # what the server says
    assert servers_part["voice"] == {"url": None, "port": 8765, "unit_installed": False,
                                     "reachable": False}
    assert servers_part["local_agent"]["enabled"] and not servers_part["local_agent"]["default"]


# --- models pull --json: JSON lines --------------------------------------------------------


def events_of(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


TERMINAL = {"done", "adopted", "error", "cancelled"}


def test_pull_json_tells_progress_then_done(models_settings, monkeypatch):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def fake(url, dest, *, size, sha256, progress):
        progress(0)
        progress(size)
        return dest

    monkeypatch.setattr(download, "download", fake)

    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == 0, result.output
    path = str(download.model_path(models_settings.cache_dir, CODER))
    assert events_of(result) == [
        {"event": "progress", "done": 0, "total": CODER.size},
        {"event": "progress", "done": CODER.size, "total": CODER.size},
        {"event": "done", "path": path},
    ]
    assert result.stderr == ""


def test_pull_json_says_a_copy_already_on_disk_was_adopted(models_settings, monkeypatch, tmp_path):
    found = tmp_path / CODER.file
    found.write_bytes(b"x")
    monkeypatch.setattr(download, "already_on_disk", lambda entry: found)

    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == 0
    assert events_of(result) == [{
        "event": "adopted", "path": str(download.model_path(models_settings.cache_dir, CODER)),
        "from": str(found),
    }]


def test_pull_json_refuses_with_an_error_event(models_settings, monkeypatch):
    unknown = runner.invoke(app, ["models", "pull", "nope", "--json"])
    assert unknown.exit_code == 2
    assert events_of(unknown) == [
        {"event": "error", "message": "there is no model called nope (`keryx models list`)"}
    ]

    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**9)
    full = runner.invoke(app, ["models", "pull", CODER.key, "--json"])
    assert full.exit_code == 1
    [event] = events_of(full)
    assert event["event"] == "error" and "needs 19.6 GB free" in event["message"]

    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**12)

    def broken(*args, **kwargs):
        raise download.DownloadError("did not arrive whole")

    monkeypatch.setattr(download, "download", broken)
    bad = runner.invoke(app, ["models", "pull", CODER.key, "--json"])
    assert bad.exit_code == 1
    assert events_of(bad)[-1] == {"event": "error", "message": "did not arrive whole"}


def test_pull_json_cancelled_keeps_the_part(models_settings, monkeypatch):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def interrupted(url, dest, *, size, sha256, progress):
        progress(size // 2)
        raise KeyboardInterrupt  # what the SIGTERM handler turns a terminate into

    monkeypatch.setattr(download, "download", interrupted)
    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])
    events = [json.loads(line) for line in result.output.splitlines()]
    assert events[-1] == {"event": "cancelled"}
    assert [e["event"] for e in events].count("cancelled") == 1
    assert result.exit_code == 130


def test_pull_json_is_cancelled_by_a_terminate_and_the_part_stays(models_settings, monkeypatch):
    """What the Mac app's Cancel sends. The `.part` is the next pull's head start."""
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    part = download.model_path(models_settings.cache_dir, CODER).with_suffix(".gguf.part")
    before = signal.getsignal(signal.SIGTERM)

    def terminated(url, dest, *, size, sha256, progress):
        part.write_bytes(b"half")
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the terminate should have stopped it")

    monkeypatch.setattr(download, "download", terminated)

    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == 130
    assert events_of(result) == [{"event": "cancelled"}]
    assert part.read_bytes() == b"half"
    assert signal.getsignal(signal.SIGTERM) is before


def test_pull_without_json_leaves_a_terminate_alone(models_settings, monkeypatch):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    before = signal.getsignal(signal.SIGTERM)
    during: list = []

    def fake(url, dest, *, size, sha256, progress):
        during.append(signal.getsignal(signal.SIGTERM))
        return dest

    monkeypatch.setattr(download, "download", fake)

    assert runner.invoke(app, ["models", "pull", CODER.key]).exit_code == 0
    assert during == [before]


@pytest.fixture
def old_jarvis_dir(models_settings, tmp_path):
    """An install from before the rename: Jarvis's data directory still holds a file."""
    old = tmp_path / "home" / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")
    assert models_settings.storage_refusal()
    return models_settings


def test_pull_json_tells_the_migration_refusal_on_stdout(old_jarvis_dir):
    plain = runner.invoke(app, ["models", "pull", CODER.key])
    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == plain.exit_code == 2
    sentence = plain.stderr.strip()
    assert sentence.startswith("keryx cannot do that yet:") and "keryx migrate" in sentence
    assert result.stdout.splitlines() == [json.dumps({"event": "error", "message": sentence})]
    assert result.stderr == ""


def broken_settings(monkeypatch) -> None:
    try:
        Settings(_env_file=None, openai_api_key="test", port="not a port")
    except ValidationError as error:
        failure = error

    def refuse(**overrides):
        raise failure

    monkeypatch.setattr("keryx.cli.load_settings", refuse)


def test_pull_json_tells_settings_that_will_not_load(models_settings, monkeypatch):
    broken_settings(monkeypatch)

    plain = runner.invoke(app, ["models", "pull", CODER.key])
    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == plain.exit_code == 1
    [event] = events_of(result)
    assert event["event"] == "error" and event["message"].startswith("PORT ")


def test_pull_json_tells_anything_unexpected_as_an_error(models_settings, monkeypatch):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def unwritable(cache_dir, entry):
        raise PermissionError("the cache is not writable")

    monkeypatch.setattr(download, "ensure_model_dir", unwritable)

    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    assert result.exit_code == 1
    assert events_of(result) == [{"event": "error", "message": "the cache is not writable"}]


def _done(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)
    monkeypatch.setattr(download, "download",
                        lambda url, dest, *, size, sha256, progress: progress(size))


def _adopted(monkeypatch, tmp_path):
    found = tmp_path / CODER.file
    found.write_bytes(b"x")
    monkeypatch.setattr(download, "already_on_disk", lambda entry: found)


def _failed(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def broken(url, dest, *, size, sha256, progress):
        progress(1)
        raise download.DownloadError("did not arrive whole")

    monkeypatch.setattr(download, "download", broken)


def _cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def interrupted(url, dest, *, size, sha256, progress):
        progress(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(download, "download", interrupted)


def _refused(monkeypatch, tmp_path):
    old = tmp_path / "home" / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")


def _unloadable(monkeypatch, tmp_path):
    broken_settings(monkeypatch)


@pytest.mark.parametrize(
    ("arrange", "ending", "code"),
    [(_done, "done", 0), (_adopted, "adopted", 0), (_failed, "error", 1),
     (_cancelled, "cancelled", 130), (_refused, "error", 2), (_unloadable, "error", 1)],
)
def test_pull_json_ends_with_exactly_one_ending(
    models_settings, monkeypatch, tmp_path, arrange, ending, code
):
    arrange(monkeypatch, tmp_path)

    result = runner.invoke(app, ["models", "pull", CODER.key, "--json"])

    events = events_of(result)
    assert [event["event"] for event in events if event["event"] in TERMINAL] == [ending]
    assert events[-1]["event"] == ending
    assert all(event["event"] == "progress" for event in events[:-1])
    assert result.exit_code == code


# --- models use ----------------------------------------------------------------------------


@pytest.fixture
def downloaded(models_settings, monkeypatch):
    path = download.model_path(models_settings.cache_dir, CODER)
    path.parent.mkdir(parents=True)
    with path.open("wb") as out:
        out.truncate(CODER.size)  # sparse: `is_downloaded` reads the size
    monkeypatch.setattr(servers, "free_port", lambda preferred: preferred)
    monkeypatch.setattr("keryx.localmodels.runtimes.llama_server",
                        lambda cache_dir, **_: "/opt/homebrew/bin/llama-server")
    return path


def test_use_refuses_a_name_it_does_not_know(models_settings):
    result = runner.invoke(app, ["models", "use", "nope"])

    assert result.exit_code == 2
    assert "there is no model called nope" in result.stderr


def test_use_wants_the_model_downloaded_first(models_settings):
    result = runner.invoke(app, ["models", "use", CODER.key])

    assert result.exit_code == 1
    assert result.stderr.strip() == f"download it first: keryx models pull {CODER.key}"
    assert ConfigStore().stored() == {}


def test_use_makes_a_downloaded_model_the_local_agents(models_settings, downloaded):
    result = runner.invoke(app, ["models", "use", CODER.key, "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["saved"] == {
        "LLM_SERVER_MODEL": CODER.key,
        "LLM_SERVER_PORT": 8090,
        "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
        "LOCAL_AGENT_MODEL": CODER.key,
        "LOCAL_AGENT_API_KEY": None,
        "LOCAL_AGENT_API": "anthropic-messages",
        "AGENTS_ENABLED": ["claude", "local"],  # as stored: validated, a list
        "AGENT_BACKEND": "claude",
    }
    assert document["llama_server"] == "/opt/homebrew/bin/llama-server"
    assert document["restart_needed"] is True
    stored = ConfigStore().stored()
    assert stored["LLM_SERVER_MODEL"] == CODER.key
    assert stored["AGENTS_ENABLED"] == ["claude", "local"]


def test_use_as_the_default_agent(models_settings, downloaded):
    result = runner.invoke(app, ["models", "use", CODER.key, "--default-agent", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"]["AGENT_BACKEND"] == "local"
    assert ConfigStore().stored()["AGENT_BACKEND"] == "local"


def test_use_says_what_it_saved_and_what_comes_next(models_settings, downloaded, monkeypatch):
    monkeypatch.setattr("keryx.cli.sys.platform", "darwin")

    result = runner.invoke(app, ["models", "use", CODER.key])

    assert result.exit_code == 0, result.output
    assert "LLM_SERVER_MODEL saved to config.toml" in result.stdout
    assert "LOCAL_AGENT_API_KEY unset" in result.stdout
    assert "scripts/install-launchd.sh --llm" in result.stdout
    assert "keryx restart" in result.stdout


def test_use_without_a_llama_server_says_so(models_settings, downloaded, monkeypatch):
    monkeypatch.setattr("keryx.localmodels.runtimes.llama_server", lambda cache_dir, **_: None)

    result = runner.invoke(app, ["models", "use", CODER.key, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["llama_server"] is None


def test_use_is_refused_to_the_service(models_settings, downloaded, monkeypatch):
    """The addresses are protected: a subagent cannot point the local agent anywhere, and
    is refused before anything is looked at."""
    monkeypatch.setenv("KERYX_ACTOR", "service")
    asked: list[int] = []
    monkeypatch.setattr(servers, "free_port", lambda preferred: asked.append(preferred) or 1)

    for command in (["models", "use", CODER.key, "--json"], ["models", "use", CODER.key]):
        result = runner.invoke(app, command)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "only the owner may" in result.stderr
    assert asked == [] and ConfigStore().stored() == {}
