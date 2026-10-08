"""The Mac app's Configuration screen: seven tabs, each a few key settings and an "Advanced"
disclosure for the rest. One table (`TABS`) says which groups a tab holds and in what order
the tabs come; another (`SCREEN_OF_GROUP`) says which groups an app screen of their own owns
instead; each field says for itself whether it is advanced."""

import pytest

from keryx.config.settings import (
    GROUPS,
    SCREEN_OF_GROUP,
    SCREENS,
    TABS,
    Settings,
    env_var_name,
    field_advanced,
    field_group,
    field_screen,
    field_tab,
    setting,
)

#: The spec's tabs, word for word: the Swift decoders follow these names.
SPEC_TABS = [
    ("general", "General", ["owner"]),
    ("voice", "Voice", ["voice", "realtime"]),
    ("agents", "Agents", ["agents", "local_agent"]),
    ("phone", "Phone", ["phone"]),
    ("approvals", "Approvals", ["approvals"]),
    ("integrations", "Integrations", ["google", "plugins", "projects"]),
    ("system", "System", ["limits", "service", "debug"]),
]


#: The groups an app screen owns, and the screens: the Voice screen and the Local Models one.
SPEC_SCREENS = {"voice_model": "voice", "model_servers": "models"}


def test_the_tabs_are_the_specified_seven_in_order():
    assert [(key, tab.title, list(tab.groups)) for key, tab in TABS.items()] == SPEC_TABS


def test_the_screens_own_the_voice_models_and_the_model_servers():
    assert SCREEN_OF_GROUP == SPEC_SCREENS
    assert set(SCREEN_OF_GROUP.values()) == set(SCREENS)


def test_every_group_is_in_exactly_one_tab_or_one_screen():
    placed = [group for tab in TABS.values() for group in tab.groups] + list(SCREEN_OF_GROUP)

    assert sorted(placed) == sorted(GROUPS)
    assert len(placed) == len(set(placed))


def test_a_screen_owned_setting_is_on_no_tab():
    assert (field_tab("voice_server_stt"), field_screen("voice_server_stt")) == (None, "voice")
    assert (field_tab("agent_server_model"), field_screen("agent_server_model")) == (
        None, "models"
    )
    assert (field_tab("vad_threshold"), field_screen("vad_threshold")) == ("voice", None)


@pytest.mark.parametrize(
    ("key", "group"),
    [("OPENAI_API_KEY", "voice"), ("ASSISTANT_NAME", "voice"), ("CLOCK_FORMAT", "voice"),
     ("TRANSCRIPTION_LANGUAGE", "voice"), ("OPENAI_WEB_SEARCH_MODEL", "voice"),
     ("OPENAI_TRANSCRIPTION_MODEL", "realtime"), ("VAD_MODE", "realtime"),
     ("VAD_EAGERNESS", "realtime"), ("VAD_SILENCE_MS", "realtime"),
     ("VAD_THRESHOLD", "realtime"), ("VAD_PREFIX_MS", "realtime"),
     ("NOISE_REDUCTION", "realtime"),
     ("VOICE_MODE", "voice_model"), ("OPENAI_REALTIME_MODEL", "voice_model"),
     ("OPENAI_VOICE", "voice_model"), ("VOICE_BASE_URL", "voice_model"),
     ("VOICE_API_KEY", "voice_model"), ("VOICE_SERVER_STT", "voice_model"),
     ("VOICE_SERVER_TTS", "voice_model"), ("VOICE_SERVER_VOICE", "voice_model"),
     ("VOICE_SERVER_ARGS", "voice_model"), ("VOICE_LLM", "voice_model"),
     ("VOICE_LLM_MODEL", "voice_model"), ("VOICE_LLM_BASE_URL", "voice_model"),
     ("VOICE_LLM_API_KEY", "voice_model"),
     ("CLAUDE_MODEL", "agents"), ("CODEX_MODEL", "agents"),
     ("LOCAL_AGENT_BASE_URL", "local_agent"), ("LOCAL_AGENT_API_KEY", "local_agent"),
     ("LOCAL_AGENT_MODEL", "local_agent"), ("LOCAL_AGENT_API", "local_agent"),
     ("AGENT_SERVER_MODEL", "model_servers"), ("AGENT_SERVER_PORT", "model_servers"),
     ("VOICE_SERVER_PORT", "model_servers"), ("VOICE_LLM_SERVER_PORT", "model_servers")],
)
def test_each_model_setting_is_in_the_designs_group(key, group):
    by_key = {env_var_name(name): field_group(name) for name in Settings.model_fields}

    assert by_key[key] == group


def test_the_openai_realtime_tuning_is_all_advanced():
    tuning = [name for name in Settings.model_fields if field_group(name) == "realtime"]

    assert tuning and all(field_advanced(name) for name in tuning)
    assert GROUPS["realtime"] == "OpenAI Realtime tuning"


def test_the_voice_screens_advanced_disclosure_is_the_extra_flags_alone():
    voice = [env_var_name(name) for name in Settings.model_fields
             if field_screen(name) == "voice" and field_advanced(name)]
    servers = [env_var_name(name) for name in Settings.model_fields
               if field_screen(name) == "models" and field_advanced(name)]

    assert voice == ["VOICE_SERVER_ARGS"]
    assert sorted(servers) == ["AGENT_SERVER_PORT", "VOICE_LLM_SERVER_PORT", "VOICE_SERVER_PORT"]


@pytest.mark.parametrize("name", list(Settings.model_fields))
def test_every_setting_declares_whether_it_is_advanced(name):
    extra = Settings.model_fields[name].json_schema_extra

    assert isinstance(extra, dict) and isinstance(extra.get("advanced"), bool), name


def test_a_setting_cannot_be_declared_without_saying_whether_it_is_advanced():
    with pytest.raises(TypeError):
        setting(1, "A number.", title="A number", group="voice")  # type: ignore[call-arg]


def test_a_settings_tab_is_its_groups():
    assert field_tab("vad_threshold") == "voice"
    assert field_tab("owner_name") == "general"
    assert field_tab("slack_bot_token") == "integrations"
    assert field_tab("demo_mode") == "system"


@pytest.mark.parametrize("tab", list(TABS))
def test_every_tab_has_a_key_setting(tab):
    keys = [name for name in Settings.model_fields
            if field_tab(name) == tab and not field_advanced(name)]

    assert keys, f"the {tab} tab would open on nothing but its Advanced disclosure"


@pytest.mark.parametrize("screen", list(SCREENS))
def test_every_screen_has_a_key_setting(screen):
    keys = [name for name in Settings.model_fields
            if field_screen(name) == screen and not field_advanced(name)]

    assert keys, f"the {screen} screen would show nothing but its Advanced disclosure"


def test_every_debug_switch_is_advanced():
    debug = [name for name in Settings.model_fields if field_group(name) == "debug"]

    assert debug
    assert all(field_advanced(name) for name in debug), [
        env_var_name(name) for name in debug if not field_advanced(name)
    ]


def test_the_first_call_settings_are_key():
    """What an owner must set before the first call ever opens on the front of a tab."""
    first_call = {
        "OPENAI_API_KEY", "AGENT_BACKEND", "ANTHROPIC_API_KEY", "OWNER_NAME",
        "ALLOWED_CALLERS", "KERYX_PIN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "TWILIO_NUMBER", "PUBLIC_HOST",
    }
    by_key = {env_var_name(name): field_advanced(name) for name in Settings.model_fields}

    assert {key for key in first_call if by_key[key]} == set()


def test_the_owners_rulings_on_key_and_advanced():
    """The local agent's server is set in context by the Local Models screen; retention and
    the daily cap are the owner's privacy and spend; the log level is a debugging knob. A
    voice server's address and key are the Voice screen's own rows for that mode, beside the
    voice LLM's, so they are key there."""
    by_key = {env_var_name(name): field_advanced(name) for name in Settings.model_fields}
    advanced = ("LOCAL_AGENT_BASE_URL", "LOCAL_AGENT_API_KEY", "LOG_LEVEL")
    key = ("TRANSCRIPT_RETENTION_DAYS", "TASK_RETENTION_DAYS", "DAILY_TASK_CAP",
           "VOICE_BASE_URL", "VOICE_API_KEY", "VOICE_LLM_BASE_URL", "VOICE_LLM_API_KEY")

    assert [name for name in advanced if not by_key[name]] == []
    assert [name for name in key if by_key[name]] == []
