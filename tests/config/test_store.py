"""The configuration store: what goes in which file, what `Settings` reads back, and in
what order it looks."""

import os
import stat
import tomllib
from datetime import date
from pathlib import Path

import pytest

from jarvis.config import Settings, jarvis_home, pin_file, read_enrolled_pin, write_enrolled_pin
from jarvis.config.files import dump_toml, read_toml, write_private
from jarvis.config.settings import ConfigFileError
from jarvis.config.store import (
    FROM_DEFAULT,
    FROM_ENV,
    FROM_PIN_FILE,
    FROM_SECRETS,
    ConfigError,
    ConfigStore,
    validate,
)


@pytest.fixture
def store():
    return ConfigStore()


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def load() -> Settings:
    return Settings(_env_file=None, openai_api_key="test")


# --- where things go ------------------------------------------------------------------


def test_the_home_is_jarvis_home(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "elsewhere"))
    assert jarvis_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("JARVIS_HOME")
    assert jarvis_home() == Path.home() / ".config" / "jarvis"


def test_a_plain_setting_goes_in_config_and_a_secret_in_secrets(store):
    store.set({"OPENAI_VOICE": "marin", "TWILIO_AUTH_TOKEN": "tok-123"})

    assert read_toml(store.config_path) == {"OPENAI_VOICE": "marin"}
    assert read_toml(store.secrets_path) == {"TWILIO_AUTH_TOKEN": "tok-123"}


def test_both_files_are_owner_only_in_an_owner_only_directory(store):
    os.umask(0o022)
    store.set({"OPENAI_VOICE": "marin", "OPENAI_API_KEY": "sk-1"})

    assert mode(store.home) == 0o700
    assert mode(store.config_path) == 0o600
    assert mode(store.secrets_path) == 0o600


def test_a_write_leaves_no_temporary_file_behind(store):
    store.set({"OPENAI_VOICE": "marin"})
    store.set({"OPENAI_VOICE": "cedar"})

    assert sorted(path.name for path in store.home.iterdir()) == ["config.toml"]


def test_a_failed_write_leaves_the_old_file_whole(store, monkeypatch):
    store.set({"OPENAI_VOICE": "marin"})

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("jarvis.config.files.os.replace", broken)
    with pytest.raises(OSError):
        store.set({"OPENAI_VOICE": "cedar"})

    assert read_toml(store.config_path) == {"OPENAI_VOICE": "marin"}
    assert sorted(path.name for path in store.home.iterdir()) == ["config.toml"]


def test_values_are_stored_typed_and_read_back(store):
    store.set(
        {
            "PORT": "9000",
            "SMS_ENABLED": "true",
            "AGENTS_ENABLED": "Claude, codex",
            "PROJECTS": '{"orchard": "/srv/orchard", "two words": "/srv/two"}',
        }
    )

    raw = tomllib.loads(store.config_path.read_text())
    assert raw["PORT"] == 9000 and raw["SMS_ENABLED"] is True
    assert raw["AGENTS_ENABLED"] == ["claude", "codex"]
    assert raw["PROJECTS"] == {"orchard": "/srv/orchard", "two words": "/srv/two"}
    settings = load()
    assert settings.port == 9000
    assert settings.projects == {"orchard": "/srv/orchard", "two words": "/srv/two"}


def test_setting_none_removes_a_key(store):
    store.set({"OPENAI_VOICE": "marin", "PORT": 9000})
    store.set({"OPENAI_VOICE": None})

    assert read_toml(store.config_path) == {"PORT": 9000}


def test_unset_says_which_keys_were_there(store):
    store.set({"OPENAI_VOICE": "marin"})

    assert store.unset(["openai_voice", "PORT"]) == ["OPENAI_VOICE"]
    assert store.stored() == {}


def test_a_secret_found_in_config_moves_to_secrets_when_it_is_set(store):
    write_private(store.config_path, dump_toml({"OPENAI_API_KEY": "hand-edited"}))
    assert store.secrets_in_config() == ["OPENAI_API_KEY"]

    store.set({"OPENAI_API_KEY": "sk-new"})

    assert store.secrets_in_config() == []
    assert read_toml(store.secrets_path) == {"OPENAI_API_KEY": "sk-new"}


# --- validation -----------------------------------------------------------------------


def test_an_invalid_value_is_refused_and_nothing_is_written(store):
    with pytest.raises(ConfigError, match="PORT"):
        store.set({"OPENAI_VOICE": "marin", "PORT": "eighty"})

    assert not store.config_path.exists()


def test_an_unknown_key_is_refused(store):
    with pytest.raises(ConfigError, match="no setting called NOPE"):
        store.set({"NOPE": "1"})


def test_the_pin_is_never_a_stored_setting(store):
    with pytest.raises(ConfigError, match="jarvis setup"):
        store.set({"JARVIS_PIN": "482915"})


def test_a_pin_hand_written_into_a_file_is_not_read(store, tmp_path):
    write_private(store.secrets_path, dump_toml({"JARVIS_PIN": "482915"}))

    assert Settings(_env_file=None, openai_api_key="x", data_dir=tmp_path / "d").pin is None


def test_a_refused_value_is_not_quoted_back(store):
    with pytest.raises(ConfigError) as caught:
        store.set({"PORT": "sk-secret-looking"})

    assert "sk-secret-looking" not in str(caught.value)


def test_validate_reports_every_problem_at_once():
    with pytest.raises(ConfigError) as caught:
        validate({"PORT": "x", "MAX_PHONE_SESSIONS": "0"})

    assert "PORT" in str(caught.value) and "MAX_PHONE_SESSIONS" in str(caught.value)


# --- precedence -----------------------------------------------------------------------


def test_the_environment_beats_both_files_and_secrets_beat_config(store, monkeypatch):
    write_private(store.config_path, dump_toml({"OPENAI_VOICE": "from-config"}))
    write_private(store.secrets_path, dump_toml({"OPENAI_VOICE": "from-secrets"}))
    assert load().openai_voice == "from-secrets"
    assert store.source_of("OPENAI_VOICE") == FROM_SECRETS

    monkeypatch.setenv("OPENAI_VOICE", "from-env")

    assert load().openai_voice == "from-env"
    assert store.source_of("OPENAI_VOICE") == FROM_ENV


def test_a_env_in_the_working_directory_is_never_read(store):
    """A checkout is the one place a secret must never live; `jarvis migrate` moves it."""
    Path(".env").write_text("OPENAI_VOICE=from-dotenv\nPORT=7000\n")

    settings = Settings(openai_api_key="x")

    assert (settings.openai_voice, settings.port) == ("cedar", 8080)
    assert store.source_of("PORT") == FROM_DEFAULT


def test_the_code_beats_everything(store):
    store.set({"PORT": 9000})

    assert Settings(_env_file=None, openai_api_key="x", port=1234).port == 1234


def test_the_pin_file_is_a_source_of_its_own(store, settings):
    assert store.source_of("JARVIS_PIN", settings) == FROM_DEFAULT
    write_enrolled_pin(settings.config_dir, "482915")

    assert store.source_of("JARVIS_PIN", settings) == FROM_PIN_FILE


def test_a_config_file_that_does_not_parse_says_so(store):
    store.home.mkdir(parents=True)
    store.config_path.write_text("PORT = = 1\n")

    with pytest.raises(ConfigFileError, match="does not parse"):
        load()


# --- service permissions --------------------------------------------------------------


def test_the_service_may_write_a_service_writable_key(store):
    store.set({"OPENAI_VOICE": "marin"}, actor="service")

    assert store.stored() == {"OPENAI_VOICE": "marin"}


def test_the_service_may_not_write_a_key_nobody_unlocked(store):
    with pytest.raises(ConfigError, match="not unlocked"):
        store.set({"PROJECTS_ROOT": "/tmp"}, actor="service")


def test_the_service_may_not_write_a_protected_key_even_unlocked_by_hand(store):
    write_private(
        store.config_path, dump_toml({"service_writable": {"ALLOWED_CALLERS": True}})
    )

    with pytest.raises(ConfigError, match="protected"):
        store.set({"ALLOWED_CALLERS": "+15550000000"}, actor="service")


def test_lock_and_unlock_are_the_owners_override(store):
    store.lock("OPENAI_VOICE")
    with pytest.raises(ConfigError):
        store.set({"OPENAI_VOICE": "marin"}, actor="service")

    store.unlock("PROJECTS_ROOT")
    store.set({"PROJECTS_ROOT": "/srv"}, actor="service")

    assert store.overrides() == {"OPENAI_VOICE": False, "PROJECTS_ROOT": True}


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "ALLOWED_CALLERS", "PIN_LOCKOUT_MINUTES"])
def test_a_protected_key_cannot_be_unlocked(store, key):
    with pytest.raises(ConfigError, match="protected"):
        store.unlock(key)
    assert store.overrides() == {}


def test_the_service_may_switch_agents_only_among_those_enabled(store, settings):
    with pytest.raises(ConfigError, match="enabled agents"):
        store.set({"AGENT_BACKEND": "codex"}, actor="service", settings=settings)

    both = settings.model_copy(update={"agents_enabled": ["claude", "codex"]})
    store.set({"AGENT_BACKEND": "codex"}, actor="service", settings=both)


def test_walked_sections_are_remembered(store):
    store.mark_walked("google")
    store.mark_walked("phone")
    store.mark_walked("google", walked=False)

    assert store.walked_sections() == ["phone"]
    assert store.stored() == {}


# --- importing a legacy .env ------------------------------------------------------------


def write_env(tmp_path, text):
    path = tmp_path / "repo" / ".env"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def test_import_env_moves_values_and_renames_the_file(store, tmp_path):
    env = write_env(
        tmp_path,
        "OPENAI_API_KEY=sk-live\nOPENAI_VOICE=marin\nPORT=8080\nWHATEVER=1\n"
        f"DATA_DIR={tmp_path / 'data'}\nSUBAGENT_MODEL=claude-opus-5\n",
    )

    report = store.import_env(env, today=date(2026, 9, 27))

    assert sorted(report.imported) == ["DATA_DIR", "OPENAI_API_KEY", "OPENAI_VOICE"]
    assert sorted(report.defaults) == ["PORT", "SUBAGENT_MODEL"]
    assert report.unknown == ["WHATEVER"]
    assert read_toml(store.secrets_path) == {"OPENAI_API_KEY": "sk-live"}
    assert not env.exists()
    assert report.renamed_to == env.with_name(".env.imported-2026-09-27")
    assert report.renamed_to.exists()


def test_import_env_never_overwrites_an_earlier_import(store, tmp_path):
    for _ in range(2):
        env = write_env(tmp_path, "OPENAI_VOICE=marin\n")
        store.import_env(env, today=date(2026, 9, 27))

    assert (tmp_path / "repo" / ".env.imported-2026-09-27-2").exists()


def test_import_env_resolves_relative_paths_against_the_env_file(store, tmp_path):
    env = write_env(tmp_path, "DATA_DIR=state\n")

    store.import_env(env)

    assert store.stored()["DATA_DIR"] == str(tmp_path / "repo" / "state")


def test_import_env_moves_the_pin_to_its_own_file(store, tmp_path):
    env = write_env(tmp_path, f"JARVIS_PIN=482915\nDATA_DIR={tmp_path / 'data'}\n")

    report = store.import_env(env)

    assert report.pin == "moved to JARVIS_HOME/pin"
    assert pin_file(store.home).read_text().strip() == "482915"
    assert "JARVIS_PIN" not in store.stored()


def test_import_env_refuses_everything_over_a_different_enrolled_pin(store, tmp_path):
    write_enrolled_pin(store.home, "111357")
    env = write_env(tmp_path, "JARVIS_PIN=482915\nOPENAI_VOICE=marin\n")

    with pytest.raises(ConfigError, match="Nothing was imported"):
        store.import_env(env)

    assert env.exists() and store.stored() == {}
    assert pin_file(store.home).read_text().strip() == "111357"


def test_import_env_refuses_an_invalid_file_whole(store, tmp_path):
    env = write_env(tmp_path, "OPENAI_VOICE=marin\nPORT=eighty\n")

    with pytest.raises(ConfigError, match="PORT"):
        store.import_env(env)

    assert env.exists() and store.stored() == {}


def test_import_env_copies_the_legacy_google_client_file(store, tmp_path):
    env = write_env(tmp_path, f"DATA_DIR={tmp_path / 'data'}\n")
    client = env.parent / ".secrets" / "client_secret.json"
    client.parent.mkdir()
    client.write_text('{"installed": {"client_id": "id", "client_secret": "shh"}}')

    report = store.import_env(env)

    assert report.client_file == store.home / "google_client_secret.json"
    assert mode(report.client_file) == 0o600
    settings = Settings(_env_file=None, openai_api_key="x")
    assert settings.google_oauth_client() == ("id", "shh")


def test_import_env_refuses_a_client_file_that_is_not_one(store, tmp_path):
    env = write_env(tmp_path, "GOOGLE_CLIENT_SECRETS_FILE=client.json\n")
    (env.parent / "client.json").write_text("{}")

    with pytest.raises(ConfigError, match="not a Google OAuth client"):
        store.import_env(env)


# --- what the review found -------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [("LOG_LEVEL", "verbose"), ("MAX_CONCURRENT_TASKS", "0"), ("VAD_THRESHOLD", "7"),
     ("MAX_CALL_SECONDS", "-5"), ("PORT", "0")],
)
def test_a_value_that_would_stop_serve_is_refused(store, key, value):
    with pytest.raises(ConfigError, match=key):
        store.set({key: value}, actor="service")
    assert store.stored() == {}


@pytest.mark.parametrize("key", ["SUBAGENT_TIMEOUT_S", "MAX_CALL_SECONDS", "LOCAL_SILENCE_TIMEOUT"])
def test_the_service_may_tune_a_limit_but_never_lift_it(store, key):
    store.set({key: "900"}, actor="service")

    with pytest.raises(ConfigError, match="switch .* off"):
        store.set({key: "0"}, actor="service")
    store.set({key: "0"})  # the owner may


def test_the_service_names_an_agent_in_any_case(store, settings):
    both = settings.model_copy(update={"agents_enabled": ["claude", "codex"]})

    store.set({"AGENT_BACKEND": " Codex "}, actor="service", settings=both)

    assert store.stored()["AGENT_BACKEND"] == "codex"


def test_import_env_is_the_owners_alone(store, tmp_path):
    env = write_env(tmp_path, "ALLOWED_CALLERS=+15550000000\nJARVIS_PIN=482915\n")

    with pytest.raises(ConfigError, match="only the owner"):
        store.import_env(env, actor="service")

    assert env.exists() and store.stored() == {}


def test_import_env_puts_the_pin_beside_the_store_wherever_data_lives(
    store, tmp_path, monkeypatch
):
    """Where the PIN is must not depend on a setting: `DATA_DIR` moves nothing of it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "from-env"))
    env = write_env(tmp_path, f"JARVIS_PIN=482915\nDATA_DIR={tmp_path / 'from-dotenv'}\n")

    store.import_env(env)

    assert read_enrolled_pin(store.home) == "482915"
    assert not (tmp_path / "from-env").exists()
    assert not (tmp_path / "from-dotenv").exists()


def test_import_env_from_a_relative_path_stores_absolute_paths(store, tmp_path, monkeypatch):
    write_env(tmp_path, "DATA_DIR=./data\n")
    monkeypatch.chdir(tmp_path / "repo")

    store.import_env(Path(".env"))

    assert store.stored()["DATA_DIR"] == str(tmp_path / "repo" / "data")


def test_import_env_keeps_what_the_store_already_had(store, tmp_path):
    store.set({"OPENAI_API_KEY": "sk-new"})
    env = write_env(tmp_path, "OPENAI_API_KEY=sk-stale\nOPENAI_VOICE=marin\n")

    report = store.import_env(env)

    assert report.kept == ["OPENAI_API_KEY"]
    assert read_toml(store.secrets_path) == {"OPENAI_API_KEY": "sk-new"}
    assert store.stored()["OPENAI_VOICE"] == "marin"


def test_the_renamed_env_is_owner_only(store, tmp_path):
    env = write_env(tmp_path, "OPENAI_API_KEY=sk\n")
    env.chmod(0o664)

    report = store.import_env(env)

    assert mode(report.renamed_to) == 0o600
