"""Tests for jarvis.config.Settings."""

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.config import (
    OPTIONAL_STR_FIELDS,
    OWNER_FALLBACK,
    Settings,
    load_settings,
    secure_file,
)


def test_allowed_callers_parses_comma_separated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ALLOWED_CALLERS", "+15555555555,+15556666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.allowed_callers == ["+15555555555", "+15556666666"]


def test_allowed_callers_defaults_to_empty_list(settings):
    assert settings.allowed_callers == []


def test_the_default_agent_is_claude_and_alone(settings):
    assert settings.agent_backend == "claude"
    assert settings.enabled_agents == ("claude",)
    assert settings.agent_refusal() is None


def test_agents_enabled_parses_a_comma_list_in_any_case(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AGENTS_ENABLED", " Claude, ")
    monkeypatch.setenv("AGENT_BACKEND", "CLAUDE")

    settings = Settings(_env_file=None, data_dir=tmp_path)

    assert settings.agents_enabled == ["claude"]
    assert settings.agent_backend == "claude"


def test_an_agent_jarvis_does_not_know_fails_the_load(tmp_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_api_key="t", data_dir=tmp_path, agents_enabled="gemini")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_api_key="t", data_dir=tmp_path, agent_backend="gemini")


def test_clusters_parse_from_json_env(monkeypatch, tmp_path):
    """Names are what the model says, so they are matched lower-case."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("CLUSTERS", '{"Alpha": "shared", "beta": "gpu"}')
    monkeypatch.setenv("CLUSTER_SSH_GUARD", "~/bin/guard.sh")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.clusters == {"alpha": "shared", "beta": "gpu"}
    assert settings.cluster_ssh_guard == Path.home() / "bin" / "guard.sh"


@pytest.mark.parametrize(
    "clusters",
    [{"alpha; rm -rf ~": "gpu"}, {"alpha": "gpu && reboot"}, {"": "gpu"}, {"alpha": " "}],
)
def test_a_cluster_that_is_not_a_bare_word_is_refused_at_startup(tmp_path, clusters):
    """Both halves reach a remote shell, so a bad one fails loudly rather than per call."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis", clusters=clusters
        )


def test_a_blank_cluster_guard_is_no_guard(tmp_path):
    settings = Settings(
        _env_file=None, openai_api_key="test", data_dir=tmp_path / "jarvis", cluster_ssh_guard=""
    )

    assert settings.cluster_ssh_guard is None


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
    monkeypatch.setenv("JARVIS_PIN", "123456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.pin == "123456"


def test_pin_defaults_to_none(settings):
    assert settings.pin is None


def test_owner_number_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OWNER_NUMBER", "+15551000000")
    monkeypatch.setenv("ALLOWED_CALLERS", "+15555555555,+15556666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.owner_number == "+15551000000"


def test_owner_number_falls_back_to_first_allowed_caller(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ALLOWED_CALLERS", "+15555555555,+15556666666")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.owner_number == "+15555555555"


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


def test_ensure_dirs_makes_the_whole_tree_owner_only(settings):
    """`calls/*.log` is every word of every call; the default umask would publish it."""
    settings.ensure_dirs()

    for path in (
        settings.data_dir,
        settings.data_dir / "tasks",
        settings.data_dir / "calls",
        settings.data_dir / "approvals",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_ensure_dirs_tightens_a_directory_that_already_exists(settings):
    """An install made before this becomes private the next time anything starts."""
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "calls").mkdir()
    settings.data_dir.chmod(0o755)
    (settings.data_dir / "calls").chmod(0o755)

    settings.ensure_dirs()

    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((settings.data_dir / "calls").stat().st_mode) == 0o700


def test_secure_file_leaves_a_missing_file_alone(tmp_path):
    """Best effort: nothing under `data_dir` is worth refusing to run over."""
    assert secure_file(tmp_path / "not-there") == tmp_path / "not-there"


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


def test_defaults_match_spec_table():
    settings = Settings(_env_file=None, openai_api_key="test")
    assert settings.openai_realtime_model == "gpt-realtime-2.1"
    assert settings.openai_voice == "cedar"
    assert settings.openai_transcription_model == "gpt-4o-mini-transcribe"
    assert settings.anthropic_api_key is None
    assert settings.subagent_model == "claude-opus-5"
    assert settings.subagent_max_turns == 200
    assert settings.subagent_max_budget_usd == 10.0
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.projects_root == Path.home() / "projects"
    assert settings.max_concurrent_tasks == 3
    assert settings.dispatch_wait_max_seconds == 25
    assert settings.vad_mode == "semantic"
    assert settings.vad_eagerness == "medium"
    assert settings.noise_reduction == "auto"
    assert settings.vad_silence_ms == 1200
    assert settings.local_silence_timeout == 30
    assert settings.max_call_seconds == 1800
    assert settings.daily_task_cap == 50
    assert settings.max_phone_sessions == 2
    assert settings.wakeword_model == "hey_jarvis"
    assert settings.wakeword_threshold == 0.5
    assert settings.log_level == "INFO"
    assert settings.debug_skip_twilio_validation is False
    assert settings.fake_agents is False
    assert settings.slack_mcp_server is None
    assert settings.clusters == {}
    assert settings.cluster_ssh_guard is None


def test_debug_skip_twilio_validation_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("DEBUG_SKIP_TWILIO_VALIDATION", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "jarvis"))

    settings = Settings(_env_file=None)

    assert settings.debug_skip_twilio_validation is True


def test_the_phone_server_may_start_with_signatures_checked(tmp_path):
    settings = Settings(_env_file=None, openai_api_key="test", public_host="jarvis.example")

    assert settings.phone_refusal() is None


def test_skipping_signatures_is_allowed_where_no_tunnel_is_named(tmp_path):
    """Local development: nothing tells Twilio, or anyone else, where this machine is."""
    settings = Settings(_env_file=None, openai_api_key="test", debug_skip_twilio_validation=True)

    assert settings.phone_refusal() is None


def test_skipping_signatures_behind_a_public_host_is_refused(tmp_path):
    """With the check off, anyone who can reach the tunnel can pose as Twilio."""
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        debug_skip_twilio_validation=True,
        public_host="jarvis.example",
    )

    refusal = settings.phone_refusal()
    assert refusal is not None
    assert "DEBUG_SKIP_TWILIO_VALIDATION" in refusal
    assert "PUBLIC_HOST" in refusal
    assert "\n" not in refusal


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


def test_the_suites_dotenv_guard_hides_a_real_env_file(tmp_path, monkeypatch):
    """A `.env` in the working directory must not reach a `Settings` built without one.

    `Settings.model_config` names `env_file=".env"`, so before the session-wide guard in
    `conftest._no_dotenv` this read the developer's own credentials off disk — it once put
    a live admin key into pytest output.
    """
    (tmp_path / ".env").write_text("OPENAI_ADMIN_KEY=leaked\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings(openai_api_key="test")

    assert settings.openai_admin_key is None


def test_optional_str_fields_covers_every_optional_string_field():
    """The list the blank-is-unset validator is built from must not drift."""
    optional = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation == (str | None)
    }

    assert set(OPTIONAL_STR_FIELDS) == optional


# --- the PIN is strictly 6-8 digits (spec §5) --------------------------------


@pytest.mark.parametrize("pin", ["123456", "1234567", "12345678"])
def test_a_conforming_pin_is_accepted(tmp_path, pin):
    assert Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path, pin=pin).pin == pin


@pytest.mark.parametrize(
    ("pin", "why"),
    [
        ("12345", "five digits is too few"),
        ("1", "one digit is not a PIN"),
        ("123456789", "nine digits is too many"),
        ("12345a", "a letter cannot be keyed on a phone"),
        ("12 34 56", "nor can a space"),
        ("12-34-56", "nor a separator"),
        ("hunter2", "nor a password"),
    ],
)
def test_a_nonconforming_pin_is_refused(tmp_path, pin, why):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path, pin=pin)


def test_no_pin_at_all_is_still_allowed(tmp_path):
    """Unset means "no PIN", which refuses every dispatch from the phone. That is legal."""
    assert Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path, pin=None).pin is None


def test_the_rejection_names_the_rule_and_never_quotes_the_pin(tmp_path):
    """A refused PIN reaches a journal or a terminal on its way to being fixed."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path, pin="12345")

    message = str(excinfo.value)
    assert "6 to 8 digits" in message
    assert "12345" not in message


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


# --- Google OAuth client -----------------------------------------------------


def test_the_google_client_comes_from_the_env_pair_when_it_is_set(settings):
    settings.google_oauth_client_id = "id-from-env"
    settings.google_oauth_client_secret = "secret-from-env"

    assert settings.google_oauth_client() == ("id-from-env", "secret-from-env")


def test_the_google_client_falls_back_to_the_secrets_file(settings, tmp_path):
    """The console hands out a JSON file; there is no need to copy it into the env."""
    path = tmp_path / "client_secret.json"
    path.write_text(
        json.dumps({"installed": {"client_id": "id-from-file", "client_secret": "shh"}}),
        encoding="utf-8",
    )
    settings.google_client_secrets_file = path

    assert settings.google_oauth_client() == ("id-from-file", "shh")


def test_a_web_client_file_works_too(settings, tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(
        json.dumps({"web": {"client_id": "web-id", "client_secret": "web-secret"}}),
        encoding="utf-8",
    )
    settings.google_client_secrets_file = path

    assert settings.google_oauth_client() == ("web-id", "web-secret")


def test_a_missing_or_broken_secrets_file_just_means_no_google(settings, tmp_path):
    settings.google_client_secrets_file = tmp_path / "nothing-here.json"
    assert settings.google_oauth_client() is None

    broken = tmp_path / "client_secret.json"
    broken.write_text("{not json", encoding="utf-8")
    settings.google_client_secrets_file = broken
    assert settings.google_oauth_client() is None

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    settings.google_client_secrets_file = empty
    assert settings.google_oauth_client() is None


def test_noise_reduction_follows_the_channel_by_default():
    """A phone is held to the head; the Mac's microphone is across the room."""
    settings = Settings(_env_file=None, openai_api_key="test")

    assert settings.noise_reduction_for("phone") == "near_field"
    assert settings.noise_reduction_for("local") == "far_field"


def test_noise_reduction_can_be_turned_off_entirely():
    settings = Settings(_env_file=None, openai_api_key="test", noise_reduction="off")

    assert settings.noise_reduction_for("phone") is None
    assert settings.noise_reduction_for("local") is None


def test_an_explicit_noise_reduction_profile_wins_on_every_channel():
    settings = Settings(_env_file=None, openai_api_key="test", noise_reduction="far_field")

    assert settings.noise_reduction_for("phone") == "far_field"
    assert settings.noise_reduction_for("local") == "far_field"


# --- whom Jarvis works for -------------------------------------------------


def test_the_owner_is_called_by_name_when_one_is_set():
    settings = Settings(_env_file=None, openai_api_key="test", owner_name="  Ada  ")

    assert settings.owner_label == "Ada"


def test_a_blank_owner_name_is_the_owner():
    """`.env.example` ships `OWNER_NAME=` blank, and a blank name is no name."""
    for blank in (None, "", "   "):
        settings = Settings(_env_file=None, openai_api_key="test", owner_name=blank)

        assert settings.owner_name is None
        assert settings.owner_label == OWNER_FALLBACK == "the owner"
