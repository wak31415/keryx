import os
import socket

import pytest

from keryx.endpoints import Endpoint, Probe
from keryx.localmodels import runtimes, servers
from keryx.localmodels.catalog import by_key
from keryx.localmodels.download import model_path
from keryx.localmodels.servers import (
    ServerConfigError,
    free_port,
    llm_argv,
    llm_model,
    serve,
    voice_argv,
    wait_for,
)

CODER = by_key("qwen3-coder-30b-a3b")


@pytest.fixture
def downloaded(settings):
    path = model_path(settings.cache_dir, CODER)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gguf")
    settings.llm_server_model = CODER.key
    return path


def test_the_model_server_serves_the_catalog_model_under_its_name(settings, downloaded):
    settings.llm_server_port = 8091

    argv = llm_argv(settings, "/bin/llama-server")

    assert argv[0] == "/bin/llama-server"
    flags = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert flags["--model"] == str(downloaded) and flags["--alias"] == CODER.key
    assert flags["--host"] == "127.0.0.1" and flags["--port"] == "8091"
    assert flags["--ctx-size"] == str(CODER.context)
    assert argv[-1] == "--jinja"


def test_a_gguf_of_your_own_is_served_under_its_stem(settings, tmp_path):
    own = tmp_path / "my-model.Q5.gguf"
    own.write_bytes(b"gguf")
    settings.llm_server_model = str(own)

    assert llm_model(settings) == ("my-model.Q5", own, servers.DEFAULT_CONTEXT)


@pytest.mark.parametrize(
    ("value", "says"),
    [(None, "LLM_SERVER_MODEL is not set"), ("qwen3-coder-30b-a3b", "keryx models pull"),
     ("/no/such.gguf", "is the path right")],
)
def test_what_is_missing_is_said(settings, value, says):
    settings.llm_server_model = value
    with pytest.raises(ServerConfigError, match=says):
        llm_model(settings)


def test_the_voice_server_takes_its_words_from_the_local_model(settings, monkeypatch):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "qwen3-coder-30b-a3b"
    settings.local_agent_api_key = "lk-1"
    settings.voice_server_port = 18765
    settings.voice_server_voice = "bm_george"

    argv, env = voice_argv(settings, "/bin/s2s")

    flags = dict(zip(argv[2::2], argv[3::2], strict=False))
    assert argv[:2] == ["/bin/s2s", "serve"]
    assert flags["--responses_api_base_url"] == "http://127.0.0.1:8090/v1"
    assert flags["--model_name"] == "qwen3-coder-30b-a3b"
    assert flags["--port"] == "18765" and flags["--host"] == "127.0.0.1"
    assert flags["--kokoro_voice"] == "bm_george" and flags["--num_pipelines"] == "2"
    assert "lk-1" not in argv  # a key never on argv
    assert env == {"OPENAI_API_KEY": "lk-1"}


def test_the_owners_openai_key_never_reaches_the_local_model(settings):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    settings.openai_api_key = "sk-owner"
    _, env = voice_argv(settings, "/bin/s2s")
    assert env["OPENAI_API_KEY"] != "sk-owner"


def test_more_flags_go_last_as_a_command_line(settings):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    settings.voice_server_args = "--stt_model_name openai/whisper-base --kokoro_speed '1.1'"
    argv, _ = voice_argv(settings, "/bin/s2s")
    assert argv[-4:] == ["--stt_model_name", "openai/whisper-base", "--kokoro_speed", "1.1"]


def test_another_tts_takes_no_kokoro_voice(settings):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    settings.voice_server_tts = "qwen3"
    argv, _ = voice_argv(settings, "/bin/s2s")
    assert "--kokoro_voice" not in argv


def test_no_local_model_is_no_voice_server(settings):
    with pytest.raises(ServerConfigError, match="LOCAL_AGENT_BASE_URL"):
        voice_argv(settings, "/bin/s2s")


def test_serve_becomes_the_server(settings, downloaded, monkeypatch):
    monkeypatch.setattr(runtimes, "llama_server", lambda cache: "/bin/llama-server")
    became: list = []

    serve("llm", settings, execvpe=lambda path, argv, env: became.append((path, argv, env)))

    [(path, argv, env)] = became
    assert path == "/bin/llama-server" and argv[0] == path and env["PATH"] == os.environ["PATH"]


def test_serve_the_voice_passes_its_key_in_the_environment(settings, monkeypatch):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    became: list = []

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(env))

    assert became[0]["OPENAI_API_KEY"] == "keryx-local"


@pytest.mark.parametrize(
    ("kind", "missing", "says"),
    [("llm", "llama_server", "llama-server is not installed"),
     ("voice", "speech_to_speech", "speech-to-speech is not installed"),
     ("tts", None, "no tts server")],
)
def test_serve_refuses_saying_why(settings, monkeypatch, kind, missing, says):
    if missing:
        monkeypatch.setattr(runtimes, missing, lambda *args: None)
    with pytest.raises(ServerConfigError, match=says):
        serve(kind, settings, execvpe=lambda *args: None)


def test_free_port_steps_past_one_in_use():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert free_port(taken) != taken


def test_wait_for_asks_until_it_answers_or_the_time_is_up():
    asked: list[str] = []
    answers = iter([Probe(False, problem="refused"), Probe(True, ("m",))])
    clock = iter([0.0, 1.0, 2.0])
    endpoint = Endpoint.parse("http://127.0.0.1:8090")

    def models(found):
        asked.append(found.base_url)
        return next(answers)

    assert wait_for(endpoint, "models", 10, models=models, sleep=lambda s: None,
                    clock=lambda: next(clock)) is None
    assert len(asked) == 2

    times = iter([0.0, 5.0, 11.0])
    assert wait_for(endpoint, "realtime", 10, realtime=lambda e: "not yet",
                    sleep=lambda s: None, clock=lambda: next(times)) == "not yet"
