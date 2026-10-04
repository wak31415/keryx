"""Tests for the shared auth tiers: one precedence, for every agent."""

import dataclasses

import pytest

from keryx.agents.auth import (
    AuthMode,
    EndpointAuth,
    child_env,
    endpoint_auth,
    redact,
    resolve_auth,
)
from keryx.agents.claude import CLAUDE_AUTH
from keryx.agents.codex import CODEX_AUTH
from keryx.endpoints import Endpoint


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


def test_an_endpoint_is_ready_with_an_address_and_its_key_is_the_secret(settings):
    source = EndpointAuth(endpoint=lambda s: s.local_agent_endpoint, login_hint="set one")
    assert resolve_auth(source, settings).mode is AuthMode.NONE
    assert "set one" in resolve_auth(source, settings).detail

    settings.local_agent_base_url = "http://box:11434"
    settings.local_agent_api_key = "lk-1"
    status = resolve_auth(source, settings)

    assert (status.mode, status.secret, status.variable) == (AuthMode.ENDPOINT, "lk-1", None)
    assert status.ready and "with a key" in status.detail and "lk-1" not in status.detail
    assert child_env(status) == {}  # the harness decides what it travels under


def test_an_endpoint_in_hand_resolves_to_itself_whatever_the_settings(settings):
    endpoint = Endpoint.parse("http://gpu:8080", api_key="lk-2")

    status = resolve_auth(endpoint_auth(endpoint), settings)

    assert status.secret == "lk-2" and "no key" not in status.detail
