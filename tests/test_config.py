"""Tests for jarvis.config.Settings."""

import stat
from pathlib import Path

from jarvis.config import OPTIONAL_STR_FIELDS, Settings, load_settings


def test_allowed_callers_parses_comma_separated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ALLOWED_CALLERS", "+491555555555,+491666666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.allowed_callers == ["+491555555555", "+491666666666"]


def test_allowed_callers_defaults_to_empty_list(settings):
    assert settings.allowed_callers == []


def test_projects_parses_json_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("PROJECTS", '{"jarvis": "/home/me/jarvis", "other": "/home/me/other"}')
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.projects == {"jarvis": "/home/me/jarvis", "other": "/home/me/other"}


def test_projects_defaults_to_empty_dict(settings):
    assert settings.projects == {}


def test_jarvis_pin_env_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("JARVIS_PIN", "1234")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.pin == "1234"


def test_pin_defaults_to_none(settings):
    assert settings.pin is None


def test_owner_number_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OWNER_NUMBER", "+491000000000")
    monkeypatch.setenv("ALLOWED_CALLERS", "+491555555555,+491666666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.owner_number == "+491000000000"


def test_owner_number_falls_back_to_first_allowed_caller(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ALLOWED_CALLERS", "+491555555555,+491666666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.owner_number == "+491555555555"


def test_owner_number_none_when_nothing_set(settings):
    assert settings.owner_number is None


def test_report_secret_value_persists_across_calls(settings):
    first = settings.report_secret_value()
    second = settings.report_secret_value()

    assert first == second
    assert len(first) == 64  # 32 random bytes, hex-encoded


def test_report_secret_value_writes_file_with_0600_mode(settings):
    settings.report_secret_value()

    secret_path = settings.data_dir / "report_secret"
    assert secret_path.exists()
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    assert mode == 0o600


def test_report_secret_value_returns_explicit_value_when_set(tmp_path):
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        report_secret="explicit-secret",
    )

    assert settings.report_secret_value() == "explicit-secret"
    assert not (settings.data_dir / "report_secret").exists()


def test_ensure_dirs_creates_data_dir_tree(settings):
    settings.ensure_dirs()

    assert settings.data_dir.is_dir()
    assert (settings.data_dir / "tasks").is_dir()
    assert (settings.data_dir / "calls").is_dir()


def test_data_dir_expands_tilde(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("DATA_DIR", "~/.jarvis-test-does-not-exist")

    settings = Settings(_env_file=None)

    assert "~" not in str(settings.data_dir)
    assert settings.data_dir == Path.home() / ".jarvis-test-does-not-exist"


def test_load_settings_returns_settings_instance(tmp_path):
    settings = load_settings(
        _env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis"
    )
    assert isinstance(settings, Settings)


def test_defaults_match_spec_table(settings):
    assert settings.openai_realtime_model == "gpt-realtime-2.1"
    assert settings.openai_voice == "cedar"
    assert settings.openai_transcription_model == "gpt-4o-mini-transcribe"
    assert settings.anthropic_api_key is None
    assert settings.subagent_model == "claude-opus-5"
    assert settings.subagent_max_turns == 200
    assert settings.subagent_max_budget_usd == 10.0
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.projects_root == (Path.home() / "Local" / "coding_projects")
    assert settings.max_concurrent_tasks == 3
    assert settings.dispatch_wait_max_seconds == 25
    assert settings.local_silence_timeout == 30
    assert settings.max_call_seconds == 1800
    assert settings.daily_task_cap == 50
    assert settings.wakeword_model == "hey_jarvis"
    assert settings.wakeword_threshold == 0.5
    assert settings.log_level == "INFO"
    assert settings.debug_skip_twilio_validation is False
    assert settings.fake_agents is False


def test_debug_skip_twilio_validation_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("DEBUG_SKIP_TWILIO_VALIDATION", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.debug_skip_twilio_validation is True


def test_fake_agents_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("FAKE_AGENTS", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.fake_agents is True


# --- blank optional settings count as unset (spec §3.3 PIN gate) -------------


def test_the_shipped_env_example_leaves_every_optional_setting_unset(tmp_path):
    """`.env.example` ships blank values; not one of them may become an empty string."""
    env_file = tmp_path / ".env"
    env_file.write_text(Path(".env.example").read_text())

    settings = Settings(
        _env_file=env_file, openai_api_key="test", data_dir=tmp_path / "jarvis"
    )

    for name in OPTIONAL_STR_FIELDS:
        assert getattr(settings, name) is None, name
    assert settings.allowed_callers == []
    assert settings.pin is None
    assert settings.owner_number is None


def test_optional_str_fields_covers_every_optional_string_field():
    """The list the blank-is-unset validator is built from must not drift."""
    optional = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation == (str | None)
    }

    assert set(OPTIONAL_STR_FIELDS) == optional


def test_a_blank_pin_is_not_a_pin(tmp_path):
    for blank in ("", "   "):
        settings = Settings(
            _env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis", pin=blank
        )
        assert settings.pin is None


def test_repr_never_leaks_a_secret(tmp_path):
    """`repr(settings)` turns up in logs and tracebacks; the secrets must not."""
    values = {
        "openai_api_key": "openai-secret-value",
        "anthropic_api_key": "anthropic-secret-value",
        "twilio_auth_token": "twilio-secret-value",
        "pin": "424242",
        "report_secret": "report-secret-value",
        "google_oauth_client_secret": "google-secret-value",
    }
    settings = Settings(_env_file=None, data_dir=tmp_path / "jarvis", **values)

    text = repr(settings)

    for name, value in values.items():
        assert value not in text, name
    assert "openai_realtime_model" in text  # the harmless ones are still there


def test_report_secret_value_reads_the_file_only_once(settings, monkeypatch):
    """It is called per notification and per report request: not once per file read."""
    first = settings.report_secret_value()

    def explode(*args, **kwargs):
        raise AssertionError("the report secret was read from disk again")

    monkeypatch.setattr(Path, "read_text", explode)

    assert settings.report_secret_value() == first
