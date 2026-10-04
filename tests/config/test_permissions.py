"""What the running service may change: the defaults, the owner's overrides, and the keys
nothing unlocks."""

import pytest

from keryx.config import GROUPS, Settings, env_var_name, field_group, is_secret
from keryx.config.permissions import (
    PROTECTED_KEYS,
    current_actor,
    is_protected,
    service_writable,
    writable_keys,
)
from keryx.config.settings import canonical_key
from keryx.config.store import ConfigError, ConfigStore

KEYS = [env_var_name(name) for name in Settings.model_fields]


def test_every_setting_says_what_it_is_and_where_it_belongs():
    for name, info in Settings.model_fields.items():
        assert info.description, f"{name} has no description"
        assert field_group(name) in GROUPS, name


def test_every_secret_is_protected():
    for name in Settings.model_fields:
        if is_secret(name):
            assert env_var_name(name) in PROTECTED_KEYS, name


def test_nothing_protected_defaults_to_writable():
    """A field that declares itself writable and matches a protected pattern is a mistake
    in one of the two places, and the protection would silently win."""
    for name, info in Settings.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("service_writable"):
            assert not is_protected(env_var_name(name)), name


@pytest.mark.parametrize(
    "key",
    [
        "KERYX_PIN",
        "ALLOWED_CALLERS",
        "OWNER_NUMBER",
        "BRIEFING_BEFORE_PIN",
        "PIN_FAILURE_LIMIT",
        "APPROVALS_ENABLED",
        "APPROVAL_BASH_ALLOW",
        "APPROVAL_ROOTS",
        "ISSUE_REPORTING",
        "ISSUE_REPO",
        "SUBAGENT_MAX_BUDGET_USD",
        "DAILY_TASK_CAP",
        "TRANSCRIPT_RETENTION_DAYS",
        "SMS_ENABLED",
        "PUBLIC_HOST",
        "HOST",
        "PORT",
        "DATA_DIR",
        "STATE_DIR",
        "CACHE_DIR",
        "SERVICE_MANAGER",
        "SKILLS_DIR",
        "SLACK_BOT_TOKEN",
        "OPENAI_ADMIN_KEY",
        "DEBUG_SKIP_TWILIO_VALIDATION",
        "DEMO_MODE",
        "TWILIO_AUTH_TOKEN",
        "VOICE_BASE_URL",
        "VOICE_API_KEY",
        "LOCAL_AGENT_BASE_URL",
        "LOCAL_AGENT_API_KEY",
        "VOICE_SERVER_ARGS",
    ],
)
def test_the_lines_of_defence_are_protected(key):
    assert key in PROTECTED_KEYS
    assert not service_writable(key, {key: True})


def test_the_writable_defaults_are_the_ones_you_would_say_on_a_call():
    """A plugin's settings are in its own file, which `set_config` does not reach: the
    budget and the email model went with them (2026-09-29), an accepted cost."""
    assert set(writable_keys({})) == {
        "ASSISTANT_NAME",
        "OPENAI_VOICE",
        "VAD_MODE",
        "VAD_EAGERNESS",
        "VAD_SILENCE_MS",
        "VAD_THRESHOLD",
        "VAD_PREFIX_MS",
        "NOISE_REDUCTION",
        "TRANSCRIPTION_LANGUAGE",
        "LOCAL_AGENT_MODEL",
        "LOCAL_AGENT_API",
        "CLOCK_FORMAT",
        "SUBAGENT_MODEL",
        "CODEX_MODEL",
        "AGENT_BACKEND",
        "LOCAL_SILENCE_TIMEOUT",
        "MAX_CALL_SECONDS",
        "SUBAGENT_TIMEOUT_S",
        "MAX_CONCURRENT_TASKS",
        "APPROVAL_QUIET_HOURS",
        "LOG_LEVEL",
    }


def test_an_override_moves_an_unprotected_key_either_way():
    assert service_writable("PROJECTS_ROOT", {"PROJECTS_ROOT": True})
    assert not service_writable("OPENAI_VOICE", {"OPENAI_VOICE": False})
    assert not service_writable("NOT_A_SETTING", {"NOT_A_SETTING": True})


def test_the_actor_is_the_service_only_inside_keryx_serve(monkeypatch):
    assert current_actor() == "owner"
    monkeypatch.setenv("KERYX_ACTOR", "service")
    assert current_actor() == "service"


def test_a_subagent_of_the_old_service_is_still_the_service(monkeypatch):
    """Security, not courtesy: `jarvis serve` marks its subagents with the old name, and
    one of them running the new command must not be taken for the owner at a terminal."""
    monkeypatch.setenv("JARVIS_ACTOR", "service")

    assert current_actor() == "service"


@pytest.mark.parametrize("old", ["FAKE_AGENTS", "JARVIS_CHECKOUT", "JARVIS_PIN"])
def test_an_old_name_is_protected_exactly_as_the_current_one(old):
    """A setting's rules are the same whatever name it is asked for by."""
    new = canonical_key(old)
    overrides = {old: True, new: True}
    assert is_protected(old) == is_protected(new)
    assert service_writable(old, overrides) == service_writable(new, overrides)
    assert is_protected("FAKE_AGENTS") and is_protected("JARVIS_PIN")


def test_an_old_name_cannot_be_unlocked_or_written_by_the_service():
    store = ConfigStore()

    with pytest.raises(ConfigError, match="DEMO_MODE is protected"):
        store.unlock("fake_agents")
    with pytest.raises(ConfigError):
        store.set({"FAKE_AGENTS": "true"}, actor="service")

    store.set({"FAKE_AGENTS": "true"})  # the owner may, and it is stored under the new name
    assert store.stored() == {"DEMO_MODE": True}
