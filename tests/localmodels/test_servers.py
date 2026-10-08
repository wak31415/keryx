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
    settings.agent_server_model = CODER.key
    return path


def test_the_model_server_serves_the_catalog_model_under_its_name(settings, downloaded):
    settings.agent_server_port = 8091

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
    settings.agent_server_model = str(own)

    assert llm_model(settings) == ("my-model.Q5", own, servers.DEFAULT_CONTEXT)


@pytest.mark.parametrize(
    ("value", "says"),
    [(None, "AGENT_SERVER_MODEL is not set"), ("qwen3-coder-30b-a3b", "keryx models pull"),
     ("/no/such.gguf", "is the path right")],
)
def test_what_is_missing_is_said(settings, value, says):
    settings.agent_server_model = value
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

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(env),
          words_up=lambda endpoint: True)

    assert became[0]["OPENAI_API_KEY"] == "keryx-local"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_the_voice_server_waits_for_its_words_before_it_starts(settings, monkeypatch, capsys):
    """At login the voice and model servers load together: the voice server waits for the
    words rather than crash on them until llama.cpp has loaded."""
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    answers = iter([False, False, True])
    asked: list[str] = []
    clock, became = Clock(), []

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(argv),
          words_up=lambda endpoint: asked.append(endpoint.base_url) or next(answers),
          sleep=clock.sleep, clock=lambda: clock.now)

    assert asked == ["http://127.0.0.1:8090/v1"] * 3
    assert clock.slept == [servers.WORDS_POLL_S] * 2 == [2.0, 2.0]
    assert became and became[0][0] == "/bin/s2s"
    assert "waiting for the local model server at http://127.0.0.1:8090/v1" in \
        capsys.readouterr().err


def test_a_voice_server_whose_words_never_answer_does_not_start(settings, monkeypatch):
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    clock, became = Clock(), []

    with pytest.raises(ServerConfigError,
                       match="waiting for the local model server at http://127.0.0.1:8090/v1"):
        serve("voice", settings, execvpe=lambda *args: became.append(args),
              words_up=lambda endpoint: False, sleep=clock.sleep, clock=lambda: clock.now)

    assert became == [] and clock.now >= servers.WORDS_WAIT_S


def test_the_voice_server_waits_as_long_as_the_model_server_may_load():
    from keryx.localmodels import server

    assert servers.WORDS_WAIT_S == server.LLM_START_S


def test_the_voice_server_asks_for_its_words_as_voice_use_here_does(settings, monkeypatch):
    from keryx.localmodels import voice

    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "m"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    monkeypatch.setattr(voice, "words_up", lambda endpoint: True)
    became: list = []

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(argv))

    assert became


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


# --- the voice LLM: where the voice server's words come from --------------------------------

#: An install from before `VOICE_LLM` was a setting: its voice here, its words the local
#: agent's, under the names it was saved with (`LLM_SERVER_*`).
LEGACY_HERE = {
    "VOICE_BASE_URL": "http://127.0.0.1:8765/v1",
    "VOICE_SERVER_PORT": 8765,
    "VOICE_SERVER_VOICE": "bf_emma",
    "VOICE_SERVER_ARGS": "--kokoro_speed 1.1",
    "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
    "LOCAL_AGENT_MODEL": "qwen3-coder-30b-a3b",
    "LLM_SERVER_MODEL": "qwen3-coder-30b-a3b",
    "LLM_SERVER_PORT": 8090,
}
#: What that install's voice server was started with before the voice LLM had a source of
#: its own, byte for byte (taken from the build before it).
LEGACY_ARGV = [
    "/bin/s2s", "serve", "--host", "127.0.0.1", "--port", "8765",
    "--llm_backend", "responses-api", "--responses_api_base_url", "http://127.0.0.1:8090/v1",
    "--model_name", "qwen3-coder-30b-a3b",
    "--stt", "parakeet-tdt", "--tts", "kokoro", "--num_pipelines", "2",
    "--kokoro_voice", "bf_emma", "--kokoro_speed", "1.1",
]


def _stored(config, secrets=None):
    """Settings read from a store holding `config` and `secrets`, as a unit reads them."""
    from keryx.config import Settings
    from keryx.config.files import config_file, dump_toml, secrets_file, write_private

    write_private(config_file(), dump_toml(config))
    if secrets:
        write_private(secrets_file(), dump_toml(secrets))
    return Settings(_env_file=None)


@pytest.mark.parametrize("secrets", [None, {"LOCAL_AGENT_API_KEY": "lk-1"}])
def test_a_legacy_install_starts_its_voice_server_exactly_as_before(secrets):
    settings = _stored(LEGACY_HERE, {"OPENAI_API_KEY": "sk-owner", **(secrets or {})})

    argv, env = voice_argv(settings, "/bin/s2s")

    assert argv == LEGACY_ARGV
    assert env == {"OPENAI_API_KEY": "lk-1" if secrets else "keryx-local"}


def _flags(argv):
    return dict(zip(argv[2::2], argv[3::2], strict=False))


def test_a_voice_llm_of_its_own_here_is_the_voice_servers_words(settings):
    """`VOICE_LLM=here`: its own server on `VOICE_LLM_SERVER_PORT`, and no key of anyone's —
    the local agent's is not lent to it, and the owner's never was."""
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = "qwen3-coder-30b-a3b"
    settings.local_agent_api_key = "lk-1"
    settings.openai_api_key = "sk-owner"
    settings.voice_llm = "here"
    settings.voice_llm_model = "qwen3.5-4b"
    settings.voice_llm_server_port = 8092

    argv, env = voice_argv(settings, "/bin/s2s")

    flags = _flags(argv)
    assert flags["--responses_api_base_url"] == "http://127.0.0.1:8092/v1"
    assert flags["--model_name"] == "qwen3.5-4b"
    assert env == {"OPENAI_API_KEY": "keryx-local"}
    assert "lk-1" not in argv and "sk-owner" not in argv


def test_a_remote_voice_llms_key_reaches_the_voice_server_in_its_environment_only(settings):
    settings.voice_llm = "remote"
    settings.voice_llm_base_url = "http://gpu-box:11434/v1"
    settings.voice_llm_model = "qwen3:4b"
    settings.voice_llm_api_key = "vk-secret"
    settings.openai_api_key = "sk-owner"

    argv, env = voice_argv(settings, "/bin/s2s")

    flags = _flags(argv)
    assert flags["--responses_api_base_url"] == "http://gpu-box:11434/v1"
    assert flags["--model_name"] == "qwen3:4b"
    assert env == {"OPENAI_API_KEY": "vk-secret"}
    assert not any("vk-secret" in arg or "sk-owner" in arg for arg in argv)


def test_a_remote_voice_llm_elsewhere_is_never_handed_the_owners_openai_key(settings):
    settings.voice_llm = "remote"
    settings.voice_llm_base_url = "http://gpu-box:11434/v1"
    settings.voice_llm_model = "qwen3:4b"
    settings.openai_api_key = "sk-owner"

    _, env = voice_argv(settings, "/bin/s2s")

    assert env == {"OPENAI_API_KEY": "keryx-local"}


def test_openais_own_host_as_the_voice_llm_is_handed_the_owners_key_in_the_environment(settings):
    settings.voice_llm = "remote"
    settings.voice_llm_base_url = "https://api.openai.com/v1"
    settings.voice_llm_model = "gpt-5.4-mini"
    settings.openai_api_key = "sk-owner"

    argv, env = voice_argv(settings, "/bin/s2s")

    assert _flags(argv)["--responses_api_base_url"] == "https://api.openai.com/v1"
    assert env == {"OPENAI_API_KEY": "sk-owner"} and "sk-owner" not in argv


@pytest.mark.parametrize(("values", "says"), [
    ({}, "LOCAL_AGENT_BASE_URL"),
    ({"voice_llm": "here"}, "VOICE_LLM_MODEL"),
    ({"voice_llm": "remote", "voice_llm_model": "m"}, "VOICE_LLM_BASE_URL"),
    ({"voice_llm": "remote", "voice_llm_base_url": "http://gpu-box:11434/v1"},
     "VOICE_LLM_MODEL"),
])
def test_a_voice_llm_with_nothing_to_ask_is_no_voice_server(settings, values, says):
    for name, value in values.items():
        setattr(settings, name, value)
    with pytest.raises(ServerConfigError, match=says):
        voice_argv(settings, "/bin/s2s")


@pytest.mark.parametrize(("values", "asked", "named"), [
    ({"local_agent_base_url": "http://127.0.0.1:8090/v1", "local_agent_model": "m"},
     "http://127.0.0.1:8090/v1", "the local model server"),
    ({"voice_llm": "here", "voice_llm_model": "qwen3.5-4b"},
     "http://127.0.0.1:8091/v1", "the voice LLM server"),
    ({"voice_llm": "remote", "voice_llm_base_url": "http://gpu-box:11434/v1",
      "voice_llm_model": "qwen3:4b", "voice_llm_api_key": "vk"},
     "http://gpu-box:11434/v1", "the voice LLM server"),
])
def test_the_voice_server_waits_for_the_voice_llm_it_takes_its_words_from(
        settings, monkeypatch, capsys, values, asked, named):
    for name, value in values.items():
        setattr(settings, name, value)
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    answers = iter([False, True])
    endpoints: list = []
    clock, became = Clock(), []

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(argv),
          words_up=lambda endpoint: endpoints.append(endpoint) or next(answers),
          sleep=clock.sleep, clock=lambda: clock.now)

    assert [endpoint.base_url for endpoint in endpoints] == [asked, asked]
    assert became
    assert f"waiting for {named} at {asked}" in capsys.readouterr().err
    if values.get("voice_llm_api_key"):  # asked with its own key, as the voice will ask it
        assert endpoints[0].api_key == "vk"


def test_a_voice_llm_here_that_never_answers_says_how_to_start_it(settings, monkeypatch):
    settings.voice_llm, settings.voice_llm_model = "here", "qwen3.5-4b"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    clock = Clock()

    with pytest.raises(ServerConfigError,
                       match="the voice LLM server at http://127.0.0.1:8091/v1.*--voice-llm"):
        serve("voice", settings, execvpe=lambda *args: None, words_up=lambda endpoint: False,
              sleep=clock.sleep, clock=lambda: clock.now)


# --- keryx models serve voice-llm -----------------------------------------------------------

SMALL = by_key("qwen3.5-4b")


def test_the_voice_llm_server_serves_its_own_model_on_its_own_port(settings):
    path = model_path(settings.cache_dir, SMALL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gguf")
    settings.voice_llm_model = SMALL.key
    settings.voice_llm_server_port = 8092

    argv = servers.voice_llm_argv(settings, "/bin/llama-server")

    flags = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert flags["--model"] == str(path) and flags["--alias"] == SMALL.key
    assert flags["--host"] == "127.0.0.1" and flags["--port"] == "8092"
    assert flags["--ctx-size"] == str(SMALL.context)  # its whole context: none is cut
    assert argv[-1] == "--jinja"


def test_the_voice_llm_server_is_the_model_servers_command_line_with_its_model_and_port(
        settings, downloaded, tmp_path):
    """One command line for both llama.cpp servers: only the model and the port differ, and
    the agent's is exactly what it was."""
    own = tmp_path / "voice.gguf"
    own.write_bytes(b"gguf")
    settings.voice_llm_model = str(own)

    agent = llm_argv(settings, "/bin/llama-server")
    words = servers.voice_llm_argv(settings, "/bin/llama-server")

    assert agent == [
        "/bin/llama-server", "--model", str(downloaded), "--alias", CODER.key,
        "--host", "127.0.0.1", "--port", "8090",
        "--n-gpu-layers", "999", "--ctx-size", str(CODER.context), "--jinja",
    ]
    # A GGUF of the voice's own is served under the name the voice asks for: the setting.
    assert words == [
        "/bin/llama-server", "--model", str(own), "--alias", str(own),
        "--host", "127.0.0.1", "--port", "8091",
        "--n-gpu-layers", "999", "--ctx-size", str(servers.DEFAULT_CONTEXT), "--jinja",
    ]
    assert settings.model_copy(update={"voice_llm": "here"}).voice_llm_endpoint.model == str(own)


@pytest.mark.parametrize(
    ("value", "says"),
    [(None, "VOICE_LLM_MODEL is not set"), ("qwen3.5-4b", "keryx models pull qwen3.5-4b"),
     ("/no/such.gguf", "is the path right")],
)
def test_what_the_voice_llm_server_is_missing_is_said(settings, value, says):
    settings.voice_llm_model = value
    with pytest.raises(ServerConfigError, match=says):
        servers.voice_llm_argv(settings, "/bin/llama-server")


def test_serve_becomes_the_voice_llm_server(settings, monkeypatch):
    path = model_path(settings.cache_dir, SMALL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gguf")
    settings.voice_llm_model = SMALL.key
    monkeypatch.setattr(runtimes, "llama_server", lambda cache: "/bin/llama-server")
    became: list = []

    serve("voice-llm", settings,
          execvpe=lambda path, argv, env: became.append((path, argv, env)))

    [(binary, argv, env)] = became
    assert binary == "/bin/llama-server" and "--port" in argv
    assert argv[argv.index("--port") + 1] == "8091"
    assert env["PATH"] == os.environ["PATH"]


def test_the_voice_llm_server_is_served_whatever_the_source_says(settings, monkeypatch):
    """Its unit is started, and seen to answer, before `VOICE_LLM=here` is saved: `serve`
    asks only for its model."""
    path = model_path(settings.cache_dir, SMALL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gguf")
    settings.voice_llm, settings.voice_llm_model = "shared", SMALL.key
    monkeypatch.setattr(runtimes, "llama_server", lambda cache: "/bin/llama-server")
    became: list = []

    serve("voice-llm", settings, execvpe=lambda *args: became.append(args))

    assert became


@pytest.mark.parametrize(("ports", "named"), [
    ({"agent_server_port": 8091}, "the agent's model server"),
    ({"voice_server_port": 8091}, "the voice server"),
    ({"agent_server_port": 8091, "voice_server_port": 8091},
     "the agent's model server and the voice server"),
])
def test_the_voice_llm_server_refuses_another_servers_port_before_anything(
        settings, monkeypatch, ports, named):
    from wording import for_the_app

    for name, value in ports.items():
        setattr(settings, name, value)
    settings.voice_llm_model = SMALL.key
    monkeypatch.setattr(runtimes, "llama_server",
                        lambda cache: pytest.fail("refused before llama.cpp is looked for"))

    with pytest.raises(ServerConfigError) as refused:
        serve("voice-llm", settings, execvpe=lambda *args: pytest.fail("never exec'd"))

    said = str(refused.value)
    assert said == servers.port_clash(settings)
    assert said == (f"The voice LLM's server and {named} are both set to port 8091: give the "
                    "voice LLM a port of its own.")
    assert for_the_app(said)


def test_ports_of_their_own_are_no_clash(settings):
    assert servers.port_clash(settings) is None  # 8090, 8765 and 8091 by default


def test_the_voice_server_asks_its_own_voice_llm_which_model_it_serves(settings, monkeypatch):
    """With `VOICE_LLM=here` the voice server waits for its own server serving its model,
    not for anything answering on the port."""
    from keryx.localmodels import use

    settings.voice_llm, settings.voice_llm_model = "here", "qwen3.5-4b"
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    answers = iter([(True, "qwen3-coder-30b-a3b"), (True, "qwen3.5-4b")])
    monkeypatch.setattr(use, "probe", lambda url: next(answers))
    clock, became = Clock(), []

    serve("voice", settings, execvpe=lambda path, argv, env: became.append(argv),
          sleep=clock.sleep, clock=lambda: clock.now)

    assert became and clock.slept == [servers.WORDS_POLL_S]


def test_no_llama_server_is_no_voice_llm_server(settings, monkeypatch):
    monkeypatch.setattr(runtimes, "llama_server", lambda cache: None)
    with pytest.raises(ServerConfigError, match="llama-server is not installed"):
        serve("voice-llm", settings, execvpe=lambda *args: None)
