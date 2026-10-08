"""`keryx models use` and the wizard's one source for the values they save."""

import httpx
import pytest

from keryx.localmodels import use
from keryx.localmodels.catalog import by_key
from keryx.restart.service import ServiceTarget

CODER = by_key("qwen3-coder-30b-a3b")


def never(*args, **kwargs):
    raise AssertionError("asked when it should not have been")


# --- llama_values ------------------------------------------------------------------------


def test_the_agents_port_steps_past_the_voice_llms_and_the_voice_servers(settings):
    """Free now is not free: the voice LLM's server (8091) and the voice server may not be
    running yet, and two servers set to one port both fail to bind."""
    settings.agent_server_port = 8091
    settings.voice_server_port = 8092
    asked: list[int] = []

    values = use.llama_values(settings, CODER.key, CODER.key,
                              free_port=lambda port: asked.append(port) or port)

    assert values["AGENT_SERVER_PORT"] == 8093
    assert values["LOCAL_AGENT_BASE_URL"] == "http://127.0.0.1:8093/v1"
    assert asked == [8091, 8092, 8093]


def test_the_voice_llms_port_steps_past_the_agents_and_the_voice_servers(settings):
    settings.agent_server_port, settings.voice_server_port = 8091, 8092

    assert use.voice_llm_port(settings, free_port=lambda port: port) == 8093
    settings.agent_server_port = 8090
    assert use.voice_llm_port(settings, free_port=lambda port: port) == 8091


def test_a_search_with_no_port_left_but_taken_ones_says_so():
    from keryx.localmodels.servers import free_port_avoiding

    with pytest.raises(OSError, match="no free port from 8091"):
        free_port_avoiding(lambda port: 8091, 8091, {8091})


def test_a_first_model_is_served_on_a_port_found_free(settings):
    asked: list[int] = []

    def free_port(preferred: int) -> int:
        asked.append(preferred)
        return 8092  # 8090 is another program's; 8091 is the voice LLM's, so never offered

    values = use.llama_values(settings, CODER.key, CODER.key, free_port=free_port)

    assert asked == [8090]
    assert values == {
        "AGENT_SERVER_MODEL": CODER.key,
        "AGENT_SERVER_PORT": 8092,
        "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8092/v1",
        "LOCAL_AGENT_MODEL": CODER.key,
        "LOCAL_AGENT_API_KEY": None,
        "LOCAL_AGENT_API": "anthropic-messages",
    }


def test_a_model_server_already_ours_keeps_its_port(settings):
    """Its own port is taken by itself: asking would move it for nothing."""
    settings.agent_server_model = "gpt-oss-20b"
    settings.agent_server_port = 8095

    values = use.llama_values(settings, "/models/mine.gguf", "mine", free_port=never)

    assert values["AGENT_SERVER_PORT"] == 8095
    assert values["AGENT_SERVER_MODEL"] == "/models/mine.gguf"
    assert values["LOCAL_AGENT_BASE_URL"] == "http://127.0.0.1:8095/v1"
    assert values["LOCAL_AGENT_MODEL"] == "mine"


# --- enable_local ------------------------------------------------------------------------


def test_enabling_the_local_agent_adds_it_once_and_leaves_the_default(settings):
    settings.agents_enabled = ["claude", "codex"]

    values = use.enable_local(settings, default=False)

    assert values == {"AGENTS_ENABLED": "claude,codex,local", "AGENT_BACKEND": "claude"}


def test_enabling_it_twice_does_not_list_it_twice(settings):
    settings.agents_enabled = ["claude", "local"]

    assert use.enable_local(settings, default=False)["AGENTS_ENABLED"] == "claude,local"


def test_as_the_default_it_does_what_nobody_named_an_agent_for(settings):
    values = use.enable_local(settings, default=True)

    assert values == {"AGENTS_ENABLED": "claude,local", "AGENT_BACKEND": "local"}


# --- server_status -----------------------------------------------------------------------


def test_with_nothing_set_up_every_server_is_absent_and_nothing_is_asked(settings):
    status = use.server_status(settings, installed=lambda kind: False, probe=never,
                               which=lambda name: None)

    assert status == {
        "llm": {"model": None, "port": 8090, "url": None, "binary": None,
                "unit_installed": False, "reachable": False, "served_model": None},
        "voice": {"url": None, "port": 8765, "unit_installed": False, "reachable": False},
        "local_agent": {"base_url": None, "model": None, "api": "anthropic-messages",
                        "enabled": False, "default": False},
    }


def test_a_model_server_and_a_voice_here_are_asked_whether_they_answer(settings):
    settings.agent_server_model = CODER.key
    settings.voice_base_url = "http://127.0.0.1:8765/v1"
    settings.local_agent_base_url = "http://127.0.0.1:8090/v1"
    settings.local_agent_model = CODER.key
    settings.agents_enabled = ["claude", "local"]
    settings.agent_backend = "local"
    asked: list[str] = []

    def probe(url: str) -> tuple[bool, str | None]:
        asked.append(url)
        # The voice server answers too, and names a model; only the model server's counts.
        return (True, "qwen3-coder") if url.endswith(":8090/v1") else (False, "kokoro")

    status = use.server_status(
        settings, installed=lambda kind: kind == "llm", probe=probe,
        which=lambda name: f"/opt/homebrew/bin/{name}",
    )

    assert status["llm"] == {
        "model": CODER.key, "port": 8090, "url": "http://127.0.0.1:8090/v1",
        "binary": "/opt/homebrew/bin/llama-server", "unit_installed": True, "reachable": True,
        "served_model": "qwen3-coder",
    }
    assert status["voice"] == {"url": "http://127.0.0.1:8765/v1", "port": 8765,
                               "unit_installed": False, "reachable": False}
    assert status["local_agent"] == {
        "base_url": "http://127.0.0.1:8090/v1", "model": CODER.key,
        "api": "anthropic-messages", "enabled": True, "default": True,
    }
    assert sorted(asked) == ["http://127.0.0.1:8090/v1", "http://127.0.0.1:8765/v1"]


def test_the_status_carries_no_key(settings):
    settings.local_agent_base_url = "http://gpu-box:8000/v1"
    settings.local_agent_api_key = "lk-secret"
    settings.voice_base_url = "http://gpu-box:8765/v1"
    settings.voice_api_key = "vk-secret"

    status = use.server_status(settings, installed=lambda kind: False,
                               probe=lambda url: (False, None), which=lambda name: None)

    assert "secret" not in repr(status)


def test_the_llama_server_keryx_installed_is_found_off_path(settings):
    build = use.runtimes.llama_cpp_dir(settings.cache_dir) / "bin" / "llama-server"
    build.parent.mkdir(parents=True)
    build.write_bytes(b"#!/bin/sh\n")
    build.chmod(0o755)

    status = use.server_status(settings, installed=lambda kind: False,
                               probe=never, which=lambda name: None)

    assert status["llm"]["binary"] == str(build)


# --- probe: whether it answers -----------------------------------------------------------


def test_a_remote_address_is_not_reachable_and_is_never_asked():
    assert use.probe("http://gpu-box:8000/v1", get=never) == (False, None)
    assert use.probe("http://100.101.102.103:8000/v1", get=never) == (False, None)


def test_a_loopback_server_is_asked_for_its_models_within_a_second():
    asked: list = []

    def get(url, *, timeout, trust_env):
        assert trust_env is False  # a proxy would carry "loopback" off the machine
        asked.append((url, timeout))
        return httpx.Response(200, json={"data": []})

    assert use.probe("http://127.0.0.1:8090/v1", get=get)[0] is True
    assert use.probe("http://localhost:8765/v1", get=get)[0] is True
    assert asked == [("http://127.0.0.1:8090/v1/models", 1.0),
                     ("http://localhost:8765/v1/models", 1.0)]


@pytest.mark.parametrize("answer", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_a_loopback_server_that_does_not_answer_is_not_reachable(answer):
    def get(url, *, timeout, trust_env):
        raise answer

    assert use.probe("http://127.0.0.1:8090/v1", get=get)[0] is False


def test_a_server_that_answers_with_an_error_is_not_reachable():
    def get(url, *, timeout, trust_env):
        return httpx.Response(503)

    assert use.probe("http://127.0.0.1:8090/v1", get=get)[0] is False


def test_a_server_without_a_model_list_still_answers():
    """speech-to-speech serves the Realtime socket, not necessarily `/models`: a 404 is a
    server that is up."""

    def get(url, *, timeout, trust_env):
        return httpx.Response(404)

    assert use.probe("http://127.0.0.1:8765/v1", get=get)[0] is True


def test_an_address_that_is_not_one_is_not_reachable():
    assert use.probe("not a url", get=never) == (False, None)


def test_a_model_server_that_is_up_but_not_serving_its_model_says_so(settings):
    """The setting says which model ought to be served; `served_model` is what is."""
    settings.agent_server_model = CODER.key

    status = use.server_status(settings, installed=lambda kind: True,
                               probe=lambda url: (True, None), which=lambda name: None)

    assert status["llm"]["model"] == CODER.key
    assert status["llm"]["reachable"] is True and status["llm"]["served_model"] is None


# --- probe: which model it serves --------------------------------------------------------


def llama_models(*ids: str) -> dict:
    """What llama-server answers on `GET /v1/models`: the OpenAI list, one entry a model."""
    return {"object": "list",
            "data": [{"id": id_, "object": "model", "owned_by": "llamacpp"} for id_ in ids]}


def test_a_loopback_server_names_the_first_model_it_serves():
    asked: list = []

    def get(url, *, timeout, trust_env):
        assert trust_env is False
        asked.append((url, timeout))
        return httpx.Response(200, json=llama_models("qwen3-coder", "other"))

    assert use.probe("http://127.0.0.1:8090/v1", get=get) == (True, "qwen3-coder")
    assert asked == [("http://127.0.0.1:8090/v1/models", 1.0)]


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(200, json=llama_models()),  # up, nothing loaded
     httpx.Response(200, json={"models": []}),  # no OpenAI list
     httpx.Response(200, json=[{"id": "a-list-not-an-object"}]),
     httpx.Response(200, json={"data": ["not-an-object"]}),
     httpx.Response(200, json={"data": [{"id": 7}]}),
     httpx.Response(200, json={"data": [{"id": ""}]}),
     httpx.Response(200, text="not json"),
     httpx.Response(404, json=llama_models("qwen3-coder"))],  # up, but no model list
)
def test_a_server_that_answers_without_a_model_is_up_and_serves_none(answer):
    assert use.probe("http://127.0.0.1:8090/v1", get=lambda url, **_: answer) == (True, None)


@pytest.mark.parametrize("answer", [httpx.ConnectError("refused"), httpx.Response(503)])
def test_a_server_that_is_not_up_serves_nothing(answer):
    def get(url, *, timeout, trust_env):
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert use.probe("http://127.0.0.1:8090/v1", get=get) == (False, None)


def test_the_served_model_is_read_through_the_transport_without_a_proxy(monkeypatch):
    """End to end over httpx's own client, its transport faked. With a proxy in the
    environment (or macOS's system settings), httpx would send even a 127.0.0.1 request to
    it, and a proxy's 407 would read as a server that is up."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.com:3128")
    proxies: list = []
    seen: list[str] = []
    real_init = httpx.HTTPTransport.__init__

    def recording_init(self, *args, proxy=None, **kwargs):
        proxies.append(proxy)
        real_init(self, *args, proxy=proxy, **kwargs)

    def answer(self, request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=llama_models("qwen3-coder"), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", recording_init)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", answer)

    assert use.probe("http://127.0.0.1:8090/v1") == (True, "qwen3-coder")
    assert seen == ["http://127.0.0.1:8090/v1/models"]
    assert proxies and all(proxy is None for proxy in proxies)


# --- unit_installed ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "kind", "target"),
    [("darwin", "llm", ServiceTarget("launchd", "dev.keryx.llm")),
     ("darwin", "voice", ServiceTarget("launchd", "dev.keryx.voice")),
     ("linux", "llm", ServiceTarget("systemd", "keryx-llm.service")),
     ("linux", "voice", ServiceTarget("systemd", "keryx-voice.service"))],
)
def test_each_server_is_its_own_unit(platform, kind, target):
    asked: list = []

    def is_installed(found):
        asked.append(found)
        return True

    assert use.unit_installed(kind, platform=platform, is_installed=is_installed) is True
    assert asked == [target]


def test_a_platform_with_no_service_manager_has_no_unit():
    assert use.unit_installed("llm", platform="win32", is_installed=never) is False



# --- what the agent's model takes on this machine ------------------------------------------


@pytest.mark.parametrize(("update", "gb"), [
    ({}, 0.0),  # no local agent
    ({"agent_server_model": "qwen3-coder-30b-a3b"}, 25),
    ({"agent_server_model": "/models/own.gguf"}, None),
    ({"local_agent_base_url": "http://gpu-box:11434/v1", "local_agent_model": "big"}, 0.0),
    ({"local_agent_base_url": "http://127.0.0.1:11434/v1",
      "local_agent_model": "hf.co/ggml-org/gpt-oss-20b-GGUF:MXFP4"}, 16),
    ({"local_agent_base_url": "http://127.0.0.1:1234/v1", "local_agent_model": "lm-studio"},
     None),
])
def test_the_agents_model_is_sized_only_when_it_runs_here(settings, update, gb):
    assert use.agent_memory_gb(settings.model_copy(update=update)) == gb


def test_a_server_stopped_with_keryx_is_still_installed():
    """`keryx service stop --all` boots the voice LLM's server out and keeps its plist: it
    is installed and stopped, not gone, as `service --json` says."""
    from keryx.restart.service import unit_file

    plist = unit_file(ServiceTarget("launchd", "dev.keryx.voice-llm"))
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>")

    assert use.unit_installed("voice_llm", platform="darwin") is True  # loaded: no
    assert use.unit_installed("voice", platform="darwin") is False


# --- what the voice's own LLM takes, and the recommendations ------------------------------

from keryx.localmodels.hardware import Hardware  # noqa: E402

RTX_5090 = Hardware("cuda", "RTX 5090", 31.8, 62)
MAC = {gb: Hardware("metal", "Apple silicon", 0, gb) for gb in (16, 48, 64)}


@pytest.mark.parametrize(("update", "gb"), [
    ({}, 0.0),  # shared: the agent's model, counted as the agent's
    ({"voice_llm": "here"}, 0.0),  # none named yet: nothing runs
    ({"voice_llm": "here", "voice_llm_model": "qwen3.5-9b"}, 9),
    ({"voice_llm": "here", "voice_llm_model": "/models/own.gguf"}, None),
    ({"voice_llm": "remote", "voice_llm_base_url": "https://api.openai.com/v1",
      "voice_llm_model": "qwen3.5-9b"}, 0.0),  # elsewhere: nothing here
])
def test_the_voices_own_llm_is_sized_only_while_it_runs_here(settings, update, gb):
    assert use.voice_llm_memory_gb(settings.model_copy(update=update)) == gb


def keys(advice):
    return (getattr(advice.recommended, "key", None),
            getattr(advice.with_local_voice, "key", None), advice.voice_llm,
            advice.voice_llm_separate)


def test_a_5090_with_the_agent_elsewhere_keeps_the_voices_words_shared(settings):
    """The agent the machine would be given (the tested 25 GB one, beside the speech models'
    3) leaves 3.8 GB: no voice model fits beside it, so the owner's rule says shared."""
    advice = use.recommendations(settings, RTX_5090)

    assert keys(advice) == (CODER.key, CODER.key, None, False)
    assert not advice.fits_with_voice_llm(CODER)  # 25 + 6 + 3 = 34 > 31.8
    assert advice.fits_with_voice_llm(by_key("gpt-oss-20b"))  # 16 + 9 + 3 = 28
    assert not advice.fits_with_voice_llm(by_key("gpt-oss-120b"))


def test_a_64_gb_mac_gives_the_voice_a_model_of_its_own_beside_the_agents(settings):
    advice = use.recommendations(settings, MAC[64])

    # 25 + 9 + 3 = 37 <= 48: the larger of the two small models.
    assert keys(advice) == (CODER.key, CODER.key, "qwen3.5-9b", True)
    assert advice.fits_with_voice_llm(CODER)


def test_the_rule_is_applied_to_the_agents_model_already_here(settings):
    """A 48 GB Mac (36 for models) running gpt-oss-20b (16) has room for the 9b (28); the
    tested model it would otherwise be given (25) only for the 4b (34)."""
    assert keys(use.recommendations(settings, MAC[48]))[2:] == ("qwen3.5-4b", True)
    running = settings.model_copy(update={"agent_server_model": "gpt-oss-20b"})
    assert keys(use.recommendations(running, MAC[48]))[2:] == ("qwen3.5-9b", True)
    own = settings.model_copy(update={"agent_server_model": "/models/own.gguf"})
    assert keys(use.recommendations(own, MAC[64]))[2:] == (None, False)  # cannot be sized


def test_the_speech_models_are_kept_free_only_while_the_voice_is_here(settings):
    tight = Hardware("cuda", "RTX 4090", 27.0, 64)  # 25 + 3 = 28 > 27
    here = settings.model_copy(update={"voice_mode_explicit": "here"})
    # The loopback address alone, with the mode said to be OpenAI's, is not the voice here.
    stale = settings.model_copy(update={"voice_mode_explicit": "openai",
                                        "voice_base_url": "http://127.0.0.1:8765/v1"})

    assert use.recommendations(here, tight).recommended.key == "qwen3.8-27b"
    assert use.recommendations(stale, tight).recommended.key == CODER.key
    assert use.recommendations(stale, tight).with_local_voice.key == "qwen3.8-27b"


def test_a_voice_llm_already_chosen_is_reserved_and_kept(settings):
    chosen = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": "qwen3.5-9b",
                                         "voice_mode_explicit": "here"})
    advice = use.recommendations(chosen, MAC[48])

    # 25 + 9 + 3 = 37 > 36: the agent's model makes room for the voice's, never the other way.
    assert keys(advice) == ("qwen3.8-27b", "qwen3.8-27b", "qwen3.5-9b", True)
    assert advice.fits_with_voice_llm(by_key("qwen3.8-27b"))
    assert not advice.fits_with_voice_llm(CODER)


def test_nothing_fits_a_small_machine(settings):
    advice = use.recommendations(settings, Hardware("cpu", None, 0, 4))

    assert keys(advice) == (None, None, None, False)
