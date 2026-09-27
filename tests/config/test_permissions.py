"""What the running service may change: the defaults, the owner's overrides, and the keys
nothing unlocks."""

import pytest

from jarvis.config import GROUPS, Settings, env_var_name, field_group, is_secret
from jarvis.config.permissions import (
    PROTECTED_KEYS,
    current_actor,
    is_protected,
    service_writable,
    writable_keys,
)

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
        "JARVIS_PIN",
        "ALLOWED_CALLERS",
        "OWNER_NUMBER",
        "BRIEFING_BEFORE_PIN",
        "PIN_FAILURE_LIMIT",
        "APPROVALS_ENABLED",
        "APPROVAL_BASH_ALLOW",
        "APPROVAL_ROOTS",
        "SUBAGENT_MAX_BUDGET_USD",
        "DAILY_TASK_CAP",
        "TRANSCRIPT_RETENTION_DAYS",
        "SMS_ENABLED",
        "PUBLIC_HOST",
        "HOST",
        "PORT",
        "DATA_DIR",
        "SERVICE_MANAGER",
        "CLUSTERS",
        "SKILLS_DIR",
        "DEBUG_SKIP_TWILIO_VALIDATION",
        "FAKE_AGENTS",
        "TWILIO_AUTH_TOKEN",
    ],
)
def test_the_lines_of_defence_are_protected(key):
    assert key in PROTECTED_KEYS
    assert not service_writable(key, {key: True})


def test_the_writable_defaults_are_the_ones_you_would_say_on_a_call():
    assert set(writable_keys({})) == {
        "OPENAI_VOICE",
        "VAD_MODE",
        "VAD_EAGERNESS",
        "VAD_SILENCE_MS",
        "VAD_THRESHOLD",
        "VAD_PREFIX_MS",
        "NOISE_REDUCTION",
        "SUBAGENT_MODEL",
        "CODEX_MODEL",
        "EMAIL_MODEL",
        "EMAIL_EFFORT",
        "AGENT_BACKEND",
        "LOCAL_SILENCE_TIMEOUT",
        "MAX_CALL_SECONDS",
        "SUBAGENT_TIMEOUT_S",
        "MAX_CONCURRENT_TASKS",
        "APPROVAL_QUIET_HOURS",
        "BILLING_MONTHLY_BUDGET",
        "LOG_LEVEL",
    }


def test_an_override_moves_an_unprotected_key_either_way():
    assert service_writable("PROJECTS_ROOT", {"PROJECTS_ROOT": True})
    assert not service_writable("OPENAI_VOICE", {"OPENAI_VOICE": False})
    assert not service_writable("NOT_A_SETTING", {"NOT_A_SETTING": True})


def test_the_actor_is_the_service_only_inside_jarvis_serve(monkeypatch):
    assert current_actor() == "owner"
    monkeypatch.setenv("JARVIS_ACTOR", "service")
    assert current_actor() == "service"
