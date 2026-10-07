"""`keryx.config.options`: the values a setting can take, found when the owner asks.

Every source is faked: the HTTP getter answers with a body shaped like the provider's own,
Codex's model list is a function handed in, and the hardware and the disk are stand-ins.
Nothing here reaches the network or starts a `codex` process.
"""

import dataclasses
import json
import sys
import threading
import types

import httpx
import pytest

from keryx.config import options
from keryx.config.options import Option, Sources, Unavailable, options_for
from keryx.localmodels import catalog
from keryx.localmodels.hardware import Hardware

OPENAI_BODY = {
    "object": "list",
    "data": [
        {"id": "gpt-realtime-2.1", "object": "model", "created": 1, "owned_by": "system"},
        {"id": "gpt-realtime-mini", "object": "model", "created": 2, "owned_by": "system"},
        {"id": "gpt-4o-mini-transcribe", "object": "model", "created": 3, "owned_by": "system"},
        {"id": "whisper-1", "object": "model", "created": 4, "owned_by": "system"},
        {"id": "gpt-6-luna", "object": "model", "created": 5, "owned_by": "system"},
        {"id": "gpt-6-sol", "object": "model", "created": 6, "owned_by": "system"},
        {"id": "gpt-4o-audio-preview", "object": "model", "created": 7, "owned_by": "s"},
        {"id": "gpt-4o-search-preview", "object": "model", "created": 8, "owned_by": "s"},
        {"id": "gpt-image-2", "object": "model", "created": 9, "owned_by": "system"},
        {"id": "gpt-4o-mini-tts", "object": "model", "created": 10, "owned_by": "system"},
        {"id": "text-embedding-3-large", "object": "model", "created": 11, "owned_by": "s"},
        {"id": "omni-moderation-latest", "object": "model", "created": 12, "owned_by": "s"},
        {"id": "dall-e-3", "object": "model", "created": 13, "owned_by": "system"},
    ],
}
ANTHROPIC_BODY = {
    "data": [
        {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
         "created_at": "2026-08-01T00:00:00Z"},
        {"type": "model", "id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5",
         "created_at": "2026-08-01T00:00:00Z"},
        {"type": "model", "id": "claude-mythos-1", "display_name": "Claude Mythos 1",
         "created_at": "2026-09-01T00:00:00Z"},
    ],
    "has_more": False,
    "first_id": "claude-opus-5-5",
    "last_id": "claude-mythos-1",
}
KEY = "sk-test-not-a-real-key-123456"


class FakeGet:
    """An `httpx.get` that records what it was asked and answers as it was told."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body if self.body is not None else {})


def unreachable(*args, **kwargs):
    raise AssertionError("this source must not be asked")


def sources(**given) -> Sources:
    base = Sources(
        get=unreachable,
        codex_models=unreachable,
        hardware=lambda: Hardware("metal", "Apple silicon", 0.0, 32.0),
        downloaded=lambda entry: False,
    )
    return dataclasses.replace(base, **given)


def values(result) -> list[str]:
    return [option.value for option in result.options]


@pytest.fixture
def keyed(settings):
    return settings.model_copy(update={"openai_api_key": KEY})


# --- the shape -------------------------------------------------------------------------------


def test_as_dict_is_the_contract(keyed):
    get = FakeGet(body=OPENAI_BODY)

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=get)).as_dict()

    assert list(found) == [
        "key", "current", "options", "source", "live", "free_form", "error"
    ]
    assert found["key"] == "OPENAI_REALTIME_MODEL"
    assert found["current"] == "gpt-realtime-2.1"
    assert set(found["options"][0]) == {"value", "label", "detail"}
    assert (found["source"], found["live"], found["free_form"], found["error"]) == (
        "openai", True, True, None
    )


def test_every_supported_key_is_a_setting():
    from keryx.config.settings import field_for

    assert options.OPTION_KEYS
    assert all(field_for(key) is not None for key in options.OPTION_KEYS)


def test_an_unsupported_key_has_no_options(settings):
    assert not options.has_options("PORT")
    with pytest.raises(KeyError):
        options_for("PORT", settings, sources())


# --- OpenAI ------------------------------------------------------------------------------------


def test_realtime_models_are_read_from_openai_with_the_key(keyed):
    get = FakeGet(body=OPENAI_BODY)

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=get))

    assert values(found) == ["gpt-realtime-mini", "gpt-realtime-2.1"]
    ((url, kwargs),) = get.calls
    assert url == "https://api.openai.com/v1/models"
    assert kwargs["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert kwargs["timeout"] == options.TIMEOUT_S == 5.0
    assert kwargs["follow_redirects"] is False


def test_realtime_models_are_only_those_that_can_run_a_voice_session(keyed):
    body = {"data": [
        {"id": "gpt-realtime-2.1"}, {"id": "gpt-realtime-whisper"},
        {"id": "gpt-realtime-translate"}, {"id": "gpt-4o-realtime-preview-2025-06-03"},
        {"id": "gpt-realtime-transcribe"}, {"id": "gpt-realtime-audio-preview"},
    ]}

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=FakeGet(body=body)))

    assert values(found) == ["gpt-realtime-2.1", "gpt-4o-realtime-preview-2025-06-03"]


def test_transcription_models_are_the_transcribers_and_whisper(keyed):
    found = options_for("OPENAI_TRANSCRIPTION_MODEL", keyed, sources(get=FakeGet(body=OPENAI_BODY)))

    assert values(found) == ["whisper-1", "gpt-4o-mini-transcribe"]
    assert found.source == "openai" and found.live


def test_web_search_models_are_the_chat_models_newest_id_first(keyed):
    found = options_for("OPENAI_WEB_SEARCH_MODEL", keyed, sources(get=FakeGet(body=OPENAI_BODY)))

    assert values(found) == ["gpt-6-sol", "gpt-6-luna"]


def test_the_keryx_default_is_marked(keyed):
    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=FakeGet(body=OPENAI_BODY)))

    default = next(option for option in found.options if option.value == "gpt-realtime-2.1")
    assert default.detail == "Keryx's default"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_falls_back_and_says_so_without_the_key(keyed, status):
    get = FakeGet(status=status, body={"error": {"message": f"Incorrect API key {KEY}"}})

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=get))

    assert (found.source, found.live, found.error) == (
        "builtin", False, "OpenAI didn't accept the API key."
    )
    assert values(found) == ["gpt-realtime-2.1"]
    assert KEY not in repr(found.as_dict())


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused")]
)
def test_an_unreachable_openai_falls_back(keyed, error):
    found = options_for("OPENAI_TRANSCRIPTION_MODEL", keyed, sources(get=FakeGet(raises=error)))

    assert (found.source, found.live) == ("builtin", False)
    assert found.error == f"Keryx couldn't reach OpenAI ({type(error).__name__})."
    assert values(found) == ["gpt-4o-mini-transcribe"]


def test_another_status_is_named_by_its_code_only(keyed):
    get = FakeGet(status=502, body="<html>upstream said something long</html>")

    found = options_for("OPENAI_WEB_SEARCH_MODEL", keyed, sources(get=get))

    assert found.error == "OpenAI answered with HTTP 502."
    assert "upstream" not in found.error


def test_a_redirect_is_not_followed(keyed):
    found = options_for("OPENAI_WEB_SEARCH_MODEL", keyed, sources(get=FakeGet(status=307)))

    assert found.error == "OpenAI answered with HTTP 307." and not found.live


def test_a_body_that_is_not_a_model_list_falls_back(keyed):
    get = FakeGet(body="not json")

    found = options_for("OPENAI_WEB_SEARCH_MODEL", keyed, sources(get=get))

    assert found.error == "OpenAI didn't answer with a list of models."


def test_a_list_with_none_of_the_family_falls_back(keyed):
    get = FakeGet(body={"data": [{"id": "dall-e-3"}]})

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=get))

    assert not found.live and found.error == "OpenAI lists no Realtime voice models."


def test_an_unexpected_failure_in_a_source_is_a_reason_not_a_crash(keyed):
    get = FakeGet(raises=RuntimeError(f"something odd {KEY}"))

    found = options_for("OPENAI_REALTIME_MODEL", keyed, sources(get=get))

    assert (found.source, found.live) == ("builtin", False)
    assert found.error == "Keryx couldn't read the list (RuntimeError)."
    assert values(found) == ["gpt-realtime-2.1"]


def test_a_provider_that_fails_outside_its_read_still_answers(settings):
    def broken():
        raise OSError("sysctl went away")

    found = options_for("LLM_SERVER_MODEL", settings, sources(hardware=broken))

    assert (found.source, found.live, found.options) == ("builtin", False, [])
    assert found.error == "Keryx couldn't read the list (OSError)."


def test_a_server_answering_with_a_json_array_is_not_a_model_list(settings):
    local = settings.model_copy(update={
        "local_agent_base_url": "http://gpu-box:11434", "local_agent_model": "qwen3",
    })

    found = options_for("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(body=["x"])))

    assert found.error == "The server at gpu-box didn't answer with a list of models."
    assert values(found) == ["qwen3"]


def test_without_an_openai_key_nothing_is_asked(settings):
    unkeyed = settings.model_copy(update={"openai_api_key": None})

    found = options_for("OPENAI_REALTIME_MODEL", unkeyed, sources())

    assert found.error == "There's no OpenAI API key, so Keryx can't ask which models exist."
    assert values(found) == ["gpt-realtime-2.1"]


def test_the_current_value_is_appended_when_the_list_lacks_it(keyed):
    custom = keyed.model_copy(update={"openai_realtime_model": "gpt-realtime-preview-old"})

    found = options_for("OPENAI_REALTIME_MODEL", custom, sources(get=FakeGet(body=OPENAI_BODY)))

    assert found.current == "gpt-realtime-preview-old"
    assert found.options[-1] == Option("gpt-realtime-preview-old", "gpt-realtime-preview-old",
                                       "current")


def test_a_voice_server_of_ones_own_is_asked_instead_of_openai(keyed):
    local = keyed.model_copy(update={
        "voice_base_url": "http://127.0.0.1:8765/v1", "voice_api_key": "vk-local",
        "openai_realtime_model": "s2s",
    })
    get = FakeGet(body={"data": [{"id": "s2s"}, {"id": "other"}]})

    found = options_for("OPENAI_REALTIME_MODEL", local, sources(get=get))

    assert (found.source, found.live, values(found)) == ("server", True, ["s2s", "other"])
    ((url, kwargs),) = get.calls
    assert url == "http://127.0.0.1:8765/v1/models"
    assert kwargs["headers"] == {"Authorization": "Bearer vk-local"}
    assert kwargs["timeout"] == options.TIMEOUT_S
    assert kwargs["follow_redirects"] is False


def test_a_voice_server_that_does_not_answer_offers_only_the_current(keyed):
    local = keyed.model_copy(update={
        "voice_base_url": "http://127.0.0.1:8765/v1", "openai_realtime_model": "s2s",
    })
    get = FakeGet(raises=httpx.ConnectError("refused"))

    found = options_for("OPENAI_REALTIME_MODEL", local, sources(get=get))

    assert (found.source, found.live) == ("builtin", False)
    assert found.error == "Keryx couldn't reach the server at 127.0.0.1 (ConnectError)."
    assert values(found) == ["s2s"]


# --- the Realtime voices -----------------------------------------------------------------------


def test_voices_are_the_realtime_ones_and_the_assistants_own(settings):
    found = options_for("OPENAI_VOICE", settings, sources())

    assert values(found) == [
        "", "alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer",
        "verse",
    ]
    by_value = {option.value: option for option in found.options}
    assert by_value[""].label == "The assistant's own voice"
    assert (by_value["marin"].label, by_value["marin"].detail) == ("Marin", "Lyra's voice")
    assert by_value["cedar"].detail == "Jarvis's voice"
    assert by_value["alloy"].detail is None
    assert (found.source, found.live, found.free_form, found.error) == (
        "builtin", False, False, None
    )
    assert found.current == ""


def test_a_voice_set_outside_the_list_is_still_offered(settings):
    odd = settings.model_copy(update={"openai_voice": "nova"})

    found = options_for("OPENAI_VOICE", odd, sources())

    assert found.options[-1] == Option("nova", "nova", "current")


# --- Anthropic ---------------------------------------------------------------------------------


def test_claude_models_are_read_from_anthropic_with_the_api_key(settings):
    keyed = settings.model_copy(update={"anthropic_api_key": KEY})
    get = FakeGet(body={**ANTHROPIC_BODY, "data": [*ANTHROPIC_BODY["data"], {"id": "other"}]})

    found = options_for("SUBAGENT_MODEL", keyed, sources(get=get))

    assert values(found) == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-mythos-1"]
    assert found.options[0] == Option("claude-opus-5-5", "Claude Opus 5.5", "opus")
    assert found.options[2].detail is None
    assert (found.source, found.live, found.error) == ("anthropic", True, None)
    ((url, kwargs),) = get.calls
    assert url.startswith("https://api.anthropic.com/v1/models")
    assert kwargs["headers"] == {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    assert kwargs["timeout"] == options.TIMEOUT_S and kwargs["follow_redirects"] is False


def test_without_an_anthropic_key_the_known_models_are_offered(settings):
    found = options_for("SUBAGENT_MODEL", settings, sources())

    assert found.error == (
        "There's no Anthropic API key, so Keryx can't ask which models exist."
    )
    assert (found.source, found.live) == ("builtin", False)
    assert found.options[0] == Option("claude-opus-5-5", "Claude Opus", "opus")
    assert "claude-haiku-4-5-20251001" in values(found)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_anthropic_key_falls_back(settings, status):
    keyed = settings.model_copy(update={"anthropic_api_key": KEY})

    found = options_for("SUBAGENT_MODEL", keyed, sources(get=FakeGet(status=status)))

    assert found.error == "Anthropic didn't accept the API key."
    assert found.source == "builtin" and "claude-sonnet-5-5" in values(found)


def test_an_unreachable_anthropic_falls_back(settings):
    keyed = settings.model_copy(update={"anthropic_api_key": KEY})
    get = FakeGet(raises=httpx.ReadTimeout("slow"))

    found = options_for("SUBAGENT_MODEL", keyed, sources(get=get))

    assert found.error == "Keryx couldn't reach Anthropic (ReadTimeout)."


def test_anthropic_listing_no_claude_model_falls_back(settings):
    keyed = settings.model_copy(update={"anthropic_api_key": KEY})

    found = options_for("SUBAGENT_MODEL", keyed, sources(get=FakeGet(body={"data": []})))

    assert found.error == "Anthropic lists no Claude models." and not found.live


def test_claude_has_no_blank_option_since_a_blank_model_is_not_a_default(settings):
    """`SUBAGENT_MODEL` is a plain string with a default: blank does not mean "Claude's own"."""
    keyed = settings.model_copy(update={"anthropic_api_key": KEY})

    assert "" not in values(options_for("SUBAGENT_MODEL", settings, sources()))
    assert "" not in values(
        options_for("SUBAGENT_MODEL", keyed, sources(get=FakeGet(body=ANTHROPIC_BODY)))
    )


def test_a_claude_model_set_by_hand_is_kept(settings):
    custom = settings.model_copy(update={"subagent_model": "claude-opus-4-1"})

    found = options_for("SUBAGENT_MODEL", custom, sources())

    assert found.options[-1] == Option("claude-opus-4-1", "claude-opus-4-1", "current")


# --- Codex -------------------------------------------------------------------------------------

CODEX_LIST = [
    options.CodexModel("gpt-6-astra", "GPT-6-Astra", "Frontier intelligence."),
    options.CodexModel("gpt-6-luna", "GPT-6-Luna", "Fast and affordable."),
]


def test_codex_models_are_what_codex_lists(settings):
    asked = []

    def codex_models(env, timeout):
        asked.append((env, timeout))
        return CODEX_LIST

    found = options_for("CODEX_MODEL", settings, sources(codex_models=codex_models))

    assert values(found) == ["", "gpt-6-astra", "gpt-6-luna"]
    assert found.options[0] == Option("", "Codex's default", None)
    assert found.options[1] == Option("gpt-6-astra", "GPT-6-Astra", "Frontier intelligence.")
    # Unset is Codex choosing, which is the "" option: the app selects it.
    assert (found.source, found.live, found.error, found.current) == ("codex", True, None, "")
    ((env, timeout),) = asked
    # The stored login: every credential variable blanked, the owner's CODEX_HOME untouched.
    assert env == {"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": ""}
    assert timeout == options.TIMEOUT_S


def test_codex_on_a_subscription_token_is_handed_the_token_in_its_environment(settings):
    token = settings.model_copy(update={"codex_access_token": "tok-secret"})
    asked = []

    def codex_models(env, timeout):
        asked.append(env)
        return CODEX_LIST

    options_for("CODEX_MODEL", token, sources(codex_models=codex_models))

    assert asked[0]["CODEX_ACCESS_TOKEN"] == "tok-secret"
    assert asked[0]["OPENAI_API_KEY"] == ""


def test_codex_on_an_api_key_uses_keryxs_own_home_once_logged_in(settings):
    from keryx.agents.codex import private_codex_home

    keyed = settings.model_copy(update={"codex_api_key": KEY})
    asked = []

    def codex_models(env, timeout):
        asked.append(env)
        return CODEX_LIST

    found = options_for("CODEX_MODEL", keyed, sources(codex_models=codex_models))

    # Not logged in yet: a dropdown never logs in, so it says so instead.
    assert asked == []
    assert found.error == "Codex hasn't logged in with CODEX_API_KEY yet."
    assert values(found) == ["", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"]
    assert found.options[0] == Option("", "Codex's default", None)
    assert found.options[1] == Option("gpt-6-astra", "GPT-6-Astra", "astra")

    home = private_codex_home(keyed)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    found = options_for("CODEX_MODEL", keyed, sources(codex_models=codex_models))

    assert found.live
    assert asked[0]["CODEX_HOME"] == str(home)
    assert KEY not in asked[0].values()


def test_codex_that_fails_falls_back_to_the_known_models(settings):
    def codex_models(env, timeout):
        raise Unavailable("Codex didn't list its models within 5 seconds.")

    found = options_for("CODEX_MODEL", settings, sources(codex_models=codex_models))

    assert (found.source, found.live) == ("builtin", False)
    assert found.error == "Codex didn't list its models within 5 seconds."
    assert "gpt-5.6-terra" in values(found)


def test_codex_listing_nothing_falls_back(settings):
    found = options_for("CODEX_MODEL", settings, sources(codex_models=lambda env, timeout: []))

    assert found.error == "Codex lists no models." and not found.live


class FakeCodex:
    """`openai_codex.Codex` as far as the lister uses it. "hang" blocks in `models()` until
    the client is closed, the way an app-server that never answers does."""

    made: list = []
    instances: list = []
    behaviour = "list"

    def __init__(self, config):
        FakeCodex.made.append(config)
        if FakeCodex.behaviour == "fail":
            raise RuntimeError(f"app-server said {KEY}")
        self.closes = 0
        self.released = threading.Event()
        FakeCodex.instances.append(self)

    def close(self):
        self.closes += 1
        self.released.set()

    def models(self):
        if FakeCodex.behaviour == "hang":
            self.released.wait(5)
        model = types.SimpleNamespace(
            id="gpt-6-astra", display_name="GPT-6-Astra", description="Frontier.", hidden=False
        )
        hidden = types.SimpleNamespace(
            id="secret-model", display_name="Hidden", description="", hidden=True
        )
        return types.SimpleNamespace(data=[model, hidden])


@pytest.fixture
def fake_sdk(monkeypatch):
    module = types.ModuleType("openai_codex")
    module.Codex = FakeCodex
    module.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", module)
    FakeCodex.made = []
    FakeCodex.instances = []
    FakeCodex.behaviour = "list"
    return FakeCodex


def test_the_sdk_lister_runs_codex_with_the_env_and_skips_hidden_models(fake_sdk):
    listed = options.sdk_codex_models({"CODEX_API_KEY": ""}, 5.0)

    assert listed == [options.CodexModel("gpt-6-astra", "GPT-6-Astra", "Frontier.")]
    (config,) = fake_sdk.made
    assert config["env"] == {"CODEX_API_KEY": ""}
    assert config["client_name"] == "keryx"
    assert [codex.closes for codex in fake_sdk.instances] == [1]


def test_the_sdk_lister_says_why_without_the_error_text(fake_sdk):
    fake_sdk.behaviour = "fail"

    with pytest.raises(Unavailable) as raised:
        options.sdk_codex_models({}, 5.0)

    assert str(raised.value) == "Codex couldn't list its models (RuntimeError)."
    assert KEY not in str(raised.value)


def test_the_sdk_lister_gives_up_at_the_timeout(fake_sdk):
    fake_sdk.behaviour = "hang"

    with pytest.raises(Unavailable) as raised:
        options.sdk_codex_models({}, 0.05)

    assert str(raised.value) == "Codex didn't list its models within 0.05 seconds."
    # The app-server it started is not left behind: the caller closes it.
    (codex,) = fake_sdk.instances
    assert codex.closes >= 1


def test_the_sdk_lister_without_the_sdk_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)

    with pytest.raises(Unavailable) as raised:
        options.sdk_codex_models({}, 5.0)

    assert str(raised.value) == "The Codex agent isn't installed."


# --- the local agent's server ------------------------------------------------------------------


def test_the_local_agent_server_is_asked_for_its_models(settings):
    local = settings.model_copy(update={
        "local_agent_base_url": "http://gpu-box:11434", "local_agent_model": "qwen3",
    })
    get = FakeGet(body={"models": [{"name": "qwen3"}, {"name": "gemma"}]})

    found = options_for("LOCAL_AGENT_MODEL", local, sources(get=get))

    assert (found.source, found.live, values(found)) == ("server", True, ["qwen3", "gemma"])
    assert get.calls[0][0] == "http://gpu-box:11434/v1/models"


def test_an_unset_key_whose_blank_is_an_option_is_current_as_blank(settings):
    codex = options_for("CODEX_MODEL", settings, sources(codex_models=lambda env, t: CODEX_LIST))
    voice = options_for("OPENAI_VOICE", settings, sources())

    assert settings.codex_model is None and settings.openai_voice == ""
    assert codex.current == "" and voice.current == ""
    assert codex.as_dict()["current"] == "" and values(codex).count("") == 1


def test_no_local_agent_server_offers_only_the_current(settings):
    found = options_for("LOCAL_AGENT_MODEL", settings, sources())

    assert found.error == (
        "There's no local agent server address, so Keryx can't ask which models exist."
    )
    assert (found.options, found.current) == ([], None)


def test_a_local_agent_server_that_refuses_the_key_says_so(settings):
    local = settings.model_copy(update={
        "local_agent_base_url": "http://gpu-box:11434", "local_agent_model": "qwen3",
    })

    found = options_for("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(status=401)))

    assert found.error == "The server at gpu-box didn't accept the API key."
    assert values(found) == ["qwen3"]


def test_a_server_listing_no_models_says_so(settings):
    local = settings.model_copy(update={"local_agent_base_url": "http://gpu-box:11434"})

    found = options_for("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(body={"data": []})))

    assert found.error == "The server at gpu-box lists no models." and found.options == []


# --- the catalog -------------------------------------------------------------------------------


def test_the_model_to_serve_is_the_catalog_downloaded_first(settings):
    downloaded = {"gemma-4-12b"}
    found = options_for(
        "LLM_SERVER_MODEL",
        settings,
        sources(
            downloaded=lambda entry: entry.key in downloaded,
            hardware=lambda: Hardware("metal", "Apple silicon", 0.0, 32.0),  # 24 GB for a model
        ),
    )

    assert values(found)[0] == "gemma-4-12b"
    assert set(values(found)) == {entry.key for entry in catalog.MODELS}
    by_value = {option.value: option for option in found.options}
    assert by_value["gemma-4-12b"].detail == "downloaded"
    assert (by_value["qwen3.8-27b"].label, by_value["qwen3.8-27b"].detail) == (
        "Qwen3.8 27B", "fits"
    )
    assert by_value["gpt-oss-120b"].detail == "too big for this Mac"
    assert (found.source, found.live, found.free_form, found.error) == (
        "catalog", False, True, None
    )


def test_on_another_machine_too_big_names_no_mac(settings):
    found = options_for(
        "LLM_SERVER_MODEL", settings,
        sources(hardware=lambda: Hardware("cuda", "RTX", 12.0, 64.0)),
    )

    by_value = {option.value: option for option in found.options}
    assert by_value["gpt-oss-120b"].detail == "too big for this machine"


def test_a_gguf_path_being_served_is_kept(settings):
    custom = settings.model_copy(update={"llm_server_model": "/models/mine.gguf"})

    found = options_for("LLM_SERVER_MODEL", custom, sources())

    assert found.options[-1] == Option("/models/mine.gguf", "/models/mine.gguf", "current")


def test_the_kokoro_voices_come_from_the_catalog(settings):
    found = options_for("VOICE_SERVER_VOICE", settings, sources())

    assert values(found) == [name for name, _ in catalog.VOICES]
    assert found.options[0] == Option("af_heart", "Heart", "warm, American, female")
    assert (found.source, found.free_form, found.current) == ("catalog", True, "af_heart")


def test_the_voice_servers_speech_models_are_built_in(settings):
    stt = options_for("VOICE_SERVER_STT", settings, sources())
    tts = options_for("VOICE_SERVER_TTS", settings, sources())

    assert values(stt)[0] == "parakeet-tdt" and "whisper" in values(stt)
    assert values(tts) == ["kokoro", "qwen3"]
    assert stt.source == tts.source == "builtin" and stt.free_form and tts.free_form


def test_a_speech_model_already_downloaded_says_so(settings):
    cached = {("stt", "whisper"), ("tts", "kokoro")}
    asked: list[tuple] = []
    found = sources(speech_ready=lambda kind, value: asked.append((kind, value))
                    or (kind, value) in cached)

    stt = options_for("VOICE_SERVER_STT", settings, found)
    tts = options_for("VOICE_SERVER_TTS", settings, found)

    assert [(o.value, o.detail) for o in stt.options] == [
        ("parakeet-tdt", None), ("whisper", "downloaded")]
    assert [(o.value, o.detail) for o in tts.options] == [("kokoro", "downloaded"),
                                                         ("qwen3", None)]
    assert asked == [("stt", "parakeet-tdt"), ("stt", "whisper"),
                     ("tts", "kokoro"), ("tts", "qwen3")]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_only_speech_models_keryx_can_set_up_are_offered(platform):
    """Each one offered has models `keryx voice download` can name and fetch."""
    from keryx.localmodels import speech

    assert all(speech.stt_repos(v, platform=platform) is not None
               for v in options.VOICE_SERVER_STT)
    assert all(speech.tts_repos(v, "af_heart", platform=platform) is not None
               for v in options.VOICE_SERVER_TTS)
    assert "whisper-mlx" not in options.VOICE_SERVER_STT


def test_a_speech_model_no_longer_offered_is_still_the_current_one(settings):
    """An owner's whisper-mlx, set before: kept, shown, never offered to anyone else."""
    chosen = settings.model_copy(update={"voice_server_stt": "whisper-mlx"})

    found = options_for("VOICE_SERVER_STT", chosen, sources())

    assert found.current == "whisper-mlx" and found.free_form
    assert found.options[-1] == Option("whisper-mlx", "whisper-mlx", "current")


def test_the_real_sources_ask_the_hugging_face_cache(settings, monkeypatch):
    from keryx.localmodels import speech

    monkeypatch.setattr(speech, "ready", lambda chosen, kind, value: (chosen, kind, value))

    assert options.default_sources(settings).speech_ready("stt", "whisper") == (
        settings, "stt", "whisper")


def test_the_default_sources_are_the_real_ones(settings):
    real = options.default_sources(settings)

    assert real.get is options.http_get
    assert real.codex_models is options.sdk_codex_models


def test_http_get_never_follows_a_redirect(monkeypatch):
    seen = {}

    def fake(url, **kwargs):
        seen.update(kwargs, url=url)
        return httpx.Response(200)

    monkeypatch.setattr(options.httpx, "get", fake)
    options.http_get("https://example.test/v1/models", headers={}, timeout=5.0,
                     follow_redirects=True)

    assert seen["follow_redirects"] is False


def test_the_real_hardware_and_disk_are_asked(monkeypatch, settings):
    from keryx.localmodels import download, hardware

    monkeypatch.setattr(hardware, "detect", lambda: Hardware("cpu", None, 0.0, 8.0))
    monkeypatch.setattr(download, "is_downloaded", lambda cache, entry: entry.key == "qwen3.5-4b")
    real = options.default_sources(settings)

    assert real.hardware().ram_gb == 8.0
    assert real.downloaded(catalog.by_key("qwen3.5-4b"))


# --- what an error says -------------------------------------------------------------------


def _every_error(settings) -> list[str]:
    """One error of each kind the module can give, each from its own failure."""
    openai = settings.model_copy(update={"openai_api_key": KEY})
    anthropic = settings.model_copy(update={"anthropic_api_key": KEY})
    local = settings.model_copy(update={"local_agent_base_url": "http://gpu-box:11434"})
    unkeyed = settings.model_copy(update={"openai_api_key": None})
    coded = settings.model_copy(update={"codex_api_key": KEY})

    def failing(error):
        def codex_models(env, timeout):
            raise error

        return codex_models

    cases = [
        ("OPENAI_REALTIME_MODEL", unkeyed, sources()),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(status=401))),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(raises=httpx.ConnectError("x")))),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(status=500))),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(body=["x"]))),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(body={"data": []}))),
        ("OPENAI_TRANSCRIPTION_MODEL", openai, sources(get=FakeGet(body={"data": []}))),
        ("OPENAI_WEB_SEARCH_MODEL", openai, sources(get=FakeGet(body={"data": []}))),
        ("OPENAI_REALTIME_MODEL", openai, sources(get=FakeGet(raises=RuntimeError(KEY)))),
        ("SUBAGENT_MODEL", settings, sources()),
        ("SUBAGENT_MODEL", anthropic, sources(get=FakeGet(status=403))),
        ("SUBAGENT_MODEL", anthropic, sources(get=FakeGet(body={"data": []}))),
        ("CODEX_MODEL", coded, sources()),
        ("CODEX_MODEL", settings, sources(codex_models=lambda env, timeout: [])),
        ("LOCAL_AGENT_MODEL", settings, sources()),
        ("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(status=401))),
        ("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(status=500))),
        ("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(raises=httpx.ReadTimeout("x")))),
        ("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(body=["x"]))),
        ("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(body={"data": []}))),
        ("LLM_SERVER_MODEL", settings, sources(hardware=failing(OSError("x")))),
    ]
    return [options_for(key, given, found).error for key, given, found in cases]


def test_every_error_is_one_sentence_of_cause_only(settings):
    """The app prefixes "Showing the built-in list —", so an error says why and only why."""
    errors = _every_error(settings)

    assert len(set(errors)) == len(errors), errors
    for error in errors:
        assert error and error[0].isupper() and error.endswith("."), error
        assert error.count(". ") == 0 and ";" not in error, error
        assert "show" not in error.lower() and "known" not in error.lower(), error
        assert KEY not in error, error


def test_the_family_names_itself_when_openai_lists_none(keyed):
    empty = sources(get=FakeGet(body={"data": []}))

    assert options_for("OPENAI_TRANSCRIPTION_MODEL", keyed, empty).error == (
        "OpenAI lists no transcription models."
    )
    assert options_for("OPENAI_WEB_SEARCH_MODEL", keyed, empty).error == (
        "OpenAI lists no chat models."
    )


@pytest.mark.parametrize(
    ("status", "says"),
    [
        (403, "The server at gpu-box didn't accept the API key."),
        (500, "The server at gpu-box answered with HTTP 500."),
    ],
)
def test_a_server_says_what_went_wrong_by_status(settings, status, says):
    local = settings.model_copy(update={"local_agent_base_url": "http://gpu-box:11434"})

    found = options_for("LOCAL_AGENT_MODEL", local, sources(get=FakeGet(status=status)))

    assert found.error == says


# --- TWILIO_NUMBER: the account's own numbers ---------------------------------------------

TWILIO_SID = "AC0123456789abcdef0123456789abcdef"
TWILIO_TOKEN = "tok-planted-0123456789abcdef"


class FakeTwilio:
    """`(sid, token)` → an admin with these numbers, or one that raises `error`."""

    def __init__(self, numbers=(), error=None):
        self.numbers_, self.error = list(numbers), error
        self.pairs: list[tuple[str, str]] = []

    def __call__(self, sid, token):
        self.pairs.append((sid, token))
        return self

    def account_name(self):
        if self.error:
            raise self.error
        return "Ada's account"

    def numbers(self):
        if self.error:
            raise self.error
        return self.numbers_


@pytest.fixture
def twilio_saved(settings):
    return settings.model_copy(update={
        "twilio_account_sid": TWILIO_SID, "twilio_auth_token": TWILIO_TOKEN,
        "twilio_number": "+15550001111",
    })


def test_twilio_numbers_are_the_accounts_own(twilio_saved):
    from keryx.notify.twilio_out import TwilioNumber

    twilio = FakeTwilio([TwilioNumber("PN1", "+15550001111", None, None, "Office"),
                         TwilioNumber("PN2", "+15550002222", None, None, "")])

    found = options_for("TWILIO_NUMBER", twilio_saved, sources(twilio=twilio))

    assert (found.source, found.live, found.error) == ("twilio", True, None)
    assert found.current == "+15550001111"
    assert found.options == [Option("+15550001111", "+15550001111", "Office"),
                             Option("+15550002222", "+15550002222", None)]
    assert twilio.pairs == [(TWILIO_SID, TWILIO_TOKEN)]


def test_without_twilio_credentials_only_the_current_number_is_offered(twilio_saved):
    twilio = FakeTwilio()
    bare = twilio_saved.model_copy(update={"twilio_auth_token": None})

    found = options_for("TWILIO_NUMBER", bare, sources(twilio=twilio))

    assert (found.source, found.live) == ("builtin", False)
    assert found.options == [Option("+15550001111", "+15550001111", "current")]
    assert found.error == "There's no TWILIO_AUTH_TOKEN saved, so Keryx can't ask Twilio."
    assert twilio.pairs == []


def test_twilio_refusing_is_a_cause_and_never_the_token(twilio_saved):
    from keryx.notify.twilio_out import TwilioError

    twilio = FakeTwilio(error=TwilioError(f"Authenticate {TWILIO_TOKEN}", status=401))

    found = options_for("TWILIO_NUMBER", twilio_saved, sources(twilio=twilio))

    assert (found.source, found.live) == ("builtin", False)
    assert found.error == "Twilio didn't accept the account SID and auth token."
    assert TWILIO_TOKEN not in json.dumps(found.as_dict())


def test_an_account_with_no_numbers_says_so(twilio_saved):
    found = options_for("TWILIO_NUMBER", twilio_saved, sources(twilio=FakeTwilio()))

    assert found.error == "That Twilio account has no phone numbers."


def test_the_twilio_source_is_the_phones_admin(twilio_saved, monkeypatch):
    from keryx import phone
    from keryx.notify.twilio_out import TwilioNumber

    twilio = FakeTwilio([TwilioNumber("PN1", "+15550001111", None, None, "Office")])
    monkeypatch.setattr(phone, "rest_admin", twilio)

    found = options_for("TWILIO_NUMBER", twilio_saved, sources())

    assert found.live and twilio.pairs == [(TWILIO_SID, TWILIO_TOKEN)]
