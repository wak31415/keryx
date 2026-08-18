"""Tests for the shared application state the server and the CLI are built on."""

from jarvis.app import AppState, build_app_state
from jarvis.realtime.openai import OpenAIRealtimeClient


def test_build_app_state_wires_empty_shared_registries(settings):
    state = build_app_state(settings)

    assert isinstance(state, AppState)
    assert state.settings is settings
    assert state.sessions.live() == []
    assert state.registry.schemas() == []
    assert len(state.stream_tokens) == 0


def test_build_app_state_leaves_the_later_pieces_unset(settings):
    state = build_app_state(settings)

    assert (state.store, state.manager, state.notifier, state.twilio_out) == (None,) * 4


def test_the_provider_factory_builds_a_realtime_client_per_call(settings):
    state = build_app_state(settings)

    provider = state.provider_factory()

    assert isinstance(provider, OpenAIRealtimeClient)
    assert provider is not state.provider_factory()


def test_the_provider_factory_passes_the_configured_key_and_model(settings, monkeypatch):
    built: list[tuple] = []
    monkeypatch.setattr("jarvis.app.OpenAIRealtimeClient", lambda *args: built.append(args))
    state = build_app_state(settings)

    assert built == []  # nothing is connected until a call actually arrives
    state.provider_factory()

    assert built == [(settings.openai_api_key, settings.openai_realtime_model)]
