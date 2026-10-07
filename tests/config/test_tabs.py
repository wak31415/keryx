"""The Mac app's Configuration screen: eight tabs, each a few key settings and an "Advanced"
disclosure for the rest. One table (`TABS`) says which groups a tab holds and in what order
the tabs come; each field says for itself whether it is advanced."""

import pytest

from keryx.config.settings import (
    GROUPS,
    TABS,
    Settings,
    env_var_name,
    field_advanced,
    field_group,
    field_tab,
    setting,
)

#: The spec's tabs, word for word: the Swift decoders follow these names.
SPEC_TABS = [
    ("general", "General", ["owner"]),
    ("voice", "Voice", ["voice"]),
    ("agents", "Agents", ["agents"]),
    ("local", "Local Models", ["local"]),
    ("phone", "Phone", ["phone"]),
    ("approvals", "Approvals", ["approvals"]),
    ("integrations", "Integrations", ["google", "plugins", "projects"]),
    ("system", "System", ["limits", "service", "debug"]),
]


def test_the_tabs_are_the_specified_eight_in_order():
    assert [(key, tab.title, list(tab.groups)) for key, tab in TABS.items()] == SPEC_TABS


def test_every_group_is_in_exactly_one_tab():
    placed = [group for tab in TABS.values() for group in tab.groups]

    assert sorted(placed) == sorted(GROUPS)
    assert len(placed) == len(set(placed))


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
