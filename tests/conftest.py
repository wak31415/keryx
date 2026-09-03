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


@pytest.fixture(scope="session", autouse=True)
def _no_dotenv():
    """Make the developer's real `.env` unreachable for the whole suite.

    `_isolated_env` strips the ambient environment, but `Settings` also reads
    `env_file=".env"` relative to the working directory, so any `Settings(...)` built
    without an explicit `_env_file=None` — including ones deep inside the code under test —
    picks up whatever credentials are on the machine. That once printed a live
    `OPENAI_ADMIN_KEY` into pytest output. Blanking the setting on the class closes it for
    every construction and needs no cwd juggling; an explicit `_env_file=` argument still
    wins, so the tests that point at a fixture `.env` are unaffected.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch):
    """Render CLI output the same way on every machine — colour and width included.

    Typer draws its help through rich, and rich *with colour on* splits an option name
    across escape sequences: `--no-phone` comes out as `-`, `-no`, `-phone`, each in its
    own styled span, so a test asserting the literal string fails. Locally there is no tty
    and rich stays plain; GitHub Actions sets `FORCE_COLOR`, and that difference alone is
    why four help tests passed here and failed there. Width is pinned for the same class of
    reason — a narrow box wraps option names — and `TERM=dumb` needs `COLUMNS` with it, or
    rich renders nothing at all.
    """
    for forced in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(forced, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")


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


@pytest.fixture
def unwrapped():
    """Collapse whitespace, so an assertion can span a sentence the markdown wrapped.

    The prompt guardrails assert on sentences from `prompts/*.md`, which are hard-wrapped
    at 100 columns. Without this, re-flowing a paragraph fails a test that the rule it
    guards is still perfectly intact.
    """

    def _flat(text: str) -> str:
        return " ".join(text.split())

    return _flat
