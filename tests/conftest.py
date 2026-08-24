"""Shared pytest fixtures."""

import pytest

from jarvis.config import Settings


def _settings_env_var_names() -> set[str]:
    """Every env var name `Settings` reads: field-name uppercase + any validation alias.

    Derived from `Settings.model_fields` so it can't drift as fields are added/renamed.
    """
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name.upper())
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
    return names


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Strip ambient env vars `Settings` reads so tests are hermetic on any machine/CI."""
    for name in _settings_env_var_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path):
    """A Settings instance with no env/.env leakage, safe for tests.

    `google_client_secrets_file` defaults to a path relative to the working directory, so
    it is pinned into `tmp_path` here: a developer's real client file must never take part
    in a test.
    """
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        google_client_secrets_file=tmp_path / "no-client-secrets.json",
    )
