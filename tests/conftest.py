"""Shared pytest fixtures."""

import dataclasses

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


#: The XDG base directories Jarvis resolves its own from, each moved into the test's home.
XDG_HOMES = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Strip ambient env vars `Settings` reads so tests are hermetic on any machine/CI.

    `JARVIS_HOME` too, pointed at a directory of the test's own: `Settings` reads
    `config.toml` and `secrets.toml` from it, and the developer's real ones must never take
    part in a test — nor be written by one. `JARVIS_ACTOR` goes because a suite run by a
    subagent of the live service inherits `service`, and would be refused as one.

    `HOME`, every `XDG_*_HOME` and the working directory move into the test's own
    directory as well. Every default
    Jarvis has for where it keeps things is derived from them, and one check looks for a
    legacy `~/.jarvis` — which on a developer's machine really is there, holding every call
    they ever made.
    """
    for name in _settings_env_var_names():
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for name, relative in XDG_HOMES.items():
        monkeypatch.setenv(name, str(home / relative))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis-home"))
    monkeypatch.delenv("JARVIS_ACTOR", raising=False)
    # The Claude CLI's own directory is derived from `HOME` unless this moves it.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # And the working directory: a `.env` in the checkout the suite runs from is what
    # `jarvis serve` refuses to start beside, and what `jarvis migrate` would import.
    working = tmp_path / "cwd"
    working.mkdir()
    monkeypatch.chdir(working)


@pytest.fixture
def settings(tmp_path):
    """A Settings instance with no env/.env leakage, safe for tests.

    `google_client_secrets_file` defaults to a path relative to the working directory, so
    it is pinned into `tmp_path` here: a developer's real client file must never take part
    in a test. `projects_root` and `skills_dir` default into the home directory, and are
    pinned for the same reason — to places that do not exist, which is a fresh install.
    """
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        google_client_secrets_file=tmp_path / "no-client-secrets.json",
        projects_root=tmp_path / "no-projects",
        skills_dir=tmp_path / "no-skills",
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


@pytest.fixture(autouse=True)
def _outside_any_service(monkeypatch):
    """Never find ourselves inside a service unit, and never ask a real manager about one.

    `SERVICE_MANAGER=auto` reads this process's cgroup and asks `systemctl`/`launchctl`
    whether the unit is installed. A suite run by a subagent of the live service *is*
    inside `jarvis.service`, and would resolve to it. The tests about those two probes
    import the real functions, which this does not reach.
    """
    monkeypatch.setattr("jarvis.restart.service.runs_under", lambda target, **_: False)
    monkeypatch.setattr("jarvis.restart.service.is_installed", lambda target, **_: False)


@pytest.fixture
def every_agent_installed(monkeypatch):
    """As if every agent's extra were installed, whatever this environment has.

    CI installs them all, but a machine synced with one agent runs the suite too; a test
    that is not about which extras are there asks for this rather than depend on it.
    """
    from jarvis import doctor
    from jarvis.agents import registry

    for module in (registry, doctor):
        monkeypatch.setattr(module, "installed", lambda agent: True)
    for name, spec in list(registry.BACKENDS.items()):
        cli = f"/venv/bin/{name}"
        monkeypatch.setitem(
            registry.BACKENDS, name, dataclasses.replace(spec, find_cli=lambda cli=cli: cli)
        )


@pytest.fixture(autouse=True)
def _no_checkout_env(monkeypatch, tmp_path):
    """`jarvis migrate` reads a `.env` and `.secrets/` in the checkout the service runs in,
    and in a test that checkout would be this one — a developer's real one, with every key
    in it. Pointed at a directory of the test's own instead."""
    monkeypatch.setattr("jarvis.cli.repo_root", lambda: tmp_path / "checkout")


@pytest.fixture(autouse=True)
def _no_gh_from_doctor(monkeypatch):
    """`jarvis doctor` runs `gh auth status` while issue reports are on, which asks GitHub.
    Never from a test: `gh` is not installed, unless a test says what it answers."""
    from jarvis.issues import GhStatus

    monkeypatch.setattr("jarvis.doctor.gh_status", lambda: GhStatus(installed=False))


@pytest.fixture(autouse=True)
def _no_twilio_from_doctor(monkeypatch):
    """`jarvis doctor` asks Twilio where the number points when it has credentials. Never
    from a test: the doctor tests hand in a fake client of their own. Returns the real one,
    for the one test about it — never `monkeypatch.undo()`, which would also undo the
    suite's HOME and XDG isolation and read the developer's own settings."""
    from jarvis import cli

    real = cli._twilio_admin
    monkeypatch.setattr(cli, "_twilio_admin", lambda settings: None)
    return real


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """No test reaches the network through httpx, whatever a patch missed.

    The transports are where a real request leaves the process; `httpx.MockTransport`, which
    the tests that want HTTP use, is not one of them and keeps working.
    """
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the network through httpx")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
