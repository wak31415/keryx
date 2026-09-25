"""Tests for the shared auth tiers: one precedence, for every agent."""

import dataclasses

import pytest

from jarvis.agents.auth import AuthMode, child_env, redact, resolve_auth
from jarvis.agents.claude import CLAUDE_AUTH
from jarvis.agents.codex import CODEX_AUTH


def with_login(source, present: bool):
    return dataclasses.replace(source, stored_login=lambda: present)


@pytest.mark.parametrize(
    ("source", "key_field", "token_field", "key_var", "token_var"),
    [
        (CLAUDE_AUTH, "anthropic_api_key", "claude_code_oauth_token",
         "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        (CODEX_AUTH, "codex_api_key", "codex_access_token", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    ],
    ids=["claude", "codex"],
)
def test_key_beats_token_beats_login_beats_nothing(
    settings, source, key_field, token_field, key_var, token_var
):
    source = with_login(source, True)
    setattr(settings, key_field, "the-key")
    setattr(settings, token_field, "the-token")
    status = resolve_auth(source, settings)
    assert (status.mode, status.variable, status.secret) == (AuthMode.API_KEY, key_var, "the-key")

    setattr(settings, key_field, None)
    status = resolve_auth(source, settings)
    assert (status.mode, status.variable) == (AuthMode.TOKEN, token_var)

    setattr(settings, token_field, None)
    assert resolve_auth(source, settings).mode is AuthMode.SUBSCRIPTION

    status = resolve_auth(with_login(source, False), settings)
    assert status.mode is AuthMode.NONE
    assert not status.ready
    assert "no login found" in status.detail


def test_without_a_probe_a_login_is_assumed(settings):
    """A runner does not look; the agent's own CLI finds the login by itself."""
    status = resolve_auth(with_login(CODEX_AUTH, False), settings, probe=False)

    assert status.mode is AuthMode.SUBSCRIPTION


def test_the_secret_is_never_in_the_repr(settings):
    settings.codex_api_key = "sk-hidden"

    assert "sk-hidden" not in repr(resolve_auth(CODEX_AUTH, settings))


def test_the_child_gets_the_credential_under_its_variable_or_nothing(settings):
    settings.anthropic_api_key = "k"
    assert child_env(resolve_auth(CLAUDE_AUTH, settings)) == {"ANTHROPIC_API_KEY": "k"}

    settings.anthropic_api_key = None
    assert child_env(resolve_auth(CLAUDE_AUTH, settings, probe=False)) == {}


def test_redact_removes_the_secret_and_anything_masked_like_a_key():
    text = "bad key sk-live-12345; provider says: Incorrect API key provided: sk-jarvi****fake."

    cleaned = redact(text, ["sk-live-12345", None, ""])

    assert "sk-live-12345" not in cleaned
    assert "sk-jarvi" not in cleaned and "fake" not in cleaned
    assert cleaned.count("[redacted]") == 2
