"""Where a call's voice goes, and where its words come from: `Settings.voice_mode`,
`voice_endpoint`, `voice`, `voice_transcription_model`, `voice_llm_endpoint` and
`voice_refusal`.

An existing install sets none of `VOICE_MODE`/`VOICE_LLM*`, and must resolve exactly as it
did before they were settings, apart from the three deliberate fixes, each tested below.
"""

from typing import Any

import pytest

from keryx.config import Settings
from keryx.config.files import config_file, dump_toml, secrets_file, write_private
from keryx.endpoints import OPENAI_BASE_URL, PLACEHOLDER_BEARER, RESPONSES_API, Endpoint
from keryx.realtime import make_provider
from keryx.realtime.openai import realtime_url

HERE_URL = "http://127.0.0.1:8765/v1"
SERVER_URL = "https://voice.example.com/v1"
AGENT_URL = "http://127.0.0.1:8090/v1"
REMOTE_LLM_URL = "http://gpu-box:11434/v1"
REALTIME = "gpt-realtime-2.1"
TRANSCRIBE = "gpt-4o-mini-transcribe"


def _settings(**values: Any) -> Settings:
    values.setdefault("openai_api_key", "sk-owner")
    return Settings(_env_file=None, **values)


def _stored(config: dict[str, Any], secrets: dict[str, Any] | None = None) -> Settings:
    """Settings read from a store holding `config` and `secrets`, as `keryx serve` reads."""
    write_private(config_file(), dump_toml(config))
    if secrets:
        write_private(secrets_file(), dump_toml(secrets))
    return Settings(_env_file=None)


# --- the mode ------------------------------------------------------------------------------


@pytest.mark.parametrize(("values", "mode"), [
    ({}, "openai"),
    ({"voice_base_url": HERE_URL}, "here"),
    ({"voice_base_url": SERVER_URL}, "server"),
    # The loopback address of another port is not this machine's voice: as before.
    ({"voice_base_url": "http://127.0.0.1:9999/v1"}, "server"),
    ({"voice_base_url": "http://127.0.0.1:9999/v1", "voice_server_port": 9999}, "here"),
])
def test_an_unset_voice_mode_is_worked_out_as_it_was_before(values, mode):
    assert _settings(**values).voice_mode == mode


@pytest.mark.parametrize("mode", ["openai", "here", "server"])
def test_voice_mode_says_where_the_voice_is_whatever_the_address(mode):
    # A rolled-forward `voice use here` keeps writing the loopback URL for one release; the
    # mode, once set, is what decides.
    assert _settings(voice_mode_explicit=mode, voice_base_url=HERE_URL).voice_mode == mode


def test_voice_mode_is_read_from_the_store_under_its_key():
    assert _stored({"VOICE_MODE": "here"}).voice_mode == "here"


# --- each mode × each source of the pipeline's words ----------------------------------------

#: What each mode resolves to: the endpoint (base, key, model), its socket, the timbre and
#: the transcription model in `session.update`.
MODES = {
    "openai": ((OPENAI_BASE_URL, "sk-owner", REALTIME),
               f"wss://api.openai.com/v1/realtime?model={REALTIME}", "marin", TRANSCRIBE),
    # The voice server's key goes to the pipeline here too, as it did: one given its own
    # through `VOICE_SERVER_ARGS` checks it.
    "here": ((HERE_URL, "vk", ""), "ws://127.0.0.1:8765/v1/realtime", "", None),
    "server": ((SERVER_URL, "vk", REALTIME),
               f"wss://voice.example.com/v1/realtime?model={REALTIME}", "", None),
}
#: What each `VOICE_LLM` resolves to: (base, key, model) of the pipeline's words.
SOURCES = {
    "shared": (AGENT_URL, None, "qwen3-coder-30b-a3b"),
    "here": ("http://127.0.0.1:8091/v1", None, "qwen3.5-4b"),
    "remote": (REMOTE_LLM_URL, "lk-voice", "qwen3.5-9b"),
}
SOURCE_VALUES = {
    "shared": {},
    "here": {"voice_llm_model": "qwen3.5-4b"},
    "remote": {"voice_llm_base_url": REMOTE_LLM_URL, "voice_llm_model": "qwen3.5-9b",
               "voice_llm_api_key": "lk-voice"},
}


@pytest.mark.parametrize("source", list(SOURCES))
@pytest.mark.parametrize("mode", list(MODES))
def test_each_mode_and_each_source_reads_only_its_own_settings(mode, source):
    values: dict[str, Any] = {
        "voice_mode_explicit": mode, "voice_base_url": SERVER_URL, "voice_api_key": "vk",
        "local_agent_base_url": AGENT_URL, "local_agent_model": "qwen3-coder-30b-a3b",
        "voice_llm": source, **SOURCE_VALUES[source],
    }
    if mode != "server":  # a chosen Kokoro voice, which only a server elsewhere is sent
        values["voice_server_voice"] = "bm_george"
    settings = _settings(**values)
    (base, key, model), url, voice, transcription = MODES[mode]

    endpoint = settings.voice_endpoint
    assert (endpoint.base_url, endpoint.api_key, endpoint.model) == (base, key, model)
    assert realtime_url(endpoint) == url
    assert settings.voice == voice
    assert settings.voice_transcription_model == transcription

    words = settings.voice_llm_endpoint
    assert (words.base_url, words.api_key, words.model) == SOURCES[source]
    assert words.api == RESPONSES_API
    assert settings.voice_refusal() is None


def test_the_here_endpoint_is_derived_from_the_voice_port_not_the_address():
    settings = _settings(voice_mode_explicit="here", voice_server_port=8770,
                         voice_base_url="http://127.0.0.1:8765/v1")

    assert settings.voice_endpoint.base_url == "http://127.0.0.1:8770/v1"
    assert settings.voice_here_url == "http://127.0.0.1:8770/v1"


def test_the_voice_llm_here_listens_on_its_own_port():
    settings = _settings(voice_llm="here", voice_llm_model="qwen3.5-4b",
                         voice_llm_server_port=8099)

    assert settings.voice_llm_endpoint.base_url == "http://127.0.0.1:8099/v1"


def test_shared_words_are_the_local_agents_endpoint_and_none_without_one():
    settings = _settings(local_agent_base_url="http://box:8080", local_agent_api_key="lk",
                         local_agent_model="qwen")
    agent, words = settings.local_agent_endpoint, settings.voice_llm_endpoint

    assert (words.base_url, words.api_key, words.model, words.bearer) == (
        agent.base_url, agent.api_key, agent.model, agent.bearer)
    assert _settings().voice_llm_endpoint is None


def test_a_remote_voice_llm_with_no_address_has_no_endpoint():
    assert _settings(voice_llm="remote", voice_llm_model="m").voice_llm_endpoint is None


# --- the key rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "values"), [
    ("shared", {"local_agent_base_url": "http://box:11434", "local_agent_model": "m"}),
    ("here", {"voice_llm_model": "qwen3.5-4b"}),
    ("remote", {"voice_llm_base_url": "http://gpu-box:11434", "voice_llm_model": "m"}),
    ("remote", {"voice_llm_base_url": "https://api.together.xyz/v1", "voice_llm_model": "m"}),
    # OpenAI's name in a path is not OpenAI's host.
    ("remote", {"voice_llm_base_url": "https://gw.example.com/api.openai.com/v1",
                "voice_llm_model": "m"}),
])
def test_owner_openai_key_never_reaches_a_non_openai_voice_llm(source, values):
    words = _settings(openai_api_key="sk-owner", voice_llm=source, **values).voice_llm_endpoint

    assert words.api_key != "sk-owner" and words.bearer != "sk-owner"
    assert words.bearer == PLACEHOLDER_BEARER


def test_openais_own_host_as_the_voice_llm_is_sent_the_owners_key_unless_it_has_one():
    openai = _settings(openai_api_key="sk-owner", voice_llm="remote",
                       voice_llm_base_url="https://api.openai.com", voice_llm_model="gpt-5-mini")
    assert openai.voice_llm_endpoint.api_key == "sk-owner"
    assert openai.voice_llm_endpoint.base_url == OPENAI_BASE_URL

    own = openai.model_copy(update={"voice_llm_api_key": "sk-voice"})
    assert own.voice_llm_endpoint.api_key == "sk-voice"
    elsewhere = openai.model_copy(update={"voice_llm_base_url": "http://gpu-box:11434/v1",
                                          "voice_llm_api_key": "lk"})
    assert elsewhere.voice_llm_endpoint.api_key == "lk"


def test_the_placeholder_openai_key_is_never_sent_to_openai_either():
    words = _settings(openai_api_key=None, voice_llm="remote", voice_llm_model="m",
                      voice_llm_base_url=OPENAI_BASE_URL).voice_llm_endpoint

    assert words.api_key is None


# --- what refuses a call ----------------------------------------------------------------------


def test_an_unset_mode_is_refused_as_before():
    keyless = _settings(openai_api_key=None)

    assert keyless.voice_refusal() == (
        "OPENAI_API_KEY is not set and VOICE_BASE_URL names no voice server of your own, "
        "so no call could open — `keryx setup`"
    )
    assert keyless.model_copy(update={"voice_base_url": HERE_URL}).voice_refusal() is None
    assert keyless.model_copy(update={"voice_base_url": SERVER_URL}).voice_refusal() is None


@pytest.mark.parametrize(("values", "named"), [
    ({"voice_mode_explicit": "openai", "openai_api_key": None,
      "voice_base_url": HERE_URL}, "OPENAI_API_KEY"),
    ({"voice_mode_explicit": "server"}, "VOICE_BASE_URL"),
    ({"voice_mode_explicit": "here", "voice_llm": "here"}, "VOICE_LLM_MODEL"),
    ({"voice_mode_explicit": "here", "voice_llm": "remote", "voice_llm_model": "m"},
     "VOICE_LLM_BASE_URL"),
    ({"voice_mode_explicit": "here", "voice_llm": "remote",
      "voice_llm_base_url": REMOTE_LLM_URL}, "VOICE_LLM_MODEL"),
])
def test_each_mode_is_refused_for_its_own_missing_setting(values, named):
    refusal = _settings(**values).voice_refusal()

    assert refusal is not None and named in refusal and "keryx setup" in refusal


def test_a_voice_llm_is_not_asked_of_a_voice_that_does_not_use_it():
    for mode in ("openai", "server"):
        settings = _settings(voice_mode_explicit=mode, voice_base_url=SERVER_URL,
                             voice_llm="remote")
        assert settings.voice_refusal() is None


def test_a_server_with_no_address_has_no_endpoint_and_no_provider():
    settings = _settings(voice_mode_explicit="server")

    assert settings.voice_endpoint is None
    with pytest.raises(ValueError, match="VOICE_BASE_URL"):
        make_provider(settings)


# --- an existing install: today's keys only --------------------------------------------------

LEGACY_HERE = {
    "VOICE_BASE_URL": HERE_URL,
    "VOICE_SERVER_PORT": 8765,
    "VOICE_SERVER_VOICE": "bf_emma",
    "LOCAL_AGENT_BASE_URL": AGENT_URL,
    "LOCAL_AGENT_MODEL": "qwen3-coder-30b-a3b",
    "LLM_SERVER_MODEL": "qwen3-coder-30b-a3b",
    "LLM_SERVER_PORT": 8090,
}
OWNER_KEY = {"OPENAI_API_KEY": "sk-owner"}


def test_a_legacy_install_whose_voice_is_here_resolves_as_today_but_for_the_fixes():
    settings = _stored(LEGACY_HERE, OWNER_KEY)

    assert settings.voice_mode == "here"
    endpoint = settings.voice_endpoint
    assert (endpoint.base_url, endpoint.api_key) == (HERE_URL, None)  # today's
    assert settings.voice == ""  # today's: the pipeline speaks in its own Kokoro voice
    # The words: today's endpoint, byte for byte, which the voice server's argv is made of.
    words, agent = settings.voice_llm_endpoint, settings.local_agent_endpoint
    assert (words.base_url, words.model, words.bearer) == (
        AGENT_URL, "qwen3-coder-30b-a3b", PLACEHOLDER_BEARER)
    assert (words.base_url, words.api_key, words.model) == (
        agent.base_url, agent.api_key, agent.model)
    assert settings.voice_refusal() is None


def test_fix_b_the_pipeline_here_is_sent_no_realtime_or_transcription_model():
    settings = _stored(LEGACY_HERE, OWNER_KEY)

    # Before: `ws://127.0.0.1:8765/v1/realtime?model=gpt-realtime-2.1`, and the transcription
    # block naming `gpt-4o-mini-transcribe`. speech-to-speech ignored both.
    assert settings.voice_endpoint.model == ""
    assert realtime_url(settings.voice_endpoint) == "ws://127.0.0.1:8765/v1/realtime"
    assert settings.voice_transcription_model is None


def test_fix_a_openai_voice_no_longer_overrides_the_kokoro_voice_here():
    settings = _stored({**LEGACY_HERE, "OPENAI_VOICE": "ash"}, OWNER_KEY)

    assert settings.voice == ""  # before: "ash", sent per session over `VOICE_SERVER_VOICE`


def test_a_legacy_install_on_openai_resolves_exactly_as_today():
    settings = _stored({"OPENAI_VOICE": "ash", "OPENAI_REALTIME_MODEL": "gpt-realtime"},
                       OWNER_KEY)

    assert settings.voice_mode == "openai"
    assert settings.voice_endpoint == Endpoint(OPENAI_BASE_URL, "sk-owner", "gpt-realtime")
    assert realtime_url(settings.voice_endpoint) == (
        "wss://api.openai.com/v1/realtime?model=gpt-realtime")
    assert (settings.voice, settings.voice_transcription_model) == ("ash", TRANSCRIBE)
    assert _stored({}, OWNER_KEY).voice == "marin"


def test_a_legacy_server_elsewhere_is_still_sent_the_realtime_model_and_its_voice():
    settings = _stored({"VOICE_BASE_URL": SERVER_URL, "OPENAI_VOICE": "ash"},
                       {**OWNER_KEY, "VOICE_API_KEY": "vk"})

    assert settings.voice_mode == "server"
    assert settings.voice_endpoint == Endpoint(SERVER_URL, "vk", REALTIME)
    assert realtime_url(settings.voice_endpoint) == (
        f"wss://voice.example.com/v1/realtime?model={REALTIME}")
    assert settings.voice == "ash"  # OPENAI_VOICE, read for one release


def test_fix_b_a_server_elsewhere_is_sent_no_transcription_model():
    settings = _stored({"VOICE_BASE_URL": SERVER_URL}, OWNER_KEY)

    assert settings.voice_transcription_model is None  # before: gpt-4o-mini-transcribe


def test_fix_c_a_server_elsewhere_speaks_in_the_kokoro_voice_when_one_was_chosen():
    chosen = _stored({"VOICE_BASE_URL": SERVER_URL, "OPENAI_VOICE": "ash",
                      "VOICE_SERVER_VOICE": "bm_george"}, OWNER_KEY)
    assert chosen.voice == "bm_george"  # before: "ash"

    neither = _stored({"VOICE_BASE_URL": SERVER_URL}, OWNER_KEY)
    assert neither.voice == ""  # its default `af_heart` was never chosen, so is not sent


def test_a_server_at_openais_own_address_keeps_openais_voice_and_transcription():
    # `VOICE_BASE_URL=https://api.openai.com/v1` was a server by the old rule, and was
    # sent the persona and the transcription model: OpenAI is still OpenAI.
    settings = _stored({"VOICE_BASE_URL": OPENAI_BASE_URL, "VOICE_SERVER_VOICE": "bm_george"},
                       {**OWNER_KEY, "VOICE_API_KEY": "sk-voice"})

    assert settings.voice_mode == "server"
    assert settings.voice == "marin"
    assert settings.voice_transcription_model == TRANSCRIBE
